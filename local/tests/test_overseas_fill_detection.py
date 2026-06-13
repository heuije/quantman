"""해외(미국) 체결 감지 — 날짜창(RC1)·예약주문 번호공간(RC2) 결함 재현/회귀 가드.

2026-06-12 라이브 실증(δ):
  RC1 — `_overseas_ccnl_today`가 KST 당일만 조회하는데 KIS 해외 체결행의 주문일자는
        **미국 현지 날짜**다. KST 04:55 발주 매도(GOOG 0000040620)의 체결행 주문일자가
        20260611이라 20260612 조회는 0행 → status 'unknown' → 체결 미기록.
  RC2 — 예약주문 접수번호(448, 예약 번호공간)와 체결조회 odno(10자리 주문 번호공간)가
        불일치 → odno 정확 매칭이 영원히 실패 → 'unknown'.

실계좌·네트워크 없이 _get_retry/_overseas_ccnl_today를 가로채 파라미터·매칭만 검증한다.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))

from localapp import kis_broker as kb
from localapp.kis_broker import KisBroker, canonical_odno


def _broker():
    """__init__(자격증명 로드) 우회 — 조회에 필요한 속성만 주입."""
    b = KisBroker.__new__(KisBroker)
    b.virtual = True
    b.cano = "12345678"
    b.acnt_cd = "01"
    b._us_excd = lambda s: "NASD"
    return b


# ── RC1: 조회 날짜창 = [미국 현지 D-1, KST 오늘] ─────────────────────────────


def test_query_window_kst_after_midnight():
    """KST 자정 후(미장 진행 중) — 미국 현지는 전날. 창이 미국 현지 D-1까지 덮어야 한다.

    UTC 2026-06-11 18:00 = KST 06-12 03:00 = 미국 동부(EDT) 06-11 14:00.
    실증 결함 시각대: 이때 KST 당일(20260612)만 조회하면 당일 세션 체결(주문일자
    20260611)이 0행이었다.
    """
    now = datetime(2026, 6, 11, 18, 0, tzinfo=timezone.utc)
    start, end = kb._overseas_query_window(now)
    assert start == "20260610"          # 미국 현지(06-11) D-1
    assert end == "20260612"            # KST 오늘


def test_query_window_us_morning_session():
    """미장 초반(KST 23:00) — 날짜 일치 구간에서도 창이 [D-1, D]를 유지."""
    now = datetime(2026, 6, 12, 14, 0, tzinfo=timezone.utc)   # ET 10:00 / KST 23:00
    start, end = kb._overseas_query_window(now)
    assert start == "20260611"
    assert end == "20260612"


def test_ccnl_query_uses_window_and_start_override(monkeypatch):
    """_overseas_ccnl_today가 날짜창을 파라미터에 싣고, 더 이른 start 요청을 존중한다."""
    b = _broker()
    seen = {}

    def fake_get(path, tr, params, **kw):
        seen.update(path=path, tr=tr, params=params)
        return {"output": []}

    b._get_retry = fake_get
    monkeypatch.setattr(kb, "_overseas_query_window",
                        lambda now=None: ("20260610", "20260612"))

    b._overseas_ccnl_today("GOOG")
    assert seen["params"]["ORD_STRT_DT"] == "20260610"
    assert seen["params"]["ORD_END_DT"] == "20260612"

    # 오래된 pending 추적: 제출일 기반 더 이른 start가 창보다 우선
    b._overseas_ccnl_today("GOOG", start_yyyymmdd="20260605")
    assert seen["params"]["ORD_STRT_DT"] == "20260605"
    assert seen["params"]["ORD_END_DT"] == "20260612"


# ── RC2: 예약주문 — 종목+매수매도+수량 매칭 (접수번호 의존 금지) ────────────────


_ROW_GOOG_BUY = {
    "odno": "0000040620", "pdno": "GOOG", "sll_buy_dvsn_cd": "02",
    "ft_ord_qty": "263", "ft_ccld_qty": "263", "ft_ccld_unpr3": "344.81",
    "prcs_stat_name": "완료", "rjct_rson_name": "",
}


def _status(b, order_no, rows, hint):
    b._overseas_ccnl_today = lambda symbol, start_yyyymmdd=None: rows
    return b._overseas_order_status(order_no, "GOOG", hint=hint)


def test_exact_odno_match_unchanged():
    """일반 주문(접수=체결 번호공간 일치)은 기존대로 정확 매칭."""
    b = _broker()
    st = _status(b, "0000040620", [_ROW_GOOG_BUY], hint=None)
    assert st["status"] == "filled"
    assert st["filled_qty"] == 263
    assert st["fill_price"] == 344.81


def test_reserved_order_matches_by_symbol_side_qty():
    """예약주문(접수 448 ≠ 체결 0000040620) — hint로 종목+사이드+수량 매칭."""
    b = _broker()
    hint = {"reserved": True, "side": "buy", "qty": 263, "exclude_odnos": []}
    st = _status(b, "448", [_ROW_GOOG_BUY], hint=hint)
    assert st["status"] == "filled", "RC2 미수정: 예약주문 체결을 영원히 unknown 처리"
    assert st["filled_qty"] == 263
    assert st["fill_price"] == 344.81
    assert canonical_odno(st["exec_odno"]) == canonical_odno("0000040620")


def test_reserved_match_requires_hint():
    """hint 없으면(일반 폴링·구 호출자) 예약 매칭을 시도하지 않는다 — 보수적."""
    b = _broker()
    st = _status(b, "448", [_ROW_GOOG_BUY], hint=None)
    assert st["status"] == "unknown"


def test_reserved_match_respects_exclude():
    """이미 다른 주문이 청구한 체결행은 재청구 금지(이중 기장 차단)."""
    b = _broker()
    hint = {"reserved": True, "side": "buy", "qty": 263,
            "exclude_odnos": [canonical_odno("0000040620")]}
    st = _status(b, "448", [_ROW_GOOG_BUY], hint=hint)
    assert st["status"] == "unknown"


def test_reserved_match_filters_side_and_qty():
    """사이드·수량이 다른 행은 내 주문이 아니다."""
    b = _broker()
    sell_row = dict(_ROW_GOOG_BUY, sll_buy_dvsn_cd="01")
    other_qty = dict(_ROW_GOOG_BUY, ft_ord_qty="100", ft_ccld_qty="100")
    hint = {"reserved": True, "side": "buy", "qty": 263, "exclude_odnos": []}
    assert _status(b, "448", [sell_row], hint=hint)["status"] == "unknown"
    assert _status(b, "448", [other_qty], hint=hint)["status"] == "unknown"


def test_reserved_match_deterministic_first_by_odno():
    """동형 행이 여럿이면 odno 오름차순 첫 행 — 호출자가 exclude로 1:1 배정."""
    b = _broker()
    r1 = dict(_ROW_GOOG_BUY, odno="0000040625", ft_ccld_unpr3="345.00")
    r2 = dict(_ROW_GOOG_BUY)                      # 0000040620
    hint = {"reserved": True, "side": "buy", "qty": 263, "exclude_odnos": []}
    st = _status(b, "448", [r1, r2], hint=hint)
    assert canonical_odno(st["exec_odno"]) == canonical_odno("0000040620")
    hint2 = {"reserved": True, "side": "buy", "qty": 263,
             "exclude_odnos": [canonical_odno("0000040620")]}
    st2 = _status(b, "449", [r1, r2], hint=hint2)
    assert canonical_odno(st2["exec_odno"]) == canonical_odno("0000040625")


def test_reserved_rejected_row_maps_cancelled():
    """예약주문이 개장에 거부된 경우 — cancelled로 pending 회수."""
    b = _broker()
    rej = dict(_ROW_GOOG_BUY, ft_ccld_qty="0", ft_ccld_unpr3="0",
               prcs_stat_name="거부", rjct_rson_name="증거금부족")
    hint = {"reserved": True, "side": "buy", "qty": 263, "exclude_odnos": []}
    st = _status(b, "448", [rej], hint=hint)
    assert st["status"] == "cancelled"

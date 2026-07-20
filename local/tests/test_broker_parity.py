"""LS 브로커 엔진 패리티 회귀 (P1·P3·P4·P5).

P1 — Broker Protocol의 account_snapshot 시그니처가 실제 구현과 일치
P3 — reconcile_submitting이 _daily_ccld 없는 브로커에서 AttributeError 없이 graceful skip
P4 — make_quote_ws/make_order_ws 팩토리가 브로커별 WS(kis/ls)를 선택
P5 — cancel(partial=…) 부분취소 계약을 Protocol·4어댑터·라우터가 모두 지원
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


# ── P1: Protocol account_snapshot 시그니처 ─────────────────────────────────────

def test_p1_kis_broker_account_snapshot_has_overseas_param():
    """KisBroker.account_snapshot이 overseas kwarg를 받는다."""
    from localapp.kis_broker import KisBroker
    sig = inspect.signature(KisBroker.account_snapshot)
    assert "overseas" in sig.parameters, (
        "KisBroker.account_snapshot에 'overseas' 파라미터가 없습니다"
    )


def test_p1_ls_broker_account_snapshot_has_overseas_param():
    """LsBroker.account_snapshot이 overseas kwarg를 받는다."""
    from localapp.ls_broker import LsBroker
    sig = inspect.signature(LsBroker.account_snapshot)
    assert "overseas" in sig.parameters, (
        "LsBroker.account_snapshot에 'overseas' 파라미터가 없습니다"
    )


def test_p1_protocol_account_snapshot_has_overseas_param():
    """Broker Protocol 선언도 overseas 파라미터를 명시한다."""
    from localapp.broker import Broker
    sig = inspect.signature(Broker.account_snapshot)
    assert "overseas" in sig.parameters, (
        "Broker Protocol의 account_snapshot에 'overseas' 파라미터가 없습니다"
    )


def test_p1_overseas_defaults_to_true_on_all():
    """overseas 파라미터의 기본값이 True (Protocol·KIS·LS 전부)."""
    from localapp.broker import Broker
    from localapp.kis_broker import KisBroker
    from localapp.ls_broker import LsBroker

    for cls in (Broker, KisBroker, LsBroker):
        sig = inspect.signature(cls.account_snapshot)
        param = sig.parameters.get("overseas")
        assert param is not None, f"{cls.__name__}.account_snapshot에 overseas 없음"
        assert param.default is True, (
            f"{cls.__name__}.account_snapshot(overseas) 기본값이 True가 아님: "
            f"{param.default!r}"
        )


# ── P3: reconcile_submitting — _daily_ccld 없는 브로커에서 graceful skip ──────

def test_p3_ls_broker_no_daily_ccld_no_raise(tmp_path):
    """_daily_ccld 없는 브로커로 reconcile_submitting을 호출해도 AttributeError 없이 반환."""
    from localapp import intents

    # submitting intent 하나 준비
    jpath = tmp_path / "intents.jsonl"
    intents.begin("2026-06-19", "iid-ls-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)

    class FakeLs:
        """_daily_ccld 없음 — LS 브로커 시뮬레이션."""

    broker = FakeLs()
    assert not hasattr(broker, "_daily_ccld")

    # AttributeError 없이 반환해야 함
    result = intents.reconcile_submitting(broker, "2026-06-19", path=jpath)

    # KR intent는 kr_rows=None → kis_query_failed로 집계
    assert isinstance(result, dict), "reconcile_submitting이 dict를 반환해야 합니다"
    assert result["kis_query_failed"] == 1, (
        f"_daily_ccld 미지원 브로커의 KR intent는 kis_query_failed여야 합니다: {result}"
    )


def test_p3_ls_no_daily_ccld_is_conservative_gate(tmp_path):
    """_daily_ccld 없는 브로커 → submitting 게이트 유지(보수적) — 이중 발주 차단."""
    from localapp import intents

    jpath = tmp_path / "intents.jsonl"
    intents.begin("2026-06-19", "iid-ls-2", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)

    class FakeLs:
        pass

    intents.reconcile_submitting(FakeLs(), "2026-06-19", path=jpath)

    # submitting 상태 유지 → is_active True (게이트 잠김)
    assert intents.is_active("2026-06-19", 42, "005930", "buy", path=jpath) is True, (
        "reconcile skip 후 submitting 게이트가 유지(보수적)돼야 합니다"
    )


def test_p3_kis_path_unchanged(tmp_path, monkeypatch):
    """KIS 브로커(공개 seam 지원)는 매칭 경로 정상 — R2-③에서 조회가
    daily_orders_today(라우터 관통 공개 seam)로 이관됐다."""
    from unittest.mock import MagicMock
    from localapp import intents

    monkeypatch.setattr(intents, "_NO_FILL_MIN_AGE_SEC", 0)   # 지금-생성 intent 검증용
    jpath = tmp_path / "intents.jsonl"
    intents.begin("2026-06-19", "iid-kis-1", 42, "T", "005930", "buy", 10, 70000,
                  path=jpath)

    broker = MagicMock()
    broker.daily_orders_today.return_value = [
        {"pdno": "005930", "sll_buy_dvsn_cd": "02",
         "ord_qty": 10, "ord_unpr": 70000, "odno": "ORD-K1",
         "cncl_yn": "N"}
    ]

    result = intents.reconcile_submitting(broker, "2026-06-19", path=jpath)

    # KIS 경로: 매칭 1건 → matched=1
    assert result["matched"] == 1, f"KIS 경로 matched 불일치: {result}"
    broker.daily_orders_today.assert_called_once()


# ── P4: 브로커별 WS 팩토리 (make_quote_ws / make_order_ws) ──────────────────
# 구 _should_start_kis_ws 게이트가 팩토리로 일반화됨 — KIS는 기존 KisWebSocket/
# KisOrderWebSocket을 그대로 받고(byte-identical), LS도 전용 WS를 start한다.


class _FakeWsBroker:
    """WS 팩토리 구성용 최소 브로커 — 네트워크 호출 없음."""

    virtual = True

    def _token(self):
        return "TOK"


def _patch_active_broker(monkeypatch, kind):
    from localapp import intraday_loop, secrets_store as s
    monkeypatch.setattr(s, "get_active_broker", lambda: kind)
    # intraday_loop도 같은 함수 참조를 사용하므로 모듈 속성도 패치
    monkeypatch.setattr(intraday_loop, "get_active_broker", lambda: kind)


def test_p4_quote_ws_ls_is_ls_websocket(monkeypatch):
    """LS 활성 → make_quote_ws가 LsWebSocket을 만든다(이전엔 시세 WS skip)."""
    from localapp import intraday_loop
    from localapp.ls_websocket import LsWebSocket
    _patch_active_broker(monkeypatch, "ls")
    ws = intraday_loop.make_quote_ws(_FakeWsBroker(), lambda s, p: None)
    assert isinstance(ws, LsWebSocket)


def test_p4_quote_ws_kis_is_kis_websocket(monkeypatch):
    """KIS 활성 → make_quote_ws가 KisWebSocket을 만든다(기존 경로 불변)."""
    from localapp import intraday_loop
    from localapp.kis_websocket import KisWebSocket
    _patch_active_broker(monkeypatch, "kis")
    ws = intraday_loop.make_quote_ws(_FakeWsBroker(), lambda s, p: None)
    assert isinstance(ws, KisWebSocket)


def test_p4_order_ws_ls_normalizes_krx_market(monkeypatch):
    """LS 활성 → make_order_ws가 LsOrderWebSocket·루프 'KRX'를 'KR'로 정규화.

    회귀 방지: 정규화 없으면 LsOrderWebSocket(market='KRX')가 ValueError를 던져
    loop-start가 크래시한다(2026-06-25 라이브 실측). 실제 호출부 market은 'KRX'.
    """
    from localapp import intraday_loop
    from localapp.ls_order_websocket import LsOrderWebSocket
    _patch_active_broker(monkeypatch, "ls")
    ws = intraday_loop.make_order_ws(_FakeWsBroker(), lambda e: None, "KRX")
    assert isinstance(ws, LsOrderWebSocket)
    assert ws._market == "KR"   # 'KRX' → 'KR' 정규화


def test_p4_order_ws_ls_us_market_kept(monkeypatch):
    """LS US 시장은 'US' 유지."""
    from localapp import intraday_loop
    _patch_active_broker(monkeypatch, "ls")
    ws = intraday_loop.make_order_ws(_FakeWsBroker(), lambda e: None, "US")
    assert ws._market == "US"


def test_p4_order_ws_kis_with_hts_is_kis_order_websocket(monkeypatch):
    """KIS 활성 + hts_id → KisOrderWebSocket(기존 경로 불변·market 그대로 전달)."""
    from localapp import intraday_loop
    from localapp.kis_order_websocket import KisOrderWebSocket
    _patch_active_broker(monkeypatch, "kis")
    monkeypatch.setattr(intraday_loop, "load_kis", lambda: {"hts_id": "myhts"})
    ws = intraday_loop.make_order_ws(_FakeWsBroker(), lambda e: None, "KR")
    assert isinstance(ws, KisOrderWebSocket)


def test_p4_order_ws_kis_no_hts_returns_none(monkeypatch):
    """KIS 활성 + hts_id 없음 → None(REST 폴링이 체결 인지 fallback) — 기존 동작 불변."""
    from localapp import intraday_loop
    _patch_active_broker(monkeypatch, "kis")
    monkeypatch.setattr(intraday_loop, "load_kis", lambda: {})
    ws = intraday_loop.make_order_ws(_FakeWsBroker(), lambda e: None, "KRX")
    assert ws is None


# ── P5: 부분취소(partial) 계약 — Protocol·4어댑터·라우터 동시 지원 ─────────────
# 동시호가 가드가 목표 초과 수동 미체결에서 "초과분만" 걷으려면 취소 수량 의사를
# 어댑터까지 전달해야 한다. KIS 계열은 잔량전부 플래그가 수량을 지배하고(실측
# 2026-07-20 모의 국내선물: 3계약 미체결에 ORD_QTY=1 취소 시 RMN_QTY_YN "N"→잔량 2,
# "Y"→잔량 0으로 남은 2 전부 취소), LS 계열은 대응 필드가 없어 인자를 받되 쓰지
# 않는다. 한 구현이라도 인자를 안 받으면 **그 브로커를 쓰는 유저에게서만** 가드
# 부분취소가 TypeError로 조용히 실패한다(주식 창은 LsBroker 경로까지 도달).

def test_p5_cancel_accepts_partial_kwarg_on_every_broker():
    """cancel(partial=…)이 Protocol·KIS/LS 주식·KIS/LS 선물·라우터 전부에 있다."""
    from localapp.broker import Broker
    from localapp.broker_router import BrokerRouter
    from localapp.kis_broker import KisBroker
    from localapp.kis_futures_broker import KisFuturesBroker
    from localapp.ls_broker import LsBroker
    from localapp.ls_futures_broker import LsFuturesBroker

    for owner in (Broker, KisBroker, KisFuturesBroker, LsBroker,
                  LsFuturesBroker, BrokerRouter):
        p = inspect.signature(owner.cancel).parameters.get("partial")
        assert p is not None, f"{owner.__name__}.cancel에 partial 파라미터가 없습니다"
        assert p.kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{owner.__name__}.cancel의 partial은 키워드 전용이어야 합니다"
            " — 기존 3-인자 호출자(trader·gui·verify) 무영향 보장")
        assert p.default is False, (
            f"{owner.__name__}.cancel의 partial 기본값은 False(전량 취소)여야 합니다")


def test_p5_kis_domestic_cancel_body_flag_follows_partial():
    """KIS 국내주식 취소 바디 QTY_ALL_ORD_YN — 기본 Y(전량) · partial이면 N(일부).

    KB 정의 'Y@전량 N@일부'(docs/kis-api/.../TTTC0013U_주식주문-정정취소.md).
    국내주식 자체 실측은 없으나 같은 성격의 선물 필드(RMN_QTY_YN)가 실측
    2026-07-20에서 문서대로 동작했다 — Y는 ORD_QTY를 무시하고 잔량 전부를 취소한다."""
    from localapp.kis_broker import KisBroker

    b = KisBroker.__new__(KisBroker)
    b.virtual, b.cano, b.acnt_cd = True, "12345678", "01"
    seen: list = []
    b._post_retry = lambda path, tr, body, **kw: (
        seen.append(body), {"rt_cd": "0", "msg1": "", "msg_cd": ""})[1]

    b.cancel("0000000101", "005930", 5)                    # 전량(기존 호출자 형태)
    b.cancel("0000000101", "005930", 2, partial=True)      # 부분
    assert [x["QTY_ALL_ORD_YN"] for x in seen] == ["Y", "N"]
    assert [x["ORD_QTY"] for x in seen] == ["5", "2"]

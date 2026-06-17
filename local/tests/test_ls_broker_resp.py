"""LsBroker 주문/취소가 Broker 정규형 {success,order_no,message,msg_cd}를 반환하는지 전수.
account_snapshot이 부분 실패 시 fetch_failed 마커를 다는지(−98% 킬스위치 부류버그 방지).
⚠ LS 응답 fixture는 A2 조사 기반 '가정'. 키 검증(Phase C) 후 실측으로 교체.

    cd local && python -m pytest tests/test_ls_broker_resp.py -q
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

import pytest
from localapp.ls_broker import normalize_ls_order_resp


# ── normalize_ls_order_resp 순수 함수 ─────────────────────────────────────────

def test_normalize_success_extracts_ordno():
    raw = {"rsp_cd": "00000", "rsp_msg": "정상처리",
           "CSPAT00601OutBlock2": {"OrdNo": "12345"}}
    r = normalize_ls_order_resp(raw, ordno_field="OrdNo")
    assert r == {"success": True, "order_no": "12345",
                 "message": "정상처리", "msg_cd": "00000"}


def test_normalize_reject_is_not_success():
    raw = {"rsp_cd": "12345", "rsp_msg": "주문가능수량 초과"}
    r = normalize_ls_order_resp(raw, ordno_field="OrdNo")
    assert r["success"] is False and r["order_no"] == ""
    assert r["message"] == "주문가능수량 초과"


def test_normalize_never_leaks_raw_keys():
    raw = {"rsp_cd": "00000", "CSPAT00601OutBlock2": {"OrdNo": "7"}}
    r = normalize_ls_order_resp(raw, ordno_field="OrdNo")
    assert "rsp_cd" not in r and "CSPAT00601OutBlock2" not in r


def test_normalize_ordno_as_int_converted_to_str():
    """OrdNo가 int로 와도 str로 변환 (LS 샘플에서 long 반환 확인 — 🟢)."""
    raw = {"rsp_cd": "00000", "rsp_msg": "", "CSPAT00601OutBlock2": {"OrdNo": 99}}
    r = normalize_ls_order_resp(raw, ordno_field="OrdNo")
    assert r["order_no"] == "99"
    assert isinstance(r["order_no"], str)


def test_normalize_cancel_outblock1():
    """취소(CSPAT00801) OutBlock1에 OrdNo가 있는 경우 — OutBlock 탐색 순서 확인."""
    raw = {"rsp_cd": "00000", "rsp_msg": "취소완료",
           "CSPAT00801OutBlock1": {"OrdNo": "55555"},
           "CSPAT00801OutBlock2": {"OrdNo": "55555"}}
    r = normalize_ls_order_resp(raw, ordno_field="OrdNo")
    assert r["success"] is True
    assert r["order_no"] == "55555"


def test_normalize_missing_outblock_returns_empty_ordno():
    """OutBlock 없을 때 order_no=""이고 성공 플래그는 rsp_cd 기준."""
    raw = {"rsp_cd": "00000", "rsp_msg": "OK"}
    r = normalize_ls_order_resp(raw, ordno_field="OrdNo")
    assert r["success"] is True
    assert r["order_no"] == ""


# ── account_snapshot fetch_failed 마커 ────────────────────────────────────────

def test_account_snapshot_tags_fetch_failed_on_error(monkeypatch):
    """잔고 조회 실패를 0으로 위장하지 않고 fetch_failed로 표면화(−98% 부류버그 방지)."""
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    b.account_no = "5550123401"
    def _boom(*a, **k): raise RuntimeError("LS 게이트웨이 5xx")
    monkeypatch.setattr(b, "_balance_raw", _boom, raising=False)
    snap = b.account_snapshot()
    assert snap["balance"].get("fetch_failed"), "부분 실패가 fetch_failed로 표면화 안 됨"
    assert snap["positions"] == []


def test_account_snapshot_happy_path(monkeypatch):
    """t0424 fixture → 정규형 계약 + DOMESTIC/KRW 태그 + qty>0 필터링.
    ⚠ 필드명(expcode, hname, janqty, pamt, price) A2 KB 기반 가정.
    """
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    b.account_no = "5550123401"

    # A2 KB t0424 응답 예시 기반 fixture (⚠ 초안)
    _fixture = {
        "rsp_cd": "00000",
        "t0424OutBlock": {
            "cts_expcode": "",
            "mamt": "326800",
            "sunamt": "8000",
            "dtsunik": "1000",
            "tdtsunik": "1200",
        },
        "t0424OutBlock1": [
            {
                "expcode": "005930",
                "hname": "삼성전자",
                "janqty": 4,
                "mdposqt": 4,
                "pamt": 80875,
                "price": 81300,
                "sunikrt": 0.31,
            },
            {
                # qty=0 → 포지션에서 제외
                "expcode": "000660",
                "hname": "SK하이닉스",
                "janqty": 0,
                "pamt": 100000,
                "price": 99000,
            },
        ],
    }
    monkeypatch.setattr(b, "_balance_raw", lambda: _fixture, raising=False)
    snap = b.account_snapshot()

    # balance 구조 확인
    bal = snap["balance"]
    assert "cash" in bal
    assert "total_eval" in bal
    assert bal["cash_usd"] == 0.0
    assert bal["fx_usdkrw"] == 0.0
    assert bal["foreign_eval_krw"] == 0.0
    assert "fetch_failed" not in bal

    # 포지션 계약 + qty>0 필터
    positions = snap["positions"]
    assert len(positions) == 1
    p = positions[0]
    assert p["symbol"] == "005930"
    assert p["name"] == "삼성전자"
    assert p["qty"] == 4
    assert p["market"] == "DOMESTIC"
    assert p["currency"] == "KRW"


# ── 전 주문/취소 메서드가 정규형을 반환하는지 (부류 닫기 — 전수) ──────────────────


class _Resp:
    """requests.Response 최소 위조 — HTTP 200 JSON 응답."""
    def __init__(self, payload, status: int = 200):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _broker_with_fakes(monkeypatch, payload):
    """creds·네트워크 없이 LsBroker — requests.post와 _token()을 가짜로 대체."""
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    b.account_no = "5550123401"
    b.key = "FAKEKEY"
    b.secret = "FAKESECRET"
    b.virtual = True
    b.base = "https://openapi.ls-sec.co.kr:8080"
    b._token_fp = "deadbeef"
    # _token()이 실제 network 시도하지 않도록 패치
    monkeypatch.setattr(b, "_token", lambda: "FAKE_TOKEN", raising=False)
    monkeypatch.setattr(ls_broker.requests, "post", lambda *a, **k: _Resp(payload))
    return b


# ⚠ LS OutBlock 필드명·blk 키 전부 A2 KB 가정 — Phase C 실측 확정
_OK_ORDER = {
    "rsp_cd": "00000",
    "rsp_msg": "모의투자 주문이 완료 되었습니다.",
    "CSPAT00601OutBlock1": {},
    "CSPAT00601OutBlock2": {"OrdNo": "0000777"},
}

_OK_CANCEL = {
    "rsp_cd": "00000",
    "rsp_msg": "모의투자 취소주문이 완료 되었습니다.",
    "CSPAT00801OutBlock1": {},
    "CSPAT00801OutBlock2": {"OrdNo": "0000777"},
}

# (메서드명, 인자, 응답 fixture)
_ORDER_METHODS = [
    ("buy",             ("005930", 5),              _OK_ORDER),
    ("sell",            ("005930", 5),              _OK_ORDER),
    ("buy_limit",       ("005930", 5, 80000),       _OK_ORDER),
    ("sell_limit",      ("005930", 5, 80000),       _OK_ORDER),
    ("cancel",          ("0000777", "005930", 5),   _OK_CANCEL),
]


@pytest.mark.parametrize("method,args,fixture", _ORDER_METHODS)
def test_all_order_methods_return_normalized(monkeypatch, method, args, fixture):
    from localapp import ls_broker
    b = _broker_with_fakes(monkeypatch, fixture)
    r = getattr(b, method)(*args)
    assert isinstance(r, dict), f"{method} 반환이 dict 아님"
    assert "success" in r, f"{method} 반환에 success 없음"
    assert isinstance(r["success"], bool), f"{method} success가 bool 아님"
    # raw LS 응답 필드 누출 금지
    assert "rsp_cd" not in r, f"{method} raw rsp_cd 누출"
    assert "CSPAT00601OutBlock2" not in r, f"{method} raw OutBlock 누출"
    assert "CSPAT00801OutBlock2" not in r, f"{method} raw OutBlock 누출"
    # cancel은 {success, message, msg_cd} — order_no 없어도 됨 (Broker 계약).
    # buy/sell/buy_limit/sell_limit은 order_no 필수.
    if method != "cancel":
        assert "order_no" in r, f"{method} 반환에 order_no 없음"
        assert isinstance(r["order_no"], str), f"{method} order_no가 str 아님"


@pytest.mark.parametrize("method,args,fixture", _ORDER_METHODS[:-1])  # cancel 제외
def test_order_methods_success_true_on_ok(monkeypatch, method, args, fixture):
    """rsp_cd=00000 → success=True."""
    from localapp import ls_broker
    b = _broker_with_fakes(monkeypatch, fixture)
    r = getattr(b, method)(*args)
    assert r["success"] is True, f"{method} rsp_cd=00000 이지만 success=False"


@pytest.mark.parametrize("method,args,_", _ORDER_METHODS[:-1])
def test_order_methods_success_false_on_reject(monkeypatch, method, args, _):
    """rsp_cd≠00000 → success=False."""
    reject = {
        "rsp_cd": "99999",
        "rsp_msg": "주문가능수량 초과",
    }
    from localapp import ls_broker
    b = _broker_with_fakes(monkeypatch, reject)
    r = getattr(b, method)(*args)
    assert r["success"] is False, f"{method} reject 응답에서 success=True"
    assert r["order_no"] == "", f"{method} reject 시 order_no가 비어있지 않음"


# ── buy_resv_limit / sell_resv_limit → NotImplementedError ────────────────────

def test_buy_resv_limit_raises_not_implemented():
    """국내주식 예약주문 미구현 — 명시적 NotImplementedError (조용한 no-op 금지)."""
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    with pytest.raises(NotImplementedError):
        b.buy_resv_limit("005930", 1, 80000.0)


def test_sell_resv_limit_raises_not_implemented():
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    with pytest.raises(NotImplementedError):
        b.sell_resv_limit("005930", 1, 80000.0)


# ── order_status 상태 어휘 + canonical_odno 매칭 ─────────────────────────────

def _t0425_row(ordno, qty, remain, side_str, price=80000, ordtime="112251750"):
    """t0425OutBlock1 행 fixture — A2 KB 필드명 기반 (⚠ 초안)."""
    return {
        "ordno": ordno,
        "expcode": "005930",
        "medosu": side_str,   # "매수" / "매도"
        "qty": qty,
        "price": price,
        "ordrem": remain,
        "price1": 81300,
        "orgordno": 0,
        "ordtime": ordtime,
        "hname": "삼성전자",
    }


@pytest.mark.parametrize("remain,qty,expected_status", [
    (0, 5, "filled"),
    (3, 5, "partial"),
    (5, 5, "submitted"),
])
def test_order_status_vocab(monkeypatch, remain, qty, expected_status):
    """t0425 row → filled/partial/submitted 어휘 매핑."""
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    b.account_no = "5550123401"

    _fixture_resp = {
        "rsp_cd": "00000",
        "t0425OutBlock": {"cts_ordno": ""},
        "t0425OutBlock1": [_t0425_row("16086", qty, remain, "매수")],
    }
    monkeypatch.setattr(b, "_pending_raw", lambda: _fixture_resp, raising=False)

    result = b.order_status("16086", "005930")
    assert result["status"] == expected_status, (
        f"remain={remain}/qty={qty} 인데 status={result['status']!r}, "
        f"expected={expected_status!r}")
    assert result["order_no"] == "16086"
    assert result["filled_qty"] == qty - remain
    assert result["remain_qty"] == remain


def test_order_status_leading_zero_canonical_match(monkeypatch):
    """ordno=016086(leading zero) 과 요청 order_no="16086" 이 매칭돼야 한다 (canonical_odno 확인)."""
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    b.account_no = "5550123401"

    _fixture_resp = {
        "rsp_cd": "00000",
        "t0425OutBlock1": [_t0425_row("016086", 5, 0, "매수")],
    }
    monkeypatch.setattr(b, "_pending_raw", lambda: _fixture_resp, raising=False)
    result = b.order_status("16086", "005930")
    assert result["status"] == "filled"


def test_order_status_unknown_on_query_failure(monkeypatch):
    """조회 실패 시 'unknown' 반환 — crash 금지."""
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    b.account_no = "5550123401"
    monkeypatch.setattr(b, "_pending_raw", lambda: (_ for _ in ()).throw(RuntimeError("5xx")),
                        raising=False)
    result = b.order_status("16086", "005930")
    assert result["status"] == "unknown"
    assert result["filled_qty"] == 0


def test_order_status_hint_accepted_and_ignored(monkeypatch):
    """hint 파라미터를 받아도 에러 없이 처리 (국내는 무시)."""
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    b.account_no = "5550123401"
    _fixture_resp = {
        "rsp_cd": "00000",
        "t0425OutBlock1": [_t0425_row("999", 2, 0, "매도")],
    }
    monkeypatch.setattr(b, "_pending_raw", lambda: _fixture_resp, raising=False)
    result = b.order_status("999", hint={"reserved": True, "qty": 2})
    assert result["status"] == "filled"


# ── pending_orders 반환 계약 ──────────────────────────────────────────────────

def test_pending_orders_returns_normalized_list(monkeypatch):
    """t0425OutBlock1 → {order_no,symbol,...,market,currency} 목록 계약."""
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    b.account_no = "5550123401"

    _fixture_resp = {
        "rsp_cd": "00000",
        "t0425OutBlock": {"cts_ordno": ""},
        "t0425OutBlock1": [_t0425_row("16086", 5, 3, "매수")],
    }
    monkeypatch.setattr(b, "_pending_raw", lambda: _fixture_resp, raising=False)

    orders = b.pending_orders()
    assert isinstance(orders, list)
    assert len(orders) == 1
    o = orders[0]
    assert o["order_no"] == "16086"
    assert o["symbol"] == "005930"
    assert o["side"] == "buy"
    assert o["qty"] == 5
    assert o["remain_qty"] == 3
    assert o["market"] == "DOMESTIC"
    assert o["currency"] == "KRW"
    assert o["ord_branch"] == ""


def test_pending_orders_empty_on_failure(monkeypatch):
    """조회 실패 → [] 반환 (비치명적)."""
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    b.account_no = "5550123401"
    monkeypatch.setattr(b, "_pending_raw",
                        lambda: (_ for _ in ()).throw(RuntimeError("5xx")),
                        raising=False)
    orders = b.pending_orders()
    assert orders == []


# ── price / today_open 계약 ──────────────────────────────────────────────────

def _t1102_resp(price=81300, open_=73200):
    """t1102 응답 fixture — A2 KB 기반 (⚠ 필드명 초안)."""
    return {
        "rsp_cd": "00000",
        "t1102OutBlock": {
            "hname": "삼성전자",
            "price": price,
            "open": open_,
            "high": 81500,
            "low": 80000,
        },
    }


def test_price_returns_float(monkeypatch):
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    b.account_no = "5550123401"
    monkeypatch.setattr(b, "_price_raw", lambda sym: _t1102_resp(), raising=False)
    assert b.price("005930") == 81300.0


def test_today_open_returns_float(monkeypatch):
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    b.account_no = "5550123401"
    monkeypatch.setattr(b, "_price_raw", lambda sym: _t1102_resp(), raising=False)
    assert b.today_open("005930") == 73200.0


def test_today_open_returns_zero_on_missing(monkeypatch):
    """시가 필드 없을 때 0.0 (catch-up fallback 트리거)."""
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    b.account_no = "5550123401"
    monkeypatch.setattr(b, "_price_raw",
                        lambda sym: {"rsp_cd": "00000", "t1102OutBlock": {"price": 81300}},
                        raising=False)
    assert b.today_open("005930") == 0.0

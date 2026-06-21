"""LsFuturesBroker 응답 정규화 전수 — account 2-TR·order normalize·t0434 status·pending 필터.
⚠ fixture는 research 기반. Phase D-C 모의 E2E 후 실측 교체."""
from __future__ import annotations
import sys
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))
from localapp import ls_futures_broker as lfb


def _broker():
    return object.__new__(lfb.LsFuturesBroker)


def test_account_snapshot_merges_two_trs(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_acct_summary_raw", lambda: {
        "CFOAQ50600OutBlock2": {"EvalDpsamtTotamt": "50000000", "MnyOrdAbleAmt": "30000000",
                                "CsgnMgnTotamt": "8000000", "FutsEvalPnlAmt": "120000"}}, raising=False)
    monkeypatch.setattr(b, "_positions_raw", lambda: {
        "t0441OutBlock1": [{"expcode": "101V6000", "medosu": "매수", "jqty": "2",
                            "pamt": "342.25", "price": "343.10", "dtsunik1": "120000"}]}, raising=False)
    snap = b.account_snapshot()
    assert snap["account"]["equity"] == 50000000
    assert snap["account"]["order_cash"] == 30000000
    pos = snap["positions"][0]
    assert pos["symbol"] == "101V6000" and pos["side"] == "long" and pos["qty"] == 2


def test_account_snapshot_short_position(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_acct_summary_raw", lambda: {"CFOAQ50600OutBlock2": {"EvalDpsamtTotamt": "1", "MnyOrdAbleAmt": "1"}}, raising=False)
    monkeypatch.setattr(b, "_positions_raw", lambda: {"t0441OutBlock1": [
        {"expcode": "101V6000", "medosu": "매도", "jqty": "1", "pamt": "340.0", "price": "339.0"}]}, raising=False)
    assert b.account_snapshot()["positions"][0]["side"] == "short"


def test_account_snapshot_raises_on_partial_failure(monkeypatch):
    """2-TR 중 하나 실패 → raise(라우터가 fetch_failed 표식; 0 위장 금지)."""
    import pytest
    b = _broker()
    monkeypatch.setattr(b, "_acct_summary_raw", lambda: (_ for _ in ()).throw(RuntimeError("5xx")), raising=False)
    monkeypatch.setattr(b, "_positions_raw", lambda: {"t0441OutBlock1": []}, raising=False)
    with pytest.raises(Exception):
        b.account_snapshot()


def test_price_and_open(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_quote_raw", lambda sym: {
        "t2101OutBlock": {"price": "343.10", "open": "342.00", "jnilclose": "341.50"}}, raising=False)
    assert b.price("101V6000") == 343.10
    assert b.today_open("101V6000") == 342.00


def test_today_open_zero_fallback(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_quote_raw", lambda sym: {"t2101OutBlock": {"open": ""}}, raising=False)
    assert b.today_open("101V6000") == 0.0


def test_buy_market_normalizes_ordno(monkeypatch):
    b = _broker()
    captured = {}
    def _post(path, tr, body, **k):
        captured["body"] = body["CFOAT00100InBlock1"]
        return {"rsp_cd": "00040", "rsp_msg": "정상", "CFOAT00100OutBlock2": {"OrdNo": "777"}}
    monkeypatch.setattr(b, "_post", _post, raising=False)
    r = b.buy("101V6000", 1)
    assert r == {"success": True, "order_no": "777", "message": "정상", "msg_cd": "00040"}
    assert captured["body"]["BnsTpCode"] == "2"
    assert captured["body"]["FnoOrdprcPtnCode"] == "03"


def test_sell_uses_bnstp_1(monkeypatch):
    b = _broker()
    captured = {}
    def _post(path, tr, body, **k):
        captured["body"] = body["CFOAT00100InBlock1"]
        return {"CFOAT00100OutBlock2": {"OrdNo": "8"}}
    monkeypatch.setattr(b, "_post", _post, raising=False)
    b.sell("101V6000", 1)
    assert captured["body"]["BnsTpCode"] == "1"


def test_buy_limit_sends_double_price(monkeypatch):
    b = _broker()
    captured = {}
    def _post(path, tr, body, **k):
        captured["body"] = body["CFOAT00100InBlock1"]
        return {"CFOAT00100OutBlock2": {"OrdNo": "9"}}
    monkeypatch.setattr(b, "_post", _post, raising=False)
    b.buy_limit("101V6000", 1, 342.25)
    assert captured["body"]["FnoOrdprcPtnCode"] == "00"
    assert captured["body"]["FnoOrdPrc"] == 342.25   # double 포인트 — int 절삭 금지


def test_order_reject_no_ordno(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"rsp_cd": "99", "rsp_msg": "증거금부족"}, raising=False)
    r = b.buy("101V6000", 1)
    assert r["success"] is False and r["order_no"] == ""


def test_resv_not_implemented():
    import pytest
    b = _broker()
    with pytest.raises(NotImplementedError):
        b.buy_resv_limit("101V6000", 1, 342.0)
    with pytest.raises(NotImplementedError):
        b.sell_resv_limit("101V6000", 1, 342.0)

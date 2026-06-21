"""LS 해외주식(미국) 경로 — 시장판정·잔고·시세·주문·취소·조회·예약 전수.
⚠ fixture는 research(overseas-stock-research.md) 기반. 모의 E2E 후 실측 교체."""
from __future__ import annotations
import sys
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))
from localapp import ls_broker as lb


def _broker():
    b = object.__new__(lb.LsBroker)
    b.account_no = "55500000000"
    b.virtual = True
    return b


def test_detect_market_domestic(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: None, raising=False)
    monkeypatch.setattr(lb.market_index, "_looks_domestic", lambda s: True, raising=False)
    assert b._detect_market("000660") == "DOMESTIC"


def test_detect_market_us(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: "NAS", raising=False)
    assert b._detect_market("AAPL") == "NAS"


def test_detect_market_unknown_us_ticker_raises(monkeypatch):
    """미국 티커 형태인데 인덱스에 없으면 추측 금지 → RoutingError(발주 차단)."""
    import pytest
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: None, raising=False)
    monkeypatch.setattr(lb.market_index, "_looks_domestic", lambda s: False, raising=False)
    with pytest.raises(lb.market_index.RoutingError):
        b._detect_market("XYZ")


def test_ls_excd_mapping():
    b = _broker()
    assert b._ls_excd("NAS") == "82"   # NASDAQ
    assert b._ls_excd("NYS") == "81"   # NYSE
    assert b._ls_excd("AMS") == "81"   # AMEX→NYSE 통합(G23-1)


def test_ls_ticker_bare():
    """LS IsuNo/keysymbol는 bare 티커(A접두·슬래시 없음)."""
    b = _broker()
    assert b._ls_ticker("AAPL") == "AAPL"
    assert b._ls_ticker("aapl") == "AAPL"


def _us_index_stub(monkeypatch):
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: "NAS", raising=False)


def test_overseas_snapshot_fields(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    monkeypatch.setattr(b, "_overseas_balance_raw", lambda: {
        "COSOQ00201OutBlock3": [{"CrcyCode": "USD", "FcurrDps": "10000.50", "BaseXchrat": "1350.0"}],
        "COSOQ00201OutBlock4": [
            {"ShtnIsuNo": "AAPL", "AstkBalQty": "10", "FcstckUprc": "150.0",
             "OvrsScrtsCurpri": "200.0", "FcurrMktCode": "82"}]}, raising=False)
    ov = b.overseas_snapshot()
    assert ov["usd_cash"] == 10000.50
    assert ov["fx_usdkrw"] == 1350.0
    assert ov["foreign_eval_krw"] == (10000.50 + 10 * 200.0) * 1350.0
    p = ov["positions"][0]
    assert p["symbol"] == "AAPL" and p["qty"] == 10 and p["currency"] == "USD"
    assert p["avg_price"] == 150.0 and p["eval_price"] == 200.0


def test_account_snapshot_merges_overseas(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_balance_raw", lambda: {
        "t0424OutBlock": {"sunamt": "5000000", "sunamt1": "5000000"},
        "t0424OutBlock1": []}, raising=False)
    monkeypatch.setattr(b, "overseas_snapshot", lambda: {
        "usd_cash": 1000.0, "fx_usdkrw": 1300.0, "foreign_eval_krw": 2600000.0,
        "positions": [{"symbol": "AAPL", "qty": 5, "currency": "USD", "market": "NAS",
                       "avg_price": 100.0, "eval_price": 120.0}]}, raising=False)
    snap = b.account_snapshot(overseas=True)
    bal = snap["balance"]
    assert bal["total_eval"] == 5000000
    assert bal["cash_usd"] == 1000.0
    assert bal["fx_usdkrw"] == 1300.0
    assert bal["foreign_eval_krw"] == 2600000.0
    assert any(p["symbol"] == "AAPL" for p in snap["positions"])


def test_account_snapshot_overseas_failure_marks_fetch_failed(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_balance_raw", lambda: {
        "t0424OutBlock": {"sunamt": "5000000", "sunamt1": "5000000"}, "t0424OutBlock1": []}, raising=False)
    monkeypatch.setattr(b, "overseas_snapshot",
                        lambda: (_ for _ in ()).throw(RuntimeError("5xx")), raising=False)
    snap = b.account_snapshot(overseas=True)
    assert snap["balance"]["total_eval"] == 5000000
    assert snap["balance"]["fetch_failed"] == ["overseas"]


def test_account_snapshot_overseas_false_skips(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_balance_raw", lambda: {
        "t0424OutBlock": {"sunamt": "5000000", "sunamt1": "5000000"}, "t0424OutBlock1": []}, raising=False)
    called = {"n": 0}
    monkeypatch.setattr(b, "overseas_snapshot",
                        lambda: called.__setitem__("n", called["n"] + 1) or {}, raising=False)
    b.account_snapshot(overseas=False)
    assert called["n"] == 0


def test_price_overseas(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["keysymbol"] = body["g3101InBlock"]["keysymbol"]
        return {"g3101OutBlock": {"price": "201.55", "open": "199.00"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    assert b.price("TSLA") == 201.55
    assert captured["keysymbol"] == "82TSLA"


def test_today_open_overseas(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"g3101OutBlock": {"open": "199.00"}}, raising=False)
    assert b.today_open("TSLA") == 199.00


def test_price_domestic_unchanged(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: None, raising=False)
    monkeypatch.setattr(lb.market_index, "_looks_domestic", lambda s: True, raising=False)
    monkeypatch.setattr(b, "_price_raw", lambda s: {"t1102OutBlock": {"price": "70000", "open": "69500"}}, raising=False)
    assert b.price("000660") == 70000.0
    assert b.today_open("000660") == 69500.0


def test_today_open_overseas_zero_fallback(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"g3101OutBlock": {"open": ""}}, raising=False)
    assert b.today_open("TSLA") == 0.0

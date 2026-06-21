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


def test_buy_overseas_market_quotes_to_limit(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    monkeypatch.setattr(b, "_price_overseas", lambda s, m: 201.55, raising=False)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["body"] = body["COSAT00301InBlock1"]
        return {"COSAT00301OutBlock2": {"OrdNo": "12345"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    r = b.buy("AAPL", 3)
    assert r["success"] is True and r["order_no"] == "12345"
    bd = captured["body"]
    assert bd["OrdPtnCode"] == "02"
    assert bd["OrdMktCode"] == "82"
    assert bd["IsuNo"] == "AAPL"
    assert bd["OrdprcPtnCode"] == "00"
    assert bd["OvrsOrdPrc"] == 201.55


def test_sell_overseas_limit_float_price(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["body"] = body["COSAT00301InBlock1"]
        return {"COSAT00301OutBlock2": {"OrdNo": "9"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    b.sell_limit("AAPL", 2, 198.25)
    bd = captured["body"]
    assert bd["OrdPtnCode"] == "01"
    assert bd["OvrsOrdPrc"] == 198.25
    assert bd["OrdprcPtnCode"] == "00"


def test_buy_overseas_quote_fail_raises(monkeypatch):
    import pytest
    b = _broker()
    _us_index_stub(monkeypatch)
    monkeypatch.setattr(b, "_price_overseas", lambda s, m: 0.0, raising=False)
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"COSAT00301OutBlock2": {"OrdNo": "1"}}, raising=False)
    with pytest.raises(RuntimeError):
        b.buy("AAPL", 1)


def test_buy_domestic_unchanged(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: None, raising=False)
    monkeypatch.setattr(lb.market_index, "_looks_domestic", lambda s: True, raising=False)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["tr"] = tr; captured["body"] = body
        return {"CSPAT00601OutBlock2": {"OrdNo": "777"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    r = b.buy("000660", 1)
    assert r["order_no"] == "777"
    assert captured["tr"] == "CSPAT00601"
    assert captured["body"]["CSPAT00601InBlock1"]["IsuNo"] == "A000660"


def test_order_reject_overseas_no_ordno(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    monkeypatch.setattr(b, "_price_overseas", lambda s, m: 200.0, raising=False)
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"rsp_cd": "99", "rsp_msg": "증거금부족"}, raising=False)
    r = b.buy("AAPL", 1)
    assert r["success"] is False and r["order_no"] == ""


def test_cancel_overseas(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "is_us", lambda s: True, raising=False)
    _us_index_stub(monkeypatch)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["body"] = body["COSAT00301InBlock1"]
        return {"COSAT00301OutBlock2": {"OrdNo": "55"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    r = b.cancel("12345", "AAPL", 3)
    assert r["success"] is True
    assert "order_no" not in r
    assert captured["body"]["OrdPtnCode"] == "08"
    assert captured["body"]["OrgOrdNo"] == 12345
    assert captured["body"]["IsuNo"] == "AAPL"


def test_cancel_domestic_unchanged(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "is_us", lambda s: False, raising=False)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["tr"] = tr
        return {"CSPAT00801OutBlock2": {"OrdNo": "9"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    b.cancel("100", "000660", 1)
    assert captured["tr"] == "CSPAT00801"


# ── E6: 해외 체결조회 COSAQ00102 ─────────────────────────────────────────────


def _ccld_rows(rows):
    return {"COSAQ00102OutBlock3": rows}


def test_overseas_order_status_filled(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "is_us", lambda s: True, raising=False)
    monkeypatch.setattr(b, "_overseas_ccld_raw", lambda exec_yn: _ccld_rows([
        {"OrdNo": "12345", "OrgOrdNo": "0", "ShtnIsuNo": "AAPL", "OrdQty": "3",
         "ExecQty": "3", "UnercQty": "0", "OvrsExecPrc": "201.55", "OrdTrxPtnNm": "체결"}]), raising=False)
    st = b.order_status("12345", symbol="AAPL")
    assert st["status"] == "filled" and st["filled_qty"] == 3 and st["fill_price"] == 201.55


def test_overseas_order_status_cancelled(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "is_us", lambda s: True, raising=False)
    monkeypatch.setattr(b, "_overseas_ccld_raw", lambda exec_yn: _ccld_rows([
        {"OrdNo": "5", "OrgOrdNo": "0", "ShtnIsuNo": "AAPL", "OrdQty": "1",
         "ExecQty": "0", "UnercQty": "0", "OrdTrxPtnNm": "취소완료"}]), raising=False)
    assert b.order_status("5", symbol="AAPL")["status"] == "cancelled"


def test_overseas_order_status_partial(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "is_us", lambda s: True, raising=False)
    monkeypatch.setattr(b, "_overseas_ccld_raw", lambda exec_yn: _ccld_rows([
        {"OrdNo": "7", "OrgOrdNo": "0", "ShtnIsuNo": "AAPL", "OrdQty": "10",
         "ExecQty": "4", "UnercQty": "6", "OvrsExecPrc": "200.0", "OrdTrxPtnNm": "체결"}]), raising=False)
    assert b.order_status("7", symbol="AAPL")["status"] == "partial"


def test_overseas_order_status_unknown(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "is_us", lambda s: True, raising=False)
    monkeypatch.setattr(b, "_overseas_ccld_raw", lambda exec_yn: _ccld_rows([]), raising=False)
    assert b.order_status("999", symbol="AAPL")["status"] == "unknown"


def test_overseas_pending_merges(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_pending_raw", lambda: {"t0425OutBlock1": []}, raising=False)
    monkeypatch.setattr(b, "_overseas_ccld_raw", lambda exec_yn: _ccld_rows([
        {"OrdNo": "10", "OrgOrdNo": "0", "ShtnIsuNo": "AAPL", "OrdQty": "5",
         "ExecQty": "0", "UnercQty": "5", "OvrsOrdPrc": "190.0", "OrdMktCode": "82"}]), raising=False)
    pend = b.pending_orders()
    assert len(pend) == 1 and pend[0]["order_no"] == "10" and pend[0]["currency"] == "USD"

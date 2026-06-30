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


def test_account_snapshot_mock_equity_reconstructs_from_used_margin(monkeypatch):
    """모의(CFOAQ50600 평가 미제공): equity = 가용(OrdAbleAmt) + 잠긴증거금(UsePreargMgn).

    종전엔 equity를 가용증거금만으로 근사 → 포지션이 열려 증거금이 잠기면 가용액↓ → equity 가짜 하락
    (킬스위치 오발동·−98% 부류). 진짜 예탁자산 = 가용 + 잠긴 — 둘 다 CFOAQ10100에 있어 추정 없이 복원
    (2026-06-30 실전 프로브 발견: OrdAbleAmt 236M + UsePreargMgn 264M = EvalDpsamtTotamt 500M).
    사이징(order_cash)은 가용 그대로 — equity(킬스위치)만 복원."""
    b = _broker()
    monkeypatch.setattr(b, "_acct_summary_raw", lambda: {"CFOAQ50600OutBlock2": {}}, raising=False)  # 평가 미제공
    monkeypatch.setattr(b, "_margin_amounts_krw", lambda: (236_254_700, 263_745_300), raising=False)
    monkeypatch.setattr(b, "_positions_raw", lambda: {"t0441OutBlock1": []}, raising=False)
    acct = b.account_snapshot()["account"]
    assert acct["order_cash"] == 236_254_700                  # 사이징 = 가용
    assert acct["equity"] == 500_000_000                      # 킬스위치 = 가용+잠긴 (가짜하락 없음)


def test_price_and_open(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_quote_raw", lambda sym: {
        "t2111OutBlock": {"price": "343.10", "open": "342.00", "jnilclose": "341.50"}}, raising=False)
    assert b.price("101V6000") == 343.10
    assert b.today_open("101V6000") == 342.00


def test_today_open_zero_fallback(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_quote_raw", lambda sym: {"t2111OutBlock": {"open": ""}}, raising=False)
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


def test_order_status_filled_from_t0434(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_ccld_raw", lambda chegb: {"t0434OutBlock1": [
        {"ordno": "777", "orgordno": "0", "qty": "1", "cheqty": "1", "ordrem": "0",
         "cheprice": "343.10", "status": "완료"}]}, raising=False)
    st = b.order_status("777", symbol="101V6000")
    assert st["status"] == "filled" and st["filled_qty"] == 1 and st["fill_price"] == 343.10


def test_order_status_cancelled(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_ccld_raw", lambda chegb: {"t0434OutBlock1": [
        {"ordno": "5", "orgordno": "0", "qty": "1", "cheqty": "0", "ordrem": "0", "status": "취소완료"}]}, raising=False)
    assert b.order_status("5", symbol="101V6000")["status"] == "cancelled"


def test_order_status_unknown_when_absent(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_ccld_raw", lambda chegb: {"t0434OutBlock1": []}, raising=False)
    assert b.order_status("999", symbol="101V6000")["status"] == "unknown"


def test_pending_excludes_modify_cancel_rows(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_ccld_raw", lambda chegb: {"t0434OutBlock1": [
        {"ordno": "10", "orgordno": "0", "qty": "2", "cheqty": "0", "ordrem": "2", "medosu": "매수", "price": "342.0", "expcode": "101V6000"},
        {"ordno": "11", "orgordno": "10", "qty": "2", "cheqty": "0", "ordrem": "2", "medosu": "매수", "price": "342.0", "expcode": "101V6000"}]}, raising=False)
    pend = b.pending_orders()
    assert len(pend) == 1 and pend[0]["order_no"] == "10" and pend[0]["side"] == "buy"


def test_cancel_normalizes(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_post", lambda p, t, body, **k: {"CFOAT00300OutBlock2": {"OrdNo": "99"}}, raising=False)
    r = b.cancel("10", "101V6000", 2)
    assert r["success"] is True and "order_no" not in r


# --- orderable_qty (CFOAQ10100 NewOrdAbleQty) — 모델 A 라이브 사이징 브로커 기준값 ---

_OQ_OK = {"CFOAQ10100OutBlock2": {"NewOrdAbleQty": "120", "LqdtOrdAbleQty": "0", "OrdAbleAmt": "500000000"}}


def test_orderable_qty_buy_parses_and_sends_inblock(monkeypatch):
    b = _broker()
    captured = {}
    def _post(path, tr, body, **k):
        captured["path"] = path
        captured["tr"] = tr
        captured["ib"] = body["CFOAQ10100InBlock1"]
        return _OQ_OK
    monkeypatch.setattr(b, "_post", _post, raising=False)
    qty = b.orderable_qty("101V6000", "buy", 342.25)
    assert qty == 120
    assert captured["tr"] == "CFOAQ10100"
    assert captured["path"] == "/futureoption/accno"
    ib = captured["ib"]
    assert ib["BnsTpCode"] == "2"                  # 매수
    assert ib["FnoIsuNo"] == "101V6000"
    assert ib["FnoOrdPrc"] == 342.25               # double 포인트 — int 절삭 금지
    assert ib["FnoOrdprcPtnCode"] == "00"          # 지정가 고정


def test_orderable_qty_sell_uses_bnstp_1(monkeypatch):
    b = _broker()
    captured = {}
    def _post(path, tr, body, **k):
        captured["ib"] = body["CFOAQ10100InBlock1"]
        return _OQ_OK
    monkeypatch.setattr(b, "_post", _post, raising=False)
    b.orderable_qty("101V6000", "sell", 342.0)
    assert captured["ib"]["BnsTpCode"] == "1"       # 매도


def test_orderable_qty_raises_when_outblock_absent(monkeypatch):
    """조회 실패(OutBlock2 부재) → raise. 호출자(Trader)가 catch해 카탈로그로 강등.
    ⚠ _orderable_amt_krw(soft 0폴백)와 달리 0 위장 금지 — 실패와 0계약 구분."""
    import pytest
    b = _broker()
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"rsp_cd": "99", "rsp_msg": "조회실패"}, raising=False)
    with pytest.raises(RuntimeError):
        b.orderable_qty("101V6000", "buy", 342.0)


def test_orderable_qty_zero_contracts_returns_zero(monkeypatch):
    """정상 응답의 NewOrdAbleQty=0(증거금 부족)은 0 반환 — raise 아님."""
    b = _broker()
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"CFOAQ10100OutBlock2": {"NewOrdAbleQty": "0"}}, raising=False)
    assert b.orderable_qty("101V6000", "buy", 342.0) == 0


def test_orderable_qty_bad_side_raises_value_error(monkeypatch):
    import pytest
    b = _broker()
    monkeypatch.setattr(b, "_post", lambda *a, **k: _OQ_OK, raising=False)
    with pytest.raises(ValueError):
        b.orderable_qty("101V6000", "hold", 342.0)

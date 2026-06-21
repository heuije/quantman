"""LS 해외선물(CME) 경로 — 듀얼인증·잔고·주문·체결·취소·resolver.
⚠ fixture는 research(overseas-futures-research.md) 기반. 모의 E2E 후 실측 교체."""
from __future__ import annotations
import sys
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))
from localapp import ls_futures_broker as lfb


class _OvDouble:
    """overseas _LsAuth 더블 — _post 캡처."""
    def __init__(self, resp_map=None, resp=None):
        self.resp_map = resp_map or {}
        self.resp = resp
        self.calls = []
    def _post(self, path, tr, body, **k):
        self.calls.append((tr, body))
        return self.resp_map.get(tr, self.resp or {})


def test_overseas_configured_true_when_ov_present():
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble()
    assert b.overseas_configured is True


def test_overseas_configured_false_when_absent():
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = None
    assert b.overseas_configured is False


def test_init_loads_both_creds(monkeypatch):
    monkeypatch.setattr(lfb, "load_ls_futures",
                        lambda: {"app_key": "DK", "app_secret": "DS", "account_no": "111", "virtual": True}, raising=False)
    monkeypatch.setattr(lfb, "load_ls_overseas_futures",
                        lambda: {"app_key": "OK", "app_secret": "OS", "account_no": "222", "virtual": True}, raising=False)
    b = lfb.LsFuturesBroker()
    assert b.domestic_configured is True
    assert b.overseas_configured is True
    assert b.key == "DK"
    assert b._ov.key == "OK"


def test_init_overseas_only(monkeypatch):
    monkeypatch.setattr(lfb, "load_ls_futures", lambda: None, raising=False)
    monkeypatch.setattr(lfb, "load_ls_overseas_futures",
                        lambda: {"app_key": "OK", "app_secret": "OS", "account_no": "222", "virtual": True}, raising=False)
    b = lfb.LsFuturesBroker()
    assert b.domestic_configured is False
    assert b.overseas_configured is True


def test_init_raises_when_neither(monkeypatch):
    import pytest
    monkeypatch.setattr(lfb, "load_ls_futures", lambda: None, raising=False)
    monkeypatch.setattr(lfb, "load_ls_overseas_futures", lambda: None, raising=False)
    with pytest.raises(RuntimeError):
        lfb.LsFuturesBroker()


def test_save_load_overseas_futures_roundtrip(monkeypatch):
    """secrets_store overseas-futures 슬롯 round-trip (저장 매커니즘은 도메스틱과 동일)."""
    from localapp import secrets_store as ss
    creds = {"app_key": "K", "app_secret": "S", "account_no": "999", "virtual": True}
    # 도메스틱 save_ls_futures/load_ls_futures 테스트가 쓰는 것과 동일한 격리 패턴을 따를 것.
    # (keyring 더블/monkeypatch가 기존 secrets 테스트에 있으면 그 패턴 재사용. 없으면 이 테스트는 생략 가능 —
    #  대신 load_ls_overseas_futures가 import 가능하고 None을 반환함만 확인.)
    assert hasattr(ss, "save_ls_overseas_futures") and hasattr(ss, "load_ls_overseas_futures")
    store = {}
    monkeypatch.setattr(ss.keyring, "set_password", lambda svc, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(ss.keyring, "get_password", lambda svc, k: store.get(k))
    ss.save_ls_overseas_futures("K", "S", "999", virtual=True)
    loaded = ss.load_ls_overseas_futures()
    assert loaded == creds


def _ovb(resp_map):
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble(resp_map=resp_map)
    return b


def test_overseas_account_snapshot_krw():
    b = _ovb({
        "CIDBQ03000": {"CIDBQ03000OutBlock2": [{"CrcyObjCode": "TOT", "EvalAssetAmt": "10000",
                       "AbrdFutsOrdAbleAmt": "8000", "AbrdFutsCsgnMgn": "2000", "AbrdFutsEvalPnlAmt": "150"}]},
        "CIDBQ05300": {"CIDBQ05300OutBlock2": [{"CrcyCode": "USD", "Xchrat": "1350.0"}]},
        "CIDBQ01500": {"CIDBQ01500OutBlock2": [{"IsuCodeVal": "CLM26", "BnsTpCode": "2", "BalQty": "2",
                       "PchsPrc": "70.50", "OvrsDrvtNowPrc": "71.20", "AbrdFutsEvalPnlAmt": "150"}]},
    })
    snap = b.overseas_account_snapshot()
    assert snap["account"]["equity"] == 10000 * 1350.0
    assert snap["account"]["order_cash"] == 8000 * 1350.0
    assert snap["account"]["currency"] == "KRW"
    p = snap["positions"][0]
    assert p["symbol"] == "CLM26" and p["side"] == "long" and p["qty"] == 2
    assert p["currency"] == "USD" and p["asset_class"] == "futures"


def test_overseas_account_snapshot_short():
    b = _ovb({
        "CIDBQ03000": {"CIDBQ03000OutBlock2": [{"EvalAssetAmt": "1", "AbrdFutsOrdAbleAmt": "1"}]},
        "CIDBQ05300": {"CIDBQ05300OutBlock2": [{"CrcyCode": "USD", "Xchrat": "1300"}]},
        "CIDBQ01500": {"CIDBQ01500OutBlock2": [{"IsuCodeVal": "GCM26", "BnsTpCode": "1", "BalQty": "1",
                       "PchsPrc": "2000", "OvrsDrvtNowPrc": "1990"}]},
    })
    assert b.overseas_account_snapshot()["positions"][0]["side"] == "short"


def test_overseas_account_snapshot_raises_on_zero_xchrat():
    import pytest
    b = _ovb({
        "CIDBQ03000": {"CIDBQ03000OutBlock2": [{"EvalAssetAmt": "10000", "AbrdFutsOrdAbleAmt": "8000"}]},
        "CIDBQ05300": {"CIDBQ05300OutBlock2": [{"CrcyCode": "USD", "Xchrat": "0"}]},
        "CIDBQ01500": {"CIDBQ01500OutBlock2": []},
    })
    with pytest.raises(Exception):
        b.overseas_account_snapshot()


def test_overseas_buy_market():
    b = object.__new__(lfb.LsFuturesBroker)
    ov = _OvDouble(resp={"CIDBT00100OutBlock2": {"OvrsFutsOrdNo": "0000000777", "OrdDt": "20260622"}})
    b._ov = ov
    r = b.overseas_buy("CLM26", 1)
    assert r["success"] is True and r["order_no"] == "0000000777"
    tr, body = ov.calls[-1]
    bd = body["CIDBT00100InBlock1"]
    assert tr == "CIDBT00100"
    assert bd["FutsOrdTpCode"] == "1"
    assert bd["BnsTpCode"] == "2"
    assert bd["AbrdFutsOrdPtnCode"] == "1"
    assert bd["IsuCodeVal"] == "CLM26"


def test_overseas_sell_limit_double_price():
    b = object.__new__(lfb.LsFuturesBroker)
    ov = _OvDouble(resp={"CIDBT00100OutBlock2": {"OvrsFutsOrdNo": "8"}})
    b._ov = ov
    b.overseas_sell_limit("GCM26", 2, 1985.5)
    bd = ov.calls[-1][1]["CIDBT00100InBlock1"]
    assert bd["BnsTpCode"] == "1"
    assert bd["AbrdFutsOrdPtnCode"] == "2"
    assert bd["OvrsDrvtOrdPrc"] == 1985.5


def test_overseas_order_reject_no_ordno():
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble(resp={"rsp_cd": "99", "rsp_msg": "증거금부족"})
    r = b.overseas_buy("CLM26", 1)
    assert r["success"] is False and r["order_no"] == ""


def _ov_ccld(rows):
    return {"CIDBQ02400OutBlock2": rows}


def test_overseas_order_status_filled():
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble(resp=_ov_ccld([{"OvrsFutsOrdNo": "0000000777", "OrdQty": "1", "ExecQty": "1",
                       "UnercQty": "0", "AbrdFutsExecPrc": "71.20", "TrxStatCodeNm": "체결"}]))
    st = b.overseas_order_status("777")
    assert st["status"] == "filled" and st["filled_qty"] == 1 and st["fill_price"] == 71.20


def test_overseas_order_status_partial():
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble(resp=_ov_ccld([{"OvrsFutsOrdNo": "7", "OrdQty": "5", "ExecQty": "2",
                       "UnercQty": "3", "AbrdFutsExecPrc": "71.0"}]))
    assert b.overseas_order_status("7")["status"] == "partial"


def test_overseas_order_status_cancelled():
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble(resp=_ov_ccld([{"OvrsFutsOrdNo": "5", "OrdQty": "1", "ExecQty": "0",
                       "UnercQty": "0", "TrxStatCodeNm": "취소"}]))
    assert b.overseas_order_status("5")["status"] == "cancelled"


def test_overseas_order_status_unknown():
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble(resp=_ov_ccld([]))
    assert b.overseas_order_status("999")["status"] == "unknown"


def test_overseas_cancel_body():
    b = object.__new__(lfb.LsFuturesBroker)
    ov = _OvDouble(resp={"CIDBT01000OutBlock2": {"OvrsFutsOrdNo": "55"}})
    b._ov = ov
    r = b.overseas_cancel("777", "CLM26", "20260622")
    assert r["success"] is True and "order_no" not in r
    bd = ov.calls[-1][1]["CIDBT01000InBlock1"]
    assert bd["FutsOrdTpCode"] == "3"
    assert bd["OvrsFutsOrgOrdNo"] == "777"
    assert bd["IsuCodeVal"] == "CLM26"
    assert bd["OrdDt"] == "20260622"

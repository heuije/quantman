"""해외선물 자격증명 슬롯 + 순수함수 단위검증."""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
for _p in (str(_LOCAL), str(_LOCAL.parent / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def test_overseas_creds_roundtrip(monkeypatch):
    import localapp.secrets_store as ss
    store = {}
    monkeypatch.setattr(ss.keyring, "set_password", lambda s, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(ss.keyring, "get_password", lambda s, k: store.get(k))
    assert ss.load_kis_overseas_futures() is None
    ss.save_kis_overseas_futures("AK", "SK", "80012345-08", virtual=False)
    c = ss.load_kis_overseas_futures()
    assert c == {"app_key": "AK", "app_secret": "SK",
                 "account_no": "80012345-08", "virtual": False}


from localapp.kis_overseas_futures import (
    build_overseas_order_body, parse_overseas_balance, scale_overseas_price,
)


def test_build_order_body_limit_buy():
    b = build_overseas_order_body(cano="81012345", acnt_prdt_cd="08",
                                  symbol="6BZ22", side="buy", qty=1,
                                  price=1.17, order_type="limit")
    assert b["OVRS_FUTR_FX_PDNO"] == "6BZ22"
    assert b["SLL_BUY_DVSN_CD"] == "02"        # 매수
    assert b["PRIC_DVSN_CD"] == "1"            # 지정가
    assert b["FM_LIMIT_ORD_PRIC"] == "1.17"
    assert b["FM_STOP_ORD_PRIC"] == ""
    assert b["FM_ORD_QTY"] == "1"
    assert b["CCLD_CNDT_CD"] == "6"            # 지정가 EOD
    assert b["CPLX_ORD_DVSN_CD"] == "0" and b["ECIS_RSVN_ORD_YN"] == "N"


def test_build_order_body_market_sell():
    s = build_overseas_order_body(cano="81012345", acnt_prdt_cd="08",
                                  symbol="GCZ25", side="sell", qty=2,
                                  price=0, order_type="market")
    assert s["SLL_BUY_DVSN_CD"] == "01"        # 매도
    assert s["PRIC_DVSN_CD"] == "2"            # 시장가
    assert s["FM_LIMIT_ORD_PRIC"] == ""        # 시장가 → 가격 공란
    assert s["CCLD_CNDT_CD"] == "2"            # 시장가
    assert s["FM_ORD_QTY"] == "2"


def test_build_order_body_bad_side():
    import pytest
    with pytest.raises(ValueError):
        build_overseas_order_body(cano="1", acnt_prdt_cd="08", symbol="x",
                                  side="hold", qty=1, price=1, order_type="limit")


def test_parse_overseas_balance_row_array():
    resp = {"output": [
        {"ovrs_futr_fx_pdno": "6BZ22", "sll_buy_dvsn_cd": "02", "fm_ustl_qty": "2",
         "fm_ccld_avg_pric": "1.1898", "fm_now_pric": "1.2350",
         "fm_evlu_pfls_amt": "5656.24", "crcy_cd": "USD"},
        {"ovrs_futr_fx_pdno": "ZBZ22", "sll_buy_dvsn_cd": "01", "fm_ustl_qty": "100",
         "fm_ccld_avg_pric": "132.29", "fm_now_pric": "131.21",
         "fm_evlu_pfls_amt": "107438.00", "crcy_cd": "USD"},
    ]}
    out = parse_overseas_balance(resp)
    assert len(out["positions"]) == 2
    p0 = out["positions"][0]
    assert p0["symbol"] == "6BZ22" and p0["side"] == "long" and p0["qty"] == 2
    assert p0["avg_price"] == 1.1898 and p0["eval_price"] == 1.2350
    assert p0["eval_pnl"] == 5656.24 and p0["currency"] == "USD"
    assert out["positions"][1]["side"] == "short" and out["positions"][1]["qty"] == 100


def test_parse_overseas_balance_empty():
    assert parse_overseas_balance({}) == {"positions": []}
    assert parse_overseas_balance({"output": []}) == {"positions": []}


def test_scale_overseas_price():
    # sCalcDesz: raw × 10^desz. GC desz=-1 → 19225 = 1922.5 ; desz 0 = 그대로 ; 빈값 = 0.0
    assert scale_overseas_price("19225", -1) == 1922.5
    assert scale_overseas_price("  75.63 ", 0) == 75.63
    assert scale_overseas_price("", -1) == 0.0

"""KisFuturesBroker 순수 헬퍼 — 주문 바디(TTTO1101U)·잔고 파라미터·잔고 파싱(VTFO6118R) 단위검증.

네트워크·자격증명 없이 검증 가능한 부분만(build/params/parse/_json). 잔고 파라미터·output1/2 형태는
라이브 모의(2026-06-08)로 확정. 주문 성공 응답·포지션 행 키·시장가 코드는 파생 모의 충전 후 검증(phase2).

    cd platform/local && python -m pytest tests/test_kis_futures_broker.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent   # tests → local
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

import pytest

from localapp.kis_futures_broker import (
    KisFuturesBroker,
    _balance_rows,
    _json,
    build_balance_params,
    build_futures_cancel_body,
    build_futures_order_body,
    parse_ccnl_order_status,
    parse_futures_balance,
)


def test_reservation_orders_raise_for_futures():
    """선물은 예약주문 미지원 — buy_resv_limit/sell_resv_moo는 명시 오류(__init__ 우회로 가드만 검증)."""
    b = object.__new__(KisFuturesBroker)              # creds 불요 — 가드는 즉시 raise
    with pytest.raises(NotImplementedError):
        b.buy_resv_limit("A01606", 1, 350.0)
    with pytest.raises(NotImplementedError):
        b.sell_resv_moo("A01606", 1)


def test_build_order_body_buy():
    b = build_futures_order_body(cano="81012345", acnt_prdt_cd="03",
                                 symbol="167R12", qty=2, price=345.05, side="buy")
    assert b["SLL_BUY_DVSN_CD"] == "02"        # 매수
    assert b["ACNT_PRDT_CD"] == "03"           # 선물옵션 계좌
    assert b["SHTN_PDNO"] == "167R12"
    assert b["ORD_QTY"] == "2"                 # 계약수
    assert b["UNIT_PRICE"] == "345.05"         # 지정가
    assert b["ORD_DVSN_CD"] == "01"            # 지정가
    assert b["ORD_PRCS_DVSN_CD"] == "02"       # 주문전송


def test_build_order_body_sell():
    s = build_futures_order_body(cano="81012345", acnt_prdt_cd="03",
                                 symbol="167R12", qty=1, price=340, side="sell")
    assert s["SLL_BUY_DVSN_CD"] == "01"        # 매도


def test_build_order_body_bad_side():
    import pytest
    with pytest.raises(ValueError):
        build_futures_order_body(cano="1", acnt_prdt_cd="03", symbol="x",
                                 qty=1, price=1, side="hold")


def test_build_balance_params():
    # 라이브 검증: MGNA_DVSN 누락 시 KIS가 'INPUT_FIELD_NAME MGNA_DVSN'로 거절 → 필수.
    p = build_balance_params("50188802", "03")
    assert p["CANO"] == "50188802" and p["ACNT_PRDT_CD"] == "03"
    assert p["MGNA_DVSN"] == "01"               # 위탁증거금 (필수)
    assert p["EXCC_STAT_CD"] == "1"             # 정산가 기준 (필수)
    assert "CTX_AREA_FK200" in p and "CTX_AREA_NK200" in p   # 연속조회 키 (필수)


def test_balance_rows_both_shapes():
    # 빈잔고=[], 행형(list of dicts), 컬럼형(dict of arrays) 모두 행 리스트로 정규화.
    assert _balance_rows([]) == []
    assert _balance_rows(None) == []
    assert _balance_rows([{"a": "1"}]) == [{"a": "1"}]
    assert _balance_rows({"a": ["1", "2"], "b": ["x", "y"]}) == [
        {"a": "1", "b": "x"}, {"a": "2", "b": "y"}]


def test_parse_balance_contracts_row():
    # 행형 output1. 신규 키: shtn_pdno(6자 거래코드)·side(롱숏)·settle_price·eval_pnl.
    resp = {"output1": [
        {"shtn_pdno": "A01606", "sll_buy_dvsn_name": "매수", "cblc_qty": "2",
         "ccld_avg_unpr1": "344.70", "excc_unpr": "345.70", "evlu_pfls_amt": "500000"},
        {"shtn_pdno": "EMPTY", "cblc_qty": "0"},     # 0계약 → 제외
    ]}
    out = parse_futures_balance(resp)
    assert len(out["positions"]) == 1
    p = out["positions"][0]
    assert p["symbol"] == "A01606" and p["side"] == "buy" and p["qty"] == 2
    assert p["avg_price"] == 344.70 and p["settle_price"] == 345.70 and p["eval_pnl"] == 500000.0


def test_parse_balance_columnar():
    # KIS 공식 spec 예시 형태: output1=컬럼형(dict of arrays). 병렬배열 전치 + 롱숏 구분.
    resp = {"output1": {
        "shtn_pdno": ["A01606", "A01609"],
        "sll_buy_dvsn_name": ["매수", "매도"],
        "cblc_qty": ["3", "1"],
        "ccld_avg_unpr1": ["1178.25", "1165.00"],
        "excc_unpr": ["1180.00", "1160.00"],
        "evlu_pfls_amt": ["131250", "-12500"],
    }}
    out = parse_futures_balance(resp)
    assert len(out["positions"]) == 2
    assert out["positions"][0]["symbol"] == "A01606" and out["positions"][0]["side"] == "buy" and out["positions"][0]["qty"] == 3
    assert out["positions"][1]["symbol"] == "A01609" and out["positions"][1]["side"] == "sell"
    assert out["positions"][1]["eval_pnl"] == -12500.0


def test_parse_balance_account_summary():
    # output2 = 계좌 요약(라이브 모의 VTFO6118R 키). 자동매매가 쓰는 주문가능현금·증거금.
    resp = {"output1": [], "output2": {
        "ord_psbl_cash": "5000000", "mgna_tota": "1200000",
        "dnca_cash": "5000000", "futr_evlu_pfls_amt": "-30000"}}
    out = parse_futures_balance(resp)
    assert out["positions"] == []
    acc = out["account"]
    assert acc["order_cash"] == 5000000.0 and acc["margin_total"] == 1200000.0
    assert acc["deposit_cash"] == 5000000.0 and acc["eval_pnl"] == -30000.0


def test_parse_balance_empty():
    # output1/output2 부재여도 안전. account는 0으로 채움.
    out = parse_futures_balance({})
    assert out["positions"] == []
    assert out["account"] == {"order_cash": 0.0, "margin_total": 0.0,
                              "deposit_cash": 0.0, "eval_pnl": 0.0}


def test_json_decodes_utf8_korean():
    # KIS는 charset=utf-8 본문을 주지만 requests 자동탐지가 한글을 깨뜨림 → 명시 UTF-8 디코딩.
    class _Resp:
        content = '{"rt_cd":"1","msg1":"모의투자 주문처리가 안되었습니다."}'.encode("utf-8")
    out = _json(_Resp())
    assert out["msg1"] == "모의투자 주문처리가 안되었습니다."


def test_build_order_body_market_buy():
    b = build_futures_order_body(cano="50188802", acnt_prdt_cd="03", symbol="A01606",
                                 qty=1, price=0, side="buy", order_type="market")
    assert b["ORD_DVSN_CD"] == "02"            # 시장가
    assert b["UNIT_PRICE"] == "0"              # 시장가 가격 0
    assert b["SLL_BUY_DVSN_CD"] == "02"


def test_build_order_body_limit_default_unchanged():
    # order_type 미지정 → 기존 지정가 동작(회귀).
    b = build_futures_order_body(cano="5", acnt_prdt_cd="03", symbol="A01606",
                                 qty=2, price=375.0, side="sell")
    assert b["ORD_DVSN_CD"] == "01" and b["UNIT_PRICE"] == "375.0"


def test_build_cancel_body():
    b = build_futures_cancel_body(cano="50188802", acnt_prdt_cd="03",
                                  order_no="0000005605", qty=1)
    assert b["RVSE_CNCL_DVSN_CD"] == "02"      # 취소
    assert b["ORGN_ODNO"] == "0000005605"
    assert b["ORD_QTY"] == "1"                 # 모의 필수
    assert b["UNIT_PRICE"] == "0"              # 취소 시 0
    assert b["KRX_NMPR_CNDT_CD"] == "0"        # 취소 시 0
    assert b["ORD_DVSN_CD"] == "01"            # 취소 시 01
    assert b["RMN_QTY_YN"] == "Y"
    assert b["ORD_PRCS_DVSN_CD"] == "02"


def test_overseas_limit_buy_routes_to_overseas_endpoint(monkeypatch):
    """overseas_buy_limit은 해외 엔드포인트로 라우팅하고 올바른 바디를 전송해야 한다."""
    import localapp.kis_futures_broker as kfb
    captured = {}

    class _Resp:
        status_code = 200
        content = b'{"rt_cd":"0","msg_cd":"APBK0013","msg1":"ok","output":{"ODNO":"00298040","ORD_DT":"20260608"}}'
        def raise_for_status(self): pass

    monkeypatch.setattr(
        kfb.requests, "post",
        lambda url, headers=None, json=None, timeout=None:
            (captured.update(url=url, body=json, tr=headers.get("tr_id")), _Resp())[1]
    )

    b = kfb.KisFuturesBroker.__new__(kfb.KisFuturesBroker)
    # inject overseas context (match __init__ attribute names):
    b._ov_key = "AK"
    b._ov_secret = "SK"
    b._ov_cano = "80012345"
    b._ov_acnt_prdt_cd = "08"
    b._ov_base = kfb._REAL
    b._ov_tok = "TKN"
    b._ov_tok_exp = 9999999999.0

    r = b.overseas_buy_limit("GCZ25", 1, 1922.5)

    assert "overseas-futureoption/v1/trading/order" in captured["url"]
    assert captured["tr"] == "OTFM3001U"
    assert captured["body"]["OVRS_FUTR_FX_PDNO"] == "GCZ25"
    assert captured["body"]["SLL_BUY_DVSN_CD"] == "02"
    assert captured["body"]["PRIC_DVSN_CD"] == "1"
    assert r["output"]["ODNO"] == "00298040"


def test_parse_ccnl_filled():
    resp = {"output1": [
        {"odno": "0000007045", "tot_ccld_qty": "1", "qty": "0", "avg_idx": "400.00", "rjct_qty": "0"},
    ]}
    s = parse_ccnl_order_status(resp, "0000007045")
    assert s["status"] == "filled" and s["filled_qty"] == 1 and s["remain_qty"] == 0
    assert s["fill_price"] == 400.0 and s["order_no"] == "0000007045"


def test_parse_ccnl_partial_and_canonical_odno():
    # 부분체결 + 0-패딩 무관 매칭(canonical lstrip("0")).
    resp = {"output1": [
        {"odno": "0000007006", "tot_ccld_qty": "1", "qty": "1", "avg_idx": "375.0", "rjct_qty": "0"},
    ]}
    s = parse_ccnl_order_status(resp, "7006")
    assert s["status"] == "partial" and s["filled_qty"] == 1 and s["remain_qty"] == 1


def test_parse_ccnl_rejected_and_unknown():
    resp = {"output1": [{"odno": "0000000001", "tot_ccld_qty": "0", "qty": "0", "rjct_qty": "1"}]}
    assert parse_ccnl_order_status(resp, "1")["status"] == "rejected"
    assert parse_ccnl_order_status({}, "999")["status"] == "unknown"

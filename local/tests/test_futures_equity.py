"""선물 equity가 kill-switch·drawdown 통합자산에 반영되는지 (라이브 확정 필드 기반).

라이브(2026-06-09 60044290) 캡처: 선물 잔고 output2.`prsm_dpast_amt`(추정예탁자산)=계좌 equity.
미배선 시 _unified_equity_krw가 선물 PnL을 완전 무시 → kill-switch가 선물 급락에 무반응.
주식만 쓰는 사용자는 futures_eval_krw 키 부재 → byte-identical(무회귀).

    cd platform/local && python -m pytest tests/test_futures_equity.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp.broker_router import BrokerRouter
from localapp.kis_futures_broker import parse_futures_balance
from localapp.kis_overseas_futures import parse_overseas_deposit
from localapp.trader import _unified_equity_krw


# ── ① parse: prsm_dpast_amt → account.equity ────────────────────────────────────
def test_parse_futures_balance_extracts_equity():
    resp = {"output1": [], "output2": {
        "prsm_dpast_amt": "499831830", "mgna_tota": "57843825",
        "ord_psbl_cash": "442156175", "dnca_cash": "500000000", "futr_evlu_pfls_amt": "-162500"}}
    acct = parse_futures_balance(resp)["account"]
    assert acct["equity"] == 499831830.0
    assert acct["margin_total"] == 57843825.0 and acct["eval_pnl"] == -162500.0


# ── ② _unified_equity_krw: 선물 합산 + 미존재 시 주식 보존 ──────────────────────────
def test_unified_equity_includes_futures():
    assert _unified_equity_krw({"total_eval": 1_000_000, "futures_eval_krw": 500_000}) == 1_500_000


def test_unified_equity_stock_only_unchanged():
    # 선물 키 부재 = 종전 동작 (byte-identical)
    bal = {"total_eval": 1_000_000, "foreign_eval_krw": 200_000, "cash_usd": 100, "fx_usdkrw": 1300}
    assert _unified_equity_krw(bal) == 1_000_000 + 200_000 + 100 * 1300


# ── US-F2: 장중 kill-switch equity 사본 drift — trader와 단일 출처 ──────────────────
def test_intraday_ks_equity_matches_trader_and_includes_futures():
    """장중 kill-switch monitor가 쓰는 _ks_unified_equity_krw가 trader._unified_equity_krw와
    동일(선물 합산). 사본이 futures_eval_krw를 누락해 장중 kill-switch가 선물 손익을
    무시하던 drift(국내·해외 선물 공통)를 단일 출처로 닫는다."""
    from localapp.intraday_stop import _ks_unified_equity_krw as ks_eq
    bal = {"total_eval": 1_000_000, "futures_eval_krw": 500_000,
           "foreign_eval_krw": 200_000, "cash_usd": 100.0, "fx_usdkrw": 1300.0}
    assert ks_eq(bal) == _unified_equity_krw(bal)                          # 단일 출처(drift 없음)
    assert ks_eq(bal) == 1_000_000 + 500_000 + 200_000 + 100 * 1300        # 선물 포함
    assert ks_eq({"total_eval": 1_000_000}) == 1_000_000                   # 주식만 보존


# ── ③ BrokerRouter: 선물계좌 equity를 balance에 병합 ───────────────────────────────
class _Stock:
    def account_snapshot(self, overseas=True):
        return {"balance": {"total_eval": 1_000_000}, "positions": []}


class _Fut:
    def account_snapshot(self):
        return {"account": {"equity": 499_831_830}, "positions": []}


def test_router_merges_futures_equity_into_balance():
    router = BrokerRouter(_Stock(), _Fut(), resolve=lambda s: s)
    bal = router.account_snapshot()["balance"]
    assert bal["futures_eval_krw"] == 499_831_830
    assert bal["total_eval"] == 1_000_000           # 주식 보존


def test_router_stock_only_no_futures_key():
    router = BrokerRouter(_Stock(), None, resolve=lambda s: s)
    bal = router.account_snapshot()["balance"]
    assert "futures_eval_krw" not in bal             # 주식 byte-identical


# ── ④ G1/G3: 해외선물 예수금현황(OTFM1411R) → 사이징 예산·kill-switch equity ──────────
def test_parse_overseas_deposit_extracts_account_fields():
    """공식 스펙(OTFM1411R, CRCY_CD=TKR) output → order_cash·equity 추출."""
    resp = {"output": {
        "fm_ord_psbl_amt": "13700000",          # 주문가능금액(KRW) → 사이징 예산 base(G1)
        "fm_tot_asst_evlu_amt": "15000000",     # 총자산평가금액(KRW) → kill-switch equity(G3)
        "fm_brkg_mgn_amt": "5000000",           # 위탁증거금
        "fm_fuop_evlu_pfls_amt": "-120000"}}    # 선물옵션평가손익
    acct = parse_overseas_deposit(resp)
    assert acct["order_cash"] == 13_700_000.0
    assert acct["equity"] == 15_000_000.0
    assert acct["margin_total"] == 5_000_000.0 and acct["eval_pnl"] == -120_000.0


def test_parse_overseas_deposit_empty_account():
    # 빈/누락 응답 → 0(graceful)
    acct = parse_overseas_deposit({"output": {}})
    assert acct["order_cash"] == 0.0 and acct["equity"] == 0.0


class _OvFut:
    """해외선물 컨텍스트만 구성된 브로커 더블 — overseas_account_snapshot이 positions+account 반환."""
    domestic_configured = False
    overseas_configured = True

    def overseas_account_snapshot(self):
        return {"positions": [{"symbol": "GCM26", "side": "long", "qty": 2,
                               "contract_code": "GCM26", "avg_price": 2600.0}],
                "account": {"order_cash": 13_700_000.0, "equity": 15_000_000.0,
                            "margin_total": 5_000_000.0, "eval_pnl": -120_000.0}}


def test_router_merges_overseas_futures_account_into_balance():
    """G1/G3: 해외선물 예수금현황이 futures_order_cash(사이징 예산)·futures_eval_krw(kill-switch)에
    KRW로 합산(둘 다 TKR KRW환산이라 FX 추측 없음). 종전엔 해외 account 미노출로 둘 다 0이었다."""
    router = BrokerRouter(_Stock(), _OvFut(), resolve=lambda s: s)
    bal = router.account_snapshot()["balance"]
    assert bal["futures_order_cash"] == 13_700_000.0    # 해외선물 계좌 주문가능 → 사이징 예산(G1)
    assert bal["futures_eval_krw"] == 15_000_000.0      # 해외선물 총자산 → 통합 equity(G3)
    assert bal["total_eval"] == 1_000_000               # 주식 보존


def test_real_overseas_account_snapshot_combines_positions_and_deposit(monkeypatch):
    """실제 overseas_account_snapshot이 미결제내역(OTFM1412R positions)+예수금현황(OTFM1411R
    account)을 두 번 호출해 {positions, account}로 결합한다."""
    from localapp.kis_futures_broker import KisFuturesBroker
    b = object.__new__(KisFuturesBroker)
    b._ov_base, b._ov_cano, b._ov_acnt_prdt_cd = "https://y", "47272165", "08"
    monkeypatch.setattr(b, "_ov_headers", lambda tr: {})          # 토큰 우회

    def fake_read_get(url, headers, params):
        if "inquire-unpd" in url:        # 미결제내역(positions)
            return {"output": [{"ovrs_futr_fx_pdno": "GCM26", "sll_buy_dvsn_cd": "02",
                                "fm_ustl_qty": "2", "fm_ccld_avg_pric": "2600",
                                "fm_now_pric": "2610", "fm_evlu_pfls_amt": "2000",
                                "crcy_cd": "USD"}]}
        if "inquire-deposit" in url:      # 예수금현황(account)
            return {"output": {"fm_ord_psbl_amt": "13700000",
                               "fm_tot_asst_evlu_amt": "15000000",
                               "fm_brkg_mgn_amt": "5000000", "fm_fuop_evlu_pfls_amt": "0"}}
        return {}
    monkeypatch.setattr(b, "_read_get", fake_read_get)

    snap = b.overseas_account_snapshot()
    assert len(snap["positions"]) == 1 and snap["positions"][0]["symbol"] == "GCM26"
    assert snap["account"]["order_cash"] == 13_700_000.0
    assert snap["account"]["equity"] == 15_000_000.0


def test_overseas_account_snapshot_graceful_when_deposit_fails(monkeypatch):
    """예수금현황 조회 실패 시 positions는 보존하고 account만 생략(graceful)."""
    from localapp.kis_futures_broker import KisFuturesBroker
    b = object.__new__(KisFuturesBroker)
    b._ov_base, b._ov_cano, b._ov_acnt_prdt_cd = "https://y", "47272165", "08"
    monkeypatch.setattr(b, "_ov_headers", lambda tr: {})

    def fake_read_get(url, headers, params):
        if "inquire-unpd" in url:
            return {"output": []}        # 빈 포지션
        raise RuntimeError("deposit 500")
    monkeypatch.setattr(b, "_read_get", fake_read_get)

    snap = b.overseas_account_snapshot()
    assert snap["positions"] == [] and "account" not in snap   # positions 보존·account 생략

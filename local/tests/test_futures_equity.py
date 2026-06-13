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

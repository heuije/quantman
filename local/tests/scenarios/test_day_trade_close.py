"""Workbench 시나리오 — 당일매매(hold_days=0) 종가 청산 사이클 E2E (Stage B).

당일매매 전략(아침 시가 진입 → 같은 날 종가 청산)을 SimBroker 위 production trader로
검증한다:
  1. 진입(strategy_buy_and_fill) → 원장에 포지션.
  2. 일반 사이클(같은 날, is_close=False) → 당일매매 미청산(종가까지 보유).
  3. liquidate_day_trades(종가 사이클, is_close=True) → 매도 발주 → 체결 → FLAT.
  4. instrument_class 라우팅: 주식 당일매매를 "futures"로 청산하면 무동작(반대도).
  5. 비당일(hold_days>=1) 포지션은 종가 사이클이 건드리지 않음.

    cd platform/local && python -m pytest tests/scenarios/test_day_trade_close.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from sim import invariants, scenario

_DUMMY_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "right": {"op": "const", "params": {"value": 0}}},
}


def _ir_def(symbol, hold_days):
    """단일 종목 당일/비당일 IR 전략 — 100% 전액 사이징, hold_days로 청산."""
    return {
        "name": "당일매매" if hold_days == 0 else "보유전략", "engine": "ir",
        "universe": {"kind": "single", "symbols": [symbol]},
        "signal": _DUMMY_SIGNAL,
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 10},
                     "entry": {"mode": "on_signal"},
                     "exit": {"hold_days": hold_days}, "overlays": {}},
        "simulation": {},
    }


def _dataset(symbol, closes):
    idx = pd.date_range("2026-05-01", periods=len(closes), freq="B")
    return {symbol: pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)}


def _enter_day_trade(t, broker, symbol, sid, fill=70000.0):
    """당일매매 포지션 진입 → 원장 적재 + SimBroker 실보유 반영(L-04 클램프 통과용)."""
    broker._prices[symbol] = fill
    ds = _dataset(symbol, [fill] * 8)
    ir = _ir_def(symbol, hold_days=0)
    order_no, decisions = scenario.strategy_buy_and_fill(
        t, broker, sid, ir, symbol, ds, fill_price=fill, equity=10_000_000.0)
    assert order_no is not None, decisions
    qty = t.ledger[sid]["qty"]
    # 종가 청산의 L-04 클램프가 KIS 실보유를 확인 → SimBroker 잔고에 반영
    broker.set_positions([{"symbol": symbol, "qty": qty}])
    return ds, qty


def test_day_trade_close_cycle_market(isolated_trader):
    """당일매매(시장가): 일반 사이클은 보유 유지 → 종가 사이클이 청산 → FLAT."""
    t, broker = isolated_trader
    sid = "dt_mkt"
    ds, qty = _enter_day_trade(t, broker, "005930", sid)

    # 2) 같은 날 일반 사이클 — 당일매매는 종가까지 보유(미청산)
    strategies = [{"id": sid, "name": "당일매매", "definition": t.ledger[sid]["definition"]}]
    t.cycle(strategies, ds, buy_candidates=[], market="KRX")
    assert sid in t.ledger, "일반 사이클이 당일매매를 청산하면 안 됨(종가까지 보유)"

    # 3) 종가 사이클 — 당일매매 청산 발주(SimBroker 체결은 명시 주입 전이므로 submit만 확인)
    n_before = len(broker.submitted)
    payload = t.liquidate_day_trades(ds, "stock", market="KRX")
    assert len(broker.submitted) == n_before + 1, broker.submitted
    sell = broker.submitted[-1]
    assert sell["side"] == "sell" and sell["qty"] == qty, broker.submitted
    assert sell["limit"] == 0, "국내 종가청산은 시장가 → limit=0"
    assert payload["cycle_summary"]["kind"] == "day_trade_close"

    # 4) 체결 → FLAT (실 WS 체결 경로 → 원장 청산)
    scenario.inject_ws_fill(t, broker, sell["order_no"], qty, 70000.0)
    assert sid not in t.ledger
    invariants.check_all(t)


def test_day_trade_close_instrument_routing(isolated_trader):
    """주식 당일매매를 instrument_class='futures'로 청산 → 무동작(반대 라우팅)."""
    t, broker = isolated_trader
    sid = "dt_route"
    ds, _qty = _enter_day_trade(t, broker, "005930", sid)

    n_before = len(broker.submitted)
    t.liquidate_day_trades(ds, "futures", market="KRX")   # 주식 보유 — 선물 청산은 무시
    assert len(broker.submitted) == n_before, "주식 보유는 futures 청산이 건드리면 안 됨"
    assert sid in t.ledger


def test_day_trade_close_skips_non_day_trade(isolated_trader):
    """비당일(hold_days>=1) 포지션은 종가 사이클이 건드리지 않음."""
    t, broker = isolated_trader
    broker._prices["005930"] = 70000.0
    ds = _dataset("005930", [70000.0] * 8)
    sid = "hold5"
    ir = _ir_def("005930", hold_days=5)
    order_no, _ = scenario.strategy_buy_and_fill(
        t, broker, sid, ir, "005930", ds, fill_price=70000.0, equity=10_000_000.0)
    assert order_no is not None
    broker.set_positions([{"symbol": "005930", "qty": t.ledger[sid]["qty"]}])

    n_before = len(broker.submitted)
    t.liquidate_day_trades(ds, "stock", market="KRX")
    assert len(broker.submitted) == n_before, "hold_days>=1은 종가 사이클 청산 대상 아님"
    assert sid in t.ledger

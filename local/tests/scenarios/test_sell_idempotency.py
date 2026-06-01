"""REV-C 회귀 — 매도 멱등 게이트가 모든 매도 경로에 적용되는가(INV-FILL-1/이중매도).

L-01 intent journal의 매도 중복 차단(intents.is_active "sell")이 EOD cycle 청산
패스에만 있고, 장중 tick 손절(IntradayStopManager.on_tick)·catch-up 손절은 우회했다.
두 경로가 같은 포지션을 동시 평가하면(예: 장중 재기동 시 catch-up + loop 동시 기동)
첫 매도가 아직 미체결이라 KIS 잔고가 안 줄어 L-04 클램프도 못 막아 이중매도가 난다.

intent journal을 _submit_sell 진입부의 단일 게이트로 끌어올려 전 경로가 공유하게 한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from sim import scenario

_DUMMY_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "right": {"op": "const", "params": {"value": 0}}},
}


def _ir_def(exit_spec=None):
    return {
        "name": "멱등 전략", "engine": "ir",
        "universe": {"kind": "list", "symbols": ["005930", "000660"]},
        "signal": _DUMMY_SIGNAL,
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 10},
                     "entry": {"mode": "on_signal"},
                     "exit": exit_spec or {}, "overlays": {}},
        "simulation": {}, "sweep": {"axis": "none"},
    }


def _dataset(closes):
    idx = pd.date_range("2026-05-01", periods=len(closes), freq="B")
    return {"005930": pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)}


_DS = _dataset([90000, 85000, 82000, 80000, 78000, 80000, 75000, 70000])


def test_no_double_sell_across_two_stop_managers(isolated_trader):
    """장중 loop manager + catch-up이 새로 만든 manager가 같은 포지션을 손절 평가해도
    매도는 1회만 발주(intent journal 단일 멱등). 첫 매도 미체결이라 KIS 잔고 미감소."""
    from localapp.intraday_stop import IntradayStopManager

    t, broker = isolated_trader
    ir = _ir_def(exit_spec={"stop_loss": -5.0})
    scenario.strategy_buy_and_fill(
        t, broker, "s1", ir, "005930", _DS, fill_price=70000.0, equity=10_000_000.0)
    qty = t.ledger["s1"]["qty"]
    broker.set_positions([{"symbol": "005930", "qty": qty}])   # 보유 유지(미체결)
    broker._prices["005930"] = 66000                            # -5.7% 손절선 아래

    def _mgr():
        m = IntradayStopManager(broker=broker, get_ledger=lambda: t.ledger,
                                 submit_sell_fn=t._submit_sell, dataset=_DS)
        m.reset_daily()
        return m

    _mgr().on_tick("005930", 66000.0)      # 1차 경로(장중 loop)
    _mgr().on_tick("005930", 66000.0)      # 2차 경로(catch-up — 새 manager, 빈 sold_today)

    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert len(sells) == 1, f"이중매도 발생: {len(sells)}건 {sells}"

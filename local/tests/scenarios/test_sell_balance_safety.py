"""Workbench 시나리오 — 매도 수량 잔고 정합(자금 안전).

한 부류: "발주 직전 ledger 수량을 KIS 실 보유로 클램프"가 모든 매도 경로에
적용되는가. 두 회귀를 잠근다.

  · #1 catch-up 손절: PC를 장중에 켰을 때 보유분 즉시 손절 평가가 실제로
    매도를 발주해야 한다. (v0.9.17까지 IntradayStopManager 인자 순서 오류로
    on_tick이 AttributeError로 죽어 단 한 건도 발주 못 함.)
  · #4 EOD cycle 청산: 08:55 청산이 ledger 수량을 그대로 쓰지 않고 KIS 실
    보유로 클램프해야 한다. (intraday 손절엔 L-04 클램프가 있으나 EOD cycle
    청산엔 없어 비대칭 — 외부 수동매도 시 over-sell.)

    cd platform/local && python -m pytest tests/scenarios/test_sell_balance_safety.py -q
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
        "name": "잔고안전 전략", "engine": "ir",
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


def test_catchup_stop_loss_fires_sell(isolated_trader):
    """#1 — catch-up 손절이 폭락 보유분에 대해 실제 매도를 발주한다.

    PC가 꺼져 있다 장중에 켜졌을 때 보유분 즉시 손절 평가(_catchup_stop_loss).
    """
    from localapp.catchup import _catchup_stop_loss

    t, broker = isolated_trader
    broker._prices["005930"] = 70000
    sid = "cu1"
    ir = _ir_def(exit_spec={"stop_loss": -5.0})
    order_no, _ = scenario.strategy_buy_and_fill(
        t, broker, sid, ir, "005930", _DS, fill_price=70000.0, equity=10_000_000.0)
    assert order_no is not None
    qty = t.ledger[sid]["qty"]
    broker.set_positions([{"symbol": "005930", "qty": qty}])

    # 장중 -5.7% 폭락 — 손절선(-5%) 아래
    broker._prices["005930"] = 66000

    res = _catchup_stop_loss("KRX", broker, t)

    # 핵심 안전 동작 = 실제 매도가 KIS(broker)로 발주됐는가. (res["fired"]는
    # manager.decisions 증가분이라 미체결 발주를 동기적으로 세지 않으므로 신호로 부적합.)
    assert res["error"] is None, res
    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert len(sells) == 1, f"catch-up 손절이 매도 발주 안 함: {broker.submitted}"
    assert sells[0]["qty"] == qty, f"발주 수량 불일치: {sells[0]}"


def test_eod_liquidation_clamps_to_broker_qty(isolated_trader):
    """#4 — EOD cycle 청산이 ledger가 아닌 KIS 실 보유 수량으로 클램프한다.

    사용자가 HTS로 일부 수동매도 → ledger는 그대로. 킬스위치로 전량 청산을
    강제하고, 매도 발주 수량이 broker 보유(4)로 클램프되는지 확인.
    """
    from localapp import killswitch

    t, broker = isolated_trader
    broker._prices["005930"] = 70000
    sid = "eod1"
    ir = _ir_def(exit_spec={"stop_loss": -5.0})
    order_no, _ = scenario.strategy_buy_and_fill(
        t, broker, sid, ir, "005930", _DS, fill_price=70000.0, equity=10_000_000.0)
    assert order_no is not None
    ledger_qty = t.ledger[sid]["qty"]
    assert ledger_qty > 4, ledger_qty

    # 사용자가 HTS/MTS에서 일부 수동 매도 → KIS 잔고엔 4주만
    broker.set_positions([{"symbol": "005930", "qty": 4, "avg_price": 70000}])
    killswitch.activate("테스트 — 전량 청산 강제")

    t.cycle(strategies=[], dataset=_DS, buy_candidates=[],
            risk_limits={"kill_switch_daily_loss_pct": 3.0}, market="KRX")

    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert len(sells) == 1, f"청산 매도 1건 기대: {sells}"
    assert sells[0]["qty"] == 4, \
        f"EOD 청산이 broker 보유(4)로 클램프 안 됨 — over-sell qty={sells[0]['qty']}"

"""엔진이 일별 비중 패널을 노출하는가 — 일반화 비교의 기질(substrate).

scheduled 백테스트는 종목별 포지션을 매일 추적한다(engine.py NAV 계산). 그 end-of-day
비중(shares×close/nav)을 패널로 노출하면, 기여(=비중×종목수익)·고유성과를 사후 계산해
compare_by_partition으로 섹터·레짐 등 임의 기준 비교가 된다.

    cd platform/core && python -m pytest tests/test_contribution_panel.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.blocks import data
from quant_core.ir_engine import (Entry, PositionSpec, Sizing, SimSpec,
                                   StrategyIR, Universe, run_strategy_ir)


def _dataset():
    idx = pd.date_range("2020-01-01", periods=120, freq="B")

    def mk(drift):
        close = 100 * (1 + drift) ** np.arange(120)
        return pd.DataFrame({"Open": close, "High": close * 1.001,
                             "Low": close * 0.999, "Close": close, "Volume": 1e6,
                             "momentum_12_1m": 1.0}, index=idx)
    return {"AAA": mk(0.002), "BBB": mk(-0.001), "CCC": mk(0.001)}


def _scheduled_ir():
    return StrategyIR(
        signal=data("momentum_12_1m"), universe=Universe(kind="all"),
        position=PositionSpec(direction="long", sizing=Sizing(mode="equal_weight"),
                              entry=Entry(mode="scheduled", rebalance="monthly", top_n=3)),
        simulation=SimSpec(initial_capital=1e7))


def test_scheduled_exposes_weight_panel():
    res = run_strategy_ir(_scheduled_ir(), _dataset())
    assert res["success"], res.get("error")
    w = res.get("weight")
    assert isinstance(w, pd.DataFrame), "엔진이 'weight' 비중 패널을 노출하지 않음"
    assert set(w.columns) <= {"AAA", "BBB", "CCC"}, w.columns.tolist()


def test_weight_panel_sums_to_invested_fraction():
    """전액투자(leverage 1)면 보유일 비중합 ≈ 1(현금 잔여 약간 허용), 음수 없음."""
    res = run_strategy_ir(_scheduled_ir(), _dataset())
    w = res["weight"]
    invested = w.sum(axis=1)
    assert invested.max() <= 1.0001, invested.max()
    assert invested.max() > 0.5, f"실제 보유가 안 일어남: {invested.max()}"
    assert (w.values >= -1e-9).all(), "비중에 음수(롱 전략인데)"

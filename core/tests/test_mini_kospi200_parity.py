"""미니 코스피200선물 — 백테스트 엔진이 승수(50,000=정규 1/5)를 사이징·손익 양쪽에
일관 적용함을 고정(T6 통합검증). 미니는 정규와 동일 KOSPI200 지수를 alias로 공유하고
손익차는 오직 승수에서만 발생한다.

    cd platform/core && python -m pytest tests/test_mini_kospi200_parity.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.blocks import const, data
from quant_core.blocks.node import Node
from quant_core.exec_defaults import instrument_spec, instrument_category, is_futures
from quant_core.ir_engine import StrategyIR, run_strategy_ir

REG = "코스피200선물"
MINI = "미니코스피200선물"


def test_mini_catalog_is_kr_futures_one_fifth_multiplier():
    sp = instrument_spec(MINI)
    assert is_futures(MINI) and instrument_category(MINI) == "kr_futures"
    assert sp.multiplier == instrument_spec(REG).multiplier / 5   # 50,000 = 250,000/5
    assert sp.currency == "KRW" and sp.tick == instrument_spec(REG).tick


def _always_true() -> Node:
    return Node(op="compare", params={"op": ">"},
                inputs={"left": data("Close"), "right": const(0)})


def _bars(n=4, open_=400.0, close=396.0) -> pd.DataFrame:
    idx = pd.date_range("2026-05-27", periods=n, freq="B")
    o = np.full(n, float(open_)); c = np.full(n, float(close))
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c),
                         "Close": c, "Volume": np.full(n, 1e6)}, index=idx)


def _fixed_amount_ir(symbol: str, amount: float) -> StrategyIR:
    return StrategyIR.model_validate({
        "name": "t", "universe": {"kind": "single", "symbols": [symbol]},
        "signal": _always_true().model_dump(),
        "position": {"direction": "long",
                     "sizing": {"mode": "fixed_amount", "amount_krw": amount},
                     "entry": {"mode": "on_signal"}, "exit": {"hold_days": 0}, "overlays": {}},
        "simulation": {"initial_capital": 1e9, "commission": 0.0, "slippage": 0.0, "sell_tax": 0.0},
    })


def test_mini_backtest_exposure_parity_with_regular():
    """동일 예산(fixed_amount)·동일 지수 가격 → 미니 총손익 == 정규 총손익.

    fixed_amount는 예산이 자본과 무관해 매 바 계약수가 일정하다. 미니는 계약당 증거금이
    1/5라 같은 예산으로 5배 계약을 잡지만 계약당 손익도 1/5 → 명목 노출·총손익이 정규와
    정확히 같다. 엔진이 instrument_spec.multiplier를 **사이징과 손익 양쪽에** 일관 적용함을
    보증(어느 한쪽만 적용되면 패리티가 깨진다). 미니 가격은 정규 지수와 동일하므로 같은 bars 주입."""
    b = _bars()
    rr = run_strategy_ir(_fixed_amount_ir(REG, 1e8), {REG: b})
    rm = run_strategy_ir(_fixed_amount_ir(MINI, 1e8), {MINI: b})
    assert rr["success"] and rm["success"], (rr.get("error"), rm.get("error"))
    pnl_reg = float(rr["equity"].iloc[-1]) - 1e9
    pnl_mini = float(rm["equity"].iloc[-1]) - 1e9
    assert pnl_reg < 0                                  # 손실 트레이드(진입>청산)
    assert abs(pnl_mini - pnl_reg) < 1e-6, (pnl_mini, pnl_reg)


def test_mini_per_contract_pnl_is_one_fifth():
    """계약당 손익은 승수에 비례 — 미니 1계약 손익 = 정규 1계약 × 1/5(승수 정의)."""
    delta = -4.0                                        # 396 - 400
    reg_per_contract = delta * instrument_spec(REG).multiplier
    mini_per_contract = delta * instrument_spec(MINI).multiplier
    assert mini_per_contract == reg_per_contract / 5

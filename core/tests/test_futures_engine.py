"""선물 엔진 회계 — 승수(point value) PnL·증거금 레버리지·숏 대칭.

scheduled/always 경로에서 선물 심볼이 (1) 명목이 자본을 넘어도 증거금으로 보유되고
(2) 증거금 레버리지로 가격 1% 변동이 자본 ~10%로 증폭되며 (3) 숏은 하락에서 이익(차입
불필요)인지 검증. 주식 회귀(mult=1·mr=1 항등)는 기존 78 테스트가 보장한다.

    cd platform/core && python -m pytest tests/test_futures_engine.py -q
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
from quant_core.exec_defaults import instrument_spec
from quant_core.ir_engine import (Entry, PositionSpec, SimSpec, Sizing,
                                   StrategyIR, Universe, run_strategy_ir)


def _ds(symbol: str, closes) -> dict:
    idx = pd.date_range("2020-01-01", periods=len(closes), freq="B")
    c = np.asarray(closes, dtype=float)
    return {symbol: pd.DataFrame(
        {"Open": c, "High": c, "Low": c, "Close": c, "Volume": 1e6,
         "momentum_12_1m": 1.0}, index=idx)}


def _always(symbol: str, direction: str = "long") -> StrategyIR:
    # always 보유 마스크(momentum=1.0 상수 양수) — 매일 목표비중으로 풀보유. 비용 0으로 순효과 격리.
    return StrategyIR(
        signal=data("momentum_12_1m"),
        universe=Universe(kind="single", symbols=[symbol]),
        position=PositionSpec(direction=direction, sizing=Sizing(mode="equal_weight"),
                              entry=Entry(mode="always")),
        simulation=SimSpec(initial_capital=1e7, commission=0.0, slippage=0.0, fill="close"))


_UP = [400.0] * 20 + [404.0] * 10      # +1%
_DOWN = [400.0] * 20 + [396.0] * 10    # -1%


def test_catalog_kospi_multiplier():
    s = instrument_spec("코스피200선물")
    assert s.multiplier == 250_000.0 and s.init_margin_rate == 0.10


def test_futures_margin_leverage_amplifies_return():
    """KOSPI200선물(승수 250,000·증거금 0.10): 1% 가격상승 → ~10% 자본수익(10x).
    명목(100M/계약) > 자본(10M)이라도 증거금으로 보유 — 현금모델이면 0거래로 평탄(~0%)."""
    res = run_strategy_ir(_always("코스피200선물"), _ds("코스피200선물", _UP))
    assert res["success"], res.get("error")
    eq = res["equity"]
    ret = eq.iloc[-1] / eq.iloc[0] - 1
    assert ret > 0.05, f"선물 레버리지 미작동(수익 {ret:.3%}) — 현금모델이면 ~0"
    assert abs(ret - 0.10) < 0.03, f"증거금 레버리지 배수 이상: {ret:.3%}"


def test_equity_same_path_no_leverage():
    """같은 1% 경로를 주식(005930)으로: 승수=1·증거금=1 → ~1%(레버리지 없음).
    선물과의 차이가 곧 승수·증거금 회계의 효과(백워드 호환 교차확인)."""
    res = run_strategy_ir(_always("005930"), _ds("005930", _UP))
    assert res["success"], res.get("error")
    ret = res["equity"].iloc[-1] / res["equity"].iloc[0] - 1
    assert abs(ret) < 0.02, f"주식인데 레버리지 효과({ret:.3%})"


def test_futures_short_profits_on_drop():
    """선물 숏: 가격 하락이 이익(승수 적용·차입 불필요)."""
    res = run_strategy_ir(_always("코스피200선물", direction="short"),
                          _ds("코스피200선물", _DOWN))
    assert res["success"], res.get("error")
    ret = res["equity"].iloc[-1] / res["equity"].iloc[0] - 1
    assert ret > 0.05, f"선물 숏이 하락에서 이익 안 남: {ret:.3%}"

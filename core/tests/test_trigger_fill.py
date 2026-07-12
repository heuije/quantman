# -*- coding: utf-8 -*-
"""fill="trigger"(장중 돌파의 보수 일봉 근사) + watchlist_trigger_v1 매처 — P2 핵심 계약.

체결 규칙(장중 템플릿 설계 §4): 정규형 트리거 신호(high_change_1d ≥ X — 당일 고가가 전일
종가 대비 임계 도달)가 참인 바에서 체결가 = max(시가, 전일종가×(1+X%)). 경로 무지는 항상
불리한 방향(갭 상승=시가 체결). 요건 위반은 S-trigger/S-template 명시 거부(조용한 오독 차단).

픽스처 트릭: Close=100 고정·Open=111 고정·트리거일 High만 급등 — 체결가가 정확히
경계가(125)/갭 시가(133)로 떨어지는지 값으로 판별(비용 0 설정).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_core.indicators import add_returns
from quant_core.ir_engine import StrategyIR
from quant_core.ir_engine.run import run_strategy_ir
from quant_core.ir_engine.spec import validate_strategy
from quant_core.ir_engine.templates import watchlist_params

_N = 8
_IDX = pd.bdate_range("2026-01-05", periods=_N)


def _ds(open3: float = 111.0):
    closes = np.full(_N, 100.0)
    opens = np.full(_N, 111.0)
    opens[3] = open3
    highs = np.full(_N, 105.0)
    highs[3] = 140.0                       # day3 장중 +40% 도달(임계 25% 초과)
    df = pd.DataFrame({"Open": opens, "High": highs, "Low": np.full(_N, 95.0),
                       "Close": closes, "Volume": 1e6}, index=_IDX)
    df["high_change_1d"] = (df["High"] / df["Close"].shift(1) - 1) * 100
    return {"돌파주": df}


def _ir(thr: float = 25.0, **over):
    ir = {
        "name": "돌파", "query": "simulate",
        "universe": {"kind": "single", "symbols": ["돌파주"]},
        "signal": {"op": "compare", "params": {"op": ">="}, "inputs": {
            "left": {"op": "data", "params": {"ref": "__SELF__.high_change_1d"}},
            "right": {"op": "const", "params": {"value": thr}}}},
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 100},
                     "entry": {"mode": "on_signal"},
                     "exit": {"hold_days": 1, "fill": "next_open"}},
        "simulation": {"initial_capital": 1_000_000.0, "fill": "trigger",
                       "commission": 0.0, "slippage": 0.0, "sell_tax": 0.0},
    }
    ir.update(over)
    return ir


def _first_trade(ir: dict, ds=None):
    res = run_strategy_ir(StrategyIR.model_validate(ir), ds or _ds())
    tr = res.get("trades")
    assert tr is not None and len(tr) == 1, f"거래 1건이어야: {res.get('error')}"
    row = tr.iloc[0]
    return float(row["진입가"]), float(row["청산가"])


# ── 엔진: 보수 체결 규칙 ──────────────────────────────────────────────────────

def test_trigger_fills_at_boundary_price():
    """시가(111) < 경계가(100×1.25=125) → 경계가 체결. 청산은 익일 시가(111)."""
    entry, exit_ = _first_trade(_ir())
    assert entry == pytest.approx(125.0)
    assert exit_ == pytest.approx(111.0)


def test_trigger_gap_open_fills_at_open():
    """갭 상승(시가 133 > 경계가 125) → 시가 체결(불리한 방향 — 보수 규칙)."""
    entry, _ = _first_trade(_ir(), ds=_ds(open3=133.0))
    assert entry == pytest.approx(133.0)


def test_trigger_no_signal_no_trade():
    """임계 미달(50%)이면 무거래 — 판정의 단일 출처는 신호(high_change_1d)."""
    res = run_strategy_ir(StrategyIR.model_validate(_ir(thr=50.0)), _ds())
    tr = res.get("trades")
    assert tr is None or len(tr) == 0


def test_trigger_requires_canonical_signal_engine_guard():
    ir = _ir()
    ir["signal"]["inputs"]["left"]["params"]["ref"] = "__SELF__.pct_change_1d"
    res = run_strategy_ir(StrategyIR.model_validate(ir), _ds())
    assert not res.get("success", True) and "정규형" in str(res.get("error", ""))


# ── validator: S-trigger 요건 ─────────────────────────────────────────────────

def _s(issues, rule):
    return [i for i in issues if getattr(i, "rule", "") == rule]


def test_validator_canonical_passes():
    issues = validate_strategy(StrategyIR.model_validate(_ir()))
    assert _s(issues, "S-trigger") == []


@pytest.mark.parametrize("mutate", [
    lambda ir: ir["position"]["entry"].update(mode="scheduled"),
    lambda ir: ir["position"].update(direction="short"),
    lambda ir: ir["signal"]["inputs"]["left"]["params"].update(ref="__SELF__.pct_change_1d"),
])
def test_validator_rejects_deviation(mutate):
    ir = _ir()
    mutate(ir)
    issues = validate_strategy(StrategyIR.model_validate(ir))
    assert _s(issues, "S-trigger"), issues


# ── 지표: high_change_1d — Close-only 시리즈 무가드 부류(#343) 재발 방지 ───────

def test_high_change_indicator_and_close_only_nan():
    df = _ds()["돌파주"].drop(columns=["high_change_1d"])
    out = add_returns(df)
    assert out["high_change_1d"].iloc[3] == pytest.approx(40.0)
    close_only = pd.DataFrame({"Close": np.arange(10.0) + 100}, index=pd.bdate_range("2026-01-05", periods=10))
    out2 = add_returns(close_only)                     # KeyError 없이 NaN 컬럼
    assert out2["high_change_1d"].isna().all()


# ── watchlist_trigger_v1 매처 ─────────────────────────────────────────────────

def _wl_ir(**over):
    ir = _ir()
    ir["universe"] = {"kind": "list", "symbols": ["123450", "005930"]}
    ir["position"]["exit"] = {"hold_days": 3}
    ir["signal"]["inputs"]["right"]["params"]["value"] = 10.0
    ir["template"] = {"id": "watchlist_trigger_v1", "max_daily_entries": 2}
    ir.update(over)
    return ir


def test_watchlist_canonical_passes():
    issues = validate_strategy(StrategyIR.model_validate(_wl_ir()))
    assert _s(issues, "S-template") == [], issues


@pytest.mark.parametrize("mutate", [
    lambda ir: ir.update(universe={"kind": "all"}),
    lambda ir: ir.update(universe={"kind": "list",
                                   "symbols": [f"{i:06d}" for i in range(21)]}),
    lambda ir: ir["signal"]["inputs"]["right"]["params"].update(value=35.0),
    lambda ir: ir["position"]["exit"].pop("hold_days"),
    lambda ir: ir["simulation"].update(fill="close"),
    lambda ir: ir["universe"].update(symbols=["AAPL", "005930"]),
])
def test_watchlist_deviation_rejected(mutate):
    ir = _wl_ir()
    mutate(ir)
    issues = validate_strategy(StrategyIR.model_validate(ir))
    assert _s(issues, "S-template"), issues


def test_watchlist_params_extraction():
    p = watchlist_params(StrategyIR.model_validate(_wl_ir()))
    assert p == {"threshold_pct": 10.0, "symbols": ["123450", "005930"],
                 "max_entries": 2}

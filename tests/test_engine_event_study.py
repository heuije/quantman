"""A1+A2 — 유의성 배선(조건축) + 이벤트 스터디(시간축) 회귀.

명세 §4·§5. 직전 사용례(WTI 돌파 후 forward 수익 × vol 국면 × 유의성)를 고정한다.

    cd platform && pytest tests/test_engine_event_study.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.blocks import Node, const, data  # noqa: E402
from quant_core.ir_engine import (  # noqa: E402
    Entry, PositionSpec, Sizing, SimSpec, StrategyIR, Study, Universe, run_query,
)


# ── A1: 조건축 sweep에 유의성 포함 ────────────────────────────────────────────

def _multi():
    idx = pd.date_range("2020-01-01", periods=252, freq="B")

    def mk(drift, mom):
        close = 100 * (1 + drift) ** np.arange(252)
        return pd.DataFrame({
            "Open": close, "High": close * 1.001, "Low": close * 0.999,
            "Close": close, "Volume": 1e6, "momentum_12_1m": float(mom),
        }, index=idx)
    d = {"AAA": mk(0.003, 10.0), "BBB": mk(-0.001, -5.0), "CCC": mk(0.001, 2.0)}
    d["VIX"] = pd.DataFrame(
        {"Close": np.where(np.arange(252) < 126, 15.0, 35.0)}, index=idx)
    return d


def test_condition_sweep_includes_significance():
    s = StrategyIR(
        signal=data("momentum_12_1m"), universe=Universe(kind="all"),
        position=PositionSpec(direction="long", sizing=Sizing(mode="equal_weight"),
                              entry=Entry(mode="scheduled", rebalance="monthly", top_n=1)),
        simulation=SimSpec(initial_capital=1e7),
        query="simulate",
        study=Study(axis="label", reduction="contrast",
                    label=Node(op="bucket", params={"edges": [25.0]},
                               inputs={"signal": data("VIX.Close")})))
    res = run_query(s, _multi())
    assert res["success"] and res["axis"] == "condition"
    assert "compare" in res                      # A1 — 유의성 동봉
    assert "pairwise" in res["compare"]
    # 두 국면이 있으면 쌍 비교에 p_value 존재
    for v in res["compare"]["pairwise"].values():
        assert "p_value" in v


# ── A2: 이벤트 스터디 (WTI 돌파 후 forward 수익 × vol 국면) ────────────────────

def _wti():
    """WTI 합성 — 60 하향돌파 이벤트가 여러 번 발생하도록 사인파 + 노이즈."""
    idx = pd.date_range("2015-01-01", periods=1000, freq="B")
    t = np.arange(1000)
    close = 65 + 10 * np.sin(t / 30.0) + np.random.default_rng(7).normal(0, 1.5, 1000)
    # 고변동성 구간(후반)에서 반등이 더 크도록 — 선행지표 가설 검증용
    realized_vol = np.where(t < 500, 25.0, 45.0)
    return {"원유선물": pd.DataFrame({
        "Open": close, "High": close * 1.01, "Low": close * 0.99,
        "Close": close, "Volume": 1e6, "realized_vol_20d": realized_vol,
    }, index=idx)}


def _event_strategy(with_regime: bool):
    cross = Node(op="cross", params={"direction": "down"},
                 inputs={"left": data("__SELF__.Close"), "right": const(60.0)})
    study = Study(event=cross, windows=[5, 10, 20])
    if with_regime:
        study.label = Node(op="bucket", params={"edges": [35.0]},
                           inputs={"signal": data("__SELF__.realized_vol_20d")})
    return StrategyIR(
        signal=cross, universe=Universe(kind="single", symbols=["원유선물"]),
        position=PositionSpec(entry=Entry(mode="on_signal")),
        simulation=SimSpec(initial_capital=1e7), query="relate", study=study)


def test_event_study_forward_windows():
    res = run_query(_event_strategy(with_regime=False), _wti())
    assert res["success"] and res["axis"] == "time"
    assert set(res["windows"]) == {"5", "10", "20"}
    assert res["n_events"] > 0
    for w in ("5", "10", "20"):
        o = res["overall"][w]
        assert "mean" in o and "p_value" in o and "prob_positive" in o
        assert o["n"] > 0


def test_event_study_regime_split_significance():
    res = run_query(_event_strategy(with_regime=True), _wti())
    assert res["success"]
    assert res["by_regime"] is not None
    w20 = res["by_regime"]["20"]
    assert "by_regime" in w20 and "pairwise" in w20
    # 저변동성(25.0)·고변동성(45.0) 두 국면 + 쌍 비교 p_value
    assert len(w20["by_regime"]) >= 2
    for v in w20["pairwise"].values():
        assert "p_value" in v and "mean_diff" in v


# ── C+E: 경로지표(MAE/MFE) + basis(close/intraday/excess) ─────────────────────

def test_event_study_path_metrics_present():
    """모든 윈도 요약에 forward 낙폭(MAE)·상승(MFE) 경로지표 포함 — task8·task9."""
    res = run_query(_event_strategy(with_regime=False), _wti())
    for w in ("5", "10", "20"):
        o = res["overall"][w]
        for k in ("mean_mae", "worst_mae", "mean_mfe", "payoff_ratio"):
            assert k in o, f"{k} 누락"
        assert o["mean_mae"] <= o["mean_mfe"]      # 불리편차 ≤ 유리편차


def test_event_study_intraday_basis():
    """basis=intraday — 당일 시가→종가 반등(task13). w=0 포함 경로."""
    s = _event_strategy(with_regime=False)
    s.study.event_basis = "intraday"
    s.study.windows = [0, 1, 3]
    res = run_query(s, _wti())
    assert res["success"] and res["basis"] == "intraday"
    assert res["overall"]["0"]["n"] > 0            # 당일(w=0)도 측정


def test_event_study_excess_basis_multi():
    """basis=excess — 다종목 시장초과 forward(task2). 단일종목이면 검증 거부."""
    cross = Node(op="compare", params={"op": "<"},
                 inputs={"left": data("__SELF__.Close"), "right": const(1e12)})
    s = StrategyIR(
        signal=cross, universe=Universe(kind="list", symbols=["AAA", "BBB", "CCC"]),
        position=PositionSpec(entry=Entry(mode="on_signal")),
        simulation=SimSpec(initial_capital=1e7), query="relate",
        study=Study(event=cross, windows=[5, 10], event_basis="excess"))
    res = run_query(s, _multi())
    assert res["success"] and res["basis"] == "excess"
    assert res["n_events"] > 0


def test_event_study_excess_single_rejected():
    from quant_core.ir_engine import validate_strategy
    s = _event_strategy(with_regime=False)
    s.study.event_basis = "excess"                 # 단일종목 — 시장 지수 불가
    assert any(i.rule == "S-event" and i.is_error for i in validate_strategy(s))


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

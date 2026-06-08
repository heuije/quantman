"""분석층 통합(W1) — target=signal·relation + 통계 보강 검증.

읽기층 일반화: 분석 대상을 '수익률'에서 임의 신호값(signal)·횡단관계(relation/IC)로
확장. 신호 대수는 안 건드린다. pct 스케일·왜도/첨도·블록부트스트랩·잭나이프·IC를 손계산과 대조.

    cd platform && pytest tests/test_analysis_layer.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from quant_core.ir_engine import strategy_from_spec                       # noqa: E402
from quant_core.ir_engine.compare import (                                # noqa: E402
    block_bootstrap_ci, distribution, jackknife_by_year,
)


# ── 데이터 (factor가 forward수익을 단조 예측하도록 구성) ────────────────────────

def _ds():
    """market_cap=[4,3,2,1] 고정, Close는 시총비례 성장 → forward수익 순위 = 시총 순위."""
    idx = pd.date_range("2021-01-01", periods=120, freq="B")
    t = np.arange(120)
    out = {}
    for i, name in enumerate(["S0", "S1", "S2", "S3"]):
        rate = 0.002 * (4 - i)              # S0 최고 성장
        close = 100 * (1 + rate) ** t
        out[name] = pd.DataFrame(
            {"Open": close, "High": close, "Low": close, "Close": close,
             "Volume": 1e6, "market_cap": float(4 - i)}, index=idx)
    return out


def _data(ref):
    return {"op": "data", "params": {"ref": ref}}


def _spec(target, target_node, label=None, windows=None):
    # target=signal → query=describe(신호값 분포), target=relation → query=relate(횡단 IC).
    study = {"target_node": target_node}
    if label is not None:
        study["label"] = label
    if windows is not None:
        study["windows"] = windows
    return {"signal": _data("Close"),
            "universe": {"kind": "list", "symbols": ["S0", "S1", "S2", "S3"]},
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 2}},
            "simulation": {"initial_capital": 1e7},
            "query": "describe" if target == "signal" else "relate",
            "study": study}


# ── 통계층 (compare.py) 단위 ───────────────────────────────────────────────────

def test_distribution_pct_flag_and_moments():
    d = distribution(pd.Series([1.0, 2, 3, 4] * 30), pct=False)
    assert abs(d["mean"] - 2.5) < 1e-9           # 비율값: ×100 안 함
    assert "skew" in d and "kurtosis" in d
    assert "q10" in d["quantiles"] and "q90" in d["quantiles"]
    assert "bootstrap_ci" in d and d["bootstrap_ci"]["low"] <= d["mean"] <= d["bootstrap_ci"]["high"]
    dp = distribution(pd.Series([0.01, 0.02, 0.03, 0.04] * 30), pct=True)
    assert abs(dp["mean"] - 2.5) < 1e-6          # 수익률: 0.025×100=2.5


def test_block_bootstrap_brackets_mean():
    idx = pd.date_range("2019-01-01", periods=252 * 3, freq="B")
    r = pd.Series(0.001, index=idx)
    bb = block_bootstrap_ci(r, block_len=252)
    assert bb["ci_low"] <= bb["mean"] <= bb["ci_high"]
    assert bb["block_len"] == 252


def test_jackknife_finds_outlier_year():
    idx = pd.date_range("2019-01-01", periods=252 * 3, freq="B")
    r = pd.Series(0.001, index=idx)
    r[r.index.year == 2020] = -0.01              # 2020만 음수 outlier
    jk = jackknife_by_year(r)
    assert jk["most_influential"] == 2020


# ── target=signal (신호값 분포) ────────────────────────────────────────────────

def test_signal_study_value_distribution():
    """market_cap 값 분포 — 평균 2.5(=mean[4,3,2,1]), ×100 안 됨(비율 스케일)."""
    res = strategy_from_spec(_spec("signal", _data("market_cap")), _ds())
    assert res["success"], res
    assert res["axis"] == "signal"
    assert abs(res["overall"]["mean"] - 2.5) < 1e-6
    assert "skew" in res["overall"]


def test_signal_study_partition_by_regime():
    """bucket(market_cap, [2.5]) 국면별 — regime0={1,2}→1.5, regime1={3,4}→3.5."""
    label = {"op": "bucket", "params": {"edges": [2.5]}, "inputs": {"signal": _data("market_cap")}}
    res = strategy_from_spec(_spec("signal", _data("market_cap"), label=label), _ds())
    assert res["success"], res
    by_label = res["by_regime"]["by_label"]
    means = sorted(v["mean"] for v in by_label.values())
    assert abs(means[0] - 1.5) < 1e-6 and abs(means[1] - 3.5) < 1e-6


# ── target=relation (횡단 IC) ──────────────────────────────────────────────────

def test_ic_study_perfect_predictor():
    """factor=market_cap가 forward수익을 단조 예측 → 횡단 순위 IC ≈ +1."""
    res = strategy_from_spec(_spec("relation", _data("market_cap"), windows=[5]), _ds())
    assert res["success"], res
    assert res["axis"] == "relation" and res["relation"] == "ic"
    ic = res["by_window"]["5"]["overall"]
    assert ic["mean"] > 0.9                       # IC는 [-1,1] (×100 아님)
    assert ic["prob_positive"] > 99.0


def test_ic_study_requires_two_symbols():
    spec = _spec("relation", _data("market_cap"), windows=[5])
    spec["universe"] = {"kind": "single", "symbols": ["S0"]}
    res = strategy_from_spec(spec, _ds())
    assert not res["success"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

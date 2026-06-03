"""일반화 비교 프리미티브 — 기여/비중 패널을 임의 라벨로 그룹화(atomic·MECE).

재설계 핵심: 섹터별·종목별·레짐별 비교가 *하나의* 메커니즘에서 나온다 — 라벨 λ(종목,일)→그룹.
가산성(Σ그룹 기여 = 전체)이 MECE를 수학적으로 보장. 기존 condition축(시간라벨)이 종목축을
columns[0]으로 붕괴시켜 섹터를 못 했던 결함(CompileLog id=6)을 구조적으로 대체.

    cd platform/core && python -m pytest tests/test_comparison_partition.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.ir_engine.comparison import compare_by_partition

_IDX = pd.date_range("2026-01-01", periods=3, freq="B")
# 기여 패널: contribution[일,종목] — Σ종목 = 포트 일별수익
_CONTRIB = pd.DataFrame(
    {"A": [0.01, -0.005, 0.02], "B": [0.02, 0.01, -0.01]}, index=_IDX)
# 비중 패널: weight[일,종목] — 기여/비중 = 종목 일별수익
_WEIGHT = pd.DataFrame(
    {"A": [0.5, 0.5, 0.5], "B": [0.5, 0.5, 0.5]}, index=_IDX)


def _static_label(mapping: dict) -> pd.DataFrame:
    """종목→그룹 정적 라벨 패널(날짜 상수) — attribute(Sector) 모양."""
    return pd.DataFrame({s: [g] * len(_IDX) for s, g in mapping.items()}, index=_IDX)


def test_sector_partition_makes_multiple_buckets():
    """섹터(종목 정적 라벨)로 그룹화 — 옛 condition축이 못 하던 것."""
    label = _static_label({"A": "Tech", "B": "Fin"})
    res = compare_by_partition(_CONTRIB, _WEIGHT, label)
    assert set(res["buckets"].keys()) == {"Tech", "Fin"}, res["buckets"].keys()


def test_attribution_additivity_holds():
    """가산성(MECE) — 그룹별 일별 기여 합 = 전체 포트 일별수익."""
    label = _static_label({"A": "Tech", "B": "Fin"})
    res = compare_by_partition(_CONTRIB, _WEIGHT, label)
    total_daily = _CONTRIB.sum(axis=1)
    summed = sum(res["buckets"][g]["daily_contribution"] for g in res["buckets"])
    np.testing.assert_allclose(summed.values, total_daily.values, atol=1e-12)


def test_time_regime_partition_also_works():
    """레짐(일 함수 라벨)도 같은 메커니즘 — 1·3일=호황, 2일=불황."""
    label = pd.DataFrame({"A": ["U", "D", "U"], "B": ["U", "D", "U"]}, index=_IDX)
    res = compare_by_partition(_CONTRIB, _WEIGHT, label)
    assert set(res["buckets"].keys()) == {"U", "D"}
    # 불황(D)=2일차만: A=-0.005, B=0.01 → 합 0.005
    np.testing.assert_allclose(
        res["buckets"]["D"]["daily_contribution"].sum(), 0.005, atol=1e-12)


def test_standalone_return_normalizes_by_weight():
    """고유성과 = 기여/비중 (그 그룹만 거래 시 수익). Tech 1일차: 0.01/0.5 = 0.02."""
    label = _static_label({"A": "Tech", "B": "Fin"})
    res = compare_by_partition(_CONTRIB, _WEIGHT, label)
    np.testing.assert_allclose(
        res["buckets"]["Tech"]["daily_standalone"].iloc[0], 0.02, atol=1e-12)

"""W3(G6) — 기간분할(study.axis=time_fold) 명시 날짜 경계 검증.

학습/검증을 등분이 아닌 *지정 시점*으로 분할(예: 2018-01-01 → 2010-17 / 2018-25 워크포워드).
study.split_dates 비면 folds 등분, 채우면 그 경계로 분할.

    cd platform && pytest tests/test_period_split_dates.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))

from quant_core.ir_engine import strategy_from_spec       # noqa: E402


def _data(ref):
    return {"op": "data", "params": {"ref": ref}}


def _ds():
    idx = pd.date_range("2021-01-01", "2023-06-30", freq="B")
    close = 100 * (1.0005) ** np.arange(len(idx), dtype=float)
    return {"X": pd.DataFrame({"Open": close, "High": close, "Low": close,
                               "Close": close, "Volume": 1e6}, index=idx)}


def _spec(study_extra):
    # 기간분할은 study.axis=time_fold — split_dates(명시 경계)·folds(등분 수)를 그 안에 둔다.
    return {"signal": _data("Close"),
            "universe": {"kind": "single", "symbols": ["X"]},
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "scheduled", "rebalance": "daily", "top_n": 1}},
            "simulation": {"initial_capital": 1e7},
            "study": {"axis": "time_fold", "reduction": "consistency", **study_extra}}


def test_explicit_date_split():
    res = strategy_from_spec(_spec({"split_dates": ["2022-01-01"]}), _ds())
    assert res["success"], res
    assert res["axis"] == "period_split"
    spans = sorted(res["buckets"].keys())
    assert len(spans) == 2
    assert spans[0].startswith("2021")        # 경계 이전 세그먼트
    assert spans[1].startswith("2022")        # 경계 이후 세그먼트


def test_two_cuts_make_three_segments():
    res = strategy_from_spec(_spec({"split_dates": ["2022-01-01", "2023-01-01"]}), _ds())
    assert res["success"], res
    assert len(res["buckets"]) == 3


def test_default_oos_unchanged():
    res = strategy_from_spec(_spec({"folds": 2}), _ds())
    assert res["success"], res
    assert set(res["buckets"].keys()) == {"인샘플", "아웃샘플"}


def test_split_with_sweep_rejected():
    """기간분할 × 펼침 동시 금지 — split_dates + label(조건 분할)은 거부."""
    spec = _spec({"split_dates": ["2022-01-01"],
                  "label": {"op": "bucket", "params": {"edges": [100.0]},
                            "inputs": {"signal": _data("Close")}}})
    res = strategy_from_spec(spec, _ds())
    assert not res["success"]
    assert any(i["rule"] == "S-split" for i in res.get("issues", []))


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

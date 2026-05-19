"""백테스트/분석 결과(pandas·numpy)를 JSON 안전 형태로 변환."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd


def _num(v: Any):
    """NaN/inf는 None으로, numpy 스칼라는 파이썬 기본형으로."""
    if v is None:
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating, float)):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    return v


def _series_points(s: pd.Series) -> list[dict]:
    return [
        {"date": idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx),
         "value": _num(val)}
        for idx, val in s.items()
    ]


def serialize_backtest(result: dict) -> dict:
    """run_strategy_backtest 결과를 JSON 직렬화 가능한 dict로."""
    if not result.get("success"):
        return {"success": False, "error": result.get("error")}

    trades_df: pd.DataFrame = result["trades"]
    trades = []
    for _, row in trades_df.iterrows():
        rec = {}
        for k, v in row.items():
            if hasattr(v, "strftime"):
                rec[k] = v.strftime("%Y-%m-%d")
            else:
                rec[k] = _num(v)
        trades.append(rec)

    return {
        "success": True,
        "metrics": {k: _num(v) for k, v in result["metrics"].items()},
        "equity": _series_points(result["equity"]),
        "benchmark": _series_points(result["benchmark"]),
        "trades": trades,
    }


def serialize_analysis(result: dict) -> dict:
    """run_analysis 결과를 JSON 직렬화 가능한 dict로."""
    if not result.get("success"):
        return {"success": False, "error": result.get("error")}

    dist: pd.Series = result.get("distribution", pd.Series(dtype=float))
    dates = result.get("condition_dates", pd.DatetimeIndex([]))
    return {
        "success": True,
        "n_samples": _num(result["n_samples"]),
        "prob_positive": _num(result["prob_positive"]),
        "mean": _num(result["mean"]),
        "median": _num(result["median"]),
        "q25": _num(result["q25"]),
        "q75": _num(result["q75"]),
        "std": _num(result["std"]),
        "t_stat": _num(result["t_stat"]),
        "p_value": _num(result["p_value"]),
        "distribution": [_num(v) for v in dist.tolist()],
        "condition_dates": [d.strftime("%Y-%m-%d") for d in dates],
    }

"""end-to-end — sweep condition축이 종목별 라벨(섹터)로 실제 다중 버킷을 내는가.

유저가 라이브에서 겪은 결함(CompileLog id=6: 섹터 비교가 'Other' 단일 버킷)을 잠근다.
옛 condition축은 라벨 패널을 columns[0] 단일 컬럼으로 붕괴 → 1버킷. 새 배선은 기여 패널을
셀 단위로 그룹화 → 진짜 섹터별 다중 버킷.

    cd platform/core && python -m pytest tests/test_comparison_e2e.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.blocks import Node, data
from quant_core.expression_parser import get_symbol_group
from quant_core.ir_engine import (Entry, PositionSpec, Sizing, SimSpec, StrategyIR,
                                   SweepSpec, Universe, run_sweep)


def _ds(codes):
    idx = pd.date_range("2020-01-01", periods=120, freq="B")

    def mk(drift):
        close = 100 * (1 + drift) ** np.arange(120)
        return pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                             "Close": close, "Volume": 1e6, "momentum_12_1m": 1.0}, index=idx)
    return {c: mk(0.0005 * (j + 1)) for j, c in enumerate(codes)}


def _sector_ir(codes):
    return StrategyIR(
        signal=data("momentum_12_1m"), universe=Universe(kind="list", symbols=codes),
        position=PositionSpec(direction="long", sizing=Sizing(mode="equal_weight"),
                              entry=Entry(mode="scheduled", rebalance="monthly", top_n=len(codes))),
        simulation=SimSpec(initial_capital=1e7),
        sweep=SweepSpec(axis="condition", target="return",
                        label=Node(op="attribute", params={"attr": "Industry"})))


def test_condition_sector_partition_makes_multiple_buckets():
    codes = ["005930", "000660", "105560"]  # 전기전자·반도체·금융(추정)
    sectors = {get_symbol_group(c, "Industry") for c in codes}
    if len(sectors) < 2:
        pytest.skip(f"분류 사이드카 부재 또는 단일 섹터: {sectors}")
    res = run_sweep(_sector_ir(codes), _ds(codes))
    assert res["success"], res.get("error")
    assert res["axis"] == "condition"
    assert len(res["buckets"]) >= 2, f"섹터 분할 실패(여전히 1버킷?): {list(res['buckets'])}"
    assert "overall" in res
    # 버킷 키가 실제 섹터명이어야(숫자/Other 단일 아님)
    assert set(res["buckets"].keys()).issubset(sectors), res["buckets"].keys()

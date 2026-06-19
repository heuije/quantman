"""summarize_result — 13 결과형상 MECE 검증 (②관측 근본수정).

핵심: 모델이 되먹임받는 요약이 형상별 '분할 결과'(연도별·파라미터별·팩터별 등)를 담아,
못 본 답을 찾아 재실행하던 헛돌이를 차단하는지 고정한다 — compact_summary가 simulate에
4스칼라만 주고 연도별 buckets를 폐기하던 것의 근본 대체.

13형상 픽스처는 test_ir_excel_export_shapes.CASES 재사용(엔진 실결과로 검증).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CORE = Path(__file__).resolve().parent.parent
_TESTS = Path(__file__).resolve().parent
for _p in (str(_CORE), str(_TESTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from quant_core.ir_engine import StrategyIR, result_shape, run_query, summarize_result
from test_ir_excel_export_shapes import CASES   # 동일 13형상 픽스처 재사용

_BY_LABEL = {c[0]: c for c in CASES}


def _run(label):
    _l, ds_fn, ir_dict, _checks = _BY_LABEL[label]
    ds = ds_fn() if callable(ds_fn) else ds_fn
    res = run_query(StrategyIR.model_validate(ir_dict), ds)
    assert res.get("success"), f"[{label}] 엔진 실행 실패: {res.get('error')}"
    return res


EXPECT_SHAPE = {
    "simulate": "simulate",
    "sweep_parameter": "sweep", "sweep_entity": "sweep", "sweep_label": "sweep",
    "period_split": "sweep", "extremize": "extremize", "select": "select",
    "describe_single": "describe_single", "describe_portfolio": "describe_portfolio",
    "describe_signal": "signal_dist", "relate_ic": "relate_ic",
    "relate_regression": "relate_regression", "relate_event": "event_study",
}

# 요약에 반드시 등장하는 핵심 needle (형상별 '분할 결과'가 모델에 보이는지)
NEEDLES = {
    "simulate": ["백테스트", "CAGR"],
    "sweep_parameter": ["분할분석", "commission=0.0", "누적"],
    "sweep_entity": ["분할분석", "AAA"],
    "sweep_label": ["조건별"],
    "period_split": ["기간별", "2020", "누적"],
    "extremize": ["최적화", "AAA"],
    "select": ["스크리닝", "DDD"],
    "describe_single": ["종목분석", "PBR"],
    "describe_portfolio": ["포트진단", "HHI"],
    "describe_signal": ["신호분포"],
    "relate_ic": ["IC", "5일"],
    "relate_regression": ["회귀", "fac1"],
    "relate_event": ["이벤트", "5일"],
}


@pytest.mark.parametrize("label", list(EXPECT_SHAPE), ids=list(EXPECT_SHAPE))
def test_result_shape(label):
    res = _run(label)
    assert result_shape(res) == EXPECT_SHAPE[label], (
        f"[{label}] shape={result_shape(res)} != {EXPECT_SHAPE[label]}")


@pytest.mark.parametrize("label", list(NEEDLES), ids=list(NEEDLES))
def test_summary_contains_disaggregated(label):
    s = summarize_result(_run(label))
    for n in NEEDLES[label]:
        assert n in s, f"[{label}] 요약에 '{n}' 없음 — 요약:\n{s}"


def test_period_split_lists_every_year():
    """기간별(연도) 요약은 모든 연도 키 + 각 연도 수익을 담는다(②의 핵심 — 모델이 한 번에 봄)."""
    res = _run("period_split")
    s = summarize_result(res)
    buckets = res.get("buckets") or {}
    assert len(buckets) >= 2
    for key in buckets:
        assert str(key) in s, f"연도 '{key}' 요약 누락 — {s}"
    assert s.count("누적") >= len(buckets)


def test_failure_summary():
    s = summarize_result({"success": False, "error": "원달러환율 시계열 없음"})
    assert s.startswith("[실패]") and "원달러환율" in s


def test_token_cap():
    """큰 버킷셋은 max_rows로 캡(토큰 가드)."""
    res = {"success": True, "axis": "parameter",
           "buckets": {f"p={i}": {"cum_return": i, "cagr": i, "sharpe": 0.1, "mdd": -1, "n": 10}
                       for i in range(100)}}
    s = summarize_result(res, max_rows=10)
    assert "…외 90개" in s


def test_simulate_not_misclassified_as_sweep():
    """단일 백테스트(buckets 없음)는 simulate — equity 분기 폴백."""
    res = {"success": True, "equity": [100, 101, 102], "metrics": {"cagr": 5.0, "sharpe": 1.0}}
    assert result_shape(res) == "simulate"
    assert "백테스트" in summarize_result(res)

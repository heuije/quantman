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
from analysis_corpus import BASE_CASES   # 13형상 픽스처(단일 진실원천)

_BY_LABEL = {c["name"]: c for c in BASE_CASES}


def _run(label):
    case = _BY_LABEL[label]
    ds_fn, ir_dict = case["ds"], case["ir"]
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


def test_inactive_buckets_flagged():
    """무체결 구간(변동·수익 0) 감지 — '0%=무수익'이 아니라 '무체결'(데이터·신호 결손) 표면화(#2)."""
    from quant_core.ir_engine.run import _inactive_buckets
    buckets = {
        "2020": {"n": 248, "std": 1.2, "cum_return": 5.0},      # 활성
        "2021": {"n": 248, "std": 0.0, "cum_return": 0.0},      # 무체결
        "2023": {"n": 245, "std": 0, "cum_return": 0},          # 무체결
    }
    assert _inactive_buckets(buckets) == ["2021", "2023"]


def test_summarize_surfaces_warnings():
    """모델 요약이 무거래/데이터결손 경고를 표면화 — 봇이 침묵의 0%를 오독하지 않도록(#2)."""
    res = {"success": True, "axis": "period_split",
           "buckets": {"2021": {"n": 248, "std": 0.0, "cum_return": 0.0, "cagr": 0.0, "mdd": 0.0}},
           "warnings": [{"code": "inactive_buckets", "message": "무거래 구간: 2021 — 데이터 결손 가능"}]}
    s = summarize_result(res)
    assert "⚠" in s and "무거래 구간: 2021" in s


def test_strategy_from_spec_preserves_run_query_warnings(monkeypatch):
    """service가 run_query(무거래 등) 경고를 검증경고로 **덮어쓰지 않고 병합**(#2 전달 버그 회귀가드 —
    conv#10에서 2026 무거래 경고가 strategy_from_spec의 res['warnings']= 덮어쓰기로 유실됐던 것)."""
    import numpy as np
    import pandas as pd
    from quant_core.ir_engine import service

    fake = {"success": True, "axis": "period_split", "buckets": {}, "metrics": {},
            "warnings": [{"code": "inactive_buckets", "message": "무거래 구간: 2026"}]}
    monkeypatch.setattr(service, "run_query", lambda s, ds: dict(fake))
    idx = pd.date_range("2020-01-02", periods=60, freq="B")
    ds = {"AAA": pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0,
                              "Close": np.linspace(100, 110, 60), "Volume": 1e6}, index=idx)}
    ir = {"universe": {"kind": "single", "symbols": ["AAA"]},
          "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
          "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                       "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 1}},
          "query": "simulate", "study": {"axis": "time_fold", "split_period": "year"}}
    res = service.strategy_from_spec(ir, ds)
    msgs = [w.get("message", "") for w in (res.get("warnings") or [])]
    assert any("무거래" in m for m in msgs), f"run_query 경고 유실: {res.get('warnings')}"

"""P4 골든 — extremize(최적해) + 과최적화 OOS 가드.

기존 결정적 픽스처(test_engine_sweep_axes._multi: AAA drift>CCC>BBB)로 손계산 검증.

    cd platform && pytest core/tests/test_question_extremize_p4.py -v
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.blocks import data
from quant_core.ir_engine import (
    Entry, ParamAxis, PositionSpec, Sizing, SimSpec, StrategyIR, Study, Universe,
    run_query, validate_strategy,
)
from quant_core.ir_engine.spec import Objective


def _multi():
    idx = pd.date_range("2020-01-01", periods=252, freq="B")

    def mk(drift, mom):
        close = 100 * (1 + drift) ** np.arange(252)
        return pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                             "Close": close, "Volume": 1e6, "momentum_12_1m": float(mom)}, index=idx)
    return {"AAA": mk(0.003, 10.0), "BBB": mk(-0.001, -5.0), "CCC": mk(0.001, 2.0)}


def _factor():
    return StrategyIR(signal=data("momentum_12_1m"), universe=Universe(kind="all"),
                      position=PositionSpec(direction="long", sizing=Sizing(mode="equal_weight"),
                                            entry=Entry(mode="scheduled", rebalance="monthly", top_n=1)),
                      simulation=SimSpec(initial_capital=1e7))


def _errs(s):
    return [i.rule for i in validate_strategy(s) if i.is_error]


def test_objective_schema_defaults():
    o = Objective()
    assert o.metric == "sharpe" and o.direction == "max" and o.oos_guard is True


def test_reduction_extremize_parses():
    s = _factor()
    s.study = Study(axis="entity", reduction="extremize", assets=["AAA", "BBB"],
                    objective=Objective(metric="cum_return", direction="max", oos_guard=False))
    assert s.study.reduction == "extremize" and s.study.objective.metric == "cum_return"


def test_validate_extremize_requires_search_axis():
    s = _factor()
    s.study = Study(axis="none", reduction="extremize")
    assert "S-OPT" in _errs(s)        # none 축은 최적화 검색공간 없음


def test_validate_extremize_rejects_non_simulate():
    s = _factor()
    s.query = "describe"
    s.study = Study(axis="parameter", reduction="extremize",
                    param_grid=[ParamAxis(path="simulation.commission", values=[0.0, 0.01])])
    s.universe = Universe(kind="all")
    assert "S-OPT" in _errs(s)


def _ext_entity(metric, direction, oos=False):
    s = _factor()
    s.study = Study(axis="entity", reduction="extremize", assets=["AAA", "BBB", "CCC"],
                    objective=Objective(metric=metric, direction=direction, oos_guard=oos))
    return s


def test_extremize_entity_max_cum_return_picks_AAA():
    res = run_query(_ext_entity("cum_return", "max"), _multi())
    assert res["success"] and res["reduction"] == "extremize" and res["axis"] == "asset"
    assert res["best"]["label"] == "AAA"          # drift 최고
    assert [r["label"] for r in res["ranked"]] == ["AAA", "CCC", "BBB"]   # cum_return 내림차순


def test_extremize_entity_min_picks_BBB():
    res = run_query(_ext_entity("cum_return", "min"), _multi())
    assert res["best"]["label"] == "BBB"          # drift 음수


def test_extremize_parameter_lower_commission_wins():
    s = _factor()
    s.study = Study(axis="parameter", reduction="extremize",
                    param_grid=[ParamAxis(path="simulation.commission", values=[0.0, 0.02])],
                    objective=Objective(metric="cum_return", direction="max", oos_guard=False))
    res = run_query(s, _multi())
    assert res["success"] and res["best"]["label"] == "commission=0.0"   # 저비용=고수익


def test_extremize_oos_guard_structure():
    res = run_query(_ext_entity("cum_return", "max", oos=True), _multi())
    assert "oos_guard" in res and ("consistency" in res["oos_guard"] or "error" in res["oos_guard"])


def test_extremize_oos_guard_absent_when_off():
    res = run_query(_ext_entity("cum_return", "max", oos=False), _multi())
    assert "oos_guard" not in res


# ── 챗 '자동매매 연동' 버튼용: best.ir = 승자의 tradable 단일 실행 전략 ──────────────
def test_extremize_best_exposes_tradable_winner_ir():
    """best.ir이 승자의 단일 실행 전략 IR(펼침 제거·승자 종목 단일화)이라 그대로 draft 저장·
    자동매매로 실행 가능하다 — 챗 결과가 그리드 스펙이 아닌 tradable 전략을 넘기게 하는 계약."""
    res = run_query(_ext_entity("cum_return", "max"), _multi())   # AAA 승(drift 최고)
    best_ir = res["best"]["ir"]
    assert isinstance(best_ir, dict)
    ir = StrategyIR.model_validate(best_ir)                       # 유효한 StrategyIR로 재검증
    assert not _errs(ir)                                          # 검증 오류 0(저장 게이트 통과 형태)
    assert ir.universe.kind == "single" and ir.universe.symbols == ["AAA"]   # 승자 종목 단일화
    assert ir.study is None or ir.study.axis == "none"           # 펼침(extremize) 제거


def test_extremize_best_ir_bakes_winning_parameter():
    """파라미터 최적화 승자 IR엔 최적 파라미터 값이 적용돼 있다(그리드가 아니라 확정 전략)."""
    s = _factor()
    s.study = Study(axis="parameter", reduction="extremize",
                    param_grid=[ParamAxis(path="simulation.commission", values=[0.0, 0.02])],
                    objective=Objective(metric="cum_return", direction="max", oos_guard=False))
    res = run_query(s, _multi())
    best_ir = StrategyIR.model_validate(res["best"]["ir"])
    assert best_ir.simulation.commission == 0.0                  # 저비용 승자가 IR에 baked

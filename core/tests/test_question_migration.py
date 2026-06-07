"""클린 컷오버 경계 — 신 query/study 형태 수용 + 레거시 sweep/period_split 거부.

레거시 dict(sweep/period_split)→신(query/study) 번역 셰임은 제거됐다. StrategyIR은
extra="ignore"(pydantic 기본)라 제거된 키를 누가 넘기면 조용히 무시→study 기본값이 되어
silent 오작동(NL repair loop도 못 잡음)할 위험이 있다. 따라서 _reject_legacy 가드가
레거시 키를 검증 에러로 loud하게 거부한다 — 이 파일은 그 거부와 신형태 통과를 함께 잠근다.

    cd platform && pytest core/tests/test_question_migration.py -v
"""
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

from quant_core.ir_engine.spec import StrategyIR  # noqa: E402

_BASE = {"universe": {"kind": "single", "symbols": ["AAA"]},
         "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
         "position": {"entry": {"mode": "always"}}}


# ── 신 형태 통과 ───────────────────────────────────────────────────────────────

def test_new_form_default():
    """query/study 미지정 → 기본 simulate·axis=none."""
    s = StrategyIR.model_validate(_BASE)
    assert s.query == "simulate" and s.study.axis == "none"


def test_new_form_parameter_passthrough():
    s = StrategyIR.model_validate({**_BASE, "query": "simulate", "study": {
        "axis": "parameter", "reduction": "enumerate",
        "param_grid": [{"path": "simulation.leverage", "values": [1, 2]}]}})
    assert s.query == "simulate" and s.study.axis == "parameter" and len(s.study.param_grid) == 1


def test_new_form_time_fold_passthrough():
    """기간분할 신형태 — study.axis=time_fold + folds/split_dates(study 내부)는 정상 통과."""
    s = StrategyIR.model_validate({**_BASE, "study": {
        "axis": "time_fold", "reduction": "consistency", "folds": 2,
        "split_dates": ["2022-01-01"]}})
    assert s.study.axis == "time_fold" and s.study.folds == 2
    assert s.study.split_dates == ["2022-01-01"]   # study 내부 split_dates는 신형태(거부 안 됨)


# ── 레거시 거부 (silent-drop 방지) ─────────────────────────────────────────────

@pytest.mark.parametrize("legacy", [
    {"sweep": {"axis": "none", "target": "return"}},
    {"sweep": {"axis": "parameter", "param_grid": [{"path": "simulation.commission", "values": [0, 0.1]}]}},
    {"sweep": {"axis": "asset", "assets": ["AAA", "BBB"]}},
    {"sweep": {"target": "signal", "target_node": {"op": "data", "params": {"ref": "__SELF__.rsi_14"}}}},
])
def test_legacy_sweep_rejected(legacy):
    """레거시 top-level 'sweep' 키는 조용히 무시되지 않고 검증 에러로 거부된다."""
    with pytest.raises(ValidationError, match="레거시"):
        StrategyIR.model_validate({**_BASE, **legacy})


def test_legacy_period_split_rejected():
    """레거시 simulation.period_split는 거부(신형태는 study.axis=time_fold)."""
    with pytest.raises(ValidationError, match="레거시"):
        StrategyIR.model_validate({**_BASE, "simulation": {"period_split": "oos"}})


def test_legacy_simulation_split_dates_rejected():
    """레거시 simulation.split_dates는 거부 — split_dates는 이제 study 안에만 존재."""
    with pytest.raises(ValidationError, match="레거시"):
        StrategyIR.model_validate({**_BASE, "simulation": {"split_dates": ["2022-01-01"]}})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

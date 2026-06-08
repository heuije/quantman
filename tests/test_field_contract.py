"""field_contract — 검증 에러 위치(loc)의 스키마 계약을 자동 파생(케이스별 하드코딩 아님).

NL→IR 컴파일러 repair 루프가 "어느 필드를 어떤 모양으로" 고칠지 LLM에 알려주는 핵심.
StrategyIR 모델을 걸어가며 그 자리의 필수·옵션 필드와 타입을 추출하므로, 이번 한 건이 아니라
모든 위치에 대해 같은 메커니즘으로 동작한다(구조적).

    cd platform && pytest tests/test_field_contract.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.ir_engine import field_contract  # noqa: E402


def test_param_grid_item_contract():
    """실측 실패 지점 — study.param_grid 각 항목은 {path(필수), values}."""
    c = field_contract(("study", "param_grid", 0))
    assert c is not None
    assert "ParamAxis" in c
    assert "path: str(필수)" in c
    assert "values" in c


def test_root_lists_required_signal():
    c = field_contract(())
    assert "StrategyIR" in c
    assert "signal: Node(필수)" in c          # signal은 기본값 없는 필수 필드


def test_nested_exit_contract():
    c = field_contract(("position", "exit"))
    assert "Exit" in c
    assert "hold_days" in c and "stop_loss" in c


def test_enum_values_shown():
    """Literal(enum) 필드는 허용값을 노출해야 LLM이 유효값만 쓴다."""
    c = field_contract(("study",))
    assert "parameter" in c and "entity" in c   # axis Literal 값


def test_bogus_loc_returns_none():
    assert field_contract(("nope", "x")) is None
    assert field_contract(("signal", "doesnotexist")) is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

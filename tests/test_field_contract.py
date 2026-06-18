"""field_contract — 검증 에러 위치(loc)의 스키마 계약을 자동 파생(케이스별 하드코딩 아님).

NL→IR 컴파일러 repair 루프가 "어느 필드를 어떤 모양으로" 고칠지 LLM에 알려주는 핵심.
StrategyIR 모델을 걸어가며 그 자리의 필수·옵션 필드와 타입을 추출하므로, 이번 한 건이 아니라
모든 위치에 대해 같은 메커니즘으로 동작한다(구조적).

    cd platform && pytest tests/test_field_contract.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.ir_engine import field_contract, unknown_field_issues  # noqa: E402

# 유효한 최소 신호(Node 트리) — unknown_field 테스트가 signal 트리를 오탐하지 않는지 확인용.
_SIG = {"op": "compare", "params": {"op": ">"}, "inputs": {
    "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
    "right": {"op": "const", "params": {"value": 0}}}}


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


# ── unknown_field_issues — extra="ignore" 모델에서 조용히 버려지는 환각 필드 표면화 ──────
# (라이브 결함: commission_pct/transaction_cost_pct 가 SimSpec에 없어 silent drop → 비용 무시)

def test_unknown_simulation_field_flagged():
    """SimSpec엔 commission_pct가 없다 — extra='ignore'로 버려지기 전에 잡아 repair에 피드백."""
    issues = unknown_field_issues({"signal": _SIG, "simulation": {"commission_pct": 0.01}})
    paths = {i["path"] for i in issues}
    assert "simulation.commission_pct" in paths
    iss = next(i for i in issues if i["path"] == "simulation.commission_pct")
    assert iss["is_error"] is True
    assert "commission" in iss["message"]      # 올바른 필드(commission)를 계약으로 안내


def test_known_fields_produce_no_issue():
    """전부 유효 필드면 무이슈 — 정상 IR을 거짓 거부하면 안 됨."""
    ir = {"signal": _SIG,
          "simulation": {"initial_capital": 100000000, "commission": 0.0001, "fill": "next_open"},
          "position": {"direction": "long", "exit": {"hold_days": 0}},
          "study": {"axis": "time_fold", "folds": 10}}
    assert unknown_field_issues(ir) == []


def test_unknown_top_level_field_flagged():
    issues = unknown_field_issues({"signal": _SIG, "bogus_key": 1})
    assert any(i["path"] == "bogus_key" for i in issues)


def test_signal_node_params_not_flagged():
    """signal Node의 params(window·value·ref·op)는 자유 dict — 미지필드로 오탐하면 안 됨."""
    assert unknown_field_issues({"signal": _SIG}) == []


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

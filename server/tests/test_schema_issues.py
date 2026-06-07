"""_schema_issues — Pydantic 스키마 오류를 위치(loc)+그 자리 계약과 함께 'fixable'하게.

실측 회귀(프로덕션 CompileLog id=2): LLM이 study.param_grid 항목을 이름으로 감싸 path를
누락 → 검증이 "Field required·path=root"로만 와서 repair 2회 다 실패. 이제 위치(study.
param_grid.0.path)와 ParamAxis 계약을 담아 LLM이 모양을 고칠 수 있어야 한다. 모든 에러를
반환(첫 1개만이 아니라)하는지도 고정.

    cd platform/server && python -m pytest tests/test_schema_issues.py -v
"""

import sys
from pathlib import Path

from pydantic import ValidationError

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.routers.ir_compile import _schema_issues  # noqa: E402
from quant_core.ir_engine import StrategyIR  # noqa: E402

# 실측: LLM이 param_grid 항목을 {long_threshold:{path,value}}로 감싸 최상위 path 누락.
_BAD_STUDY_IR = {
    "name": "WTI", "universe": {"kind": "single", "symbols": ["원유선물"]},
    "signal": {"op": "compare", "params": {"op": ">"}, "inputs": {
        "left": {"op": "data", "params": {"ref": "__SELF__.Low"}},
        "right": {"op": "const", "params": {"value": 30}}}},
    "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                 "entry": {"mode": "on_signal"}, "exit": {}},
    "simulation": {"initial_capital": 10000000},
    "study": {"axis": "parameter", "param_grid": [
        {"long_threshold": {"path": "x", "value": 0}}]},   # 최상위 path 없음 → 누락
}


def _issues_for(ir: dict) -> list[dict]:
    try:
        StrategyIR.model_validate(ir)
    except ValidationError as e:
        return _schema_issues(e)
    raise AssertionError("검증이 통과하면 안 되는 IR")


def test_locates_missing_param_grid_path_with_contract():
    issues = _issues_for(_BAD_STUDY_IR)
    by_path = {i["path"]: i["message"] for i in issues}
    assert "study.param_grid.0.path" in by_path, list(by_path)
    msg = by_path["study.param_grid.0.path"]
    # 메시지에 그 자리 계약(ParamAxis 모양)이 담겨야 LLM이 고친다
    assert "ParamAxis" in msg and "values" in msg, msg


def test_surfaces_all_errors_not_just_first():
    """signal(필수) 누락 + study 모양 오류를 동시에 깨뜨려 둘 다 보고되는지."""
    bad = {k: v for k, v in _BAD_STUDY_IR.items() if k != "signal"}
    issues = _issues_for(bad)
    paths = {i["path"] for i in issues}
    assert "signal" in paths, paths                         # 필수 누락도 위치로 보고
    assert any(p.startswith("study.param_grid") for p in paths), paths   # 다른 에러도 함께


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

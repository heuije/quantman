"""관용구 쿡북 레시피 회귀 — 프롬프트가 가르치는 '비자명 합성'이 실제로 유효 IR을 만드는지.

스키마가 진화해도 쿡북 레시피가 깨지지 않게 잠근다(LLM 없이 검증). 특히 #1 부호점수
양방향은 실측 실패(WTI 롱숏→롱온리 축소)를 막는 핵심 레시피.

    cd platform && pytest tests/test_idiom_recipes.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.ir_engine import StrategyIR, validate_strategy  # noqa: E402

_REFS = {"005930", "Open", "High", "Low", "Close", "Volume"}


def _errs(ir: dict):
    s = StrategyIR.model_validate(ir)
    return [i for i in validate_strategy(s, valid_refs=_REFS) if i.is_error]


def _sel(op, thr, a_val, b_node):
    return {"op": "select",
            "inputs": {"cond": {"op": "compare", "params": {"op": op}, "inputs": {
                           "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                           "right": {"op": "const", "params": {"value": thr}}}},
                       "a": {"op": "const", "params": {"value": a_val}},
                       "b": b_node}}


def test_idiom1_signed_score_two_sided_validates():
    """[조건 기반 롱/숏/중립] select로 +1/-1/0 부호점수 + long_short threshold=0 → 유효."""
    signed = _sel("<", 60000, 1, _sel(">", 80000, -1, {"op": "const", "params": {"value": 0}}))
    ir = {
        "name": "양방향 밴드", "universe": {"kind": "single", "symbols": ["005930"]},
        "signal": signed,
        "position": {"direction": "long_short", "sizing": {"mode": "equal_weight"},
                     "entry": {"mode": "scheduled", "rebalance": "daily", "threshold": 0},
                     "exit": {}},
        "simulation": {"initial_capital": 10000000, "fill": "next_open"},
        "study": {"axis": "parameter", "param_grid": [
            {"path": "signal.inputs.cond.inputs.right.params.value", "values": [50000, 60000]},
            {"path": "signal.inputs.b.inputs.cond.inputs.right.params.value", "values": [80000, 90000]}]},
    }
    assert _errs(ir) == [], [str(e.message) for e in _errs(ir)]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

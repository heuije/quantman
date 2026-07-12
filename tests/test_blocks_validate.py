"""P0-5 — 메타규칙 엔진 회귀 (규칙0·1·2·3·5 + 우선순위).

명세 §5. 잘못된 조립을 거부하고 빈칸을 기본값으로 채우는지 고정한다.

    cd platform && pytest tests/test_blocks_validate.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.blocks import (  # noqa: E402
    Node, apply_defaults, const, data, infer_shape, is_valid, validate,
)
from quant_core.blocks.types import Shape  # noqa: E402
from quant_core.blocks.validate import SEV_ERROR, SEV_INTEGRITY, Issue, prioritize  # noqa: E402


# ── 정상 트리 ─────────────────────────────────────────────────────────────────

def test_valid_tree_no_errors():
    node = Node(op="compare", params={"op": "<"},
                inputs={"left": data("__SELF__.rsi_14"), "right": const(30)})
    assert is_valid(node)
    assert validate(node) == []


def test_valid_nested():
    node = Node(op="rank", inputs={"signal":
        Node(op="ts_mean", params={"window": 10}, inputs={"signal": data("Close")})})
    assert is_valid(node)


# ── 규칙1 타입 호환 ───────────────────────────────────────────────────────────

def test_r1_score_in_condition_slot():
    """logic(조건 결합)에 score(data 잎)를 넣으면 타입 불일치."""
    node = Node(op="logic", params={"logic": "AND"}, inputs={"0": data("Close")})
    issues = validate(node)
    assert any(i.rule == "R1" for i in issues)
    assert not is_valid(node)


def test_r1_condition_in_score_slot():
    """binary(산술)에 condition(compare)을 넣으면 타입 불일치."""
    cond = Node(op="compare", params={"op": ">"},
                inputs={"left": data("Close"), "right": const(0)})
    node = Node(op="binary", params={"op": "+"}, inputs={"a": cond, "b": const(1)})
    assert any(i.rule == "R1" for i in validate(node))


# ── 규칙2 형태 호환 ───────────────────────────────────────────────────────────

def test_r2_cross_sectional_on_series():
    """rank(VIX.Close) — VIX.Close는 SERIES(브로드캐스트) → 횡단 무의미."""
    node = Node(op="rank", inputs={"signal": data("VIX.Close")})
    issues = validate(node)
    assert any(i.rule == "R2" for i in issues)


def test_r2_panel_ok():
    """rank(Close) — Close는 PANEL → 정상."""
    node = Node(op="rank", inputs={"signal": data("Close")})
    assert not any(i.rule == "R2" for i in validate(node))


def test_infer_shape():
    assert infer_shape(data("Close")) == Shape.PANEL
    assert infer_shape(data("__SELF__.rsi_14")) == Shape.PANEL
    assert infer_shape(data("VIX.Close")) == Shape.SERIES
    # 시장 series에 시계열 op → 여전히 series
    assert infer_shape(Node(op="ts_mean", params={"window": 5},
                            inputs={"signal": data("VIX.Close")})) == Shape.SERIES


# ── 규칙3 구조 ────────────────────────────────────────────────────────────────

def test_r3_missing_slot():
    node = Node(op="compare", params={"op": "<"}, inputs={"left": data("Close")})
    assert any(i.rule == "R3" and "right" in i.message for i in validate(node))


def test_r3_undefined_slot():
    node = Node(op="compare", params={"op": "<"},
                inputs={"left": data("Close"), "right": const(1), "extra": const(2)})
    assert any(i.rule == "R3" and "미정의" in i.message for i in validate(node))


def test_r3_missing_required_param():
    node = Node(op="compare", inputs={"left": data("Close"), "right": const(1)})
    assert any(i.rule == "R3" and "op" in i.message for i in validate(node))


def test_unknown_op():
    node = Node(op="does_not_exist")
    assert any(i.rule == "catalog" for i in validate(node))


# ── 규칙0 데이터 가용성 ───────────────────────────────────────────────────────

def test_r0_missing_data():
    valid = {"Close", "rsi_14", "AAA", "VIX"}
    bad = Node(op="rank", inputs={"signal": data("nonexistent_indic")})
    assert any(i.rule == "R0" for i in validate(bad, valid_refs=valid))
    ok = Node(op="rank", inputs={"signal": data("Close")})
    assert not any(i.rule == "R0" for i in validate(ok, valid_refs=valid))


def test_r0_empty_symbol_key_not_available():
    """키만 있고 df가 None/빈 심볼은 유효 참조가 아니다 — "SYM.x" 참조가 NaN→False로
    조용히 0건이 되던 부류를 R0 거부로 마감(__SELF__.x의 컬럼 실재 요구와 대칭)."""
    import pandas as pd

    from quant_core.blocks import available_refs

    ds = {"AAA": pd.DataFrame({"Close": [1.0, 2.0]},
                              index=pd.bdate_range("2024-01-01", periods=2)),
          "VIX": pd.DataFrame(), "GOLD": None}
    refs = available_refs(ds)
    assert "AAA" in refs and "Close" in refs
    assert "VIX" not in refs and "GOLD" not in refs
    bad = Node(op="compare", params={"op": ">"},
               inputs={"left": data("VIX.Close"), "right": const(0)})
    assert any(i.rule == "R0" for i in validate(bad, valid_refs=refs))


# ── 규칙5 기본값 ──────────────────────────────────────────────────────────────

def test_r5_apply_defaults_window():
    """ts_mean의 window 미지정 → 기본 20 채움 (retail 친화)."""
    node = Node(op="ts_mean", inputs={"signal": data("Close")})
    filled = apply_defaults(node)
    assert filled.params["window"] == 20
    # 명시값은 보존
    node2 = Node(op="ts_mean", params={"window": 60}, inputs={"signal": data("Close")})
    assert apply_defaults(node2).params["window"] == 60


def test_r5_recursive():
    node = Node(op="rank", inputs={"signal": Node(op="ts_mean", inputs={"signal": data("Close")})})
    filled = apply_defaults(node)
    assert filled.inputs["signal"].params["window"] == 20


# ── 우선순위 ──────────────────────────────────────────────────────────────────

def test_priority_integrity_first():
    issues = [Issue("R1", SEV_ERROR, "타입"), Issue("integrity", SEV_INTEGRITY, "무결성")]
    ordered = prioritize(issues)
    assert ordered[0].rule == "integrity"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

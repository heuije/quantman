"""컴파일 후보정 — %수익률 임계 단위 오류 결정적 교정 (#3, 별개 A 부류).

프롬프트가 '−0.1로 쓰라'고 명시해도 LLM이 비결정적으로 −0.001(÷100 분수화)을 내므로,
compile_strategy 후보정이 |const|<0.01인 %수익률 임계를 ×100 결정적 교정한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
_CORE_DIR = _SERVER_DIR.parent / "core"
for _p in (str(_CORE_DIR), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.compile_service import _normalize_pct_thresholds


def test_corrects_decimal_threshold_error():
    sig = {"op": "compare", "params": {"op": "<="}, "inputs": {
        "left": {"op": "data", "params": {"ref": "S&P500.pct_change_1d"}},
        "right": {"op": "const", "params": {"value": -0.001}}}}
    warns = _normalize_pct_thresholds(sig)
    assert sig["inputs"]["right"]["params"]["value"] == -0.1        # ×100 교정
    assert warns and "교정" in warns[0]


def test_leaves_legit_threshold_and_nonreturn_indicator():
    # 정상 임계(−0.5%=−0.5)는 불변(|const|≥0.01)
    s1 = {"op": "compare", "params": {"op": "<="}, "inputs": {
        "left": {"op": "data", "params": {"ref": "pct_change_1d"}},
        "right": {"op": "const", "params": {"value": -0.5}}}}
    assert _normalize_pct_thresholds(s1) == []
    assert s1["inputs"]["right"]["params"]["value"] == -0.5
    # 수익률류가 아닌 지표(rsi_14)는 교정 대상 아님
    s2 = {"op": "compare", "params": {"op": ">"}, "inputs": {
        "left": {"op": "data", "params": {"ref": "rsi_14"}},
        "right": {"op": "const", "params": {"value": 0.005}}}}
    assert _normalize_pct_thresholds(s2) == []
    assert s2["inputs"]["right"]["params"]["value"] == 0.005


def test_nested_select_both_thresholds_corrected():
    """conv#9 실제 구조(부호점수 select) — 두 임계(-0.001/0.001) 모두 ×100 교정."""
    sig = {"op": "select", "inputs": {
        "cond": {"op": "compare", "params": {"op": "<="}, "inputs": {
            "left": {"op": "data", "params": {"ref": "S&P500.pct_change_1d"}},
            "right": {"op": "const", "params": {"value": -0.001}}}},
        "a": {"op": "const", "params": {"value": 1}},
        "b": {"op": "select", "inputs": {
            "cond": {"op": "compare", "params": {"op": ">="}, "inputs": {
                "left": {"op": "data", "params": {"ref": "S&P500.pct_change_1d"}},
                "right": {"op": "const", "params": {"value": 0.001}}}},
            "a": {"op": "const", "params": {"value": -1}},
            "b": {"op": "const", "params": {"value": 0}}}}}}
    warns = _normalize_pct_thresholds(sig)
    assert len(warns) == 2
    assert sig["inputs"]["cond"]["inputs"]["right"]["params"]["value"] == -0.1
    assert sig["inputs"]["b"]["inputs"]["cond"]["inputs"]["right"]["params"]["value"] == 0.1

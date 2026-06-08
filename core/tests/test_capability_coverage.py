"""구조 가드 — capability_spec()이 엔진(StrategyIR)의 모든 Literal 능력을 기술하는가.

NL→IR 컴파일러는 capability_spec()을 읽어 "엔진이 무엇을 할 수 있는가"를 안다. 엔진(spec.py)에
새 enum 값이 생겼는데 capability_spec에 안 적히면, 그 능력은 LLM에게 *보이지 않아* 매핑 불가가
된다(실측: sweep.axis='condition'이 누락돼 "산업별 비교" 전략이 컴파일 실패 — CompileLog id=3).

이 테스트는 그 표류(drift) 부류를 영구히 닫는 단일 진입점이다: StrategyIR 모델 트리의 모든
Literal 값을 모아, capability_spec() 출력에 'value' 항목으로 등장하는지 단언한다. 엔진에 enum이
추가되면 capability_spec이 기술할 때까지 이 테스트가 실패한다(표류가 조용히 재발 불가).

의도적으로 LLM에 숨길 내부 전용 enum 값은 _ALLOWLIST에 *사유와 함께* 등록한다(예외는 명시적으로).

    cd platform/core && python -m pytest tests/test_capability_coverage.py -q
"""
from __future__ import annotations

import sys
import typing
from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parent.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from pydantic import BaseModel

from quant_core.ir_engine import capability_spec
from quant_core.ir_engine.spec import StrategyIR

# LLM에 의도적으로 숨기는 내부 전용 enum 값 — 비우는 게 기본. 추가 시 반드시 사유 주석.
# query/study 평면(describe·relate·simulate / none·parameter·entity·label·time_fold /
# enumerate·contrast·consistency)은 이제 capability_spec()이 query·study_axis·study_reduction
# 항목으로 직접 기술한다(NL 컴파일러가 신 어휘를 산출). 숨길 내부 전용 값이 없으므로 비어 있다.
_ALLOWLIST: set[str] = set()


def _collect_from_annotation(ann, lit: set, models: set) -> None:
    """타입 주석을 재귀로 훑어 Literal 값과 중첩 BaseModel을 모은다(Optional/Union/list/dict 언랩)."""
    if typing.get_origin(ann) is typing.Literal:
        lit.update(str(a) for a in typing.get_args(ann))
        return
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        models.add(ann)
        return
    for a in typing.get_args(ann):
        _collect_from_annotation(a, lit, models)


def _engine_literals(root) -> set:
    """root 모델 트리(중첩 포함)의 모든 Literal 값."""
    lit: set = set()
    seen: set = set()
    queue = [root]
    while queue:
        m = queue.pop()
        if m in seen:
            continue
        seen.add(m)
        nested: set = set()
        for f in m.model_fields.values():
            _collect_from_annotation(f.annotation, lit, nested)
        queue.extend(nested)
    return lit


def _described_values(obj, out: set) -> None:
    """capability_spec() 출력을 재귀로 훑어 모든 dict의 'value' 항목을 모은다."""
    if isinstance(obj, dict):
        v = obj.get("value")
        if isinstance(v, str):
            out.add(v)
        for child in obj.values():
            _described_values(child, out)
    elif isinstance(obj, list):
        for it in obj:
            _described_values(it, out)


def test_capability_spec_covers_all_engine_literals():
    required = _engine_literals(StrategyIR)
    described: set = set()
    _described_values(capability_spec(), described)
    missing = required - described - _ALLOWLIST
    assert not missing, (
        "엔진(spec.py)에 있으나 capability_spec()이 'value'로 기술하지 않은 enum 값 "
        f"— LLM에게 보이지 않음:\n  {sorted(missing)}\n"
        "→ capability_spec()에 {value, does} 항목으로 추가하거나, 내부 전용이면 "
        "_ALLOWLIST에 사유와 함께 등록하세요."
    )

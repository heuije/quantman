"""NL 컴파일러 시스템 프롬프트 빌드 가드 — f-string 미이스케이프 중괄호 회귀 차단.

_system_prompt는 거대한 f-string이라 idiom/스키마 예시의 *리터럴* 중괄호를 {{}}로 이스케이프해야
한다. 단일 `{top_n:N}` 같은 실수는 import 시점엔 안 보이고 *호출 시점*에야 NameError로 터진다 —
실제로 프로덕션 /ir/compile이 이 버그로 500 사망했고, import-only 스모크 테스트는 못 잡았다.
이 테스트는 실제 catalog/capability로 프롬프트를 빌드해 그 부류(미이스케이프 중괄호)를 영구 차단한다.

    cd platform && pytest server/tests/test_ir_compiler_prompt.py -v
"""
import pytest

ic = pytest.importorskip("app.ir_compiler")
from quant_core.blocks import catalog_spec
from quant_core.ir_engine import capability_spec


def test_system_prompt_builds_without_brace_error():
    """실제 catalog/capability로 _system_prompt 빌드 — 미이스케이프 {} 있으면 NameError/KeyError."""
    prompt = ic._system_prompt(
        catalog_spec(), capability_spec(),
        ["pb_ratio", "trailing_pe", "ev_ebitda", "rsi_14", "momentum_12_1m"])
    assert isinstance(prompt, str) and len(prompt) > 1000


def test_system_prompt_exposes_question_vocabulary():
    """신규 동사·환원·관계 어휘가 프롬프트에 실제 노출돼야 컴파일러가 매핑할 수 있다."""
    prompt = ic._system_prompt(catalog_spec(), capability_spec(), ["pb_ratio"])
    for kw in ("select", "describe", "relate", "extremize", "regression", "portfolio"):
        assert kw in prompt, f"프롬프트에 '{kw}' 어휘 누락 — 컴파일러가 해당 기능을 못 본다"

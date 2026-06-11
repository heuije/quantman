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


# ── M5d 라우팅 — 조건양방향 당일매매를 on_signal directional로(scheduled 강등 회귀 차단) ──

def test_prompt_routes_conditional_bidirectional_to_on_signal():
    """레시피1이 조건양방향 이벤트/당일을 on_signal로 안내하는지 — 프롬프트에 라우팅 노출."""
    prompt = ic._system_prompt(catalog_spec(), capability_spec(), ["pb_ratio"])
    assert "이벤트/당일" in prompt and 'entry.mode="on_signal"' in prompt


def _ls(mode, hold_days, **entry_extra):
    entry = {"mode": mode, **entry_extra}
    return {"position": {"direction": "long_short", "entry": entry,
                         "exit": {"hold_days": hold_days}}}


def test_route_directional_daytrade_forces_on_signal():
    """부호방향 long_short 당일매매(hold_days=0·비랭킹)는 scheduled로 와도 on_signal로 정규화."""
    out = ic._route_directional(_ls("scheduled", 0))
    assert out["position"]["entry"]["mode"] == "on_signal"


def test_route_directional_ranking_stays_scheduled():
    """랭킹(top_n) long_short는 횡단 — hold_days=0이어도 on_signal로 바꾸지 않는다."""
    out = ic._route_directional(_ls("scheduled", 0, top_n=5))
    assert out["position"]["entry"]["mode"] == "scheduled"


def test_route_directional_sets_explicit_threshold():
    """라우팅 시 threshold 미지정(None)을 0으로 명시 — None은 _select(랭킹 추락)와
    _direction_for(0.0)가 다르게 해석돼 단일 종목 롱·숏 동시 후보 사고를 냈다(2026-06-11)."""
    out = ic._route_directional(_ls("scheduled", 0))
    assert out["position"]["entry"]["threshold"] == 0.0
    # 랭킹은 불변 — threshold를 건드리지 않는다
    out2 = ic._route_directional(_ls("scheduled", 0, top_n=5))
    assert out2["position"]["entry"].get("threshold") is None


def test_route_directional_swing_stays_scheduled():
    """비당일(hold_days>=1) long_short는 불변(기존 scheduled TSMOM/팩터 보존)."""
    out = ic._route_directional(_ls("scheduled", 5))
    assert out["position"]["entry"]["mode"] == "scheduled"


def test_route_directional_long_not_targeted():
    """long(단방향)은 long_short 정규화 대상 아님 — 불변."""
    strat = {"position": {"direction": "long", "entry": {"mode": "scheduled"},
                          "exit": {"hold_days": 0}}}
    assert ic._route_directional(strat)["position"]["entry"]["mode"] == "scheduled"

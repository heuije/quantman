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


def test_system_prompt_symbol_catalog_lists_cross_asset_symbols():
    """symbols 인자를 주면 크로스에셋 참조 가능 심볼 카탈로그가 프롬프트에 노출돼야(#B) —
    매크로 심볼(옵션풋콜비율 등)을 신호 ref로 못 내던 갭을 닫는다."""
    symbols = {"매크로·시장지표": ["옵션풋콜비율", "코스피200변동성지수", "VIX"],
               "자산(선물·지수·크립토)": ["S&P500", "코스피200선물"]}
    prompt = ic._system_prompt(catalog_spec(), capability_spec(), ["pb_ratio"], symbols)
    assert "크로스에셋 참조 가능 심볼" in prompt
    assert "옵션풋콜비율" in prompt and "코스피200변동성지수" in prompt


def test_system_prompt_symbol_catalog_omitted_when_absent():
    """symbols 미제공(None)이면 카탈로그 섹션은 생략 — 하위호환·프롬프트 무결."""
    prompt = ic._system_prompt(catalog_spec(), capability_spec(), ["pb_ratio"])
    assert "크로스에셋 참조 가능 심볼" not in prompt


# ── 단위·비용 스케일 가이드 — 라이브 결함(±0.1%→±0.001 100×축소·commission_pct 환각) 차단 ──

def test_prompt_states_percent_scale_for_pct_fields():
    """%계열 지표 임계는 퍼센트 스케일(0.1% → 0.1)임을 명시 — 분수화(−0.001) 금지를 컴파일러에 가르침."""
    prompt = ic._system_prompt(catalog_spec(), capability_spec(),
                               ["pct_change_1d", "rsi_14", "momentum_12_1m"])
    assert "pct_change_1d" in prompt
    assert "퍼센트" in prompt           # %계열은 이미 퍼센트 단위라는 규칙
    assert "-0.001" in prompt          # 분수화 금지 반례를 명시


def test_prompt_states_cost_field_names_and_fraction_scale():
    """비용은 commission/slippage(분수)임을 명시 — commission_pct 환각·스케일오류 차단."""
    prompt = ic._system_prompt(catalog_spec(), capability_spec(), ["pct_change_1d"])
    assert "commission" in prompt and "slippage" in prompt
    assert "0.0001" in prompt          # 편도 0.01% → 0.0001(분수) 예시
    assert "commission_pct" in prompt  # 환각 필드명을 명시적으로 금지


def test_fewshot_has_cross_asset_percent_intraday_example():
    """크로스에셋(SYM.지표) + %임계 + 비용 + 당일 롱숏 few-shot이 그 부류를 앵커한다."""
    prompt = ic._system_prompt(catalog_spec(), capability_spec(), ["pct_change_1d"])
    assert "S&P500.pct_change_1d" in prompt   # cross-asset 신호 참조 예시
    assert '"commission": 0.0001' in prompt   # 비용 분수 스케일 실예(commission_pct 아님)


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


# ── 섹터/업종 필터 부분일치 강제(반도체→"반도체 제조업" 정확매칭 0건 근본수정) ──

def _sector_screener(values, match=None):
    params = {"values": values}
    if match is not None:
        params["match"] = match
    return {"universe": {"kind": "all", "screener": {"condition": {
                "op": "is_in", "params": params,
                "inputs": {"signal": {"op": "attribute", "params": {"attr": "Sector"}}}}}}}


def test_force_contains_on_attribute_filter():
    """is_in(attribute(Sector))는 match 없이 와도 contains로 강제."""
    out = ic._force_attribute_filter_contains(_sector_screener(["반도체"]))
    cond = out["universe"]["screener"]["condition"]
    assert cond["params"]["match"] == "contains"


def test_force_contains_overrides_exact_on_attribute():
    """LLM이 attribute 필터에 exact를 내도 contains로 교정(exact는 자유서술 분류에 무용)."""
    out = ic._force_attribute_filter_contains(_sector_screener(["반도체"], match="exact"))
    assert out["universe"]["screener"]["condition"]["params"]["match"] == "contains"


def test_force_contains_leaves_non_attribute_is_in_exact():
    """attribute가 아닌 is_in(버킷·국면 라벨)은 정확매칭 불변 — 과교정 차단."""
    strat = {"universe": {"screener": {"condition": {
        "op": "is_in", "params": {"values": ["1분위"]},
        "inputs": {"signal": {"op": "bucket", "params": {"edges": [0.5]}}}}}}}
    out = ic._force_attribute_filter_contains(strat)
    assert "match" not in out["universe"]["screener"]["condition"]["params"]

"""S1 — StrategyIR 스키마 + 정합성 검증 회귀.

명세 §7·§3.3. 전략 구조가 룰·팩터 전략을 빠짐없이 표현하고, 신호타입×진입/방향
호환 등 구조 규칙을 강제하는지 고정한다.

    cd platform && pytest tests/test_engine_spec.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.blocks import Node, const, data  # noqa: E402
from quant_core.ir_engine import (  # noqa: E402
    Entry, Exit, Overlays, PositionSpec, Sizing, StrategyIR, Study, Universe,
    validate_strategy,
)


def _cond():
    return Node(op="compare", params={"op": ">"},
                inputs={"left": data("__SELF__.ma_dev_20d"), "right": const(0.0)})


def _score():
    return Node(op="rank", inputs={"signal": data("momentum_12_1m")})


def _label():
    return Node(op="bucket", params={"edges": [0.0]}, inputs={"signal": _score()})


def _errs(s):
    return [i for i in validate_strategy(s) if i.is_error]


# ── 정상 구조 ─────────────────────────────────────────────────────────────────

def test_rule_strategy_valid():
    """룰: 단일종목 + 조건신호 + on_signal + 손익절."""
    s = StrategyIR(
        signal=_cond(),
        universe=Universe(kind="single", symbols=["005930"]),
        position=PositionSpec(
            entry=Entry(mode="on_signal"),
        ),
    )
    assert _errs(s) == []


def test_factor_strategy_valid():
    """팩터: 전체 유니버스 + 점수신호 + 정기리밸런싱 + 상위N + 롱숏."""
    s = StrategyIR(
        signal=_score(),
        universe=Universe(kind="all"),
        position=PositionSpec(
            direction="long_short",
            sizing=Sizing(mode="signal_proportional"),
            entry=Entry(mode="scheduled", rebalance="monthly", top_n=20),
        ),
    )
    assert _errs(s) == []


# ── 구조 규칙 위반 ────────────────────────────────────────────────────────────

def test_on_signal_requires_condition():
    s = StrategyIR(signal=_score(), universe=Universe(kind="single", symbols=["005930"]),
                   position=PositionSpec(entry=Entry(mode="on_signal")))
    assert any(i.rule == "S-entry" for i in validate_strategy(s))


def test_long_short_requires_score():
    s = StrategyIR(signal=_cond(), universe=Universe(kind="all"),
                   position=PositionSpec(direction="long_short", entry=Entry(mode="scheduled")))
    assert any(i.rule == "S-dir" for i in validate_strategy(s))


def test_signal_proportional_requires_score():
    s = StrategyIR(signal=_cond(), universe=Universe(kind="all"),
                   position=PositionSpec(sizing=Sizing(mode="signal_proportional"),
                                         entry=Entry(mode="scheduled")))
    assert any(i.rule == "S-size" for i in validate_strategy(s))


def test_single_universe_needs_one_symbol():
    s = StrategyIR(signal=_cond(), universe=Universe(kind="single", symbols=[]),
                   position=PositionSpec(entry=Entry(mode="on_signal")))
    assert any(i.rule == "S-univ" for i in validate_strategy(s))


def test_on_signal_all_universe_rejected():
    s = StrategyIR(signal=_cond(), universe=Universe(kind="all"),
                   position=PositionSpec(entry=Entry(mode="on_signal")))
    assert any(i.rule == "S-univ" for i in validate_strategy(s))


def test_exit_condition_must_be_condition():
    s = StrategyIR(
        signal=_cond(), universe=Universe(kind="single", symbols=["005930"]),
        position=PositionSpec(entry=Entry(mode="on_signal"),
                              exit=Exit(condition=_score())))
    assert any(i.rule == "S-exit" for i in validate_strategy(s))


# ── 루트 경계 타입 계약 (명세 §5.4) ───────────────────────────────────────────

def test_group_label_must_be_label():
    """그룹 노출 라벨은 label만 — score면 S-overlay 거부."""
    s = StrategyIR(signal=_score(), universe=Universe(kind="all"),
                   position=PositionSpec(entry=Entry(mode="scheduled"),
                                         overlays=Overlays(max_group_pct=30.0, group_label=_score())))
    assert any(i.rule == "S-overlay" for i in validate_strategy(s))


def test_sweep_label_must_be_label():
    """펼침 분할 라벨은 label만 — score면 S-sweep 거부."""
    s = StrategyIR(signal=_score(), universe=Universe(kind="all"),
                   position=PositionSpec(entry=Entry(mode="scheduled")),
                   query="simulate", study=Study(axis="label", reduction="contrast", label=_score()))
    assert any(i.rule == "S-sweep" for i in validate_strategy(s))


def test_screener_condition_must_be_condition():
    """스크리너 조건은 condition만 — score면 S-univ 거부."""
    s = StrategyIR(signal=_score(),
                   universe=Universe(kind="list", symbols=["AAA", "BBB"],
                                     screener={"condition": _score().model_dump()}),
                   position=PositionSpec(entry=Entry(mode="scheduled")))
    assert any(i.rule == "S-univ" for i in validate_strategy(s))


def test_sweep_event_must_be_condition():
    """펼침 이벤트는 condition만 — score면 S-event 거부."""
    s = StrategyIR(signal=_score(), universe=Universe(kind="all"),
                   position=PositionSpec(entry=Entry(mode="scheduled")),
                   query="relate", study=Study(event=_score(), windows=[5]))
    assert any(i.rule == "S-event" for i in validate_strategy(s))


def test_capabilities_no_screener_kind():
    """능력 카탈로그는 screener를 universe_kind 값으로 제시하지 않는다(세부조건 필드로 직교화)."""
    from quant_core.ir_engine.capabilities import capability_spec
    cap = capability_spec()
    kinds = [k["value"] for k in cap["universe_kind"]]
    assert "screener" not in kinds
    assert set(kinds) == {"single", "list", "all"}


def test_roundtrip_serialization():
    s = StrategyIR(signal=_score(), universe=Universe(kind="all"),
                   position=PositionSpec(direction="long_short",
                                         entry=Entry(mode="scheduled", top_n=10)))
    restored = StrategyIR.model_validate(s.model_dump())
    assert restored == s


# ── 의미 검증 (M-rules) — 타입은 유효하나 논리적으로 공허·모순 ─────────────────

def _on_signal(sig, exit_=None):
    return StrategyIR(signal=sig, universe=Universe(kind="single", symbols=["005930"]),
                      position=PositionSpec(entry=Entry(mode="on_signal"),
                                            exit=exit_ or Exit()))


def test_m1_constant_signal_rejected():
    """순수 상수 신호(시장 미참조) → M-const 에러 (사용자 예시: 이벤트 진입 + 상수)."""
    sig = Node(op="compare", params={"op": ">"},
               inputs={"left": const(5.0), "right": const(0.0)})
    assert any(i.rule == "M-const" and i.is_error for i in validate_strategy(_on_signal(sig)))


def test_m1_arithmetic_only_signal_rejected():
    """상수 산술만으로 만든 조건도 시장 미참조 → M-const."""
    expr = Node(op="binary", params={"op": "+"},
                inputs={"a": const(1.0), "b": const(2.0)})
    sig = Node(op="compare", params={"op": ">"}, inputs={"left": expr, "right": const(0.0)})
    assert any(i.rule == "M-const" and i.is_error for i in validate_strategy(_on_signal(sig)))


def test_m2_self_comparison_rejected():
    """X > X (좌우 동일) → 항상 거짓 → M-degen."""
    sig = Node(op="compare", params={"op": ">"},
               inputs={"left": data("__SELF__.Close"), "right": data("__SELF__.Close")})
    assert any(i.rule == "M-degen" and i.is_error for i in validate_strategy(_on_signal(sig)))


def test_m2_self_subtraction_rejected():
    """(Close − Close) ≡ 0 (상수) → M-degen."""
    diff = Node(op="binary", params={"op": "-"},
                inputs={"a": data("__SELF__.Close"), "b": data("__SELF__.Close")})
    sig = Node(op="compare", params={"op": ">"}, inputs={"left": diff, "right": const(-1.0)})
    assert any(i.rule == "M-degen" and i.is_error for i in validate_strategy(_on_signal(sig)))


def test_m2_exit_tautology_rejected():
    """청산조건이 동어반복(Close>=Close) → 즉시청산 → M-degen(exit.condition)."""
    taut = Node(op="compare", params={"op": ">="},
                inputs={"left": data("__SELF__.Close"), "right": data("__SELF__.Close")})
    errs = validate_strategy(_on_signal(_cond(), Exit(condition=taut)))
    assert any(i.rule == "M-degen" and "exit" in i.path for i in errs)


def test_m3_nonpositive_window_rejected():
    """ts_mean window ≤ 0 → 크래시/무거래 → M-window."""
    ma = Node(op="ts_mean", params={"window": 0}, inputs={"signal": data("__SELF__.Close")})
    sig = Node(op="compare", params={"op": ">"},
               inputs={"left": data("__SELF__.Close"), "right": ma})
    assert any(i.rule == "M-window" and i.is_error for i in validate_strategy(_on_signal(sig)))


def test_m3_negative_window_rejected():
    ma = Node(op="ts_mean", params={"window": -5}, inputs={"signal": data("__SELF__.Close")})
    sig = Node(op="compare", params={"op": "<"},
               inputs={"left": data("__SELF__.Close"), "right": ma})
    assert any(i.rule == "M-window" for i in validate_strategy(_on_signal(sig)))


def test_meaningful_strategy_has_no_m_errors():
    """정상 전략(Close가 20일 평균 상회)은 M-* 에러 없음 — 회귀 가드(거짓양성 방지)."""
    ma = Node(op="ts_mean", params={"window": 20}, inputs={"signal": data("__SELF__.Close")})
    sig = Node(op="compare", params={"op": ">"},
               inputs={"left": data("__SELF__.Close"), "right": ma})
    errs = validate_strategy(_on_signal(sig))
    assert not any(i.rule.startswith("M-") and i.is_error for i in errs), \
        [i for i in errs if i.rule.startswith("M-")]


# ── M4·M5·M6 — 파라미터 범위·부호·공허 조합 ───────────────────────────────────

def test_m4_top_n_zero_rejected():
    s = StrategyIR(signal=_score(), universe=Universe(kind="all"),
                   position=PositionSpec(entry=Entry(mode="scheduled", top_n=0)))
    assert any(i.rule == "M-select" and i.is_error for i in validate_strategy(s))


def test_m4_top_pct_out_of_range_rejected():
    s = StrategyIR(signal=_score(), universe=Universe(kind="all"),
                   position=PositionSpec(entry=Entry(mode="scheduled", top_pct=200.0)))
    assert any(i.rule == "M-select" and i.is_error for i in validate_strategy(s))


def test_m5_hold_days_zero_rejected():
    assert any(i.rule == "M-exit" and i.is_error
               for i in validate_strategy(_on_signal(_cond(), Exit(hold_days=0))))


def test_m5_positive_stop_loss_rejected():
    """손절은 음수(%) 관례 — 양수면 부호 오류 → M-exit."""
    assert any(i.rule == "M-exit" and i.is_error
               for i in validate_strategy(_on_signal(_cond(), Exit(stop_loss=5.0))))


def test_m5_negative_take_profit_rejected():
    assert any(i.rule == "M-exit" and i.is_error
               for i in validate_strategy(_on_signal(_cond(), Exit(take_profit=-3.0))))


def test_m5_valid_exit_no_error():
    s = _on_signal(_cond(), Exit(take_profit=8.0, stop_loss=-5.0, hold_days=10))
    assert not any(i.rule == "M-exit" for i in validate_strategy(s))


def test_m6_top_n_exceeds_universe_warns():
    """top_n>유니버스 종목수 → 경고(차단 아님)."""
    s = StrategyIR(signal=_score(), universe=Universe(kind="list", symbols=["005930", "000660"]),
                   position=PositionSpec(entry=Entry(mode="scheduled", top_n=5)))
    issues = validate_strategy(s)
    assert any(i.rule == "M-vacuous" for i in issues)
    assert not any(i.rule == "M-vacuous" and i.is_error for i in issues)   # WARN


def test_m6_ghost_fixed_weight_warns():
    s = StrategyIR(signal=_score(), universe=Universe(kind="list", symbols=["005930"]),
                   position=PositionSpec(sizing=Sizing(mode="fixed_weight", weights={"AAPL": 1.0}),
                                         entry=Entry(mode="scheduled")))
    assert any(i.rule == "M-vacuous" for i in validate_strategy(s))


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

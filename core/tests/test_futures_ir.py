"""선물 IR 스키마층 — 계약 카탈로그·SimSpec 선물필드·capability 의미론·검증 규칙.

핵심: 선물다움은 신호 어휘가 아니라 (1) 계약 카탈로그(instrument_spec) (2) SimSpec 연속물
필드 (3) 검증 규칙에 atomic하게 들어간다. 신호·Universe·Sizing 어휘는 무변경.
(엔진 회계 — 승수 PnL·증거금 사이징·만기 롤 — 은 다음 단계, 본 테스트 범위 밖.)

    cd platform/core && python -m pytest tests/test_futures_ir.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.blocks import const, data
from quant_core.blocks.node import Node
from quant_core.exec_defaults import (InstrumentSpec, instrument_spec, is_futures,
                                       margin_rate, round_to_tick)
from quant_core.ir_engine import (Entry, PositionSpec, SimSpec, StrategyIR,
                                   Universe, capability_spec, explain_ir,
                                   validate_strategy)


# ── 계약 카탈로그 (단일 출처) ──────────────────────────────────────────────────

def test_futures_resolves_to_spec():
    s = instrument_spec("원유선물")
    assert isinstance(s, InstrumentSpec)
    assert s.asset_class == "futures"
    assert s.multiplier == 1_000.0          # WTI point value(배럴/계약)
    assert 0 < s.init_margin_rate < 1       # 부분증거금
    assert s.expiry_rule and s.default_roll  # 만기·롤 규칙 존재


def test_kospi200_is_real_futures_etf_separated():
    # F1: "코스피200선물" = 실선물(지수포인트·승수 250,000), CSV 백필+KIS 증분 수급.
    # 261220 ETF는 "코스피200선물ETF" 별도 키로 분리 — 승수 충돌(F0 임시 후퇴) 해소.
    s = instrument_spec("코스피200선물")
    assert s.asset_class == "futures"
    assert s.multiplier == 250_000.0 and s.currency == "KRW"
    assert 0 < s.init_margin_rate < 1
    assert instrument_spec("코스피200선물ETF").asset_class == "equity"   # ETF는 주식
    assert is_futures("코스피200선물ETF") is False


def test_overseas_futures_usd_and_multiplier():
    assert instrument_spec("원유선물").currency == "USD"
    assert instrument_spec("원유선물").multiplier == 1000.0
    assert instrument_spec("나스닥선물").multiplier == 20.0


def test_equity_symbol_is_default_spec():
    s = instrument_spec("005930")           # 삼성전자(주식)
    assert s.asset_class == "equity"
    assert s.multiplier == 1.0              # 승수 없음(현금모델 그대로)
    assert s.init_margin_rate == 1.0        # 전액 증거금
    assert s.currency == "KRW"              # 숫자코드 → KRW
    assert instrument_spec("AAPL").currency == "USD"   # 비숫자 → USD


def test_is_futures_helper():
    assert is_futures("원유선물") is True
    assert is_futures("코스피200선물") is True    # F1: 실선물로 승격
    assert is_futures("코스피200선물ETF") is False  # ETF는 주식
    assert is_futures("005930") is False
    assert is_futures("AAPL") is False


def test_margin_rate_single_source_from_catalog():
    # margin_rate는 카탈로그를 그대로 반영(중복 출처 제거)
    assert margin_rate("원유선물") == instrument_spec("원유선물").init_margin_rate
    assert margin_rate("005930") == 1.0     # 주식=전액


# ── SimSpec 선물 필드 (스키마 표면 추가) ───────────────────────────────────────

def test_simspec_accepts_futures_fields():
    sim = SimSpec(roll_method="days_before_5", series_adjust="back_adjust",
                  roll_cost_pct=0.2, account_currency="USD")
    assert sim.roll_method == "days_before_5"
    assert sim.series_adjust == "back_adjust"
    assert sim.roll_cost_pct == 0.2
    assert sim.account_currency == "USD"


def test_simspec_futures_fields_default_inert():
    sim = SimSpec()
    assert sim.roll_method is None and sim.series_adjust is None
    assert sim.roll_cost_pct is None
    assert sim.account_currency == "KRW"    # 국내 기본 — equity면 무관


# ── capability_spec 선물 의미론 (NL→IR 컴파일러 입력) ─────────────────────────

def test_capability_documents_futures_instruments():
    cap = capability_spec()
    assert "instruments" in cap
    # 롤·연속물·통화 옵션이 {value, does} 리스트로 노출(coverage 가드 만족 + LLM 가시)
    assert "roll_method" in cap and "series_adjust" in cap and "account_currency" in cap
    roll_vals = {it["value"] for it in cap["roll_method"]}
    assert {"days_before_5", "volume_cross"} <= roll_vals
    # 선물 심볼명·숏 의미가 컴파일러가 읽는 텍스트(does/use_for)에 실제로 들어가야 함
    text = cap["instruments"]["does"] + cap["instruments"]["use_for"]
    assert "원유선물" in text and "나스닥선물" in text   # 실 선물 심볼이 컴파일러 텍스트에
    assert "short" in cap["instruments"]["use_for"]


def test_capability_direction_mentions_futures_short():
    cap = capability_spec()
    short = next(d for d in cap["direction"] if d["value"] == "short")
    assert "선물" in short["use_for"]       # 선물 숏 차입불필요 명시


# ── 검증 규칙 (silent no-op 방지) ──────────────────────────────────────────────

def _strat(symbol: str, **sim) -> StrategyIR:
    return StrategyIR(
        signal=data("momentum_12_1m"),
        universe=Universe(kind="single", symbols=[symbol]),
        position=PositionSpec(entry=Entry(mode="always")),
        simulation=SimSpec(**sim),
    )


def test_roll_settings_on_equity_universe_warns():
    issues = validate_strategy(_strat("005930", roll_method="days_before_5"))
    assert any(i.rule == "S-futures" for i in issues)


def test_roll_settings_on_futures_universe_ok():
    issues = validate_strategy(_strat("원유선물", roll_method="days_before_5"))
    assert not any(i.rule == "S-futures" for i in issues)


def test_no_roll_settings_no_futures_warning():
    issues = validate_strategy(_strat("005930"))   # roll 미설정 → 경고 없음
    assert not any(i.rule == "S-futures" for i in issues)


# ── 선물 계약 틱 라운딩 (InstrumentSpec.tick 소비 — 더는 dead 필드 아님) ──────────

def test_round_to_tick_futures_explicit_tick():
    # 명시 틱이면 통화 무관 그 배수로 라운딩, float 유지(선물은 분수 호가).
    assert round_to_tick(404.07, "down", "USD", tick=0.05) == 404.05
    assert round_to_tick(404.07, "up",   "USD", tick=0.05) == 404.10
    assert round_to_tick(401.03, "nearest", "KRW", tick=0.05) == 401.05   # 지수선물 분수 pt


def test_round_to_tick_equity_unaffected_by_default():
    # tick=0(주식 기본)이면 기존 통화별 표 그대로 — 정수 KRW 호가, 완전 무영향(백워드 호환).
    assert round_to_tick(15037, "down", "KRW") == 15030       # 1만~2만 → 10원 틱
    assert isinstance(round_to_tick(15037, "down", "KRW"), int)
    assert round_to_tick(404.07, "down", "USD") == 404.07      # USD $0.01


# ── 혼합 증거금 유니버스 경고 (S-futures-mix — silent 근사 표면화) ─────────────────

def _list_strat(symbols):
    return StrategyIR(
        signal=data("momentum_12_1m"),
        universe=Universe(kind="list", symbols=symbols),
        position=PositionSpec(entry=Entry(mode="always")),
        simulation=SimSpec())


def test_mixed_equity_futures_universe_warns():
    issues = validate_strategy(_list_strat(["005930", "원유선물"]))
    assert any(i.rule == "S-futures-mix" for i in issues)


def test_homogeneous_universe_no_mix_warning():
    assert not any(i.rule == "S-futures-mix"
                   for i in validate_strategy(_list_strat(["원유선물", "나스닥선물"])))
    assert not any(i.rule == "S-futures-mix"
                   for i in validate_strategy(_list_strat(["005930", "000660"])))


# ── 선물 이벤트(on_signal) 미지원 경고 (엔진 가드를 validate에 미러 — finding 2) ─────

def _event_strat(symbol: str) -> StrategyIR:
    cond = Node(op="compare", params={"op": ">"},
                inputs={"left": data("Close"), "right": const(0)})
    return StrategyIR(signal=cond, universe=Universe(kind="single", symbols=[symbol]),
                      position=PositionSpec(entry=Entry(mode="on_signal")), simulation=SimSpec())


def test_futures_on_signal_warns_event_unsupported():
    issues = validate_strategy(_event_strat("원유선물"))
    assert any(i.rule == "S-futures-event" for i in issues)


def test_equity_on_signal_no_event_warning():
    assert not any(i.rule == "S-futures-event"
                   for i in validate_strategy(_event_strat("005930")))


def test_futures_trend_following_no_event_warning():
    # always(추세추종) 선물은 백테스트 지원 경로 → 이벤트 경고 없음
    assert not any(i.rule == "S-futures-event"
                   for i in validate_strategy(_strat("원유선물")))


# ── explain_ir 정직성: 선물 롤·통화 '미적용' 결정론적 표면화 (finding 1) ──────────

def test_explain_surfaces_futures_roll_not_applied():
    doc = explain_ir(_strat("원유선물", roll_method="days_before_5"))
    env = next(b for b in doc["buckets"] if b["key"] == "environment")
    txt = " ".join(it["value"] for it in env["items"])
    assert "미적용" in txt and "연속 시계열" in txt
    assert "미반영" in txt          # 설정한 roll_method이 미반영임을 명시


def test_explain_equity_has_no_futures_note():
    doc = explain_ir(_strat("005930"))
    env = next(b for b in doc["buckets"] if b["key"] == "environment")
    labels = " ".join(it["label"] for it in env["items"])
    assert "선물 연속물" not in labels

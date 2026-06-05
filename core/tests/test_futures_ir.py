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

from quant_core.blocks import data
from quant_core.exec_defaults import (InstrumentSpec, instrument_spec, is_futures,
                                       margin_rate)
from quant_core.ir_engine import (Entry, PositionSpec, SimSpec, StrategyIR,
                                   Universe, capability_spec, validate_strategy)


# ── 계약 카탈로그 (단일 출처) ──────────────────────────────────────────────────

def test_domestic_futures_resolves_to_spec():
    s = instrument_spec("코스피200선물")
    assert isinstance(s, InstrumentSpec)
    assert s.asset_class == "futures"
    assert s.multiplier == 250_000.0       # KOSPI200 point value(원/pt)
    assert s.currency == "KRW"
    assert 0 < s.init_margin_rate < 1       # 부분증거금
    assert s.expiry_rule and s.default_roll  # 만기·롤 규칙 존재


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
    assert is_futures("코스피200선물") is True
    assert is_futures("005930") is False
    assert is_futures("AAPL") is False


def test_margin_rate_single_source_from_catalog():
    # margin_rate는 카탈로그를 그대로 반영(중복 출처 제거)
    assert margin_rate("코스피200선물") == instrument_spec("코스피200선물").init_margin_rate
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
    assert "코스피200선물" in text and "원유선물" in text
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
    issues = validate_strategy(_strat("코스피200선물", roll_method="days_before_5"))
    assert not any(i.rule == "S-futures" for i in issues)


def test_no_roll_settings_no_futures_warning():
    issues = validate_strategy(_strat("005930"))   # roll 미설정 → 경고 없음
    assert not any(i.rule == "S-futures" for i in issues)

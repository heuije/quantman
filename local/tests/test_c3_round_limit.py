"""C3 — 지정가 호가단위 라운딩이 선물 계약 틱(instrument_spec.tick)을 따르는지.

기존엔 모든 종목이 통화별 호가표(주식 KRW/USD)로 라운딩돼, 선물 계약 틱(KOSPI200 0.05·
GC 0.10·NQ 0.25·BTC 5)을 무시했다. 특히 BTC(틱 5)는 정수 라운딩이 그리드를 이탈해 KIS가
거부할 수 있었다. C3는 선물에 한해 instrument_spec.tick 그리드로 라운딩한다. **주식은 무변경.**

    cd platform/local && python -m pytest tests/test_c3_round_limit.py -q
"""
from __future__ import annotations

import quant_core as qc

from localapp.trader import _currency_of, _round_limit


# ── 선물: 계약 틱 그리드 ────────────────────────────────────────────────────────
def test_round_limit_domestic_futures_tick_005():
    # 코스피200선물 tick 0.05 — 353.47 → up 353.50 / down 353.45 (통화표 정수 라운딩 아님)
    assert _round_limit(353.47, "up", "코스피200선물") == 353.5
    assert _round_limit(353.47, "down", "코스피200선물") == 353.45


def test_round_limit_overseas_futures_tick_010():
    # 금선물 tick 0.10 (USD) — 1950.07 → up 1950.10 / down 1950.00
    assert _round_limit(1950.07, "up", "금선물") == 1950.1
    assert _round_limit(1950.07, "down", "금선물") == 1950.0


def test_round_limit_btc_tick_5_avoids_offgrid():
    # 비트코인선물 tick 5 — 60003 → up 60005 / down 60000 (정수 라운딩이면 60003=그리드 이탈)
    assert _round_limit(60003, "up", "비트코인선물") == 60005
    assert _round_limit(60003, "down", "비트코인선물") == 60000


def test_round_limit_nasdaq_tick_025():
    # 나스닥선물 tick 0.25 — 15000.40 → up 15000.50 / down 15000.25
    assert _round_limit(15000.40, "up", "나스닥선물") == 15000.5
    assert _round_limit(15000.40, "down", "나스닥선물") == 15000.25


# ── 주식: 통화별 호가표 그대로 (byte-identical) ─────────────────────────────────
def test_round_limit_kr_equity_unchanged():
    # 국내 주식(005930) — KRW 호가표 그대로(틱 미적용)
    assert _round_limit(70123, "up", "005930") == qc.round_to_tick(
        70123, direction="up", currency="KRW")
    assert _currency_of("005930") == "KRW"


def test_round_limit_us_equity_unchanged():
    # 미국 주식(AAPL) — USD 센트 그대로
    assert _round_limit(150.123, "up", "AAPL") == qc.round_to_tick(
        150.123, direction="up", currency="USD")
    assert _currency_of("AAPL") == "USD"

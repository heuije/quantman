"""L-03 회귀 — KRX 캘린더 + 휴장일 게이트.

KRX 평일 휴장(설/추석/광복절·부처님오신날 대체공휴일 등)에도 cron이 그대로
fire되어 cycle/intraday/settlement가 돌면 휴장 시세로 stop loss를 평가하거나
KIS에 매도 발주가 들어가는 위험이 있었다. is_session_day("KR", day)로 게이트.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

_CORE_DIR = Path(__file__).resolve().parent.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from quant_core import market_calendar as mc


def test_us_still_supported():
    """기존 US 동작 회귀 — 평일·휴장일 판정 그대로."""
    # 2026-05-25 NYSE Memorial Day (Mon) — 휴장
    assert mc.is_session_day("US", date(2026, 5, 25)) is False
    # 2026-05-26 (Tue) — 정상 거래일
    assert mc.is_session_day("US", date(2026, 5, 26)) is True


def test_kr_weekend_closed():
    # 2026-05-23 (Sat), 2026-05-24 (Sun) — 휴장
    assert mc.is_session_day("KR", date(2026, 5, 23)) is False
    assert mc.is_session_day("KR", date(2026, 5, 24)) is False


def test_kr_known_holidays_closed():
    # 어린이날(2026-05-05 화요일), 광복절(2026-08-15 토 → 8-17 월 대체)
    # 부처님오신날 대체공휴일 2026-05-25 (월) — 부처님오신날(5/24)이 일요일
    assert mc.is_session_day("KR", date(2026, 5, 5)) is False
    assert mc.is_session_day("KR", date(2026, 5, 25)) is False


def test_kr_normal_weekday_open():
    # 2026-05-22 (금) — 정상 거래일
    assert mc.is_session_day("KR", date(2026, 5, 22)) is True
    # 2026-05-26 (화) — 정상 거래일
    assert mc.is_session_day("KR", date(2026, 5, 26)) is True


def test_kr_session_hours():
    """KRX 09:00 ~ 15:30 KST."""
    sess = mc.session_kst("KR", date(2026, 5, 22))
    assert sess is not None
    o, c = sess
    assert (o.hour, o.minute) == (9, 0)
    assert (c.hour, c.minute) == (15, 30)


def test_kr_coverage():
    start, end = mc.coverage_range("KR")
    assert start <= "2024-01-02"
    assert end >= "2030-12-30"


def test_prev_session_day_skips_weekend_and_holiday():
    """신선도 게이트 기준일 — 주말·공휴일을 건너뛴 직전 거래일."""
    # 2026-05-26(화) 직전 거래일 = 05-22(금): 주말(23·24) + 부처님오신날 대체(25 월) 건너뜀
    assert mc.prev_session_day("KR", date(2026, 5, 26)) == date(2026, 5, 22)
    # 휴장일(05-25 월) 기준으로도 직전 거래일 = 05-22
    assert mc.prev_session_day("KR", date(2026, 5, 25)) == date(2026, 5, 22)
    # 토요일(05-23) 기준 = 05-22
    assert mc.prev_session_day("KR", date(2026, 5, 23)) == date(2026, 5, 22)


def test_prev_session_day_us_memorial_day():
    # US도 동일 — 05-25 Memorial Day 휴장 + 주말 건너뛰어 05-22
    assert mc.prev_session_day("US", date(2026, 5, 26)) == date(2026, 5, 22)


def test_prev_session_day_before_calendar_is_none():
    # 캘린더 범위(과거 경계) 밖 → None (호출자는 신선도 판정 보류·over-block 금지)
    assert mc.prev_session_day("KR", date(2000, 1, 1)) is None


def test_unknown_market_raises():
    with pytest.raises(mc.CalendarError):
        mc.is_session_day("JP", date(2026, 5, 22))

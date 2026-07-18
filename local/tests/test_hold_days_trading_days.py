"""로드맵 D — 보유일 만기를 거래일(봉 수) 기준으로 통일 (백테스트 파리티).

종전 달력일 산술((today−entry).days)은 주말·휴장을 세어 hold_days가 긴
전략일수록 실전이 백테스트(held=i−entry_i, 봉 수)보다 일찍 만기됐다.

1. _held_trading_days — dataset 봉 수 + "오늘 봉 미도착이면 오늘=진행 봉 1" 규칙.
2. market_calendar.sessions_between — 정산 관측 경로용 (start, end] 카운트,
   커버 범위 밖은 None(추정 금지).
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from localapp.trader import _held_trading_days
from quant_core import market_calendar as mc


def _ds(*dates):
    idx = pd.to_datetime(list(dates))
    return {"X": pd.DataFrame({"Close": [1.0] * len(idx)}, index=idx)}


def test_weekend_not_counted():
    """금요일 진입 → 월요일 아침(봉은 금요일까지): 거래일 1 (달력일은 3)."""
    ds = _ds("2026-07-09", "2026-07-10")          # 목·금
    held = _held_trading_days(ds, "X", "2026-07-10", date(2026, 7, 13))  # 월
    assert held == 1


def test_holiday_gap_matches_bar_count():
    """중간 휴장(봉 부재)은 세지 않는다 — 백테스트 봉 수와 동일."""
    ds = _ds("2026-07-10", "2026-07-13", "2026-07-16")   # 14·15 휴장 가정
    held = _held_trading_days(ds, "X", "2026-07-10", date(2026, 7, 16))
    assert held == 2                                     # 13, 16


def test_today_bar_present_vs_absent_equivalent():
    """오늘 봉 유무와 무관하게 같은 값 — 진행 봉 +1 규칙."""
    with_today = _ds("2026-07-10", "2026-07-13", "2026-07-14")
    without_today = _ds("2026-07-10", "2026-07-13")
    t = date(2026, 7, 14)
    assert _held_trading_days(with_today, "X", "2026-07-10", t) == 2
    assert _held_trading_days(without_today, "X", "2026-07-10", t) == 2


def test_entry_day_is_zero():
    """진입 당일 = 진입 바 → 0 (백테스트 i−entry_i=0과 동일).

    0이 아니면 당일매매(hold_days=0)가 같은 날 재실행(08:52 수렴)에서
    '보유기간 1일' 경로로 종가 전에 조기 청산된다 — 회귀 방지 핵심."""
    ds = _ds("2026-07-10", "2026-07-13")
    assert _held_trading_days(ds, "X", "2026-07-14", date(2026, 7, 14)) == 0
    # 다음 거래일 아침(봉은 전일까지) → 1 (당일매매 익일 회수 판정 정합)
    ds2 = _ds("2026-07-10", "2026-07-13", "2026-07-14")
    assert _held_trading_days(ds2, "X", "2026-07-14", date(2026, 7, 15)) == 1


def test_long_hold_uses_bars_not_calendar():
    """20거래일 보유 — 달력일(28)이 아니라 봉 수(20)."""
    idx = pd.bdate_range("2026-06-01", periods=21)       # 진입봉 + 20
    ds = {"X": pd.DataFrame({"Close": [1.0] * len(idx)}, index=idx)}
    entry = idx[0].date().isoformat()
    today = idx[-1].date()
    assert _held_trading_days(ds, "X", entry, today) == 20
    assert (today - idx[0].date()).days == 28            # 종전 산술은 28로 조기 만기


def test_missing_symbol_raises():
    with pytest.raises(ValueError, match="시세 없음"):
        _held_trading_days({}, "X", "2026-07-10", date(2026, 7, 14))


# ── sessions_between (정산 관측 경로) ────────────────────────────────────────


@pytest.fixture
def fake_cal(monkeypatch):
    days = ["2026-07-09", "2026-07-10", "2026-07-13", "2026-07-16", "2026-07-17"]
    monkeypatch.setattr(mc, "_load", lambda m: {"sorted_days": days})


def test_sessions_between_counts_half_open(fake_cal):
    assert mc.sessions_between("KR", date(2026, 7, 10), date(2026, 7, 17)) == 3
    assert mc.sessions_between("KR", date(2026, 7, 10), date(2026, 7, 13)) == 1
    assert mc.sessions_between("KR", date(2026, 7, 10), date(2026, 7, 10)) == 0
    # 비세션일 경계도 정확 (주말 진입·주말 종료)
    assert mc.sessions_between("KR", date(2026, 7, 11), date(2026, 7, 14)) == 1


def test_sessions_between_out_of_coverage_none(fake_cal):
    """커버 범위 이전 진입(장기 보유) — 추정 대신 None(판정 보류)."""
    assert mc.sessions_between("KR", date(2026, 7, 1), date(2026, 7, 17)) is None

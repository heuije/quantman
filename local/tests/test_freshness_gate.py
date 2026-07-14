"""신선도 게이트(fail-stale) 회귀 — trader._bar_is_stale + prev_session_day 배선.

2026-07-14 mwmw 사고: 서버가 08:10 KRX 선물을 번들에 재포장하지 않아 07-10 봉(1210.5)이
'전일 종가'로 참조가·사이징·넷팅에 쓰였다(로컬 신선도 검증 부재). 이 게이트가 마지막 봉이
직전 거래일보다 오래됐을 때 stale로 판정해, 진입 경로가 live 우선/보류(fail-closed)하도록 한다.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

_LOCAL_DIR = Path(__file__).resolve().parent.parent
_CORE_DIR = _LOCAL_DIR.parent / "core"
for _p in (str(_LOCAL_DIR), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from localapp import trader

# today=2026-05-26(화) → KR 직전 거래일 = 05-22(금): 주말(23·24) + 부처님오신날 대체(25 월) 건너뜀
_TODAY = date(2026, 5, 26)


def _sdf(last_iso: str) -> pd.DataFrame:
    return pd.DataFrame({"Close": [1000.0]}, index=pd.to_datetime([last_iso]))


def test_stale_bar_flagged():
    """마지막 봉이 직전 거래일보다 오래 → stale True (07-14 시나리오)."""
    assert trader._bar_is_stale(_sdf("2026-05-20"), "코스피200선물", _TODAY) is True
    assert trader._bar_is_stale(_sdf("2026-05-10"), "코스피200선물", _TODAY) is True


def test_fresh_bar_not_flagged():
    """마지막 봉 = 직전 거래일(정상) → False. 주말·공휴일 gap이 있어도 stale 아님(over-block 방지)."""
    assert trader._bar_is_stale(_sdf("2026-05-22"), "코스피200선물", _TODAY) is False


def test_today_bar_not_flagged():
    """당일 봉(직전 거래일보다 새것) → False."""
    assert trader._bar_is_stale(_sdf("2026-05-26"), "코스피200선물", _TODAY) is False


def test_empty_or_bad_index_not_flagged():
    """빈 df·비날짜 인덱스 → 판정 불가라 over-block 금지(False)."""
    assert trader._bar_is_stale(pd.DataFrame({"Close": []}), "코스피200선물", _TODAY) is False
    assert trader._bar_is_stale(
        pd.DataFrame({"Close": [1.0]}, index=[0]), "코스피200선물", _TODAY) is False

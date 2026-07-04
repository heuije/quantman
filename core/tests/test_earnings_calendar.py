"""실적 캘린더 facet(yfinance earnings_dates) 단위 테스트 — 네트워크 없음(합성 DF).

검증: 과거(확정)/미래(예정) 분리·정렬·상한 / tz-aware 인덱스 정규화 / KR 6자리→.KS/.KQ
후보 / 빈 결과 정직(가짜 채움 0).
"""

from __future__ import annotations

import pandas as pd

from quant_core.data.feeds import earnings_calendar as ec


def _df(dates_cols):
    idx = pd.to_datetime([d for d, _ in dates_cols], utc=True)
    rows = [c for _, c in dates_cols]
    return pd.DataFrame(rows, index=idx)


def test_tickers_kr_and_us():
    assert ec._tickers("005930", "KR") == ["005930.KS", "005930.KQ"]
    assert ec._tickers("AAPL", "US") == ["AAPL"]
    assert ec._tickers("", None) == []


def test_split_past_upcoming():
    df = _df([
        ("2026-08-01", {"EPS Estimate": 2.0, "Reported EPS": None, "Surprise(%)": None}),
        ("2026-05-01", {"EPS Estimate": 1.5, "Reported EPS": 1.7, "Surprise(%)": 13.3}),
        ("2026-02-01", {"EPS Estimate": 1.0, "Reported EPS": 1.1, "Surprise(%)": 10.0}),
    ])
    res = ec._split(df, past=6, upcoming=4, now=pd.Timestamp("2026-06-01"))
    assert res["source"] == "yfinance"
    assert [r["date"] for r in res["upcoming"]] == ["2026-08-01"]
    # 과거는 최신순
    assert [r["date"] for r in res["past"]] == ["2026-05-01", "2026-02-01"]
    assert res["past"][0]["eps_actual"] == 1.7
    assert res["past"][0]["surprise_pct"] == 13.3
    assert res["upcoming"][0]["eps_actual"] is None      # 예정=실적 미정(정직)


def test_split_empty():
    assert ec._split(pd.DataFrame(), 6, 4) == {"past": [], "upcoming": [], "source": None}


def test_fetch_earnings_empty_symbol():
    assert ec.fetch_earnings("")["source"] is None

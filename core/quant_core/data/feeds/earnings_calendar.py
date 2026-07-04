"""실적 캘린더 on-demand facet — 종목별 실적 발표일(과거 확정 + 미래 예정).

news_kr 패턴의 on-demand 조회(벌크 아님·종목당 1콜, 전 종목 사전수집 비현실적). 단일 소스=
yfinance `earnings_dates`(데이터포인트당 소스 1개·백업 없음 — 4원칙). US=티커, KR=코드+.KS/.KQ
(데이터 있는 쪽 채택). 미제공·실패 시 빈 결과(정직 — 가짜 채움 0).

PIT: 과거=확정 발표일(+실적·서프라이즈) / 미래=예정일(변경 가능·현시점 스냅샷 — snapshot-only,
백테스트 이벤트 아님). describe 리포트 facet으로만 노출(챗 '실적 언제·다음 실적' 응답용).
run_describe_report(결정적 엔진) 밖 서버 엣지에서 조회 → 골든 불변(뉴스·추정실적 facet 규약 동형).
"""

from __future__ import annotations

import pandas as pd


def _tickers(symbol: str, market: str | None = None) -> list[str]:
    """종목 → yfinance 티커 후보. KR 6자리=.KS/.KQ 둘 다 시도, US=티커 그대로."""
    s = str(symbol or "").strip()
    if not s:
        return []
    if s.isdigit() and len(s) == 6:
        return [s + ".KS", s + ".KQ"]        # 유가증권/코스닥 — 데이터 있는 쪽 채택
    return [s.upper()]


def _num(v):
    """숫자 셀 → float(반올림) 또는 None(NaN/파싱 실패)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else round(f, 4)


def _row(idx, r) -> dict:
    """earnings_dates 행 → {date, eps_estimate, eps_actual, surprise_pct}. 순수함수."""
    return {"date": pd.Timestamp(idx).date().isoformat(),
            "eps_estimate": _num(r.get("EPS Estimate")),
            "eps_actual": _num(r.get("Reported EPS")),
            "surprise_pct": _num(r.get("Surprise(%)"))}


def _split(df: pd.DataFrame, past: int, upcoming: int, now=None) -> dict:
    """earnings_dates DF → {past(확정·최신순), upcoming(예정·임박순)}. 순수함수(테스트용).

    now = 과거/미래 경계(기본 현재 UTC·naive) — 테스트 결정성을 위해 주입 가능.
    """
    if df is None or df.empty:
        return {"past": [], "upcoming": [], "source": None}
    df = df.copy()
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(None)   # tz-aware→naive UTC
    now = pd.Timestamp.utcnow().tz_convert(None) if now is None else pd.Timestamp(now)
    past_rows = [_row(i, r) for i, r in
                 df[df.index < now].sort_index(ascending=False).iterrows()][:past]
    up_rows = [_row(i, r) for i, r in
               df[df.index >= now].sort_index().iterrows()][:upcoming]
    if not (past_rows or up_rows):
        return {"past": [], "upcoming": [], "source": None}
    return {"past": past_rows, "upcoming": up_rows, "source": "yfinance"}


def fetch_earnings(symbol: str, market: str | None = None,
                   past: int = 6, upcoming: int = 4) -> dict:
    """종목 실적 발표일(과거 확정 + 미래 예정). yfinance 단일 소스·미제공 시 빈 결과.

    반환 {"past": [{date, eps_estimate, eps_actual, surprise_pct}], "upcoming": [...],
    "source": "yfinance"|None}. past/upcoming = 각 방향 행 상한. KR은 커버리지 편차 있음(정직).
    """
    empty = {"past": [], "upcoming": [], "source": None}
    tickers = _tickers(symbol, market)
    if not tickers:
        return empty
    try:
        import yfinance as yf
    except Exception:                         # noqa: BLE001 — 의존성 부재=빈(정직)
        return empty
    for tk in tickers:
        try:
            df = yf.Ticker(tk).earnings_dates
        except Exception:                     # noqa: BLE001 — 외부 API 실패=빈(정직)
            continue
        res = _split(df, past, upcoming)
        if res["source"]:
            return res
    return empty

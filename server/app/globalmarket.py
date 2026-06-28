"""글로벌 시장 분석 — 세계 주요 지수·원자재 선물·미국 경제지표(컨센서스 대비).

소스(모두 무료):
- 지수·원자재: FinanceDataReader (Yahoo 백엔드)
- 경제지표 캘린더: TradingView 공개 endpoint (actual·forecast(컨센서스)·previous)

캐싱: 지수·원자재는 일자 키, 경제지표는 시간 키 lru_cache. 실패는 부분 제공.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from functools import lru_cache

_log = logging.getLogger("app.globalmarket")
_H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

# 세계 10대 지수 (표시명, FDR 심볼)
INDICES = [
    ("S&P 500", "US500"), ("나스닥", "IXIC"), ("다우", "DJI"),
    ("코스피", "KS11"), ("닛케이225", "N225"), ("항셍", "HSI"),
    ("항셍", "HSI"), ("FTSE100", "FTSE"), ("DAX", "^GDAXI"), ("유로스톡스50", "^STOXX50E"),
]
# 원자재 선물 (표시명, FDR 심볼, 단위, 분류)
# ⚠ LME 비철(니켈·아연·납·주석)·배터리 소재(탄산리튬·코발트)는 Yahoo/FDR에 신뢰할 선물
#    심볼이 없다(ZN=F=국채선물, LE=F=생우 등 오매핑 함정). 정확히 매핑되는 심볼만 수록한다.
COMMODITIES = [
    # 에너지
    ("WTI 원유", "CL=F", "$/bbl", "에너지"), ("Brent 원유", "BZ=F", "$/bbl", "에너지"),
    ("천연가스", "NG=F", "$/MMBtu", "에너지"), ("가솔린", "RB=F", "$/gal", "에너지"),
    ("난방유", "HO=F", "$/gal", "에너지"),
    # 금속·산업재 (귀금속 + 산업금속 + 철강 밸류체인)
    ("금", "GC=F", "$/oz", "금속"), ("은", "SI=F", "$/oz", "금속"),
    ("백금", "PL=F", "$/oz", "금속"), ("팔라듐", "PA=F", "$/oz", "금속"),
    ("구리", "HG=F", "$/lb", "금속"), ("알루미늄", "ALI=F", "$/톤", "금속"),
    ("철광석", "TIO=F", "$/톤", "금속"), ("철강(열연)", "HRC=F", "$/톤", "금속"),
]


def _fetch_close(symbol: str, days: int):
    import FinanceDataReader as fdr
    start = (date.today() - timedelta(days=days)).isoformat()
    df = fdr.DataReader(symbol, start)
    return df["Close"].dropna() if (df is not None and "Close" in df.columns) else None


@lru_cache(maxsize=2)
def _indices(_day: str) -> dict:
    """10대 지수 — 원시 종가 시계열(오버레이) + 현재값·등락률.

    리베이스(시작점 대비 %)는 프론트가 *가시 구간* 첫 점 기준으로 수행한다 —
    기간 컨트롤(1개월~10년)을 바꾸면 기준점도 그 구간 시작으로 따라가야 하므로
    서버에서 미리 % 변환하지 않고 원시 종가를 보낸다(스케일이 달라도 프론트가 정규화).
    """
    import pandas as pd
    from concurrent.futures import ThreadPoolExecutor

    def one(it):
        name, sym = it
        try:
            return name, sym, _fetch_close(sym, 3700)     # 10년치 — 기간 컨트롤(1개월~10년) 지원
        except Exception:
            return name, sym, None

    cols, items = {}, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for name, sym, c in ex.map(one, INDICES):
            if c is None or len(c) < 2:
                items.append({"name": name, "symbol": sym, "last": None, "chg_pct": None})
                continue
            last, prev = float(c.iloc[-1]), float(c.iloc[-2])
            items.append({"name": name, "symbol": sym, "last": round(last, 2),
                          "chg_pct": round((last / prev - 1) * 100, 2) if prev else None})
            cols[name] = c                                # 원시 종가 — 프론트가 가시 구간 기준 리베이스
    series = []
    if cols:
        df = pd.DataFrame(cols).ffill()
        for idx, row in df.iterrows():
            d = idx.date().isoformat() if hasattr(idx, "date") else str(idx)
            pt = {"date": d}
            for name in cols:
                v = row.get(name)
                pt[name] = None if pd.isna(v) else round(float(v), 2)
            series.append(pt)
    return {"series": series, "items": items}


@lru_cache(maxsize=2)
def _commodities(_day: str) -> dict:
    """원자재 선물 — 표용 현재값·변동(절대·%). 개별 차트는 프론트가 종목 차트(symbolDetail)로 재사용."""
    from concurrent.futures import ThreadPoolExecutor

    def one(it):
        name, sym, unit, cat = it
        try:
            return name, sym, unit, cat, _fetch_close(sym, 10)     # 최근값·전일대비만 필요
        except Exception:
            return name, sym, unit, cat, None

    out = []
    with ThreadPoolExecutor(max_workers=len(COMMODITIES)) as ex:
        for name, sym, unit, cat, c in ex.map(one, COMMODITIES):
            if c is None or len(c) < 2:
                out.append({"name": name, "symbol": sym, "unit": unit, "category": cat,
                            "last": None, "change": None, "chg_pct": None, "date": None})
                continue
            last, prev = float(c.iloc[-1]), float(c.iloc[-2])
            li = c.index[-1]
            d = li.date().isoformat() if hasattr(li, "date") else str(li)[:10]   # 기준일자(마지막 거래일)
            out.append({"name": name, "symbol": sym, "unit": unit, "category": cat,
                        "last": round(last, 4), "change": round(last - prev, 4),
                        "chg_pct": round((last / prev - 1) * 100, 2) if prev else None, "date": d})
    return {"items": out}


# 경제지표는 TradingView importance≥1(고중요)만 — CPI·PCE·PPI·ISM PMI·고용·금리·GDP·소매판매 등.
# 데이터 가용 최장(약 2015~). 20개년 요청해도 소스가 그 이전은 없음.
_ECON_START_YEAR = 2014


def _econ_title(e: dict) -> str:
    t = (e.get("title") or "").strip()
    per = (e.get("period") or "").strip()
    return f"{t} ({per})" if per else t


def _fetch_econ_range(frm: str, to: str) -> list:
    import requests
    try:
        r = requests.get("https://economic-calendar.tradingview.com/events",
                         params={"from": frm, "to": to, "countries": "US"},
                         headers={**_H, "Origin": "https://www.tradingview.com"}, timeout=20)
        return [e for e in r.json().get("result", []) if (e.get("importance") or -9) >= 1]
    except Exception as e:  # noqa: BLE001
        _log.warning("경제지표 fetch 실패 %s~%s: %s", frm, to, e)
        return []


def _fetch_econ_year(y: int) -> list:
    return _fetch_econ_range(f"{y}-01-01T00:00:00.000Z", f"{y}-12-31T23:59:59.000Z")


@lru_cache(maxsize=2)
def _econ(_day: str) -> dict:
    """미국 경제지표 — 과거(실제 vs 컨센서스 vs 직전 + 서프라이즈) 최장기간 + 연내 예정(컨센서스).

    연도별로 받아 합친다(TradingView가 한 번 호출에 2000건 cap이라 연 단위 청크가 필요).
    """
    from concurrent.futures import ThreadPoolExecutor
    now = datetime.utcnow()
    cur, today = now.year, now.strftime("%Y-%m-%d")
    yr_end = f"{cur}-12-31"
    years = list(range(_ECON_START_YEAR, cur + 1))       # 과거 = 연도 청크(2000건 cap 회피)
    allev = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for evs in ex.map(_fetch_econ_year, years):
            allev.extend(evs)
    # 예정은 연 청크의 2000건 cap에 미래가 잘리므로 '오늘~연말'을 직접 조회.
    fut = _fetch_econ_range(f"{today}T00:00:00.000Z", f"{cur}-12-31T23:59:59.000Z")

    hist, seen_h = [], set()
    for e in allev:                                      # 과거 = 발표 완료(실제값 있음)
        a = e.get("actual")
        if a is None:
            continue
        d, title = (e.get("date") or "")[:10], _econ_title(e)
        if (title, d) in seen_h:
            continue
        seen_h.add((title, d))
        f, p = e.get("forecast"), e.get("previous")
        surprise = round(a - f, 2) if isinstance(a, (int, float)) and isinstance(f, (int, float)) else None
        hist.append({"title": title, "date": d, "actual": a, "forecast": f, "previous": p,
                     "surprise": surprise, "unit": e.get("unit") or ""})

    up, seen_u = [], set()
    for e in fut:                                        # 예정 = 미래(실제 없음·컨센서스 있음·연내)
        a, f = e.get("actual"), e.get("forecast")
        d = (e.get("date") or "")[:10]
        if a is not None or f is None or not (today <= d <= yr_end):
            continue
        title = _econ_title(e)
        if (title, d) in seen_u:
            continue
        seen_u.add((title, d))
        up.append({"title": title, "date": d, "forecast": f, "previous": e.get("previous"),
                   "unit": e.get("unit") or ""})

    hist.sort(key=lambda x: x["date"], reverse=True)
    up.sort(key=lambda x: x["date"])
    return {"events": hist, "upcoming": up, "span": [years[0], cur]}


def indices() -> dict:
    try:
        return _indices(date.today().isoformat())
    except Exception as e:
        _log.warning("글로벌 지수 fetch 실패: %s", e)
        return {"series": [], "items": []}


def commodities() -> dict:
    try:
        return _commodities(date.today().isoformat())
    except Exception as e:
        _log.warning("원자재 fetch 실패: %s", e)
        return {"items": []}


def econ() -> dict:
    try:
        return _econ(date.today().isoformat())
    except Exception as e:
        _log.warning("경제지표 fetch 실패: %s", e)
        return {"events": [], "upcoming": [], "span": []}

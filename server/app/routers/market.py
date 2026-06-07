"""시장 컨텍스트 — 자동매매 페이지 상단에 띄울 환경 정보.

지수·VIX·환율 최근값 + 전일대비 %, KRX 거래일 캘린더 (간단 휴장 추정).
모두 data_cache의 dataset에서 가져온다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException

import quant_core as qc

from ..deps import get_current_user
from ..models import User

router = APIRouter(prefix="/market", tags=["market"])
_log = logging.getLogger("app.market")


# 표시할 시장 지표 (라벨, dataset 심볼 후보들)
_MARKET_SYMBOLS = [
    # KOSPI 프록시 = 261220 ETF(코스피200선물ETF). F1에서 "코스피200선물" 키가 ETF→실선물(승수
    # 250,000·지수포인트 스케일)로 의미가 바뀌어, 이 표시 후보를 ETF 키로 고정(스케일 회귀 차단).
    ("KOSPI",   ["KOSPI", "코스피", "코스피200선물ETF", "KS11", "^KS11"]),
    ("KOSDAQ",  ["KOSDAQ", "코스닥", "KQ11", "^KQ11"]),
    ("VKOSPI",  ["VKOSPI", "변동성지수"]),
    ("VIX",     ["VIX", "^VIX"]),
    ("USD/KRW", ["USDKRW", "USD/KRW", "달러원", "원달러"]),
    ("S&P 500", ["S&P500", "^GSPC", "SP500"]),
]


def _resolve(data: dict, names: list[str]) -> str | None:
    for n in names:
        if n in data:
            return n
    return None


@router.get("/context")
def market_context(user: User = Depends(get_current_user)):
    # 지수·VIX·환율 ~6종목의 Close만 필요 — 후보 키만 부분집합 로드(전 유니버스 빌드 회피).
    # 지표 계산 불요(raw OHLCV로 충분). 없는 후보는 load_dataset_for가 조용히 skip.
    cands = [n for _, names in _MARKET_SYMBOLS for n in names]
    data = qc.load_dataset_for(cands, with_indicators=False)
    indicators = []
    for label, cands in _MARKET_SYMBOLS:
        key = _resolve(data, cands)
        if key is None:
            indicators.append({"label": label, "available": False})
            continue
        df = data[key]
        if "Close" not in df.columns or len(df) < 2:
            indicators.append({"label": label, "available": False})
            continue
        cur = float(df["Close"].iloc[-1])
        prev = float(df["Close"].iloc[-2])
        chg_pct = (cur - prev) / prev * 100 if prev else 0.0
        as_of = str(df.index[-1].date()) if hasattr(df.index[-1], "date") else None
        indicators.append({
            "label": label, "available": True,
            "value": round(cur, 2),
            "change_pct": round(chg_pct, 2),
            "as_of": as_of,
        })
    return {
        "indicators": indicators,
        "session": _session_now(),
    }


_RANGE_DAYS = {
    "1m": 30, "3m": 95, "6m": 190, "12m": 380, "1y": 380,
    "3y": 1120, "5y": 1850, "10y": 3700, "15y": 5550,
    "20y": 7400, "25y": 9250, "30y": 11100,
}


@lru_cache(maxsize=2)
def _all_listings_cached(_day: str) -> list[dict]:
    """한국(KRX)+미국(NASDAQ/NYSE/AMEX) 전종목 목록 — fdr on-demand, 일 1회 캐시.

    _day(오늘 날짜)를 키로 lru_cache → 같은 날은 1회만 fetch(수천~만 종목).
    개별 시장 실패는 skip(부분 제공). 서버 dataset 미의존.
    """
    import FinanceDataReader as fdr
    out: list[dict] = []
    seen: set[str] = set()

    def _add(sym, name, market):
        s = str(sym).strip().upper()
        n = str(name).strip()
        if s and n and s not in seen:
            seen.add(s)
            out.append({"symbol": s, "name": n, "market": market})

    try:
        kr = fdr.StockListing("KRX")
        mk = kr["Market"] if "Market" in kr.columns else [""] * len(kr)
        for code, name, market in zip(kr["Code"], kr["Name"], mk):
            _add(code, name, market or "KRX")
    except Exception as e:  # 외부 소스 한계 — 부분 제공
        _log.warning("KRX listing 실패: %s", e)

    for m in ("NASDAQ", "NYSE", "AMEX"):
        try:
            us = fdr.StockListing(m)
            for sym, name in zip(us["Symbol"], us["Name"]):
                _add(sym, name, m)
        except Exception as e:
            _log.warning("%s listing 실패: %s", m, e)

    return out


@router.get("/listings")
def market_listings(user: User = Depends(get_current_user)):
    """전종목(한국+미국) 검색·드롭다운용 목록. {symbol, name, market}."""
    from datetime import date
    return {"listings": _all_listings_cached(date.today().isoformat())}


# 지표 카탈로그 — 드롭다운용. pane: price(주가 그래프 오버레이) / sub(별도 패널).
# fields: 한 지표가 여러 라인일 때 series의 키들.
_INDICATORS = [
    {"key": "ma5", "label": "이동평균 5", "pane": "price"},
    {"key": "ma20", "label": "이동평균 20", "pane": "price"},
    {"key": "ma60", "label": "이동평균 60", "pane": "price"},
    {"key": "ma120", "label": "이동평균 120", "pane": "price"},
    {"key": "bb", "label": "볼린저밴드(20,2σ)", "pane": "price",
     "fields": ["bb_upper", "bb_mid", "bb_lower"]},
    {"key": "rsi_14", "label": "RSI(14)", "pane": "sub"},
    {"key": "macd", "label": "MACD(12,26,9)", "pane": "sub",
     "fields": ["macd", "macd_signal", "macd_hist"]},
    {"key": "stoch", "label": "스토캐스틱(14,3)", "pane": "sub",
     "fields": ["stoch_k", "stoch_d"]},
    {"key": "volume", "label": "거래량", "pane": "sub"},
    {"key": "atr_14", "label": "ATR(14)", "pane": "sub"},
    {"key": "obv", "label": "OBV", "pane": "sub"},
    {"key": "vol_20d", "label": "변동성(20일)", "pane": "sub"},
]


def _benchmark_for(market: str, is_kr: bool) -> tuple[str, str]:
    """종목 시장 → 추종 벤치마크 지수(FDR 심볼, 표시명).

    KOSPI/KONEX→코스피(^KS11), KOSDAQ→코스닥(^KQ11),
    NASDAQ→나스닥(^IXIC), NYSE/AMEX→S&P500(^GSPC).
    """
    m = (market or "").upper()
    if "KOSDAQ" in m:
        return "^KQ11", "코스닥"
    if "KOSPI" in m or "KONEX" in m:
        return "^KS11", "코스피"
    if "NASDAQ" in m:
        return "^IXIC", "나스닥"
    if m in ("NYSE", "AMEX"):
        return "^GSPC", "S&P500"
    return ("^KS11", "코스피") if is_kr else ("^GSPC", "S&P500")


@router.get("/symbol/{symbol}")
def symbol_detail(symbol: str, range: str = "1y",
                  user: User = Depends(get_current_user)):
    """개별 종목 on-demand 조회 — 가격·차트·지표(RSI/이동평균). 한국+미국 상장.

    서버 dataset(parquet)에 의존하지 않고 FinanceDataReader로 실시간 fetch한다
    (light mode·무료티어에서도 동작). 200일 이동평균 워밍업을 위해 표시 구간보다
    넉넉히 받은 뒤 표시 구간만 잘라 반환.
    """
    import numpy as np
    import pandas as pd
    import FinanceDataReader as fdr
    from quant_core.indicators import compute_all

    sym = symbol.strip().upper()
    if not sym:
        raise HTTPException(status_code=400, detail="종목 코드가 필요합니다.")
    span = _RANGE_DAYS.get(range, 400)
    fetch_start = (datetime.now().date() - timedelta(days=span + 320)).isoformat()
    try:
        raw = fdr.DataReader(sym, fetch_start)
    except Exception as e:  # 외부 데이터 소스 한계 — 호출자에 명확히 전달
        raise HTTPException(status_code=502, detail=f"가격 데이터 조회 실패: {e}")
    if raw is None or raw.empty or "Close" not in raw.columns:
        raise HTTPException(status_code=404,
                            detail=f"'{symbol}' 가격 데이터를 찾을 수 없습니다.")

    ind = compute_all(raw.copy())     # rsi_14·atr_14·realized_vol 등 기본 지표
    c = ind["Close"]
    # 이동평균
    for w in (5, 20, 60, 120):
        ind[f"ma{w}"] = c.rolling(w).mean()
    # 볼린저밴드 (20, 2σ)
    m20, s20 = c.rolling(20).mean(), c.rolling(20).std()
    ind["bb_upper"], ind["bb_mid"], ind["bb_lower"] = m20 + 2 * s20, m20, m20 - 2 * s20
    # MACD (12,26,9)
    e12 = c.ewm(span=12, adjust=False).mean()
    e26 = c.ewm(span=26, adjust=False).mean()
    ind["macd"] = e12 - e26
    ind["macd_signal"] = ind["macd"].ewm(span=9, adjust=False).mean()
    ind["macd_hist"] = ind["macd"] - ind["macd_signal"]
    # 스토캐스틱 (14,3)
    low14, high14 = ind["Low"].rolling(14).min(), ind["High"].rolling(14).max()
    rng14 = (high14 - low14).replace(0, np.nan)
    ind["stoch_k"] = (c - low14) / rng14 * 100
    ind["stoch_d"] = ind["stoch_k"].rolling(3).mean()
    # OBV
    ind["obv"] = (np.sign(c.diff().fillna(0)) * ind["Volume"]).fillna(0).cumsum()
    # 전일대비 % (급등락 마커용)
    ind["chg_pct"] = c.pct_change() * 100
    show = ind.tail(span)

    def _n(v, nd=2):
        return None if v is None or pd.isna(v) else round(float(v), nd)

    series = []
    for idx, r in show.iterrows():
        d = idx.date().isoformat() if hasattr(idx, "date") else str(idx)
        series.append({
            "date": d,
            "open": _n(r.get("Open")), "high": _n(r.get("High")),
            "low": _n(r.get("Low")), "close": _n(r.get("Close")),
            "volume": _n(r.get("Volume"), 0),
            "chg_pct": _n(r.get("chg_pct")),
            "ma5": _n(r.get("ma5")), "ma20": _n(r.get("ma20")),
            "ma60": _n(r.get("ma60")), "ma120": _n(r.get("ma120")),
            "bb_upper": _n(r.get("bb_upper")), "bb_mid": _n(r.get("bb_mid")),
            "bb_lower": _n(r.get("bb_lower")),
            "rsi_14": _n(r.get("rsi_14"), 1),
            "macd": _n(r.get("macd"), 3), "macd_signal": _n(r.get("macd_signal"), 3),
            "macd_hist": _n(r.get("macd_hist"), 3),
            "stoch_k": _n(r.get("stoch_k"), 1), "stoch_d": _n(r.get("stoch_d"), 1),
            "atr_14": _n(r.get("atr_14")), "obv": _n(r.get("obv"), 0),
            "vol_20d": _n(r.get("realized_vol_20d")),
        })
    if not series:
        raise HTTPException(status_code=404, detail="표시할 데이터가 부족합니다.")

    last = show.iloc[-1]
    prev = show.iloc[-2] if len(show) >= 2 else last
    close = float(last["Close"]); pclose = float(prev["Close"])
    is_kr = symbol.strip().isdigit()

    # 베타 — 종목이 추종하는 벤치마크(코스피/코스닥/나스닥/S&P500) 대비 민감도.
    # 시장은 전종목 listing(캐시)에서 판별, 벤치마크 지수를 fetch해 회귀.
    mkt = ""
    try:
        for it in _all_listings_cached(datetime.now().date().isoformat()):
            if it["symbol"] == sym:
                mkt = it["market"]
                break
    except Exception:
        pass
    bench_sym, bench_name = _benchmark_for(mkt, is_kr)
    beta = None
    try:
        braw = fdr.DataReader(bench_sym, fetch_start)
        if braw is not None and not braw.empty and "Close" in braw.columns:
            sret = show["Close"].pct_change().dropna()
            bret = braw["Close"].pct_change().dropna()
            common = sret.index.intersection(bret.index)
            if len(common) > 20:
                sv, bv = sret.reindex(common), bret.reindex(common)
                if bv.var() and bv.var() > 0:
                    beta = round(float(np.cov(sv, bv)[0, 1] / bv.var()), 2)
    except Exception as e:  # 벤치마크 fetch 실패 — 베타 생략
        _log.warning("베타 계산 실패 %s vs %s: %s", sym, bench_sym, e)

    return {
        "symbol": sym,
        "currency": "KRW" if is_kr else "USD",
        "range": range,
        "last": {
            "date": series[-1]["date"],
            "close": _n(close),
            "change_pct": _n((close - pclose) / pclose * 100 if pclose else 0.0),
            "rsi_14": _n(last.get("rsi_14"), 1),
            "volume": _n(last.get("Volume"), 0),
            "ma20": _n(last.get("ma20")), "ma60": _n(last.get("ma60")),
            "macd": _n(last.get("macd"), 3), "stoch_k": _n(last.get("stoch_k"), 1),
            "atr_14": _n(last.get("atr_14")), "vol_20d": _n(last.get("realized_vol_20d")),
            "beta": beta, "benchmark": bench_name,
            "high_52w": _n(show["High"].tail(252).max()),
            "low_52w": _n(show["Low"].tail(252).min()),
        },
        "indicators": _INDICATORS,
        "series": series,
    }


def _session_now() -> dict:
    """한국 정규장 기준 현재 세션 표시. 서버 tz와 무관하게 KST로 계산."""
    kst = datetime.now(ZoneInfo("Asia/Seoul"))
    hm = kst.hour * 60 + kst.minute
    dow = kst.weekday()    # 0=월 ... 6=일
    if dow >= 5:
        phase = "휴일"
    elif hm < 8 * 60:
        phase = "장 시작 전"
    elif hm < 9 * 60:
        phase = "동시호가 (장초)"
    elif hm < 15 * 60 + 20:
        phase = "정규장"
    elif hm < 15 * 60 + 30:
        phase = "동시호가 (마감)"
    else:
        phase = "장 종료"
    return {
        "phase": phase,
        "kst_now": kst.replace(microsecond=0).isoformat(),
    }

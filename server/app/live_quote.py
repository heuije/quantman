"""통합 준실시간 시세 — 네이버 공개 폴링 API(키 불필요). 챗 사이드카 전용.

EOD 데이터엔진(parquet) **밖**에서 거래자산의 현재가·등락률을 조회한다(골든 무누출). 동일
polling 패밀리(domestic/stock·domestic/index·worldstock/stock·worldstock/index)는 응답
필드가 같아(closePriceRaw·fluctuationsRatioRaw) **단일 파서**로 처리된다. market_snapshot은
'시장이 왜'(breadth) 해석용 거시 맥락 — KR·US 지수 + VIX. 비공식 스크래핑이라 best-effort
(실패=빈 dict). 폴링 권고(70~90초)에 맞춰 90초 버킷 캐시.

확장 지점(후속): FX(api.stock.naver.com/marketindex — 응답 포맷 다름)·US 개별주(worldstock/
stock + 티커→Reuters 코드 매핑)·크립토(Binance ticker). 모두 _poll 또는 별도 파서로 추가.
"""
from __future__ import annotations

import re
import time
from functools import lru_cache

import requests

_UA = {"User-Agent": "Mozilla/5.0"}
_BASE = "https://polling.finance.naver.com/api/realtime/"
_AC_URL = "https://ac.stock.naver.com/ac"                       # 티커→reutersCode resolve
_FX_URL = "https://api.stock.naver.com/marketindex/exchange/"   # 환율(다른 포맷)
_BINANCE_URL = "https://api.binance.com/api/v3/ticker/24hr"     # 크립토
_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/"   # 해외 선물·상품(~10분 지연)

# 시장 스냅샷 구성(거래 지수만 — 직접 코드, 티커 매핑 불요). KR=domestic/index, US/VIX=worldstock/index.
_KR_INDEX = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}
_US_INDEX = {".IXIC": "나스닥", ".INX": "S&P500", ".VIX": "VIX"}

# 심볼 분류 — KR 종목(6자리)·US 티커(영문)·크립토·해외선물상품(데이터엔진 자산명 기준).
_KR_CODE = re.compile(r"^\d{6}$")
_US_TICKER = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")
_CRYPTO_MAP = {"비트코인": "BTCUSDT"}
# 데이터엔진 자산명 → Yahoo 심볼(해외 선물·상품·지수선물). 엔진 YFINANCE_SYMBOLS와 동일 소스(단일소스 유지).
_YAHOO_MAP = {
    "원유선물": "CL=F", "천연가스선물": "NG=F", "금선물": "GC=F",
    "나스닥선물": "NQ=F", "은선물(COMEX)": "SI=F", "비트코인선물": "BTC=F", "S&P500": "^GSPC",
}
# 국내 지수선물 — 네이버 domestic/index(폴링 패밀리) 코드. 엔진명 "코스피200선물"(CSV+KIS 실선물) 기준.
_KR_FUTURES_MAP = {"코스피200선물": "FUT"}


def _bucket() -> str:
    """90초 버킷 — lru_cache 키에 넣어 주기적 재조회(네이버 폴링 권고 준수)."""
    return f"t{int(time.time() // 90)}"


def _num(s):
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=64)
def _poll(category: str, codes_csv: str, bucket: str) -> dict:
    """polling 패밀리 공통 파서 — {code: {price, chg, change}}. 실패 시 {}(graceful).

    category=domestic/stock·domestic/index·worldstock/stock·worldstock/index 모두 동일 필드.
    bucket은 캐시 신선도 키(값 자체는 미사용).
    """
    out: dict = {}
    try:
        r = requests.get(_BASE + category + "/" + codes_csv, headers=_UA, timeout=8)
        for d in r.json().get("datas", []):
            code = str(d.get("itemCode") or d.get("reutersCode") or "")
            price = _num(d.get("closePriceRaw") or d.get("closePrice"))
            if code and price is not None:
                out[code] = {
                    "price": price,
                    "chg": _num(d.get("fluctuationsRatioRaw") or d.get("fluctuationsRatio")),
                    "change": _num(d.get("compareToPreviousClosePriceRaw")),
                }
    except Exception:   # noqa: BLE001 — 비공식 스크래핑·네트워크 실패는 부가정보라 graceful
        pass
    return out


def market_snapshot() -> dict:
    """시장 거시 맥락 — KR·US 지수 + VIX(준실시간). '시장이 왜' 해석용. {라벨: {price, chg, change}}.

    배치 2콜(domestic/index, worldstock/index)·90초 캐시. 실패한 축은 빠지고 나머지는 반환.
    """
    b = _bucket()
    kr = _poll("domestic/index", ",".join(_KR_INDEX), b)
    us = _poll("worldstock/index", ",".join(_US_INDEX), b)
    out: dict = {}
    for code, label in {**_KR_INDEX, **_US_INDEX}.items():
        q = kr.get(code) or us.get(code)
        if q:
            out[label] = q
    fx = _fx("FX_USDKRW", b)        # 원/달러 환율(거시 맥락)
    if fx:
        out["원/달러"] = fx
    return out


@lru_cache(maxsize=16)
def _fx(pair_code: str, bucket: str) -> dict | None:
    """환율 — naver marketindex(polling 패밀리와 포맷 다름: exchangeInfo.calcPrice). {price, chg, change}."""
    try:
        info = (requests.get(_FX_URL + pair_code, headers=_UA, timeout=8).json() or {}).get("exchangeInfo") or {}
        price = _num(info.get("calcPrice") or info.get("closePrice"))
        if price is not None:
            return {"price": price, "chg": _num(info.get("fluctuationsRatio")),
                    "change": _num(info.get("fluctuations"))}
    except Exception:   # noqa: BLE001
        pass
    return None


@lru_cache(maxsize=512)
def _resolve_us(ticker: str) -> str | None:
    """US 티커 → 네이버 reutersCode(예: AAPL→AAPL.O). 자동완성 resolve(드물게 변경 → 캐시)."""
    try:
        r = requests.get(_AC_URL, params={"q": ticker, "target": "stock", "st": "111"},
                         headers=_UA, timeout=8)
        for it in r.json().get("items", []):
            if str(it.get("code", "")).upper() == ticker.upper() and it.get("reutersCode"):
                return str(it["reutersCode"])
    except Exception:   # noqa: BLE001
        pass
    return None


def world_stock_quotes(tickers: tuple) -> dict:
    """US 개별주 준실시간 — 티커→reutersCode resolve 후 worldstock 배치. {ticker: {price,chg,change}}."""
    rmap = {}    # reutersCode → 원래 ticker
    for t in tickers:
        rc = _resolve_us(t)
        if rc:
            rmap[rc] = t
    if not rmap:
        return {}
    polled = _poll("worldstock/stock", ",".join(rmap), _bucket())   # {reutersCode: {...}}
    return {rmap[rc]: q for rc, q in polled.items() if rc in rmap}


@lru_cache(maxsize=32)
def _binance(symbol: str, bucket: str) -> dict | None:
    try:
        j = requests.get(_BINANCE_URL, params={"symbol": symbol}, headers=_UA, timeout=8).json()
        price = _num(j.get("lastPrice"))
        if price is not None:
            return {"price": price, "chg": _num(j.get("priceChangePercent")),
                    "change": _num(j.get("priceChange"))}
    except Exception:   # noqa: BLE001
        pass
    return None


def crypto_quotes(names: tuple) -> dict:
    """크립토 준실시간(Binance 24hr) — 데이터엔진 자산명 기준. {name: {price,chg,change}}."""
    b = _bucket()
    out = {}
    for name in names:
        sym = _CRYPTO_MAP.get(name)
        q = _binance(sym, b) if sym else None
        if q:
            out[name] = q
    return out


@lru_cache(maxsize=64)
def _yahoo(sym: str, bucket: str) -> dict | None:
    """Yahoo v8 chart(무인증) — {price, chg, change}. 거래소 정책상 ~10분 지연. 실패=None.

    chg는 chartPreviousClose(전 거래일 종가) 대비. range=1d라 당일 1일 변동률.
    """
    try:
        m = requests.get(f"{_YAHOO_URL}{sym}", params={"interval": "1d", "range": "1d"},
                         headers=_UA, timeout=8).json()["chart"]["result"][0]["meta"]
        price, prev = _num(m.get("regularMarketPrice")), _num(m.get("chartPreviousClose"))
        if price is not None:
            return {"price": price,
                    "chg": round((price / prev - 1) * 100, 2) if prev else None,
                    "change": round(price - prev, 2) if prev else None}
    except Exception:   # noqa: BLE001
        pass
    return None


def commodity_quotes(names: tuple) -> dict:
    """해외 선물·상품 준실시간(Yahoo·~10분 지연) — 데이터엔진 자산명 기준. {name: {price,chg,change}}."""
    b = _bucket()
    out = {}
    for name in names:
        sym = _YAHOO_MAP.get(name)
        q = _yahoo(sym, b) if sym else None
        if q:
            out[name] = q
    return out


def quotes_for(symbols: tuple) -> dict:
    """대표 종목 준실시간 시세 — KR(6자리)·US(티커)·크립토·해외선물상품 자동 분류·라우팅. {symbol: {price,chg,change}}.

    네이버 폴링(KR domestic/stock · US worldstock/stock) + Binance(크립토) + Yahoo(해외 선물·상품·지수선물,
    ~10분 지연). 모두 엔진 의존 소스(단일소스 유지)·골든 무누출·best-effort.
    """
    syms = [str(s) for s in symbols if s]
    kr = tuple(s for s in syms if _KR_CODE.match(s))
    kr_fut = tuple(s for s in syms if s in _KR_FUTURES_MAP)
    crypto = tuple(s for s in syms if s in _CRYPTO_MAP)
    commodity = tuple(s for s in syms if s in _YAHOO_MAP)
    us = tuple(s for s in syms if s not in crypto and s not in commodity and s not in kr_fut
               and not _KR_CODE.match(s) and _US_TICKER.match(s))
    out: dict = {}
    if kr:
        out.update(_poll("domestic/stock", ",".join(kr), _bucket()))
    if kr_fut:
        polled = _poll("domestic/index", ",".join(_KR_FUTURES_MAP[s] for s in kr_fut), _bucket())
        out.update({s: polled[_KR_FUTURES_MAP[s]] for s in kr_fut if _KR_FUTURES_MAP[s] in polled})
    if us:
        out.update(world_stock_quotes(us))
    if crypto:
        out.update(crypto_quotes(crypto))
    if commodity:
        out.update(commodity_quotes(commodity))
    return out

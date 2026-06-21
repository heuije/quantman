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

import time
from functools import lru_cache

import requests

_UA = {"User-Agent": "Mozilla/5.0"}
_BASE = "https://polling.finance.naver.com/api/realtime/"

# 시장 스냅샷 구성(거래 지수만 — 직접 코드, 티커 매핑 불요). KR=domestic/index, US/VIX=worldstock/index.
_KR_INDEX = {"KOSPI": "코스피", "KOSDAQ": "코스닥"}
_US_INDEX = {".IXIC": "나스닥", ".INX": "S&P500", ".VIX": "VIX"}


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
    return out

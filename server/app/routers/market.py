"""시장 컨텍스트 — 자동매매 페이지 상단에 띄울 환경 정보.

지수·VIX·환율 최근값 + 전일대비 %, KRX 거래일 캘린더 (간단 휴장 추정).
모두 data_cache의 dataset에서 가져온다.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends

import quant_core as qc

from ..deps import get_current_user
from ..models import User

router = APIRouter(prefix="/market", tags=["market"])


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

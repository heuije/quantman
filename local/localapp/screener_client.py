"""서버 /screener API 호출 — 자동선정 전략의 매수 후보 조회.

전략의 trade_symbol이 'screener:<preset_key>' 형식이면 trader가 이 모듈로
서버에 매칭 종목 리스트를 요청한다. 서버 캐시(KRX 16:30 KST 갱신)에서 즉시 응답.

보안: 사용자 토큰을 헤더로 전송하지만 데이터 자체는 공용 시세성. 응답에 계좌·
주문 정보 없음.
"""

from __future__ import annotations

import logging

import requests

from .config import PLATFORM_URL
from .secrets_store import load_device_token

log = logging.getLogger("localapp.screener_client")


SCREENER_PREFIX = "screener:"


def parse_screener_key(trade_symbol: str) -> str | None:
    """전략의 trade_symbol이 자동선정 prefix면 preset key 반환, 아니면 None."""
    if trade_symbol and trade_symbol.startswith(SCREENER_PREFIX):
        return trade_symbol[len(SCREENER_PREFIX):]
    return None


def fetch_preset_matches(preset_key: str, *, timeout: int = 15) -> list[dict]:
    """프리셋 매칭 결과를 서버에서 가져온다.

    반환: [{symbol, name, market, close, pct_change_1d, market_cap, ...}, ...]
    """
    token = load_device_token()
    if not token:
        raise RuntimeError("기기 페어링이 필요합니다.")

    url = f"{PLATFORM_URL.rstrip('/')}/screener/preset/{preset_key}/run"
    r = requests.post(url, headers={"Authorization": f"Bearer {token}"},
                       timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data.get("matches", [])

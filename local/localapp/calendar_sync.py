"""Q2+Q8 — 서버에서 시장 캘린더 일일 pull.

서버 일일 03:00 KST cron으로 exchange_calendars 최신 데이터 재빌드.
로컬앱은 매일 04:00 KST(서버 cron 이후 안전 마진)에 /calendars/{market}로
pull → ~/.quantman/calendars/에 저장 → market_calendar._load 캐시 무효화.

실패해도 quant_core/calendars/ 번들 fallback이 동작하므로 sync 실패가
시스템을 막지 않는다. 단순 try/except + 다음 cron 재시도.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from quant_core.market_calendar import USER_CACHE_DIR

from .config import PLATFORM_URL
from .secrets_store import load_device_token

log = logging.getLogger("localapp.calendar_sync")


def _headers() -> dict:
    token = load_device_token()
    if not token:
        raise RuntimeError("기기 페어링이 필요합니다.")
    return {"Authorization": f"Bearer {token}"}


def pull_one(market: str) -> bool:
    """1개 시장 캘린더 pull. 성공 시 사용자 캐시 저장 + _load 캐시 무효화.

    실패 시 False 반환 — 기존 사용자 캐시 또는 번들이 그대로 fallback.
    """
    try:
        r = requests.get(f"{PLATFORM_URL}/calendars/{market}",
                          headers=_headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("[%s] 캘린더 sync 실패 (기존 캐시/번들 fallback): %s",
                     market, e)
        return False

    try:
        USER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = USER_CACHE_DIR / f"{market.lower()}_sessions.json"
        # Atomic write: tmp + replace
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        import os
        os.replace(tmp, path)
    except Exception as e:
        log.warning("[%s] 캘린더 디스크 저장 실패: %s", market, e)
        return False

    # 캐시 무효화 — 다음 _load 호출이 새 파일을 읽도록
    try:
        from quant_core import market_calendar
        market_calendar._load.cache_clear()
    except Exception:
        pass

    n_sessions = len(data.get("sessions", {}))
    log.info("[%s] 캘린더 sync 성공: %d 세션, %s",
              market, n_sessions, data.get("range", []))
    return True


def pull_all() -> dict:
    """KR/US 둘 다 pull. 부분 실패 허용."""
    return {m: pull_one(m) for m in ("KR", "US")}

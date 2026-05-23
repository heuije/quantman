"""Q2+Q8 — 시장 세션 캘린더 배포 라우터.

로컬앱이 일일 1회 pull. 기기 토큰 인증.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from .. import calendar_cache
from ..deps import get_current_device
from ..models import Device

_log = logging.getLogger("app.calendars")

router = APIRouter(prefix="/calendars", tags=["calendars"])


@router.get("/{market}")
def get_calendar(market: str,
                  device: Device = Depends(get_current_device)) -> dict:
    """시장 세션 dict 반환. market은 'KR' 또는 'US'.

    반환 형식: quant_core/calendars/{m}_sessions.json과 동일.
      {market, calendar, tz_local, generated_at, range, sessions}
    """
    m = market.upper()
    if m not in ("KR", "US"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                             "market은 KR 또는 US")
    data = calendar_cache.get(m)
    if data is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                             "캘린더가 아직 빌드되지 않았습니다. "
                             "잠시 후 재시도하세요.")
    return data

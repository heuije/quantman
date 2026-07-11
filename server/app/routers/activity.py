"""웹 유저 활동 계측 — 화면·종목 조회 이벤트 적재(운영자 대시보드 소스).

프론트 Layout이 라우트 변경 시 fire-and-forget으로 호출한다(로그인 유저만).
안전정보만(화면 경로·종목코드) — 자격증명·계좌번호 없음(보안 불변식).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import Session

from ..db import get_session
from ..deps import get_current_user
from ..models import ActivityEvent, User

router = APIRouter(prefix="/activity", tags=["activity"])

_MAX_PATH = 200
_MAX_SYMBOL = 40


class ActivityIn(BaseModel):
    path: str
    symbol: str | None = None


@router.post("")
def log_activity(body: ActivityIn, user: User = Depends(get_current_user),
                 session: Session = Depends(get_session)):
    """화면 전환·종목 조회 1건 적재. 프론트는 결과를 기다리지 않는다(부가 텔레메트리)."""
    path = (body.path or "").strip()[:_MAX_PATH]
    if not path:
        return {"ok": False}
    symbol = ((body.symbol or "").strip().upper() or None)
    if symbol:
        symbol = symbol[:_MAX_SYMBOL]
    session.add(ActivityEvent(user_id=user.id, path=path, symbol=symbol))
    session.commit()
    return {"ok": True}

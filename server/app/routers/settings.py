"""사용자별 모니터링 설정 — 알림 webhook URL 등."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlmodel import Session

from ..db import get_session
from ..deps import get_current_user
from ..models import User, UserSettings
from ..schemas import UserSettingsIO

router = APIRouter(prefix="/settings", tags=["settings"])


def _get_or_create(session: Session, user_id: int) -> UserSettings:
    s = session.get(UserSettings, user_id)
    if s is None:
        s = UserSettings(user_id=user_id)
        session.add(s)
        session.commit()
        session.refresh(s)
    return s


@router.get("", response_model=UserSettingsIO)
def get_settings(user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    s = _get_or_create(session, user.id)
    return UserSettingsIO(
        alert_webhook_url=s.alert_webhook_url,
        alert_on_killswitch=s.alert_on_killswitch,
        alert_on_daily_loss_pct=s.alert_on_daily_loss_pct,
        alert_on_unfilled_count=s.alert_on_unfilled_count,
    )


@router.put("", response_model=UserSettingsIO)
def put_settings(body: UserSettingsIO,
                  user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    s = _get_or_create(session, user.id)
    s.alert_webhook_url = body.alert_webhook_url
    s.alert_on_killswitch = body.alert_on_killswitch
    s.alert_on_daily_loss_pct = body.alert_on_daily_loss_pct
    s.alert_on_unfilled_count = body.alert_on_unfilled_count
    s.updated_at = datetime.now(timezone.utc)
    session.add(s)
    session.commit()
    return body

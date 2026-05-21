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
        kill_switch_daily_loss_pct=s.kill_switch_daily_loss_pct,
        max_drawdown_pct=s.max_drawdown_pct,
        preview_missing_alert_threshold=s.preview_missing_alert_threshold,
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
    # Phase 38.7/38.10 — null이면 기존 값 유지 안 하고 null 그대로 저장 (default로 fallback)
    # 사용자 입력 1~10/1~50% 범위 검증
    if body.kill_switch_daily_loss_pct is not None:
        if not (0.5 <= body.kill_switch_daily_loss_pct <= 20.0):
            from fastapi import HTTPException, status
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "kill_switch_daily_loss_pct는 0.5~20.0 범위여야 합니다.")
    if body.max_drawdown_pct is not None:
        if not (1.0 <= body.max_drawdown_pct <= 80.0):
            from fastapi import HTTPException, status
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "max_drawdown_pct는 1.0~80.0 범위여야 합니다.")
    s.kill_switch_daily_loss_pct = body.kill_switch_daily_loss_pct
    s.max_drawdown_pct = body.max_drawdown_pct
    s.preview_missing_alert_threshold = max(1, int(body.preview_missing_alert_threshold))
    s.updated_at = datetime.now(timezone.utc)
    session.add(s)
    session.commit()
    return body

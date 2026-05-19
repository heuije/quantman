"""인증 의존성 — 웹 사용자(JWT) / 로컬앱 기기(기기 토큰)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlmodel import Session, select

from .db import get_session
from .models import Device, User
from .security import decode_access_token, hash_token


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "인증 토큰이 필요합니다.")
    return authorization[7:]


def get_current_user(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> User:
    """웹 사용자 — JWT 인증."""
    user_id = decode_access_token(_bearer(authorization))
    if user_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "토큰이 유효하지 않습니다.")
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "사용자를 찾을 수 없습니다.")
    return user


def get_current_device(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Device:
    """로컬앱 — 기기 토큰 인증. 동기화 엔드포인트 전용."""
    token = _bearer(authorization)
    device = session.exec(
        select(Device).where(Device.token_hash == hash_token(token))
    ).first()
    if device is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "기기 토큰이 유효하지 않습니다.")
    device.last_seen_at = datetime.now(timezone.utc)
    session.add(device)
    session.commit()
    session.refresh(device)
    return device

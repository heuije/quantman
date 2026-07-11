"""인증 의존성 — 웹 사용자(JWT) / 로컬앱 기기(기기 토큰)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlmodel import Session, select

from .config import settings
from .db import get_session
from .models import Device, User
from .security import decode_access_token, hash_token

# S-06 — last_seen_at 매 요청 commit이 Postgres write 증폭을 만든다.
# 로컬앱 SSE 폴링·sync push가 분당 수회씩 들어오면 device row가 같은 빈도로
# 업데이트 → 디스크 IO·WAL 증가. 분 단위 정밀도면 운영에 충분하므로
# 메모리 캐시로 60s throttle. 프로세스 재시작 시 캐시가 비고, 첫 요청이
# 즉시 갱신하므로 stale 손실은 최대 60s.
_LAST_SEEN_THROTTLE_SEC = 60.0
_last_seen_cache: dict[int, float] = {}
_last_seen_lock = threading.Lock()


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


def is_admin(user: User) -> bool:
    """운영자(서비스 운영진) 여부 — ADMIN_EMAILS 허용목록 대조. 관리자 식별 SSOT."""
    return (user.email or "").strip().lower() in settings.ADMIN_EMAILS


def require_admin(user: User = Depends(get_current_user)) -> User:
    """운영자 전용 게이트 — 대시보드·모더레이션 등 운영 엔드포인트에 붙인다."""
    if not is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "운영자 전용입니다.")
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
    # S-06 — 60s throttle. 최근 갱신한 기기는 commit skip하고 메모리만 갱신.
    now_epoch = time.monotonic()
    with _last_seen_lock:
        last_epoch = _last_seen_cache.get(device.id, 0.0)
        should_commit = (now_epoch - last_epoch) >= _LAST_SEEN_THROTTLE_SEC
        if should_commit:
            _last_seen_cache[device.id] = now_epoch
    if should_commit:
        device.last_seen_at = datetime.now(timezone.utc)
        session.add(device)
        session.commit()
        session.refresh(device)
    return device

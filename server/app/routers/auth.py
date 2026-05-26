"""인증 라우터 — 회원가입·로그인 + 로컬앱 기기 페어링(OAuth 기기 그랜트)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..deps import get_current_device, get_current_user
from ..models import Device, PairingRequest, User
from ..schemas import (DeviceApproveIn, DeviceOut, DeviceStartIn, DeviceStartOut,
                       DeviceTokenIn, DeviceTokenOut, GoogleLoginIn, LoginIn,
                       SignupIn, TokenOut, UserOut)
from ..security import (create_access_token, hash_password, hash_token,
                        new_device_code, new_device_token, new_user_code,
                        verify_password)

router = APIRouter(prefix="/auth", tags=["auth"])


# ── 웹 계정 ────────────────────────────────────────────────────────────────────

@router.post("/signup", response_model=TokenOut)
def signup(body: SignupIn, session: Session = Depends(get_session)):
    exists = session.exec(select(User).where(User.email == body.email)).first()
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 가입된 이메일입니다.")
    user = User(email=body.email, password_hash=hash_password(body.password))
    session.add(user)
    session.commit()
    session.refresh(user)
    return TokenOut(access_token=create_access_token(user.id))


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.email == body.email)).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "이메일 또는 비밀번호가 올바르지 않습니다.")
    return TokenOut(access_token=create_access_token(user.id))


@router.post("/google", response_model=TokenOut)
def google_login(body: GoogleLoginIn, session: Session = Depends(get_session)):
    """Google ID 토큰을 검증하고 이메일로 사용자를 찾거나 생성한다.

    비밀번호 가입자가 같은 이메일로 Google 로그인하면 자동으로 연동된다.
    """
    if not settings.GOOGLE_CLIENT_ID:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Google 로그인이 설정되지 않았습니다.")
    # 지연 import — Google 로그인 미사용 환경에서 의존성 부담을 줄임
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    try:
        info = google_id_token.verify_oauth2_token(
            body.credential, google_requests.Request(), settings.GOOGLE_CLIENT_ID
        )
    except Exception:  # noqa: BLE001  — 서명·만료·audience 불일치 등 모두 인증 실패로
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google 인증에 실패했습니다.")

    email = info.get("email")
    sub = info.get("sub")
    if not email or not info.get("email_verified") or not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "이메일이 확인되지 않은 Google 계정입니다.")

    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        user = User(email=email, password_hash=None, google_sub=sub)
        session.add(user)
        session.commit()
        session.refresh(user)
    elif user.google_sub is None:          # 기존 비밀번호 계정에 Google 연동
        user.google_sub = sub
        session.add(user)
        session.commit()
    return TokenOut(access_token=create_access_token(user.id))


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return UserOut(id=user.id, email=user.email, created_at=user.created_at)


@router.get("/device/me", response_model=UserOut)
def device_me(
    device: Device = Depends(get_current_device),
    session: Session = Depends(get_session),
):
    """로컬앱이 device_token으로 연결된 사용자 정보 조회 — GUI에 표시용."""
    user = session.get(User, device.user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "사용자를 찾을 수 없습니다.")
    return UserOut(id=user.id, email=user.email, created_at=user.created_at)


# ── 기기 페어링 ────────────────────────────────────────────────────────────────

@router.post("/device/start", response_model=DeviceStartOut)
def device_start(body: DeviceStartIn, session: Session = Depends(get_session)):
    """로컬앱이 호출 — 페어링 코드 발급 (인증 불필요)."""
    pr = PairingRequest(
        device_code=new_device_code(),
        user_code=new_user_code(),
        device_name=body.device_name,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.PAIRING_TTL_MIN),
    )
    session.add(pr)
    session.commit()
    session.refresh(pr)
    return DeviceStartOut(
        device_code=pr.device_code,
        user_code=pr.user_code,
        verification_uri=f"{settings.WEB_URL}/pair",
        verification_uri_complete=f"{settings.WEB_URL}/pair?code={pr.user_code}",
        expires_in=settings.PAIRING_TTL_MIN * 60,
    )


@router.post("/device/approve")
def device_approve(
    body: DeviceApproveIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """웹에서 로그인한 사용자가 user_code를 입력해 기기를 승인."""
    pr = session.exec(
        select(PairingRequest).where(PairingRequest.user_code == body.user_code.upper())
    ).first()
    if pr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "페어링 코드를 찾을 수 없습니다.")
    if pr.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_410_GONE, "페어링 코드가 만료되었습니다.")
    if pr.consumed:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 사용된 코드입니다.")
    pr.approved = True
    pr.user_id = user.id
    session.add(pr)
    session.commit()
    return {"ok": True, "device_name": pr.device_name}


@router.post("/device/token", response_model=DeviceTokenOut)
def device_token(body: DeviceTokenIn, session: Session = Depends(get_session)):
    """로컬앱이 폴링 — 승인되면 기기 토큰을 1회 발급."""
    pr = session.exec(
        select(PairingRequest).where(PairingRequest.device_code == body.device_code)
    ).first()
    if pr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "페어링 요청을 찾을 수 없습니다.")
    if pr.expires_at.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HTTPException(status.HTTP_410_GONE, "페어링 요청이 만료되었습니다.")
    if not pr.approved or pr.user_id is None:
        return DeviceTokenOut(status="pending")
    if pr.consumed:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 발급된 페어링입니다.")

    raw = new_device_token()
    device = Device(user_id=pr.user_id, name=pr.device_name, token_hash=hash_token(raw))
    session.add(device)
    pr.consumed = True
    session.add(pr)
    session.commit()
    session.refresh(device)
    return DeviceTokenOut(status="approved", device_token=raw, device_id=device.id)


@router.get("/devices", response_model=list[DeviceOut])
def list_devices(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    devices = session.exec(select(Device).where(Device.user_id == user.id)).all()
    return [DeviceOut(id=d.id, name=d.name, created_at=d.created_at,
                      last_seen_at=d.last_seen_at) for d in devices]


@router.delete("/devices/{device_id}")
def revoke_device(
    device_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    device = session.get(Device, device_id)
    if device is None or device.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "기기를 찾을 수 없습니다.")
    session.delete(device)
    session.commit()
    return {"ok": True}

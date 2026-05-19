"""인증 유틸 — 비밀번호 해시, JWT, 기기 토큰."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from .config import settings


# ── 비밀번호 ──────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# ── JWT (웹 로그인) ───────────────────────────────────────────────────────────

def create_access_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(hours=settings.ACCESS_TOKEN_HOURS),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGO)


def decode_access_token(token: str) -> int | None:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGO])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None


# ── 기기 토큰 (로컬앱 동기화) ──────────────────────────────────────────────────

def new_device_token() -> str:
    """로컬앱에 1회만 노출되는 원본 토큰."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """기기 토큰은 해시만 DB에 저장한다."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_device_code() -> str:
    return secrets.token_urlsafe(24)


def new_user_code() -> str:
    """사람이 입력하기 쉬운 짧은 코드 (예: 7K3Q-9F2A)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"

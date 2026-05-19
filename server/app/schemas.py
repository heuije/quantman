"""API 요청/응답 스키마 (DB 모델과 분리)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, EmailStr


# ── 인증 ──────────────────────────────────────────────────────────────────────

class SignupIn(BaseModel):
    email: EmailStr
    password: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class GoogleLoginIn(BaseModel):
    credential: str        # Google Identity Services가 발급한 ID 토큰(JWT)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: int
    email: str
    created_at: datetime


# ── 기기 페어링 ────────────────────────────────────────────────────────────────

class DeviceStartIn(BaseModel):
    device_name: str = "내 PC"


class DeviceStartOut(BaseModel):
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str    # user_code가 쿼리에 미리 채워진 URL
    expires_in: int


class DeviceApproveIn(BaseModel):
    user_code: str


class DeviceTokenIn(BaseModel):
    device_code: str


class DeviceTokenOut(BaseModel):
    status: str                       # "pending" | "approved"
    device_token: Optional[str] = None
    device_id: Optional[int] = None


class DeviceOut(BaseModel):
    id: int
    name: str
    created_at: datetime
    last_seen_at: Optional[datetime] = None


# ── 전략 ──────────────────────────────────────────────────────────────────────

class StrategyIn(BaseModel):
    definition: dict[str, Any]        # core quant_core.Strategy 형태
    run_mode: str = "draft"           # draft | paper | live


class StrategyOut(BaseModel):
    id: int
    name: str
    run_mode: str
    definition: dict[str, Any]
    created_at: datetime
    updated_at: datetime


# ── 백테스트 / 분석 ────────────────────────────────────────────────────────────

class BacktestIn(BaseModel):
    strategy: dict[str, Any]          # core Strategy 형태
    start: Optional[str] = None
    end: Optional[str] = None
    initial_capital: float = 10_000_000.0


class AnalysisIn(BaseModel):
    conditions: list[dict[str, Any]]
    logic: str = "AND"
    target_symbol: str
    target_indicator: str
    forward_days: int = 1
    lookback_years: Optional[int] = None


# ── 동기화 ────────────────────────────────────────────────────────────────────

class SyncPushIn(BaseModel):
    payload: dict[str, Any]           # 잔고·포지션·자산곡선·체결로그 (안전정보만)


class SyncSnapshotOut(BaseModel):
    payload: dict[str, Any]
    received_at: datetime
    device_id: int

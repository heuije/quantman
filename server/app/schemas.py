"""API 요청/응답 스키마 (DB 모델과 분리)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Optional

from pydantic import BaseModel, EmailStr, PlainSerializer


def _serialize_utc(dt: datetime) -> str:
    """naive datetime을 UTC로 간주해 offset(+00:00)을 달아 직렬화.

    근본 이유: DB(Postgres `timestamp without time zone`)는 모든 시각을 UTC(_now=
    datetime.now(timezone.utc))로 저장하지만 round-trip 시 tzinfo를 잃어 naive로
    읽힌다. naive ISO(offset 없음)를 그대로 JSON으로 내보내면 브라우저
    `new Date("...15:45:01")`가 이를 **현지시간(KST 등)으로 오해**해 시각이 어긋난다
    (KST 사용자는 정확히 -9h). 출력 datetime은 항상 UTC offset을 달아야 한다.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# 모든 출력 스키마의 datetime 필드는 이 타입으로 UTC offset 직렬화를 보장한다
# (부류 전체를 한 진입점에서 닫음 — 신규 출력 스키마도 datetime 대신 UtcDateTime 사용).
UtcDateTime = Annotated[
    datetime, PlainSerializer(_serialize_utc, return_type=str, when_used="json")
]


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
    created_at: UtcDateTime


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
    created_at: UtcDateTime
    last_seen_at: Optional[UtcDateTime] = None


# ── 전략 ──────────────────────────────────────────────────────────────────────

class StrategyIn(BaseModel):
    definition: dict[str, Any]        # ir=StrategyIR 형태 (IR 단일 체제)
    run_mode: str = "draft"           # draft | paper | live
    engine: str = "ir"                # ir(전략 연구소) 단일 — 레거시 operand 제거됨
    account_ref: Optional[str] = None   # 바인딩할 계좌 핸들 id (없으면 미바인딩·레거시)


class StrategyOut(BaseModel):
    id: int
    name: str
    run_mode: str
    engine: str = "ir"
    definition: dict[str, Any]
    created_at: UtcDateTime
    updated_at: UtcDateTime
    # Phase 59 — 적용 기간 계산용
    paper_started_at: Optional[UtcDateTime] = None
    live_started_at: Optional[UtcDateTime] = None
    # P5-2 — 바인딩된 계좌 핸들 id (NULL=미바인딩·레거시)
    account_ref: Optional[str] = None


class StrategyVersionOut(BaseModel):
    """전략 버전 이력 한 항목."""
    version_no: int
    name: str
    created_at: UtcDateTime
    created_reason: str
    definition: Optional[dict[str, Any]] = None     # list endpoint에선 omit


class StrategyStatsOut(BaseModel):
    """전략 현황 — 적용 기간, 누적 P&L 요약. /strategies/{id}/stats."""
    paper_started_at: Optional[datetime] = None
    live_started_at: Optional[datetime] = None
    days_paper: Optional[int] = None
    days_live: Optional[int] = None
    pnl_total: Optional[float] = None              # 누적 손익 (KRW)
    pnl_pct: Optional[float] = None                # 누적 손익률 (%)
    traded_amount: Optional[float] = None          # 거래된 금액 (총 체결대금, KRW) — 집계만
    win_rate: Optional[float] = None
    n_trades: Optional[int] = None
    n_positions: int = 0                            # 현재 보유 종목 수
    last_snapshot_at: Optional[UtcDateTime] = None


class StrategyRestoreIn(BaseModel):
    version_no: int


# ── 동기화 ────────────────────────────────────────────────────────────────────

class SyncPushIn(BaseModel):
    payload: dict[str, Any]           # 잔고·포지션·자산곡선·체결로그 (안전정보만)


class SyncSnapshotOut(BaseModel):
    payload: dict[str, Any]
    received_at: UtcDateTime
    device_id: Optional[int] = None
    # Phase 58 — cycle 외 시간(새벽 등) 로컬앱 alive 신호. snapshot 갱신과 별도로
    # 5분 주기로 갱신. 웹앱이 last_heartbeat_at 또는 received_at 중 최신을 사용해
    # "끊김" 판단. 메모리 dict 기반이라 server restart 시 최대 5분 stale 가능.
    last_heartbeat_at: Optional[UtcDateTime] = None


# ── 종목마스터 sync ───────────────────────────────────────────────────────────

class UserSettingsIO(BaseModel):
    alert_webhook_url: str = ""
    alert_on_killswitch: bool = True
    alert_on_daily_loss_pct: float = 2.0
    alert_on_unfilled_count: int = 5
    # Phase 48 P1-C — 슬리피지 임계 초과 알림 (bps, 0=비활성)
    alert_on_slippage_bps: int = 30
    # Phase 48 P1-D — 일일 거래 한도 (0=비활성)
    daily_turnover_limit_krw: int = 0
    daily_trade_count_limit: int = 0
    # Phase 38.7 — kill switch 일일 손실 한도. None이면 default(3.0)
    kill_switch_daily_loss_pct: Optional[float] = None
    # Phase 38.10 — 누적 drawdown 한도. None이면 default(20.0)
    max_drawdown_pct: Optional[float] = None
    # Phase 38.5 — preview 연속 누락 알림 임계값
    preview_missing_alert_threshold: int = 3
    # Phase 40 — KIS ↔ ledger 정합성 drift 알림 토글
    alert_on_reconcile_drift: bool = True
    # 미국 매수여력 모드: "integrated"(통합증거금) | "usd_cash"(USD 예수금 한정)
    us_buying_power_mode: str = "integrated"


class TradableSymbolIn(BaseModel):
    symbol: str
    name: str = ""
    market: str = ""


class TradableSymbolsSyncIn(BaseModel):
    """로컬앱이 KIS 종목마스터를 push할 때 사용. 받는 즉시 전체 교체(snapshot)."""
    symbols: list[TradableSymbolIn]


# ── 명령 큐 ───────────────────────────────────────────────────────────────────

class CommandIn(BaseModel):
    device_id: int                       # 명령을 받을 기기 (사용자 소유 확인)
    type: str                            # 명령 타입
    params: dict[str, Any] = {}


class CommandAckIn(BaseModel):
    status: str                          # done | failed
    result: dict[str, Any] = {}


class CommandOut(BaseModel):
    id: int
    device_id: int
    type: str
    params: dict[str, Any]
    status: str
    created_at: UtcDateTime
    delivered_at: Optional[UtcDateTime] = None
    completed_at: Optional[UtcDateTime] = None
    result: dict[str, Any]

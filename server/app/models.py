"""DB 모델 (SQLModel).

플랫폼은 안전정보만 보관한다 — 계정·전략·동기화 스냅샷.
API키·계좌번호·원시주문은 절대 저장하지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    # Google 전용 가입자는 비밀번호가 없으므로 nullable
    password_hash: Optional[str] = None
    # Google 계정 고유 ID(sub). 소셜 로그인으로 가입·연동된 사용자에만 존재
    google_sub: Optional[str] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now)


class Strategy(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    name: str
    run_mode: str = "draft"          # draft | paper | live
    definition: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    # Phase 59 — run_mode 전환 시점 기록. "적용 기간" 계산에 사용.
    paper_started_at: Optional[datetime] = None
    live_started_at: Optional[datetime] = None
    live_capital_at_start: Optional[float] = None    # 실전 전환 시점 자본 (수익률 기준점)


class StrategyVersion(SQLModel, table=True):
    """전략 정의 이력 — PUT /strategies/{id}에서 자동 스냅샷.

    매 PUT마다 변경 전 정의를 보존. 30일 또는 50건 초과분은 자동 회전(삭제).
    사용자가 잘못 수정한 후 특정 버전으로 복원 가능.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    strategy_id: int = Field(index=True, foreign_key="strategy.id")
    version_no: int                   # 1, 2, 3... strategy당 sequential
    name: str                         # 스냅샷 시점의 이름
    definition: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)
    # "manual_edit" | "restore_from_vN" | "initial"
    created_reason: str = "manual_edit"


class Device(SQLModel, table=True):
    """페어링된 로컬앱 기기. token_hash만 저장, 원본 토큰은 발급 시 1회만 노출."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    name: str
    token_hash: str = Field(index=True)
    created_at: datetime = Field(default_factory=_now)
    last_seen_at: Optional[datetime] = None


class PairingRequest(SQLModel, table=True):
    """OAuth 기기 인증 그랜트 — 로컬앱이 시작, 웹에서 사용자가 승인."""
    id: Optional[int] = Field(default=None, primary_key=True)
    device_code: str = Field(index=True, unique=True)
    user_code: str = Field(index=True)
    device_name: str
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    approved: bool = False
    consumed: bool = False
    created_at: datetime = Field(default_factory=_now)
    expires_at: datetime


class SyncSnapshot(SQLModel, table=True):
    """로컬앱이 푸시한 안전정보 스냅샷 (잔고·포지션·자산곡선·체결로그)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    device_id: int = Field(foreign_key="device.id")
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON))
    received_at: datetime = Field(default_factory=_now)


class UserSettings(SQLModel, table=True):
    """사용자별 모니터링·알림·위험 한도 설정 (1:1)."""
    user_id: int = Field(primary_key=True, foreign_key="user.id")
    alert_webhook_url: str = ""           # Discord/Slack-compatible webhook URL
    alert_on_killswitch: bool = True
    alert_on_daily_loss_pct: float = 2.0   # |손실|이 이 % 도달 시 webhook
    alert_on_unfilled_count: int = 5       # 미체결이 N건 이상 누적되면 webhook
    # Phase 48 P1-C — 슬리피지 임계 초과 알림. 0/null이면 비활성.
    alert_on_slippage_bps: int = 30        # 평균 슬리피지가 N bps 초과 시 webhook (1bp=0.01%)
    last_alerted_slippage: Optional[datetime] = None
    # Phase 48 P1-D — 일일 거래 한도 (0=비활성, 가이드라인 부록2 권장).
    daily_turnover_limit_krw: int = 0       # 일일 거래 대금 한도(원). 도달 시 신규 진입 차단.
    daily_trade_count_limit: int = 0        # 일일 거래 횟수 한도. 도달 시 신규 진입 차단.
    last_alerted_killswitch: Optional[datetime] = None
    last_alerted_loss: Optional[datetime] = None
    # Phase 38.7 — kill switch 일일 손실 한도 (자본 대비 %, 1~10 범위 권장).
    # null이면 글로벌 default (DEFAULT_EXECUTION['daily_loss_limit_pct'])
    kill_switch_daily_loss_pct: Optional[float] = None
    # Phase 38.10 — 누적 drawdown 한도 (자본 고점 대비 %). null이면 default.
    max_drawdown_pct: Optional[float] = None
    # Phase 38.5 — preview 연속 누락 일수 카운터 + 알림 임계값
    preview_missing_streak: int = 0
    preview_missing_alert_threshold: int = 3
    last_alerted_preview_missing: Optional[datetime] = None
    # Phase 40 — 잔고 정합성 (KIS ↔ ledger) drift 알림
    alert_on_reconcile_drift: bool = True
    last_alerted_reconcile: Optional[datetime] = None
    # 미국 매수여력 모드: "integrated"=KIS 통합증거금(KRW 담보, FX 노출) /
    # "usd_cash"=USD 예수금 한정(보수적, FX 노출 없음). 미국 종목 사이징에만 영향.
    us_buying_power_mode: str = "integrated"
    updated_at: datetime = Field(default_factory=_now)


class BacktestRun(SQLModel, table=True):
    """백테스트 실행 내역 — 저장된 전략과 연결되면 strategy_id 보관.

    Phase 59: strategy_id NULL 이면 빌더에서 저장 안 한 시범 실행 → 즉시 삭제 대상.
    저장 시점에 strategy_id 확정. 전략 detail "백테스트 내역" 탭의 데이터 소스.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    strategy_id: Optional[int] = Field(default=None, index=True, foreign_key="strategy.id")
    version_no: Optional[int] = None        # 어떤 버전 시점의 백테스트인지
    name: str = ""                          # 전략 이름 스냅샷
    definition: dict = Field(default_factory=dict, sa_column=Column(JSON))
    result: dict = Field(default_factory=dict, sa_column=Column(JSON))  # 메트릭+요약만 (trades는 별도)
    initial_capital: float = 0.0
    start: Optional[str] = None             # 백테스트 시작일 (YYYY-MM-DD)
    end: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)


class TradableSymbol(SQLModel, table=True):
    """KIS 종목마스터에서 sync된 거래 가능 종목 화이트리스트.

    로컬앱이 KIS 공식 마스터(.mst)를 다운로드/파싱 후 push한다.
    /symbols 응답에서 tradable=True 판정의 기준이 된다.
    user_id별로 격리 — 사용자 계좌(KOSPI/KOSDAQ/해외 등)에 따라 다를 수 있다.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    symbol: str = Field(index=True)
    name: str = ""
    market: str = ""                 # KOSPI | KOSDAQ | 등등
    updated_at: datetime = Field(default_factory=_now)


class ScreenerUserPreset(SQLModel, table=True):
    """사용자가 직접 만든 자동 선택 '세트' — 계정에 저장되어 전략 간 재사용.

    spec은 screener.parse_spec이 받는 ScreenerSpec dict. 시세성 데이터로만
    동작하므로 안전정보 원칙에 위배되지 않는다(계좌·자격증명 없음).
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    name: str
    spec: dict = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Command(SQLModel, table=True):
    """웹 → 로컬앱 명령 큐.

    웹에서 사용자가 발행하면 status='pending'으로 저장. 로컬앱이 SSE 또는
    폴링으로 pickup → 실행 → status='done|failed'로 업데이트.
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    device_id: int = Field(index=True, foreign_key="device.id")
    # RUN_CYCLE_NOW / PAUSE_AUTO / RESUME_AUTO / LIQUIDATE_ALL
    # / CANCEL_ORDER / RESET_KILL_SWITCH
    type: str
    params: dict = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = "pending"           # pending | delivered | done | failed
    created_at: datetime = Field(default_factory=_now)
    delivered_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    result: dict = Field(default_factory=dict, sa_column=Column(JSON))

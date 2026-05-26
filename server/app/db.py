"""DB 엔진 및 세션."""

import logging

from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy import text

from .config import settings

_log = logging.getLogger("app.db")

_is_sqlite = settings.DB_URL.startswith("sqlite")
_is_postgres = settings.DB_URL.startswith("postgresql")

# Phase 42-1 — Postgres 연결 안정화.
# Railway·기타 호스팅 proxy가 idle connection을 끊으면 SQLAlchemy pool에 들어 있던
# socket이 stale → 다음 요청에서 `psycopg.errors.ProtocolViolation: server conn crashed?`
# traceback. pool_pre_ping(매 사용 전 ping)·pool_recycle(주기적 재생성)·OS keepalives
# 3중 방어로 차단. SQLite는 pool 없으므로 적용 불필요.
if _is_sqlite:
    _connect_args = {"check_same_thread": False}
    _engine_kwargs: dict = {}
elif _is_postgres:
    _connect_args = {
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }
    _engine_kwargs = {"pool_pre_ping": True, "pool_recycle": 300}
else:
    _connect_args = {}
    _engine_kwargs = {}

engine = create_engine(settings.DB_URL, echo=False,
                       connect_args=_connect_args, **_engine_kwargs)


# Phase 49-FX-9 — 멱등 스키마 보정의 단일 진실 공급원.
# 기존: PG/SQLite 분기 두 곳에 같은 컬럼을 따로 적어야 했고, 한쪽 누락이
# production cascade(5/24 Fix B)를 일으켰음. 신규 컬럼은 이 list 한 곳에만
# 추가하면 _ensure_column이 PG/SQLite 모두 적용한다.
#
# 타입은 PG 표기 기준. SQLite는 _ensure_column이 자동 보정
# (DOUBLE PRECISION → REAL, BIGINT → INTEGER).
_NEW_COLS: list[tuple[str, str, str]] = [
    ("user",         "google_sub",                       "VARCHAR"),
    ("usersettings", "kill_switch_daily_loss_pct",       "DOUBLE PRECISION"),
    ("usersettings", "max_drawdown_pct",                 "DOUBLE PRECISION"),
    ("usersettings", "preview_missing_streak",           "INTEGER DEFAULT 0"),
    ("usersettings", "preview_missing_alert_threshold",  "INTEGER DEFAULT 3"),
    ("usersettings", "last_alerted_preview_missing",     "TIMESTAMP"),
    ("usersettings", "alert_on_reconcile_drift",         "BOOLEAN DEFAULT TRUE"),
    ("usersettings", "last_alerted_reconcile",           "TIMESTAMP"),
    ("usersettings", "us_buying_power_mode",             "VARCHAR DEFAULT 'integrated'"),
    # Phase 48 P1-C — 슬리피지 임계 알림
    ("usersettings", "alert_on_slippage_bps",            "INTEGER DEFAULT 30"),
    ("usersettings", "last_alerted_slippage",            "TIMESTAMP"),
    # Phase 48 P1-D — 일일 거래 금액·횟수 한도 (0=비활성)
    ("usersettings", "daily_turnover_limit_krw",         "BIGINT DEFAULT 0"),
    ("usersettings", "daily_trade_count_limit",          "INTEGER DEFAULT 0"),
    # Phase 59 — 전략 버전 이력·현황 컬럼
    ("strategy",     "paper_started_at",                 "TIMESTAMP"),
    ("strategy",     "live_started_at",                  "TIMESTAMP"),
    ("strategy",     "live_capital_at_start",            "DOUBLE PRECISION"),
    ("backtestrun",  "strategy_id",                      "INTEGER"),
    ("backtestrun",  "version_no",                       "INTEGER"),
]


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """PG/SQLite 통합 멱등 컬럼 추가.

    PG: information_schema 조회 후 ADD COLUMN.
    SQLite: PRAGMA table_info 조회 후 ADD COLUMN. PG-only 타입(DOUBLE PRECISION·
    BIGINT)은 SQLite 동등 타입(REAL·INTEGER)으로 자동 변환.
    """
    if _is_postgres:
        exists = conn.execute(text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"),
            {"t": table, "c": column}).fetchone() is not None
        type_ddl = ddl
    else:
        rows = conn.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()
        exists = column in {r[1] for r in rows}
        # SQLite는 DOUBLE PRECISION·BIGINT 미지원 — 동등 타입으로 변환.
        type_ddl = (ddl
                    .replace("DOUBLE PRECISION", "REAL")
                    .replace("BIGINT", "INTEGER"))
    if exists:
        return
    if _is_postgres:
        conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {column} {type_ddl}'))
    else:
        conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN {column} {type_ddl}')


def _migrate() -> None:
    """기존 배포 DB에 대한 멱등 스키마 보정.

    create_all은 신규 테이블만 만들고 기존 테이블 컬럼은 바꾸지 않으므로,
    이미 운영 중인 테이블에 신규 컬럼을 추가한다. 새로 만들어진 DB에서는
    모두 no-op(컬럼이 이미 존재). PG와 SQLite를 통합한 단일 경로로 처리해
    한쪽 분기 누락으로 인한 production cascade(5/24 Fix B 회귀) 차단.
    """
    try:
        with engine.begin() as conn:
            # PG-only: password_hash DROP NOT NULL. SQLite는 ALTER COLUMN
            # 미지원이지만 기본 nullable이라 무동작이 곧 정상.
            if _is_postgres:
                conn.execute(text(
                    'ALTER TABLE "user" ALTER COLUMN password_hash DROP NOT NULL'))
            # 통합 컬럼 보정 — _NEW_COLS 한 곳에만 추가하면 PG/SQLite 모두 적용.
            for table, column, ddl in _NEW_COLS:
                _ensure_column(conn, table, column, ddl)
            # Phase 59 — orphan BacktestRun(strategy_id가 NULL인 row) 즉시 삭제.
            # 사용자 결정: 저장 안 한 시범 백테스트는 보관 X. backtestrun.strategy_id
            # 컬럼이 막 추가됐으면 기존 row는 모두 NULL — 일괄 삭제.
            try:
                conn.execute(text(
                    "DELETE FROM backtestrun WHERE strategy_id IS NULL"))
            except Exception:
                _log.exception("[migrate] orphan BacktestRun 삭제 실패")
    except Exception:  # noqa: BLE001  — 마이그레이션 실패가 기동을 막지 않도록
        # S-11 — print는 로그 수집기에 안 잡혀 배포 "성공"인데 스키마 손상이
        # 침묵하는 위험. exception 레벨로 traceback까지 남겨야 운영에서 보임.
        _log.exception("[migrate] 스키마 보정 건너뜀")


def create_db_and_tables() -> None:
    from . import models  # noqa: F401  (테이블 등록)
    SQLModel.metadata.create_all(engine)
    _migrate()


def get_session():
    with Session(engine) as session:
        yield session

"""DB 엔진 및 세션."""

from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy import text

from .config import settings

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


def _migrate() -> None:
    """기존 배포 DB에 대한 멱등 스키마 보정 (Google 로그인 도입 및 UserSettings 컬럼 보정).

    create_all은 신규 테이블만 만들고 기존 테이블 컬럼은 바꾸지 않으므로,
    이미 운영 중인 user 테이블에 google_sub 추가 + password_hash nullable 처리.
    또한 UserSettings에 새로 추가된 컬럼들을 안전하게 추가한다.
    새로 만들어진 DB에서는 모두 no-op이다.
    """
    is_pg = settings.DB_URL.startswith("postgresql")
    try:
        with engine.begin() as conn:
            if is_pg:
                # user 테이블 보정
                conn.execute(text(
                    'ALTER TABLE "user" ALTER COLUMN password_hash DROP NOT NULL'))
                conn.execute(text(
                    'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS google_sub VARCHAR'))
                
                # usersettings 테이블 보정 (Phase 38~40 추가 컬럼)
                conn.execute(text(
                    'ALTER TABLE "usersettings" ADD COLUMN IF NOT EXISTS kill_switch_daily_loss_pct DOUBLE PRECISION'))
                conn.execute(text(
                    'ALTER TABLE "usersettings" ADD COLUMN IF NOT EXISTS max_drawdown_pct DOUBLE PRECISION'))
                conn.execute(text(
                    'ALTER TABLE "usersettings" ADD COLUMN IF NOT EXISTS preview_missing_streak INTEGER DEFAULT 0'))
                conn.execute(text(
                    'ALTER TABLE "usersettings" ADD COLUMN IF NOT EXISTS preview_missing_alert_threshold INTEGER DEFAULT 3'))
                conn.execute(text(
                    'ALTER TABLE "usersettings" ADD COLUMN IF NOT EXISTS last_alerted_preview_missing TIMESTAMP'))
                conn.execute(text(
                    'ALTER TABLE "usersettings" ADD COLUMN IF NOT EXISTS alert_on_reconcile_drift BOOLEAN DEFAULT TRUE'))
                conn.execute(text(
                    'ALTER TABLE "usersettings" ADD COLUMN IF NOT EXISTS last_alerted_reconcile TIMESTAMP'))
            else:
                # SQLite - user 테이블 보정
                cols = [r[1] for r in conn.exec_driver_sql(
                    'PRAGMA table_info("user")').fetchall()]
                if cols and "google_sub" not in cols:
                    conn.exec_driver_sql(
                        'ALTER TABLE "user" ADD COLUMN google_sub VARCHAR')
                
                # SQLite - usersettings 테이블 보정
                us_cols = [r[1] for r in conn.exec_driver_sql(
                    'PRAGMA table_info("usersettings")').fetchall()]
                if us_cols:
                    if "kill_switch_daily_loss_pct" not in us_cols:
                        conn.exec_driver_sql('ALTER TABLE "usersettings" ADD COLUMN kill_switch_daily_loss_pct REAL')
                    if "max_drawdown_pct" not in us_cols:
                        conn.exec_driver_sql('ALTER TABLE "usersettings" ADD COLUMN max_drawdown_pct REAL')
                    if "preview_missing_streak" not in us_cols:
                        conn.exec_driver_sql('ALTER TABLE "usersettings" ADD COLUMN preview_missing_streak INTEGER DEFAULT 0')
                    if "preview_missing_alert_threshold" not in us_cols:
                        conn.exec_driver_sql('ALTER TABLE "usersettings" ADD COLUMN preview_missing_alert_threshold INTEGER DEFAULT 3')
                    if "last_alerted_preview_missing" not in us_cols:
                        conn.exec_driver_sql('ALTER TABLE "usersettings" ADD COLUMN last_alerted_preview_missing TIMESTAMP')
                    if "alert_on_reconcile_drift" not in us_cols:
                        conn.exec_driver_sql('ALTER TABLE "usersettings" ADD COLUMN alert_on_reconcile_drift BOOLEAN DEFAULT TRUE')
                    if "last_alerted_reconcile" not in us_cols:
                        conn.exec_driver_sql('ALTER TABLE "usersettings" ADD COLUMN last_alerted_reconcile TIMESTAMP')
    except Exception as e:  # noqa: BLE001  — 마이그레이션 실패가 기동을 막지 않도록
        print(f"[migrate] 스키마 보정 건너뜀: {e}")


def create_db_and_tables() -> None:
    from . import models  # noqa: F401  (테이블 등록)
    SQLModel.metadata.create_all(engine)
    _migrate()


def get_session():
    with Session(engine) as session:
        yield session

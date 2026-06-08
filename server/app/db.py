"""DB 엔진 및 세션."""

import logging

from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

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
    # Phase 58+ — heartbeat 영구 저장 (server 재부팅 false alarm 방지)
    ("usersettings", "last_heartbeat_at",                "TIMESTAMP"),
    # Stage 1 (IR 수렴) — 전략 표현 엔진 판별자(operand|ir). 레거시 row는 default 'operand'.
    ("strategy",     "engine",                           "VARCHAR DEFAULT 'operand'"),
    # Phase 59 — 전략 버전 이력·현황 컬럼
    ("strategy",     "paper_started_at",                 "TIMESTAMP"),
    ("strategy",     "live_started_at",                  "TIMESTAMP"),
    ("strategy",     "live_capital_at_start",            "DOUBLE PRECISION"),
    # Task 12b — 정적 세부조건 라이브 바스켓(symbol list, JSON). create_all이 만드는
    # definition 등 dict 컬럼과 동일 타입(SQLAlchemy JSON → PG/SQLite 모두 'JSON').
    ("strategy",     "live_basket",                      "JSON"),
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


# Phase 60 — Neon 서버리스 연결 끊김 재시도.
# Neon은 유휴 시 compute를 suspend하며 연결을 끊는다. pool_pre_ping(checkout 시
# SELECT 1)은 Neon `-pooler`(PgBouncer)에선 ping과 실제 쿼리가 다른 백엔드를 쓸 수
# 있어 mid-request/cron 연결 끊김을 못 막는다(`server conn crashed?`가 그대로 전파).
# C1은 "계산 중 연결 점유"만 제거했고, 이 헬퍼는 *남은 부류* — 끊긴 연결로의 쿼리
# 자체 — 를 닫는다: 끊김 감지 시 pool을 폐기(fresh 연결 강제)하고 1회 재시도.

def is_disconnect(exc: BaseException) -> bool:
    """예외가 DB 연결 끊김(Neon suspend 등)인지 판별.

    SQLAlchemy가 disconnect로 분류하면 connection_invalidated=True로 표시하고 해당
    연결을 pool에서 제거한다. 분류 못 한 경우를 위해 Neon 특유 메시지도 함께 본다.
    """
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return True
    return "server conn crashed" in str(exc).lower()


def call_with_disconnect_retry(fn, *args, **kwargs):
    """fn(*args, **kwargs) 실행 — DB 연결 끊김이면 pool 폐기 후 fresh 연결로 1회 재시도.

    서버리스 Postgres(Neon)의 표준 대응. 끊김이 아닌 예외는 그대로 전파한다.
    재시도도 끊김으로 실패하면(드뭄) 전파 — 호출자(cron 재시도 큐 등)가 처리.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 — is_disconnect로 선별, 나머지는 즉시 재전파
        if not is_disconnect(e):
            raise
        _log.warning("DB 연결 끊김 감지(Neon suspend 추정) — pool 재생성 후 1회 재시도")
        engine.dispose()
        return fn(*args, **kwargs)

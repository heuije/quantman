"""감사 #2 — 핫경로 복합 인덱스 멱등 마이그레이션 명세.

폴링 핫경로가 인덱스 없이 풀스캔이었다:
- syncsnapshot "유저 최신 1건"(ORDER BY received_at DESC LIMIT 1) — sync·trading·
  portfolio·preview_engine 7곳 → (user_id, received_at DESC) 복합 인덱스.
- command pending 폴링(WHERE device_id=? AND status='pending') → (device_id, status).

_migrate는 매 부트 실행되므로 CREATE INDEX IF NOT EXISTS로 멱등이어야 한다.
_migrate가 예외를 삼키고 ERROR 로그만 남기는 구조라, "2회 실행에 ERROR 로그 0"이
멱등성의 신호다(인덱스 존재만 보면 1회차 성공이 2회차 실패를 가린다).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import db

# 인덱스명 → (테이블, 컬럼 순서). 컬럼 순서가 곧 인덱스 효용(선두 컬럼 =-필터)이므로 잠근다.
_EXPECTED = {
    "ix_syncsnapshot_user_id_received_at": ("syncsnapshot", ["user_id", "received_at"]),
    "ix_command_device_id_status": ("command", ["device_id", "status"]),
}


def _engine(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    return engine


def _index_names(engine):
    with engine.connect() as c:
        return {r[0] for r in c.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index'")).fetchall()}


def _index_cols(engine, name):
    with engine.connect() as c:
        rows = c.exec_driver_sql(f"PRAGMA index_info('{name}')").fetchall()
    return [r[2] for r in rows]


def test_migrate_creates_hot_path_indexes(monkeypatch):
    engine = _engine(monkeypatch)
    db._migrate()
    names = _index_names(engine)
    for name, (_table, cols) in _EXPECTED.items():
        assert name in names, f"{name} 인덱스가 생성되지 않았다"
        assert _index_cols(engine, name) == cols


def test_migrate_is_idempotent_on_second_boot(monkeypatch, caplog):
    """매 부트 실행되는 _migrate를 2회 실행 — 에러 로그 0 + 인덱스는 1개씩만."""
    engine = _engine(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="app.db"):
        db._migrate()
        db._migrate()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], f"_migrate 2회 실행에서 에러 발생: {[r.message for r in errors]}"
    with engine.connect() as c:
        for name in _EXPECTED:
            cnt = c.execute(text(
                "SELECT count(*) FROM sqlite_master WHERE type='index' AND name=:n"),
                {"n": name}).scalar()
            assert cnt == 1

"""감사 #1 — 무한 성장 테이블(HeartbeatEvent·SyncSnapshot) pruning 명세.

HeartbeatEvent는 5분 ping마다 row 1개(기기당 288행/일), SyncSnapshot은 이벤트 push마다
수백KB JSON — 정리 로직 없이 무한 누적돼 최신조회 정렬·egress 비용이 시간에 비례 증가했다
(2026-06-10 Neon egress 쿼터 인시던트를 서서히 재유발하는 구조). db.prune_old_rows 보존정책:

- heartbeatevent: 30일 초과 삭제. latest는 UserSettings.last_heartbeat_at가 따로 들고,
  이력 소비처(/trading·/sync timeline)는 24h window만 읽는다.
- syncsnapshot: 30일 초과 삭제하되 **유저별 최신 1건은 무조건 보존** —
  preview_engine·/sync/snapshot·/trading/timeline·/portfolio가 유저 최신 1건에 의존
  (휴면 유저도 마지막 상태는 보여야 한다).
- 대량 누적 1회차에도 락을 오래 잡지 않도록 batch(LIMIT) 단위 삭제.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import db
from app.models import HeartbeatEvent, SyncSnapshot, User


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _engine(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    return engine


def _users(engine, n=2):
    ids = []
    with Session(engine) as s:
        for i in range(n):
            u = User(email=f"u{i}@example.com")
            s.add(u); s.commit(); s.refresh(u)
            ids.append(u.id)
    return ids


def _add(engine, rows):
    with Session(engine) as s:
        for r in rows:
            s.add(r)
        s.commit()
        return [r.id for r in rows]


def test_prune_heartbeat_deletes_only_rows_older_than_30d(monkeypatch):
    """31일 전·30일+1분 전 행은 삭제, 30일 안쪽(경계 안)·오늘·타유저 행은 보존."""
    engine = _engine(monkeypatch)
    uid, other = _users(engine)
    now = _now()
    old_ids = _add(engine, [
        HeartbeatEvent(user_id=uid, at=now - timedelta(days=31)),
        HeartbeatEvent(user_id=uid, at=now - timedelta(days=30, minutes=1)),
    ])
    keep_ids = _add(engine, [
        # 경계 안쪽(29일 23시간 59분 전) — 보존돼야 한다
        HeartbeatEvent(user_id=uid, at=now - timedelta(days=30) + timedelta(minutes=1)),
        HeartbeatEvent(user_id=uid, at=now),
        # 타 유저 불간섭 — 최근 행은 그대로
        HeartbeatEvent(user_id=other, at=now - timedelta(days=1)),
    ])

    result = db.prune_old_rows()

    assert result["heartbeatevent"] == len(old_ids)
    with Session(engine) as s:
        remaining = {r.id for r in s.exec(select(HeartbeatEvent)).all()}
    assert remaining == set(keep_ids)


def test_prune_snapshot_keeps_latest_per_user(monkeypatch):
    """유저별 최신 1건은 30일을 넘겨도 보존(휴면 유저), 나머지 구건만 삭제."""
    engine = _engine(monkeypatch)
    uid_idle, uid_active = _users(engine)
    now = _now()
    # 휴면 유저 — 전부 30일 초과. 최신(31일 전) 1건만 남아야 한다.
    idle_old = _add(engine, [
        SyncSnapshot(user_id=uid_idle, device_id=1, payload={"n": 1},
                     received_at=now - timedelta(days=40)),
        SyncSnapshot(user_id=uid_idle, device_id=1, payload={"n": 2},
                     received_at=now - timedelta(days=35)),
    ])
    idle_latest = _add(engine, [
        SyncSnapshot(user_id=uid_idle, device_id=1, payload={"n": 3},
                     received_at=now - timedelta(days=31)),
    ])
    # 활성 유저 — 구건은 삭제, 최근 건이 최신이므로 보존.
    active_old = _add(engine, [
        SyncSnapshot(user_id=uid_active, device_id=2, payload={"n": 4},
                     received_at=now - timedelta(days=31)),
    ])
    active_recent = _add(engine, [
        SyncSnapshot(user_id=uid_active, device_id=2, payload={"n": 5},
                     received_at=now - timedelta(days=1)),
    ])

    result = db.prune_old_rows()

    assert result["syncsnapshot"] == len(idle_old) + len(active_old)
    with Session(engine) as s:
        remaining = {r.id for r in s.exec(select(SyncSnapshot)).all()}
    assert remaining == set(idle_latest) | set(active_recent)


def test_prune_deletes_in_batches(monkeypatch):
    """batch(LIMIT)보다 많은 누적분도 루프로 전부 삭제 — 첫 실행 대량분 시나리오."""
    engine = _engine(monkeypatch)
    (uid,) = _users(engine, n=1)
    monkeypatch.setattr(db, "_PRUNE_BATCH", 2)
    now = _now()
    _add(engine, [HeartbeatEvent(user_id=uid, at=now - timedelta(days=31, minutes=i))
                  for i in range(5)])
    hb_keep = _add(engine, [HeartbeatEvent(user_id=uid, at=now)])
    _add(engine, [SyncSnapshot(user_id=uid, device_id=1, payload={},
                               received_at=now - timedelta(days=31, minutes=i))
                  for i in range(5)])
    snap_keep = _add(engine, [SyncSnapshot(user_id=uid, device_id=1, payload={},
                                           received_at=now)])

    result = db.prune_old_rows()

    assert result == {"heartbeatevent": 5, "syncsnapshot": 5}
    with Session(engine) as s:
        assert {r.id for r in s.exec(select(HeartbeatEvent)).all()} == set(hb_keep)
        assert {r.id for r in s.exec(select(SyncSnapshot)).all()} == set(snap_keep)


def test_prune_empty_tables_is_noop(monkeypatch):
    _engine(monkeypatch)
    assert db.prune_old_rows() == {"heartbeatevent": 0, "syncsnapshot": 0}

"""Neon egress 절감 — timeline projection + tag-first ETag 회귀 테스트.

인시던트 docs/incidents/2026-06-10-neon-data-transfer-quota.md 의 근본 동인
D1/D2/D3 재발 방지를 고정한다:
  · D1 — /trading·/sync timeline이 full payload SELECT → cycle_summary만 projection
  · D2 — /trading/timeline ETag 부재 → tag-first 304 (window 조회 전 회피)
  · D3 — /sync/snapshot이 payload 로드 후 ETag 계산 → scalar로 tag-first

자기완결: 인메모리 SQLite + 최소 FastAPI 앱. 네트워크·lifespan 없음.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import sqlalchemy
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.db import get_session
from app.deps import get_current_user
from app.models import Device, SyncSnapshot, User, UserSettings
from app.routers import sync as sync_router
from app.routers import trading as trading_router

KST = ZoneInfo("Asia/Seoul")


# ── 픽스처 ───────────────────────────────────────────────────────────────────

def _kst_to_naive_utc(dt_kst: datetime) -> datetime:
    """KST aware → naive-UTC. prod의 received_at 저장 형식(tz-naive UTC)과 일치."""
    return dt_kst.astimezone(timezone.utc).replace(tzinfo=None)


def _build(seed):
    """인메모리 DB + trading·sync 라우터 + 시드 유저. (TestClient, engine) 반환.

    seed(session, user) 콜백으로 스냅샷·설정을 시드한다.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="t@example.com")
        s.add(user); s.commit(); s.refresh(user)
        device = Device(user_id=user.id, name="pc", token_hash="x")
        s.add(device); s.commit(); s.refresh(device)
        seed(s, user, device)
        s.commit()

    app = FastAPI()
    app.include_router(trading_router.router)
    app.include_router(sync_router.router)

    def _override_session():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _override_session

    with Session(engine) as s:
        seeded_user = s.exec(
            sqlalchemy.select(User)).scalars().first()
    app.dependency_overrides[get_current_user] = lambda: seeded_user

    return TestClient(app), engine


def _capture_sql(engine):
    """engine에 실행되는 SQL 문자열을 수집하는 list 반환 + 리스너 등록."""
    captured: list[str] = []

    @sqlalchemy.event.listens_for(engine, "before_cursor_execute")
    def _on_exec(conn, cursor, statement, parameters, context, executemany):  # noqa
        captured.append(statement)

    return captured


# ── 1. DB-level projection (D1) ───────────────────────────────────────────────

def test_snapshots_in_window_projects_at_db_level():
    """_snapshots_in_window가 cycle_summary만 DB에서 projection — full row SELECT 금지."""
    heavy = {
        "cycle_summary": {"market": "KRX", "n_bought": 1, "n_sold": 0},
        # full-payload SELECT 회귀 검출용 무거운 junk 키들
        "equity": list(range(500)),
        "trades": [{"sym": f"{i:06d}", "qty": i} for i in range(200)],
        "positions": [{"sym": f"{i:06d}", "v": i * 1.5} for i in range(200)],
    }
    now_kst = datetime(2026, 6, 10, 14, 0, tzinfo=KST)
    snap_kst = datetime(2026, 6, 10, 8, 55, tzinfo=KST)

    def seed(s, user, device):
        s.add(SyncSnapshot(user_id=user.id, device_id=device.id,
                           payload=heavy,
                           received_at=_kst_to_naive_utc(snap_kst)))

    client, engine = _build(seed)
    captured = _capture_sql(engine)

    start = now_kst - timedelta(hours=24)
    end = now_kst + timedelta(hours=24)
    with Session(engine) as s:
        rows = trading_router._snapshots_in_window(s, 1, start, end)

    # (a) 반환 payload는 cycle_summary 키만 — junk 키는 DB에서 읽지 않았다
    assert len(rows) == 1
    assert set(rows[0].payload.keys()) == {"cycle_summary"}
    assert rows[0].payload["cycle_summary"]["n_bought"] == 1

    # (b) 실행 SQL에 json_extract 존재 — SQLite에서 DB-level projection 증거
    #     (full-row SELECT면 json_extract가 나오지 않는다)
    joined = "\n".join(captured)
    assert "json_extract" in joined.lower(), joined


# ── 2. 이벤트 동등성 (refactor 후 회귀 가드) ──────────────────────────────────

def test_timeline_events_parity_after_refactor(monkeypatch):
    """projection·preview 헬퍼 리팩토링 후에도 krx_cycle·krx_preview 이벤트 동일."""
    now_kst = datetime(2026, 6, 10, 14, 0, tzinfo=KST)
    monkeypatch.setattr(trading_router, "_now_kst", lambda: now_kst)
    monkeypatch.setattr(trading_router, "_is_session_day_safe", lambda m, d: True)
    # US 이벤트는 실제 캘린더 의존 — 결정성 위해 차단(CalendarError로 skip).
    def _raise(*a, **k):
        raise trading_router.mc.CalendarError("test")
    monkeypatch.setattr(trading_router.mc, "next_session_kst", _raise)

    cycle_kst = datetime(2026, 6, 10, 8, 55, tzinfo=KST)
    preview_kst = datetime(2026, 6, 10, 7, 30, tzinfo=KST)

    def seed(s, user, device):
        s.add(SyncSnapshot(
            user_id=user.id, device_id=device.id,
            payload={"cycle_summary": {"market": "KRX", "n_bought": 1, "n_sold": 0,
                                       "today": "2026-06-10"}},
            received_at=_kst_to_naive_utc(cycle_kst)))
        s.add(SyncSnapshot(
            user_id=user.id, device_id=device.id,
            payload={"next_day_preview": {
                "generated_at": preview_kst.isoformat(),
                "by_strategy": [{"candidates": [{"symbol": "005930"}]}]}},
            received_at=_kst_to_naive_utc(preview_kst + timedelta(minutes=1))))

    client, engine = _build(seed)
    r = client.get("/trading/timeline")
    assert r.status_code == 200, r.text
    events = r.json()["events"]

    krx_cycle = [e for e in events if e["kind"] == "krx_cycle"
                 and e["at"].startswith("2026-06-10")]
    assert krx_cycle, events
    done = [e for e in krx_cycle if e["status"] == "done"]
    assert done, krx_cycle
    assert "1건" in done[0]["summary"]

    krx_preview = [e for e in events if e["kind"] == "krx_preview"
                   and e["at"].startswith("2026-06-10") and e["status"] == "done"]
    assert krx_preview, events
    assert "1건" in krx_preview[0]["summary"]


# ── 3. tag-first ETag + 300s bucket (D2) ──────────────────────────────────────

def test_timeline_etag_304_and_bucket(monkeypatch):
    """/trading/timeline: 200+ETag → 304(매칭) → bucket 경과 시 새 tag → snapshot 추가 시 새 tag."""
    base_kst = datetime(2026, 6, 10, 14, 0, tzinfo=KST)
    pinned = {"now": base_kst}
    monkeypatch.setattr(trading_router, "_now_kst", lambda: pinned["now"])
    monkeypatch.setattr(trading_router, "_is_session_day_safe", lambda m, d: True)
    def _raise(*a, **k):
        raise trading_router.mc.CalendarError("test")
    monkeypatch.setattr(trading_router.mc, "next_session_kst", _raise)

    cycle_kst = datetime(2026, 6, 10, 8, 55, tzinfo=KST)

    def seed(s, user, device):
        s.add(SyncSnapshot(
            user_id=user.id, device_id=device.id,
            payload={"cycle_summary": {"market": "KRX", "n_bought": 1, "n_sold": 0,
                                       "today": "2026-06-10"}},
            received_at=_kst_to_naive_utc(cycle_kst)))

    client, engine = _build(seed)

    r1 = client.get("/trading/timeline")
    assert r1.status_code == 200, r1.text
    etag1 = r1.headers.get("ETag")
    assert etag1, r1.headers

    # 매칭 → 304
    r2 = client.get("/trading/timeline", headers={"If-None-Match": etag1})
    assert r2.status_code == 304

    # bucket 경과(301s) → 같은 데이터라도 새 tag (staleness guard)
    pinned["now"] = base_kst + timedelta(seconds=301)
    r3 = client.get("/trading/timeline", headers={"If-None-Match": etag1})
    assert r3.status_code == 200, r3.text
    etag3 = r3.headers.get("ETag")
    assert etag3 and etag3 != etag1

    # 새 snapshot 추가(시간 미경과) → data_ms 변동 → tag 변동
    pinned["now"] = base_kst
    with Session(engine) as s:
        s.add(SyncSnapshot(
            user_id=1, device_id=1,
            payload={"cycle_summary": {"market": "KRX", "n_bought": 0, "n_sold": 0}},
            received_at=_kst_to_naive_utc(base_kst - timedelta(minutes=1))))
        s.commit()
    r4 = client.get("/trading/timeline", headers={"If-None-Match": etag1})
    assert r4.status_code == 200, r4.text
    assert r4.headers.get("ETag") != etag1


# ── 4. /sync/snapshot — 304 경로에서 payload 미열람 (D3) ───────────────────────

def test_snapshot_304_reads_no_payload():
    """/sync/snapshot 304 경로에서 payload 컬럼을 SELECT하지 않는다(scalar tag-first)."""
    snap_kst = datetime(2026, 6, 10, 8, 55, tzinfo=KST)

    def seed(s, user, device):
        s.add(SyncSnapshot(
            user_id=user.id, device_id=device.id,
            payload={"cycle_summary": {"market": "KRX"}, "equity": list(range(500))},
            received_at=_kst_to_naive_utc(snap_kst)))

    client, engine = _build(seed)
    r1 = client.get("/sync/snapshot")
    assert r1.status_code == 200, r1.text
    etag = r1.headers.get("ETag")
    assert etag and etag.startswith('W/"') and etag[3:-1].rstrip('"').split('"')[0]
    # 포맷: W/"<digits>"
    import re
    assert re.fullmatch(r'W/"\d+"', etag), etag

    captured = _capture_sql(engine)
    r2 = client.get("/sync/snapshot", headers={"If-None-Match": etag})
    assert r2.status_code == 304

    joined = "\n".join(captured).lower()
    assert "payload" not in joined, joined


# ── 5. /sync/snapshot — 200 응답 shape 무변경 ─────────────────────────────────

def test_snapshot_200_shape_unchanged():
    """fresh GET이 payload·received_at·device_id·last_heartbeat_at를 그대로 반환."""
    snap_kst = datetime(2026, 6, 10, 8, 55, tzinfo=KST)
    hb = datetime(2026, 6, 10, 14, 0, tzinfo=timezone.utc)
    payload = {"cycle_summary": {"market": "KRX", "n_bought": 3}}

    def seed(s, user, device):
        s.add(SyncSnapshot(
            user_id=user.id, device_id=device.id, payload=payload,
            received_at=_kst_to_naive_utc(snap_kst)))
        s.add(UserSettings(user_id=user.id, last_heartbeat_at=hb))

    client, engine = _build(seed)
    r = client.get("/sync/snapshot")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["payload"] == payload
    assert body["device_id"] == 1
    assert body["received_at"] is not None
    assert body["last_heartbeat_at"] is not None

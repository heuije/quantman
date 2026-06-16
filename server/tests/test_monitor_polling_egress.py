"""Monitor 폴링 egress 절감 — devices·commands·market/context tag-first ETag 회귀 테스트.

핸드오프 #5(docs/handoff/2026-06-12-server-infra-handoff.md): Monitor 페이지가 15s마다
폴링하는 4개 GET 중 /sync/snapshot만 ETag가 있고 /auth/devices·/sync/commands·
/market/context는 무조건 full body 응답 → egress 잔존 표면. PR#75/#76과 동일한
tag-first 패턴(scalar 먼저 계산 → If-None-Match 매칭 시 304, 큰 payload 미조회)을
세 endpoint에 고정한다. test_timeline_egress.py 관례 미러.

자기완결: 인메모리 SQLite + 최소 FastAPI 앱. 네트워크·lifespan 없음.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from app.models import Command, Device, User
from app.routers import auth as auth_router
from app.routers import commands as commands_router
from app.routers import market as market_router


# ── 픽스처 ───────────────────────────────────────────────────────────────────

def _build(seed):
    """인메모리 DB + auth·commands·market 라우터 + 시드 유저. (TestClient, engine) 반환.

    seed(session, user) 콜백으로 device·command를 시드한다.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="t@example.com")
        s.add(user); s.commit(); s.refresh(user)
        seed(s, user)
        s.commit()

    app = FastAPI()
    app.include_router(auth_router.router)
    app.include_router(commands_router.router)
    app.include_router(market_router.router)

    def _override_session():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _override_session

    with Session(engine) as s:
        seeded_user = s.exec(sqlalchemy.select(User)).scalars().first()
    app.dependency_overrides[get_current_user] = lambda: seeded_user

    return TestClient(app), engine


def _capture_sql(engine):
    """engine에 실행되는 SQL 문자열을 수집하는 list 반환 + 리스너 등록."""
    captured: list[str] = []

    @sqlalchemy.event.listens_for(engine, "before_cursor_execute")
    def _on_exec(conn, cursor, statement, parameters, context, executemany):  # noqa
        captured.append(statement)

    return captured


# ── 1. /auth/devices — tag-first ETag + 304 경로 full row 미조회 ───────────────

def test_devices_etag_304_and_no_row_select():
    """200+ETag → If-None-Match 매칭 시 304(본문 0) + device full row(token_hash) 미SELECT."""
    base = datetime(2026, 6, 9, 12, 0, 0)

    def seed(s, user):
        s.add(Device(user_id=user.id, name="PC-A", token_hash="ha",
                     last_seen_at=base - timedelta(hours=1)))
        s.add(Device(user_id=user.id, name="PC-B", token_hash="hb",
                     last_seen_at=base))

    client, engine = _build(seed)

    r1 = client.get("/auth/devices")
    assert r1.status_code == 200, r1.text
    etag = r1.headers.get("ETag")
    assert etag and etag.startswith('W/"'), r1.headers
    # 200 본문 의미 무변경 — last_seen desc 정렬·필드 그대로 (test_devices_order 불변식)
    rows = r1.json()
    assert [r["name"] for r in rows] == ["PC-B", "PC-A"]
    assert {"id", "name", "created_at", "last_seen_at"} <= set(rows[0].keys())

    captured = _capture_sql(engine)
    r2 = client.get("/auth/devices", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""
    # tag-first 증거 — 304 경로는 aggregate scalar만 조회, full row(token_hash 포함) 미열람
    joined = "\n".join(captured).lower()
    assert "token_hash" not in joined, joined


def test_devices_etag_changes_on_activity_and_membership():
    """last_seen 갱신·기기 추가·기기 삭제가 각각 새 ETag → 200 (stale 304 방지)."""
    base = datetime(2026, 6, 9, 12, 0, 0)

    def seed(s, user):
        s.add(Device(user_id=user.id, name="PC-A", token_hash="ha", last_seen_at=base))

    client, engine = _build(seed)
    etag1 = client.get("/auth/devices").headers["ETag"]

    # last_seen_at 전진(heartbeat 인증 갱신) → 새 tag
    with Session(engine) as s:
        d = s.exec(sqlalchemy.select(Device)).scalars().first()
        d.last_seen_at = base + timedelta(minutes=5)
        s.add(d); s.commit()
    r = client.get("/auth/devices", headers={"If-None-Match": etag1})
    assert r.status_code == 200
    etag2 = r.headers["ETag"]
    assert etag2 != etag1

    # 기기 추가(페어링) → 새 tag
    with Session(engine) as s:
        s.add(Device(user_id=1, name="PC-B", token_hash="hb", last_seen_at=None))
        s.commit()
    r = client.get("/auth/devices", headers={"If-None-Match": etag2})
    assert r.status_code == 200
    etag3 = r.headers["ETag"]
    assert etag3 != etag2

    # 기기 삭제(revoke) → 새 tag
    with Session(engine) as s:
        d = s.exec(sqlalchemy.select(Device).where(Device.name == "PC-B")).scalars().first()
        s.delete(d); s.commit()
    r = client.get("/auth/devices", headers={"If-None-Match": etag3})
    assert r.status_code == 200
    assert r.headers["ETag"] != etag3


# ── 2. /sync/commands — tag-first ETag + 304 경로 params/result 미조회 ─────────

_JUNK = {"big": ["x" * 50] * 50}     # full-row SELECT 회귀 검출용 무거운 JSON


def test_commands_etag_304_and_no_payload_select():
    """200+ETag → 매칭 시 304 + params/result JSON 컬럼 미SELECT (scalar tag-first)."""
    base = datetime(2026, 6, 9, 12, 0, 0)

    def seed(s, user):
        dev = Device(user_id=user.id, name="PC", token_hash="h")
        s.add(dev); s.commit(); s.refresh(dev)
        s.add(Command(user_id=user.id, device_id=dev.id, type="PAUSE_AUTO",
                      params=_JUNK, result=_JUNK, status="done",
                      created_at=base, delivered_at=base, completed_at=base))

    client, engine = _build(seed)

    r1 = client.get("/sync/commands")
    assert r1.status_code == 200, r1.text
    etag = r1.headers.get("ETag")
    assert etag and etag.startswith('W/"'), r1.headers
    # 200 본문 의미 무변경
    rows = r1.json()
    assert len(rows) == 1 and rows[0]["type"] == "PAUSE_AUTO"
    assert rows[0]["params"] == _JUNK and rows[0]["result"] == _JUNK

    captured = _capture_sql(engine)
    r2 = client.get("/sync/commands", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""
    joined = "\n".join(captured).lower()
    assert "params" not in joined and "result" not in joined, joined


def test_commands_etag_changes_on_create_and_status():
    """명령 생성·delivered 전이·done ack가 각각 새 ETag → 200 (stale 304 방지)."""
    base = datetime(2026, 6, 9, 12, 0, 0)

    def seed(s, user):
        dev = Device(user_id=user.id, name="PC", token_hash="h")
        s.add(dev); s.commit(); s.refresh(dev)
        s.add(Command(user_id=user.id, device_id=dev.id, type="PAUSE_AUTO",
                      status="pending", created_at=base))

    client, engine = _build(seed)
    etag1 = client.get("/sync/commands").headers["ETag"]

    # pending → delivered (delivered_at 세팅 — SSE/poll pickup)
    with Session(engine) as s:
        c = s.exec(sqlalchemy.select(Command)).scalars().first()
        c.status = "delivered"
        c.delivered_at = base + timedelta(seconds=30)
        s.add(c); s.commit()
    r = client.get("/sync/commands", headers={"If-None-Match": etag1})
    assert r.status_code == 200
    etag2 = r.headers["ETag"]
    assert etag2 != etag1

    # delivered → done (completed_at + result — ack)
    with Session(engine) as s:
        c = s.exec(sqlalchemy.select(Command)).scalars().first()
        c.status = "done"
        c.completed_at = base + timedelta(minutes=1)
        c.result = {"ok": True}
        s.add(c); s.commit()
    r = client.get("/sync/commands", headers={"If-None-Match": etag2})
    assert r.status_code == 200
    etag3 = r.headers["ETag"]
    assert etag3 != etag2

    # 새 명령 생성 → 새 tag
    with Session(engine) as s:
        s.add(Command(user_id=1, device_id=1, type="RESUME_AUTO",
                      status="pending", created_at=base + timedelta(minutes=2)))
        s.commit()
    r = client.get("/sync/commands", headers={"If-None-Match": etag3})
    assert r.status_code == 200
    assert r.headers["ETag"] != etag3


def test_commands_etag_filter_scoped():
    """only_pending 필터별로 tag가 분리 — pending 이탈이 pending 뷰 tag를 바꾼다."""
    base = datetime(2026, 6, 9, 12, 0, 0)

    def seed(s, user):
        dev = Device(user_id=user.id, name="PC", token_hash="h")
        s.add(dev); s.commit(); s.refresh(dev)
        s.add(Command(user_id=user.id, device_id=dev.id, type="PAUSE_AUTO",
                      status="pending", created_at=base))
        s.add(Command(user_id=user.id, device_id=dev.id, type="RESUME_AUTO",
                      status="done", created_at=base - timedelta(minutes=5),
                      completed_at=base - timedelta(minutes=4)))

    client, engine = _build(seed)
    r_all = client.get("/sync/commands")
    r_pending = client.get("/sync/commands?only_pending=true")
    assert len(r_all.json()) == 2 and len(r_pending.json()) == 1
    etag_pending = r_pending.headers["ETag"]

    # pending 명령이 done으로 이탈 → pending 뷰 멤버십 변화 → 새 tag (count 변동)
    with Session(engine) as s:
        c = s.exec(sqlalchemy.select(Command)
                   .where(Command.status == "pending")).scalars().first()
        c.status = "done"
        c.completed_at = base + timedelta(minutes=1)
        s.add(c); s.commit()
    r = client.get("/sync/commands?only_pending=true",
                   headers={"If-None-Match": etag_pending})
    assert r.status_code == 200
    assert r.json() == []
    assert r.headers["ETag"] != etag_pending


# ── 3. /market/context — tag-first ETag (304 경로 dataset 미로드) ──────────────

def _market_stub(monkeypatch, gen, phase):
    """data_generation·load_dataset_for·_session_now를 결정적으로 고정.

    load 호출 횟수를 세는 counter dict 반환 — 304 경로 dataset 미로드 증거.
    """
    import pandas as pd

    counter = {"loads": 0}

    def fake_load(cands, with_indicators=True):
        counter["loads"] += 1
        idx = pd.to_datetime(["2026-06-09", "2026-06-10"])
        return {"KOSPI": pd.DataFrame({"Close": [1.0, 2.0]}, index=idx)}

    monkeypatch.setattr(market_router.data_fetcher, "data_generation",
                        lambda: gen["v"])
    monkeypatch.setattr(market_router.qc, "load_dataset_for", fake_load)
    monkeypatch.setattr(market_router, "_session_now",
                        lambda: {"phase": phase["v"], "kst_now": "2026-06-10T10:00:00+09:00"})
    return counter


def test_market_context_etag_304_skips_dataset_load(monkeypatch):
    """매칭 304 경로에서 load_dataset_for를 아예 호출하지 않는다(tag-first)."""
    gen, phase = {"v": 111}, {"v": "정규장"}

    client, engine = _build(lambda s, user: None)
    counter = _market_stub(monkeypatch, gen, phase)

    r1 = client.get("/market/context")
    assert r1.status_code == 200, r1.text
    etag = r1.headers.get("ETag")
    assert etag and etag.startswith('W/"'), r1.headers
    assert counter["loads"] == 1
    # 200 본문 의미 무변경 — indicators + session.phase
    body = r1.json()
    kospi = [i for i in body["indicators"] if i["label"] == "KOSPI"][0]
    assert kospi["available"] is True and kospi["value"] == 2.0
    assert body["session"]["phase"] == "정규장"

    r2 = client.get("/market/context", headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.content == b""
    assert counter["loads"] == 1      # 304 경로 — dataset 로드 0회


def test_market_context_etag_changes_on_generation_and_phase(monkeypatch):
    """데이터 세대 bump(cron 갱신)·세션 phase 전이가 각각 새 ETag → 200."""
    gen, phase = {"v": 111}, {"v": "정규장"}

    client, engine = _build(lambda s, user: None)
    counter = _market_stub(monkeypatch, gen, phase)

    etag1 = client.get("/market/context").headers["ETag"]

    gen["v"] = 222           # naver 15분 cron 등 데이터 쓰기 → mark_data_dirty
    r = client.get("/market/context", headers={"If-None-Match": etag1})
    assert r.status_code == 200
    etag2 = r.headers["ETag"]
    assert etag2 != etag1
    assert counter["loads"] == 2

    phase["v"] = "장 종료"    # 세션 전이 — 데이터 동일해도 phase 배지가 바뀌어야 함
    r = client.get("/market/context", headers={"If-None-Match": etag2})
    assert r.status_code == 200
    assert r.headers["ETag"] != etag2

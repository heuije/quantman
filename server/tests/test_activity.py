"""활동 계측(POST /activity) + 운영자 대시보드 인기종목·화면별 사용량 집계 테스트.

HERMETIC: in-memory SQLite. 프론트 라우트 비컨이 적재하는 ActivityEvent가
① 엔드포인트로 정상 저장되고 ② compute_admin_metrics의 top_symbols·screen_usage·
활성유저 집계에 반영되는지 검증.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.admin_metrics import compute_admin_metrics
from app.db import get_session
from app.models import ActivityEvent, User
from app.routers import activity as activity_router
from app.security import create_access_token


def _engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    return e


def _client(engine):
    with Session(engine) as s:
        u = User(email="u@example.com"); s.add(u); s.commit(); s.refresh(u)
        uid = u.id
    app = FastAPI()
    app.include_router(activity_router.router)

    def _ov():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _ov
    return TestClient(app), create_access_token(uid), uid


def test_log_activity_persists_row():
    eng = _engine()
    client, jwt, uid = _client(eng)
    r = client.post("/activity", json={"path": "/dashboard", "symbol": "005930"},
                    headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code == 200 and r.json()["ok"] is True
    with Session(eng) as s:
        rows = s.exec(select(ActivityEvent)).all()
    assert len(rows) == 1
    assert rows[0].user_id == uid and rows[0].path == "/dashboard"
    assert rows[0].symbol == "005930"       # 대문자 정규화


def test_log_activity_blank_path_ignored():
    eng = _engine()
    client, jwt, _ = _client(eng)
    r = client.post("/activity", json={"path": "   "},
                    headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code == 200 and r.json()["ok"] is False
    with Session(eng) as s:
        assert s.exec(select(ActivityEvent)).all() == []


def test_log_activity_requires_auth():
    eng = _engine()
    client, _jwt, _ = _client(eng)
    r = client.post("/activity", json={"path": "/dashboard"})   # 토큰 없음
    assert r.status_code == 401


def test_metrics_top_symbols_and_screen_usage():
    now = datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc)
    eng = _engine()
    with Session(eng) as s:
        u = User(email="a@example.com", created_at=now - timedelta(days=1))
        s.add(u); s.commit(); s.refresh(u)
        # 종목 조회: 005930 ×3, 000660 ×1 / 화면: /dashboard ×4, /chat ×2
        for _ in range(3):
            s.add(ActivityEvent(user_id=u.id, path="/dashboard", symbol="005930",
                                at=now - timedelta(hours=2)))
        s.add(ActivityEvent(user_id=u.id, path="/dashboard", symbol="000660",
                            at=now - timedelta(hours=3)))
        for _ in range(2):
            s.add(ActivityEvent(user_id=u.id, path="/chat", at=now - timedelta(hours=1)))
        s.commit()

        m = compute_admin_metrics(s, now=now, days=30)

    # 인기 종목 — 조회수 내림차순
    assert m["top_symbols"] == [{"symbol": "005930", "views": 3},
                                {"symbol": "000660", "views": 1}]
    # 화면별 사용량 — path별 카운트(종목조회도 /dashboard 4건)
    usage = {r["path"]: r["views"] for r in m["screen_usage"]}
    assert usage == {"/dashboard": 4, "/chat": 2}
    # ActivityEvent도 활성 신호 — 이 유저는 오늘 활동했으니 DAU=1
    assert m["active_users"]["dau"] == 1


def test_metrics_empty_activity_ok():
    now = datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc)
    with Session(_engine()) as s:
        m = compute_admin_metrics(s, now=now, days=7)
    assert m["top_symbols"] == [] and m["screen_usage"] == []

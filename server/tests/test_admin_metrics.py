"""운영자 대시보드 집계(compute_admin_metrics) + /admin 게이트 단위 테스트.

전 테스트 HERMETIC: in-memory SQLite. 고정 now를 주입해 롤링 윈도우·KST 일별 버킷을
결정적으로 검증한다.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.admin_metrics import compute_admin_metrics
from app.db import get_session
from app.models import (BacktestRun, ChatTurnMetric, CompileLog, Conversation,
                        Device, HeartbeatEvent, Message, Strategy, User)
from app.routers import admin as admin_router
from app.security import create_access_token


def _engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    return e


def test_compute_admin_metrics_empty():
    now = datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc)
    with Session(_engine()) as s:
        m = compute_admin_metrics(s, now=now, days=7)
    assert m["totals"]["users"] == 0
    assert m["totals"]["backtests"] == 0
    assert m["active_users"] == {"dau": 0, "wau": 0, "mau": 0}
    assert m["users"] == []
    assert len(m["daily"]) == 7
    assert all(d["signups"] == 0 and d["active_users"] == 0 for d in m["daily"])
    assert m["daily"][-1]["date"] == "2026-07-11"   # KST 오늘(now=KST 12:00)


def test_compute_admin_metrics_aggregates():
    now = datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc)   # KST 2026-07-11 12:00
    eng = _engine()
    with Session(eng) as s:
        # 유저 3 — password / google / naver, 가입 시점 상이
        u1 = User(email="a@example.com", created_at=now - timedelta(hours=2))
        u2 = User(email="b@example.com", google_sub="g1", created_at=now - timedelta(days=3))
        u3 = User(email="c@example.com", naver_sub="n1", created_at=now - timedelta(days=40))
        s.add_all([u1, u2, u3]); s.commit()
        for u in (u1, u2, u3):
            s.refresh(u)
        # 전략: u1 live+draft, u2 paper
        s.add_all([
            Strategy(user_id=u1.id, name="s1", run_mode="live"),
            Strategy(user_id=u1.id, name="s2", run_mode="draft"),
            Strategy(user_id=u2.id, name="s3", run_mode="paper"),
        ])
        # 자동매매 연동 기기: u1 1대(오늘 접속)
        s.add(Device(user_id=u1.id, name="pc", token_hash="h1",
                     last_seen_at=now - timedelta(hours=1)))
        # 백테스트: u1 오늘 2건, u2 5일 전 1건
        s.add_all([
            BacktestRun(user_id=u1.id, created_at=now - timedelta(hours=1)),
            BacktestRun(user_id=u1.id, created_at=now - timedelta(hours=3)),
            BacktestRun(user_id=u2.id, created_at=now - timedelta(days=5)),
        ])
        # 챗봇 턴: u1 오늘 1, u2 2일 전 1 (conversation FK 필요)
        c1 = Conversation(user_id=u1.id); c2 = Conversation(user_id=u2.id)
        s.add_all([c1, c2]); s.commit(); s.refresh(c1); s.refresh(c2)
        s.add_all([
            ChatTurnMetric(conversation_id=c1.id, user_id=u1.id,
                           created_at=now - timedelta(hours=2)),
            ChatTurnMetric(conversation_id=c2.id, user_id=u2.id,
                           created_at=now - timedelta(days=2)),
        ])
        # 전략 컴파일: u1 오늘 1
        s.add(CompileLog(user_id=u1.id, created_at=now - timedelta(hours=4)))
        # heartbeat: u3 40일 전(비활성 — mau 밖)
        s.add(HeartbeatEvent(user_id=u3.id, at=now - timedelta(days=40)))
        s.commit()

        m = compute_admin_metrics(s, now=now, days=30)

    t = m["totals"]
    assert t["users"] == 3
    assert t["strategies"] == 3 and t["live_strategies"] == 1 and t["paper_strategies"] == 1
    assert t["devices"] == 1
    assert t["backtests"] == 3 and t["chat_turns"] == 2 and t["compiles"] == 1

    assert m["auth_breakdown"] == {"google": 1, "naver": 1, "password": 1}

    # 활성(롤링·**사람 행동만**): dau=u1만(24h 백테스트) / wau=u1+u2 / mau=u1+u2.
    # u3는 heartbeat(기계)뿐이라 사람 활동 없음 — 어디에도 안 잡힘.
    assert m["active_users"] == {"dau": 1, "wau": 2, "mau": 2}
    # 가입: 24h=u1 / 7d=u1+u2 / 30d=u1+u2(u3 40일 제외)
    assert m["signups"] == {"last_24h": 1, "last_7d": 2, "last_30d": 2}

    # 일별(KST) — 오늘 버킷
    assert len(m["daily"]) == 30
    today = m["daily"][-1]
    assert today["date"] == "2026-07-11"
    assert today["backtests"] == 2 and today["chat_turns"] == 1
    assert today["signups"] == 1 and today["active_users"] == 1

    # 유저별 롤업
    by_email = {u["email"]: u for u in m["users"]}
    a = by_email["a@example.com"]
    assert a["backtests"] == 2 and a["strategies"] == 2 and a["live_strategies"] == 1
    assert a["devices"] == 1 and a["chat_turns"] == 1 and a["auth"] == "password"
    assert by_email["b@example.com"]["auth"] == "google"
    # 사람/기계 분리 — u3는 heartbeat(기계)만: 최근활동 없음·로컬앱 시각은 별도 기록.
    # 자동매매만 켜둔 유저가 "방금 활동"으로 표시되던 오해의 회귀 가드.
    c_row = by_email["c@example.com"]
    assert c_row["last_active_at"] is None and c_row["local_app_at"] is not None
    # u1은 기기 sync(1h 전)가 로컬앱 시각으로 잡힘
    assert a["local_app_at"] is not None
    # 최근활동순 — u1이 가장 최근(1h 전)
    assert m["users"][0]["email"] == "a@example.com"


def _build_gate_app():
    """admin 라우터 + 인메모리 DB + 운영자/일반 유저 시드. (client, admin_jwt, normal_jwt)."""
    eng = _engine()
    with Session(eng) as s:
        admin = User(email="test@mystock.io")     # ADMIN_EMAILS 기본 포함
        normal = User(email="joe@example.com")
        s.add_all([admin, normal]); s.commit()
        s.refresh(admin); s.refresh(normal)
        admin_id, normal_id = admin.id, normal.id
    app = FastAPI()
    app.include_router(admin_router.router)

    def _ov():
        with Session(eng) as s:
            yield s
    app.dependency_overrides[get_session] = _ov
    return (TestClient(app), create_access_token(admin_id),
            create_access_token(normal_id))


def test_admin_metrics_gate_blocks_non_admin():
    client, _admin_jwt, normal_jwt = _build_gate_app()
    r = client.get("/admin/metrics", headers={"Authorization": f"Bearer {normal_jwt}"})
    assert r.status_code == 403


def test_admin_metrics_gate_allows_admin():
    client, admin_jwt, _normal_jwt = _build_gate_app()
    r = client.get("/admin/metrics", headers={"Authorization": f"Bearer {admin_jwt}"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"totals", "active_users", "signups", "daily", "users"}
    assert body["totals"]["users"] == 2   # 시드 2명


def test_admin_metrics_requires_auth():
    client, _a, _n = _build_gate_app()
    r = client.get("/admin/metrics")     # 토큰 없음
    assert r.status_code == 401


# ── 챗봇 입력 내역 ──────────────────────────────────────────────────────────

def test_recent_chat_inputs_pairs_question_answer():
    from app.admin_metrics import recent_chat_inputs
    eng = _engine()
    with Session(eng) as s:
        u = User(email="a@example.com"); s.add(u); s.commit(); s.refresh(u)
        c = Conversation(user_id=u.id); s.add(c); s.commit(); s.refresh(c)
        s.add(Message(conversation_id=c.id, role="user",
                      parts=[{"type": "text", "text": "삼성전자 어때?"}]))
        s.add(Message(conversation_id=c.id, role="assistant",
                      parts=[{"type": "tool_use", "id": "t", "name": "x", "input": {}},
                             {"type": "text", "text": "저평가입니다."}]))
        s.add(Message(conversation_id=c.id, role="user",
                      parts=[{"type": "text", "text": "SK하이닉스는?"}]))
        s.commit()
        rows = recent_chat_inputs(s, limit=10)
    # 최신순 — 질문2 먼저
    assert [r["question"] for r in rows] == ["SK하이닉스는?", "삼성전자 어때?"]
    assert rows[0]["answer"] is None                    # 질문2는 아직 답변 없음
    assert rows[1]["answer"] == "저평가입니다."          # 도구 블록 제외, 텍스트만
    assert all(r["email"] == "a@example.com" for r in rows)


def test_recent_chat_inputs_empty():
    from app.admin_metrics import recent_chat_inputs
    with Session(_engine()) as s:
        assert recent_chat_inputs(s, limit=10) == []


def test_admin_chat_log_gate():
    client, admin_jwt, normal_jwt = _build_gate_app()
    assert client.get("/admin/chat-log",
                      headers={"Authorization": f"Bearer {normal_jwt}"}).status_code == 403
    r = client.get("/admin/chat-log", headers={"Authorization": f"Bearer {admin_jwt}"})
    assert r.status_code == 200 and "messages" in r.json()

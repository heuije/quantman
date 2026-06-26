"""자연어 전략 빌더(NL→IR 컴파일) 일일 사용량 제한 + admin 언락.

요구사항:
  · 인당 1일 10회 제한 (KST 경계). 초과 시 LLM 호출 없이 차단.
  · admin 비밀번호(config) 일치 시 50회로 상향.
  · 카운트는 모든 변환 시도(성공·실패 무관) — 기존 CompileLog row 재사용.
  · GET 아닌 quota 엔드포인트로 잔여·언락 상태 조회(비번 버튼 즉시 검증·카운터 표시).

LLM은 호출하지 않는다 — compile_nl을 monkeypatch로 대체(비용·비결정 제거).
인메모리 SQLite + get_current_user/get_session override로 인증·DB 격리.

    cd platform/server && python -m pytest tests/test_ir_compile_rate_limit.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

import app.compile_service as compile_service_mod
from app.config import settings
from app.db import get_session
from app.deps import get_current_user
from app.models import CompileLog, User
from app.routers import ir_compile as ir_compile_router

KST = ZoneInfo("Asia/Seoul")

# compile_nl을 대체할 결정적 stub — 항상 성공한 단순 IR 반환(LLM 호출 없음).
_FAKE_IR = {
    "name": "테스트", "universe": {"kind": "single", "symbols": ["005930"]},
    "signal": {"op": "const", "params": {"value": 1}},
    "position": {"direction": "long", "sizing": {"mode": "pct_cash", "amount_pct": 10},
                 "entry": {"mode": "on_signal"}, "exit": {}, "overlays": {}},
    "simulation": {},
}


def _fake_compile(*a, **k):
    return {"success": True, "ir": _FAKE_IR, "assumptions": [], "issues": [],
            "repair_count": 0}


def _build(monkeypatch, *, seed_today=0, seed_yesterday=0):
    """인메모리 DB + ir_compile 라우터 + 유저 1명. (client, engine, user_id) 반환.

    seed_today/seed_yesterday: 미리 적재할 CompileLog 수(KST 오늘/어제).
    """
    monkeypatch.setattr(compile_service_mod, "compile_nl", _fake_compile)
    # explain_ir은 성공 IR에 대해 호출되나 본 테스트 관심 밖 — 가벼운 stub.
    monkeypatch.setattr(compile_service_mod, "explain_ir",
                        lambda *a, **k: None)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    now_kst = datetime.now(KST)
    kst_midnight = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    with Session(engine) as s:
        user = User(email="t@example.com")
        s.add(user); s.commit(); s.refresh(user)
        uid = user.id
        # 오늘 분 — 정오 KST로 적재(경계 안전)
        for _ in range(seed_today):
            s.add(CompileLog(user_id=uid, nl_input="x",
                             created_at=(kst_midnight + timedelta(hours=12)).astimezone(timezone.utc)))
        # 어제 분 — 어제 정오 KST(오늘 카운트에 들어가면 안 됨)
        for _ in range(seed_yesterday):
            s.add(CompileLog(user_id=uid, nl_input="x",
                             created_at=(kst_midnight - timedelta(hours=12)).astimezone(timezone.utc)))
        s.commit()

    app = FastAPI()
    app.include_router(ir_compile_router.router)

    def _override_session():
        with Session(engine) as s:
            yield s

    def _override_user():
        with Session(engine) as s:
            return s.get(User, uid)

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_user] = _override_user
    return TestClient(app), engine, uid


def _count_logs(engine, uid) -> int:
    with Session(engine) as s:
        from sqlmodel import select
        return len(s.exec(select(CompileLog).where(CompileLog.user_id == uid)).all())


# ── 제한 강제 ───────────────────────────────────────────────────────────────

def test_under_limit_compiles_and_reports_quota(monkeypatch):
    """한도 미만이면 컴파일 진행 + 응답에 used/limit/remaining."""
    client, engine, uid = _build(monkeypatch, seed_today=3)
    r = client.post("/ir/compile", json={"nl": "삼성전자 사기"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    assert body["limit"] == settings.NL_COMPILE_DAILY_LIMIT
    assert body["used"] == 4          # 기존 3 + 이번 1
    assert body["remaining"] == settings.NL_COMPILE_DAILY_LIMIT - 4
    assert body.get("rate_limited") in (False, None)
    assert _count_logs(engine, uid) == 4   # 새 로그 1건 기록됨


def test_at_limit_blocks_without_calling_llm(monkeypatch):
    """오늘 10건이면 차단 — compile_nl 호출 안 됨, 새 로그도 안 쌓임."""
    called = {"n": 0}
    def _counting_compile(*a, **k):
        called["n"] += 1
        return _fake_compile()
    monkeypatch.setattr(compile_service_mod, "compile_nl", _counting_compile)

    client, engine, uid = _build(monkeypatch, seed_today=10)
    # _build이 다시 _fake_compile로 덮으므로 카운터를 재주입
    monkeypatch.setattr(compile_service_mod, "compile_nl", _counting_compile)

    r = client.post("/ir/compile", json={"nl": "또 하나"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rate_limited"] is True
    assert body["success"] is False
    assert body["used"] == 10 and body["limit"] == 10 and body["remaining"] == 0
    assert called["n"] == 0, "한도 초과인데 LLM이 호출됨"
    assert _count_logs(engine, uid) == 10, "차단인데 로그가 추가됨"


def test_yesterday_logs_do_not_count(monkeypatch):
    """어제(KST) 로그는 오늘 한도에 포함되지 않는다."""
    client, engine, uid = _build(monkeypatch, seed_today=2, seed_yesterday=20)
    r = client.post("/ir/compile", json={"nl": "오늘 첫 변환은 아님"})
    body = r.json()
    assert body["success"] is True
    assert body["used"] == 3, body          # 오늘 2 + 1 (어제 20 무관)


# ── admin 언락 ──────────────────────────────────────────────────────────────

def test_admin_password_raises_limit(monkeypatch):
    """올바른 admin 비번 → 한도 50. 오늘 10건이어도 통과."""
    client, engine, uid = _build(monkeypatch, seed_today=10)
    r = client.post("/ir/compile",
                    json={"nl": "관리자 변환", "admin_password": settings.NL_COMPILE_ADMIN_PASSWORD})
    body = r.json()
    assert body["success"] is True, body
    assert body["limit"] == settings.NL_COMPILE_ADMIN_LIMIT
    assert body["used"] == 11
    assert body.get("admin_unlocked") is True


def test_wrong_admin_password_stays_limited(monkeypatch):
    """틀린 비번 → 한도 10 유지 → 10건이면 차단."""
    client, engine, uid = _build(monkeypatch, seed_today=10)
    r = client.post("/ir/compile",
                    json={"nl": "사칭", "admin_password": "wrong"})
    body = r.json()
    assert body["rate_limited"] is True
    assert body["limit"] == settings.NL_COMPILE_DAILY_LIMIT
    assert body.get("admin_unlocked") is False


# ── quota 엔드포인트 (비번 버튼 즉시 검증 + 카운터) ──────────────────────────

def test_quota_endpoint_reports_usage(monkeypatch):
    """POST /ir/compile/quota — 컴파일 없이 used/limit/remaining 반환."""
    client, engine, uid = _build(monkeypatch, seed_today=4)
    r = client.post("/ir/compile/quota", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["used"] == 4
    assert body["limit"] == settings.NL_COMPILE_DAILY_LIMIT
    assert body["remaining"] == settings.NL_COMPILE_DAILY_LIMIT - 4
    assert body["admin_unlocked"] is False


def test_quota_endpoint_validates_admin_password(monkeypatch):
    """quota에 올바른 비번 → admin_unlocked True + limit 50 (컴파일 소모 없음)."""
    client, engine, uid = _build(monkeypatch, seed_today=4)
    r = client.post("/ir/compile/quota",
                    json={"admin_password": settings.NL_COMPILE_ADMIN_PASSWORD})
    body = r.json()
    assert body["admin_unlocked"] is True
    assert body["limit"] == settings.NL_COMPILE_ADMIN_LIMIT
    assert body["remaining"] == settings.NL_COMPILE_ADMIN_LIMIT - 4
    assert _count_logs(engine, uid) == 4, "quota 조회가 사용량을 소모하면 안 됨"


def test_quota_endpoint_rejects_wrong_password(monkeypatch):
    """quota에 틀린 비번 → admin_unlocked False + limit 10."""
    client, engine, uid = _build(monkeypatch, seed_today=4)
    r = client.post("/ir/compile/quota", json={"admin_password": "nope"})
    body = r.json()
    assert body["admin_unlocked"] is False
    assert body["limit"] == settings.NL_COMPILE_DAILY_LIMIT


# ── 모델 기본값 ─────────────────────────────────────────────────────────────

def test_default_model_is_sonnet():
    """NL 컴파일 기본 모델이 Sonnet 4.6으로 설정됨(env 미설정 시)."""
    import os
    if os.getenv("QP_NL_COMPILE_MODEL"):
        import pytest
        pytest.skip("env override 설정됨 — 기본값 테스트 불가")
    assert settings.NL_COMPILE_MODEL == "claude-sonnet-4-6"

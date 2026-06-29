"""P0 회귀 — 로컬앱 preview pull 인증 경로.

버그: 로컬앱(디바이스 토큰)이 유저 전용 `/preview/next-day`(get_current_user, JWT)를
호출해 항상 401 → "preview 없음 → 신규 진입 0 → 청산만". 자동 매수가 구조적으로 차단됨.

수정(Fix A): 디바이스 인증 `/sync/preview` 신설 + 로컬앱 repoint. 역할 분리 유지
(디바이스=/sync/*·디바이스 토큰, 웹=/preview/*·유저 JWT).

네트워크/lifespan 없이 인메모리 SQLite + 최소 앱으로 인증 분리를 양·음성 검증.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

# server 디렉터리를 path에 추가 — `import app.*`
_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.db import get_session
from app.models import Device, SyncSnapshot, User
from app.routers import preview as preview_router
from app.routers import sync as sync_router
from app.security import create_access_token, hash_token

_DEVICE_TOKEN = "test-device-token-abc"
_PREVIEW = {
    "available": True,
    "generated_at": "2026-05-23T09:00:00",
    "data_source": "test",
    "summary": {"n_buy_candidates": 1, "est_total_buy_amount": 1000.0,
                "n_holding": 0, "cash": 5000.0},
    "by_strategy": [{
        "strategy_id": 1, "strategy_name": "T", "trade_symbol": "AAPL",
        "run_mode": "paper", "signal_passed": True,
        "candidates": [{"symbol": "AAPL", "name": "애플", "qty": 1,
                        "prev_close": 304.99, "est_limit_price": 308.0,
                        "est_total": 308.0, "sizing_mode": "pct_cash",
                        "data_as_of": "2026-05-22"}],
        "skipped": [],
    }],
    "exit_candidates": [],
}


def _build(payload: dict | None):
    """인메모리 DB + 최소 앱(sync/preview 라우터) + 시드. (TestClient, jwt) 반환.

    payload=None이면 스냅샷을 만들지 않는다(스냅샷 없음 케이스).
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)

    with Session(engine) as s:
        user = User(email="t@example.com")
        s.add(user); s.commit(); s.refresh(user)
        device = Device(user_id=user.id, name="dev",
                        token_hash=hash_token(_DEVICE_TOKEN))
        s.add(device); s.commit(); s.refresh(device)
        if payload is not None:
            s.add(SyncSnapshot(user_id=user.id, device_id=device.id,
                               payload=payload))
            s.commit()
        user_id = user.id

    app = FastAPI()
    app.include_router(sync_router.router)
    app.include_router(preview_router.router)

    def _override():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _override
    return TestClient(app), create_access_token(user_id)


def _dev():
    return {"Authorization": f"Bearer {_DEVICE_TOKEN}"}


def _jwt(tok: str):
    return {"Authorization": f"Bearer {tok}"}


# ── 양성: 디바이스 토큰으로 /sync/preview → 200 + preview ─────────────────────

def test_device_pulls_preview_via_sync():
    client, _ = _build({"balance": {}, "next_day_preview": _PREVIEW})
    r = client.get("/sync/preview", headers=_dev())
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["by_strategy"][0]["candidates"][0]["symbol"] == "AAPL"


# ── 음성/경계 ─────────────────────────────────────────────────────────────────

def test_sync_preview_rejects_no_token():
    client, _ = _build({"next_day_preview": _PREVIEW})
    assert client.get("/sync/preview").status_code == 401


def test_sync_preview_rejects_user_jwt():
    """유저 JWT는 디바이스 토큰이 아니므로 /sync/preview에서 거부(401)."""
    client, tok = _build({"next_day_preview": _PREVIEW})
    assert client.get("/sync/preview", headers=_jwt(tok)).status_code == 401


def test_sync_preview_builds_on_demand_when_preview_key_missing(monkeypatch):
    """스냅샷은 있으나 next_day_preview 키가 없으면 즉석(on-demand) build (v0.9.13).

    preview 키 부재 시 build_user_preview를 자리에서 호출해 cron 의존을 제거한다.
    전략이 0개인 유저는 build가 정상 동작하되 매수 후보가 없어 available=True·후보 0
    (available=False는 '스냅샷 자체가 없음'일 때만 — 아래 no_snapshot 케이스).
    이는 cron(refresh_all_users_preview)이 빈 후보 preview를 그대로 merge하는 동작과
    일치하며, 무신호 날을 'preview 없음'으로 오인해 신규 진입을 막지 않게 한다.

    실제 dataset 로드(수십초)·KIS 마스터를 피하려 데이터 의존만 stub —
    build_user_preview의 진짜 로직(전략 0개 → available=True)을 그대로 검증한다.
    """
    from app import kis_master_cache, preview_engine
    monkeypatch.setattr(preview_engine, "get_dataset", lambda: {})
    monkeypatch.setattr(kis_master_cache, "get_master_list", lambda: [])
    client, _ = _build({"balance": {}})       # preview 키 없음 → on-demand build
    r = client.get("/sync/preview", headers=_dev())
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True          # 전략 0개라도 build 성공
    assert body["summary"]["n_buy_candidates"] == 0
    assert body["by_strategy"] == []


def test_sync_preview_available_false_when_no_snapshot():
    """스냅샷 자체가 없으면 available=false (200)."""
    client, _ = _build(None)
    r = client.get("/sync/preview", headers=_dev())
    assert r.status_code == 200
    assert r.json()["available"] is False


# ── 웹 엔드포인트는 그대로(유저 JWT 전용) — 역할 분리 보존 ──────────────────────

def test_web_preview_still_user_authed():
    client, tok = _build({"next_day_preview": _PREVIEW})
    # 디바이스 토큰으로 웹 엔드포인트 호출 → 거부(401)
    assert client.get("/preview/next-day", headers=_dev()).status_code == 401
    # 유저 JWT로는 200 + 동일 preview
    r = client.get("/preview/next-day", headers=_jwt(tok))
    assert r.status_code == 200
    assert r.json()["available"] is True


# ── /sync/strategies — engine을 definition에 주입(로컬앱 IR 디스패치 정합성) ─────

_IR_DEF_MIN = {
    "name": "IR pull",
    "universe": {"kind": "single", "symbols": ["005930"]},
    "signal": {"op": "compare", "params": {"op": ">"},
               "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                          "right": {"op": "const", "params": {"value": 0}}}},
    "position": {"direction": "long", "entry": {"mode": "on_signal"}},
}


def test_sync_strategies_injects_engine_into_definition(monkeypatch):
    """로컬앱 pull 시 definition에 engine 주입 — trader·intraday_stop이 그걸로 디스패치.

    StrategyIR.model_dump엔 engine 필드가 없어 stored definition엔 빠져 있다(아래 create
    응답으로 확인). /sync/strategies가 column 값을 definition에 합쳐 자기완결 spec으로 serve.
    """
    from app.routers import strategies as strategies_router

    # 모의 승격은 capability 게이트(G5)를 통과해야 한다 — 005930을 KOSPI 마스터로 시드해
    # kr_equity(KIS ok)로 판정되게 한다(테스트 환경엔 네트워크 마스터 없음).
    monkeypatch.setattr("app.kis_master_cache.get_master_list",
                        lambda: [{"symbol": "005930", "market": "KOSPI", "name": "삼성전자"}])

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="t@example.com")
        s.add(user); s.commit(); s.refresh(user)
        s.add(Device(user_id=user.id, name="dev", token_hash=hash_token(_DEVICE_TOKEN)))
        s.commit()
        user_id = user.id

    app = FastAPI()
    app.include_router(strategies_router.router)
    app.include_router(sync_router.router)

    def _override():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _override
    client = TestClient(app)
    tok = create_access_token(user_id)

    # IR 전략(paper) 생성 — 실 create 경로(검증 후 model_dump 저장)
    r = client.post("/strategies", headers=_jwt(tok),
                    json={"definition": _IR_DEF_MIN, "run_mode": "paper", "engine": "ir"})
    assert r.status_code == 201, r.text
    assert "engine" not in r.json()["definition"]   # stored definition엔 engine 없음(버그 전제)

    # 로컬앱 pull — engine이 top-level + definition 양쪽에 실려 옴
    rows = client.get("/sync/strategies", headers=_dev()).json()
    assert len(rows) == 1
    assert rows[0]["engine"] == "ir"
    assert rows[0]["definition"]["engine"] == "ir"   # serve 시점 주입 → 로컬앱 디스패치 가능

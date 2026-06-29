"""P5-2 — Strategy.account_ref 데이터 계층 + 라우터 라운드트립 회귀.

전략을 로컬 계좌 핸들(account_handle.account_id, opaque uuid)에 묶는 nullable
account_ref 컬럼을 모델·스키마·마이그레이션·라우터(create/update/_out/sync)에
end-to-end로 추가했음을 잠근다. NULL=미바인딩(레거시 호환).

네트워크/lifespan 없이 인메모리 SQLite + 최소 앱(기존 test_strategies_ir.py·
test_sync_preview_auth.py 픽스처 패턴 재사용 — 새 패턴 도입 금지).
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import db as appdb
from app.db import get_session
from app.models import Device, Strategy, User
from app.routers import strategies as strategies_router
from app.routers import sync as sync_router
from app.schemas import StrategyIn, StrategyOut
from app.security import create_access_token, hash_token


# ── Task 1: 데이터 계층 ──────────────────────────────────────────────────────

def test_strategy_model_has_account_ref():
    s = Strategy(user_id=1, name="t", account_ref="acc_abc")
    assert s.account_ref == "acc_abc"
    assert Strategy(user_id=1, name="t2").account_ref is None  # nullable·레거시 기본


def test_schemas_have_account_ref():
    assert StrategyIn(definition={}).account_ref is None       # 입력 optional
    out = StrategyOut(id=1, name="t", run_mode="draft", engine="ir",
                      definition={}, account_ref="acc_abc",
                      created_at=_dt.datetime.now(),
                      updated_at=_dt.datetime.now())
    assert out.account_ref == "acc_abc"


def test_new_cols_registers_account_ref():
    assert ("strategy", "account_ref", "VARCHAR") in appdb._NEW_COLS


# ── Task 2: 라우터 라운드트립 (기존 test_strategies_ir.py 픽스처·IR 재사용) ──────

# 최소 유효 IR — test_strategies_ir.py의 _IR_DEF에서 그대로 가져옴(새 IR 금지).
_IR_DEF = {
    "name": "연구소 모멘텀",
    "universe": {"kind": "single", "symbols": ["005930"]},
    "signal": {
        "op": "compare", "params": {"op": ">"},
        "inputs": {
            "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
            "right": {"op": "ts_mean", "params": {"window": 20},
                      "inputs": {"signal": {"op": "data",
                                            "params": {"ref": "__SELF__.Close"}}}},
        },
    },
    "position": {"direction": "long", "entry": {"mode": "on_signal"}},
    "simulation": {"initial_capital": 5_000_000},
}

_DEVICE_TOKEN = "test-device-token-acc"


def _build():
    """인메모리 DB + strategies·sync 라우터 + 시드 유저·기기.

    (TestClient, jwt) 반환. test_strategies_ir._build()·test_sync_preview_auth._build()
    패턴 결합 — 라운드트립에 strategies(JWT)·/sync/strategies(device) 둘 다 필요.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="acc@example.com")
        s.add(user); s.commit(); s.refresh(user)
        s.add(Device(user_id=user.id, name="dev",
                     token_hash=hash_token(_DEVICE_TOKEN)))
        s.commit()
        user_id = user.id

    app = FastAPI()
    app.include_router(strategies_router.router)
    app.include_router(sync_router.router)

    def _override():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _override
    return TestClient(app), create_access_token(user_id)


def _jwt(tok: str):
    return {"Authorization": f"Bearer {tok}"}


def _dev():
    return {"Authorization": f"Bearer {_DEVICE_TOKEN}"}


def test_create_persists_and_serves_account_ref():
    """create 시 account_ref 저장 → 응답·GET(_out 경로) 양쪽에 서빙."""
    client, tok = _build()
    r = client.post("/strategies", headers=_jwt(tok),
                    json={"definition": _IR_DEF, "run_mode": "draft",
                          "engine": "ir", "account_ref": "acc_xyz"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["account_ref"] == "acc_xyz"
    # _out 경로 — GET /strategies/{id}
    assert client.get(f"/strategies/{sid}",
                      headers=_jwt(tok)).json()["account_ref"] == "acc_xyz"


def test_update_changes_account_ref(monkeypatch):
    """생성(account_ref=None) → PUT account_ref="acc_2" → GET 반영(재바인딩)."""
    # paper 승격은 매매가능 유니버스를 요구(tradable 게이트) — 테스트 환경엔 KIS
    # 마스터가 없어 헬퍼를 _IR_DEF 종목으로 고정(test_strategies_ir.py와 동일).
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"005930"})
    client, tok = _build()
    sid = client.post("/strategies", headers=_jwt(tok),
                      json={"definition": _IR_DEF, "run_mode": "draft",
                            "engine": "ir"}).json()["id"]
    assert client.get(f"/strategies/{sid}",
                      headers=_jwt(tok)).json()["account_ref"] is None
    r = client.put(f"/strategies/{sid}", headers=_jwt(tok),
                   json={"definition": _IR_DEF, "run_mode": "paper",
                         "engine": "ir", "account_ref": "acc_2"})
    assert r.status_code == 200, r.text
    assert r.json()["account_ref"] == "acc_2"
    assert client.get(f"/strategies/{sid}",
                      headers=_jwt(tok)).json()["account_ref"] == "acc_2"


def test_pull_strategies_includes_account_ref(monkeypatch):
    """paper 전략 account_ref="acc_p" 생성 → /sync/strategies(device-authed)에 포함."""
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"005930"})
    client, tok = _build()
    r = client.post("/strategies", headers=_jwt(tok),
                    json={"definition": _IR_DEF, "run_mode": "paper",
                          "engine": "ir", "account_ref": "acc_p"})
    assert r.status_code == 201, r.text
    rows = client.get("/sync/strategies", headers=_dev()).json()
    assert len(rows) == 1
    assert rows[0]["account_ref"] == "acc_p"


def test_legacy_strategy_account_ref_null():
    """account_ref 없이 생성 → None(레거시 호환, 기존 요청 무변경)."""
    client, tok = _build()
    r = client.post("/strategies", headers=_jwt(tok),
                    json={"definition": _IR_DEF, "run_mode": "draft", "engine": "ir"})
    assert r.status_code == 201, r.text
    assert r.json()["account_ref"] is None

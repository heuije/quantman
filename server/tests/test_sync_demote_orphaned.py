"""고아 바인딩 전략 자동 초안복귀 — /sync/push가 '이 기기에서 사라진 계좌'에 묶인 전략을 draft로.

직전 vs 현재 스냅샷 diff(사라진 계좌만)로 강등하므로 ①멀티기기(다른 기기 계좌) ②같은 계좌
재등록 ③빈 보고(에러·미등록)에는 오강등하지 않음을 잠근다. 인메모리 SQLite + 최소 앱.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.db import get_session
from app.models import Device, Strategy, User
from app.routers import sync as sync_router
from app.security import hash_token

_DEVICE_TOKEN = "test-device-token-demote"


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="demote@example.com")
        s.add(user); s.commit(); s.refresh(user)
        s.add(Device(user_id=user.id, name="dev", token_hash=hash_token(_DEVICE_TOKEN)))
        s.commit()
        uid = user.id
    app = FastAPI()
    app.include_router(sync_router.router)

    def _override():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _override
    return TestClient(app), engine, uid


def _dev():
    return {"Authorization": f"Bearer {_DEVICE_TOKEN}"}


def _snap(account_ids):
    return {"payload": {"health": {
        "account_handles": [{"account_id": a} for a in account_ids]}}}


def _seed(engine, uid, *, run_mode, account_ref):
    with Session(engine) as s:
        row = Strategy(user_id=uid, name="t", engine="ir", definition={},
                       run_mode=run_mode, account_ref=account_ref)
        s.add(row); s.commit(); s.refresh(row)
        return row.id


def _get(engine, sid):
    with Session(engine) as s:
        return s.get(Strategy, sid)


def test_removed_account_demotes_bound_strategy():
    # 실전 계좌 보고 → 자격증명 초기화 후 다른(모의) 계좌 등록 → 실전 계좌 사라짐 → 강등.
    client, engine, uid = _build()
    sid = _seed(engine, uid, run_mode="live", account_ref="acc_live")
    assert client.post("/sync/push", headers=_dev(), json=_snap(["acc_live"])).status_code == 200
    assert _get(engine, sid).run_mode == "live"
    assert client.post("/sync/push", headers=_dev(), json=_snap(["acc_paper"])).status_code == 200
    row = _get(engine, sid)
    assert row.run_mode == "draft"
    assert row.account_ref is None


def test_same_account_not_demoted():
    # 같은 계좌 재보고(=재등록 시 같은 uuid)는 사라짐 아님 → 강등 안 함(재등록 자동복원 보존).
    client, engine, uid = _build()
    sid = _seed(engine, uid, run_mode="live", account_ref="acc_live")
    client.post("/sync/push", headers=_dev(), json=_snap(["acc_live"]))
    client.post("/sync/push", headers=_dev(), json=_snap(["acc_live"]))
    assert _get(engine, sid).run_mode == "live"


def test_empty_new_handles_not_demoted():
    # 빈 보고(current_handles 에러·미등록)는 과강등 방지로 skip.
    client, engine, uid = _build()
    sid = _seed(engine, uid, run_mode="live", account_ref="acc_live")
    client.post("/sync/push", headers=_dev(), json=_snap(["acc_live"]))
    client.post("/sync/push", headers=_dev(), json=_snap([]))
    assert _get(engine, sid).run_mode == "live"


def test_other_device_account_not_demoted():
    # 이 기기엔 acc_live가 애초에 없음(다른 기기 소유) → '사라짐'에 안 잡힘 → 멀티기기 안전.
    client, engine, uid = _build()
    sid = _seed(engine, uid, run_mode="live", account_ref="acc_live")
    client.post("/sync/push", headers=_dev(), json=_snap(["acc_other"]))
    client.post("/sync/push", headers=_dev(), json=_snap(["acc_other2"]))
    assert _get(engine, sid).run_mode == "live"


def test_draft_strategy_untouched():
    # 초안은 대상 아님(account_ref 있어도) — run_mode != draft 조건.
    client, engine, uid = _build()
    sid = _seed(engine, uid, run_mode="draft", account_ref="acc_live")
    client.post("/sync/push", headers=_dev(), json=_snap(["acc_live"]))
    client.post("/sync/push", headers=_dev(), json=_snap(["acc_paper"]))
    assert _get(engine, sid).run_mode == "draft"
    assert _get(engine, sid).account_ref == "acc_live"   # 초안은 손 안 댐

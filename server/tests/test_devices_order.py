"""구조적 회귀 — `/auth/devices`는 최근 활동 기기를 먼저 돌려줘야 한다.

근본 버그: 쿼리에 `ORDER BY`가 없어 DB가 첫 행으로 무엇을 돌려줄지 비결정적.
웹은 `devices[0]`을 명령 대상으로 쓰므로, 같은 PC가 재페어링돼 죽은 device row가
쌓여 있으면 명령(재개·전량매도·킬스위치 해제)이 **죽은 기기로 가서 영영 도달하지
않을** 수 있다. 실제로 RESUME_AUTO·RECONCILE_NOW가 18일 전 죽은 기기에 pending으로
박혀 전달되지 않은 사례 확인.

수정: last_seen_at 내림차순(가장 최근 활동 기기 먼저, None은 뒤)으로 정렬.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.db import get_session  # noqa: E402
from app.models import Device, User  # noqa: E402
from app.routers import auth as auth_router  # noqa: E402
from app.security import create_access_token  # noqa: E402


def _client_with_devices(seen_offsets_hours):
    """seen_offsets_hours: 기기별 last_seen 오프셋(시간, None 허용). (client, jwt, ids) 반환."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    base = datetime(2026, 6, 9, 12, 0, 0)
    ids = []
    with Session(engine) as s:
        user = User(email="t@example.com")
        s.add(user); s.commit(); s.refresh(user)
        for i, off in enumerate(seen_offsets_hours):
            seen = None if off is None else base - timedelta(hours=off)
            d = Device(user_id=user.id, name=f"PC", token_hash=f"h{i}", last_seen_at=seen)
            s.add(d); s.commit(); s.refresh(d)
            ids.append(d.id)
        user_id = user.id

    app = FastAPI()
    app.include_router(auth_router.router)

    def _override():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _override
    return TestClient(app), create_access_token(user_id), ids


def _jwt(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_devices_ordered_by_last_seen_desc():
    """가장 최근 활동(off=0.03h=2분 전)한 기기가 devices[0]."""
    # 입력 순서: 죽음(449h)·죽음(347h)·살아있음(0.03h) — 라이브 사례 재현.
    client, tok, ids = _client_with_devices([449, 347, 0.03])
    rows = client.get("/auth/devices", headers=_jwt(tok)).json()
    assert [r["id"] for r in rows] == [ids[2], ids[1], ids[0]]
    assert rows[0]["id"] == ids[2]   # 웹이 쓰는 devices[0] = 살아있는 기기


def test_devices_none_last_seen_sorts_last():
    """한 번도 안 본(last_seen=None) 기기는 맨 뒤."""
    client, tok, ids = _client_with_devices([None, 10, 1])
    rows = client.get("/auth/devices", headers=_jwt(tok)).json()
    assert [r["id"] for r in rows] == [ids[2], ids[1], ids[0]]
    assert rows[-1]["id"] == ids[0]  # None인 기기가 마지막

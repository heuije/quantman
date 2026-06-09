"""회귀 — 기기 revoke 시 종속행(명령·스냅샷·heartbeat)도 함께 삭제.

근본 버그: revoke_device가 device row만 삭제. Command·SyncSnapshot·HeartbeatEvent가
device.id를 FK로 참조하는데 cascade가 없어, 한 번이라도 명령/스냅샷/heartbeat가 있던
기기는 Postgres FK 위반(IntegrityError)으로 삭제 불가 → 죽은 중복 기기를 정리할 수 없다.
(SQLite는 기본적으로 FK 미강제라 테스트는 '종속행이 실제로 지워졌는지'로 cascade 로직을
검증한다 — delete 성공 여부만으론 Postgres 동작을 대변하지 못함.)

수정: revoke_device가 device의 Command·SyncSnapshot·HeartbeatEvent를 먼저 지운 뒤 device 삭제.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.db import get_session  # noqa: E402
from app.models import (  # noqa: E402
    Command, Device, HeartbeatEvent, SyncSnapshot, User,
)
from app.routers import auth as auth_router  # noqa: E402
from app.security import create_access_token  # noqa: E402


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="t@example.com")
        s.add(user); s.commit(); s.refresh(user)
        dead = Device(user_id=user.id, name="dead", token_hash="h-dead")
        live = Device(user_id=user.id, name="live", token_hash="h-live")
        s.add(dead); s.add(live); s.commit(); s.refresh(dead); s.refresh(live)
        # dead 기기에 종속행 — FK 참조로 naive 삭제 차단되던 케이스
        s.add(Command(user_id=user.id, device_id=dead.id, type="RESUME_AUTO"))
        s.add(SyncSnapshot(user_id=user.id, device_id=dead.id, payload={"balance": {}}))
        s.add(HeartbeatEvent(user_id=user.id, device_id=dead.id))
        # live 기기 종속행 — 정리 대상 아님(보존돼야 함)
        s.add(Command(user_id=user.id, device_id=live.id, type="RUN_CYCLE_NOW"))
        s.add(SyncSnapshot(user_id=user.id, device_id=live.id, payload={"balance": {}}))
        s.commit()
        ctx = dict(uid=user.id, dead=dead.id, live=live.id)

    app = FastAPI()
    app.include_router(auth_router.router)

    def _override():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _override
    return TestClient(app), create_access_token(ctx["uid"]), ctx, engine


def _count(engine, model, device_id):
    with Session(engine) as s:
        return len(s.exec(select(model).where(model.device_id == device_id)).all())


def test_revoke_device_cascades_dependent_rows():
    client, tok, ctx, engine = _build()
    r = client.delete(f"/auth/devices/{ctx['dead']}",
                      headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text

    # dead 기기 종속행 전부 삭제
    assert _count(engine, Command, ctx["dead"]) == 0
    assert _count(engine, SyncSnapshot, ctx["dead"]) == 0
    assert _count(engine, HeartbeatEvent, ctx["dead"]) == 0
    with Session(engine) as s:
        assert s.get(Device, ctx["dead"]) is None

    # live 기기와 그 종속행은 보존 — 다른 기기에 영향 없음
    with Session(engine) as s:
        assert s.get(Device, ctx["live"]) is not None
    assert _count(engine, Command, ctx["live"]) == 1
    assert _count(engine, SyncSnapshot, ctx["live"]) == 1


def test_revoke_other_users_device_forbidden():
    """타 사용자 기기는 404(소유 검증 유지)."""
    client, tok, ctx, engine = _build()
    with Session(engine) as s:
        other = User(email="o@example.com")
        s.add(other); s.commit(); s.refresh(other)
        od = Device(user_id=other.id, name="other", token_hash="h-other")
        s.add(od); s.commit(); s.refresh(od)
        other_dev = od.id
    r = client.delete(f"/auth/devices/{other_dev}",
                      headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 404

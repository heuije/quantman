"""P6-4 Task 2 — GET /strategies/{id}/execution-summary 라운드트립.

서버는 core execution_summary(row.definition)를 그대로 노출한다(passthrough).
4분류 키(confirmed/assumed/at_order/unknown) + 대표 항목(방향=롱·수수료=가정)을
라운드트립으로 검증하고, 비소유·존재하지 않는 전략은 404임을 확인한다.

test_strategies_ir.py의 인메모리 DB + auth-override + 유효 IR 픽스처 패턴을 재사용.
네트워크/lifespan 없이 인메모리 SQLite + 최소 앱.
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
from app.models import User
from app.routers import strategies as strategies_router
from app.security import create_access_token

# ── 픽스처 (test_strategies_ir.py와 동일한 유효 IR) ───────────────────────────

_IR_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {
        "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
        "right": {"op": "ts_mean", "params": {"window": 20},
                  "inputs": {"signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}},
    },
}
_IR_DEF = {
    "name": "연구소 모멘텀",
    "universe": {"kind": "single", "symbols": ["005930"]},
    "signal": _IR_SIGNAL,
    "position": {"direction": "long", "entry": {"mode": "on_signal"},
                 "exit": {"stop_loss": -3.0, "take_profit": 5.0, "hold_days": 5}},
    "simulation": {"initial_capital": 5_000_000},
}


def _build():
    """인메모리 DB + strategies 라우터 + 시드 유저 둘. (TestClient, jwt, 타유저 jwt) 반환.

    두 번째 유저는 소유 불일치(own-or-404) 케이스 검증용 — get_current_user는 인증을
    통과하지만 _own_or_404가 user_id 불일치로 404를 낸다(미존재 user_id면 401이 되어
    소유 경로를 못 탄다 → 실제 유저를 시드).
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="t@example.com")
        other = User(email="other@example.com")
        s.add(user); s.add(other); s.commit(); s.refresh(user); s.refresh(other)
        user_id, other_id = user.id, other.id

    app = FastAPI()
    app.include_router(strategies_router.router)

    def _override():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _override
    return (TestClient(app), create_access_token(user_id),
            create_access_token(other_id))


def _auth(tok: str):
    return {"Authorization": f"Bearer {tok}"}


def test_execution_summary_roundtrip():
    """전략 생성 → execution-summary GET → 200 + 4분류 키 + 대표 항목."""
    client, tok, _other = _build()
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "draft",
                            "engine": "ir"}).json()["id"]

    r = client.get(f"/strategies/{sid}/execution-summary", headers=_auth(tok))
    assert r.status_code == 200, r.text
    s = r.json()

    # 4분류 키가 모두 존재
    assert set(s) == {"confirmed", "assumed", "at_order", "unknown"}

    # 확정: 방향 항목이 '롱'을 포함
    conf = {e["label"]: e["value"] for e in s["confirmed"]}
    assert "롱" in conf["방향"]

    # 가정: 수수료 항목 value에 '가정'이 명시(실율 오해 방지)
    assumed = {e["label"]: e["value"] for e in s["assumed"]}
    assert "가정" in assumed["수수료"]

    # 발주시점·미지는 문자열 리스트(비어 있지 않음)
    assert s["at_order"] and all(isinstance(x, str) for x in s["at_order"])
    assert s["unknown"] and all(isinstance(x, str) for x in s["unknown"])


def test_execution_summary_404_for_nonexistent():
    """존재하지 않는 전략 id는 404 (소유 검사 _own_or_404)."""
    client, tok, _other = _build()
    r = client.get("/strategies/999999/execution-summary", headers=_auth(tok))
    assert r.status_code == 404, r.text


def test_execution_summary_404_for_other_users_strategy():
    """다른 유저 소유 전략은 404 (own-or-404 — 정보 노출 차단)."""
    client, tok, other_tok = _build()
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "draft",
                            "engine": "ir"}).json()["id"]

    # 두 번째(실재) 유저 토큰으로 첫 유저 전략 조회 → 인증은 통과하나 소유 불일치 404
    r = client.get(f"/strategies/{sid}/execution-summary", headers=_auth(other_tok))
    assert r.status_code == 404, r.text

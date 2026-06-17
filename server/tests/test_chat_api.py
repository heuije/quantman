from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.models import User, Conversation
from app.routers import chat as chat_router
from app.security import create_access_token


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        u = User(email="t@example.com"); s.add(u); s.commit(); s.refresh(u); uid = u.id
    app = FastAPI(); app.include_router(chat_router.router)
    def _ov():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _ov
    return TestClient(app), create_access_token(uid), engine, uid


def _auth(tok): return {"Authorization": f"Bearer {tok}"}


def test_create_and_list_conversation():
    client, tok, eng, uid = _build()
    r = client.post("/chat/conversations", headers=_auth(tok))
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    rows = client.get("/chat/conversations", headers=_auth(tok)).json()
    assert any(c["id"] == cid for c in rows)


def test_get_conversation_messages():
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    from app.chat.agent import _persist
    with Session(eng) as s:
        _persist(s, cid, "user", [{"type": "text", "text": "안녕"}])
        _persist(s, cid, "assistant", [{"type": "text", "text": "무엇을 도와드릴까요?"}])
    body = client.get(f"/chat/conversations/{cid}", headers=_auth(tok)).json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["parts"][0]["text"] == "안녕"


def test_message_requires_api_key_503(monkeypatch):
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "")
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    r = client.post("/chat/message", headers=_auth(tok),
                    json={"conversation_id": cid, "message": "안녕"})
    assert r.status_code == 503, r.text


def test_message_success_with_faked_turn(monkeypatch):
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(chat_router, "run_chat_turn",
                        lambda session, cid, msg: [{"type": "text", "text": "결과입니다"}])
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    r = client.post("/chat/message", headers=_auth(tok),
                    json={"conversation_id": cid, "message": "분석해줘"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "assistant"
    assert r.json()["parts"][0]["text"] == "결과입니다"


def test_message_other_users_conversation_404(monkeypatch):
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(chat_router, "run_chat_turn", lambda *a, **k: [])
    client, tok, eng, uid = _build()
    with Session(eng) as s:
        other = User(email="o@example.com"); s.add(other); s.commit(); s.refresh(other)
        oc = Conversation(user_id=other.id); s.add(oc); s.commit(); s.refresh(oc); ocid = oc.id
    r = client.post("/chat/message", headers=_auth(tok),
                    json={"conversation_id": ocid, "message": "x"})
    assert r.status_code == 404, r.text


def test_endpoints_require_auth():
    client, tok, eng, uid = _build()
    assert client.post("/chat/conversations").status_code == 401
    assert client.get("/chat/conversations").status_code == 401

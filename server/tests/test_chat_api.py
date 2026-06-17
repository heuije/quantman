from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

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


# ── P1b: 스트리밍 SSE 엔드포인트 ──────────────────────────────────────────────

def test_stream_endpoint_emits_sse_frames(monkeypatch):
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "test-key")

    def _fake_stream(session, cid, msg):  # noqa: ARG001
        yield ("delta", {"text": "안녕"})
        yield ("tool_use", {"id": "t1", "name": "screen", "input": {"top_n": 3}})
        yield ("tool_result", {"tool_use_id": "t1", "name": "screen",
                               "result": {"success": True, "results": [{"symbol": "AAA"}]}})
        yield ("done", {})
    monkeypatch.setattr(chat_router, "stream_chat_turn", _fake_stream)

    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    r = client.post("/chat/stream", headers=_auth(tok),
                    json={"conversation_id": cid, "message": "분석해줘"})
    assert r.status_code == 200, r.text
    assert "text/event-stream" in r.headers["content-type"]
    body = r.text
    for frame in ("event: delta", "event: tool_use", "event: tool_result", "event: done"):
        assert frame in body, body
    assert "AAA" in body                       # tool_result full payload가 프레임에 실린다


def test_stream_requires_api_key_503(monkeypatch):
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "")
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    r = client.post("/chat/stream", headers=_auth(tok),
                    json={"conversation_id": cid, "message": "안녕"})
    assert r.status_code == 503, r.text


def test_stream_other_users_conversation_404(monkeypatch):
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(chat_router, "stream_chat_turn", lambda *a, **k: iter(()))
    client, tok, eng, uid = _build()
    with Session(eng) as s:
        other = User(email="o2@example.com"); s.add(other); s.commit(); s.refresh(other)
        oc = Conversation(user_id=other.id); s.add(oc); s.commit(); s.refresh(oc); ocid = oc.id
    r = client.post("/chat/stream", headers=_auth(tok),
                    json={"conversation_id": ocid, "message": "x"})
    assert r.status_code == 404, r.text


# 통합: 실제 stream_chat_turn을 StreamingResponse로 구동 — Depends 세션이 스트리밍 내내
# 살아 있어 영속까지 도는지(가짜 generator로는 못 잡는 세션 수명) 검증.
class _B:  # content block
    def __init__(self, **kw): self.__dict__.update(kw)

class _Msg:
    def __init__(self, content, stop_reason): self.content, self.stop_reason = content, stop_reason

class _Stream:
    def __init__(self, msg): self._msg = msg
    def __enter__(self): return self
    def __exit__(self, *a): return False
    @property
    def text_stream(self):
        for b in self._msg.content:
            if getattr(b, "type", None) == "text":
                yield b.text
    def get_final_message(self): return self._msg

class _Msgs:
    def __init__(self, queue): self._queue = queue
    def stream(self, **kw): return _Stream(self._queue.pop(0))  # noqa: ARG002

class _FakeAnthropic:
    def __init__(self, *a, **k):  # noqa: ARG002 — api_key 등 무시
        self.messages = _Msgs([
            _Msg([_B(type="text", text="스크리닝할게요"),
                  _B(type="tool_use", id="t1", name="screen", input={"top_n": 3})], "tool_use"),
            _Msg([_B(type="text", text="AAA가 가장 저평가입니다.")], "end_turn"),
        ])


def test_stream_endpoint_real_generator_persists(monkeypatch):
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("anthropic.Anthropic", _FakeAnthropic)        # endpoint가 만드는 클라이언트
    from app.chat import agent as chat_agent
    monkeypatch.setattr(chat_agent, "run_tool",
                        lambda name, inp: {"success": True, "query": "select",
                                           "as_of": "2026-06-17", "universe_size": 5,
                                           "results": [{"symbol": "AAA", "score": 0.8}]})
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    r = client.post("/chat/stream", headers=_auth(tok),
                    json={"conversation_id": cid, "message": "저평가주 골라줘"})
    assert r.status_code == 200, r.text
    body = r.text
    for frame in ("event: delta", "event: tool_use", "event: tool_result", "event: done"):
        assert frame in body, body
    assert "AAA" in body
    # 스트리밍 내내 세션이 살아 영속까지 돌았는지 — 새 세션으로 재조회
    from app.models import Message
    with Session(eng) as s:
        rows = s.exec(select(Message).where(Message.conversation_id == cid)
                      .order_by(Message.id)).all()
    assert [m.role for m in rows] == ["user", "assistant"]
    assert any(p["type"] == "tool_result" for p in rows[1].parts)

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


# ── 인당 일일 사용량 캡 (#127 NL컴파일러 패턴을 챗봇에 미러) ───────────────────────

def test_chat_quota_initial_zero():
    """새 유저: 오늘 사용 0, 기본 한도 5회(상품 요구), 미언락."""
    client, tok, eng, uid = _build()
    q = client.post("/chat/quota", headers=_auth(tok), json={}).json()
    assert q == {"used": 0, "limit": 5, "remaining": 5, "admin_unlocked": False}


def test_chat_quota_counts_only_user_messages():
    """'사용량' = user 메시지(질문 턴) 수. assistant 메시지는 카운트하지 않는다."""
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    from app.chat.agent import _persist
    with Session(eng) as s:
        _persist(s, cid, "user", [{"type": "text", "text": "q1"}])
        _persist(s, cid, "assistant", [{"type": "text", "text": "a1"}])
        _persist(s, cid, "user", [{"type": "text", "text": "q2"}])
    q = client.post("/chat/quota", headers=_auth(tok), json={}).json()
    assert q["used"] == 2 and q["remaining"] == 3


def test_chat_quota_per_user_across_conversations():
    """한 유저의 모든 대화를 합산하고(새 대화로 우회 불가), 남의 메시지는 제외한다."""
    client, tok, eng, uid = _build()
    c1 = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    c2 = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    from app.chat.agent import _persist
    with Session(eng) as s:
        _persist(s, c1, "user", [{"type": "text", "text": "a"}])
        _persist(s, c2, "user", [{"type": "text", "text": "b"}])      # 다른 대화도 합산
        other = User(email="z@example.com"); s.add(other); s.commit(); s.refresh(other)
        oc = Conversation(user_id=other.id); s.add(oc); s.commit(); s.refresh(oc)
        _persist(s, oc.id, "user", [{"type": "text", "text": "c"}])   # 남의 메시지는 미포함
    q = client.post("/chat/quota", headers=_auth(tok), json={}).json()
    assert q["used"] == 2


def test_chat_quota_excludes_previous_days_kst():
    """오늘(KST) 자정 이전 메시지는 카운트에서 제외(일일 리셋)."""
    from datetime import datetime, timezone, timedelta
    from app.models import Message
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    with Session(eng) as s:
        old = Message(conversation_id=cid, role="user",
                      parts=[{"type": "text", "text": "old"}],
                      created_at=datetime.now(timezone.utc) - timedelta(days=2))
        s.add(old); s.commit()
    q = client.post("/chat/quota", headers=_auth(tok), json={}).json()
    assert q["used"] == 0


def test_chat_quota_admin_password_raises_limit(monkeypatch):
    """올바른 운영진 비번 → 한도 상향(20). 틀린 비번 → 미언락(기본 한도)."""
    monkeypatch.setattr(chat_router.settings, "CHAT_DAILY_LIMIT", 2)
    monkeypatch.setattr(chat_router.settings, "CHAT_ADMIN_LIMIT", 20)
    monkeypatch.setattr(chat_router.settings, "CHAT_ADMIN_PASSWORD", "secret-pw")
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    from app.chat.agent import _persist
    with Session(eng) as s:
        for i in range(3):
            _persist(s, cid, "user", [{"type": "text", "text": f"q{i}"}])
    q = client.post("/chat/quota", headers=_auth(tok), json={}).json()
    assert q["limit"] == 2 and q["remaining"] == 0 and q["admin_unlocked"] is False
    q2 = client.post("/chat/quota", headers=_auth(tok),
                     json={"admin_password": "secret-pw"}).json()
    assert q2["admin_unlocked"] is True and q2["limit"] == 20
    assert q2["used"] == 3 and q2["remaining"] == 17
    q3 = client.post("/chat/quota", headers=_auth(tok),
                     json={"admin_password": "nope"}).json()
    assert q3["admin_unlocked"] is False and q3["limit"] == 2


def test_chat_message_blocked_at_limit(monkeypatch):
    """한도 도달 시 /chat/message는 429 + rate_limited, run_chat_turn 미호출(LLM 비용 0)."""
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(chat_router.settings, "CHAT_DAILY_LIMIT", 2)
    called = []
    monkeypatch.setattr(chat_router, "run_chat_turn", lambda *a, **k: called.append(1) or [])
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    from app.chat.agent import _persist
    with Session(eng) as s:
        _persist(s, cid, "user", [{"type": "text", "text": "q1"}])
        _persist(s, cid, "user", [{"type": "text", "text": "q2"}])
    r = client.post("/chat/message", headers=_auth(tok),
                    json={"conversation_id": cid, "message": "q3"})
    assert r.status_code == 429, r.text
    body = r.json()
    assert body["rate_limited"] is True and body["limit"] == 2 and body["remaining"] == 0
    assert called == []


def test_chat_message_allowed_under_limit(monkeypatch):
    """한도 미만이면 정상 200."""
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(chat_router.settings, "CHAT_DAILY_LIMIT", 5)
    monkeypatch.setattr(chat_router, "run_chat_turn",
                        lambda *a, **k: [{"type": "text", "text": "ok"}])
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    r = client.post("/chat/message", headers=_auth(tok),
                    json={"conversation_id": cid, "message": "q1"})
    assert r.status_code == 200, r.text


def test_chat_message_admin_password_bypasses_block(monkeypatch):
    """기본 한도 소진 후에도 운영진 비번 동봉 시 통과."""
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(chat_router.settings, "CHAT_DAILY_LIMIT", 1)
    monkeypatch.setattr(chat_router.settings, "CHAT_ADMIN_LIMIT", 20)
    monkeypatch.setattr(chat_router.settings, "CHAT_ADMIN_PASSWORD", "secret-pw")
    monkeypatch.setattr(chat_router, "run_chat_turn",
                        lambda *a, **k: [{"type": "text", "text": "ok"}])
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    from app.chat.agent import _persist
    with Session(eng) as s:
        _persist(s, cid, "user", [{"type": "text", "text": "q1"}])     # 기본 한도 1 소진
    blocked = client.post("/chat/message", headers=_auth(tok),
                          json={"conversation_id": cid, "message": "q2"})
    assert blocked.status_code == 429
    ok = client.post("/chat/message", headers=_auth(tok),
                     json={"conversation_id": cid, "message": "q2",
                           "admin_password": "secret-pw"})
    assert ok.status_code == 200, ok.text


def test_chat_stream_blocked_at_limit(monkeypatch):
    """스트림도 시작 전 동기 차단 — 429 JSON, stream_chat_turn 미호출."""
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(chat_router.settings, "CHAT_DAILY_LIMIT", 1)
    called = []
    monkeypatch.setattr(chat_router, "stream_chat_turn",
                        lambda *a, **k: called.append(1) or iter(()))
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    from app.chat.agent import _persist
    with Session(eng) as s:
        _persist(s, cid, "user", [{"type": "text", "text": "q1"}])
    r = client.post("/chat/stream", headers=_auth(tok),
                    json={"conversation_id": cid, "message": "q2"})
    assert r.status_code == 429, r.text
    assert r.json()["rate_limited"] is True
    assert called == []


# ── 멀티세션: 이름변경 · 삭제(cascade) · 소유자격리 · 자동제목 ─────────────────────

def test_rename_conversation():
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    r = client.patch(f"/chat/conversations/{cid}", headers=_auth(tok),
                     json={"title": "저평가 반도체 분석"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "저평가 반도체 분석"
    rows = client.get("/chat/conversations", headers=_auth(tok)).json()
    assert next(c for c in rows if c["id"] == cid)["title"] == "저평가 반도체 분석"


def test_rename_empty_title_falls_back():
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    r = client.patch(f"/chat/conversations/{cid}", headers=_auth(tok), json={"title": "   "})
    assert r.status_code == 200 and r.json()["title"] == "새 대화"


def test_rename_other_users_conversation_404():
    client, tok, eng, uid = _build()
    with Session(eng) as s:
        other = User(email="r@example.com"); s.add(other); s.commit(); s.refresh(other)
        oc = Conversation(user_id=other.id); s.add(oc); s.commit(); s.refresh(oc); ocid = oc.id
    r = client.patch(f"/chat/conversations/{ocid}", headers=_auth(tok), json={"title": "탈취"})
    assert r.status_code == 404, r.text


def test_delete_conversation_cascades():
    from app.chat.agent import _persist
    from app.models import ChatTurnMetric, Message
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    with Session(eng) as s:
        _persist(s, cid, "user", [{"type": "text", "text": "q"}])
        _persist(s, cid, "assistant", [{"type": "text", "text": "a"}])
        s.add(ChatTurnMetric(conversation_id=cid, user_id=uid)); s.commit()
    r = client.delete(f"/chat/conversations/{cid}", headers=_auth(tok))
    assert r.status_code == 204, r.text
    with Session(eng) as s:                       # 대화·메시지·메트릭 모두 제거(명시적 cascade)
        assert s.get(Conversation, cid) is None
        assert s.exec(select(Message).where(Message.conversation_id == cid)).all() == []
        assert s.exec(select(ChatTurnMetric)
                      .where(ChatTurnMetric.conversation_id == cid)).all() == []
    assert all(c["id"] != cid for c in client.get("/chat/conversations", headers=_auth(tok)).json())


def test_delete_other_users_conversation_404():
    client, tok, eng, uid = _build()
    with Session(eng) as s:
        other = User(email="d@example.com"); s.add(other); s.commit(); s.refresh(other)
        oc = Conversation(user_id=other.id); s.add(oc); s.commit(); s.refresh(oc); ocid = oc.id
    r = client.delete(f"/chat/conversations/{ocid}", headers=_auth(tok))
    assert r.status_code == 404, r.text
    with Session(eng) as s:                       # 남의 대화는 보존
        assert s.get(Conversation, ocid) is not None


def test_autotitle_sets_from_first_message(monkeypatch):
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(chat_router, "run_chat_turn",
                        lambda *a, **k: [{"type": "text", "text": "ok"}])
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    client.post("/chat/message", headers=_auth(tok),
                json={"conversation_id": cid, "message": "저평가 반도체주 3개 골라줘"})
    rows = client.get("/chat/conversations", headers=_auth(tok)).json()
    assert next(c for c in rows if c["id"] == cid)["title"] == "저평가 반도체주 3개 골라줘"


def test_autotitle_does_not_overwrite_renamed(monkeypatch):
    monkeypatch.setattr(chat_router.settings, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(chat_router, "run_chat_turn",
                        lambda *a, **k: [{"type": "text", "text": "ok"}])
    client, tok, eng, uid = _build()
    cid = client.post("/chat/conversations", headers=_auth(tok)).json()["id"]
    client.patch(f"/chat/conversations/{cid}", headers=_auth(tok), json={"title": "내 제목"})
    client.post("/chat/message", headers=_auth(tok),
                json={"conversation_id": cid, "message": "두 번째여도 덮지 않음"})
    rows = client.get("/chat/conversations", headers=_auth(tok)).json()
    assert next(c for c in rows if c["id"] == cid)["title"] == "내 제목"


# ── P1 (커넥션 격리): LLM 왕복·백테스트 compute 동안 DB 커넥션(트랜잭션)을 쥐지 않는다 ──────
# 멀티턴 챗 평가(2026-07-04)에서 발견 — 도구가 연 txn을 미커밋한 채 다음 LLM 왕복을 돌면
# 요청 세션이 턴 내내 풀 커넥션을 점유해, 동시 챗 부하가 풀(5+10)을 고갈시키고 /auth·/health
# 포함 전 엔드포인트가 500난다. preview cron의 C1 선례(계산 중 커넥션 반납)를 챗 경로로 확장.
# in_transaction()으로 "slow work 진입 시 커넥션 미점유"를 직접 단언(C1 테스트와 동형).

def _conn_test_engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    return eng


def test_agent_releases_connection_before_each_llm_round():
    """도구 라운드 후 다음 LLM 왕복 진입 시 세션이 트랜잭션(=풀 커넥션)을 쥐고 있지 않아야 한다.

    adjust_analysis 도구는 _dispatch_tool의 session.get(Conversation) + run_adjust의 읽기로
    txn을 연다. 수정 전엔 미커밋이라 2라운드 stream 진입 시 in_transaction()=True(red).
    """
    from app.chat import agent as chat_agent
    from app.models import Conversation

    eng = _conn_test_engine()
    with Session(eng) as s:
        u = User(email="conn@example.com"); s.add(u); s.commit(); s.refresh(u)
        conv = Conversation(user_id=u.id); s.add(conv); s.commit(); s.refresh(conv)
        cid = conv.id

    seen_in_txn: list[bool] = []

    class _SpyMsgs:
        def __init__(self, session, queue):
            self._session, self._queue = session, queue

        def stream(self, **kw):  # noqa: ARG002 — 각 LLM 왕복 진입 시점의 커넥션 점유 여부 기록
            seen_in_txn.append(self._session.in_transaction())
            return _Stream(self._queue.pop(0))

    class _SpyClient:
        def __init__(self, session):
            self.messages = _SpyMsgs(session, [
                _Msg([_B(type="text", text="조정할게요"),
                      _B(type="tool_use", id="t1", name="adjust_analysis",
                         input={"changes": [{"path": "x", "value": 1}]})], "tool_use"),
                _Msg([_B(type="text", text="완료")], "end_turn"),
            ])

    with Session(eng) as session:
        chat_agent.run_chat_turn(session, cid, "조정해줘", client=_SpyClient(session))

    # 라운드1은 persist(user) 커밋 직후라 항상 False; 라운드2가 회귀 지점(도구가 연 txn 반납 여부).
    assert seen_in_txn == [False, False], \
        f"커넥션 격리 위반: LLM 왕복 진입 시 세션이 DB 커넥션을 점유 중 {seen_in_txn}"


def test_run_simulate_releases_connection_before_backtest(monkeypatch):
    """run_simulate: compile(읽기) 직후·strategy_from_spec(백테스트 compute) 진입 전 커넥션 반납.

    compile_strategy가 연 txn을 쥔 채 백테스트를 돌리면(수정 전) 무거운 계산 내내 커넥션 점유 →
    in_transaction()=True(red). 백테스트 compute 진입 시점을 spy로 직접 단언(C1 preview와 동형).
    """
    from app.chat import tools as chat_tools

    eng = _conn_test_engine()
    with Session(eng) as s:
        u = User(email="sim@example.com"); s.add(u); s.commit(); s.refresh(u); uid = u.id

    holder: dict = {}
    seen: dict = {}

    def _fake_compile(session, user_id, nl):     # 실제 DB 읽기로 txn을 연다(원인 재현) + 성공 IR
        session.get(User, user_id)
        return {"success": True, "ir": {"query": "describe"}, "explanation": "x", "assumptions": []}

    def _spy_spec(ir, dataset, **kw):            # 백테스트 compute 진입 — 이 시점 커넥션 점유 여부
        seen["in_txn"] = holder["session"].in_transaction()
        return {"success": False, "error": "stub"}   # success=False → serialize 경로 회피(엔진 불필요)

    monkeypatch.setattr(chat_tools, "compile_strategy", _fake_compile)
    monkeypatch.setattr(chat_tools, "strategy_from_spec", _spy_spec)
    monkeypatch.setattr(chat_tools, "_load_dataset", lambda ir: None)

    with Session(eng) as session:
        holder["session"] = session
        chat_tools.run_simulate(session, uid, {"nl": "삼성 설명해줘"})

    assert seen.get("in_txn") is False, "run_simulate가 백테스트 compute 동안 DB 커넥션 점유"

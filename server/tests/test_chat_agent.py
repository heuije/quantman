"""전략 연구소 챗봇 agent 테스트 — Task 6/7/8.

모든 테스트는 HERMETIC: 실 Anthropic API·데이터 엔진 없이 monkeypatch/FakeClient로 격리.
"""
# ── Task 6: chat system prompt ───────────────────────────────────────────────
from app.chat.prompt import chat_system_prompt


def test_system_prompt_includes_capabilities_and_rules():
    p = chat_system_prompt()
    assert "<capabilities>" in p
    assert "screen" in p and "simulate" in p
    assert "예측" in p                     # 백테스트≠예측 가드레일 존재
    assert "tool_result" in p              # 숫자 규율 명시


def test_system_prompt_consult_offers_options():
    # 협의(되묻기) 시 빈 질문이 아니라 선택지·기본값을 먼저 제안하도록 지시한다.
    p = chat_system_prompt()
    assert "선택지" in p


# ── Task 7: persist + history compaction ────────────────────────────────────
from sqlmodel import Session, SQLModel, create_engine, select
from app.models import Conversation, Message
from app.chat import agent as chat_agent


def _mem_session() -> Session:
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return Session(eng)


def test_persist_and_history_compaction():
    s = _mem_session()
    conv = Conversation(user_id=1)
    s.add(conv); s.commit(); s.refresh(conv)

    chat_agent._persist(s, conv.id, "user", [{"type": "text", "text": "저평가주 골라줘"}])
    chat_agent._persist(s, conv.id, "assistant", [
        {"type": "text", "text": "스크리닝할게요"},
        {"type": "tool_use", "id": "t1", "name": "screen", "input": {"top_n": 3}},
        {"type": "tool_result", "tool_use_id": "t1", "name": "screen",
         "result": {"success": True, "as_of": "2026-06-17", "universe_size": 9,
                    "results": [{"symbol": "AAA", "score": 0.8}]}},
    ])

    wire = chat_agent._history_to_wire(s, conv.id)
    assert wire[0] == {"role": "user", "content": "저평가주 골라줘"}
    assert wire[1]["role"] == "assistant"
    assert any(b.get("type") == "tool_use" for b in wire[1]["content"])
    tr = wire[2]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "t1"
    assert "AAA" in tr["content"]            # full 아니라 compact 텍스트
    assert "results" not in str(tr["content"])  # full payload 미포함


# ── Task 8: run_chat_turn agent loop ────────────────────────────────────────
from app.chat import tools as chat_tools


class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason


class _FakeStream:
    """.messages.stream() 컨텍스트매니저 모사 — text_stream(델타) + get_final_message()."""
    def __init__(self, resp): self._resp = resp
    def __enter__(self): return self
    def __exit__(self, *a): return False
    @property
    def text_stream(self):
        for b in self._resp.content:
            if getattr(b, "type", None) == "text":
                yield b.text
    def get_final_message(self): return self._resp


class _FakeMessages:
    def __init__(self, queue): self._queue = queue
    def stream(self, **kw):  # noqa: ARG002
        return _FakeStream(self._queue.pop(0))


class _FakeClient:
    def __init__(self, queue): self.messages = _FakeMessages(queue)


def test_run_chat_turn_dispatches_and_persists(monkeypatch):
    s = _mem_session()
    conv = Conversation(user_id=1)
    s.add(conv); s.commit(); s.refresh(conv)

    monkeypatch.setattr(chat_tools, "run_tool",
                        lambda name, inp: {"success": True, "query": "select",
                                           "as_of": "2026-06-17", "universe_size": 5,
                                           "results": [{"symbol": "AAA", "score": 0.8}]})
    monkeypatch.setattr(chat_agent, "run_tool", chat_tools.run_tool, raising=False)

    queue = [
        _Resp([_Block(type="text", text="스크리닝할게요"),
               _Block(type="tool_use", id="t1", name="screen",
                      input={"score_ref": "__SELF__.pb_ratio", "top_n": 3})],
              stop_reason="tool_use"),
        _Resp([_Block(type="text", text="AAA가 가장 저평가입니다.")],
              stop_reason="end_turn"),
    ]
    parts = chat_agent.run_chat_turn(s, conv.id, "저평가주 골라줘",
                                     client=_FakeClient(queue))

    kinds = [p["type"] for p in parts]
    assert "tool_use" in kinds and "tool_result" in kinds and "text" in kinds
    tr = next(p for p in parts if p["type"] == "tool_result")
    assert tr["result"]["results"][0]["symbol"] == "AAA"     # full payload 보존

    rows = s.exec(select(Message).where(Message.conversation_id == conv.id)
                  .order_by(Message.id)).all()
    assert [r.role for r in rows] == ["user", "assistant"]
    assert any(p["type"] == "tool_result" for p in rows[1].parts)


def test_run_chat_turn_no_tool_just_text(monkeypatch):
    s = _mem_session()
    conv = Conversation(user_id=1)
    s.add(conv); s.commit(); s.refresh(conv)
    queue = [_Resp([_Block(type="text", text="무엇을 분석할까요?")], stop_reason="end_turn")]
    parts = chat_agent.run_chat_turn(s, conv.id, "안녕", client=_FakeClient(queue))
    assert parts == [{"type": "text", "text": "무엇을 분석할까요?"}]


def test_run_chat_turn_persists_error_reply_on_failure():
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)

    class _BoomMessages:
        def stream(self, **kw):  # noqa: ARG002
            raise RuntimeError("LLM down")
    class _BoomClient:
        def __init__(self): self.messages = _BoomMessages()

    parts = chat_agent.run_chat_turn(s, conv.id, "질문", client=_BoomClient())
    # 오류 답변 반환(고아 아님) + user+assistant 둘 다 영속
    assert any(p["type"] == "text" and "오류" in p["text"] for p in parts)
    rows = s.exec(select(Message).where(Message.conversation_id == conv.id)
                  .order_by(Message.id)).all()
    assert [r.role for r in rows] == ["user", "assistant"]


def test_history_reconstructs_alternating_rounds():
    s = _mem_session()
    conv = Conversation(user_id=1)
    s.add(conv); s.commit(); s.refresh(conv)
    chat_agent._persist(s, conv.id, "user", [{"type": "text", "text": "저평가주 골라줘"}])
    chat_agent._persist(s, conv.id, "assistant", [
        {"type": "text", "text": "스크리닝할게요"},
        {"type": "tool_use", "id": "t1", "name": "screen", "input": {"top_n": 3}},
        {"type": "tool_result", "tool_use_id": "t1", "name": "screen",
         "result": {"success": True, "as_of": "2026-06-17", "universe_size": 9,
                    "results": [{"symbol": "AAA", "score": 0.8}]}},
        {"type": "text", "text": "AAA가 가장 저평가입니다."},
    ])
    wire = chat_agent._history_to_wire(s, conv.id)
    roles = [m["role"] for m in wire]
    # Anthropic 계약: 엄격 교대(연속 동일 role 금지)
    assert all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), roles
    assert roles == ["user", "assistant", "user", "assistant"]
    assert wire[1]["content"][-1]["type"] == "tool_use"          # 라운드1: text+tool_use
    assert wire[2]["content"][0]["type"] == "tool_result"        # tool_result user 블록
    assert wire[3]["content"][0] == {"type": "text", "text": "AAA가 가장 저평가입니다."}  # 최종답변=tool_result 뒤 assistant
    assert "results" not in str(wire[2]["content"])              # 모델엔 compact만


# ── P1b: 스트리밍 (stream_chat_turn) ─────────────────────────────────────────
# 스트리밍 코어는 .messages.stream() 컨텍스트매니저를 쓴다(text_stream 델타 + get_final_message).
# stream_chat_turn이 단일 소스이고 run_chat_turn은 이를 소진해 parts를 반환한다(_FakeClient 재사용).


def test_stream_chat_turn_yields_ordered_events(monkeypatch):
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)
    monkeypatch.setattr(chat_tools, "run_tool",
                        lambda name, inp: {"success": True, "query": "select",
                                           "as_of": "2026-06-17", "universe_size": 5,
                                           "results": [{"symbol": "AAA", "score": 0.8}]})
    monkeypatch.setattr(chat_agent, "run_tool", chat_tools.run_tool, raising=False)
    queue = [
        _Resp([_Block(type="text", text="스크리닝할게요"),
               _Block(type="tool_use", id="t1", name="screen",
                      input={"score_ref": "__SELF__.pb_ratio", "top_n": 3})],
              stop_reason="tool_use"),
        _Resp([_Block(type="text", text="AAA가 가장 저평가입니다.")],
              stop_reason="end_turn"),
    ]
    events = list(chat_agent.stream_chat_turn(s, conv.id, "저평가주 골라줘",
                                              client=_FakeClient(queue)))
    kinds = [k for k, _ in events]
    assert kinds[0] == "delta"                         # 서두 텍스트가 먼저 흐른다
    assert "tool_use" in kinds and "tool_result" in kinds
    assert kinds[-1] == "done"
    assert kinds.index("delta") < kinds.index("tool_use")
    tr = next(p for k, p in events if k == "tool_result")
    assert tr["result"]["results"][0]["symbol"] == "AAA"   # full payload 보존
    done = next(p for k, p in events if k == "done")
    assert any(part["type"] == "tool_result" for part in done["parts"])
    rows = s.exec(select(Message).where(Message.conversation_id == conv.id)
                  .order_by(Message.id)).all()
    assert [r.role for r in rows] == ["user", "assistant"]   # 고아 없이 영속


def test_stream_chat_turn_streams_text_deltas():
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)
    queue = [_Resp([_Block(type="text", text="무엇을 분석할까요?")], stop_reason="end_turn")]
    events = list(chat_agent.stream_chat_turn(s, conv.id, "안녕", client=_FakeClient(queue)))
    deltas = [p["text"] for k, p in events if k == "delta"]
    assert "".join(deltas) == "무엇을 분석할까요?"
    assert events[-1][0] == "done"


def test_run_chat_turn_drains_stream_to_parts():
    # run_chat_turn은 스트리밍 코어를 소진해 최종 parts를 반환한다(비스트리밍 호환).
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)
    queue = [_Resp([_Block(type="text", text="무엇을 분석할까요?")], stop_reason="end_turn")]
    parts = chat_agent.run_chat_turn(s, conv.id, "안녕", client=_FakeClient(queue))
    assert parts == [{"type": "text", "text": "무엇을 분석할까요?"}]


# ── P2: save_strategy 디스패치 + 다중도구 ────────────────────────────────────
_IR_DEF_SAVE = {
    "name": "연구소 모멘텀",
    "universe": {"kind": "single", "symbols": ["005930"]},
    "signal": {"op": "compare", "params": {"op": ">"},
               "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                          "right": {"op": "ts_mean", "params": {"window": 20},
                                    "inputs": {"signal": {"op": "data",
                                                          "params": {"ref": "__SELF__.Close"}}}}}},
    "position": {"direction": "long", "entry": {"mode": "on_signal"}},
    "simulation": {"initial_capital": 5_000_000},
}


def test_stream_dispatches_save_strategy():
    # save_strategy는 순수 도구가 아니라 side-effect(DB 저장) — 루프가 대화 소유자(user_id)로
    # 호출해 draft 전략을 만든다. 엔진/네트워크 없이 실 검증·저장 경로를 탄다.
    s = _mem_session()
    from app.models import User, Strategy
    u = User(email="z@z.com"); s.add(u); s.commit(); s.refresh(u)
    conv = Conversation(user_id=u.id); s.add(conv); s.commit(); s.refresh(conv)
    queue = [
        _Resp([_Block(type="text", text="저장할게요"),
               _Block(type="tool_use", id="t1", name="save_strategy",
                      input={"name": "내 전략", "ir": _IR_DEF_SAVE})],
              stop_reason="tool_use"),
        _Resp([_Block(type="text", text="저장 완료!")], stop_reason="end_turn"),
    ]
    parts = chat_agent.run_chat_turn(s, conv.id, "이 전략 저장해줘", client=_FakeClient(queue))
    tr = next(p for p in parts if p["type"] == "tool_result")
    assert tr["result"]["success"] is True
    row = s.get(Strategy, tr["result"]["strategy_id"])
    assert row.run_mode == "draft" and row.user_id == u.id      # 소유자·draft 저장


def test_stream_handles_multiple_tools_one_response(monkeypatch):
    # 한 응답에 도구 2개(시나리오 A vs B) → 둘 다 실행되어 tool_result 2개(기 지원·회귀 잠금).
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)
    monkeypatch.setattr(chat_agent, "run_tool",
                        lambda name, inp: {"success": True, "query": "simulate",
                                           "metrics": {"cagr": 0.1}}, raising=False)
    queue = [
        _Resp([_Block(type="text", text="A·B 비교할게요"),
               _Block(type="tool_use", id="a", name="simulate", input={"strategy": {"x": 1}}),
               _Block(type="tool_use", id="b", name="simulate", input={"strategy": {"x": 2}})],
              stop_reason="tool_use"),
        _Resp([_Block(type="text", text="A가 낫습니다.")], stop_reason="end_turn"),
    ]
    parts = chat_agent.run_chat_turn(s, conv.id, "A vs B 비교", client=_FakeClient(queue))
    assert len([p for p in parts if p["type"] == "tool_result"]) == 2
    assert len([p for p in parts if p["type"] == "tool_use"]) == 2

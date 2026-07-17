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
         "result": {"success": True, "query": "select", "as_of": "2026-06-17", "universe_size": 9,
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
    def __init__(self, queue): self._queue = queue; self.received = []
    def stream(self, **kw):
        # messages는 루프가 이후 append하므로 호출 시점 스냅샷을 잡는다(레퍼런스 오염 방지).
        snap = dict(kw)
        if "messages" in snap:
            snap["messages"] = list(snap["messages"])
        self.received.append(snap)
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
    client = _FakeClient(queue)
    parts = chat_agent.run_chat_turn(s, conv.id, "저평가주 골라줘", client=client)
    # Sonnet5 thinking 기본ON→멀티턴 400 회귀 차단: 스트림 호출에 thinking=disabled 강제.
    assert client.messages.received[0]["thinking"] == {"type": "disabled"}

    kinds = [p["type"] for p in parts]
    assert "tool_use" in kinds and "tool_result" in kinds and "text" in kinds
    tr = next(p for p in parts if p["type"] == "tool_result")
    assert tr["result"]["results"][0]["symbol"] == "AAA"     # full payload 보존

    rows = s.exec(select(Message).where(Message.conversation_id == conv.id)
                  .order_by(Message.id)).all()
    assert [r.role for r in rows] == ["user", "assistant"]
    assert any(p["type"] == "tool_result" for p in rows[1].parts)


def test_aborted_turn_leaves_metric_and_log(caplog):
    """#P2-c 관측 백스톱: 클라이언트 끊김/타임아웃으로 스트림이 중단(제너레이터 close→GeneratorExit)
    돼도 반드시 흔적을 남긴다 — '중단' 메트릭(ok=False·status=aborted) + 경고 로그. 이전엔 assistant·
    metric을 남기는 코드가 실행조차 못 돼 user 메시지만 있고 아무 흔적 없이 죽었다(conv#43 무응답 드롭)."""
    import logging
    from app.models import ChatTurnMetric
    s = _mem_session()
    conv = Conversation(user_id=7); s.add(conv); s.commit(); s.refresh(conv)
    queue = [_Resp([_Block(type="text", text="분석 중입니다…")], stop_reason="tool_use")]
    client = _FakeClient(queue)
    gen = chat_agent.stream_chat_turn(s, conv.id, "US·KR 섹터별 연도별 대형 다축 쿼리", client=client, model="x")
    kind, _ = next(gen)                 # 첫 이벤트 = 라운드 진행 라벨(침묵 UX 계약)
    assert kind == "progress"
    kind, _ = next(gen)                 # 델타 소비 → 제너레이터가 loop 안 yield에서 suspend
    assert kind == "delta"
    with caplog.at_level(logging.WARNING, logger="app.chat.agent"):
        gen.close()                     # 클라이언트 끊김 시뮬레이션 → GeneratorExit
    mets = s.exec(select(ChatTurnMetric).where(ChatTurnMetric.conversation_id == conv.id)).all()
    assert len(mets) == 1                                    # 중단인데도 메트릭 1행(무흔적 아님)
    assert mets[0].ok is False and mets[0].result_status == "aborted" and mets[0].stop_reason == "aborted"
    # user 메시지는 시작에 영속됨(흔적 존재)
    roles = [r.role for r in s.exec(select(Message).where(Message.conversation_id == conv.id)).all()]
    assert "user" in roles
    assert any("aborted" in r.getMessage() for r in caplog.records)   # 근본원인 조회용 경고 로그


def test_dup_simulate_warns_model(monkeypatch):
    """③제어: 한 턴에 같은 IR로 simulate를 재호출하면 모델에 가는 요약에 '동일한 분석' 경고가 붙는다
    (conv#8식 표현만 바꾼 재실행 헛돌이 차단). 첫 호출엔 경고 없음."""
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)
    fixed = {"success": True, "equity": [1, 2], "metrics": {"cagr": 1.0},
             "ir": {"name": "x", "query": "simulate", "universe": {"kind": "single", "symbols": ["A"]}}}
    monkeypatch.setattr(chat_agent, "run_simulate", lambda sess, uid, inp: dict(fixed))
    queue = [
        _Resp([_Block(type="tool_use", id="a", name="simulate", input={"nl": "v1"})], "tool_use"),
        _Resp([_Block(type="tool_use", id="b", name="simulate", input={"nl": "v2 표현만 다름"})], "tool_use"),
        _Resp([_Block(type="text", text="답")], "end_turn"),
    ]
    fc = _FakeClient(queue)
    chat_agent.run_chat_turn(s, conv.id, "분석", client=fc)
    last_msgs = fc.messages.received[-1]["messages"]
    trs = [c for m in last_msgs if isinstance(m.get("content"), list)
           for c in m["content"] if isinstance(c, dict) and c.get("type") == "tool_result"]
    joined = [str(c.get("content")) for c in trs]
    assert len(trs) == 2
    assert "동일한 분석" not in joined[0]      # 첫 호출 — 경고 없음
    assert "동일한 분석" in joined[1]          # 둘째(동일 IR) — 경고


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
    # fail-soft 답변 반환(막다른길 아님·고아 아님) + user+assistant 둘 다 영속
    assert any(p["type"] == "text" and "다시 시도" in p["text"] for p in parts)
    rows = s.exec(select(Message).where(Message.conversation_id == conv.id)
                  .order_by(Message.id)).all()
    assert [r.role for r in rows] == ["user", "assistant"]


def test_round_cap_forces_partial_synthesis(monkeypatch):
    """상한(MAX_TOOL_ROUNDS) 소진 시 '분석이 길어졌습니다' 비답변이 아니라, **도구 없는 1콜로
    부분결과를 강제 종합**해 실제 답변을 낸다(chat-latency Phase 1a·무응답 방지). heuije conv#40
    (8라운드 전부 tool_use→비답변) 재현 케이스."""
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)
    monkeypatch.setattr(chat_tools, "run_tool",
                        lambda name, inp: {"success": True, "query": "select", "as_of": "2026-06-17",
                                           "universe_size": 5, "results": [{"symbol": "AAA", "score": 0.8}]})
    monkeypatch.setattr(chat_agent, "run_tool", chat_tools.run_tool, raising=False)
    # 8라운드 전부 tool_use → 상한 소진(최종 답변 없음) → 9번째=강제 종합(도구 없이 텍스트).
    queue = [_Resp([_Block(type="tool_use", id=f"t{i}", name="screen",
                           input={"score_ref": "__SELF__.pb_ratio", "top_n": 3})], "tool_use")
             for i in range(chat_agent.MAX_TOOL_ROUNDS)]
    queue.append(_Resp([_Block(type="text", text="지금까지 확인한 범위로는 AAA가 가장 저평가입니다.")], "end_turn"))
    fc = _FakeClient(queue)
    parts = chat_agent.run_chat_turn(s, conv.id, "복잡한 피어 비교", client=fc)

    texts = [p["text"] for p in parts if p["type"] == "text"]
    assert any("가장 저평가" in t for t in texts), texts          # 강제 종합 답변이 실제로 나옴
    assert not any("좁혀 다시" in t for t in texts), texts        # 최종 비답변 폴백은 미발동
    assert len(fc.messages.received) == chat_agent.MAX_TOOL_ROUNDS + 1   # 8라운드 + 1 종합콜
    assert "tools" not in fc.messages.received[-1]                 # 종합콜은 도구 없이 호출


def test_progress_line_is_human_readable():
    """진행 스트리밍(Phase 1b): 도구 호출 → 결정적 진행 라인(도구명·입력 반영·LLM 미사용)."""
    pl = chat_agent._progress_line
    assert "005930" in pl("describe", {"symbol": "005930"})
    assert "반도체" in pl("screen", {"sectors": ["반도체"]})
    assert "스크리닝" in pl("screen", {})
    assert pl("inspect", {"symbol": "AAA", "columns": ["rsi_14"]}).startswith("📈")
    assert "백테스트" in pl("simulate", {"nl": "x"})
    assert pl("unknown_tool", {})                     # 미지 도구도 폴백 라벨


# ── T3 (Wave 2 Phase 1): 크래시 fail-soft ─────────────────────────────────────
def test_classify_failure_transient_vs_analysis():
    """실패 부류: *재시도 유효한* 오류(연결·타임아웃·429·5xx)만 transient. BadRequest(400) 등 4xx는
    재시도 무익→analysis로 표면화(thinking-블록 400을 '일시적 연결 문제'로 은폐하던 부류 회귀 차단)."""
    import anthropic

    def _mk(cls):
        class _E(cls):
            def __init__(self): pass    # 베이스 __init__ 우회 — isinstance만 검사
        return _E()
    # 재시도 유효 → transient
    for c in (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError):
        assert chat_agent._classify_failure(_mk(c)) == "transient", c
    # 지속성 4xx(BadRequest=실제 버그 시그니처) → analysis(표면화)
    assert chat_agent._classify_failure(_mk(anthropic.BadRequestError)) == "analysis"
    assert chat_agent._classify_failure(RuntimeError("engine boom")) == "analysis"
    assert chat_agent._classify_failure(ValueError()) == "analysis"


def test_failure_message_class_and_partial():
    """fail-soft 메시지: 부류별 원인·복구 + 부분결과 있으면 안내 문구."""
    m = chat_agent._failure_message("transient", had_partial=False)
    assert "일시적" in m and "다시 시도" in m and "중간 결과" not in m
    m2 = chat_agent._failure_message("analysis", had_partial=True)
    # 내부 오류를 사용자 '조건'(종목·기간·복잡도) 탓으로 돌리지 않는다 — '좁혀/조건을 단순하게'
    # 같은 blame 문구를 재도입하지 않도록 회귀 가드(희제 실사용 신고 반영).
    assert "중간 결과" in m2 and "다시 시도" in m2
    assert "좁혀" not in m2 and "단순하게" not in m2


def test_tool_unexpected_raise_yields_structured_result(monkeypatch):
    """도구가 예기치 못한 예외로 죽어도 턴은 막다른길이 아니라 구조화 결과로 이어진다(#4a 근본).

    엔진(strategy_from_spec 등)이 도구 가드를 빠져나가 raise → success=False·status=infeasible
    tool_result(모델 피드백) + 최종 답변까지 도달. 한 도구 크래시가 전체 대화를 죽이지 않는다.
    """
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)

    def _boom(name, inp):       # noqa: ARG001
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(chat_agent, "run_tool", _boom)
    queue = [
        _Resp([_Block(type="tool_use", id="t1", name="screen",
                      input={"score_ref": "__SELF__.pb_ratio", "top_n": 3})], "tool_use"),
        _Resp([_Block(type="text", text="죄송해요, 그 분석은 실패했어요.")], "end_turn"),
    ]
    parts = chat_agent.run_chat_turn(s, conv.id, "스크리닝", client=_FakeClient(queue))
    tr = next(p for p in parts if p["type"] == "tool_result")
    assert tr["result"]["success"] is False
    assert tr["result"]["status"] == "infeasible"          # 구조화(막다른 텍스트 아님)
    assert "다시 시도" in tr["result"]["verdict"]
    assert any(p["type"] == "text" and "실패" in p["text"] for p in parts)   # 턴은 최종답변까지 도달
    rows = s.exec(select(Message).where(Message.conversation_id == conv.id)
                  .order_by(Message.id)).all()
    assert [r.role for r in rows] == ["user", "assistant"]   # 고아 없이 영속


def test_compact_summary_raise_does_not_deadend_turn(monkeypatch):
    """결과 요약(compact_summary) 렌더가 어떤 형상에서 터져도, 결과가 멀쩡한 턴을 통째로
    막다른길('오류가 생겨')로 만들지 않는다 — 최소 요약으로 대체하고 최종답변까지 도달(F1 근본).

    희제 실사용: 복합 요청(수급+차트+추천)이 크래시하며 "조건을 단순하게" 오안내됐다. compact 요약은
    모델 컨텍스트용 표시 헬퍼이므로 그 예외가 실제 결과(full payload·이미 UI로 yield)를 무효화해선 안 된다.
    """
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)
    monkeypatch.setattr(chat_agent, "run_tool",
                        lambda name, inp: {"success": True, "query": "select",
                                           "as_of": "2026-06-17", "universe_size": 5,
                                           "results": [{"symbol": "AAA", "score": 0.8}]})

    def _boom(name, full):      # noqa: ARG001 — 요약 렌더 폭발 시뮬레이션
        raise RuntimeError("summary render exploded")
    monkeypatch.setattr(chat_agent, "compact_summary", _boom)
    queue = [
        _Resp([_Block(type="tool_use", id="t1", name="screen",
                      input={"score_ref": "__SELF__.pb_ratio", "top_n": 3})], "tool_use"),
        _Resp([_Block(type="text", text="상위 종목은 AAA입니다.")], "end_turn"),
    ]
    parts = chat_agent.run_chat_turn(s, conv.id, "스크리닝", client=_FakeClient(queue))
    assert any(p["type"] == "tool_result" and p["result"].get("success") for p in parts)  # 결과 보존
    assert any(p["type"] == "text" and "AAA" in p["text"] for p in parts)                  # 최종답변 도달
    assert not any(p["type"] == "text" and "오류가 생겨" in p["text"] for p in parts)        # 막다른길 아님


def test_turn_crash_failsoft_notes_partial_and_records_status(monkeypatch):
    """루프 자체(LLM 스트림)가 죽으면 막다른 '잠시 후 다시' 대신 부류별 복구 + 부분결과 안내.

    1라운드 도구 성공 후 2라운드 스트림이 죽는 시나리오 → 중간 결과 안내 + 재시도 제안,
    메트릭은 ok=False·result_status='error'(bad_result_rate 포착).
    """
    from app.models import ChatTurnMetric
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)
    monkeypatch.setattr(chat_agent, "run_tool",
                        lambda name, inp: {"success": True, "query": "select",
                                           "as_of": "2026-06-17", "universe_size": 5,
                                           "results": [{"symbol": "AAA", "score": 0.8}]})

    class _CrashOnSecond:
        def __init__(self): self.n = 0
        def stream(self, **kw):     # noqa: ARG002
            self.n += 1
            if self.n == 1:
                return _FakeStream(_Resp([_Block(type="tool_use", id="t1", name="screen",
                                                 input={"score_ref": "__SELF__.pb_ratio",
                                                        "top_n": 3})], "tool_use"))
            raise RuntimeError("stream died")

    class _C:
        def __init__(self): self.messages = _CrashOnSecond()
    parts = chat_agent.run_chat_turn(s, conv.id, "스크리닝", client=_C())
    assert any(p["type"] == "tool_result" and p["result"].get("success") for p in parts)  # 부분결과 보존
    txt = next(p["text"] for p in parts if p["type"] == "text")
    assert "중간 결과" in txt and "다시 시도" in txt
    met = s.exec(select(ChatTurnMetric)
                 .where(ChatTurnMetric.conversation_id == conv.id)).first()
    assert met.ok is False and met.result_status == "error"


def test_history_reconstructs_alternating_rounds():
    s = _mem_session()
    conv = Conversation(user_id=1)
    s.add(conv); s.commit(); s.refresh(conv)
    chat_agent._persist(s, conv.id, "user", [{"type": "text", "text": "저평가주 골라줘"}])
    chat_agent._persist(s, conv.id, "assistant", [
        {"type": "text", "text": "스크리닝할게요"},
        {"type": "tool_use", "id": "t1", "name": "screen", "input": {"top_n": 3}},
        {"type": "tool_result", "tool_use_id": "t1", "name": "screen",
         "result": {"success": True, "query": "select", "as_of": "2026-06-17", "universe_size": 9,
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
    assert kinds[0] == "progress"                      # 라운드 진행 라벨이 최선두(침묵 UX 계약)
    assert kinds[1] == "delta"                         # 이어서 서두 텍스트가 흐른다
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
    # 호출해 draft 전략을 만든다. 새 동작: 직전 simulate의 검증 IR을 재사용(재컴파일 0).
    s = _mem_session()
    from app.models import User, Strategy, Message
    u = User(email="z@z.com"); s.add(u); s.commit(); s.refresh(u)
    conv = Conversation(user_id=u.id); s.add(conv); s.commit(); s.refresh(conv)
    # 직전 simulate 결과(검증 IR)를 대화에 영속 — save_strategy가 이를 재사용한다.
    # tool_use_id는 _history_to_wire가 와이어 포맷 복원에 사용하므로 실제 agent 형식과 동일하게.
    s.add(Message(conversation_id=conv.id, role="assistant", parts=[
        {"type": "tool_use", "id": "t0", "name": "simulate", "input": {"nl": "테스트"}},
        {"type": "tool_result", "tool_use_id": "t0", "name": "simulate",
         "result": {"success": True, "ir": _IR_DEF_SAVE}}]))
    s.commit()
    queue = [
        _Resp([_Block(type="text", text="저장할게요"),
               _Block(type="tool_use", id="t1", name="save_strategy",
                      input={"name": "내 전략"})],
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
    # simulate는 run_simulate 경로(run_tool 아님) — 실제 디스패치 경로를 패치한다.
    monkeypatch.setattr(chat_agent, "run_simulate",
                        lambda session, uid, inp: {"success": True, "query": "simulate",
                                                   "metrics": {"cagr": 0.1}}, raising=False)
    queue = [
        _Resp([_Block(type="text", text="A·B 비교할게요"),
               _Block(type="tool_use", id="a", name="simulate", input={"nl": "전략 A"}),
               _Block(type="tool_use", id="b", name="simulate", input={"nl": "전략 B"})],
              stop_reason="tool_use"),
        _Resp([_Block(type="text", text="A가 낫습니다.")], stop_reason="end_turn"),
    ]
    parts = chat_agent.run_chat_turn(s, conv.id, "A vs B 비교", client=_FakeClient(queue))
    results = [p for p in parts if p["type"] == "tool_result"]
    assert len(results) == 2
    assert all(r["result"]["success"] is True for r in results)   # 둘 다 정상 실행(실패경로 아님)
    assert len([p for p in parts if p["type"] == "tool_use"]) == 2


# ── 토큰 최적화 ①히스토리 prompt caching + ②usage 계측 ─────────────────────────

def test_mark_cache_breakpoint_string_content():
    # 문자열 content(user 메시지)는 블록 리스트로 승격돼 cache_control이 붙는다.
    msgs = [{"role": "user", "content": "안녕"}]
    chat_agent._mark_cache_breakpoint(msgs)
    blk = msgs[0]["content"]
    assert isinstance(blk, list)
    assert blk[-1]["text"] == "안녕"
    assert blk[-1]["cache_control"] == {"type": "ephemeral"}


def test_mark_cache_breakpoint_list_content():
    # 리스트 content는 마지막 블록에만 cache_control(앞 블록은 안 건드림).
    msgs = [{"role": "assistant", "content": [
        {"type": "text", "text": "a"},
        {"type": "tool_use", "id": "t", "name": "screen", "input": {}}]}]
    chat_agent._mark_cache_breakpoint(msgs)
    assert msgs[0]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in msgs[0]["content"][0]


def test_mark_cache_breakpoint_empty_noop():
    msgs = []
    chat_agent._mark_cache_breakpoint(msgs)        # 첫 턴(히스토리 없음) → 예외 없이 no-op
    assert msgs == []


def test_stream_caches_history_on_followup_turn():
    # 후속 턴: 이전 대화(히스토리) 마지막 블록에 cache_control이 실려 전송된다(멀티턴 캐싱).
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)
    chat_agent._persist(s, conv.id, "user", [{"type": "text", "text": "이전 질문"}])
    chat_agent._persist(s, conv.id, "assistant", [{"type": "text", "text": "이전 답변"}])
    client = _FakeClient([_Resp([_Block(type="text", text="새 답변")], stop_reason="end_turn")])
    chat_agent.run_chat_turn(s, conv.id, "새 질문", client=client)
    sent = client.messages.received[-1]["messages"]
    tail = sent[-2]                                 # [-1]은 새 user 턴(volatile, 캐시 제외)
    tail_block = tail["content"][-1] if isinstance(tail["content"], list) else None
    assert tail_block and tail_block.get("cache_control") == {"type": "ephemeral"}


def test_first_turn_no_history_marker():
    # 첫 턴: 히스토리 없음 → 새 user 턴엔 마커 없음(system만 캐시).
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)
    client = _FakeClient([_Resp([_Block(type="text", text="답변")], stop_reason="end_turn")])
    chat_agent.run_chat_turn(s, conv.id, "첫 질문", client=client)
    sent = client.messages.received[-1]["messages"]
    assert sent == [{"role": "user", "content": "첫 질문"}]


def test_usage_logged_when_present(caplog):
    import logging
    class _U:
        input_tokens = 100; output_tokens = 20
        cache_creation_input_tokens = 0; cache_read_input_tokens = 4200
    with caplog.at_level(logging.INFO, logger="app.chat.agent"):
        chat_agent._log_usage(7, _U())
    assert any("cache_read=4200" in r.getMessage() for r in caplog.records)


def test_usage_log_skips_when_none(caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="app.chat.agent"):
        chat_agent._log_usage(7, None)             # 스트리밍 usage 부재 → 무로깅·무예외
    assert not any("usage" in r.getMessage() for r in caplog.records)


# ── Task 3: simulate NL 위임 + 전체 라우팅 보존 ──────────────────────────────
def test_system_prompt_guides_nl_simulate_and_routing():
    from app.chat.prompt import chat_system_prompt
    p = chat_system_prompt()
    assert "자연어로" in p and "IR JSON" in p          # simulate NL 위임 안내
    assert "inspect" in p and "describe" in p          # 라우팅 보존(비백테스트 경로)
    assert "투자자문" in p or "일반" in p               # 일반 대화·범위 안내


# ── Task 7 (new): 루프 소진 graceful fallback ────────────────────────────────
def test_loop_exhaustion_yields_fallback_text(monkeypatch):
    from app.chat import agent as ag
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine
    from app.models import User, Conversation
    monkeypatch.setattr(ag, "run_tool", lambda name, inp: {"success": True})
    monkeypatch.setattr(ag, "MAX_TOOL_ROUNDS", 2)
    # 매 라운드 tool_use만 내는 가짜 클라이언트 → 루프 소진
    class _U: input_tokens = 1; output_tokens = 1
    class _B:
        def __init__(s, **k): s.__dict__.update(k)
    class _Msg:
        content = [_B(type="tool_use", id="t", name="screen", input={})]
        stop_reason = "tool_use"; usage = _U()
    class _Stream:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        @property
        def text_stream(s):
            return iter(())
        def get_final_message(s): return _Msg()
    class _Msgs:
        def stream(s, **k): return _Stream()
    class _C:
        messages = _Msgs()
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        u = User(email="t@e.com"); s.add(u); s.commit(); s.refresh(u)
        c = Conversation(user_id=u.id); s.add(c); s.commit(); s.refresh(c)
        parts = ag.run_chat_turn(s, c.id, "분석해줘", client=_C())
    # 상한 소진 → 강제 종합 시도. 이 mock은 종합콜도 빈 텍스트(text_stream=())라 종합 실패 →
    # 최종 graceful 폴백이 뜬다(Phase 1a: 종합 성공 시엔 test_round_cap_forces_partial_synthesis).
    assert any(p["type"] == "text" and "완결하지 못" in p["text"] for p in parts)


def test_tool_result_nan_sanitized_to_valid_json(monkeypatch):
    """백테스트 metrics의 NaN/inf가 None으로 정리돼 브라우저 JSON.parse가 깨지지 않는다.

    라이브 회귀: simulate가 sharpe=NaN(변동성 0 등)을 내면 SSE/응답이 'NaN' 토큰을 실어
    JSON.parse가 깨졌다. /ir 백테스트 경로와 동일한 clean_json을 도구 결과에 적용해 닫는다.
    """
    import json
    s = _mem_session()
    conv = Conversation(user_id=1); s.add(conv); s.commit(); s.refresh(conv)
    monkeypatch.setattr(chat_agent, "run_tool",
                        lambda name, inp: {"success": True,
                                           "metrics": {"cagr": 0.1, "sharpe": float("nan"),
                                                       "mdd": float("-inf")}})
    queue = [
        _Resp([_Block(type="tool_use", id="x", name="screen",
                      input={"score_ref": "__SELF__.pb_ratio", "top_n": 3})],
              stop_reason="tool_use"),
        _Resp([_Block(type="text", text="결과입니다")], stop_reason="end_turn"),
    ]
    parts = chat_agent.run_chat_turn(s, conv.id, "스크리닝", client=_FakeClient(queue))
    tr = next(p for p in parts if p["type"] == "tool_result")
    m = tr["result"]["metrics"]
    assert m["sharpe"] is None and m["mdd"] is None and m["cagr"] == 0.1   # NaN/inf→None
    json.dumps(tr["result"], allow_nan=False)   # NaN 토큰 잔존 시 ValueError(브라우저 JSON.parse 호환)

"""챗봇 성능 측정 환경 — 캡처(ChatTurnMetric 적재) + 분석 CLI 단위 테스트.

전 테스트 HERMETIC: in-memory SQLite + fake Anthropic 클라이언트(실 API·네트워크 없음).
"""
from datetime import datetime, timezone, timedelta

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import ChatTurnMetric, Conversation, Message, User


def _engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    return e


def _seed_user_conv(s) -> tuple[int, int]:
    u = User(email="t@example.com"); s.add(u); s.commit(); s.refresh(u)
    c = Conversation(user_id=u.id); s.add(c); s.commit(); s.refresh(c)
    return u.id, c.id


def test_chat_turn_metric_roundtrips():
    eng = _engine()
    with Session(eng) as s:
        uid, cid = _seed_user_conv(s)
        s.add(ChatTurnMetric(conversation_id=cid, user_id=uid, latency_ms=1200,
                             ttft_ms=300, input_tokens=100, output_tokens=50,
                             cache_read_tokens=80, cache_write_tokens=10,
                             n_rounds=2, n_tool_calls=1, tool_names=["screen"],
                             model="claude-sonnet-4-6", stop_reason="end_turn", ok=True))
        s.commit()
        row = s.exec(select(ChatTurnMetric)).one()
        assert row.conversation_id == cid and row.user_id == uid
        assert row.tool_names == ["screen"] and row.n_rounds == 2
        assert row.ok is True and row.created_at is not None


# ── 캡처: stream_chat_turn → ChatTurnMetric ──────────────────────────────────

class _Usage:
    def __init__(self, **kw): self.__dict__.update(kw)

class _B:  # content block
    def __init__(self, **kw): self.__dict__.update(kw)

class _Msg:
    def __init__(self, content, stop_reason, usage=None):
        self.content, self.stop_reason, self.usage = content, stop_reason, usage

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
    def stream(self, **kw): return _Stream(self._queue.pop(0))

class _FakeClient:
    def __init__(self, msgs): self.messages = _Msgs(msgs)


def test_metric_persisted_on_success():
    from app.chat.agent import run_chat_turn
    eng = _engine()
    with Session(eng) as s:
        _, cid = _seed_user_conv(s)
        client = _FakeClient([_Msg([_B(type="text", text="삼성전자는 저평가입니다.")],
                                   "end_turn",
                                   usage=_Usage(input_tokens=100, output_tokens=50,
                                                cache_read_input_tokens=80,
                                                cache_creation_input_tokens=10))])
        run_chat_turn(s, cid, "삼성전자 어때?", client=client, model="claude-sonnet-4-6")
        m = s.exec(select(ChatTurnMetric)).one()
        assert m.input_tokens == 100 and m.output_tokens == 50
        assert m.cache_read_tokens == 80 and m.cache_write_tokens == 10
        assert m.n_rounds == 1 and m.n_tool_calls == 0 and m.tool_names == []
        assert m.ok is True and m.latency_ms >= 0 and m.model == "claude-sonnet-4-6"
        assert m.stop_reason == "end_turn"


def test_metric_records_tools_and_rounds(monkeypatch):
    from app.chat import agent as ag
    monkeypatch.setattr(ag, "run_tool", lambda name, inp: {"success": True, "results": []})
    eng = _engine()
    with Session(eng) as s:
        _, cid = _seed_user_conv(s)
        client = _FakeClient([
            _Msg([_B(type="text", text="스크리닝할게요"),
                  _B(type="tool_use", id="t1", name="screen", input={"top_n": 3})],
                 "tool_use", usage=_Usage(input_tokens=200, output_tokens=20)),
            _Msg([_B(type="text", text="AAA가 저평가입니다.")], "end_turn",
                 usage=_Usage(input_tokens=300, output_tokens=40)),
        ])
        ag.run_chat_turn(s, cid, "저평가주 골라줘", client=client)
        m = s.exec(select(ChatTurnMetric)).one()
        assert m.n_rounds == 2 and m.n_tool_calls == 1 and m.tool_names == ["screen"]
        assert m.input_tokens == 500 and m.output_tokens == 60   # 라운드 합


def test_metric_ok_false_on_error():
    from app.chat.agent import run_chat_turn
    eng = _engine()
    class _Boom:
        def stream(self, **kw): raise RuntimeError("LLM down")
    class _C:  # noqa
        messages = _Boom()
    with Session(eng) as s:
        _, cid = _seed_user_conv(s)
        run_chat_turn(s, cid, "안녕", client=_C())
        m = s.exec(select(ChatTurnMetric)).one()
        assert m.ok is False and m.n_rounds == 0


# ── CLI: compute_stats ───────────────────────────────────────────────────────

def _seed_metric(s, cid, uid, **kw):
    base = dict(conversation_id=cid, user_id=uid, latency_ms=1000, ttft_ms=200,
                input_tokens=100, output_tokens=50, cache_read_tokens=0,
                cache_write_tokens=0, n_rounds=1, n_tool_calls=0, tool_names=[],
                model="m", stop_reason="end_turn", ok=True)
    base.update(kw)
    s.add(ChatTurnMetric(**base)); s.commit()


def test_compute_stats_empty():
    from app.chat_analytics import compute_stats
    eng = _engine()
    with Session(eng) as s:
        assert compute_stats(s, days=7)["turns"] == 0


def test_compute_stats_aggregates():
    from app.chat_analytics import compute_stats
    eng = _engine()
    with Session(eng) as s:
        uid, cid = _seed_user_conv(s)
        for lat in (100, 200, 300, 400, 1000):
            _seed_metric(s, cid, uid, latency_ms=lat, input_tokens=lat,
                         cache_read_tokens=lat // 2)
        _seed_metric(s, cid, uid, ok=False, n_tool_calls=1, tool_names=["screen"])
        st = compute_stats(s, days=7)
        assert st["turns"] == 6
        assert st["latency_ms"]["max"] == 1000
        assert st["latency_ms"]["p50"] in (200, 300)       # 6개 중앙값 근방
        assert st["tools"] == {"screen": 1}
        assert st["error_rate"] == round(1 / 6, 3)
        assert 0.0 < st["cache_hit_rate"] < 1.0


def test_compute_stats_respects_days_window():
    from app.chat_analytics import compute_stats
    eng = _engine()
    with Session(eng) as s:
        uid, cid = _seed_user_conv(s)
        old = ChatTurnMetric(conversation_id=cid, user_id=uid,
                             created_at=datetime.now(timezone.utc) - timedelta(days=10))
        s.add(old); s.commit()
        _seed_metric(s, cid, uid)                          # 오늘
        assert compute_stats(s, days=7)["turns"] == 1      # 10일 전 제외


# ── CLI: render_transcripts ──────────────────────────────────────────────────

def _seed_turn(s, cid, q, assistant_parts, **metric_kw):
    s.add(Message(conversation_id=cid, role="user", parts=[{"type": "text", "text": q}]))
    s.add(Message(conversation_id=cid, role="assistant", parts=assistant_parts))
    s.commit()
    _seed_metric(s, cid, None, **metric_kw)


def test_render_transcripts_includes_qa_tools_metrics():
    from app.chat_analytics import render_transcripts
    eng = _engine()
    with Session(eng) as s:
        _, cid = _seed_user_conv(s)
        _seed_turn(s, cid, "저평가주 골라줘",
                   [{"type": "tool_use", "id": "t1", "name": "screen", "input": {"top_n": 3}},
                    {"type": "tool_result", "tool_use_id": "t1", "name": "screen",
                     "result": {"success": True, "results": [{"symbol": "AAA", "per": 7.1}]}},
                    {"type": "text", "text": "AAA가 가장 저평가입니다."}],
                   n_tool_calls=1, tool_names=["screen"], input_tokens=300)
        txt = render_transcripts(s, days=7)
        assert "저평가주 골라줘" in txt          # 질문
        assert "screen" in txt and "AAA" in txt  # 도구 호출 + full 결과(근거 대조용)
        assert "AAA가 가장 저평가입니다." in txt  # 답변
        assert "300" in txt                      # 턴 지표(토큰) 인라인


def test_render_transcripts_suspect_filters_no_tool_and_hedge():
    from app.chat_analytics import render_transcripts
    eng = _engine()
    with Session(eng) as s:
        _, cid = _seed_user_conv(s)
        # 정상 도구 턴 — suspect 아님
        _seed_turn(s, cid, "삼성전자 PER 보여줘",
                   [{"type": "text", "text": "삼성전자 PER은 11.2입니다."}],
                   n_tool_calls=1, tool_names=["inspect"])
        # 미답변 후보 — 도구 0 + 회피표현
        _seed_turn(s, cid, "지난번 그 종목 다시 보여줘",
                   [{"type": "text", "text": "이전 대화를 확인할 수 없어요."}],
                   n_tool_calls=0)
        full = render_transcripts(s, days=7)
        assert full.count("[유저]") == 2
        sus = render_transcripts(s, days=7, suspect=True)
        assert "지난번 그 종목" in sus            # 후보는 포함
        assert "삼성전자 PER 보여줘" not in sus    # 정상 턴은 제외


# ── CLI 배선 스모크 ──────────────────────────────────────────────────────────

def test_main_cli_stats_runs(monkeypatch, capsys):
    import app.chat_analytics as ca
    eng = _engine()
    with Session(eng) as s:
        uid, cid = _seed_user_conv(s)
        _seed_metric(s, cid, uid)
    monkeypatch.setattr(ca, "engine", eng)          # 글로벌 engine을 테스트 DB로
    ca.main_cli(["stats", "--days", "7"])
    captured = capsys.readouterr().out
    assert "챗봇 성능" in captured and "turns=1" in captured

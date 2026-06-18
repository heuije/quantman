# 챗봇 성능 측정·개선 환경 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 챗봇 turn별 성능 지표(토큰·지연·라운드·도구)를 DB에 적재하고, `railway run`으로 prod를 조회하는 `chat_analytics` CLI(stats/transcripts) + 정확도 루브릭·진단 런북 docs를 만들어, Claude Code가 별도 LLM API 없이 챗봇을 진단·개선하는 환경을 구축한다.

**Architecture:** `stream_chat_turn`(server/app/chat/agent.py)에 타이밍+usage 누적 계측을 더해 턴 종료 시 `ChatTurnMetric` 1행을 적재한다(내용은 기존 Message에). `python -m app.chat_analytics`(server/app/, `manage.py` 패턴 미러)가 `from app.db import engine`로 Neon/SQLite를 조회해 집계(stats)와 가독 트랜스크립트(transcripts)를 stdout으로 낸다. 정확도 채점은 Claude Code가 트랜스크립트를 읽어 루브릭(docs/chat-perf/)으로 수행한다.

**Tech Stack:** Python 3.12 · FastAPI · SQLModel/SQLAlchemy · argparse · pytest · (배포) Railway + Neon Postgres

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `server/app/models.py` | DB 모델 | `ChatTurnMetric` 추가 |
| `server/app/chat/agent.py` | agent 루프·영속 | `stream_chat_turn` 계측 + 적재 헬퍼 |
| `server/app/chat_analytics.py` | 분석 CLI (집계·트랜스크립트) | **신규** |
| `server/tests/test_chat_analytics.py` | 캡처·CLI 단위 테스트 | **신규** |
| `docs/chat-perf/accuracy-rubric.md` | 정확도 채점 기준틀 | **신규** |
| `docs/chat-perf/diagnosis-runbook.md` | 진단→수정→검증 런북 | **신규** |

전부 챗봇 측정 책임에 응집. CLI는 순수 함수(`compute_stats`/`render_transcripts`, session 인자)와 얇은 argparse glue로 분리 — 순수 함수만 단위 테스트한다.

**작업 브랜치:** `feat/chat-perf-measurement` (worktree `_wt-chat-perf`, origin/main=dc15044 기준). 모든 커밋 이 브랜치. **push/머지는 사용자 명시 허락 시에만.**

**테스트 실행:** `cd server` 후 `python -m pytest tests/test_chat_analytics.py -q`. (conftest가 in-repo core·server를 sys.path 최우선에 둠.)

---

## Task 1: `ChatTurnMetric` 모델

**Files:**
- Modify: `server/app/models.py` (CompileLog 정의 뒤에 클래스 추가)
- Test: `server/tests/test_chat_analytics.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `server/tests/test_chat_analytics.py` 생성

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `cd server && python -m pytest tests/test_chat_analytics.py -q`
Expected: FAIL — `ImportError: cannot import name 'ChatTurnMetric' from 'app.models'`

- [ ] **Step 3: 모델 구현** — `server/app/models.py`의 `CompileLog` 클래스 정의 **직후**에 추가

```python
class ChatTurnMetric(SQLModel, table=True):
    """챗봇 turn별 성능 지표 — 토큰·지연·라운드·도구. 내용(질문·답변)은 Message가
    단일 진실원천이고 여기엔 숫자만 둔다(chat-perf 측정 환경, CompileLog 패턴 미러)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(index=True, foreign_key="conversation.id")
    user_id: Optional[int] = Field(default=None, index=True, foreign_key="user.id")
    created_at: datetime = Field(default_factory=_now)
    latency_ms: int = 0            # 턴 전체 wall-clock
    ttft_ms: Optional[int] = None  # 첫 델타까지(도구-only 턴은 None)
    input_tokens: int = 0          # 턴 내 라운드 합
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    n_rounds: int = 0              # 도구 라운드 수
    n_tool_calls: int = 0
    tool_names: list = Field(default_factory=list, sa_column=Column(JSON))
    model: str = ""
    stop_reason: Optional[str] = None
    ok: bool = True               # 턴 정상 종료 여부(에러=False)
```

> `Optional`·`datetime`·`Field`·`Column`·`JSON`·`_now`는 models.py 상단에 이미 import 되어 있음(다른 모델이 사용). 추가 import 불필요.

- [ ] **Step 4: 통과 확인**

Run: `cd server && python -m pytest tests/test_chat_analytics.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: 커밋**

```bash
git add server/app/models.py server/tests/test_chat_analytics.py
git commit -m "feat(chat-perf): ChatTurnMetric 모델 — turn별 성능 지표 테이블"
```

---

## Task 2: `stream_chat_turn` 계측 + 적재

**Files:**
- Modify: `server/app/chat/agent.py` (import 1줄, 헬퍼 2개, `stream_chat_turn` 본문)
- Test: `server/tests/test_chat_analytics.py` (fake 클라이언트 + 캡처 테스트 3개)

- [ ] **Step 1: 실패 테스트 작성** — `test_chat_analytics.py` 하단에 추가

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `cd server && python -m pytest tests/test_chat_analytics.py -q`
Expected: 3개 신규 테스트 FAIL — `ChatTurnMetric` row가 없어 `.one()`이 `NoResultFound`.

- [ ] **Step 3: 계측 구현** — `server/app/chat/agent.py`

(a) 상단 import에 `time` 추가, 모델 import에 `ChatTurnMetric` 추가:

```python
import logging
import time
```

```python
from ..models import ChatTurnMetric, Conversation, Message
```

(b) `_log_usage` 함수 **직후**에 헬퍼 2개 추가:

```python
def _accumulate_usage(acc: dict, usage) -> None:
    """라운드별 Anthropic usage를 턴 누적기에 더한다(usage 없으면 무동작)."""
    if usage is None:
        return
    acc["in"] += getattr(usage, "input_tokens", 0) or 0
    acc["out"] += getattr(usage, "output_tokens", 0) or 0
    acc["cr"] += getattr(usage, "cache_read_input_tokens", 0) or 0
    acc["cw"] += getattr(usage, "cache_creation_input_tokens", 0) or 0


def _persist_turn_metric(session: Session, conversation_id: int, model: str,
                         acc: dict, ttft_ms, latency_ms: int, ok: bool) -> None:
    """턴별 ChatTurnMetric 1행 적재(chat-perf 측정 환경). user_id는 대화 소유자.

    적재 실패가 대화 응답을 깨지 않도록 격리한다 — DB 일시오류 시 지표 누락은 허용하고
    대화는 보존(외부 시스템 한계라 fallback 정당). 대화 영속(_persist)은 이미 끝난 뒤다.
    """
    try:
        conv = session.get(Conversation, conversation_id)
        session.add(ChatTurnMetric(
            conversation_id=conversation_id,
            user_id=conv.user_id if conv else None,
            latency_ms=latency_ms, ttft_ms=ttft_ms,
            input_tokens=acc["in"], output_tokens=acc["out"],
            cache_read_tokens=acc["cr"], cache_write_tokens=acc["cw"],
            n_rounds=acc["rounds"], n_tool_calls=len(acc["tools"]),
            tool_names=list(acc["tools"]), model=model,
            stop_reason=acc["stop"], ok=ok))
        session.commit()
    except Exception:   # noqa: BLE001 — 지표 누락 허용·대화 보존(원칙: 외부 한계 fallback)
        _log.exception("[chat metric] 적재 실패 conv=%s", conversation_id)
        session.rollback()
```

(c) `stream_chat_turn` 본문을 아래로 교체(계측 누적기 `acc`·`ttft_ms`·`ok` 추가, 첫 델타에서 ttft 기록, 라운드마다 누적, 종료 시 적재):

```python
    messages = _history_to_wire(session, conversation_id)
    _mark_cache_breakpoint(messages)      # ① 히스토리 prompt caching(멀티턴 입력 캐시 재사용)
    messages.append({"role": "user", "content": user_text})
    _persist(session, conversation_id, "user", [{"type": "text", "text": user_text}])

    assistant_parts: list[dict] = []      # full payload(영속·렌더용)
    # ── 성능 계측(chat-perf) — 턴 종료 시 ChatTurnMetric 1행 ──
    t0 = time.perf_counter()
    acc = {"in": 0, "out": 0, "cr": 0, "cw": 0, "rounds": 0, "tools": [], "stop": None}
    ttft_ms = None
    ok = True
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            with client.messages.stream(model=model, max_tokens=4096, system=system,
                                        tools=TOOL_SCHEMAS, messages=messages) as stream:
                for delta in stream.text_stream:
                    if delta:
                        if ttft_ms is None:
                            ttft_ms = int((time.perf_counter() - t0) * 1000)
                        yield ("delta", {"text": delta})
                resp = stream.get_final_message()
            _accumulate_usage(acc, getattr(resp, "usage", None))        # 토큰 누적
            _log_usage(conversation_id, getattr(resp, "usage", None))   # ② 라운드별 로그(유지)
            acc["rounds"] += 1
            acc["stop"] = resp.stop_reason

            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    assistant_parts.append({"type": "text", "text": b.text})
            messages.append({"role": "assistant",
                             "content": [_block_to_wire(b) for b in resp.content]})

            if resp.stop_reason != "tool_use":
                break

            tool_results: list[dict] = []
            for b in resp.content:
                if getattr(b, "type", None) != "tool_use":
                    continue
                inp = dict(b.input or {})
                acc["tools"].append(b.name)
                yield ("tool_use", {"id": b.id, "name": b.name, "input": inp})
                if b.name == "save_strategy":
                    conv = session.get(Conversation, conversation_id)
                    full = save_strategy_tool(session, conv.user_id if conv else None, inp)
                else:
                    full = run_tool(b.name, inp)
                assistant_parts.append({"type": "tool_use", "id": b.id, "name": b.name, "input": inp})
                assistant_parts.append({"type": "tool_result", "tool_use_id": b.id,
                                        "name": b.name, "result": full})
                yield ("tool_result", {"tool_use_id": b.id, "name": b.name, "result": full})
                tool_results.append({"type": "tool_result", "tool_use_id": b.id,
                                     "content": compact_summary(b.name, full)})
            messages.append({"role": "user", "content": tool_results})
    except Exception:   # noqa: BLE001 — 외부 LLM·도구 호출 실패는 대화에 오류 답변으로 표면화(고아 방지)
        ok = False
        _log.exception("[chat] turn failed for conversation %s", conversation_id)
        err = "분석 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요."
        assistant_parts.append({"type": "text", "text": err})
        yield ("delta", {"text": err})
    _persist(session, conversation_id, "assistant", assistant_parts)
    _persist_turn_metric(session, conversation_id, model, acc, ttft_ms,
                         int((time.perf_counter() - t0) * 1000), ok)
    yield ("done", {"parts": assistant_parts})
```

> 변경 요지: `t0`/`acc`/`ttft_ms`/`ok` 추가 · 첫 델타에서 `ttft_ms` 기록 · `_accumulate_usage` + `acc["rounds"]`/`acc["stop"]`/`acc["tools"]` 누적 · except에서 `ok=False` · 마지막에 `_persist_turn_metric` 호출. 나머지 루프 로직은 기존과 동일.

- [ ] **Step 4: 통과 확인 (캡처 + 기존 챗봇 테스트 회귀 없음)**

Run: `cd server && python -m pytest tests/test_chat_analytics.py tests/test_chat_agent.py tests/test_chat_api.py -q`
Expected: PASS (신규 캡처 3 + 기존 챗봇 전부 green)

- [ ] **Step 5: 커밋**

```bash
git add server/app/chat/agent.py server/tests/test_chat_analytics.py
git commit -m "feat(chat-perf): stream_chat_turn 계측 — 턴별 토큰·지연·도구 적재"
```

---

## Task 3: 분석 CLI — `stats` 집계

**Files:**
- Create: `server/app/chat_analytics.py`
- Test: `server/tests/test_chat_analytics.py` (stats 테스트 추가)

- [ ] **Step 1: 실패 테스트 작성** — `test_chat_analytics.py` 하단에 추가

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `cd server && python -m pytest tests/test_chat_analytics.py -k compute_stats -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.chat_analytics'`

- [ ] **Step 3: CLI 모듈 + stats 구현** — `server/app/chat_analytics.py` 생성

```python
"""챗봇 성능 분석 CLI — 측정+진단 환경. railway run으로 prod Neon 조회.

사용:
    railway run python -m app.chat_analytics stats --days 7
    railway run python -m app.chat_analytics transcripts --days 7 --suspect
    (로컬: QP_DB_URL 미설정 시 SQLite — 빈 데이터)

집계(stats)·트랜스크립트(transcripts)는 순수 함수(session 인자)로 분리해 단위 테스트하고,
argparse glue는 from .db import engine으로 세션을 열어 호출·출력만 한다(manage.py 패턴).
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from .db import engine
from .models import ChatTurnMetric, Conversation, Message


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _pct(vals: list[int], p: int) -> int:
    """하위-순위 분위수(numpy 없이, 실제 관측값 반환). 빈 리스트는 0.
    (선형 보간이 아닌 floor — p50 등이 보간된 가짜값 대신 실측값이 되도록.)"""
    if not vals:
        return 0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100
    return int(s[int(k)])


def compute_stats(session: Session, days: int = 7) -> dict:
    """최근 days일(KST 무관·UTC 윈도) 챗봇 turn 지표 집계."""
    rows = session.exec(
        select(ChatTurnMetric).where(ChatTurnMetric.created_at >= _since(days))).all()
    n = len(rows)
    if not n:
        return {"turns": 0, "days": days}
    inp = [r.input_tokens for r in rows]
    out = [r.output_tokens for r in rows]
    cr = [r.cache_read_tokens for r in rows]
    lat = [r.latency_ms for r in rows]
    ttft = [r.ttft_ms for r in rows if r.ttft_ms is not None]
    tools = Counter(t for r in rows for t in (r.tool_names or []))
    rounds = Counter(r.n_rounds for r in rows)
    total_in, total_cr = sum(inp), sum(cr)
    return {
        "turns": n,
        "users": len({r.user_id for r in rows}),
        "days": days,
        "input_tok": {"p50": _pct(inp, 50), "p90": _pct(inp, 90), "max": max(inp)},
        "output_tok": {"p50": _pct(out, 50), "p90": _pct(out, 90), "max": max(out)},
        "cache_read_tok": {"p50": _pct(cr, 50), "p90": _pct(cr, 90), "max": max(cr)},
        "cache_hit_rate": round(total_cr / (total_in + total_cr), 3) if (total_in + total_cr) else 0.0,
        "latency_ms": {"p50": _pct(lat, 50), "p90": _pct(lat, 90), "max": max(lat)},
        "ttft_ms": ({"p50": _pct(ttft, 50), "p90": _pct(ttft, 90), "max": max(ttft)}
                    if ttft else None),
        "tools": dict(tools.most_common()),
        "rounds_dist": dict(sorted(rounds.items())),
        "error_rate": round(sum(1 for r in rows if not r.ok) / n, 3),
    }


def format_stats(st: dict) -> str:
    if not st.get("turns"):
        return f"(최근 {st['days']}일: 챗봇 turn 데이터 없음)"
    def trio(d): return f"p50={d['p50']} p90={d['p90']} max={d['max']}"
    lines = [
        f"=== 챗봇 성능 (최근 {st['days']}일) ===",
        f"  turns={st['turns']}  users={st['users']}  error_rate={st['error_rate']}",
        f"  input_tok   {trio(st['input_tok'])}",
        f"  output_tok  {trio(st['output_tok'])}",
        f"  cache_read  {trio(st['cache_read_tok'])}  hit_rate={st['cache_hit_rate']}",
        f"  latency_ms  {trio(st['latency_ms'])}",
        f"  ttft_ms     {trio(st['ttft_ms']) if st['ttft_ms'] else '(없음)'}",
        f"  tools       {st['tools'] or '(없음)'}",
        f"  rounds_dist {st['rounds_dist']}",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: 통과 확인**

Run: `cd server && python -m pytest tests/test_chat_analytics.py -k compute_stats -q`
Expected: PASS (3 passed)

- [ ] **Step 5: 커밋**

```bash
git add server/app/chat_analytics.py server/tests/test_chat_analytics.py
git commit -m "feat(chat-perf): chat_analytics stats — 토큰·지연·도구 집계"
```

---

## Task 4: 분석 CLI — `transcripts` + `--suspect`

**Files:**
- Modify: `server/app/chat_analytics.py` (함수 추가)
- Test: `server/tests/test_chat_analytics.py` (transcripts 테스트 추가)

- [ ] **Step 1: 실패 테스트 작성** — `test_chat_analytics.py` 하단에 추가

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `cd server && python -m pytest tests/test_chat_analytics.py -k transcripts -q`
Expected: FAIL — `ImportError: cannot import name 'render_transcripts'`

- [ ] **Step 3: 구현** — `server/app/chat_analytics.py`의 `format_stats` **뒤**에 추가

```python
_HEDGES = ("할 수 없", "지원하지 않", "확인이 어렵", "확인할 수 없", "제공할 수 없", "알 수 없")
_NEGATIONS = ("아니", "그게 아니", "그거 말고")


def _text_of(parts: list) -> str:
    return " ".join(p.get("text", "") for p in parts if p.get("type") == "text")


def _is_suspect(metric, answer: str, next_user_text: str | None) -> bool:
    """미답변 후보 표층 휴리스틱(별도 API 0) — 판정 아닌 우선순위 신호."""
    if metric is not None and metric.n_tool_calls == 0:
        return True
    if any(h in answer for h in _HEDGES):
        return True
    if next_user_text and any(neg in next_user_text for neg in _NEGATIONS):
        return True
    return False


def _render_part(p: dict) -> str:
    t = p.get("type")
    if t == "text":
        return f"  [봇] {p.get('text', '')}"
    if t == "tool_use":
        return f"  [도구] {p.get('name')}({p.get('input')})"
    if t == "tool_result":
        return f"  [결과] {p.get('name')}: {p.get('result')}"
    return ""


def render_transcripts(session: Session, days: int = 7, limit: int | None = None,
                       conv_id: int | None = None, suspect: bool = False) -> str:
    """대화별·턴별 가독 트랜스크립트(정확도 채점용). full 도구결과 포함 → 근거 대조 가능.

    suspect=True면 미답변 후보 턴만(표층 휴리스틱). limit은 대화 수 상한.
    """
    q = select(Conversation)
    if conv_id is not None:
        q = q.where(Conversation.id == conv_id)
    else:
        q = q.where(Conversation.created_at >= _since(days)).order_by(Conversation.id.desc())
    convs = session.exec(q).all()

    blocks: list[str] = []
    shown = 0
    for conv in convs:
        if limit is not None and shown >= limit:
            break
        msgs = session.exec(select(Message).where(Message.conversation_id == conv.id)
                            .order_by(Message.id)).all()
        metrics = session.exec(select(ChatTurnMetric)
                               .where(ChatTurnMetric.conversation_id == conv.id)
                               .order_by(ChatTurnMetric.id)).all()
        # user→assistant 쌍을 턴으로 묶고, 생성순 metric을 1:1 매핑.
        users = [m for m in msgs if m.role == "user"]
        assts = [m for m in msgs if m.role == "assistant"]
        turn_lines: list[str] = []
        for i, um in enumerate(users):
            am = assts[i] if i < len(assts) else None
            met = metrics[i] if i < len(metrics) else None
            answer = _text_of(am.parts) if am else ""
            next_user = users[i + 1].parts[0].get("text") if i + 1 < len(users) else None
            if suspect and not _is_suspect(met, answer, next_user):
                continue
            lines = [f"  [유저] {um.parts[0].get('text', '')}"]
            if am:
                lines += [_render_part(p) for p in am.parts if _render_part(p)]
            if met:
                lines.append(f"  · {met.input_tokens}+{met.output_tokens}tok "
                             f"cache_read={met.cache_read_tokens} {met.latency_ms}ms "
                             f"rounds={met.n_rounds} tools={met.tool_names} ok={met.ok}")
            turn_lines.append("\n".join(lines))
        if turn_lines:
            blocks.append(f"=== conv #{conv.id} (user {conv.user_id}) ===\n"
                          + "\n  ---\n".join(turn_lines))
            shown += 1
    return "\n\n".join(blocks) if blocks else "(해당 트랜스크립트 없음)"
```

> 매핑 단순화: i번째 user 턴 ↔ i번째 assistant ↔ i번째 metric. 챗봇은 턴마다 user 1 + assistant 1 + metric 1을 순서대로 영속하므로 인덱스 정렬이 성립한다(에러 턴도 assistant 1 + metric 1 기록).

- [ ] **Step 4: 통과 확인**

Run: `cd server && python -m pytest tests/test_chat_analytics.py -q`
Expected: PASS (캡처 + stats + transcripts 전부)

- [ ] **Step 5: 커밋**

```bash
git add server/app/chat_analytics.py server/tests/test_chat_analytics.py
git commit -m "feat(chat-perf): chat_analytics transcripts(+--suspect) — 채점용 트랜스크립트"
```

---

## Task 5: CLI argparse 배선 + `__main__`

**Files:**
- Modify: `server/app/chat_analytics.py` (`main_cli` + `__main__`)
- Test: `server/tests/test_chat_analytics.py` (engine 패치 스모크)

- [ ] **Step 1: 실패 테스트 작성** — `test_chat_analytics.py` 하단에 추가

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `cd server && python -m pytest tests/test_chat_analytics.py -k main_cli -q`
Expected: FAIL — `AttributeError: module 'app.chat_analytics' has no attribute 'main_cli'`

- [ ] **Step 3: 구현** — `server/app/chat_analytics.py` 맨 끝에 추가

```python
def main_cli(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="챗봇 성능 분석 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_stats = sub.add_parser("stats", help="토큰·지연·도구 집계")
    p_stats.add_argument("--days", type=int, default=7)

    p_tr = sub.add_parser("transcripts", help="채점용 가독 트랜스크립트")
    p_tr.add_argument("--days", type=int, default=7)
    p_tr.add_argument("--limit", type=int, default=None, help="대화 수 상한")
    p_tr.add_argument("--conv", type=int, default=None, help="특정 대화 id")
    p_tr.add_argument("--suspect", action="store_true", help="미답변 후보만(휴리스틱)")

    args = ap.parse_args(argv)
    with Session(engine) as s:
        if args.cmd == "stats":
            print(format_stats(compute_stats(s, days=args.days)))
        elif args.cmd == "transcripts":
            print(render_transcripts(s, days=args.days, limit=args.limit,
                                     conv_id=args.conv, suspect=args.suspect))


if __name__ == "__main__":
    main_cli()
```

- [ ] **Step 4: 통과 확인 (전체 파일)**

Run: `cd server && python -m pytest tests/test_chat_analytics.py -q`
Expected: PASS (전부)

- [ ] **Step 5: 로컬 실행 스모크 (빈 SQLite여도 깨지지 않음)**

Run: `cd server && python -m app.chat_analytics stats --days 7`
Expected: `(최근 7일: 챗봇 turn 데이터 없음)` 또는 로컬 data.db에 데이터가 있으면 집계 출력. 에러 없이 종료(exit 0).

- [ ] **Step 6: 커밋**

```bash
git add server/app/chat_analytics.py server/tests/test_chat_analytics.py
git commit -m "feat(chat-perf): chat_analytics argparse CLI(stats/transcripts) + __main__"
```

---

## Task 6: 정확도 루브릭 doc

**Files:**
- Create: `docs/chat-perf/accuracy-rubric.md`

- [ ] **Step 1: 파일 생성** — 아래 내용 그대로

````markdown
# 챗봇 답변 정확도 루브릭

Claude Code가 `python -m app.chat_analytics transcripts`로 뽑은 트랜스크립트를 읽고 적용한다.
별도 LLM API를 쓰지 않는다 — 채점은 Claude Code 본인이 한다.

## 계층형 역량 인벤토리 (갭 계층 태깅의 기준틀)

채점 전 "이 질문이 현재 역량 안인가"를 판정하려면 무엇이 되는지 알아야 한다. 4계층 호환 계약과 정렬.

- **① 노출 도구(챗봇 tool):** `screen`(스크리닝) · `simulate`(백테스트) · `save_strategy`(저장) ·
  `describe`(단일종목/포트폴리오 진단) · `inspect`(종목 시계열 컬럼 조회).
- **② 가용 데이터:** KR/US OHLCV · 펀더멘털(PER/PBR 등) · 섹터 분류. **미수급(기지 갭):** 뉴스·추정치(컨센서스)·
  수급(플로우)·인트라데이·옵션체인 — `docs/...데이터갭 분석` 참조.
- **③ 엔진 분석로직(IR verbs):** select · describe · relate(회귀) · simulate · extremize.

## 4축 채점 (축별 pass / partial / fail + 한 줄 사유)

1. **의도 이해·가이드** (최우선): 모호해도 진짜 의도를 파악했나? 생산적으로 안내했나(협의 = 선택지·추천 제시,
   더 나은 프레이밍 제안)? 단순 직역 실행에 그치지 않았나?
2. **도구·근거 정확성:** 의도에 맞는 도구를 골랐나? 답변의 수치가 `[결과]` full payload에 실제로 근거하나(날조·
   환각 없음)? — 트랜스크립트의 도구 결과와 답변 숫자를 직접 대조.
3. **질문 완결성:** 끝까지 답했나 / 적절히 되물었나? 빠뜨린 맥락은?
4. **준비도·커버리지:** 역량 밖 질문인가? (a)+(b) 아래.

### 축4 세부

- **(a) 처리 방식:** `graceful`(정직한 한계 고지 + 우회 제안) vs `bad`(환각·자신있게 틀림·무시).
  *준비된 봇은 모르면 곱게 실패해야 한다 — bad는 품질 fail.*
- **(b) 2-facet 태깅:**
  - **증상 태그**(질문에서): `history-reference` · `data-metadata` · `sector/qualitative-filter` · `analysis-type-<x>` …
  - **근본원인 계층 태그**(수정 라우팅):

    | 태그 | 의미 | 수정 트랙 |
    |---|---|---|
    | `missing-tool` | 엔진·데이터엔 있으나 챗봇 도구로 미노출 | 도구 배선(최저비용) |
    | `missing-data` | 기반 데이터 미수급 | 데이터엔진 수급(외부·고비용) |
    | `missing-logic` | IR/엔진 분석 프리미티브 부재 | 엔진 신설 |
    | `missing-metadata-access` | 데이터는 있으나 메타(as-of·유니버스·출처·커버리지) 질의 수단 없음 | 도구/프롬프트 |
    | `history-context` | 과거 대화·결과 참조 실패(컴팩트·리텐션) | 아키텍처 |
    | `out-of-scope` | 설계상 미지원(개인자문·실행) | 올바른 거절이면 OK |

## 채점 출력 형식 (대화당)

```
conv #<id> turn <n>: 의도=pass 근거=pass 완결=partial 커버리지=fail(graceful, history-reference/history-context)
  사유: "아까 그 종목"을 참조했으나 이전 screen 결과가 compact로 치환돼 재호출 못 함.
```

태그를 모아 빈도순으로 정렬 → 개선·로드맵 우선순위(`docs/chat-perf/diagnosis-runbook.md`).
````

- [ ] **Step 2: 커밋**

```bash
git add docs/chat-perf/accuracy-rubric.md
git commit -m "docs(chat-perf): 정확도 4축 루브릭 + 계층형 역량 인벤토리 + 갭 2-facet 태깅"
```

---

## Task 7: 진단→수정→검증 런북 doc

**Files:**
- Create: `docs/chat-perf/diagnosis-runbook.md`

- [ ] **Step 1: 파일 생성** — 아래 내용 그대로

````markdown
# 챗봇 진단→수정→검증 런북

## 0. 데이터 끌어오기 (Claude Code 세션에서)

```bash
railway run python -m app.chat_analytics stats --days 7
railway run python -m app.chat_analytics transcripts --days 7 --suspect
railway run python -m app.chat_analytics transcripts --conv <id>     # 특정 대화 정밀
```
`railway`는 Neon URL(QP_DB_URL)을 주입한다. 로컬(QP_DB_URL 미설정)은 SQLite를 본다.

## 1. 정량 진단 (stats)

| 신호 | 의심 근본원인 |
|---|---|
| `latency_ms.p90` 높음 + `rounds_dist` 큰 라운드 多 | 도구 과호출·프롬프트 비효율(불필요한 도구 루프) |
| `ttft_ms.p90` 높음 | 첫 라운드 입력 비대(컨텍스트·시스템 프롬프트) |
| `cache_hit_rate` ≈ 0 지속 | 히스토리 prompt caching 미작동(PR#159 회귀) |
| `input_tok.p90` 큼 | 컨텍스트 비대(히스토리 compact 미흡·도구결과 과다) |
| `error_rate` 상승 | 도구 예외·LLM 실패 — Railway 로그 `[chat] turn failed` 대조 |
| `tools` 편중 | 특정 도구 과/미사용 — 의도 매칭 점검 |

## 2. 정성 진단 (transcripts + 루브릭)

1. `--suspect`로 미답변 후보부터 본다.
2. 각 턴을 `accuracy-rubric.md` 4축으로 채점, 축4는 2-facet 태깅.
3. 태그를 빈도순 집계.

## 3. 두 루프

### 품질 루프 (축 1~3 + 토큰/지연)
증상 → 근본원인 → **타깃 수정** → **검증**:
- 근본원인 후보: 프롬프트 갭(prompt.py) · 도구 스키마/설명(tools.py) · 컨텍스트 비대(history compact) · 모델 티어(QP_CHAT_MODEL).
- 검증: ① 회귀를 고정하는 unit test 추가(`test_chat_*`) ② 수정 후 `stats` 재실행해 토큰/지연 델타 확인 ③ 같은 류 질문 샘플 재채점.

### 로드맵 루프 (축 4)
미충족 의도의 **계층 태그**를 집계 → 계층별 라우팅:
- `missing-tool` → 엔진·데이터에 이미 있으니 챗봇 도구로 배선(가장 싼 개선). 예: 섹터필터.
- `missing-data` → 데이터엔진 수급 백로그(외부 의존·고비용).
- `missing-logic` → 엔진 프리미티브 신설.
- `missing-metadata-access` → 메타 질의 도구 또는 시스템 프롬프트 보강.
- `history-context` → 컨텍스트 유지 아키텍처(compact 설계 재검토).
- 우선순위 = 빈도 × 가치.

## 4. 진단 예시

- `cache_hit_rate=0` 7일 연속 → 히스토리 마커 미작동 가설 → agent.py `_mark_cache_breakpoint` + Railway `[chat usage] cache_read` 대조 → 수정 → `stats`로 hit_rate 상승 확인.
- "저평가 반도체주" 반복 fail, tag=`missing-tool`(섹터필터 미노출) → screen에 섹터 인자 배선 → 재질문 재채점.
- "아까 그 종목 다시" fail, tag=`history-context` → compact(full→compact)가 이전 도구결과를 치환 → 참조형 질문에 한해 full 유지/재조회 설계.
````

- [ ] **Step 2: 커밋**

```bash
git add docs/chat-perf/diagnosis-runbook.md
git commit -m "docs(chat-perf): 진단→수정→검증 런북(품질 루프 + 로드맵 루프)"
```

---

## Task 8: 배포 + 라이브 스모크 (수동 검증)

> 코드 변경 없음. **push/머지/배포는 사용자 명시 허락 + 무거래창 필요** — 자율 실행 금지.

- [ ] **Step 1: 전체 챗봇 테스트 회귀 확인**

Run: `cd server && python -m pytest tests/test_chat_analytics.py tests/test_chat_agent.py tests/test_chat_api.py tests/test_chat_tools.py tests/test_chat_models.py -q`
Expected: 전부 PASS.

- [ ] **Step 2: 앱 import 스모크 (라우트·테이블 등록 깨짐 없음)**

Run: `cd server && python -c "from app.main import app; from app.models import ChatTurnMetric; print('ok', ChatTurnMetric.__tablename__)"`
Expected: `ok chatturnmetric`

- [ ] **Step 3: (사용자 허락 후) push + PR**

```bash
git push -u origin feat/chat-perf-measurement
gh pr create --fill --draft
```

- [ ] **Step 4: (사용자 허락 + 무거래창 후) main 머지 → Railway 자동 배포**
배포 후 `create_db_and_tables()`가 `chatturnmetric` 테이블을 생성한다(create_all). Railway 로그에서 기동 에러 없음 확인.

- [ ] **Step 5: 라이브 스모크 — prod 데이터 1회 조회**

Run: `railway run python -m app.chat_analytics stats --days 7`
Expected: 실제 turn이 있으면 집계 출력(turns>0). `railway run`이 Neon URL 주입을 확인.

- [ ] **Step 6: 첫 실제 진단**
대화가 몇 건 쌓인 뒤 `transcripts --suspect`로 루브릭 채점 1회 수행 → 런북으로 근본원인·태그 집계 → 첫 개선안 도출.

---

## Self-Review

**1. Spec coverage** (spec §4 대비):
- §4.1 데이터 모델 → Task 1 ✓
- §4.2 캡처(타이밍·usage·에러 ok=false·격리) → Task 2 ✓ (ttft 첫 델타·acc 누적·_persist_turn_metric try/except)
- §4.3 CLI(stats/transcripts/--suspect, railway run) → Task 3·4·5 ✓
- §4.4 루브릭(4축·계층 인벤토리·2-facet 태깅) → Task 6 ✓
- §4.5 런북(2 루프) → Task 7 ✓
- §5 환경 검증(SQLite 시드 TDD → railway 스모크) → Task 1~5 테스트 + Task 8 ✓

**2. Placeholder scan:** 코드/명령/문서 내용 모두 실제값. `analysis-type-<x>`는 태그 패턴(플레이스홀더 아님). TODO/TBD 없음.

**3. Type consistency:** `ChatTurnMetric` 필드명이 Task 1 정의 ↔ Task 2 적재(`_persist_turn_metric`) ↔ Task 3 집계(`compute_stats`) ↔ Task 4 렌더 전부 일치. `compute_stats`/`render_transcripts`/`format_stats`/`main_cli` 시그니처가 Task 5 배선과 일치. `acc` dict 키(in/out/cr/cw/rounds/tools/stop)가 `_accumulate_usage`↔`_persist_turn_metric`에서 일치.

---

## Execution Handoff

(writing-plans 이후 단계 — 사용자 선택)

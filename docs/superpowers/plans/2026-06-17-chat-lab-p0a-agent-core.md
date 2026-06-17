# Chat Lab P0a — Agent Loop Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 전략 연구소 챗봇의 **백엔드 agent 루프 코어**를 구축한다 — 대화 영속(DB) + 도구 레지스트리/디스패치(`screen`·`simulate`) + compact 요약 + Anthropic tool-use 루프. HTTP 엔드포인트·UI는 다음 계획(P0b).

**Architecture:** 사용자 메시지 → `run_chat_turn`이 Anthropic 클라이언트로 tool-use 루프 실행 → 각 `tool_use`를 `assemble_ir`로 StrategyIR로 조립 → 기존 엔진 단일진입점 `strategy_from_spec(ir, dataset)`로 검증·실행 → full 결과는 DB·반환용, `compact_summary`만 모델 컨텍스트로 되돌림 → `stop_reason != "tool_use"`면 종료. 전부 `server/app/chat/` 패키지에 격리. **테스트는 Anthropic 클라이언트를 fake로 주입해 hermetic**(실 API키·네트워크 불요), 엔진/데이터 로딩은 monkeypatch.

**Tech Stack:** Python · FastAPI/**SQLModel**(기존 스택) · `anthropic` SDK(기존) · 기존 `quant_core.ir_engine`(`strategy_from_spec`/`StrategyIR`) · pytest + monkeypatch.

**Spec:** `docs/REDESIGN/chat_lab_spec.md` (이 계획은 §4 도구·§5 컨텍스트·§6 agent 루프의 P0 백엔드 슬라이스).

**Scope guardrails (이 계획에서 건드리지 않는 것):**
- `core/`(엔진)·데이터 엔진·자동매매: **무변경**. 엔진은 기존 `strategy_from_spec`만 호출.
- `describe`/`relate`/`inspect`/`save_strategy` 도구: **이 계획 밖**(P0b 이후). `describe`는 희제 담당이라 별도 협의.
- HTTP 라우터·`main.py` 등록·웹 UI: **P0b**.
- 스트리밍·세션 예산: **P1 이후**.

---

## File Structure

| 파일 | 책임 | 신규/수정 |
|---|---|---|
| `server/app/config.py` | `CHAT_MODEL` 설정 추가 | 수정 |
| `server/app/models.py` | `Conversation`·`Message` 테이블 추가 | 수정 |
| `server/app/chat/__init__.py` | 패키지 마커 | 생성 |
| `server/app/chat/tools.py` | 도구 스키마·`assemble_ir`·`run_tool`·`compact_summary` | 생성 |
| `server/app/chat/prompt.py` | 챗 시스템 프롬프트(capability_spec 재사용) | 생성 |
| `server/app/chat/agent.py` | 영속 헬퍼 + `run_chat_turn` 루프 | 생성 |
| `server/tests/test_chat_models.py` | 모델 영속 테스트 | 생성 |
| `server/tests/test_chat_tools.py` | assemble/dispatch/compact 테스트 | 생성 |
| `server/tests/test_chat_agent.py` | 루프·영속 테스트(fake client) | 생성 |

> 모든 테스트 명령은 `server/` 디렉터리에서 실행한다(패키지 `app.*` import 기준). 예: `cd server && python -m pytest tests/test_chat_models.py -v`.

---

## Task 1: Config — `CHAT_MODEL` 설정

**Files:**
- Modify: `server/app/config.py` (Settings 클래스 끝, `NL_COMPILE_MODEL` 다음)
- Test: `server/tests/test_chat_models.py` (config 스모크)

- [ ] **Step 1: Write the failing test**

`server/tests/test_chat_models.py`:
```python
from app.config import settings


def test_chat_model_default():
    # env 미설정 시 Sonnet 기본. agentic 추론·논의 부담이 NL 컴파일러보다 커 상향.
    assert settings.CHAT_MODEL  # 비어있지 않음
    assert "claude" in settings.CHAT_MODEL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_chat_models.py::test_chat_model_default -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'CHAT_MODEL'`

- [ ] **Step 3: Write minimal implementation**

`server/app/config.py` — `NL_COMPILE_MODEL` 줄 바로 다음에 추가:
```python
    # 대화형 전략 연구소 챗봇 — agentic tool-use 루프(다중턴 추론·결과 논의). NL 컴파일러보다
    # 추론 부담이 커 Sonnet 기본(env로 교체). 키는 ANTHROPIC_API_KEY 공유.
    CHAT_MODEL: str = os.getenv("QP_CHAT_MODEL", "claude-sonnet-4-6")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_chat_models.py::test_chat_model_default -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/config.py server/tests/test_chat_models.py
git commit -m "feat(chat): add CHAT_MODEL setting (Sonnet default)"
```

---

## Task 2: DB models — `Conversation` + `Message`

**Files:**
- Modify: `server/app/models.py` (파일 끝에 추가; `_now`·`SQLModel`·`Column`·`JSON` 이미 import됨)
- Test: `server/tests/test_chat_models.py`

신규 테이블은 `SQLModel.metadata.create_all`이 자동 생성하므로 `db.py` 마이그레이션 항목 불요.

- [ ] **Step 1: Write the failing test**

`server/tests/test_chat_models.py` 에 추가:
```python
from sqlmodel import Session, SQLModel, create_engine
from app.models import Conversation, Message


def _mem_session() -> Session:
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    return Session(eng)


def test_conversation_and_message_roundtrip():
    s = _mem_session()
    conv = Conversation(user_id=1, title="테스트 대화")
    s.add(conv)
    s.commit()
    s.refresh(conv)
    assert conv.id is not None

    parts = [{"type": "text", "text": "안녕"},
             {"type": "tool_result", "tool_use_id": "t1", "name": "screen",
              "result": {"success": True, "results": [{"symbol": "005930", "score": 0.8}]}}]
    msg = Message(conversation_id=conv.id, role="assistant", parts=parts)
    s.add(msg)
    s.commit()
    s.refresh(msg)

    got = s.get(Message, msg.id)
    assert got.role == "assistant"
    assert got.parts[1]["result"]["results"][0]["symbol"] == "005930"  # JSON 왕복
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_chat_models.py::test_conversation_and_message_roundtrip -v`
Expected: FAIL — `ImportError: cannot import name 'Conversation' from 'app.models'`

- [ ] **Step 3: Write minimal implementation**

`server/app/models.py` 파일 끝에 추가:
```python
class Conversation(SQLModel, table=True):
    """전략 연구소 챗봇 대화 스레드. 안전정보만(전략·분석 텍스트, 자격증명 없음)."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="user.id")
    title: str = "새 대화"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Message(SQLModel, table=True):
    """대화 한 턴(user|assistant). parts = text/tool_use/tool_result 블록 배열(full payload).

    full 결과(차트 렌더·재현용)는 여기에 저장하고, 모델 컨텍스트로는 compact 요약만 보낸다
    (chat_lab_spec §5 이중 표현). 단일 진실원천 = 이 parts(컴팩트는 컨텍스트 빌드시 파생).
    """
    id: Optional[int] = Field(default=None, primary_key=True)
    conversation_id: int = Field(index=True, foreign_key="conversation.id")
    role: str                               # "user" | "assistant"
    parts: list = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=_now)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_chat_models.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add server/app/models.py server/tests/test_chat_models.py
git commit -m "feat(chat): add Conversation/Message tables"
```

---

## Task 3: 도구 스키마 + `assemble_ir`

**Files:**
- Create: `server/app/chat/__init__.py` (빈 파일)
- Create: `server/app/chat/tools.py`
- Test: `server/tests/test_chat_tools.py`

`assemble_ir`는 도구 입력을 StrategyIR dict로 조립한다(순수 함수). `screen`은 큐레이션 부분집합→select IR, `simulate`는 모델이 emit한 full IR 통과.

- [ ] **Step 1: Write the failing test**

`server/tests/test_chat_tools.py`:
```python
from quant_core.ir_engine import StrategyIR
from app.chat.tools import assemble_ir, TOOL_SCHEMAS


def test_tool_schemas_present():
    names = {t["name"] for t in TOOL_SCHEMAS}
    assert names == {"screen", "simulate"}


def test_assemble_screen_makes_valid_select_ir():
    ir = assemble_ir("screen", {"symbols": ["AAA", "BBB"],
                                "score_ref": "__SELF__.pb_ratio",
                                "top_n": 3, "descending": False, "display": ["pb_ratio"]})
    s = StrategyIR.model_validate(ir)         # 유효해야 함(예외 없음)
    assert s.query == "select"
    assert s.select.top_n == 3 and s.select.descending is False
    assert s.universe.kind == "list" and s.universe.symbols == ["AAA", "BBB"]


def test_assemble_screen_no_symbols_uses_all():
    ir = assemble_ir("screen", {"score_ref": "momentum_12_1m", "top_n": 5})
    s = StrategyIR.model_validate(ir)
    assert s.universe.kind == "all"


def test_assemble_simulate_passes_full_ir():
    base = {"universe": {"kind": "single", "symbols": ["AAA"]},
            "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
            "position": {"entry": {"mode": "always"}}}
    ir = assemble_ir("simulate", {"strategy": base})
    s = StrategyIR.model_validate(ir)
    assert s.query == "simulate" and s.study.axis == "none"


def test_assemble_unknown_tool_raises():
    import pytest
    with pytest.raises(ValueError):
        assemble_ir("nope", {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_chat_tools.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.chat'`

- [ ] **Step 3: Write minimal implementation**

`server/app/chat/__init__.py`: (빈 파일)

`server/app/chat/tools.py`:
```python
"""전략 연구소 챗봇 도구 — Anthropic tool 스키마 + IR 조립 + 엔진 디스패치 + compact 요약.

도구는 엔진의 동사(query)를 그대로 노출한다(chat_lab_spec D2). 서버가 도구 입력을
StrategyIR로 조립해 단일 엔진 진입점 strategy_from_spec로 실행한다(검증·valid_refs 자동).
"""
from __future__ import annotations

SCREEN_TOOL = {
    "name": "screen",
    "description": ("팩터 점수로 종목을 횡단 랭킹해 상위 종목을 선별(스크리닝). "
                    "백테스트가 아니라 현 시점(as-of) 스냅샷. score_ref·top_n 필요."),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {"type": "array", "items": {"type": "string"},
                        "description": "후보 종목 코드. 비우면 전체 유니버스."},
            "score_ref": {"type": "string",
                          "description": "랭킹 기준 지표 ref (예: __SELF__.pb_ratio, momentum_12_1m)."},
            "top_n": {"type": "integer", "description": "상위 N 종목."},
            "descending": {"type": "boolean",
                           "description": "점수 큰 순(true·기본) 또는 작은 순(false, 예: 저PBR)."},
            "display": {"type": "array", "items": {"type": "string"},
                        "description": "결과에 함께 표시할 지표 컬럼."},
        },
        "required": ["score_ref", "top_n"],
    },
}

SIMULATE_TOOL = {
    "name": "simulate",
    "description": ("완전한 매매전략(StrategyIR)을 과거 데이터로 백테스트. 저장 가능한 전략 산출물. "
                    "추상적 의도는 먼저 구체 정의로 협의한 뒤 호출."),
    "input_schema": {
        "type": "object",
        "properties": {
            "strategy": {"type": "object",
                         "description": ("완전한 StrategyIR JSON(universe/signal/position/simulation 등). "
                                         "signal은 필수.")},
        },
        "required": ["strategy"],
    },
}

TOOL_SCHEMAS = [SCREEN_TOOL, SIMULATE_TOOL]


def assemble_ir(tool_name: str, tool_input: dict) -> dict:
    """도구 입력 → StrategyIR dict. screen은 부분집합→select IR, simulate는 full IR 통과."""
    if tool_name == "screen":
        symbols = list(tool_input.get("symbols") or [])
        universe = {"kind": "list", "symbols": symbols} if symbols else {"kind": "all"}
        return {
            "universe": universe,
            "signal": {"op": "data", "params": {"ref": tool_input["score_ref"]}},
            "query": "select",
            "select": {"top_n": int(tool_input["top_n"]),
                       "descending": bool(tool_input.get("descending", True)),
                       "display": list(tool_input.get("display") or [])},
        }
    if tool_name == "simulate":
        return dict(tool_input.get("strategy") or {})
    raise ValueError(f"알 수 없는 도구: {tool_name}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_chat_tools.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add server/app/chat/__init__.py server/app/chat/tools.py server/tests/test_chat_tools.py
git commit -m "feat(chat): tool schemas + assemble_ir (screen/simulate)"
```

---

## Task 4: 도구 디스패치 `run_tool` + 데이터셋 로딩

**Files:**
- Modify: `server/app/chat/tools.py`
- Test: `server/tests/test_chat_tools.py`

`run_tool`은 IR 조립 → 데이터셋 로드 → `strategy_from_spec` 실행. 테스트는 `_load_dataset`·`strategy_from_spec`를 monkeypatch해 hermetic(실 데이터 로딩 없이 배선만 검증).

- [ ] **Step 1: Write the failing test**

`server/tests/test_chat_tools.py` 에 추가:
```python
from app.chat import tools as chat_tools


def test_run_tool_dispatches_to_engine(monkeypatch):
    captured = {}

    def fake_load(ir):
        captured["ir"] = ir
        return {"AAA": object()}            # 더미 데이터셋(엔진 호출 안 함)

    def fake_run(ir, dataset):
        captured["ran"] = (ir, dataset)
        return {"success": True, "query": "select", "results": []}

    monkeypatch.setattr(chat_tools, "_load_dataset", fake_load)
    monkeypatch.setattr(chat_tools, "strategy_from_spec", fake_run)

    out = chat_tools.run_tool("screen", {"score_ref": "__SELF__.pb_ratio", "top_n": 2})
    assert out["success"] is True
    assert captured["ir"]["query"] == "select"          # 조립된 IR이 전달됨
    assert captured["ran"][0]["query"] == "select"


def test_run_tool_bad_input_returns_error_not_raises(monkeypatch):
    # assemble_ir가 실패하면 예외 대신 error dict(루프가 모델에 피드백)
    out = chat_tools.run_tool("screen", {"top_n": 2})   # score_ref 누락 → KeyError 내부
    assert out["success"] is False and "error" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_chat_tools.py::test_run_tool_dispatches_to_engine -v`
Expected: FAIL — `AttributeError: module 'app.chat.tools' has no attribute '_load_dataset'`

- [ ] **Step 3: Write minimal implementation**

`server/app/chat/tools.py` 상단 import에 추가하고 함수 추가:
```python
import quant_core as qc
from quant_core.ir_engine import (StrategyIR, needed_columns, needed_symbols,
                                   strategy_from_spec)
```

(파일 하단에 추가)
```python
def _load_dataset(ir: dict) -> dict:
    """IR이 참조하는 데이터셋 로드 — ir.py /ir/strategy와 동일 전략.

    무효 IR이면 빈 dict 반환 → strategy_from_spec가 단일 검증경로에서 오류를 돌려준다
    (여기선 scope 계산만 — 근본 검증은 엔진 진입점이 소유, 증상 봉합 아님).
    """
    from .. import data_cache
    try:
        sir = StrategyIR.model_validate(ir)
    except Exception:
        return {}
    needed = needed_symbols(sir)
    if needed:
        return qc.load_dataset_for(needed)
    cols = needed_columns(sir)
    return data_cache.get_projected(cols, symbols=None,
                                    recent_days=400 if sir.query == "select" else None)


def run_tool(tool_name: str, tool_input: dict) -> dict:
    """도구 호출 → IR 조립 → 데이터셋 로드 → 엔진 실행. full 결과 dict 반환.

    조립 실패는 예외 대신 {success:False,error}로 — agent 루프가 tool_result로 모델에 피드백.
    """
    try:
        ir = assemble_ir(tool_name, tool_input)
    except (ValueError, KeyError, TypeError) as e:
        return {"success": False, "error": f"도구 입력 오류({tool_name}): {e}"}
    dataset = _load_dataset(ir)
    return strategy_from_spec(ir, dataset)   # valid_refs=None → 엔진이 available_refs 도출
```

> **구현 시 1줄 확인:** `needed_symbols`/`needed_columns`의 import 경로가 `server/app/routers/ir.py:19-21`과 동일한지 확인(거기서 `strategy_from_spec`와 함께 import됨). 다르면 ir.py와 일치시킬 것.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_chat_tools.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add server/app/chat/tools.py server/tests/test_chat_tools.py
git commit -m "feat(chat): run_tool dispatch to strategy_from_spec"
```

---

## Task 5: `compact_summary` — 모델 컨텍스트용 결과 요약

**Files:**
- Modify: `server/app/chat/tools.py`
- Test: `server/tests/test_chat_tools.py`

full 결과를 짧은 텍스트로 환원(chat_lab_spec D3·D5 — 숫자는 결과에서만, 모델 컨텍스트 폭발 방지).

- [ ] **Step 1: Write the failing test**

`server/tests/test_chat_tools.py` 에 추가:
```python
from app.chat.tools import compact_summary


def test_compact_screen():
    res = {"success": True, "as_of": "2026-06-17", "universe_size": 10, "eligible_size": 4,
           "results": [{"symbol": "AAA", "score": 0.82}, {"symbol": "BBB", "score": 0.79}]}
    out = compact_summary("screen", res)
    assert "AAA" in out and "0.82" in out and "2026-06-17" in out


def test_compact_simulate():
    res = {"success": True, "metrics": {"cagr": 0.123, "sharpe": 0.9, "mdd": -0.22,
                                        "cum_return": 1.4}}
    out = compact_summary("simulate", res)
    assert "cagr" in out and "0.123" in out


def test_compact_failure():
    out = compact_summary("simulate", {"success": False, "error": "전략 파싱 오류: x"})
    assert "실패" in out and "전략 파싱 오류" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_chat_tools.py::test_compact_screen -v`
Expected: FAIL — `ImportError: cannot import name 'compact_summary'`

- [ ] **Step 3: Write minimal implementation**

`server/app/chat/tools.py` 하단에 추가:
```python
def compact_summary(tool_name: str, result: dict) -> str:
    """full 엔진 결과 → 모델 컨텍스트용 짧은 요약. 숫자는 결과에서만(지어내기 금지)."""
    if not result.get("success"):
        return f"[{tool_name} 실패] {result.get('error', '알 수 없는 오류')}"
    if tool_name == "screen":
        rows = result.get("results") or []
        def _one(r):
            sc = r.get("score")
            return f"{r['symbol']}({sc:.3g})" if sc is not None else str(r["symbol"])
        top = ", ".join(_one(r) for r in rows[:8])
        return (f"[screen] as_of={result.get('as_of')}, 후보 {result.get('universe_size')}개 중 "
                f"{len(rows)}개 선별. 상위: {top}")
    if tool_name == "simulate":
        m = result.get("metrics") or {}
        parts = [f"{k}={m[k]:.3g}" for k in ("cagr", "sharpe", "mdd", "cum_return")
                 if isinstance(m.get(k), (int, float))]
        return "[simulate] " + (", ".join(parts) if parts else "결과 산출")
    return f"[{tool_name}] 완료"
```

> **구현 시 확인:** `simulate` 결과 `metrics`의 실제 키(`cagr`/`sharpe`/`mdd`/`cum_return`)가 `summarize_returns` 산출 키와 일치하는지 — `capability_spec()["objective_metric"]`이 진실원천(불일치 시 키 정정).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_chat_tools.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add server/app/chat/tools.py server/tests/test_chat_tools.py
git commit -m "feat(chat): compact_summary for model context"
```

---

## Task 6: 챗 시스템 프롬프트

**Files:**
- Create: `server/app/chat/prompt.py`
- Test: `server/tests/test_chat_agent.py`

기존 `capability_spec()`을 재사용해 메타인지 표면을 주입(chat_lab_spec D1).

- [ ] **Step 1: Write the failing test**

`server/tests/test_chat_agent.py`:
```python
from app.chat.prompt import chat_system_prompt


def test_system_prompt_includes_capabilities_and_rules():
    p = chat_system_prompt()
    assert "<capabilities>" in p
    assert "screen" in p and "simulate" in p
    assert "예측" in p                     # 백테스트≠예측 가드레일 존재
    assert "tool_result" in p              # 숫자 규율 명시
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_chat_agent.py::test_system_prompt_includes_capabilities_and_rules -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.chat.prompt'`

- [ ] **Step 3: Write minimal implementation**

`server/app/chat/prompt.py`:
```python
"""전략 연구소 챗봇 시스템 프롬프트 — capability_spec(메타인지) 재사용 + 숫자 규율·도구 안내."""
from __future__ import annotations

import json


def chat_system_prompt() -> str:
    import quant_core as qc
    from quant_core.ir_engine import capability_spec
    caps = json.dumps(capability_spec(), ensure_ascii=False)
    cols = ", ".join(sorted(qc.get_all_indicator_columns()))
    return f"""<role>
너는 전략 연구소의 데이터 분석 어시스턴트다. 사용자와 한국어로 대화하며 도구로 실시간 분석을
수행하고 결과를 해석·논의한다. 숫자·통계·종목명은 **반드시 도구 결과(tool_result)에서만** 가져오고
절대 지어내지 않는다. 도구로 답할 수 있으면 도구를 호출하고, 의도가 모호하면 먼저 협의(질문/제안)한다.
</role>
<tools_guidance>
- screen: 팩터 점수로 종목을 선별(현 시점 스냅샷). score_ref·top_n 필요.
- simulate: 완전한 매매전략(StrategyIR)을 백테스트. 저장 가능한 전략 산출물.
추상적 의도(예: "유망 종목을 사서 장기보유")는 먼저 구체적 정의(어떤 팩터·리밸런스·보유기간)로
협의해 합의한 뒤 simulate로 백테스트한다. 한 번에 여러 분석이 필요하면 도구를 여러 번 호출한다.
</tools_guidance>
<capabilities>{caps}</capabilities>
<reference_data>{cols}</reference_data>
<rules>
- 백테스트는 과거 검증이지 미래 예측이 아니다 — 결과를 "예측"이라 말하지 않는다.
- 못 하는 분석은 정직하게 말한다(데이터·도구가 없으면 지어내지 말 것).
</rules>"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_chat_agent.py::test_system_prompt_includes_capabilities_and_rules -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/chat/prompt.py server/tests/test_chat_agent.py
git commit -m "feat(chat): chat system prompt reusing capability_spec"
```

---

## Task 7: 영속 헬퍼 — `_persist` + `_history_to_wire`

**Files:**
- Create: `server/app/chat/agent.py`
- Test: `server/tests/test_chat_agent.py`

DB에 턴을 저장하고, 히스토리를 Anthropic 와이어 포맷으로 복원하되 tool_result는 **compact**로 환원(chat_lab_spec §5.3 — DB 논리턴 ↔ 와이어 포맷 변환).

- [ ] **Step 1: Write the failing test**

`server/tests/test_chat_agent.py` 에 추가:
```python
from sqlmodel import Session, SQLModel, create_engine
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
    # user 메시지 1 + assistant(텍스트+tool_use) 1 + tool_result(compact) user 1
    assert wire[0] == {"role": "user", "content": "저평가주 골라줘"}
    assert wire[1]["role"] == "assistant"
    assert any(b.get("type") == "tool_use" for b in wire[1]["content"])
    tr = wire[2]["content"][0]
    assert tr["type"] == "tool_result" and tr["tool_use_id"] == "t1"
    assert "AAA" in tr["content"]            # full 아니라 compact 텍스트
    assert "results" not in str(tr["content"])  # full payload 미포함
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_chat_agent.py::test_persist_and_history_compaction -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.chat.agent'`

- [ ] **Step 3: Write minimal implementation**

`server/app/chat/agent.py`:
```python
"""전략 연구소 챗봇 agent 루프 + 영속/컨텍스트 헬퍼.

DB는 논리적 턴(parts: text/tool_use/tool_result, full payload)을 저장하고, Anthropic 와이어
포맷으로 복원할 때 tool_result는 compact 요약으로 환원한다(chat_lab_spec §5).
"""
from __future__ import annotations

from sqlmodel import Session, select

from ..models import Message
from .tools import compact_summary


def _persist(session: Session, conversation_id: int, role: str, parts: list) -> None:
    session.add(Message(conversation_id=conversation_id, role=role, parts=parts))
    session.commit()


def _history_to_wire(session: Session, conversation_id: int) -> list[dict]:
    """저장된 메시지 → Anthropic messages 배열. tool_result는 compact로 환원."""
    rows = session.exec(
        select(Message).where(Message.conversation_id == conversation_id)
        .order_by(Message.id)).all()
    wire: list[dict] = []
    for m in rows:
        if m.role == "user":
            # user 턴은 항상 단일 텍스트(현재 입력). tool_result user 턴은 assistant 저장에 포함됨.
            text = next((p["text"] for p in m.parts if p.get("type") == "text"), "")
            wire.append({"role": "user", "content": text})
            continue
        # assistant 턴 — text/tool_use는 그대로, tool_result는 별도 user(compact)로 뒤에 붙임.
        a_content: list[dict] = []
        tool_results: list[dict] = []
        for p in m.parts:
            t = p.get("type")
            if t == "text":
                a_content.append({"type": "text", "text": p["text"]})
            elif t == "tool_use":
                a_content.append({"type": "tool_use", "id": p["id"],
                                  "name": p["name"], "input": p.get("input") or {}})
            elif t == "tool_result":
                tool_results.append({"type": "tool_result", "tool_use_id": p["tool_use_id"],
                                     "content": compact_summary(p.get("name", ""), p.get("result") or {})})
        if a_content:
            wire.append({"role": "assistant", "content": a_content})
        if tool_results:
            wire.append({"role": "user", "content": tool_results})
    return wire
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_chat_agent.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/chat/agent.py server/tests/test_chat_agent.py
git commit -m "feat(chat): persist + history-to-wire with compaction"
```

---

## Task 8: Agent 루프 `run_chat_turn`

**Files:**
- Modify: `server/app/chat/agent.py`
- Test: `server/tests/test_chat_agent.py`

핵심 루프. 테스트는 fake Anthropic client(첫 턴 tool_use, 둘째 턴 end_turn)를 주입하고 `run_tool`을 monkeypatch해 hermetic.

- [ ] **Step 1: Write the failing test**

`server/tests/test_chat_agent.py` 에 추가:
```python
from app.chat import tools as chat_tools


class _Block:
    def __init__(self, **kw): self.__dict__.update(kw)


class _Resp:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason


class _FakeMessages:
    def __init__(self, queue): self._queue = queue
    def create(self, **kw):  # noqa: ARG002
        return self._queue.pop(0)


class _FakeClient:
    def __init__(self, queue): self.messages = _FakeMessages(queue)


def test_run_chat_turn_dispatches_and_persists(monkeypatch):
    s = _mem_session()
    conv = Conversation(user_id=1)
    s.add(conv); s.commit(); s.refresh(conv)

    # run_tool은 실제 엔진 대신 카드 결과
    monkeypatch.setattr(chat_tools, "run_tool",
                        lambda name, inp: {"success": True, "query": "select",
                                           "as_of": "2026-06-17", "universe_size": 5,
                                           "results": [{"symbol": "AAA", "score": 0.8}]})
    # agent.py가 from .tools import run_tool 했다면 agent 모듈에서도 패치
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

    # 영속 확인: user 1 + assistant 1
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/test_chat_agent.py::test_run_chat_turn_dispatches_and_persists -v`
Expected: FAIL — `AttributeError: module 'app.chat.agent' has no attribute 'run_chat_turn'`

- [ ] **Step 3: Write minimal implementation**

`server/app/chat/agent.py` — import에 추가:
```python
from .tools import TOOL_SCHEMAS, compact_summary, run_tool
from .prompt import chat_system_prompt
```

(파일 하단에 추가)
```python
MAX_TOOL_ROUNDS = 8     # 한 사용자 턴당 도구 라운드 상한(무한루프·비용 가드)


def _block_to_wire(b) -> dict:
    """Anthropic 응답 content 블록(SDK 객체) → 다음 호출용 와이어 dict."""
    t = getattr(b, "type", None)
    if t == "text":
        return {"type": "text", "text": b.text}
    if t == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input or {})}
    return {"type": t}


def run_chat_turn(session: Session, conversation_id: int, user_text: str,
                  *, client=None, model: str | None = None) -> list[dict]:
    """한 사용자 메시지에 대해 agent 루프 실행. 도구 호출·결과·서술을 DB에 영속하고
    이번 턴 assistant parts(full payload 포함, 렌더·반환용)를 돌려준다."""
    from ..config import settings
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    model = model or settings.CHAT_MODEL
    system = [{"type": "text", "text": chat_system_prompt(),
               "cache_control": {"type": "ephemeral"}}]

    messages = _history_to_wire(session, conversation_id)
    messages.append({"role": "user", "content": user_text})
    _persist(session, conversation_id, "user", [{"type": "text", "text": user_text}])

    assistant_parts: list[dict] = []      # full payload(영속·렌더용)
    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.messages.create(model=model, max_tokens=4096, system=system,
                                       tools=TOOL_SCHEMAS, messages=messages)
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
            full = run_tool(b.name, inp)
            assistant_parts.append({"type": "tool_use", "id": b.id, "name": b.name, "input": inp})
            assistant_parts.append({"type": "tool_result", "tool_use_id": b.id,
                                    "name": b.name, "result": full})
            tool_results.append({"type": "tool_result", "tool_use_id": b.id,
                                 "content": compact_summary(b.name, full)})
        messages.append({"role": "user", "content": tool_results})

    _persist(session, conversation_id, "assistant", assistant_parts)
    return assistant_parts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/test_chat_agent.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add server/app/chat/agent.py server/tests/test_chat_agent.py
git commit -m "feat(chat): run_chat_turn agent loop (tool-use, persist, full+compact split)"
```

---

## Task 9: 전체 회귀 + 골든 보존 확인

**Files:** (없음 — 검증 전용)

- [ ] **Step 1: 챗 테스트 스위트 통과**

Run: `cd server && python -m pytest tests/test_chat_models.py tests/test_chat_tools.py tests/test_chat_agent.py -v`
Expected: 전부 PASS

- [ ] **Step 2: 기존 서버 스위트 무회귀**

Run: `cd server && python -m pytest -q`
Expected: 기존 통과 수 유지(신규 chat 테스트만 추가, 기존 실패 0 — base 선재 실패 제외).

- [ ] **Step 3: 코어 골든 무변경(엔진 무수정 확인)**

Run: `cd .. && python -m pytest tests/golden_backtest.py -q`
Expected: PASS, byte-identical(이 계획은 `core/` 무수정).

- [ ] **Step 4: Commit (필요 시 — 변경 없으면 생략)**

이 작업은 검증 전용이므로 새 커밋 없음. 실패 발견 시 해당 Task로 돌아가 수정.

---

## Self-Review (작성자 체크)

**1. Spec coverage (chat_lab_spec §4·§5·§6 P0 백엔드 슬라이스):**
- §4 도구(screen·simulate) → Task 3·4 ✅ (describe/relate/inspect/save는 명시적 범위 밖)
- §4 도구 전 검증 재사용(`strategy_from_spec`) → Task 4 ✅
- §5 이중 표현(full↔compact) → Task 5(compact)·Task 7(history 환원)·Task 8(parts에 full, 와이어에 compact) ✅
- §5.3 데이터 모델(Conversation/Message parts) → Task 2 ✅
- §6.2 agent 루프(stop_reason 분기·다중 tool_use) → Task 8 ✅
- §6 도구 N개/턴(비교) → Task 8 루프가 한 응답의 tool_use 전부 처리 ✅
- §9 비용 가드(라운드 상한) → Task 8 `MAX_TOOL_ROUNDS` ✅(세션 예산은 P1+)

**2. Placeholder scan:** 모든 step에 실제 코드·명령·기대출력 포함. "구현 시 확인" 2건(needed_* import 경로, simulate metrics 키)은 placeholder가 아니라 *기존 코드와 1줄 대조* 지시(로직은 완결).

**3. Type consistency:** `assemble_ir(name, input)→dict`·`run_tool(name, input)→dict`·`compact_summary(name, result)→str`·`_persist(session, conv_id, role, parts)`·`_history_to_wire(session, conv_id)→list[dict]`·`run_chat_turn(session, conv_id, text, *, client, model)→list[dict]` — Task 간 시그니처 일치. parts 블록 스키마(text/tool_use{id,name,input}/tool_result{tool_use_id,name,result}) Task 2·7·8 일관.

**미해결(다음 계획):** HTTP 엔드포인트·라우터 등록·웹 UI(P0b) · 스트리밍·세션 예산(P1) · describe/relate/inspect/save 도구(협의 후) · 실 데이터셋 통합 스모크(엔드포인트 계획에서 fixture로).

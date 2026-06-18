# 챗봇 IR 위임 재설계 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 챗봇 도구가 StrategyIR을 맨손 추측하던 부류를 구조적으로 닫는다 — simulate/save가 IR 대신 NL을 받아 검증된 `compile_nl`에 위임하고, screen 섹터필터·inspect 교정피드백·에이전트 루프 graceful 종료까지 함께 고친다.

**Architecture:** 공유 `compile_strategy(session, user_id, nl)`(server/app/compile_service.py 신규)가 `compile_nl` 배선을 단일화(router도 이 헬퍼 사용). simulate/save_strategy 도구는 NL을 받아 이 헬퍼로 검증 IR을 만든 뒤 백테스트/저장. 챗봇은 기존 NL→IR→백테스트 파이프라인의 대화형 래퍼가 된다.

**Tech Stack:** Python 3.12 · FastAPI · SQLModel · Anthropic SDK(Haiku 컴파일러) · pytest

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `server/app/compile_service.py` | NL→검증IR 공유 진입점(compile_nl 배선 + _schema_issues) | **신규** |
| `server/app/routers/ir_compile.py` | `/ir/compile` 라우터 | 공유 헬퍼 사용으로 리팩터(배선·_schema_issues 이동) |
| `server/app/chat/tools.py` | 챗봇 도구 스키마·조립·실행 | simulate/save/screen/inspect |
| `server/app/chat/agent.py` | agent 루프 | simulate 디스패치(세션·user 주입) + 루프 graceful 종료 |
| `server/app/chat/prompt.py` | 시스템 프롬프트 | tools_guidance(NL 위임·라우팅 보존) |
| `server/tests/test_compile_service.py` | 공유 헬퍼 테스트 | **신규** |
| `server/tests/test_chat_tools.py` | 도구 테스트 | simulate/save/screen/inspect |
| `server/tests/test_chat_agent.py` | 루프 테스트 | graceful 종료 |

**작업 브랜치:** `feat/chat-ir-delegation` (worktree `_wt-chat-ir`, off origin/main=8e91972). **push/머지는 사용자 명시 허락 시에만.**
**테스트:** `cd "C:/Users/USER/Desktop/창업/퀀트/_wt-chat-ir/server" && python -m pytest tests/<file> -q`. conftest가 in-repo core·server를 sys.path 최우선에.
**참고(현재 코드):** 라우터 배선 `routers/ir_compile.py:143-167`, `compile_nl` 시그니처 `ir_compiler.py:364-378`, 도구 `chat/tools.py`, 프롬프트 `chat/prompt.py`.

---

## Task 1: 공유 compile 진입점 `compile_strategy`

**Files:**
- Create: `server/app/compile_service.py`
- Modify: `server/app/routers/ir_compile.py` (배선·`_schema_issues`를 헬퍼로 이동)
- Test: `server/tests/test_compile_service.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `server/tests/test_compile_service.py`

```python
"""compile_strategy 공유 헬퍼 — compile_nl 배선을 단일화(router·chat 공용).
compile_nl(LLM)은 monkeypatch로 격리한다(실 API·네트워크 없음)."""
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import User


def _engine_user():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    with Session(e) as s:
        u = User(email="t@example.com"); s.add(u); s.commit(); s.refresh(u)
        return e, u.id


def test_compile_strategy_success(monkeypatch):
    import app.compile_service as cs
    captured = {}
    def _fake_compile_nl(nl, **kw):
        captured["nl"] = nl; captured["kw"] = kw
        return {"success": True, "ir": {"universe": {"kind": "single", "symbols": ["005930"]},
                "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}},
                "assumptions": ["가정1"], "issues": [], "repair_count": 0}
    monkeypatch.setattr(cs, "compile_nl", _fake_compile_nl)
    eng, uid = _engine_user()
    with Session(eng) as s:
        out = cs.compile_strategy(s, uid, "삼성전자 종가 전략")
    assert out["success"] is True and out["ir"]["universe"]["symbols"] == ["005930"]
    assert captured["nl"] == "삼성전자 종가 전략"
    # 배선 인자가 모두 전달됐는지(추출 정확성)
    assert set(captured["kw"]) >= {"catalog", "capabilities", "indicator_cols",
                                   "valid_keys", "name_map", "validate_fn"}
    assert "explanation" in out          # explain_ir 부착


def test_compile_strategy_failure(monkeypatch):
    import app.compile_service as cs
    monkeypatch.setattr(cs, "compile_nl", lambda nl, **kw: {
        "success": False, "error": "검증을 통과하는 IR을 생성하지 못했습니다.",
        "ir": {}, "assumptions": [], "issues": [], "repair_count": 2})
    eng, uid = _engine_user()
    with Session(eng) as s:
        out = cs.compile_strategy(s, uid, "표현 불가")
    assert out["success"] is False and "생성하지 못" in out["error"]
    assert out["explanation"] is None
```

- [ ] **Step 2: 실패 확인**

Run: `cd server && python -m pytest tests/test_compile_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.compile_service'`

- [ ] **Step 3: 헬퍼 구현** — `server/app/compile_service.py` 생성. (현재 `routers/ir_compile.py:143-167`의 배선 + `_schema_issues`를 이리로 옮긴다.)

```python
"""NL→검증 StrategyIR 공유 진입점 — compile_nl 배선을 단일화한다.

/ir/compile 라우터와 챗봇 도구(simulate/save)가 같은 컴파일 경로를 쓰도록(DRY). 챗봇은 IR을
맨손으로 짓지 않고 이 헬퍼에 위임 → 모델이 IR 스키마를 추측하는 실패 부류가 제거된다.
"""
from __future__ import annotations

from pydantic import ValidationError

import quant_core as qc
from quant_core.ir_engine import (StrategyIR, capability_spec, explain_ir,
                                   field_contract, validate_strategy)
from quant_core.blocks import catalog_spec

from sqlmodel import Session, select

from .ir_compiler import compile_nl
from .models import TradableSymbol


def _schema_issues(e: ValidationError) -> list[dict]:
    """Pydantic 스키마 오류를 'fixable' 이슈로 — 위치(loc) + 그 자리 스키마 계약(field_contract).
    repair 루프가 path·message를 인용해 수렴한다(허용 필드를 알려줘야 LLM이 고친다)."""
    issues: list[dict] = []
    for er in e.errors()[:12]:
        loc = er.get("loc", ())
        contract = field_contract(loc[:-1] if loc else ())
        hint = f" — 올바른 형식: {contract}" if contract else ""
        ctx = er.get("ctx") or {}
        if ctx.get("expected"):
            hint += f" (허용: {ctx['expected']})"
        issues.append({"rule": "schema", "severity": 30, "is_error": True,
                       "message": f"정의 형식 오류: {er.get('msg', '')}{hint}",
                       "path": ".".join(str(x) for x in loc) or "root"})
    if not issues:
        issues = [{"rule": "schema", "severity": 30, "is_error": True,
                   "message": f"정의 형식 오류: {e}", "path": "root"}]
    return issues


def compile_strategy(session: Session, user_id, nl: str) -> dict:
    """자연어 전략 서술 → 검증된 StrategyIR. compile_nl(Haiku) 내부 수리 루프.

    반환: {success, ir, assumptions, issues, repair_count, error?, explanation}. explanation은
    성공 시 explain_ir(MECE 버킷) — 챗봇이 "이렇게 해석했어요"로 유저에게 노출한다.
    """
    from quant_core import data_fetcher as _df
    sym_keys = (set(_df.ALL_SYMBOLS)
                | {s["name"] for s in _df.load_user_stocks()}
                | set(_df.load_managed_kr_codes())
                | {s["code"] for s in _df.load_managed_overseas()})
    valid_refs = (sym_keys | {"Open", "High", "Low", "Close", "Volume"}
                  | set(qc.get_all_indicator_columns()))
    valid_keys = sym_keys
    indicator_cols = sorted(qc.get_all_indicator_columns())
    rows = session.exec(select(TradableSymbol).where(TradableSymbol.user_id == user_id)).all()
    name_map = {r.name.strip().lower(): r.symbol for r in rows if r.name}

    def _validate(strat: dict) -> tuple[list[dict], bool]:
        try:
            s = StrategyIR.model_validate(strat)
        except ValidationError as e:
            return (_schema_issues(e), False)
        out = [{"rule": i.rule, "severity": i.severity, "is_error": i.is_error,
                "message": i.message, "path": i.path}
               for i in validate_strategy(s, valid_refs=valid_refs)]
        return (out, not any(i["is_error"] for i in out))

    res = compile_nl(nl, catalog=catalog_spec(), capabilities=capability_spec(),
                     indicator_cols=indicator_cols, valid_keys=valid_keys,
                     name_map=name_map, validate_fn=_validate)

    explanation = None
    if res.get("success") and res.get("ir"):
        try:
            explanation = explain_ir(StrategyIR.model_validate(res["ir"]),
                                     res.get("assumptions") or [])
        except ValidationError:
            explanation = None
    return {**res, "explanation": explanation}
```

- [ ] **Step 4: 통과 확인**

Run: `cd server && python -m pytest tests/test_compile_service.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: 라우터 리팩터** — `server/app/routers/ir_compile.py`에서 중복 제거:
  1. `_schema_issues` 정의(현 66-85행)를 **삭제**하고 `from ..compile_service import compile_strategy, _schema_issues` import로 대체(다른 곳에서 `_schema_issues`를 쓰면 유지).
  2. `ir_compile()` 함수의 배선+호출(현 143-167행: `sym_keys`…`res = compile_nl(...)`)을 다음 한 줄로 교체:

```python
    res = compile_strategy(session, user.id, body.nl)
```

  3. 그 아래 `explanation` 계산 블록(현 178-184행)을 **삭제**하고 `res["explanation"]`을 사용:

```python
    explanation = res.get("explanation")
```

  (이미 import된 `catalog_spec`·`capability_spec`·`validate_strategy`·`field_contract`·`explain_ir`·`compile_nl`가 ir_compile.py에서 더 안 쓰이면 import 정리. `_validate`/`valid_refs` 등 지역 변수는 compile_strategy로 이동했으니 제거.)

- [ ] **Step 6: 라우터 회귀 확인**

Run: `cd server && python -m pytest tests/test_ir_compile.py tests/test_compile_service.py -q`
Expected: PASS (기존 `/ir/compile` 테스트 + 신규 헬퍼 테스트 모두 green — 배선 추출이 동작 동일)

> `tests/test_ir_compile.py`가 없으면 `cd server && python -m pytest tests/ -k "compile or ir" -q`로 관련 테스트 확인. compile_nl을 직접 호출하는 테스트가 없다면 import 스모크: `python -c "from app.main import app; from app.compile_service import compile_strategy; print('ok')"`.

- [ ] **Step 7: 커밋**

```bash
git add server/app/compile_service.py server/app/routers/ir_compile.py server/tests/test_compile_service.py
git commit -m "refactor(compile): compile_strategy 공유 헬퍼 추출 — router·chat 공용(DRY)"
```

---

## Task 2: simulate — IR → NL 위임

**Files:**
- Modify: `server/app/chat/tools.py` (SIMULATE_TOOL · `run_simulate` 신규 · assemble_ir에서 simulate 제거)
- Modify: `server/app/chat/agent.py` (simulate 디스패치에 세션·user 주입)
- Test: `server/tests/test_chat_tools.py`

- [ ] **Step 1: 실패 테스트 작성** — `server/tests/test_chat_tools.py`에 추가

```python
def test_run_simulate_delegates_to_compiler(monkeypatch):
    from app.chat import tools
    monkeypatch.setattr(tools, "compile_strategy", lambda s, uid, nl: {
        "success": True,
        "ir": {"universe": {"kind": "single", "symbols": ["005930"]},
               "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "query": "backtest"},
        "assumptions": ["가정1"], "explanation": {"summary": "단일종목 백테스트"}})
    monkeypatch.setattr(tools, "_load_dataset", lambda ir: {})
    monkeypatch.setattr(tools, "strategy_from_spec",
                        lambda ir, ds: {"success": True, "metrics": {"cagr": 0.1}})
    out = tools.run_simulate(session=None, user_id=1, tool_input={"nl": "삼성전자 종가 전략"})
    assert out["success"] is True and out["metrics"]["cagr"] == 0.1
    assert out["ir"]["universe"]["symbols"] == ["005930"]   # 검증 IR 동봉(저장 재사용·표시)
    assert out["explanation"]["summary"] == "단일종목 백테스트"


def test_run_simulate_compile_failure_is_graceful(monkeypatch):
    from app.chat import tools
    monkeypatch.setattr(tools, "compile_strategy", lambda s, uid, nl: {
        "success": False, "error": "검증을 통과하는 IR을 생성하지 못했습니다.", "ir": {}})
    out = tools.run_simulate(session=None, user_id=1, tool_input={"nl": "표현 불가"})
    assert out["success"] is False and "생성하지 못" in out["error"]
```

- [ ] **Step 2: 실패 확인**

Run: `cd server && python -m pytest tests/test_chat_tools.py -k run_simulate -q`
Expected: FAIL — `AttributeError: module 'app.chat.tools' has no attribute 'run_simulate'`

- [ ] **Step 3: 구현** — `server/app/chat/tools.py`

(a) 상단 import에 추가:
```python
from ..compile_service import compile_strategy
```

(b) `SIMULATE_TOOL`을 NL 입력으로 교체:
```python
SIMULATE_TOOL = {
    "name": "simulate",
    "description": ("매매전략을 과거 데이터로 백테스트. **전략을 자연어로 완결 서술**하면 "
                    "서버가 검증된 IR로 컴파일해 실행한다(IR JSON을 직접 짓지 말 것). "
                    "추상적 의도는 먼저 구체 정의로 협의한 뒤 호출."),
    "input_schema": {
        "type": "object",
        "properties": {
            "nl": {"type": "string",
                   "description": ("백테스트할 전략의 완결된 자연어 서술 — 유니버스·신호조건·"
                                   "방향(롱/숏)·진입/청산·기간/비용 등 합의된 내용을 한 문단으로.")},
        },
        "required": ["nl"],
    },
}
```

(c) `assemble_ir`의 simulate 분기(현 117-118행) **삭제**(이제 NL 위임이라 통과 조립 불필요):
```python
    # (simulate 분기 제거 — run_simulate가 compile_strategy로 IR을 만든다)
```

(d) `run_inspect` 위에 `run_simulate` 추가:
```python
def run_simulate(session, user_id, tool_input: dict) -> dict:
    """전략 NL → compile_strategy(검증 IR) → 백테스트. 모델이 IR을 추측하지 않는다(부류 제거).

    실패(컴파일 불가)는 예외 대신 {success:False,error}로 — agent 루프가 graceful 전달.
    성공 시 검증 IR·explanation을 결과에 동봉(저장 재사용·유저 표시).
    """
    nl = str(tool_input.get("nl") or "").strip()
    if not nl:
        return {"success": False, "error": "simulate: 전략 서술(nl)이 필요합니다."}
    comp = compile_strategy(session, user_id, nl)
    if not comp.get("success"):
        return {"success": False, "error": comp.get("error") or "전략을 IR로 컴파일하지 못했습니다."}
    ir = comp["ir"]
    dataset = _load_dataset(ir)
    res = strategy_from_spec(ir, dataset)
    if isinstance(res, dict) and res.get("success"):
        res["ir"] = ir
        res["explanation"] = comp.get("explanation")
        res["assumptions"] = comp.get("assumptions") or []
    return res
```

- [ ] **Step 4: agent.py 디스패치 — simulate에 세션·user 주입** — `server/app/chat/agent.py`의 도구 디스패치 분기 교체:

```python
                if b.name in ("simulate", "save_strategy"):
                    conv = session.get(Conversation, conversation_id)
                    uid = conv.user_id if conv else None
                    if b.name == "simulate":
                        full = run_simulate(session, uid, inp)
                    else:
                        full = save_strategy_tool(session, uid, conversation_id, inp)
                else:
                    full = run_tool(b.name, inp)
```

그리고 import에 `run_simulate` 추가:
```python
from .tools import (TOOL_SCHEMAS, compact_summary, run_simulate, run_tool,
                    save_strategy_tool)
```

> `save_strategy_tool`은 Task 4에서 `conversation_id` 인자를 받도록 바뀐다. Task 2에서는 simulate만 쓰고, save 분기는 Task 4 완료 전까지 기존 시그니처(`save_strategy_tool(session, uid, inp)`)로 두되 — **Task 2·4를 연달아 구현**하면 위 코드가 최종형이다. (Task 2 단독 커밋 시 save 분기는 `save_strategy_tool(session, uid, inp)`로.)

- [ ] **Step 5: 통과 확인 (도구 + 기존 챗봇 회귀)**

Run: `cd server && python -m pytest tests/test_chat_tools.py tests/test_chat_agent.py tests/test_chat_api.py -q`
Expected: PASS (simulate 위임 테스트 + 기존 챗봇 green)

- [ ] **Step 6: 커밋**

```bash
git add server/app/chat/tools.py server/app/chat/agent.py server/tests/test_chat_tools.py
git commit -m "feat(chat): simulate를 NL 위임으로 — compile_strategy가 검증 IR 생성(추측 제거)"
```

---

## Task 3: 시스템 프롬프트 — NL 위임 + 라우팅 보존

**Files:**
- Modify: `server/app/chat/prompt.py`
- Test: `server/tests/test_chat_agent.py` (프롬프트 내용 단언)

- [ ] **Step 1: 실패 테스트 작성** — `server/tests/test_chat_agent.py`에 추가

```python
def test_system_prompt_guides_nl_simulate_and_routing():
    from app.chat.prompt import chat_system_prompt
    p = chat_system_prompt()
    assert "자연어로" in p and "IR JSON" in p          # simulate NL 위임 안내
    assert "inspect" in p and "describe" in p          # 라우팅 보존(비백테스트 경로)
    assert "투자자문" in p or "일반" in p               # 일반 대화·범위 안내
```

- [ ] **Step 2: 실패 확인**

Run: `cd server && python -m pytest tests/test_chat_agent.py -k routing -q`
Expected: FAIL (현 프롬프트엔 "IR JSON 짓지 말 것" 류 안내 없음)

- [ ] **Step 3: 구현** — `server/app/chat/prompt.py`의 `<tools_guidance>` 블록 교체:

```python
<tools_guidance>
- screen: 팩터 점수로 종목을 선별(현 시점 스냅샷). score_ref·top_n(섹터는 sector).
- simulate: 매매전략 백테스트. **전략을 자연어(nl)로 완결 서술**하면 서버가 검증된 IR로 컴파일·실행한다 — IR JSON을 직접 짓지 말 것. 협의로 합의된 내용을 한 문단 NL로.
- save_strategy: 합의된 전략을 draft로 저장. 사용자가 "저장"을 원할 때만(앞서 simulate한 전략을 그대로 저장; 모의/실전은 자동매매 메뉴에서).
- describe: 단일 종목 종합 리포트(가격·수익·변동성·밸류에이션·뉴스). "○○ 어때?"류. symbol만.
- inspect: 단일 종목의 특정 지표 원시 시계열(예: 최근 주가=Close, 목표주가=consensus_target). symbol·columns.
라우팅: 주가·데이터→inspect · 종목분석→describe · 스크리닝→screen · 백테스트→simulate(NL) · **일반 대화·투자 원론→도구 없이 직접 답변**. 개인 맞춤 투자자문은 범위 밖(교육적 일반론까지). 데이터 미수급(뉴스·광범위 추정치·수급)은 지어내지 말고 솔직히 한계를 말한다.
추상적 의도(예: "유망 종목 사서 장기보유")는 먼저 구체 정의(팩터·리밸런스·보유기간)로 협의 후 simulate. 시나리오 비교는 각각 별도 도구 호출.
</tools_guidance>
```

- [ ] **Step 4: 통과 확인**

Run: `cd server && python -m pytest tests/test_chat_agent.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add server/app/chat/prompt.py server/tests/test_chat_agent.py
git commit -m "feat(chat): 프롬프트 — simulate NL 위임 + 전체 라우팅 보존 안내"
```

---

## Task 4: save_strategy — NL 위임 + 마지막 IR 재사용

**Files:**
- Modify: `server/app/chat/tools.py` (SAVE_STRATEGY_TOOL · save_strategy_tool 시그니처+로직)
- Test: `server/tests/test_chat_tools.py`

- [ ] **Step 1: 실패 테스트 작성** — `server/tests/test_chat_tools.py`에 추가

```python
def test_save_reuses_last_simulate_ir(monkeypatch):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine
    from app.models import User, Conversation, Message
    from app.chat import tools
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    saved = {}
    monkeypatch.setattr(tools, "save_ir_draft",
                        lambda s, uid, ir: saved.setdefault("ir", ir) or type("R", (), {"id": 7, "name": ir.get("name")}))
    compiled = {"called": False}
    monkeypatch.setattr(tools, "compile_strategy",
                        lambda *a: compiled.update(called=True) or {"success": True, "ir": {"x": 1}})
    with Session(eng) as s:
        u = User(email="t@e.com"); s.add(u); s.commit(); s.refresh(u)
        c = Conversation(user_id=u.id); s.add(c); s.commit(); s.refresh(c)
        # 직전 simulate tool_result(검증 IR 동봉)를 영속
        s.add(Message(conversation_id=c.id, role="assistant", parts=[
            {"type": "tool_result", "name": "simulate",
             "result": {"success": True, "ir": {"universe": {"kind": "single", "symbols": ["005930"]}}}}]))
        s.commit()
        out = tools.save_strategy_tool(s, u.id, c.id, {"name": "내전략"})
    assert out["success"] is True and out["strategy_id"] == 7
    assert saved["ir"]["universe"]["symbols"] == ["005930"]   # 마지막 IR 재사용
    assert saved["ir"]["name"] == "내전략"
    assert compiled["called"] is False                         # 재컴파일 0(토큰 절감)


def test_save_compiles_when_no_prior_simulate(monkeypatch):
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine
    from app.models import User, Conversation
    from app.chat import tools
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(tools, "compile_strategy",
                        lambda s, uid, nl: {"success": True, "ir": {"compiled": True}})
    monkeypatch.setattr(tools, "save_ir_draft",
                        lambda s, uid, ir: type("R", (), {"id": 9, "name": ir.get("name")}))
    with Session(eng) as s:
        u = User(email="t2@e.com"); s.add(u); s.commit(); s.refresh(u)
        c = Conversation(user_id=u.id); s.add(c); s.commit(); s.refresh(c)
        out = tools.save_strategy_tool(s, u.id, c.id, {"name": "새전략", "nl": "삼성 전략"})
    assert out["success"] is True and out["strategy_id"] == 9
```

- [ ] **Step 2: 실패 확인**

Run: `cd server && python -m pytest tests/test_chat_tools.py -k save -q`
Expected: FAIL — `save_strategy_tool() takes 3 positional arguments but 4 were given` (또는 마지막 IR 재사용 미구현)

- [ ] **Step 3: 구현** — `server/app/chat/tools.py`

(a) `SAVE_STRATEGY_TOOL` 스키마를 `name` + 선택 `nl`로:
```python
SAVE_STRATEGY_TOOL = {
    "name": "save_strategy",
    "description": ("합의된 전략을 사용자 전략 목록에 draft로 저장. 사용자가 명시적으로 '저장'을 원하고 "
                    "앞서 simulate로 백테스트한 전략이 있을 때 호출(그 전략을 그대로 저장한다). "
                    "저장만 하며 모의/실전은 웹 자동매매 메뉴에서 사용자가 한다."),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "전략 이름."},
            "nl": {"type": "string",
                   "description": "(직전 simulate가 없을 때만) 저장할 전략의 자연어 서술."},
        },
        "required": ["name"],
    },
}
```

(b) 상단 import에 `Message` 추가(마지막 IR 조회용):
```python
from ..models import Message
```

(c) `save_strategy_tool`을 교체:
```python
def _last_simulate_ir(session, conversation_id) -> dict | None:
    """대화의 마지막 성공 simulate tool_result에 동봉된 검증 IR — 재컴파일 회피(토큰 절감)."""
    if session is None or conversation_id is None:
        return None
    from sqlmodel import select
    msgs = session.exec(select(Message).where(Message.conversation_id == conversation_id)
                        .order_by(Message.id.desc())).all()
    for m in msgs:
        if m.role != "assistant":
            continue
        for p in reversed(m.parts or []):
            if p.get("type") == "tool_result" and p.get("name") == "simulate":
                ir = (p.get("result") or {}).get("ir")
                if ir:
                    return dict(ir)
    return None


def save_strategy_tool(session, user_id, conversation_id, tool_input: dict) -> dict:
    """합의된 전략을 draft 저장. **마지막 simulate의 검증 IR을 재사용**(재컴파일 0); 없으면 nl로 컴파일.

    검증/저장 실패는 예외 대신 {success:False,error}로 — agent 루프가 모델에 피드백(고아 방지).
    """
    from fastapi import HTTPException
    from ..routers.strategies import save_ir_draft
    name = (tool_input.get("name") or "").strip()
    ir = _last_simulate_ir(session, conversation_id)
    if ir is None:
        nl = str(tool_input.get("nl") or "").strip()
        if not nl:
            return {"success": False, "error": "저장할 전략이 없습니다. 먼저 simulate로 백테스트하거나 전략을 서술해 주세요."}
        comp = compile_strategy(session, user_id, nl)
        if not comp.get("success"):
            return {"success": False, "error": comp.get("error") or "전략 컴파일 실패"}
        ir = comp["ir"]
    if name:
        ir["name"] = name
    try:
        row = save_ir_draft(session, user_id, ir)
    except HTTPException as e:
        return {"success": False, "error": str(e.detail)}
    except Exception as e:  # noqa: BLE001 — 저장 실패를 모델 피드백으로 표면화
        return {"success": False, "error": f"전략 저장 실패: {e}"}
    return {"success": True, "strategy_id": row.id, "name": row.name, "run_mode": "draft"}
```

> `from fastapi import HTTPException`·`save_ir_draft`는 기존처럼 함수 안 lazy import 유지. `compile_strategy`는 Task 2에서 모듈 상단에 import됨(테스트가 `tools.compile_strategy`·`tools.save_ir_draft`를 monkeypatch하므로, `save_ir_draft`도 모듈 상단 import로 올린다: `from ..routers.strategies import save_ir_draft`).

- [ ] **Step 4: 통과 확인**

Run: `cd server && python -m pytest tests/test_chat_tools.py tests/test_chat_agent.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add server/app/chat/tools.py server/tests/test_chat_tools.py
git commit -m "feat(chat): save_strategy NL 위임 + 마지막 simulate IR 재사용(재컴파일 0)"
```

---

## Task 5: screen — sector 필터(universe.screener)

**Files:**
- Modify: `server/app/chat/tools.py` (SCREEN_TOOL · assemble_ir screen 분기)
- Test: `server/tests/test_chat_tools.py`

- [ ] **Step 1: 분류 attribute 확인** — sector 필터가 어느 attribute를 타는지 엔진에서 확인(KR 분류는 Sector=소속부/Industry=KSIC로 미묘). 

Run: `cd server && python -c "import quant_core as qc; df=qc.load_dataset_for(['005930']).get('005930'); print([c for c in df.columns if 'ector' in c or 'ndustr' in c.lower() or c in ('Sector','Industry')])"`
Expected: 사용 가능한 분류 컬럼명 출력(예: `Sector`·`Industry`). **출력된 실제 컬럼명을 아래 `ATTR`에 사용**(없으면 `Sector` 기본, golden로 재확인). contains-match는 확정(반도체→반도체 제조업).

- [ ] **Step 2: 실패 테스트 작성** — `server/tests/test_chat_tools.py`에 추가

```python
def test_screen_sector_builds_screener():
    from app.chat.tools import assemble_ir
    ir = assemble_ir("screen", {"score_ref": "__SELF__.pb_ratio", "top_n": 3,
                                 "descending": False, "sector": "반도체"})
    sc = ir["universe"]["screener"]["condition"]
    assert sc["op"] == "is_in"
    assert sc["inputs"]["signal"]["op"] == "attribute"
    assert sc["params"]["values"] == ["반도체"] and sc["params"]["match"] == "contains"


def test_screen_without_sector_unchanged():
    from app.chat.tools import assemble_ir
    ir = assemble_ir("screen", {"score_ref": "__SELF__.pb_ratio", "top_n": 3, "symbols": ["005930"]})
    assert ir["universe"] == {"kind": "list", "symbols": ["005930"]}
    assert "screener" not in ir["universe"]
```

- [ ] **Step 3: 실패 확인**

Run: `cd server && python -m pytest tests/test_chat_tools.py -k screen_sector -q`
Expected: FAIL — `KeyError: 'screener'` (sector 미처리)

- [ ] **Step 4: 구현** — `server/app/chat/tools.py`

(a) `SCREEN_TOOL` properties에 `sector` 추가:
```python
            "sector": {"type": "string",
                       "description": "업종/섹터명으로 후보를 거름(예: 반도체). symbols 대신 사용."},
```

(b) `assemble_ir`의 screen 분기 교체(`ATTR`은 Step 1에서 확인한 실제 분류 컬럼명, 기본 `"Sector"`):
```python
    if tool_name == "screen":
        sector = str(tool_input.get("sector") or "").strip()
        symbols = list(tool_input.get("symbols") or [])
        if sector:
            # 모델이 종목 universe를 추측하지 않도록 — 섹터를 screener(부분일치)로 결정적 빌드.
            # contains: 분류 데이터가 KSIC 자유서술("반도체 제조업")이라 정확매칭은 0건.
            universe = {"kind": "all", "screener": {"condition": {
                "op": "is_in",
                "inputs": {"signal": {"op": "attribute", "params": {"attr": "Sector"}}},
                "params": {"values": [sector], "match": "contains"}}}}
        elif symbols:
            universe = {"kind": "list", "symbols": symbols}
        else:
            universe = {"kind": "all"}
        return {
            "universe": universe,
            "signal": {"op": "data", "params": {"ref": tool_input["score_ref"]}},
            "query": "select",
            "select": {"top_n": int(tool_input["top_n"]),
                       "descending": bool(tool_input.get("descending", True)),
                       "display": list(tool_input.get("display") or [])},
        }
```

- [ ] **Step 5: 통과 확인 (단위 + 통합)**

Run: `cd server && python -m pytest tests/test_chat_tools.py -k screen -q`
Expected: PASS

추가 — 실제 섹터 필터링 정합성(엔진 통합, golden):
Run: `cd server && python -c "import sys; sys.stdout.reconfigure(encoding='utf-8'); from app.chat.tools import run_tool; r=run_tool('screen', {'score_ref':'__SELF__.pb_ratio','top_n':3,'descending':False,'sector':'반도체'}); print(r.get('success'), r.get('eligible_size'), [x['symbol'] for x in (r.get('results') or [])])"`
Expected: `True`, eligible_size>0, 결과 종목이 실제 반도체 업종. **만약 eligible_size=0이면 Step 1의 `ATTR`을 `Industry`로 바꿔 재시도**(Sector가 대형주 nan인 taxonomy 한계).

- [ ] **Step 6: 커밋**

```bash
git add server/app/chat/tools.py server/tests/test_chat_tools.py
git commit -m "feat(chat): screen sector 필터 — universe.screener(contains-match)로 universe 추측 제거"
```

---

## Task 6: inspect — 교정 피드백(유효 컬럼)

**Files:**
- Modify: `server/app/chat/tools.py` (`run_inspect`)
- Test: `server/tests/test_chat_tools.py`

- [ ] **Step 1: 실패 테스트 작성** — `server/tests/test_chat_tools.py`에 추가

```python
def test_inspect_unknown_column_returns_valid_options(monkeypatch):
    import pandas as pd
    from app.chat import tools
    df = pd.DataFrame({"Close": [1.0, 2.0], "consensus_target": [3.0, 4.0]})
    monkeypatch.setattr(tools.qc, "load_dataset_for", lambda syms: {"005930": df})
    out = tools.run_inspect({"symbol": "005930", "columns": ["target_price"]})
    assert out["success"] is False
    assert "Close" in out["error"] and "consensus_target" in out["error"]   # 유효 컬럼 제시
```

- [ ] **Step 2: 실패 확인**

Run: `cd server && python -m pytest tests/test_chat_tools.py -k inspect_unknown -q`
Expected: FAIL (현 error는 "해당 컬럼이 없습니다: target_price"만, 유효 대안 없음)

- [ ] **Step 3: 구현** — `server/app/chat/tools.py` `run_inspect`의 `have` 미존재 분기 교체:

```python
    have = [c for c in columns if c in df.columns]
    if not have:
        avail = ", ".join(list(df.columns)[:40])
        return {"success": False,
                "error": f"해당 컬럼이 없습니다: {', '.join(columns)}. 사용 가능한 컬럼(일부): {avail}"}
```

- [ ] **Step 4: 통과 확인**

Run: `cd server && python -m pytest tests/test_chat_tools.py -k inspect -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add server/app/chat/tools.py server/tests/test_chat_tools.py
git commit -m "feat(chat): inspect 미존재 컬럼 시 유효 컬럼 목록 피드백(모델 자가수정)"
```

---

## Task 7: 에이전트 루프 — graceful 종료

**Files:**
- Modify: `server/app/chat/agent.py` (`stream_chat_turn` 루프 종료부)
- Test: `server/tests/test_chat_agent.py`

- [ ] **Step 1: 실패 테스트 작성** — `server/tests/test_chat_agent.py`에 추가. (루프가 MAX_TOOL_ROUNDS를 tool_use로 소진하면 최종 텍스트 없이 끝나던 것 → fallback 텍스트 보장.)

```python
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
    assert any(p["type"] == "text" and "완료하지 못" in p["text"] for p in parts)
```

- [ ] **Step 2: 실패 확인**

Run: `cd server && python -m pytest tests/test_chat_agent.py -k exhaustion -q`
Expected: FAIL (소진 시 fallback 텍스트 없음)

- [ ] **Step 3: 구현** — `server/app/chat/agent.py` `stream_chat_turn`: 루프가 자연 종료했는지 추적하고, tool_use로 소진했으면 fallback 텍스트 추가. `ok = True` 다음 줄에 `completed = False` 추가, `break` 직전에 `completed = True`, 루프/except 뒤에 fallback 블록.

```python
    ok = True
    completed = False
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            with client.messages.stream(...) as stream:   # (기존)
                ...
            ...
            if resp.stop_reason != "tool_use":
                completed = True
                break
            ...  # tool 처리 (기존)
    except Exception:   # noqa: BLE001 (기존)
        ok = False
        ...
    if ok and not completed:
        # 라운드 상한을 도구호출로 소진 — 최종 답변 없이 끝나는 무응답 방지(graceful).
        msg = "요청을 완료하지 못했어요(분석이 길어졌습니다). 조금 더 구체적으로 말씀해 주시겠어요?"
        assistant_parts.append({"type": "text", "text": msg})
        yield ("delta", {"text": msg})
    _persist(session, conversation_id, "assistant", assistant_parts)   # (기존)
    _persist_turn_metric(...)                                          # (기존)
    yield ("done", {"parts": assistant_parts})                        # (기존)
```

- [ ] **Step 4: 통과 확인 (전체 챗봇 회귀)**

Run: `cd server && python -m pytest tests/test_chat_agent.py tests/test_chat_api.py tests/test_chat_analytics.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add server/app/chat/agent.py server/tests/test_chat_agent.py
git commit -m "feat(chat): 에이전트 루프 — 라운드 소진 시 graceful fallback(무응답 방지)"
```

---

## Task 8: 통합 검증 + 배포 스모크 (수동·게이트)

> 코드 변경 없음. **push/머지/배포는 사용자 명시 허락 + 무거래창 필요** — 자율 실행 금지.

- [ ] **Step 1: 전체 챗봇·컴파일 회귀**

Run: `cd server && python -m pytest tests/test_chat_tools.py tests/test_chat_agent.py tests/test_chat_api.py tests/test_chat_analytics.py tests/test_compile_service.py -q`
Expected: 전부 PASS.

- [ ] **Step 2: archetype 회귀(컴파일러 커버리지)**

Run: `cd server && python -m evals.compile_archetypes` (실 Anthropic 키 필요 — 로컬 .env)
Expected: M5d(조건 양방향 선물 당일매매) 포함 archetype green. *키 없으면 스킵하고 §재진단으로 대체.*

- [ ] **Step 3: app import 스모크**

Run: `cd server && python -c "from app.main import app; from app.compile_service import compile_strategy; from app.chat.tools import run_simulate; print('ok')"`
Expected: `ok`

- [ ] **Step 4: (사용자 허락 후) push + PR**

```bash
git push -u origin feat/chat-ir-delegation
gh pr create --repo MercKR/quantman --base main --fill
```

- [ ] **Step 5: (사용자 허락 + 무거래창 후) 머지 → 배포 → 재진단**
머지·배포 후, 측정환경으로 **실측 델타**:
```bash
railway run python -m app.chat_analytics stats --days 1
railway run python -m app.chat_analytics transcripts --days 1
```
프로덕션에서 실패했던 S&P→코스피200 백테스트를 다시 시도 → **simulate 1라운드 성공·무한 8라운드/36초 소멸** 확인. 이것이 본 재설계의 최종 검증.

---

## Self-Review

**Spec coverage** (spec §5 대비):
- §5.1 공유 compile_strategy → Task 1 ✓ (compile_service.py로 — 순수 컴파일러 ir_compiler.py 비대화 회피, 라우터 중복 제거)
- §5.2 simulate IR→NL → Task 2 ✓
- §5.3 save IR→NL + 마지막 IR 재사용 → Task 4 ✓
- §5.4 screen sector → Task 5 ✓ (분류 attribute는 Step 1·5에서 실측 확인)
- §5.5 inspect 피드백 → Task 6 ✓
- §5.6 루프 graceful → Task 7 ✓
- §5.7 프롬프트 + 라우팅 보존 → Task 3 ✓
- §8 검증(archetype M5d + 재진단) → Task 8 ✓

**Placeholder scan:** screen `ATTR`(Sector/Industry)은 placeholder 아님 — Step 1에서 실측 컬럼명 확인 후 사용하는 명시적 verify-step(기본 Sector, eligible_size=0이면 Industry). 그 외 TODO/TBD 없음.

**Type consistency:** `compile_strategy(session, user_id, nl)→{success,ir,assumptions,explanation,...}` 시그니처가 Task 1 정의 ↔ Task 2 run_simulate ↔ Task 4 save에서 일치. `save_strategy_tool(session, user_id, conversation_id, tool_input)` 4-인자가 Task 2 디스패치 ↔ Task 4 정의 일치. `run_simulate(session, user_id, tool_input)`도 일치.

**주의(실행 순서):** Task 2의 agent.py 디스패치는 Task 4의 `save_strategy_tool(session, uid, conversation_id, inp)` 4-인자 최종형을 가정 — **Task 2·4를 연달아 구현**(또는 Task 2 단독 커밋 시 save 분기를 임시로 3-인자 유지 후 Task 4에서 정정).

---

## Execution Handoff

(writing-plans 이후 — 사용자 선택)

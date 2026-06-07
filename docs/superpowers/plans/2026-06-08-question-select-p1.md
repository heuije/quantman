# SELECT 동사(스크리닝) Implementation Plan — P1

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 질문 큐브에 **SELECT 동사**(`query="select"`)를 추가해 "as-of 스냅샷에서 score를 횡단 랭크해 상위 종목을 선별"하는 스크리닝을 구현한다 — "저평가 반도체주 3개" 같은 첫 사용자 체감 질의.

**Architecture:** P0의 `run_query` 디스패치에 select 분기 추가(가산적). `run_select`는 기존 엔진 헬퍼(`_universe_symbols`·`_screener_mask`·`_scoped`·`evaluate`)를 재사용해 시계열 시뮬 없이 *현 시점 단면*을 랭크한다. 답은 구조화된 랭킹 리스트(LLM 내러티브는 후속). 검증은 전적으로 로컬(결정적 골든 스크린 테스트).

**Tech Stack:** Python(pydantic v2, pandas, pytest) — `core/quant_core`; FastAPI — `server/app`; React+TS — `web`.

**범위 밖(후속):** LLM 내러티브 답변층(API키 필요)·웹 빌더 UI(라이브 round-trip은 백엔드 필요)·`universe.kind="portfolio"`(P2)·extremize/포트폴리오 최적화(P4)·RELATE 심화(P5). **enum에 extremize/portfolio 추가 금지.**

**워크플로:** branch `feature/question-plane-p0`에 스택. subagent는 편집·테스트만(**git commit 금지** — 메인 세션이 검토 후 로컬 커밋). 검증은 로컬. 모든 bash는 `cd /c/Users/USER/_wt_futures && PYTHONUTF8=1 ...`. **`C:\Users\USER\Desktop\창업\퀀트\platform` 절대 접근 금지.**

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `core/quant_core/ir_engine/spec.py` | IR 스키마 | `query` Literal에 "select"; 신규 `SelectSpec`; `StrategyIR.select`; validate(score 요구) |
| `core/quant_core/ir_engine/run.py` | 디스패치+러너 | `run_query` select 분기; 신규 `run_select` |
| `core/quant_core/ir_engine/__init__.py` | 익스포트 | `run_select` export |
| `core/quant_core/ir_engine/capabilities.py` | NL 가시성 | `query`에 select 항목 + `select` 설정 설명 |
| `core/tests/test_question_select.py` | 골든 스크린 테스트(신규) | 결정적 랭킹·자격·as_of 단언 |
| `server/app/ir_compiler.py` | NL 프롬프트 | 스크리닝 idiom |
| `server/evals/compile_archetypes.py` | NL eval | "저평가 반도체주 3개" archetype(구조) |
| `web/src/types.ts` | 웹 타입 | IrStrategyDef에 query "select"·`select` 추가 |

---

## Task 1: SelectSpec 스키마 + query enum + validate

**Files:**
- Modify: `core/quant_core/ir_engine/spec.py` (Study 정의 직후 ~:184에 SelectSpec; StrategyIR :191 query Literal; :192 직후 select 필드; validate_strategy)
- Test: `core/tests/test_question_select.py` (생성)

- [ ] **Step 1: 스키마 단위 테스트 작성(실패 예정)**

**Create** `core/tests/test_question_select.py` (이 파일은 Task 2에서 골든도 추가 — 우선 스키마부터):

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.ir_engine.spec import StrategyIR

_SIG = {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}}
_BASE = {"universe": {"kind": "list", "symbols": ["AAA", "BBB"]}, "signal": _SIG}


def test_select_query_and_spec_parse():
    s = StrategyIR.model_validate({**_BASE, "query": "select",
                                   "select": {"top_n": 3, "descending": False,
                                              "display": ["pb_ratio"]}})
    assert s.query == "select"
    assert s.select is not None and s.select.top_n == 3
    assert s.select.descending is False and s.select.as_of == "latest"


def test_select_defaults():
    s = StrategyIR.model_validate({**_BASE, "query": "select", "select": {"top_n": 5}})
    assert s.select.descending is True and s.select.display == [] and s.select.top_pct is None
```

- [ ] **Step 2: 실패 확인**

Run: `cd /c/Users/USER/_wt_futures && PYTHONUTF8=1 python -m pytest core/tests/test_question_select.py -q -p no:cacheprovider`
Expected: FAIL — `query`에 "select" 없음(ValidationError) / `select` 필드 없음.

- [ ] **Step 3: SelectSpec 추가 + query enum + select 필드**

spec.py에서 `class Study(BaseModel):` 정의가 끝난 직후(StrategyIR 앞)에 추가:

```python
class SelectSpec(BaseModel):
    """SELECT 동사 — as-of 스냅샷 횡단 랭킹 선별 설정."""
    as_of: str = "latest"               # "latest" 또는 ISO 날짜(그 시점 이하 마지막 단면)
    top_n: Optional[int] = None         # 상위 N (top_pct와 둘 중 하나)
    top_pct: Optional[float] = None     # 또는 상위 %(0<pct<=100)
    descending: bool = True             # score 큰 순(False=작은 순 — 예: 저PER)
    display: list[str] = Field(default_factory=list)   # 결과에 붙일 지표 컬럼(pb_ratio 등)
```

StrategyIR(:191) `query` Literal에 "select" 추가, `select` 필드 추가:

```python
    query: Literal["select", "describe", "relate", "simulate"] = "simulate"
    study: Study = Field(default_factory=Study)
    select: Optional[SelectSpec] = None    # query="select" 전용
```

- [ ] **Step 4: validate_strategy에 select 정합 규칙 추가**

`validate_strategy`(spec.py 하단) 내에, signal out_type을 보는 기존 분기 근처에 추가(기존 헬퍼 `_out_type`/`signal_out_type` 사용 — scheduled의 score 요구와 동형):

```python
    # SELECT 동사 — 랭킹이므로 score 신호 필요 + select 설정 정합.
    if s.query == "select":
        if signal_out_type(s.signal) != "score":
            issues.append(Issue(rule="S-SEL", severity=SEV_ERROR, is_error=True,
                                message="select(스크리닝)은 랭킹용 score 신호가 필요합니다(condition 불가).",
                                path="signal"))
        sel = s.select
        if sel is None:
            issues.append(Issue(rule="S-SEL", severity=SEV_ERROR, is_error=True,
                                message="select 질의는 select 설정(top_n 또는 top_pct)이 필요합니다.",
                                path="select"))
        else:
            if (sel.top_n is None) == (sel.top_pct is None):
                issues.append(Issue(rule="S-SEL", severity=SEV_ERROR, is_error=True,
                                    message="top_n과 top_pct 중 정확히 하나를 지정하세요.", path="select"))
            if sel.top_n is not None and sel.top_n < 1:
                issues.append(Issue(rule="S-SEL", severity=SEV_ERROR, is_error=True,
                                    message="top_n은 1 이상이어야 합니다.", path="select.top_n"))
            if sel.top_pct is not None and not (0 < sel.top_pct <= 100):
                issues.append(Issue(rule="S-SEL", severity=SEV_ERROR, is_error=True,
                                    message="top_pct는 0 초과 100 이하여야 합니다.", path="select.top_pct"))
```

> `signal_out_type`·`Issue`·`SEV_ERROR`는 spec.py가 이미 import/정의(검증 기존 사용). 없으면 `_out_type`(run.py)과 동형으로 `get(s.signal.op).out_type.value` 사용.

- [ ] **Step 5: 통과 확인**

Run: `cd /c/Users/USER/_wt_futures && PYTHONUTF8=1 python -m pytest core/tests/test_question_select.py -q -p no:cacheprovider`
Expected: 2 passed.

- [ ] **Step 6: (커밋은 메인 세션이) — 변경 파일 보고**
spec.py·core/tests/test_question_select.py.

---

## Task 2: run_select 구현 + run_query 디스패치 + 골든 스크린 테스트

**Files:**
- Modify: `core/quant_core/ir_engine/run.py` (`run_query` :116-127 select 분기; 신규 `run_select`)
- Modify: `core/quant_core/ir_engine/__init__.py` (run_select export)
- Test: `core/tests/test_question_select.py` (골든 추가)

- [ ] **Step 1: 골든 스크린 테스트 작성(실패 예정)**

`core/tests/test_question_select.py`에 추가(합성 픽스처 — 결정적):

```python
import numpy as np
import pandas as pd
from quant_core.ir_engine import run_query


def _df(pb: float, mcap: float, n: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open": np.full(n, 100.0), "High": np.full(n, 101.0),
        "Low": np.full(n, 99.0), "Close": np.full(n, 100.0),
        "Volume": np.full(n, 1e6),
        "pb_ratio": np.full(n, pb),      # 종목별 상수(결정적 랭킹)
        "market_cap": np.full(n, mcap),
    }, index=idx)


# pb: A=0.8 B=3.0 C=1.2 D=0.5 E=2.0  (저평가=낮은 pb)  market_cap: D만 소형(5e10)
_DS = {"AAA": _df(0.8, 9e11), "BBB": _df(3.0, 9e11), "CCC": _df(1.2, 9e11),
       "DDD": _df(0.5, 5e10), "EEE": _df(2.0, 9e11)}
_PB = {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}}


def _select_ir(**select):
    return {"universe": {"kind": "list", "symbols": list(_DS)},
            "signal": _PB, "query": "select", "select": select}


def test_run_select_ranks_lowest_pb_first():
    res = run_query(__import__("quant_core.ir_engine.spec", fromlist=["StrategyIR"])
                    .StrategyIR.model_validate(_select_ir(top_n=3, descending=False,
                                                          display=["pb_ratio"])), _DS)
    assert res["success"] and res["query"] == "select"
    assert res["universe_size"] == 5 and res["eligible_size"] == 5
    syms = [r["symbol"] for r in res["results"]]
    assert syms == ["DDD", "AAA", "CCC"]                 # pb 0.5 < 0.8 < 1.2
    assert res["results"][0]["metrics"]["pb_ratio"] == 0.5
    assert res["results"][0]["score"] == 0.5


def test_run_select_screener_eligibility():
    # 대형주만(market_cap > 1e11) → DDD(소형) 제외 → 저pb 상위 2 = AAA, CCC
    ir = _select_ir(top_n=2, descending=False, display=["pb_ratio"])
    ir["universe"]["screener"] = {"condition": {
        "op": "compare", "params": {"op": ">"},
        "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.market_cap"}},
                   "right": {"op": "const", "params": {"value": 1e11}}}}}
    s = __import__("quant_core.ir_engine.spec", fromlist=["StrategyIR"]).StrategyIR.model_validate(ir)
    res = run_query(s, _DS)
    assert res["success"] and res["eligible_size"] == 4   # DDD 제외
    assert [r["symbol"] for r in res["results"]] == ["AAA", "CCC"]


def test_run_select_rejects_condition_signal():
    bad = {"universe": {"kind": "list", "symbols": ["AAA"]},
           "signal": {"op": "compare", "params": {"op": ">"},
                      "inputs": {"left": _PB, "right": {"op": "const", "params": {"value": 1.0}}}},
           "query": "select", "select": {"top_n": 1}}
    s = __import__("quant_core.ir_engine.spec", fromlist=["StrategyIR"]).StrategyIR.model_validate(bad)
    res = run_query(s, _DS)
    assert res["success"] is False     # score 아님 → 거부
```

- [ ] **Step 2: 실패 확인**

Run: `cd /c/Users/USER/_wt_futures && PYTHONUTF8=1 python -m pytest core/tests/test_question_select.py -q -p no:cacheprovider`
Expected: 3 신규 테스트 FAIL(`run_query`가 select 미처리 — KeyError/`res["query"]` 없음 등).

- [ ] **Step 3: run_select 구현**

run.py에 추가(`_run_signal_study` 부근, 분석 러너 그룹):

```python
def run_select(strategy: StrategyIR, dataset: dict) -> dict:
    """SELECT 동사 — as-of 스냅샷에서 score를 횡단 랭크해 상위 종목 선별(스크리닝).

    시계열 시뮬 없음. signal(score)을 평가해 as_of 시점 단면을 랭크한다. universe.screener는
    자격 마스크(PIT), select.display는 결과에 붙일 지표 컬럼. 미래행 미참조(PIT).
    """
    from ..blocks import EvalContext, evaluate
    from ..blocks.node import Node
    from .engine import _screener_mask
    from ..expression_parser import get_symbol_group

    sel = strategy.select
    if sel is None:
        return _empty("select 질의는 select 설정이 필요합니다.")
    if _out_type(strategy.signal) != "score":
        return _empty("select(스크리닝)은 랭킹용 score 신호가 필요합니다.")
    syms = _universe_symbols(strategy, dataset)
    if not syms:
        return _empty("선별 유니버스에 종목이 없습니다.")
    screener = strategy.universe.screener or {}
    filt = Node.model_validate(screener["condition"]) if screener.get("condition") else None
    ds = _scoped(dataset, syms, strategy.signal, filt)
    ctx = EvalContext.from_dataset(ds)
    score = evaluate(strategy.signal, ctx)
    if not isinstance(score, pd.DataFrame) or score.empty:
        return _empty("score 신호가 패널(종목×날짜)을 산출하지 않습니다.")
    cols = [c for c in syms if c in score.columns]
    if not cols:
        return _empty("score가 유니버스 종목을 포함하지 않습니다.")
    score = score[cols]

    # as_of 스냅샷 (PIT — 미래행 미참조)
    if sel.as_of == "latest":
        asof = score.index[-1]
    else:
        prior = score.index[score.index <= pd.Timestamp(sel.as_of)]
        if len(prior) == 0:
            return _empty(f"as_of {sel.as_of} 이전 데이터가 없습니다.")
        asof = prior[-1]
    row = score.loc[asof]

    # 자격 마스크(screener) 적용 — 같은 as_of 단면
    if filt is not None:
        elig = _screener_mask(screener, ctx, cols)
        elig_row = (elig.loc[asof] if asof in elig.index
                    else elig.reindex([asof]).iloc[0]).reindex(cols).fillna(False).astype(bool)
        row = row.where(elig_row)
    eligible = row.dropna()
    eligible_size = int(eligible.shape[0])

    ranked = eligible.sort_values(ascending=not sel.descending)
    if sel.top_n is not None:
        ranked = ranked.head(int(sel.top_n))
    elif sel.top_pct is not None:
        k = max(1, int(round(eligible_size * float(sel.top_pct) / 100.0)))
        ranked = ranked.head(k)

    results = []
    for sym in ranked.index:
        df = dataset.get(sym)
        metrics = {}
        for col in sel.display:
            if df is not None and col in df.columns:
                sub = df.loc[df.index <= asof, col].dropna()
                metrics[col] = float(sub.iloc[-1]) if len(sub) else None
        results.append({
            "symbol": sym,
            "score": (float(ranked[sym]) if pd.notna(ranked[sym]) else None),
            "sector": get_symbol_group(sym, "Sector"),
            "metrics": metrics,
        })
    return {"success": True, "query": "select", "as_of": str(asof)[:10],
            "universe_size": len(syms), "eligible_size": eligible_size,
            "results": results}
```

> 사용 심볼 확인: `_empty`·`_out_type`·`_universe_symbols`·`_scoped`는 run.py에 이미 존재(P0). `pd`는 run.py 상단 import됨.

- [ ] **Step 4: run_query에 select 분기 추가**

run.py `run_query`(:116) `q = strategy.query` 직후, describe 분기 앞에 추가:

```python
    if q == "select":
        return run_select(strategy, dataset)
```

- [ ] **Step 5: __init__.py export**

`core/quant_core/ir_engine/__init__.py`의 `from .run import ... run_query` 줄에 `run_select` 추가, `__all__`에 `"run_select"` 추가.

- [ ] **Step 6: 통과 확인 + 골든·전체 회귀**

Run: `cd /c/Users/USER/_wt_futures && PYTHONUTF8=1 python -m pytest core/tests/test_question_select.py -v -p no:cacheprovider`
Expected: 5 passed(스키마 2 + 골든 3).
Run: `cd /c/Users/USER/_wt_futures && PYTHONUTF8=1 python -m pytest tests/test_backtest_golden.py core/tests/test_capability_coverage.py -q -p no:cacheprovider`
Expected: 골든 14 무변경. **capability_coverage는 이 시점 FAIL 예상**(select enum이 spec.py엔 있으나 capability_spec 미기술 → Task 3에서 해결). 메모만 하고 진행.

---

## Task 3: capability_spec에 select 노출 + 커버리지 가드

**Files:**
- Modify: `core/quant_core/ir_engine/capabilities.py` (`query` 항목에 select 추가 + `select` 설정 설명)

- [ ] **Step 1: 커버리지 가드가 select를 요구하는지 확인(실패 재현)**

Run: `cd /c/Users/USER/_wt_futures && PYTHONUTF8=1 python -m pytest core/tests/test_capability_coverage.py -v -p no:cacheprovider`
Expected: FAIL — missing 집합에 `'select'`(spec.py query Literal엔 있으나 capability_spec 미기술).

- [ ] **Step 2: capability_spec에 select 추가**

capabilities.py `query` 리스트(P0에서 simulate/describe/relate 노출한 곳)에 select 항목 추가:

```python
            {"value": "select",
             "does": "as-of 스냅샷에서 score를 횡단 랭크해 상위 종목을 선별(시계열 시뮬 없음)",
             "use_for": "저평가주·고배당주 등 '조건 맞는 상위 N개 종목' 스크리닝. "
                        "signal=랭킹 score(예: 낮은 PBR), universe.screener로 섹터·자격 필터, "
                        "select.top_n/top_pct·descending·display(근거 지표)."},
```

그리고 별도 키로 select 설정 노출:

```python
        "select": {
            "field": "select",
            "does": "SELECT 동사 설정 — as_of(기준시점·기본 latest)·top_n|top_pct·descending·display(근거 지표).",
            "use_for": "스크리닝 결과 모양 제어. 저PBR=descending:false, 고배당=descending:true 등.",
        },
```

- [ ] **Step 3: 커버리지 가드 통과**

Run: `cd /c/Users/USER/_wt_futures && PYTHONUTF8=1 python -m pytest core/tests/test_capability_coverage.py -q -p no:cacheprovider`
Expected: PASS(allowlist 빈 채 — select가 'value'로 노출됨).

---

## Task 4: NL 컴파일러 스크리닝 idiom + eval archetype(구조)

**Files:**
- Modify: `server/app/ir_compiler.py` (`<idioms>` 쿡북)
- Modify: `server/evals/compile_archetypes.py` (CASES)

- [ ] **Step 1: 스크리닝 idiom 추가**

ir_compiler.py `<idioms>` 섹션에 레시피 추가(문구):

```
- 스크리닝("저평가 X 상위 N개"): query="select" + signal=랭킹 score(예: 낮은 PBR이면
  data(__SELF__.pb_ratio)) + universe.kind=all + universe.screener.condition=
  is_in(attribute("Sector"), ["반도체"]) 같은 섹터/자격 필터 + select={top_n:N,
  descending:false(저평가=낮은값 우선)·true(높은값 우선), display:[pb_ratio, ...]}.
  백테스트(simulate)가 아니라 *현 시점 종목 리스트*가 답일 때 select를 쓴다.
```

- [ ] **Step 2: archetype 추가(구조 — 라이브 단언은 API키 환경)**

compile_archetypes.py `CASES`에 추가:

```python
    ("저평가 반도체주 3개",
     "저평가된 반도체 종목 3개만 골라줘 — PBR 낮은 순으로, PBR과 시가총액도 같이 보여줘.",
     lambda ir: (ir.get("query") == "select"
                 and (ir.get("select") or {}).get("top_n") == 3
                 and (ir.get("select") or {}).get("descending") is False)),
```

- [ ] **Step 3: 구조 검증(키 없이)**

Run: `cd /c/Users/USER/_wt_futures && PYTHONUTF8=1 python -c "import sys; sys.path[:0]=['core','server']; from app.ir_compiler import compile_nl; from server.evals.compile_archetypes import CASES; print('cases', len(CASES)); print([c[0] for c in CASES])"`
Expected: import 성공, CASES에 "저평가 반도체주 3개" 포함. (라이브 컴파일 단언은 ANTHROPIC_API_KEY 환경에서 `python -m evals.compile_archetypes` 별도 — 본 단계 범위 밖, 명시.)

---

## Task 5: 웹 타입(빌드 통과만)

**Files:**
- Modify: `web/src/types.ts` (IrStrategyDef)

- [ ] **Step 1: IrStrategyDef에 select 추가**

`query?`에 `"select"` 추가, `select?` 필드 추가:

```typescript
  query?: "select" | "describe" | "relate" | "simulate";
  select?: {
    as_of?: string; top_n?: number; top_pct?: number;
    descending?: boolean; display?: string[];
  };
```

- [ ] **Step 2: 빌드 통과**

Run: `cd /c/Users/USER/_wt_futures/web && npm run build 2>&1 | tail -6`
Expected: 타입에러 0, 빌드 성공.

---

## Self-Review (작성 후 점검 — 완료)
1. **spec 커버리지**: §5 SELECT(SelectSpec·as_of·top_n·descending·display·run_select·출력형)=Task1·2 · §1.1 매트릭스(종목군×SELECT)=Task2 골든 · §8 NL(V capability=Task3·T idiom=Task4·E archetype=Task4) · §6 P1(답=구조화 리스트)=Task2 출력. LLM 내러티브·웹빌더 범위밖 명시 — 일치.
2. **placeholder**: 없음. run_select·SelectSpec·골든 테스트·validate·capability 항목 전부 완전 코드.
3. **타입/이름 일관**: `query="select"`·`SelectSpec(as_of/top_n/top_pct/descending/display)`·`run_select`·출력 `{success,query,as_of,universe_size,eligible_size,results:[{symbol,score,sector,metrics}]}` — Task 전체 동일. 골든 단언이 출력 키와 일치.

## 리스크·주의
- **섹터 출력(get_symbol_group)**: 합성 테스트 심볼은 분류 데이터 없어 "기타" 반환 — 골든은 sector *값* 미단언(필드 존재만). 실데이터 섹터 필터는 KR classification 보유분에서 동작(별도).
- **capability_coverage 타이밍**: Task2 후~Task3 전 일시 FAIL(select enum 미기술) — 정상, Task3가 닫음. Task2·3은 한 묶음으로 검토.
- **NL 라이브 산출**: archetype 구조만(키 필요). 라이브 정확도는 키 환경 eval로 별도.
- subagent: 편집·테스트만, **git commit 금지**. 메인 세션이 Task별 검토 후 로컬 커밋(푸시는 워크플로대로 feature 브랜치).

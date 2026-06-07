# P4 (펼침 완성 — extremize 최적해 + 과최적화 가드) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** `study.reduction`에 **extremize**(최적해 찾기)를 추가 — 파라미터/종목 그리드를 백테스트해 목적함수(sharpe·cagr 등)를 최대/최소화하는 셀을 고르고, in-sample argmax의 과최적화를 OOS 시간폴드 일관성으로 가드한다. 사용자 #1a("최적 해 찾기") 슬라이스. **데이터 독립**(순수 컴퓨트) — 골든 검증 가능.

**Architecture:** `reduction="extremize"`면 `run_query`가 `run_extremize`로 라우팅. `run_extremize`는 param_grid(데카르트곱) 또는 assets를 각각 simulate 백테스트→`summarize_returns`로 셀별 perf→objective metric로 argmax/argmin. `oos_guard=True`(기본)면 최적 셀을 `run_period_split`(time_fold)로 재검해 OOS 일관성 표면화. 기존 머신(`run_strategy_ir`·`summarize_returns`·`run_period_split`) 재사용, 새 평가기 없음. 가산적 — 기존 sweep(enumerate/contrast/consistency) 무영향.

**Tech Stack:** Python(pydantic·pandas·numpy), pytest 골든.

---

## 컨텍스트 (P2 위에 스택 — feature/question-plane-p0, HEAD=b2e42e6)

- `Study`(spec.py:167): `reduction: Literal["enumerate","contrast","consistency"]`(extremize 미포함 — **gated**), `param_grid: list[ParamAxis]`, `assets: list[str]`, `axis: Literal["none","parameter","entity","label","time_fold"]`.
- `run_query`(run.py:111) simulate+study 라우팅: time_fold→`run_period_split`, none→`run_strategy_ir`, 그 외→`run_sweep`.
- `run_sweep`(run.py:181) parameter 분기: `base=strategy.model_dump()`, 각 combo에 `d["study"]={"axis":"none"}; d["query"]="simulate"; _set_path(d, ax.path, v)`, `run_strategy_ir(StrategyIR.model_validate(d), dataset)`, 버킷=`summarize_returns(daily_returns(res["equity"]))`. entity 분기: 종목별 `universe={"kind":"single","symbols":[a]}`.
- `run_period_split`(run.py:262): 전략 1회 실행→시간폴드(folds/split_dates)→`walk_forward_consistency`. 반환 `{success, axis:"period_split", buckets, consistency, metrics}`.
- `summarize_returns`(=`perf_from_returns`, metrics.py:19) 산출 키 = **PERF_KEYS** = `n,mean,std,sharpe,sortino,cum_return,cagr,mdd,win_rate,payoff_ratio,profit_factor,var_95,cvar_95`. ⚠ **mdd는 음수%**(낙폭). **calmar/total_return/ic_mean은 미산출**(calmar는 finalize_metrics 전용).
- `_set_path`(run.py:173), `copy`(imported), `daily_returns`·`summarize_returns`(imported from .sweep).
- `test_capability_coverage`: StrategyIR 트리 **모든 Literal**이 capability_spec에 `value`로 등장해야 PASS → 신규 `extremize`·`Objective.metric`·`Objective.direction` 값 전부 노출 필수.
- 기존 sweep 회귀 앵커: `tests/test_engine_sweep_axes.py`(parameter/entity/label/none). 골든: `tests/test_backtest_golden.py`(**GOLDEN 무수정**).
- 결정적 픽스처(`test_engine_sweep_axes._multi`): AAA(drift+0.003)>CCC(+0.001)>BBB(−0.001) cum_return; momentum_12_1m factor.

## 범위 밖 / 홀드 (기록만)
- **포트폴리오 평균-분산 QP**(spec §6 R4) — scipy/cvxpy 의존성 추가 = 무거운 결정·협의 필요. **홀드**(extremize와 별개 슬라이스).
- **ic_mean·calmar objective** — summarize_returns 미산출(ic=relate, calmar=finalize 전용). 노출 안 함(미산출 metric 노출=정직성 위배). 수요 시 후속.
- **2D+ 그리드 시각화(프런티어)** — P6.

## 제약·원칙
atomic(기존 run_strategy_ir·summarize_returns·run_period_split 재사용, 새 평가기 0). objective metric은 **실제 산출 키만**(검증된 해결책만 — PERF_KEYS ∩ 의미있는 것). over-engineering 금지(QP·calmar 제외). simulate hot path·GOLDEN 무수정. subagent는 편집·테스트만, **git commit/push 금지**.

---

## 파일 구조

| 파일 | 변경 |
|---|---|
| `core/quant_core/ir_engine/spec.py` | `Objective` 모델 신규; `Study.objective` 필드; `Study.reduction`+extremize; validate S-OPT |
| `core/quant_core/ir_engine/run.py` | `run_query` extremize 라우팅; `run_extremize` 신규 |
| `core/quant_core/ir_engine/capabilities.py` | study_reduction+extremize; objective_metric·objective_direction·objective 블록 |
| `core/quant_core/ir_engine/__init__.py` | `run_extremize`·`Objective` export |
| `core/tests/test_question_extremize_p4.py` | **신규 골든** |
| `server/app/ir_compiler.py` | query/study 문구; idiom #10(extremize) |
| `server/evals/compile_archetypes.py` | archetype 1종(최적 파라미터) |
| `web/src/types.ts` | study.reduction+extremize; study.objective; IrExtremizeResult |

---

### Task 1: 스키마 — Objective + Study.objective + reduction extremize + S-OPT

**Files:** Modify `core/quant_core/ir_engine/spec.py`; Test `core/tests/test_question_extremize_p4.py`(신규)

- [ ] **Step 1: 실패 테스트(스키마 파싱 + 검증)**

`core/tests/test_question_extremize_p4.py` 생성:
```python
"""P4 골든 — extremize(최적해) + 과최적화 OOS 가드.

기존 결정적 픽스처(test_engine_sweep_axes._multi: AAA drift>CCC>BBB)로 손계산 검증.

    cd platform && pytest core/tests/test_question_extremize_p4.py -v
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.blocks import data
from quant_core.ir_engine import (
    Entry, ParamAxis, PositionSpec, Sizing, SimSpec, StrategyIR, Study, Universe,
    run_query, validate_strategy,
)
from quant_core.ir_engine.spec import Objective


def _multi():
    idx = pd.date_range("2020-01-01", periods=252, freq="B")

    def mk(drift, mom):
        close = 100 * (1 + drift) ** np.arange(252)
        return pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                             "Close": close, "Volume": 1e6, "momentum_12_1m": float(mom)}, index=idx)
    return {"AAA": mk(0.003, 10.0), "BBB": mk(-0.001, -5.0), "CCC": mk(0.001, 2.0)}


def _factor():
    return StrategyIR(signal=data("momentum_12_1m"), universe=Universe(kind="all"),
                      position=PositionSpec(direction="long", sizing=Sizing(mode="equal_weight"),
                                            entry=Entry(mode="scheduled", rebalance="monthly", top_n=1)),
                      simulation=SimSpec(initial_capital=1e7))


def _errs(s):
    return [i.rule for i in validate_strategy(s) if i.is_error]


def test_objective_schema_defaults():
    o = Objective()
    assert o.metric == "sharpe" and o.direction == "max" and o.oos_guard is True


def test_reduction_extremize_parses():
    s = _factor()
    s.study = Study(axis="entity", reduction="extremize", assets=["AAA", "BBB"],
                    objective=Objective(metric="cum_return", direction="max", oos_guard=False))
    assert s.study.reduction == "extremize" and s.study.objective.metric == "cum_return"


def test_validate_extremize_requires_search_axis():
    s = _factor()
    s.study = Study(axis="none", reduction="extremize")
    assert "S-OPT" in _errs(s)        # none 축은 최적화 검색공간 없음


def test_validate_extremize_rejects_non_simulate():
    s = _factor()
    s.query = "describe"
    s.study = Study(axis="parameter", reduction="extremize",
                    param_grid=[ParamAxis(path="simulation.commission", values=[0.0, 0.01])])
    s.universe = Universe(kind="all")
    assert "S-OPT" in _errs(s)
```

- [ ] **Step 2: 실패 확인** — `cd platform && pytest core/tests/test_question_extremize_p4.py -v` → FAIL(Objective 미정의 등).

- [ ] **Step 3: 스키마 구현**

`spec.py` `Study` 클래스(현 :167) **직전**에 `Objective` 추가:
```python
class Objective(BaseModel):
    """reduction=extremize 전용 목적함수. metric은 summarize_returns 산출 키만(정직)."""
    # ⚠ mdd는 음수%(낙폭) — "낙폭 최소화"는 direction="max"(0에 가까울수록 좋음).
    metric: Literal["sharpe", "sortino", "cagr", "cum_return", "mdd"] = "sharpe"
    direction: Literal["max", "min"] = "max"
    oos_guard: bool = True   # in-sample 최적을 시간폴드 OOS 일관성으로 교차검증(과최적화 가드)
```

`spec.py` `Study`의 `reduction` Literal에 extremize 추가:
```python
    reduction: Literal["enumerate", "contrast", "consistency", "extremize"] = "enumerate"
```

`spec.py` `Study`에 `objective` 필드 추가(`event_basis` 줄 다음):
```python
    objective: Optional[Objective] = None   # reduction=extremize 목적함수(없으면 sharpe-max 기본)
```

`spec.py` `validate_strategy`의 펼침 검증부(현 :467 `st = s.study` 다음, S-sweep 규칙들 근처)에 S-OPT 추가:
```python
    # extremize(최적화) — 검색공간 축(parameter/entity) + simulate 동사 필요.
    if st.reduction == "extremize":
        if s.query != "simulate":
            issues.append(Issue("S-OPT", SEV_ERROR,
                                "extremize(최적해)는 손익 지표 기반이라 simulate 동사에서만 동작합니다.", "study"))
        if st.axis not in ("parameter", "entity"):
            issues.append(Issue("S-OPT", SEV_ERROR,
                                "extremize는 검색공간(axis=parameter 또는 entity)이 필요합니다.", "study"))
```

- [ ] **Step 4: 통과 확인** — `cd platform && pytest core/tests/test_question_extremize_p4.py -v` → 위 4건 PASS.

- [ ] **Step 5: Commit 없음** — 변경 파일만 보고.

---

### Task 2: run_extremize + 라우팅 — TDD 골든

**Files:** Modify `core/quant_core/ir_engine/run.py`; Test `core/tests/test_question_extremize_p4.py`

- [ ] **Step 1: 골든 테스트 추가**

`test_question_extremize_p4.py`에 추가:
```python
def _ext_entity(metric, direction, oos=False):
    s = _factor()
    s.study = Study(axis="entity", reduction="extremize", assets=["AAA", "BBB", "CCC"],
                    objective=Objective(metric=metric, direction=direction, oos_guard=oos))
    return s


def test_extremize_entity_max_cum_return_picks_AAA():
    res = run_query(_ext_entity("cum_return", "max"), _multi())
    assert res["success"] and res["reduction"] == "extremize" and res["axis"] == "asset"
    assert res["best"]["label"] == "AAA"          # drift 최고
    assert [r["label"] for r in res["ranked"]] == ["AAA", "CCC", "BBB"]   # cum_return 내림차순


def test_extremize_entity_min_picks_BBB():
    res = run_query(_ext_entity("cum_return", "min"), _multi())
    assert res["best"]["label"] == "BBB"          # drift 음수


def test_extremize_parameter_lower_commission_wins():
    s = _factor()
    s.study = Study(axis="parameter", reduction="extremize",
                    param_grid=[ParamAxis(path="simulation.commission", values=[0.0, 0.02])],
                    objective=Objective(metric="cum_return", direction="max", oos_guard=False))
    res = run_query(s, _multi())
    assert res["success"] and res["best"]["label"] == "commission=0.0"   # 저비용=고수익


def test_extremize_oos_guard_structure():
    res = run_query(_ext_entity("cum_return", "max", oos=True), _multi())
    assert "oos_guard" in res and ("consistency" in res["oos_guard"] or "error" in res["oos_guard"])


def test_extremize_oos_guard_absent_when_off():
    res = run_query(_ext_entity("cum_return", "max", oos=False), _multi())
    assert "oos_guard" not in res
```

- [ ] **Step 2: 실패 확인** — `cd platform && pytest core/tests/test_question_extremize_p4.py -k extremize -v` → FAIL(run_extremize 미정의).

- [ ] **Step 3: run_query 라우팅 + run_extremize 구현**

`run.py` `run_query`(현 :124-129)의 simulate+study 라우팅에 extremize 분기 추가(run_sweep 폴백 직전):
```python
    st = strategy.study
    if st.axis == "time_fold":
        return run_period_split(strategy, dataset)
    if st.axis == "none":
        return run_strategy_ir(strategy, dataset)
    if st.reduction == "extremize":
        return run_extremize(strategy, dataset)
    return run_sweep(strategy, dataset)
```

`run.py` `run_sweep` **직후**(현 :258 `return _empty(f"미지원 펼침 축...")` 다음, run_period_split 이전)에 추가:
```python
# ── 최적화 (extremize 환원 — 최적해 + 과최적화 OOS 가드) ────────────────────────

def run_extremize(strategy: StrategyIR, dataset: dict) -> dict:
    """펼침 축의 셀 중 목적함수 최대/최소 셀(최적해)을 찾고 OOS 일관성으로 과최적화를 가드.

    axis=parameter: param_grid 데카르트곱 / axis=entity: assets — 각 셀을 simulate 백테스트해
    summarize_returns로 perf 산출, objective.metric를 direction대로 argmax/argmin. in-sample
    argmax는 과최적화 위험이라 oos_guard=True면 최적 셀을 시간폴드(run_period_split)로 재검해
    OOS 일관성을 표면화한다(견고한 최적 vs 우연한 spike 구분). 새 평가기 없이 기존 머신 재사용.
    """
    from .spec import Objective
    st = strategy.study
    obj = st.objective or Objective()
    base = strategy.model_dump()

    combos: list = []   # [(label, kind, payload)]  kind∈{"param","entity"}
    if st.axis == "parameter":
        grid = st.param_grid
        if not grid or any(not ax.values for ax in grid):
            return _empty("파라미터 최적화는 param_grid(경로·값)가 필요합니다.")
        import itertools
        for combo in itertools.product(*[ax.values for ax in grid]):
            patch = {ax.path: v for ax, v in zip(grid, combo)}
            label = " | ".join(f"{ax.path.split('.')[-1]}={v}" for ax, v in zip(grid, combo))
            combos.append((label, "param", patch))
    elif st.axis == "entity":
        if not st.assets:
            return _empty("종목 최적화는 assets(종목 목록)가 필요합니다.")
        for a in st.assets:
            combos.append((a, "entity", a))
    else:
        return _empty(f"extremize는 parameter 또는 entity 축이 필요합니다 (현재: {st.axis}).")

    def _build(kind, payload) -> StrategyIR:
        d = copy.deepcopy(base)
        d["study"] = {"axis": "none"}; d["query"] = "simulate"
        if kind == "entity":
            d["universe"] = {"kind": "single", "symbols": [payload],
                             "screener": None, "exclude_macro": True}
        else:
            for path, v in payload.items():
                _set_path(d, path, v)
        return StrategyIR.model_validate(d)

    cells: list = []   # [(label, perf, kind, payload)]
    for label, kind, payload in combos:
        res = run_strategy_ir(_build(kind, payload), dataset)
        if res.get("success"):
            cells.append((label, summarize_returns(daily_returns(res["equity"])), kind, payload))
    if not cells:
        return _empty("최적화할 유효 결과가 없습니다(모든 셀 실패).")

    sign = 1.0 if obj.direction == "max" else -1.0

    def _score(perf):
        v = perf.get(obj.metric)
        return sign * float(v) if (v is not None and v == v) else float("-inf")  # NaN=최악

    cells.sort(key=lambda c: _score(c[1]), reverse=True)
    best_label, best_perf, best_kind, best_payload = cells[0]
    out = {
        "success": True, "axis": ("asset" if st.axis == "entity" else "parameter"),
        "reduction": "extremize", "objective": obj.model_dump(),
        "best": {"label": best_label, "metric_value": best_perf.get(obj.metric), "perf": best_perf},
        "ranked": [{"label": l, "metric_value": p.get(obj.metric)} for l, p, _, _ in cells],
    }
    if obj.oos_guard:
        guard = _build(best_kind, best_payload)
        guard.study = Study(axis="time_fold", reduction="consistency", folds=4)
        ps = run_period_split(guard, dataset)
        out["oos_guard"] = ({"buckets": ps.get("buckets"), "consistency": ps.get("consistency")}
                            if ps.get("success") else {"error": ps.get("error")})
    return out
```
(`Study` import 필요 시 run.py 상단에 이미 `from .spec import StrategyIR`만 있으면 `from .spec import StrategyIR, Study`로 확장. `Objective`는 함수 내 지연 import로 충분.)

- [ ] **Step 4: 통과 확인** — `cd platform && pytest core/tests/test_question_extremize_p4.py -v` → 전건 PASS.

- [ ] **Step 5: Commit 없음** — 변경 파일·`Study` import 추가 여부 보고.

---

### Task 3: capability_spec — extremize + objective 노출

**Files:** Modify `core/quant_core/ir_engine/capabilities.py`; Test `core/tests/test_capability_coverage.py`(기존)

- [ ] **Step 1: study_reduction에 extremize + objective 블록 추가**

`capabilities.py` `study_reduction` 리스트(현 :195-201) 끝에 추가:
```python
            {"value": "extremize", "does": "축의 셀 중 목적함수(objective)를 최대/최소화하는 최적 셀 선택 + OOS 과최적화 가드",
             "use_for": "파라미터·종목 최적해 — '샤프 최대 파라미터'·'가장 나은 종목'. axis=parameter(+param_grid) "
                        "또는 entity(+assets) + study.objective와 함께. (enumerate=모든 셀 나열과 구분 — 최적 1개 선택.)"},
```

`capabilities.py` `study_reduction` 다음에 objective 메타 추가:
```python
        # extremize 목적함수 — metric(최적화 대상)·direction·과최적화 가드. summarize_returns 산출 지표만.
        "objective_metric": [
            {"value": "sharpe", "does": "샤프 비율(위험조정수익, 기본)"},
            {"value": "sortino", "does": "소르티노(하방위험조정)"},
            {"value": "cagr", "does": "연복리수익률(%)"},
            {"value": "cum_return", "does": "누적수익률(%)"},
            {"value": "mdd", "does": "최대낙폭(음수%) — ⚠ '낙폭 최소화'는 direction=max(0에 가까울수록 좋음)"},
        ],
        "objective_direction": [
            {"value": "max", "does": "최대화"},
            {"value": "min", "does": "최소화"},
        ],
        "objective": {
            "field": "study.objective",
            "does": "extremize 목적함수 — metric·direction·oos_guard(in-sample 최적을 시간폴드 OOS 일관성으로 교차검증).",
            "use_for": "최적화 기준 지정. 기본=sharpe/max/guard. 예: '낙폭 최소'=metric:mdd+direction:max.",
        },
```

- [ ] **Step 2: 커버리지 가드 통과** — `cd platform && pytest core/tests/test_capability_coverage.py -v` → PASS(extremize·sharpe·sortino·cagr·cum_return·mdd·max·min 전부 노출).

- [ ] **Step 3: Commit 없음** — 보고.

---

### Task 4: 패키지 export

**Files:** Modify `core/quant_core/ir_engine/__init__.py`

- [ ] **Step 1: run_extremize + Objective export**

`__init__.py` run import에 `run_extremize` 추가; spec import에 `Objective` 추가; `__all__`에 둘 다 추가.

- [ ] **Step 2: import 스모크** — `cd platform && python -c "from quant_core.ir_engine import run_extremize, Objective; print('ok')"` → `ok`.

- [ ] **Step 3: Commit 없음** — 보고.

---

### Task 5: NL 컴파일러 — extremize idiom + archetype

**Files:** Modify `server/app/ir_compiler.py`, `server/evals/compile_archetypes.py`

- [ ] **Step 1: study 스키마 문구에 reduction extremize·objective 명시**

`ir_compiler.py:165`의 study 스키마 문구의 `"reduction":"enumerate|contrast|consistency"`를 `"reduction":"enumerate|contrast|consistency|extremize"`로, 끝에 `, "objective":{{"metric":..,"direction":"max|min","oos_guard":bool}}`(extremize 전용) 추가.

- [ ] **Step 2: idiom #10 추가**

`<idioms>` 블록 끝(`</idioms>` 직전)에:
```python
10. [최적 파라미터/종목 찾기(extremize)] "샤프(또는 수익률·CAGR)를 *최대화*하는 [기간/임계값/top_n] 찾아줘"·
    "어떤 종목이 제일 나은가"처럼 *그리드 중 최적 1개*가 답이면 → study.axis="parameter"(+param_grid)
    또는 "entity"(+assets) + study.reduction="extremize" + study.objective={{metric, direction, oos_guard:true}}.
    metric은 sharpe(기본)·sortino·cagr·cum_return·mdd만. ⚠ mdd는 음수라 "낙폭 최소"=direction:"max".
    oos_guard=true(기본)면 최적값을 시간폴드로 재검(과최적화 경고). (※ enumerate=모든 셀 나열, extremize=최적 1개.)
```

- [ ] **Step 3: archetype 1종 추가**

`compile_archetypes.py` `CASES`에:
```python
    ("최적 파라미터(extremize)",
     "RSI 진입 임계값을 10부터 40까지 5단위로 바꿔가며 샤프를 최대화하는 값을 찾아줘.",
     lambda ir: ir.get("study", {}).get("reduction") == "extremize"
                and ir.get("study", {}).get("axis") == "parameter"),
```
(⚠ 구조만 — 라이브 산출은 API키 필요·홀드.)

- [ ] **Step 4: import 스모크** — `cd platform && python -c "import sys; sys.path.insert(0,'server'); sys.path.insert(0,'core'); import app.ir_compiler; import evals.compile_archetypes as m; print('ok', len(m.CASES))"` → `ok N`.

- [ ] **Step 5: Commit 없음** — 보고.

---

### Task 6: 웹 타입

**Files:** Modify `web/src/types.ts`

- [ ] **Step 1: reduction extremize + objective + 결과 타입**

`types.ts` IrStrategyDef.study의 `reduction`(현 :290)에 `"extremize"` 추가. study에 objective 필드:
```typescript
    objective?: {
      metric?: "sharpe" | "sortino" | "cagr" | "cum_return" | "mdd";
      direction?: "max" | "min"; oos_guard?: boolean;
    } | null;                                  // reduction=extremize 전용
```

IrPortfolioDiagnosis 다음에:
```typescript
// reduction="extremize" — 최적해 + OOS 과최적화 가드 결과.
export interface IrExtremizeResult {
  success: boolean; axis: "parameter" | "asset"; reduction: "extremize";
  objective: { metric: string; direction: string; oos_guard: boolean };
  best: { label: string; metric_value: number | null; perf: Record<string, number> };
  ranked: { label: string; metric_value: number | null }[];
  oos_guard?: { buckets?: Record<string, unknown>; consistency?: unknown; error?: string };
}
```

- [ ] **Step 2: 웹 빌드** — `cd web && npm run build` → 타입에러 0.

- [ ] **Step 3: Commit 없음** — 보고.

---

### Task 7: 전체 회귀 검증 (게이트)

- [ ] **Step 1: P4 골든** — `cd platform && pytest core/tests/test_question_extremize_p4.py -v` → 전건 PASS.
- [ ] **Step 2: 기존 sweep·capability·migration 무회귀** — `cd platform && pytest tests/test_engine_sweep_axes.py core/tests/test_capability_coverage.py core/tests/test_question_migration.py tests/test_engine_spec.py -v` → 전건 PASS.
- [ ] **Step 3: 골든 백테스트 무변경** — `cd platform && pytest tests/test_backtest_golden.py -v` → 14 PASS, GOLDEN 무변경.
- [ ] **Step 4: 전체 스위트** — `cd platform && pytest core/tests tests server/tests -q` → 새 실패 0(P2 후 606 기준 + P4 신규만 증가). 실패 시 근본원인 조사·보고(추측 금지).
- [ ] **Step 5: 보고** — passed/failed 카운트, 골든 무변경 확인, 미해결 위험.

---

## Self-Review

**Spec coverage:** P4 "extremize 환원(목적함수 argmax+과최적화 가드)" → Task 1·2. 포트폴리오 QP는 명시 홀드(범위 밖, R4 협의). 게이트 "회귀 정합·OOS 일관성" → oos_guard(Task 2)·전체 회귀(Task 7). NL V/T/E → Task 3(V)·5(T·E). 4계층 → UI=NL(5)+웹(6), 엔진=run_extremize(2), 데이터=독립, 자동매매=N/A. ✅

**Placeholder scan:** 모든 코드 스텝 실제 코드. archetype은 구조 단언만(라이브=API키 홀드, 명시). 없음.

**Type consistency:** `Objective`(metric/direction/oos_guard) ↔ capability objective_metric/objective_direction ↔ web objective ↔ NL idiom 값 일치(sharpe/sortino/cagr/cum_return/mdd, max/min). `run_extremize` 결과 dict(axis/reduction/objective/best{label,metric_value,perf}/ranked/oos_guard) ↔ 골든 단언(Task 2) ↔ web IrExtremizeResult 일치. metric 키는 PERF_KEYS 부분집합(실제 산출 — calmar/ic_mean 제외).

**검증 가능성:** entity extremize 손계산(AAA>CCC>BBB cum_return → max=AAA·min=BBB·랭킹순). parameter는 저커미션=고수익(commission=0.0 승). mdd 음수 부호 문서화. oos_guard 구조 단언. 골든14 무변경=불변식.

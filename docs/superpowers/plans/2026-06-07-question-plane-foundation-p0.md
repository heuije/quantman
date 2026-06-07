# 질문 평면 토대(P0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `StrategyIR`의 분석 의도를 1급 `query`(동사) + `study`(펼침) 평면으로 승격하고, 기존 `sweep`/`simulation.period_split`을 거기로 흡수한다 — **simulate/describe/relate 동작은 100% 무변경**.

**Architecture:** 클린 컷오버 마이그레이션(영구 호환 alias 없음, block_ir_spec §1.2). 안전을 위해 *임시* `model_validator(mode="before")` 마이그레이션 셰임을 둬 레거시 dict→신스키마로 매핑 → 전 과정 테스트 green 유지 → 마지막에 셰임 제거. 최상위 디스패치 `run_query`가 기존 `run_sweep`/`run_period_split`/`run_strategy_ir`/분석함수를 신모델 인스턴스로 위임(흡수). 골든 백테스트(`tests/test_backtest_golden.py`)가 회귀 안전망.

**Tech Stack:** Python(pydantic v2, pandas, pytest) — `core/quant_core`; FastAPI — `server/app`; React+TS(Vite, `tsc -b`) — `web`.

**범위 밖(후속 단계 — enum 슬롯 미리 만들지 말 것):** `query="select"`+SelectSpec=P1 · `universe.kind="portfolio"`=P2 · `reduction="extremize"`+Objective+포트폴리오 최적화=P4 · RELATE 다중팩터/회귀 심화=P3 · 데이터 4종 수급=P3. **P0의 `query` enum = {describe, relate, simulate}만, `study.reduction` = {enumerate, contrast, consistency}만.**

---

## 레거시 → 신(新) 매핑 (마이그레이션의 정전 — 셰임·테스트·문서 전부 이 표 기준)

`has_period × sweep` 동시 사용은 `validate_strategy`가 이미 금지(spec.py:478-479)하므로 케이스는 상호배타·무모호.

| 레거시 (현재) | 신 (목표) |
|---|---|
| `sweep.target == "signal"` | `query="describe"` (axis="none", reduction="enumerate") |
| `sweep.target == "relation"` | `query="relate"` (study.target_node·windows·relation_kind 보존, event 없음=IC 모드) |
| `sweep.axis == "time"` (target 무관) | `query="relate"` + `study.event = sweep.event` (event 모드) + study.windows·event_basis 보존 |
| `sweep.target=="return"` & `axis=="none"` | `query="simulate"`, study.axis="none", reduction="enumerate" |
| `…return` & `axis=="parameter"` | `query="simulate"`, study.axis="parameter", reduction="enumerate", study.param_grid 보존 |
| `…return` & `axis=="asset"` | `query="simulate"`, study.axis="entity", reduction="enumerate", study.assets 보존 |
| `…return` & `axis=="condition"` | `query="simulate"`, study.axis="label", reduction="contrast", study.label 보존 |
| `simulation.period_split != "single"` 또는 `split_dates` | `query="simulate"`, study.axis="time_fold", reduction="consistency", study.folds=(2 if oos else 4), study.split_dates 보존 |
| 공통: `sweep.label` | `study.label` |

> describe/relate는 동사 분기(axis 무시)라 study.axis는 "none" 유지. period_split의 옛 `kfold`/`walk_forward`는 둘 다 folds=4, `oos`=folds=2 (run_period_split 기존 동작과 동일).

---

## 신 스키마 (목표 코드 — Task 1에서 작성)

```python
# core/quant_core/ir_engine/spec.py  (SweepSpec 대체, SimSpec에서 period_split/split_dates 제거)

class Study(BaseModel):
    """평면4 — 질문을 한 축으로 펼치고 환원(옛 SweepSpec + period_split 흡수)."""
    axis: Literal["none", "parameter", "entity", "label", "time_fold"] = "none"
    reduction: Literal["enumerate", "contrast", "consistency"] = "enumerate"  # extremize=P4
    # axis 파라미터
    param_grid: list[ParamAxis] = Field(default_factory=list)   # axis=parameter
    assets: list[str] = Field(default_factory=list)             # axis=entity (옛 asset)
    label: Optional[Node] = None                                # axis=label (옛 condition) + describe/relate 국면
    folds: int = 4                                              # axis=time_fold (옛 period_split)
    split_dates: list[str] = Field(default_factory=list)        # axis=time_fold 명시 경계
    # describe/relate 분석 파라미터 (SweepSpec에서 보존 — P3에서 RelateSpec로 재배치)
    target_node: Optional[Node] = None
    relation_kind: Literal["ic"] = "ic"
    event: Optional[Node] = None
    windows: list[int] = Field(default_factory=lambda: [5, 10, 20])
    event_basis: Literal["close", "intraday", "excess"] = "close"

class StrategyIR(BaseModel):
    query: Literal["describe", "relate", "simulate"] = "simulate"   # 평면3 동사 (기본=현 동작)
    universe: Universe
    signal: Node
    position: Position = Field(default_factory=Position)
    simulation: SimSpec = Field(default_factory=SimSpec)            # period_split/split_dates 제거됨
    study: Study = Field(default_factory=Study)                     # 옛 sweep

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data):
        """[임시 마이그레이션 셰임 — Task 15에서 삭제] 레거시 dict(sweep/period_split)→신(query/study).
        위 '레거시→신 매핑' 표를 그대로 구현. 신 형태 dict는 그대로 통과."""
        if not isinstance(data, dict):
            return data
        if "study" in data or "query" in data:
            return data  # 이미 신 형태
        sw = data.pop("sweep", None) or {}
        sim = data.get("simulation") or {}
        study: dict = {}
        # 분석 파라미터 보존
        for k in ("label", "target_node", "relation_kind", "event", "windows", "event_basis"):
            if k in sw:
                study[k] = sw[k]
        tgt, axis = sw.get("target", "return"), sw.get("axis", "none")
        ps, sd = sim.get("period_split", "single"), sim.get("split_dates") or []
        if tgt == "signal":
            data["query"] = "describe"
        elif tgt == "relation" or axis == "time":
            data["query"] = "relate"
            if axis == "time" and sw.get("event") is not None:
                study["event"] = sw["event"]
            study["param_grid"] = sw.get("param_grid", [])
        else:
            data["query"] = "simulate"
            if ps != "single" or sd:
                study.update(axis="time_fold", reduction="consistency",
                             folds=(2 if ps == "oos" else 4), split_dates=sd)
            elif axis == "parameter":
                study.update(axis="parameter", reduction="enumerate", param_grid=sw.get("param_grid", []))
            elif axis == "asset":
                study.update(axis="entity", reduction="enumerate", assets=sw.get("assets", []))
            elif axis == "condition":
                study.update(axis="label", reduction="contrast")
        if isinstance(sim, dict):  # SimSpec에서 제거된 키 청소
            sim.pop("period_split", None); sim.pop("split_dates", None)
        data["study"] = study
        return data
```

## 신 디스패치 (목표 코드 — Task 5)

```python
# core/quant_core/ir_engine/run.py
def run_query(strategy: StrategyIR, dataset: dict) -> dict:
    """최상위 질문 디스패치 — 동사(query) + 펼침(study)으로 경로 선택."""
    err = _root_type_error(strategy)
    if err is not None:
        return _empty(err)
    q = strategy.query
    if q == "describe":
        return _run_signal_study(strategy, dataset)
    if q == "relate":
        return (_run_event_study(strategy, dataset)
                if strategy.study.event is not None else _run_ic_study(strategy, dataset))
    # simulate
    st = strategy.study
    if st.axis == "time_fold":
        return run_period_split(strategy, dataset)
    if st.axis == "none":
        return run_strategy_ir(strategy, dataset)
    return run_sweep(strategy, dataset)   # parameter/entity/label
```

---

## Task 1: 신 스키마 작성 + 마이그레이션 셰임 (엔진 green 유지)

**Files:**
- Modify: `core/quant_core/ir_engine/spec.py` (SweepSpec→Study 교체 :172-190, SimSpec period_split/split_dates 제거 :141-145, StrategyIR :195+ 에 query/study/셰임 추가, `from pydantic import ... model_validator` import 추가)

- [ ] **Step 1: 베이스라인 — 현재 전체 그린 + 골든 시그니처 캡처**

Run: `cd /c/Users/USER/_wt_futures && python -m pytest tests/ core/tests/ server/tests/ -q 2>&1 | tail -20`
Expected: 전부 PASS (이 green이 리팩터 내내 유지돼야 할 기준). 실패가 있으면 **여기서 멈추고 보고** — 리팩터 전 깨진 테스트가 있으면 안전망이 오염됨.

- [ ] **Step 2: `Study` 모델 작성 + `SweepSpec` 제거**

위 "신 스키마" 블록의 `Study` 클래스로 `SweepSpec`(spec.py:172-190)을 교체. `ParamAxis`는 그대로 둠.

- [ ] **Step 3: `SimSpec`에서 `period_split`·`split_dates` 제거**

spec.py:141-145의 `period_split`·`split_dates` 두 필드 삭제(주석 포함). 나머지 SimSpec 필드(선물 연속물 등)는 유지.

- [ ] **Step 4: `StrategyIR`에 `query`·`study` 필드 + `_migrate_legacy` 셰임 추가, import 보강**

spec.py 상단 `from pydantic import BaseModel, Field` → `from pydantic import BaseModel, Field, model_validator`. StrategyIR에서 `sweep: SweepSpec` 필드를 `query`+`study`로 교체하고 위 셰임 메서드 추가(StrategyIR 정의 전체를 "신 스키마" 블록대로).

- [ ] **Step 5: 셰임이 레거시 dict를 신 형태로 매핑하는지 단위 테스트**

**File:** Create `core/tests/test_question_migration.py`

```python
import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.ir_engine.spec import StrategyIR

_BASE = {"universe": {"kind": "single", "symbols": ["AAA"]},
         "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
         "position": {"entry": {"mode": "always"}}}

def _m(**extra):
    return StrategyIR.model_validate({**_BASE, **extra})

def test_legacy_return_none_to_simulate():
    s = _m(sweep={"axis": "none", "target": "return"})
    assert s.query == "simulate" and s.study.axis == "none"

def test_legacy_parameter_axis():
    s = _m(sweep={"axis": "parameter", "param_grid": [{"path": "simulation.commission", "values": [0, 0.1]}]})
    assert s.query == "simulate" and s.study.axis == "parameter" and len(s.study.param_grid) == 1

def test_legacy_asset_to_entity():
    s = _m(sweep={"axis": "asset", "assets": ["AAA", "BBB"]})
    assert s.study.axis == "entity" and s.study.assets == ["AAA", "BBB"]

def test_legacy_condition_to_label_contrast():
    s = _m(sweep={"axis": "condition", "label": {"op": "calendar", "params": {"unit": "weekday"}}})
    assert s.study.axis == "label" and s.study.reduction == "contrast" and s.study.label is not None

def test_legacy_target_signal_to_describe():
    s = _m(sweep={"target": "signal", "target_node": {"op": "data", "params": {"ref": "__SELF__.rsi_14"}}})
    assert s.query == "describe" and s.study.target_node is not None

def test_legacy_target_relation_to_relate_ic():
    s = _m(sweep={"target": "relation", "target_node": {"op": "rank", "inputs": {"signal": {"op": "data", "params": {"ref": "momentum_12_1m"}}}}})
    assert s.query == "relate" and s.study.event is None

def test_legacy_axis_time_to_relate_event():
    s = _m(sweep={"axis": "time", "event": {"op": "compare", "params": {"op": "<"}, "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.rsi_14"}}, "right": {"op": "const", "params": {"value": 30}}}}})
    assert s.query == "relate" and s.study.event is not None

def test_legacy_period_split_oos_to_time_fold():
    s = StrategyIR.model_validate({**_BASE, "simulation": {"period_split": "oos"}})
    assert s.query == "simulate" and s.study.axis == "time_fold" and s.study.reduction == "consistency" and s.study.folds == 2

def test_new_form_passthrough():
    s = _m(query="simulate", study={"axis": "parameter", "reduction": "enumerate", "param_grid": [{"path": "simulation.leverage", "values": [1, 2]}]})
    assert s.query == "simulate" and s.study.axis == "parameter"
```

- [ ] **Step 6: 마이그레이션 단위 테스트 실행 (셰임 검증)**

Run: `cd /c/Users/USER/_wt_futures && python -m pytest core/tests/test_question_migration.py -v`
Expected: 9개 전부 PASS. (이 시점엔 엔진 내부가 아직 `strategy.sweep`을 참조하므로 *다른* 테스트는 깨질 수 있음 — Task 2~5에서 고침. 셰임만 먼저 단언.)

- [ ] **Step 7: Commit**

```bash
git add core/quant_core/ir_engine/spec.py core/tests/test_question_migration.py
git commit -m "feat(ir): question 평면 스키마(query/study) + 레거시 마이그레이션 셰임"
```

---

## Task 2: 엔진 분석 함수의 `sweep`/`period_split` 참조를 `study`/`query`로 전환

**Files:**
- Modify: `core/quant_core/ir_engine/run.py` (`_run_signal_study`:299·302·309, `_run_ic_study`:334·337·338·346, 이벤트스터디 함수들, `run_sweep`:168-244, `run_period_split`:259·261·264·277, `validate_strategy` 인용 :440-559)

- [ ] **Step 1: `run_sweep` 본체를 study 기반으로 + target 분기 제거**

`run_sweep`(run.py:159)에서 `sw = strategy.sweep` → `sw = strategy.study`. 상단 `target` 분기(171-174: `if sw.target == "signal"…relation…`) **삭제**(이 분기는 Task 5 `run_query`로 이동). axis 분기 갱신: `"asset"`→`"entity"`, `"condition"`→`"label"`, `"time"` 분기 삭제(이벤트스터디는 relate로 이동). `param_grid`/`assets`/`label` 참조는 이제 `sw.param_grid` 등(study에 보존됨)이라 그대로 동작. 내부 `d["sweep"] = {"axis": "none"}`(188·204) → `d["study"] = {"axis": "none"}` + `d["query"] = "simulate"`.

- [ ] **Step 2: 분석 함수의 `strategy.sweep.X` → `strategy.study.X`**

`_run_signal_study`·`_run_ic_study`·`_run_event_study` 내부의 `strategy.sweep.target_node`·`strategy.sweep.label`·`strategy.sweep.windows`·`strategy.sweep.event`·`strategy.sweep.event_basis` 전부 `strategy.study.X`로. (필드는 Study에 보존돼 있으므로 이름만.)

- [ ] **Step 3: `run_period_split`을 study 기반으로**

`run_period_split`(run.py:249)에서 `sim = strategy.simulation`의 `sim.split_dates`→`strategy.study.split_dates`, `sim.period_split` 분기(277-283)를 `strategy.study.folds`로(`mode=="oos"`→folds==2 판정은 folds 값으로: `n = strategy.study.folds`). oos 라벨링은 `folds==2`로 분기.

- [ ] **Step 4: `validate_strategy`의 sweep/period_split 인용 전환**

spec.py:440-559의 `s.sweep.*`→`s.study.*`, `s.sweep.event_basis`→`s.study.event_basis`, `s.sweep.target_node`→`s.study.target_node`, `s.sweep.label`/`s.sweep.event`→`s.study.*`. `has_period`(479) = `s.study.axis == "time_fold"`로. target 기반 검증(453: "target=signal·relation")은 `s.query in ("describe","relate")` 기준으로 전환. 검증 메시지의 `"sweep.target_node"` 등 path 문자열은 `"study.target_node"`로.

- [ ] **Step 5: 엔진 분석 테스트 실행**

Run: `cd /c/Users/USER/_wt_futures && python -m pytest tests/test_engine_sweep.py tests/test_engine_sweep_axes.py tests/test_engine_event_study.py tests/test_analysis_layer.py tests/test_period_split_dates.py core/tests/test_comparison_e2e.py -q`
Expected: PASS (테스트 fixture가 레거시 sweep dict여도 셰임이 신 형태로 매핑하므로 통과). 실패 시 매핑/참조 누락 — 메시지의 KeyError/AttributeError가 위치 지목.

- [ ] **Step 6: Commit**

```bash
git add core/quant_core/ir_engine/run.py core/quant_core/ir_engine/spec.py
git commit -m "refactor(engine): 분석/펼침/기간분할을 study/query 기반으로 전환"
```

---

## Task 3: 엔진 보조 모듈(`sweep.py`·`compare.py`·`comparison.py`·`compose.py`·`explain.py`·`service.py`·`__init__.py`) 전환

**Files:** Modify 위 7개. **먼저 인벤토리:**

- [ ] **Step 1: 잔여 참조 전수 인벤토리**

Run: `cd /c/Users/USER/_wt_futures && git grep -n "\.sweep\b\|\.period_split\|\.split_dates\|sweep\.target\|sweep\.axis\|SweepSpec\|run_condition_sweep\|sweep_condition" -- 'core/quant_core/*.py'`
Expected: 남은 참조 목록. 각각 매핑 표대로 전환(`.sweep`→`.study`, `.simulation.period_split`→`.study.axis=="time_fold"` 등).

- [ ] **Step 2: `service.py` 디스패치를 `run_query`로 단일화**

`service.py:141-147`의 `if s.sweep.axis != "none" or s.sweep.target != "return": run_sweep … elif period_split … else run_strategy_ir` 4줄 분기를 한 줄로:
```python
res = run_query(s, dataset)
```
import(`service.py:18`)에 `run_query` 추가.

- [ ] **Step 3: `__init__.py` 익스포트 갱신**

`core/quant_core/ir_engine/__init__.py`: `from .run import … run_query` 추가, `SweepSpec`(24·37) → `Study`로, `run_condition_sweep`/`sweep_condition`(28-29·39) 익스포트는 `sweep.py` 실제 함수명 유지 여부에 따라 — 함수 자체를 study용으로 두면 이름 유지 가능(내부만 study 참조). `__all__`(33-39)에서 `SweepSpec`→`Study`, `run_query` 추가.

- [ ] **Step 4: 나머지 모듈(`sweep.py`·`compare.py`·`comparison.py`·`compose.py`·`explain.py`) 잔여 참조 전환**

Step 1 인벤토리의 각 위치를 매핑대로. `explain.py`(전략 설명문)의 sweep/period_split 서술 분기도 query/study 기준으로.

- [ ] **Step 5: 코어 전체 테스트**

Run: `cd /c/Users/USER/_wt_futures && python -m pytest core/tests/ tests/ -q 2>&1 | tail -20`
Expected: PASS (셰임 덕에 레거시 fixture 통과).

- [ ] **Step 6: Commit**

```bash
git add core/quant_core/ir_engine/
git commit -m "refactor(engine): 보조 모듈·디스패치(run_query)·익스포트 study/query 통일"
```

---

## Task 4: 골든 불변 검증 (P0 핵심 게이트)

- [ ] **Step 1: 골든 백테스트 실행 — simulate 경로 무변경 증명**

Run: `cd /c/Users/USER/_wt_futures && python -m pytest tests/test_backtest_golden.py -v`
Expected: **전부 PASS, 골든값 변경 0**. (골든 fixture는 sweep/period_split을 안 쓰므로 simulate 경로가 보존됐다면 값이 그대로여야 함.) **하나라도 값이 틀리면 P0 실패** — 리팩터가 동작을 바꾼 것. 매핑/참조 회귀를 추적해 고친 뒤 재실행. 골든값을 임의로 갱신하지 말 것.

- [ ] **Step 2: (게이트 통과 기록) Commit (변경 없으면 생략)**

골든 통과만 확인(코드 변경 없음). 변경 필요했다면 그 수정과 함께 커밋.

---

## Task 5: 서버 계층(`ir_compiler.py`·`routers/ir.py`·evals·server 테스트) 전환

**Files:** Modify `server/app/ir_compiler.py`, `server/app/routers/ir.py`, `server/evals/compile_archetypes.py`, `server/tests/test_schema_issues.py`, `server/tests/test_preview_ir.py`, `server/tests/test_preview_live_basket.py`

- [ ] **Step 1: 서버 잔여 참조 인벤토리**

Run: `cd /c/Users/USER/_wt_futures && git grep -n "sweep\|period_split\|split_dates" -- 'server/**/*.py'`

- [ ] **Step 2: 참조 전환**

각 위치를 매핑대로(`sweep`→`study`, `target`→`query`). `ir_compiler.py`의 시스템 프롬프트·관용구에서 sweep/target 언급은 Task 6의 capability_spec 갱신과 일관되게 query/study 어휘로(상세 문구는 Task 6에서 capability와 함께).

- [ ] **Step 3: 서버 테스트**

Run: `cd /c/Users/USER/_wt_futures && python -m pytest server/tests/ -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add server/
git commit -m "refactor(server): IR 컴파일러·라우터·evals study/query 전환"
```

---

## Task 6: NL capability_spec + 커버리지 가드 전환

**Files:** Modify `core/quant_core/ir_engine/capabilities.py`(sweep_axis:163-177·sweep_target:178-185·period_split:154-160 → query·study 재구성), `core/tests/test_capability_coverage.py`(필요 시), `server/app/ir_compiler.py`(idiom 문구 일관화)

- [ ] **Step 1: capability_spec 재구성**

`capability_spec()`에서 `period_split` 항목 → `study_axis`의 `time_fold` 값으로 흡수. `sweep_axis`(none/condition/parameter/asset/time) → `study_axis`(none/parameter/entity/label/time_fold) — `asset`→`entity`, `condition`→`label`, `time` 제거(이벤트는 query=relate로 이동). `sweep_target`(return/signal/relation) → `query`(simulate/describe/relate). `reduction`(enumerate/contrast/consistency) 신규 노출. **extremize·select·portfolio는 노출 금지(후속 단계).** 각 항목 `{value, does, use_for}` 형식 유지.

- [ ] **Step 2: 커버리지 가드 실행 — 신 enum 전수 노출 확인**

Run: `cd /c/Users/USER/_wt_futures && python -m pytest core/tests/test_capability_coverage.py -v`
Expected: PASS. 실패 시 missing 집합이 capability_spec에 빠진 신 enum 값(예: `entity`·`time_fold`·`consistency`·`describe`·`relate`)을 지목 → 추가. (extremize/select/portfolio는 아직 spec.py enum에 없으므로 missing에 안 뜸 — 정직성 유지.)

- [ ] **Step 3: 관용구·field_contract 일관화**

`ir_compiler.py` `<idioms>` 쿡북·`field_contract`(spec.py:263)에서 sweep/target 표현을 query/study로. 동작 동일, 어휘만.

- [ ] **Step 4: 컴파일 관련 테스트**

Run: `cd /c/Users/USER/_wt_futures && python -m pytest tests/test_idiom_recipes.py tests/test_field_contract.py server/tests/test_schema_issues.py core/tests/test_capability_coverage.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/quant_core/ir_engine/capabilities.py core/tests/test_capability_coverage.py server/app/ir_compiler.py core/quant_core/ir_engine/spec.py
git commit -m "refactor(nl): capability_spec·관용구를 query/study 어휘로 (extremize/select 미노출)"
```

---

## Task 7: 웹 계층(`types.ts`·`IrBuilder.tsx`·`StrategyDetail.tsx`) 전환

**Files:** Modify `web/src/types.ts`(IrStrategyDef sweep:285·period_split:282-283·axis enum:340), `web/src/pages/IrBuilder.tsx`, `web/src/pages/StrategyDetail.tsx`

- [ ] **Step 1: 웹 잔여 참조 인벤토리**

Run: `cd /c/Users/USER/_wt_futures && git grep -n "sweep\|period_split\|split_dates" -- 'web/src'`
(`index.css`의 sweep은 CSS 애니메이션 키워드 — 무시. 코드 참조만.)

- [ ] **Step 2: `types.ts` IrStrategyDef 갱신**

`simulation`에서 `period_split?`·`split_dates?`(282-283) 제거. `sweep: {...}`(285) 블록을 `query?: "describe"|"relate"|"simulate"` + `study?: { axis?: "none"|"parameter"|"entity"|"label"|"time_fold"; reduction?: "enumerate"|"contrast"|"consistency"; param_grid?: …; assets?: string[]; label?: Node; folds?: number; split_dates?: string[]; target_node?: Node; windows?: number[]; event?: Node; event_basis?: …; relation_kind?: "ic" }`로 교체. axis enum(340)의 `"asset"`→`"entity"`·`"condition"`→`"label"`·`"period_split"`→`"time_fold"`·`"signal"|"relation"`은 query로 이동.

- [ ] **Step 3: `IrBuilder.tsx`·`StrategyDetail.tsx` 사용처 갱신**

sweep/period_split을 읽고 쓰는 UI 코드를 query/study로. (P0는 *기존 UI 동작 보존* — 펼침/기간분할 빌더가 신 필드를 emit하도록 매핑만. 신규 UI 없음.)

- [ ] **Step 4: 타입체크·빌드**

Run: `cd /c/Users/USER/_wt_futures/web && npm run build 2>&1 | tail -20`  (또는 `npx tsc -b`)
Expected: 타입 에러 0, 빌드 성공.

- [ ] **Step 5: Commit**

```bash
git add web/src/
git commit -m "refactor(web): IrStrategyDef·빌더 study/query 전환"
```

---

## Task 8: 잔여 테스트 fixture를 신 형태로 마이그레이션 (전수)

**Files:** Modify 레거시 sweep/period_split dict를 쓰는 모든 테스트 — `tests/test_engine_sweep.py`·`test_engine_sweep_axes.py`·`test_engine_event_study.py`·`test_analysis_layer.py`·`test_period_split_dates.py`·`test_engine_gaps.py`·`test_engine_spec.py`·`test_engine_strategy_run.py`·`test_server_ir_strategy.py`·`core/tests/test_comparison_e2e.py`·`local/tests/scenarios/*`

- [ ] **Step 1: 레거시 fixture 인벤토리**

Run: `cd /c/Users/USER/_wt_futures && git grep -n '"sweep"\|sweep=\|period_split\|split_dates' -- 'tests/**/*.py' 'core/tests/**/*.py' 'local/tests/**/*.py'`

- [ ] **Step 2: 각 fixture를 매핑 표대로 신 형태로 재작성**

예: `{"sweep": {"axis": "asset", "assets": [...]}}` → `{"query": "simulate", "study": {"axis": "entity", "assets": [...]}}`; `{"simulation": {"period_split": "oos"}}` → `{"query": "simulate", "study": {"axis": "time_fold", "reduction": "consistency", "folds": 2}}`. 결과 dict 키(`res["axis"]` 등) 단언도 신 값으로(`"asset"`→`"entity"` 등 — run_sweep이 반환하는 axis 라벨 확인).

- [ ] **Step 3: 전체 테스트 (셰임 의존 제거 확인 전 단계)**

Run: `cd /c/Users/USER/_wt_futures && python -m pytest core/tests/ tests/ server/tests/ local/tests/ -q 2>&1 | tail -25`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/ core/tests/ local/tests/
git commit -m "test: IR fixture 전수를 query/study 신 형태로 마이그레이션"
```

---

## Task 9: 마이그레이션 셰임 제거 + 최종 그린 (클린 컷오버 완료)

**Files:** Modify `core/quant_core/ir_engine/spec.py`(StrategyIR `_migrate_legacy` 삭제)

- [ ] **Step 1: 레거시 잔존 확인 — 셰임에 의존하는 곳이 없어야 함**

Run: `cd /c/Users/USER/_wt_futures && git grep -n '"sweep"\|sweep=\|\.sweep\b\|period_split\|split_dates\|"asset"\|"condition"' -- '*.py' 'web/src' | grep -v test_question_migration | grep -v index.css`
Expected: 빈 결과(또는 의도된 비-IR 잔존만). 남으면 그 위치를 신 형태로.

- [ ] **Step 2: `_migrate_legacy` 셰임 삭제**

spec.py StrategyIR에서 `@model_validator(mode="before") _migrate_legacy` 메서드 전체 삭제. (필요 없으면 `model_validator` import도 정리.)

- [ ] **Step 3: 셰임 단위 테스트 정리**

`core/tests/test_question_migration.py`의 레거시-입력 케이스는 이제 셰임이 없어 실패함 → **이 파일 삭제**(셰임은 일회성 마이그레이션 도구였음). `test_new_form_passthrough`만 남길 가치가 있으면 `test_question_schema.py`로 옮겨 신 형태 검증으로 유지.

```bash
git rm core/tests/test_question_migration.py
```

- [ ] **Step 4: 최종 전체 그린 + 골든 + 빌드**

Run: `cd /c/Users/USER/_wt_futures && python -m pytest core/tests/ tests/ server/tests/ local/tests/ -q 2>&1 | tail -25`
Expected: PASS.
Run: `cd /c/Users/USER/_wt_futures && python -m pytest tests/test_backtest_golden.py -v && cd web && npm run build 2>&1 | tail -5`
Expected: 골든 PASS(값 무변경) + 웹 빌드 성공.

- [ ] **Step 5: Commit**

```bash
git add core/quant_core/ir_engine/spec.py
git commit -m "refactor(ir): 마이그레이션 셰임 제거 — query/study 클린 컷오버 완료(P0)"
```

---

## Self-Review (계획 작성 후 점검 — 완료)

1. **spec 커버리지**: question_layer_spec §3(IR스키마)=Task1 · §4(디스패치 run_query)=Task5(service)+신디스패치블록 · §6 P0행(질문평면 1급화·sweep/compare 흡수)=Task1~9 · §10(golden 무변경)=Task4·Task9 · §11 리스크(전경로 동시수정·1PR)=Task2~8 전수 인벤토리+골든 안전망. SELECT/extremize/portfolio 미포함(후속) 명시 — 일치.
2. **placeholder**: 없음. 매핑표·스키마·디스패치·셰임은 완전 코드. 기계적 마이그레이션은 "grep으로 위치 → 매핑표 적용 → suite 검증"으로 규칙 완전 명시(재작성 규칙이 곧 코드).
3. **타입/이름 일관**: `query`{describe,relate,simulate}·`study`{axis: none/parameter/entity/label/time_fold, reduction: enumerate/contrast/consistency}·`run_query`·`Study`·`_migrate_legacy` — Task 전체 동일 사용. `asset→entity`·`condition→label`·`time→relate(event)`·`period_split→time_fold` 매핑 일관.

## 리스크·주의 (실행 시)
- **셰임이 곧 매핑의 정전** — Task1 셰임이 틀리면 전 테스트가 잘못된 신뢰를 줌. Task1 Step5의 9개 케이스가 셰임을 단언하므로 먼저 통과시킬 것.
- **골든은 simulate만 보존 증명** — describe/relate/펼침 회귀는 해당 테스트들(Task2 Step5)이 잡음. 둘 다 그린이어야 P0 완료.
- **한 PR 전수**(부분 머지 금지) — 셰임이 중간 그린을 보장하지만, 셰임 제거(Task9) 전까지는 미완 상태. 커밋은 잘게, 머지는 Task9 후.
- **git push·PR 생성은 사용자 명시 허락 후에만**(자동 금지).

# P5 (RELATE 심화 — 다중팩터 횡단 회귀 + 신뢰구간/t값) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** RELATE 동사에 **다중팩터 횡단 회귀**(Fama-MacBeth)를 추가 — 여러 설명변수(factors)가 forward 수익을 횡단으로 설명하는지 계수·t값·95% 신뢰구간으로 답한다. 현 relate는 단일팩터 IC·이벤트만. **데이터 독립**(순수 컴퓨트) — 골든 검증 + statsmodels 대조.

**Architecture:** `study.relation_kind="regression"` + `study.factors=[Node,...]`면 `run_query`가 `_run_regression_study`로 라우팅. 날짜별 횡단 OLS(numpy `lstsq`: forward수익 ~ 절편+팩터들)로 per-date 계수 β_t를 모으고, **Fama-MacBeth**로 집계(β̄=mean_t, se=std_t/√T, t=β̄/se, CI=β̄±1.96se). 기존 `_run_ic_study`의 forward수익·_scoped·evaluate 패턴 재사용. 가산적 — IC·event relate 무영향.

**Tech Stack:** Python(numpy lstsq), pytest 골든 + statsmodels 0.14.4 대조(설치 확인됨).

---

## 컨텍스트 (P4 위에 스택 — feature/question-plane-p0, HEAD=191c2d5)

- `Study`(spec.py): `relation_kind: Literal["ic"]="ic"`(regression 미포함 — **확장 대상**), `target_node`, `windows: list[int]=[5,10,20]`, `event`. **`factors` 필드 없음**(신규).
- `run_query`(run.py:111) relate 분기: `_run_event_study if study.event else _run_ic_study`.
- `_run_ic_study`(run.py:337): `syms=_universe_symbols`(≥2), `node=study.target_node`, `ctx=EvalContext.from_dataset(_scoped(dataset, syms, node, label))`, `factor=evaluate(node,ctx)`(DataFrame), `close=pd.DataFrame({s: dataset[s]["Close"]...}).reindex(factor.index)`, `fwd=close.shift(-w)/close-1`. forward수익=분석전용(look-ahead OK).
- validate(spec.py): `is_ic = s.query=="relate" and st.event is None` → target_node·windows·universe≥2 요구. relate+event=event study.
- `signal_out_type`/`validate`/`has_market_source`/`meaningfulness_issues`(spec.py import). `referenced_columns`(blocks.node — explain.py가 사용).
- `EvalContext`,`evaluate`,`_scoped`,`_universe_symbols`(run.py). `one_sample_test`·`compare_partition`(compare).
- statsmodels 0.14.4·scipy 1.13.1 설치됨(테스트 대조용).
- `test_capability_coverage`: 신규 `regression` Literal 노출 필수. 골든 앵커 `tests/test_backtest_golden.py`(**무수정**). 기존 relate 테스트=`tests/test_engine_event_study.py`·`core/tests/test_comparison_e2e.py`.

## 범위 밖 / 홀드 (기록만)
- **per-date OLS의 개별 표준오차(HAC/Newey-West)** — Fama-MacBeth는 cross-date 분산으로 t값을 내므로 per-date se 불요. HAC은 후속(필요 시 statsmodels).
- **R²·factor 직교화·시계열 회귀** — P5는 횡단 Fama-MacBeth만(과확장 회피).
- **NL 라이브 산출** — API키 eval(홀드).

## 제약·원칙
atomic(IC 스터디의 forward수익·_scoped·evaluate 재사용, 새 데이터경로 0). 검증된 해결책만(numpy lstsq=OLS, 골든은 statsmodels 대조). over-engineering 금지(R²·HAC 제외). simulate hot path·GOLDEN 무수정. subagent 편집·테스트만, **git commit/push 금지**.

---

## 파일 구조

| 파일 | 변경 |
|---|---|
| `core/quant_core/ir_engine/spec.py` | `Study.relation_kind`+regression; `Study.factors`; validate S-REG |
| `core/quant_core/ir_engine/run.py` | `run_query` relate 분기; `_fama_macbeth`·`_run_regression_study` 신규 |
| `core/quant_core/ir_engine/capabilities.py` | study_relation_kind+regression; factors 노트 |
| `core/quant_core/ir_engine/__init__.py` | `_run_regression_study`는 내부 — export 불필요(run_query 경유). (변경 없을 수 있음) |
| `core/tests/test_question_regression_p5.py` | **신규 골든**(+statsmodels 대조) |
| `server/app/ir_compiler.py` | study 문구; idiom #11(회귀) |
| `server/evals/compile_archetypes.py` | archetype 1종(다중팩터 회귀) |
| `web/src/types.ts` | study.relation_kind+regression; study.factors |

---

### Task 1: 스키마 — relation_kind regression + factors + S-REG

**Files:** Modify `core/quant_core/ir_engine/spec.py`; Test `core/tests/test_question_regression_p5.py`(신규)

- [ ] **Step 1: 실패 테스트(스키마·검증)**

`core/tests/test_question_regression_p5.py` 생성:
```python
"""P5 골든 — 다중팩터 횡단 회귀(Fama-MacBeth) + 신뢰구간/t값. statsmodels 대조.

    cd platform && pytest core/tests/test_question_regression_p5.py -v
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.blocks import data
from quant_core.ir_engine import (
    PositionSpec, SimSpec, StrategyIR, Study, Universe, run_query, validate_strategy,
)


def _errs(s):
    return [i.rule for i in validate_strategy(s) if i.is_error]


def _reg_ir(symbols, factors, windows=None):
    return StrategyIR(signal=data("__SELF__.Close"), universe=Universe(kind="list", symbols=symbols),
                      query="relate",
                      study=Study(relation_kind="regression", factors=factors,
                                  windows=windows or [5]))


def test_relation_kind_regression_parses():
    s = _reg_ir(["AAA", "BBB"], [data("__SELF__.fac1")])
    assert s.study.relation_kind == "regression" and len(s.study.factors) == 1


def test_validate_regression_requires_factors():
    s = _reg_ir(["AAA", "BBB"], [])
    assert "S-REG" in _errs(s)


def test_validate_regression_requires_multi_symbol():
    s = StrategyIR(signal=data("__SELF__.Close"), universe=Universe(kind="single", symbols=["AAA"]),
                   query="relate", study=Study(relation_kind="regression",
                                               factors=[data("__SELF__.fac1")], windows=[5]))
    assert "S-REG" in _errs(s)
```

- [ ] **Step 2: 실패 확인** — `cd platform && pytest core/tests/test_question_regression_p5.py -v` → FAIL(relation_kind regression 미허용·factors 없음).

- [ ] **Step 3: 스키마 구현**

`spec.py` `Study`의 `relation_kind`:
```python
    relation_kind: Literal["ic", "regression"] = "ic"
```
`spec.py` `Study`에 `factors` 필드 추가(`target_node` 줄 근처):
```python
    factors: list[Node] = Field(default_factory=list)   # relation_kind=regression 설명변수(다중)
```

`spec.py` `validate_strategy`의 relate 분석부 — 현 `is_ic = s.query == "relate" and st.event is None`를 교체(regression 모드 분리):
```python
    is_ic = s.query == "relate" and st.event is None and st.relation_kind == "ic"
    is_regression = s.query == "relate" and st.event is None and st.relation_kind == "regression"
```
그리고 IC target_node 요구 블록(`if describe_dist or is_ic:`) **다음**에 S-REG 추가:
```python
    if is_regression:
        if not st.factors:
            issues.append(Issue("S-REG", SEV_ERROR,
                                "다중 회귀는 설명변수(factors)가 1개 이상 필요합니다.", "study.factors"))
        if s.universe.kind == "single":
            issues.append(Issue("S-REG", SEV_ERROR,
                                "횡단 회귀는 종목이 2개 이상이어야 합니다.", "universe"))
        if not st.windows:
            issues.append(Issue("S-REG", SEV_ERROR,
                                "회귀는 forward 윈도우(windows)가 필요합니다.", "study.windows"))
        for f in st.factors:
            issues += list(validate(f, valid_refs))
            if signal_out_type(f) not in ("score", "condition"):
                issues.append(Issue("S-REG", SEV_ERROR,
                                    "설명변수(factor)는 score(점수) 또는 condition 블록이어야 합니다.",
                                    "study.factors"))
            if not has_market_source(f):
                issues.append(Issue("M-const", SEV_ERROR,
                                    "설명변수가 시장 데이터를 참조하지 않습니다.", "study.factors"))
```

- [ ] **Step 4: 통과 확인** — `cd platform && pytest core/tests/test_question_regression_p5.py -v` → 위 3건 PASS.

- [ ] **Step 5: Commit 없음** — 보고.

---

### Task 2: _fama_macbeth + _run_regression_study + 라우팅 — TDD 골든

**Files:** Modify `core/quant_core/ir_engine/run.py`; Test `core/tests/test_question_regression_p5.py`

- [ ] **Step 1: 골든 테스트 추가**

`test_question_regression_p5.py`에 추가:
```python
from quant_core.ir_engine.run import _fama_macbeth


def test_fama_macbeth_math():
    # per-date 계수 3기간 × 2팩터 — 손계산 대조.
    betas = np.array([[2.0, 0.0], [2.2, 0.2], [1.8, -0.2]])
    mean, se, t, lo, hi = _fama_macbeth(betas)
    assert abs(mean[0] - 2.0) < 1e-9 and abs(mean[1] - 0.0) < 1e-9
    # 팩터1: std(ddof=1)=0.2 → se=0.2/√3, t=2.0/se 큰 값; 팩터2: mean 0 → t≈0
    assert t[0] > 10 and abs(t[1]) < 1e-9
    assert lo[0] < 2.0 < hi[0]


def _reg_fixture():
    """forward(5일) 수익 = 2.0*fac1 정확(종목별 상수 성장률). 회귀가 β=2.0 복원."""
    n = 80
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    f1 = {"AAA": -0.02, "BBB": -0.01, "CCC": 0.0, "DDD": 0.01, "EEE": 0.02, "FFF": 0.03}
    ds = {}
    for s, fv in f1.items():
        r = (1.0 + 2.0 * fv) ** (1.0 / 5.0) - 1.0      # (1+r)^5-1 = 2*fac1
        close = 100.0 * (1.0 + r) ** np.arange(n)
        ds[s] = pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                              "Close": close, "Volume": 1e6, "fac1": np.full(n, fv)}, index=idx)
    return ds, list(f1)


def test_regression_recovers_known_coef():
    ds, syms = _reg_fixture()
    s = _reg_ir(syms, [data("__SELF__.fac1")], windows=[5])
    res = run_query(s, ds)
    assert res["success"] and res["relation"] == "regression"
    fac = res["by_window"]["5"]["factors"][0]
    assert "fac1" in fac["name"]
    assert abs(fac["coef"] - 2.0) < 1e-6        # forward=2*fac1 복원
    # 정확 관계라 per-date 계수 분산 0 → t 무한대(완전 유의) 표기
    assert fac["t_inf"] is True or (fac["t_stat"] is not None and fac["t_stat"] > 100)


def test_regression_matches_statsmodels():
    """엔진 계수를 statsmodels OLS(단일 단면)와 대조 — lstsq=OLS 확인."""
    import statsmodels.api as sm
    ds, syms = _reg_fixture()
    s = _reg_ir(syms, [data("__SELF__.fac1")], windows=[5])
    res = run_query(s, ds)
    eng_coef = res["by_window"]["5"]["factors"][0]["coef"]
    # 한 날짜 단면으로 statsmodels OLS 직접
    fac = np.array([ds[c]["fac1"].iloc[0] for c in syms])
    close = pd.DataFrame({c: ds[c]["Close"] for c in syms})
    fwd = (close.shift(-5) / close - 1.0).iloc[0].to_numpy()
    X = sm.add_constant(fac)
    beta = sm.OLS(fwd, X).fit().params[1]
    assert abs(eng_coef - beta) < 1e-6
```

- [ ] **Step 2: 실패 확인** — `cd platform && pytest core/tests/test_question_regression_p5.py -v` → FAIL(`_fama_macbeth`·`_run_regression_study` 미정의).

- [ ] **Step 3: run_query 라우팅 + 구현**

`run.py` `run_query` relate 분기(현 :121-123)를 교체:
```python
    if q == "relate":
        st = strategy.study
        if st.event is not None:
            return _run_event_study(strategy, dataset)
        if st.relation_kind == "regression":
            return _run_regression_study(strategy, dataset)
        return _run_ic_study(strategy, dataset)
```

`run.py` `_run_ic_study` **직후**(현 :377 다음)에 추가:
```python
# ── 다중팩터 횡단 회귀 (RELATE 심화 — Fama-MacBeth) ────────────────────────────

def _fama_macbeth(betas: np.ndarray):
    """per-date 계수 (T기간×K팩터) → Fama-MacBeth 집계 (평균·표준오차·t·95% CI).

    날짜별 횡단 회귀 계수의 시계열을 평균하고, 그 시계열 분산으로 t값을 낸다(횡단 상관에
    강건한 표준 방법). T=1이면 se=0. 정확관계(분산 0)면 t는 무한대(완전 유의)로 표기.
    """
    mean = betas.mean(axis=0)
    T = betas.shape[0]
    sd = betas.std(axis=0, ddof=1) if T > 1 else np.zeros(betas.shape[1])
    se = sd / np.sqrt(T)
    with np.errstate(divide="ignore", invalid="ignore"):
        t = np.where(se > 0, mean / se,
                     np.where(mean != 0, np.inf * np.sign(mean), 0.0))
    return mean, se, t, mean - 1.96 * se, mean + 1.96 * se


def _run_regression_study(strategy: StrategyIR, dataset: dict) -> dict:
    """다중 설명변수의 forward수익 횡단 예측력 — Fama-MacBeth 계수·t값·신뢰구간.

    "밸류·모멘텀·퀄리티 중 무엇이 다음 달 수익을 횡단으로 설명하나(다중 통제 후)"에 답한다.
    날짜별 횡단 OLS(절편+팩터들, numpy lstsq=OLS) → per-date 계수 → Fama-MacBeth 집계.
    forward수익은 미래참조라 분석 전용(IC 스터디와 동일 규약). 단일팩터=IC, 다중·통제=이것.
    """
    from ..blocks.node import referenced_columns
    syms = _universe_symbols(strategy, dataset)
    if len(syms) < 2:
        return _empty("횡단 회귀는 종목이 2개 이상이어야 합니다.")
    nodes = list(strategy.study.factors)
    if not nodes:
        return _empty("다중 회귀는 설명변수(factors)가 1개 이상 필요합니다.")
    windows = strategy.study.windows or [21]
    ctx = EvalContext.from_dataset(_scoped(dataset, syms, *nodes))
    panels = [evaluate(n, ctx) for n in nodes]
    if any(not isinstance(p, pd.DataFrame) for p in panels):
        return _empty("설명변수가 패널(종목×날짜)을 산출하지 않습니다.")
    idx = panels[0].index
    close = pd.DataFrame({s: dataset[s]["Close"] for s in syms
                          if s in dataset and "Close" in dataset[s].columns}).reindex(idx)
    K = len(nodes)
    names = []
    for i, n in enumerate(nodes):
        cols = sorted(referenced_columns(n))
        names.append(cols[0] if cols else f"f{i}")

    by_window: dict = {}
    for w in windows:
        fwd = close.shift(-int(w)) / close - 1.0
        betas = []
        for d in idx:
            yv = fwd.loc[d].to_numpy(dtype=float)
            Xcols = [panels[k].loc[d].reindex(close.columns).to_numpy(dtype=float)
                     for k in range(K)]
            X = np.column_stack(Xcols)
            mask = np.isfinite(yv) & np.isfinite(X).all(axis=1)
            if int(mask.sum()) < K + 2:        # 자유도 확보
                continue
            Xd = np.column_stack([np.ones(int(mask.sum())), X[mask]])   # 절편 + 팩터
            try:
                coef, *_ = np.linalg.lstsq(Xd, yv[mask], rcond=None)
            except Exception:                  # noqa: BLE001 — 특이행렬 등
                continue
            betas.append(coef[1:])             # 절편 제외 팩터 계수
        if len(betas) < 2:
            by_window[str(w)] = {"n_periods": len(betas), "factors": None,
                                 "note": "회귀 가능한 기간이 부족합니다(종목·결측 확인)."}
            continue
        mean, se, t, lo, hi = _fama_macbeth(np.array(betas))
        by_window[str(w)] = {
            "n_periods": int(len(betas)),
            "factors": [{"name": names[k], "coef": float(mean[k]), "se": float(se[k]),
                         "t_stat": (float(t[k]) if np.isfinite(t[k]) else None),
                         "t_inf": bool(not np.isfinite(t[k])),
                         "ci_low": float(lo[k]), "ci_high": float(hi[k])} for k in range(K)],
        }
    return {"success": True, "axis": "relation", "relation": "regression",
            "windows": [str(w) for w in windows], "factor_names": names, "by_window": by_window}
```

- [ ] **Step 4: 통과 확인** — `cd platform && pytest core/tests/test_question_regression_p5.py -v` → 전건 PASS(math·복원·statsmodels 대조).

- [ ] **Step 5: Commit 없음** — 보고.

---

### Task 3: capability_spec — regression 노출

**Files:** Modify `core/quant_core/ir_engine/capabilities.py`; Test `core/tests/test_capability_coverage.py`(기존)

- [ ] **Step 1: study_relation_kind에 regression + factors 노트**

`capabilities.py` `study_relation_kind`(현 :202-204)에 추가:
```python
            {"value": "regression", "does": "다중 설명변수(factors)의 forward수익 횡단 회귀 — Fama-MacBeth 계수·t값·95% 신뢰구간",
             "use_for": "'여러 팩터 중 무엇이 수익을 설명하나(상호 통제 후)'. study.factors=[score 블록들]·windows·universe 2+종목. 단일=ic."},
```
그 다음에 factors 노트:
```python
        "regression_factors": {
            "field": "study.factors",
            "does": "relation_kind=regression의 설명변수 목록(각 score/condition 블록). 날짜별 횡단 OLS로 동시 통제.",
            "use_for": "다중팩터 회귀. 예: [pb_ratio, momentum, ...]. IC(target_node 단일)와 구분.",
        },
```

- [ ] **Step 2: 커버리지 가드** — `cd platform && pytest core/tests/test_capability_coverage.py -v` → PASS(regression 노출).

- [ ] **Step 3: Commit 없음** — 보고.

---

### Task 4: NL 컴파일러 — 회귀 idiom + archetype

**Files:** Modify `server/app/ir_compiler.py`, `server/evals/compile_archetypes.py`

- [ ] **Step 1: study 스키마 문구**

`ir_compiler.py:165` study 스키마의 relate 부분에 `"relation_kind":"ic|regression", "factors":[<블록>,..]` 노출(현재 relation_kind 미표기면 추가).

- [ ] **Step 2: idiom #11 추가**

`<idioms>` 끝(`</idioms>` 직전):
```python
11. [다중팩터 횡단 회귀] "밸류·모멘텀·퀄리티 중 무엇이 forward 수익을 설명하나(상호 통제)"·"여러 지표로
    수익 횡단 회귀"처럼 *여러 설명변수의 동시 예측력*이면 → query="relate" + study.relation_kind="regression"
    + study.factors=[팩터1, 팩터2, ...](각 score 블록) + study.windows. universe.kind=all/list(종목 2+).
    Fama-MacBeth(날짜별 횡단 OLS→계수 시계열 평균+t값/신뢰구간). (※ 단일팩터 예측력=relation_kind="ic"+target_node.)
```

- [ ] **Step 3: archetype 1종**

`compile_archetypes.py` `CASES`에:
```python
    ("다중팩터 회귀(relate)",
     "PBR과 12개월 모멘텀이 forward 수익을 설명하는지 다중 횡단 회귀로 보여줘. 코스피 종목.",
     lambda ir: ir.get("query") == "relate"
                and ir.get("study", {}).get("relation_kind") == "regression"
                and len(ir.get("study", {}).get("factors", [])) >= 1),
```

- [ ] **Step 4: import 스모크** — `cd platform && python -c "import sys; sys.path.insert(0,'server'); sys.path.insert(0,'core'); import app.ir_compiler; import evals.compile_archetypes as m; print('ok', len(m.CASES))"` → `ok N`.

- [ ] **Step 5: Commit 없음** — 보고.

---

### Task 5: 웹 타입

**Files:** Modify `web/src/types.ts`

- [ ] **Step 1: relation_kind regression + factors**

`types.ts` IrStrategyDef.study의 `relation_kind`(현 :297)에 `"regression"` 추가:
```typescript
    relation_kind?: "ic" | "regression";       // relate — IC(단일) 또는 다중팩터 회귀
    factors?: IrNode[];                         // relation_kind=regression 설명변수
```
IrExtremizeResult 다음에 결과 타입:
```typescript
// query="relate" + relation_kind="regression" — 다중팩터 Fama-MacBeth 회귀 결과.
export interface IrRegressionResult {
  success: boolean; axis: "relation"; relation: "regression";
  windows: string[]; factor_names: string[];
  by_window: Record<string, {
    n_periods: number;
    factors: { name: string; coef: number; se: number; t_stat: number | null;
               t_inf: boolean; ci_low: number; ci_high: number }[] | null;
    note?: string;
  }>;
}
```

- [ ] **Step 2: 웹 빌드** — `cd web && npm run build` → 타입에러 0.

- [ ] **Step 3: Commit 없음** — 보고.

---

### Task 6: 전체 회귀 검증 (게이트)

- [ ] **Step 1: P5 골든** — `cd platform && pytest core/tests/test_question_regression_p5.py -v` → 전건 PASS(math·복원·statsmodels 대조·검증).
- [ ] **Step 2: 기존 relate·capability·migration 무회귀** — `cd platform && pytest tests/test_engine_event_study.py core/tests/test_comparison_e2e.py core/tests/test_capability_coverage.py core/tests/test_question_migration.py -v` → 전건 PASS.
- [ ] **Step 3: 골든 백테스트 무변경** — `cd platform && pytest tests/test_backtest_golden.py -v` → 14 PASS, GOLDEN 무변경.
- [ ] **Step 4: 전체 스위트** — `cd platform && pytest core/tests tests server/tests -q` → 새 실패 0(P4 후 615 기준 + P5 신규만 증가). 실패 시 근본원인 조사·보고.
- [ ] **Step 5: 보고** — passed/failed 카운트, 골든 무변경, 미해결 위험.

---

## Self-Review

**Spec coverage:** P5 "다중팩터·횡단 회귀+신뢰구간/t값" → Task 1·2. 게이트 "statsmodels 대조" → test_regression_matches_statsmodels(Task 2). NL V/T/E → Task 3(V)·4(T·E). 4계층 → UI=NL(4)+웹(5), 엔진=_run_regression_study(2), 데이터 독립, 자동매매 N/A. ✅

**Placeholder scan:** 모든 코드 스텝 실제 코드. archetype 구조 단언만(라이브=API키 홀드). 없음.

**Type consistency:** `Study.relation_kind`(ic/regression)·`factors: list[Node]` ↔ capability study_relation_kind·regression_factors ↔ web relation_kind/factors ↔ NL idiom 일치. `_run_regression_study` 결과 dict(axis/relation/windows/factor_names/by_window{n_periods,factors[{name,coef,se,t_stat,t_inf,ci_low,ci_high}]}) ↔ 골든 단언(Task 2) ↔ web IrRegressionResult 일치. `_fama_macbeth` 시그니처 (betas)→(mean,se,t,lo,hi) 일관.

**검증 가능성:** _fama_macbeth 손계산(mean=[2.0,0.0]·t[0]큼·t[1]≈0·CI 포함). e2e 복원(forward=2*fac1 정확 구성→coef=2.0·t_inf). statsmodels OLS 단면 대조(lstsq=OLS 1e-6). 골든14 무변경=불변식. mask<K+2 자유도·특이행렬 try/except·n_periods<2 graceful.

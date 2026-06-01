# 세부조건(스크리너) 팝업 이전 + 유니버스 tradable 전용 — 구현 Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 스크리너를 종목추가 팝업 내 "세부조건 설정"으로 옮겨 선택 종목을 동적/정적으로 2차 필터링하고, 유니버스를 매수/매도 가능 종목으로 제한한다.

**Architecture:** 스크리너를 배타적 `universe.kind` 에서 직교 선택 필드 `universe.screener`(condition+refresh)로 전환. 엔진은 `kind` 게이트 대신 `screener` 존재 게이트로 정기·이벤트 백테스트 경로 모두에 자격 마스크를 적용(refresh로 동적/정적 분기). 유니버스 피커는 tradable 전용, 서버는 비매매 유니버스·이벤트+세부조건의 모의/실전을 거부.

**Tech Stack:** Python(pydantic, pandas) core/server(FastAPI) · React+TypeScript(Vite) web · pytest · bun.

**핵심 순서 원칙:** 엔진/검증 게이트를 `kind=="screener"` → `screener 존재` 로 바꾸는 것은 **하위호환**(기존 full-market screener 전략은 screener.condition을 갖고 `_universe_symbols`가 전체 유니버스를 반환하므로 그대로 동작). 따라서 enum 값 `"screener"` 제거는 **맨 마지막**(Part 8)에, DB 사전 조회(Part 0) 통과 후 수행. 각 커밋은 전체 테스트 그린 유지.

**실행 명령 참조:**
- 코어 테스트: `cd platform && pytest tests/<file>.py -v`
- 서버 테스트: `cd platform && pytest server/tests/<file>.py -v`
- 웹 타입체크/빌드: `cd platform/web && bun run build`
- 웹 dev: `cd platform/web && bun run dev`

---

## Part 0 — 마이그레이션 사전 조회 (코드 변경 없음, 선결)

### Task 0: 기존 `kind="screener"` 전략 DB 조회

**Files:** 없음(조회만). 서버 DB는 Railway Postgres.

- [ ] **Step 1: DB에서 kind=screener 전략 조회**

서버 셸 또는 psql로:
```sql
SELECT id, user_id, name, run_mode,
       definition->'universe'->>'kind' AS kind
FROM strategy
WHERE definition->'universe'->>'kind' = 'screener';
```
(로컬 검증용 SQLite/세션이면 동일 쿼리. Railway는 `railway run psql $DATABASE_URL -c "..."`.)

- [ ] **Step 2: 결과 판정**

- 0건 또는 테스트용(run_mode='draft', 본인 계정) → 진행.
- 실사용(paper/live, 타 사용자) 발견 → **중단하고 사용자에게 보고**. 본 plan은 "깨끗한 제거" 전제이므로 재합의 필요.

- [ ] **Step 3: 결과 기록**

사용자에게 "kind=screener 전략 N건(상세)" 보고하고 제거 진행 승인 확인. 커밋 없음.

---

## Part 1 — 코어 엔진: refresh 헬퍼 + 정기 경로 게이트

### Task 1: `_apply_refresh` 헬퍼 (동적/정적)

**Files:**
- Modify: `core/quant_core/ir_engine/engine.py` (`_screener_mask` 아래, 약 483행)
- Test: `tests/test_screener.py`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_screener.py` 끝(검증 게이트 위)에 추가:

```python
from quant_core.ir_engine.engine import _apply_refresh  # noqa: E402  (상단 import 블록에 추가)


def test_apply_refresh_dynamic_passthrough():
    """each_rebalance — 마스크 그대로."""
    ctx = EvalContext.from_dataset(_ds())
    m = _screener_mask({"condition": _rank_cond("market_cap", 2)}, ctx, ["A", "B", "C", "D"])
    out = _apply_refresh(m, "each_rebalance", None)
    assert out.equals(m)


def test_apply_refresh_static_freezes_first_row():
    """once_at_start — 시작시점(첫 행) 자격을 전 기간 고정. 시점 자격이 뒤집혀도 불변."""
    ctx = EvalContext.from_dataset(_ds())
    m = _screener_mask({"condition": _rank_cond("market_cap", 2)}, ctx, ["A", "B", "C", "D"])
    out = _apply_refresh(m, "once_at_start", None)
    first = [c for c in m.columns if m.iloc[0][c]]              # 초기 대형 A,B
    assert first == ["A", "B"]
    # 모든 행이 첫 행과 동일(바스켓 고정)
    assert all(list(out.iloc[k][out.columns][out.iloc[k]].index) == first
               for k in range(len(out.index)))
    # 동적과 달리 마지막 행이 C,D로 뒤집히지 않음
    assert [c for c in out.columns if out.iloc[-1][c]] == first
```

- [ ] **Step 2: 실패 확인**

Run: `cd platform && pytest tests/test_screener.py::test_apply_refresh_static_freezes_first_row -v`
Expected: FAIL — `ImportError: cannot import name '_apply_refresh'`

- [ ] **Step 3: 구현** — `engine.py` `_screener_mask` 함수 바로 아래 추가:

```python
def _apply_refresh(mask: pd.DataFrame, refresh: str, start) -> pd.DataFrame:
    """자격 마스크에 재선별 시점 적용.

    each_rebalance(동적): 마스크 그대로 — 매 시점 PIT 자격.
    once_at_start(정적): 백테스트 시작(sim.start 이후 첫 행)의 자격 행을 전 기간으로
    broadcast → 후보 바스켓 고정. 시작일까지의 데이터만 쓰므로 lookahead 없음. 시작일
    충족 0개면 빈 바스켓(거래 없음).
    """
    if refresh != "once_at_start":
        return mask
    rows = mask[mask.index >= pd.Timestamp(start)] if start is not None else mask
    if rows.empty:
        rows = mask
    basket = rows.iloc[0]                      # 형성일 자격 (cols, bool)
    return pd.DataFrame(
        np.tile(basket.to_numpy(dtype=bool), (len(mask.index), 1)),
        index=mask.index, columns=mask.columns)
```

(`np` 는 engine.py 상단에 이미 import됨.)

- [ ] **Step 4: 통과 확인**

Run: `cd platform && pytest tests/test_screener.py -k apply_refresh -v`
Expected: PASS (2 tests)

- [ ] **Step 5: 커밋**

```bash
git add core/quant_core/ir_engine/engine.py tests/test_screener.py
git commit -m "feat(engine): _apply_refresh — 세부조건 동적/정적 재선별 시점"
```

### Task 2: 정기/상시 경로 게이트 일반화 + refresh 적용

**Files:**
- Modify: `core/quant_core/ir_engine/engine.py:629-650` (`_run_scheduled`)
- Test: `tests/test_screener.py`

- [ ] **Step 1: 실패 테스트** — 선택 종목 한정(list+screener) 정기 동적/정적:

```python
def _spec_list(condition, refresh="each_rebalance"):
    """list 유니버스(A..D) + 세부조건 — 선택 종목 ∩ 조건."""
    return {"signal": {"op": "data", "params": {"ref": "momentum_12_1m"}},
            "universe": {"kind": "list", "symbols": ["A", "B", "C", "D"],
                         "screener": {"condition": condition, "refresh": refresh}},
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 4}},
            "simulation": {"initial_capital": 1e7}}


def test_list_screener_dynamic_runs():
    res = strategy_from_spec(_spec_list(_rank_cond("market_cap", 2)), _ds())
    assert res["success"], res
    assert len(res["equity"]) > 0


def test_list_screener_static_runs():
    res = strategy_from_spec(_spec_list(_rank_cond("market_cap", 2), "once_at_start"), _ds())
    assert res["success"], res
```

- [ ] **Step 2: 실패 확인**

Run: `cd platform && pytest tests/test_screener.py -k list_screener -v`
Expected: FAIL — 현재 `kind=="list"` 이면 screener 미적용이라 게이트가 안 걸림(실행은 되나 의도와 다름). 우선 두 테스트가 success여야 하며, 동적/정적 차이 검증은 Task 후속. (실행 자체가 성공하면 이 단계는 통과로 간주하고 Step 3에서 게이트를 실제 적용.)

> 참고: 이 두 테스트는 "실행 성공"만 본다. 동적 vs 정적 **동작 차이**는 `_apply_refresh` 단위 테스트(Task 1)와 이벤트 테스트(Task 3)가 커버.

- [ ] **Step 3: 구현** — `engine.py` `_run_scheduled` 수정.

기존(629-631):
```python
    screener = strategy.universe.screener or {}
    filt_node = (Node.model_validate(screener["condition"])
                 if strategy.universe.kind == "screener" and screener.get("condition") else None)
```
변경:
```python
    screener = strategy.universe.screener or {}
    filt_node = (Node.model_validate(screener["condition"])
                 if screener.get("condition") else None)
```

기존(648-650):
```python
    if strategy.universe.kind == "screener":
        elig = _screener_mask(screener, ctx, cols)
        alpha = alpha.where(elig.reindex(index=alpha.index, columns=cols).fillna(False))
```
변경:
```python
    if screener.get("condition"):
        elig = _apply_refresh(_screener_mask(screener, ctx, cols),
                              screener.get("refresh", "each_rebalance"), sim.start)
        alpha = alpha.where(elig.reindex(index=alpha.index, columns=cols).fillna(False))
```

(`sim` 은 `_run_scheduled` 에서 `strategy.simulation` 으로 이미 바인딩됨 — 616행 `ent, sim, sz = ...`.)

- [ ] **Step 4: 통과 확인**

Run: `cd platform && pytest tests/test_screener.py -v`
Expected: PASS (신규 list_screener 2건 + 기존 screener 전부 — 기존 kind="screener"는 screener.condition을 가져 게이트 동일 통과)

- [ ] **Step 5: 커밋**

```bash
git add core/quant_core/ir_engine/engine.py tests/test_screener.py
git commit -m "feat(engine): 정기 경로 세부조건을 screener-존재 게이트로 일반화(선택종목 ∩ 조건)"
```

---

## Part 2 — 코어 엔진: 이벤트 경로 자격 마스크

### Task 3: 이벤트(on_signal) 진입에 자격 마스크 게이트

**Files:**
- Modify: `core/quant_core/ir_engine/engine.py` (`run_unified` — 187행 `_scoped`, 226-242 arrays, 355-369 진입검사)
- Test: `tests/test_engine_unified.py` (또는 test_screener.py)

- [ ] **Step 1: 실패 테스트** — `tests/test_screener.py` 에 추가:

```python
def test_event_screener_gates_entry():
    """이벤트 진입 + 세부조건: 자격 False인 종목/날은 신호가 참이어도 진입 차단.

    신호=항상참(momentum>0). 세부조건=시총 상위 2(count). 초기엔 A,B만 자격 →
    C,D는 신호 참이어도 미보유. 후기엔 C,D 자격(동적). trades에 자격 종목만 등장.
    """
    spec = {"signal": {"op": "compare", "params": {"op": ">"},
                       "inputs": {"left": _data("momentum_12_1m"), "right": _const(0)}},
            "universe": {"kind": "list", "symbols": ["A", "B", "C", "D"],
                         "screener": {"condition": _rank_cond("market_cap", 2),
                                      "refresh": "each_rebalance"}},
            "position": {"direction": "long", "sizing": {"mode": "fixed_amount", "amount_krw": 1e6},
                         "entry": {"mode": "on_signal"},
                         "exit": {"hold_days": 5}},
            "simulation": {"initial_capital": 1e7, "fill": "close"}}
    res = strategy_from_spec(spec, _ds())
    assert res["success"], res
    # 첫 보름(초기 대형 A·B 자격 구간)의 진입 종목엔 C·D 없음
    early = [t for t in res["trades"] if t["진입일"] <= pd.Timestamp("2021-02-01")]
    assert early and all(t["종목"] in ("A", "B") for t in early), early
```

- [ ] **Step 2: 실패 확인**

Run: `cd platform && pytest tests/test_screener.py::test_event_screener_gates_entry -v`
Expected: FAIL — 현재 이벤트 경로는 세부조건 미적용이라 C·D도 진입(early에 C/D 포함).

- [ ] **Step 3: 구현** — `run_unified` 3곳 수정.

(a) `_scoped` 에 screener 노드 추가 (기존 187행):
```python
    ds = _scoped(dataset, syms, strategy.signal, exits.condition)
```
변경:
```python
    _screener = u.screener or {}
    _filt = (Node.model_validate(_screener["condition"]) if _screener.get("condition") else None)
    ds = _scoped(dataset, syms, strategy.signal, exits.condition, _filt)
```

(b) 자격 배열 계산 (arrays 루프 뒤, 약 242행 직후 추가):
```python
    elig_arrs: dict[str, np.ndarray] | None = None
    if _screener.get("condition"):
        elig_mask = _apply_refresh(_screener_mask(_screener, ctx, syms),
                                   _screener.get("refresh", "each_rebalance"), sim.start)
        elig_arrs = {sym: _sym_bool(elig_mask, sym, master_idx) for sym in syms}
```
(`ctx`, `master_idx`, `sim`, `_sym_bool` 모두 이 스코프에 존재. `_screener_mask`·`_apply_refresh` 는 같은 모듈.)

(c) 진입검사 게이트 (355-369). defer 분기:
```python
                if buy_arrs[sym][i]:
                    pending_buys.append(sym)
```
변경:
```python
                if buy_arrs[sym][i] and (elig_arrs is None or elig_arrs[sym][i]):
                    pending_buys.append(sym)
```
비-defer 분기(369행):
```python
                if not buy_arrs[sym][i] or np.isnan(aligned[sym]["close"][i]):
                    continue
```
변경:
```python
                if (not buy_arrs[sym][i]
                        or (elig_arrs is not None and not elig_arrs[sym][i])
                        or np.isnan(aligned[sym]["close"][i])):
                    continue
```

- [ ] **Step 4: 통과 확인**

Run: `cd platform && pytest tests/test_screener.py -v`
Expected: PASS (event 포함 전체)

- [ ] **Step 5: 회귀 확인**

Run: `cd platform && pytest tests/test_engine_unified.py tests/test_backtest_golden.py -v`
Expected: PASS — 세부조건 없는 전략은 `elig_arrs is None` 이라 무해.

- [ ] **Step 6: 커밋**

```bash
git add core/quant_core/ir_engine/engine.py tests/test_screener.py
git commit -m "feat(engine): 이벤트 진입에 세부조건 자격 마스크 게이트(모든 진입방식 일관)"
```

---

## Part 3 — 코어 검증 (spec.py · run.py)

### Task 4: 검증 게이트 일반화 + symbols 필수 + 이벤트 허용 + refresh enum

**Files:**
- Modify: `core/quant_core/ir_engine/spec.py:336-362`
- Modify: `core/quant_core/ir_engine/run.py:74-77`
- Test: `tests/test_screener.py`, `tests/test_engine_spec.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_screener.py` 검증 섹션에 추가:

```python
def test_screener_requires_symbols():
    """세부조건 있는데 종목 미선택(빈 symbols) → 에러."""
    spec = {"signal": {"op": "data", "params": {"ref": "momentum_12_1m"}},
            "universe": {"kind": "all",
                         "screener": {"condition": _rank_cond("market_cap", 2)}},
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 2}},
            "simulation": {"initial_capital": 1e7}}
    res = strategy_from_spec(spec, _ds())
    assert not res["success"]
    assert any(i["rule"] == "S-univ" for i in res["issues"])


def test_event_with_screener_allowed_in_backtest():
    """이벤트 진입 + 세부조건은 백테스트 허용(라이브 차단은 서버 가드 담당)."""
    spec = {"signal": {"op": "compare", "params": {"op": ">"},
                       "inputs": {"left": _data("momentum_12_1m"), "right": _const(0)}},
            "universe": {"kind": "list", "symbols": ["A", "B", "C", "D"],
                         "screener": {"condition": _rank_cond("market_cap", 2)}},
            "position": {"direction": "long", "sizing": {"mode": "fixed_amount", "amount_krw": 1e6},
                         "entry": {"mode": "on_signal"}, "exit": {"hold_days": 5}},
            "simulation": {"initial_capital": 1e7}}
    res = strategy_from_spec(spec, _ds())
    assert res["success"], res
```

- [ ] **Step 2: 실패 확인**

Run: `cd platform && pytest tests/test_screener.py -k "requires_symbols or event_with_screener" -v`
Expected: FAIL — requires_symbols는 현재 kind="all"이면 screener 검증 안 거쳐 통과해버림; event_with_screener는 미정의 단계.

- [ ] **Step 3: 구현** — `spec.py`.

(a) 336행 이벤트 제약에서 screener 제거:
```python
    if ent.mode == "on_signal" and u.kind in ("all", "screener"):
        issues.append(Issue("S-univ", SEV_ERROR,
                            "전체·스크리너 유니버스는 정기리밸런싱(scheduled)·상시(always) 진입과 함께 쓰세요.",
                            "universe"))
```
변경:
```python
    if ent.mode == "on_signal" and u.kind == "all":
        issues.append(Issue("S-univ", SEV_ERROR,
                            "전체 종목 유니버스는 정기리밸런싱(scheduled)·상시(always) 진입과 함께 쓰세요.",
                            "universe"))
```

(b) 340행 screener 검증 블록 게이트 + symbols 필수. 기존:
```python
    if u.kind == "screener":
        # 스크리너 = 단일 선별 조건(condition). ...
        sc = u.screener or {}
        cond = sc.get("condition")
        if not cond:
            issues.append(Issue("S-univ", SEV_ERROR,
                                "스크리너는 선별 조건(condition)이 필요합니다.", "universe"))
        else:
            ...
```
변경(헤더 라인과 게이트만; 내부 검증 로직은 유지):
```python
    sc = u.screener or {}
    cond = sc.get("condition")
    if cond:
        # 세부조건 = 선택 종목에 얹는 자격 필터. 필터·횡단순위를 AND/OR로 조합한 단일 condition.
        if not u.symbols:
            issues.append(Issue("S-univ", SEV_ERROR,
                                "세부조건은 선택한 종목이 있을 때만 설정할 수 있습니다.", "universe"))
        ref = sc.get("refresh", "each_rebalance")
        if ref not in ("each_rebalance", "once_at_start"):
            issues.append(Issue("S-univ", SEV_ERROR,
                                "세부조건 재선별 시점이 올바르지 않습니다.", "universe"))
        try:
            cnode = Node.model_validate(cond)
        except Exception:                       # noqa: BLE001
            issues.append(Issue("S-univ", SEV_ERROR, "세부조건이 유효한 블록이 아닙니다.", "universe"))
        else:
            issues += list(validate(cnode, valid_refs))
            issues += meaningfulness_issues(cnode, "universe.condition")
            if signal_out_type(cnode) != "condition":
                issues.append(Issue("S-univ", SEV_ERROR,
                                    "세부조건은 condition(참/거짓) 블록이어야 합니다 "
                                    "(예: 횡단순위(시총)≤50, 거래대금>임계).", "universe"))
            if not has_market_source(cnode):
                issues.append(Issue("M-const", SEV_ERROR,
                                    "세부조건이 시장 데이터를 참조하지 않습니다.", "universe"))
```

(c) `run.py:74` `_root_type_error` 게이트. 기존:
```python
    if u.kind == "screener" and (u.screener or {}).get("condition") is not None:
        ft = _out_type(Node.model_validate(u.screener["condition"]))
```
변경:
```python
    if (u.screener or {}).get("condition") is not None:
        ft = _out_type(Node.model_validate(u.screener["condition"]))
```

- [ ] **Step 4: 통과 확인**

Run: `cd platform && pytest tests/test_screener.py tests/test_engine_spec.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add core/quant_core/ir_engine/spec.py core/quant_core/ir_engine/run.py tests/test_screener.py
git commit -m "feat(spec): 세부조건 검증 일반화 — symbols 필수·이벤트 허용·refresh enum"
```

---

## Part 4 — 코어 데이터 레이어 (deps · gate · 데이터 윈도우)

### Task 5: screener 데이터 의존·윈도우·생존편향 게이트 일반화

**Files:**
- Modify: `core/quant_core/ir_engine/spec.py:483, 514` (needed_symbols / needed_columns)
- Modify: `core/quant_core/data/deps.py:55`
- Modify: `core/quant_core/data/gate.py:84`
- Test: `tests/test_compute_columns.py` 또는 `tests/test_dataset_scope.py` (데이터 윈도우), 신규 단언

- [ ] **Step 1: 실패 테스트** — `tests/test_screener.py` 에 데이터 윈도우 단언 추가:

```python
def test_list_screener_window_not_full():
    """list+세부조건은 전체 로드(None) 아님 — 선택 종목 + 조건 참조만."""
    from quant_core.ir_engine.spec import StrategyIR, needed_symbols
    s = StrategyIR.model_validate(_spec_list(_rank_cond("market_cap", 2)))
    win = needed_symbols(s)
    assert win is not None
    assert {"A", "B", "C", "D"}.issubset(win)
```

- [ ] **Step 2: 실패 확인**

Run: `cd platform && pytest tests/test_screener.py::test_list_screener_window_not_full -v`
Expected: PASS 또는 FAIL — `kind="list"` 라 현재도 None이 아님(symbols 경로). 단, **screener 조건이 외부 종목을 참조하는 경우** 누락될 수 있음. 아래 구현으로 조건 참조까지 포함시켜 견고화. (이 테스트가 이미 통과하면 외부참조 케이스 테스트로 강화.)

- [ ] **Step 3: 구현**.

(a) `spec.py` `needed_symbols` (483행):
```python
    if s.universe.kind in ("all", "screener"):
        return None
```
변경:
```python
    if s.universe.kind == "all":
        return None
```
그리고 같은 함수 nodes 목록에 screener 조건 참조 포함 — `nodes = [...]` 다음에:
```python
    sc = (s.universe.screener or {}).get("condition")
    if sc is not None:
        try:
            nodes.append(Node.model_validate(sc))
        except Exception:                      # noqa: BLE001
            return None                        # 잘못된 트리면 안전하게 전체
```

(b) `spec.py` `needed_columns` (514행):
```python
    if s.universe.kind == "screener" and s.universe.screener:
```
변경:
```python
    if s.universe.screener:
```

(c) `data/deps.py` (55행):
```python
    if u.kind == "screener" and u.screener:
```
변경:
```python
    if u.screener:
```

(d) `data/gate.py` (84행) 생존편향 게이트:
```python
    if u.kind in ("all", "screener") and not manifest.has_membership_history:
```
변경:
```python
    if u.kind == "all" and not manifest.has_membership_history:
```
(세부조건은 이제 고정 선택 종목 위에 동작 → 멤버십-이력 생존편향 대상 아님.)

- [ ] **Step 4: 통과 확인**

Run: `cd platform && pytest tests/test_screener.py tests/test_compute_columns.py tests/test_dataset_scope.py tests/test_data_layer.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add core/quant_core/ir_engine/spec.py core/quant_core/data/deps.py core/quant_core/data/gate.py tests/test_screener.py
git commit -m "feat(data): 세부조건 데이터 의존·윈도우·생존편향 게이트를 screener-존재로 일반화"
```

---

## Part 5 — 코어 능력기술 / NL 컴파일러

### Task 6: capabilities universe_kind 갱신 + NL 쿡북 정리

**Files:**
- Modify: `core/quant_core/ir_engine/capabilities.py:16-32`
- Modify: NL 컴파일러 쿡북/관용구 파일(아래 Step 1에서 위치 확인)
- Test: `tests/test_idiom_recipes.py`

- [ ] **Step 1: NL 쿡북에서 kind:screener 사용처 확인**

Run: `cd platform && grep -rn "\"kind\": \"screener\"\|kind: screener\|screener" core/quant_core --include=*.py -l`
그리고 NL 컴파일러 관용구/프롬프트 파일 검색:
Run: `cd platform && grep -rln "screener" core server | grep -iv test`
→ 산출 예시(IR 정의)에 `kind:"screener"` 가 있으면 그 파일을 대상에 포함.

- [ ] **Step 2: 실패 테스트** — `tests/test_engine_spec.py` 또는 capabilities 테스트에 추가:

```python
def test_capabilities_no_screener_kind():
    from quant_core.ir_engine.capabilities import describe_capabilities  # 실제 함수명 확인
    cap = describe_capabilities()
    kinds = [k["value"] for k in cap["universe_kind"]]
    assert "screener" not in kinds
    assert set(kinds) == {"single", "list", "all"}
```
(함수명은 capabilities.py 상단에서 확인 — `describe_capabilities`/`capabilities` 등. Step 3 전에 정확히.)

- [ ] **Step 3: 실패 확인**

Run: `cd platform && pytest tests/test_engine_spec.py::test_capabilities_no_screener_kind -v`
Expected: FAIL — 현재 "screener" 포함.

- [ ] **Step 4: 구현** — `capabilities.py` universe_kind(16-25)에서 screener 항목 제거:
```python
        "universe_kind": [
            {"value": "single", "does": "종목 1개",
             "use_for": "단일 자산 매매 · 레버리지 ETF 복제 · 지수 추종"},
            {"value": "list", "does": "지정한 여러 종목 바스켓",
             "use_for": "소수 종목 고정 바스켓 (세부조건으로 2차 선별 가능)"},
            {"value": "all", "does": "데이터 보유 전체 종목",
             "use_for": "전체 유니버스 팩터/포트폴리오 (scheduled·always 진입과 함께)"},
        ],
```
그리고 별도 능력 항목으로 세부조건을 기술(같은 dict 내 적절한 키, 예 `"screener"`):
```python
        "screener": {
            "field": "universe.screener",
            "does": "선택 종목에 얹는 자격 필터 — 필터+횡단순위 condition. refresh로 동적/정적.",
            "use_for": "고른 종목을 거래대금·시총·밸류 등 조건으로 2차 선별. "
                       "refresh=each_rebalance(매 리밸런싱 재선별)·once_at_start(시작시점 바스켓 고정).",
        },
```
entry_mode "scheduled" use_for(32행)의 "all/screener 유니버스와." → "all 유니버스 또는 세부조건과." 로.

NL 쿡북에 `kind:"screener"` 산출 예시가 있으면 `kind:"list"+symbols+screener` 로 교체.

- [ ] **Step 5: 통과 확인**

Run: `cd platform && pytest tests/test_engine_spec.py::test_capabilities_no_screener_kind tests/test_idiom_recipes.py -v`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add core/quant_core/ir_engine/capabilities.py tests/ <NL쿡북파일>
git commit -m "feat(nl): capabilities universe_kind에서 screener 제거, 세부조건 능력 기술"
```

---

## Part 6 — 서버 가드 (tradable + 이벤트 세부조건)

### Task 7: tradable 집합 공유 헬퍼 추출

**Files:**
- Modify: `server/app/routers/backtest.py` (`_build_symbols_payload` tradable 로직)
- Create/Modify: `server/app/symbols.py` (또는 적절한 공유 모듈) — `tradable_symbol_set()` 헬퍼
- Test: `server/tests/test_strategies_ir.py`

- [ ] **Step 1: 현재 tradable 계산 위치 확인**

`_build_symbols_payload`([backtest.py:30-99](../../../server/app/routers/backtest.py))는 `master_by_code` + index로 `tradable = in_master and has_ohlc` 계산. 이 마스터/인덱스 로딩을 재사용할 공유 함수로 추출.

- [ ] **Step 2: 실패 테스트** — `server/tests/` 에 헬퍼 단위 테스트(가능하면) 또는 Task 8의 통합 테스트로 대체. 헬퍼가 마스터 IO에 의존하면 Task 8 통합 테스트로 검증하고 본 Task는 리팩터만.

- [ ] **Step 3: 구현** — tradable 판정을 함수로 추출:
```python
# server/app/symbols.py (신규 또는 기존 심볼 모듈)
def tradable_symbols() -> set[str]:
    """매수/매도 가능 종목 코드 집합 — 마스터 존재 ∧ OHLC 보유. _build_symbols_payload와 동일 기준."""
    # _build_symbols_payload 의 master_by_code/index 로딩을 재사용해 tradable=True 인 symbol만 수집
    ...
    return {row["symbol"] for row in _build_symbols_payload()["symbols"] if row["tradable"]}
```
(가장 단순: 기존 payload를 한 번 빌드해 tradable만 추림. 빌드 비용이 크면 마스터/인덱스 로딩만 떼어 캐시. 우선 단순 버전으로.)

- [ ] **Step 4: 통과 확인** — Task 8에서 통합 검증.

- [ ] **Step 5: 커밋**

```bash
git add server/app/symbols.py server/app/routers/backtest.py
git commit -m "refactor(server): tradable 종목 집합 공유 헬퍼 추출"
```

### Task 8: `_assert_live_tradable` — 비매매 유니버스·이벤트 세부조건 차단

**Files:**
- Modify: `server/app/routers/strategies.py:52-66`
- Test: `server/tests/test_strategies_ir.py`

- [ ] **Step 1: 실패 테스트** — `server/tests/test_strategies_ir.py` 에 추가(기존 레버리지 거부 테스트 패턴 따름, 171행 참조):

```python
def _ir_def_universe(universe: dict) -> dict:
    d = json.loads(json.dumps(_IR_DEF))      # 기존 _IR_DEF 복제
    d["universe"] = universe
    return d


def test_nontradable_universe_paper_rejected(client, auth):
    # 비매매 심볼(예: 지수 코드) 유니버스 → paper 거부
    body = {"definition": _ir_def_universe({"kind": "single", "symbols": ["^KS11"]}),
            "run_mode": "paper", "engine": "ir"}
    r = client.post("/strategies", json=body, headers=auth)
    assert r.status_code == 422
    assert "매매" in r.json()["detail"] or "tradable" in r.json()["detail"].lower()


def test_event_screener_paper_rejected(client, auth):
    body = {"definition": _ir_def_universe({"kind": "list", "symbols": ["005930", "000660"],
                "screener": {"condition": {"op": "compare", "params": {"op": ">"},
                    "inputs": {"left": {"op": "data", "params": {"ref": "Volume"}},
                               "right": {"op": "const", "params": {"value": 0}}}}}}),
            "run_mode": "paper", "engine": "ir"}
    # entry.mode=on_signal 가정인 _IR_DEF; 아니면 d["position"]["entry"]["mode"]="on_signal" 설정
    r = client.post("/strategies", json=body, headers=auth)
    assert r.status_code == 422


def test_tradable_universe_paper_allowed(client, auth):
    body = {"definition": _ir_def_universe({"kind": "list", "symbols": ["005930", "000660"]}),
            "run_mode": "paper", "engine": "ir"}
    r = client.post("/strategies", json=body, headers=auth)
    assert r.status_code == 200
```
(fixture `client`/`auth` 와 `_IR_DEF` 는 기존 test_strategies_ir.py 것을 사용 — 파일 상단 확인 후 시그니처 맞춤. 비매매 예시 심볼은 실제 마스터에서 tradable=False 인 코드로.)

- [ ] **Step 2: 실패 확인**

Run: `cd platform && pytest server/tests/test_strategies_ir.py -k "nontradable or event_screener or tradable_universe" -v`
Expected: FAIL — 현재 레버리지만 검사.

- [ ] **Step 3: 구현** — `strategies.py` `_assert_live_tradable` 확장:
```python
def _assert_live_tradable(run_mode: str, definition: dict) -> None:
    """모의/실전 승격 게이트 — 백테스트≠실거래 발산을 막는다.

    ① 레버리지(>1배): 현금계좌 미체결 → 차단(기존).
    ② 비매매 유니버스: 자동매매 불가 종목(지수·매크로) 또는 빈 선택(전체)·합성전략 → 차단.
    ③ 이벤트 진입 + 세부조건: 라이브 종목선별이 로컬앱 미구현(Phase 2) → 차단.
    """
    if run_mode not in ("paper", "live"):
        return
    sim = definition.get("simulation") or {}
    if float(sim.get("leverage") or 1.0) > 1.0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
            "레버리지(>1배) 전략은 백테스트 전용입니다 — 모의·실전 적용 불가. "
            "실거래에서 레버리지가 필요하면 레버리지 ETF(예: KODEX 레버리지)를 현금 매수하세요.")

    u = definition.get("universe") or {}
    syms = u.get("symbols") or []
    if u.get("kind") == "all" or not syms:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
            "모의·실전 전략은 매매할 종목을 직접 선택해야 합니다(전체 유니버스 불가).")
    from ..symbols import tradable_symbols
    ok = tradable_symbols()
    bad = [s for s in syms if s not in ok]           # 합성전략(strat:)·지수·매크로 모두 포함
    if bad:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"자동매매 불가 종목이 포함돼 모의·실전으로 적용할 수 없습니다: {', '.join(bad[:5])}")

    if (definition.get("position") or {}).get("entry", {}).get("mode") == "on_signal" \
            and (u.get("screener") or {}).get("condition"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
            "이벤트 진입 + 세부조건 전략은 현재 백테스트 전용입니다(라이브 지원 예정).")
```

- [ ] **Step 4: 통과 확인**

Run: `cd platform && pytest server/tests/test_strategies_ir.py -v`
Expected: PASS (신규 3건 + 기존 레버리지 거부 등 전부)

- [ ] **Step 5: 커밋**

```bash
git add server/app/routers/strategies.py server/tests/test_strategies_ir.py
git commit -m "feat(server): 비매매 유니버스·이벤트 세부조건 모의/실전 차단 가드"
```

---

## Part 7 — 웹 (types · MultiSymbolPicker · IrBuilder)

### Task 9: 타입 — universe screener 직교 필드

**Files:**
- Modify: `web/src/types.ts:242-249`

- [ ] **Step 1: 구현** — `IrStrategyDef.universe` 타입 교체:
```ts
  universe: {
    kind: "single" | "list" | "all";
    symbols?: string[];
    screener?: {
      condition: IrNode;
      refresh: "each_rebalance" | "once_at_start";
    } | null;
    exclude_macro?: boolean;
  };
```

- [ ] **Step 2: 타입체크**

Run: `cd platform/web && bun run build`
Expected: IrBuilder.tsx에서 `kind==="screener"` 비교가 타입 에러 → Task 11에서 해소(예상된 빨강). 본 커밋은 타입 정의만.

- [ ] **Step 3: 커밋**

```bash
git add web/src/types.ts
git commit -m "feat(web): universe.screener를 직교 필드(condition+refresh)로, kind에서 screener 제거"
```

### Task 10: MultiSymbolPicker — 세부조건 영역 + refresh 라디오

**Files:**
- Modify: `web/src/components/MultiSymbolPicker.tsx`
- Reference: `web/src/components/SentenceTree.tsx` (조건 빌더 props)

- [ ] **Step 1: props 확장** — `MultiSymbolPicker` 시그니처에 추가:
```ts
export default function MultiSymbolPicker({ symbols, value, onChange, inline, scope = "tradable",
  strategies, screener, onScreenerChange, screenerRefresh, onScreenerRefreshChange,
  catalog, selfIndicators }: {
  // ...기존...
  /** 세부조건(선택 종목 2차 필터). 주어지면 팝오버 하단에 조건 빌더 렌더. */
  screener?: IrNode | null;
  onScreenerChange?: (n: IrNode | null) => void;
  screenerRefresh?: "each_rebalance" | "once_at_start";
  onScreenerRefreshChange?: (r: "each_rebalance" | "once_at_start") => void;
  catalog?: Catalog;
  selfIndicators?: IndicatorInfo[];
}) {
```
(필요한 타입 import: `IrNode`, `IndicatorInfo` from "../types"; `Catalog` from "./SentenceTree".)

- [ ] **Step 2: 세부조건 컴포넌트** — 팝오버 foot 위에 렌더(인라인/팝오버 공용 헬퍼):
```tsx
  const showScreener = !!onScreenerChange;
  const screenerBlock = showScreener ? (
    <ScreenerSection
      disabled={selected.length === 0}
      condition={screener ?? null}
      onCondition={onScreenerChange!}
      refresh={screenerRefresh ?? "each_rebalance"}
      onRefresh={onScreenerRefreshChange!}
      catalog={catalog!} symbols={symbols} selfIndicators={selfIndicators ?? []}
      count={selected.length}
    />
  ) : null;
```
그리고 파일 하단에 `ScreenerSection` 정의(접이식 + SentenceTree + 라디오):
```tsx
function ScreenerSection({ disabled, condition, onCondition, refresh, onRefresh,
                           catalog, symbols, selfIndicators, count }: {
  disabled: boolean; condition: IrNode | null; onCondition: (n: IrNode | null) => void;
  refresh: "each_rebalance" | "once_at_start";
  onRefresh: (r: "each_rebalance" | "once_at_start") => void;
  catalog: Catalog; symbols: SymbolInfo[]; selfIndicators: IndicatorInfo[]; count: number;
}) {
  const [open, setOpen] = useState(false);
  if (disabled) return <div className="muted small" style={{ marginTop: 8 }}>
    세부조건을 쓰려면 먼저 종목을 선택하세요.</div>;
  return (
    <div className="screener-section" style={{ marginTop: 8, borderTop: "1px solid var(--line)", paddingTop: 8 }}>
      <button type="button" className="link-btn" onClick={() => setOpen(v => !v)}>
        {open ? "▾" : "▸"} 세부조건 설정{condition ? " · 적용 중" : " (선택)"}
      </button>
      {open && (
        <div style={{ marginTop: 6 }}>
          <SentenceTree node={condition} catalog={catalog} symbols={symbols}
                        selfIndicators={selfIndicators} requiredType="condition"
                        onChange={onCondition} />
          <div style={{ marginTop: 6, fontSize: 13 }}>
            <label style={{ display: "block" }}>
              <input type="radio" checked={refresh === "each_rebalance"}
                     onChange={() => onRefresh("each_rebalance")} /> 매 리밸런싱마다 재선별 (동적)
            </label>
            <label style={{ display: "block" }}>
              <input type="radio" checked={refresh === "once_at_start"}
                     onChange={() => onRefresh("once_at_start")} /> 시작 시점에 한 번만 선별 (정적·바스켓 유지)
            </label>
          </div>
          <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
            {refresh === "each_rebalance"
              ? `선택한 ${count}개 종목 중 매 리밸런싱일에 조건을 만족하는 종목만 후보가 됩니다.`
              : "시작 시점에 조건을 만족한 종목으로 바스켓을 만들어 그대로 유지합니다."}
          </p>
        </div>
      )}
    </div>
  );
}
```
(import 추가: `SentenceTree`, `Catalog` from "./SentenceTree".)

- [ ] **Step 3: 렌더 위치** — inline 모드의 foot 위, 팝오버 모드의 `multi-popover-foot` 위에 `{screenerBlock}` 삽입.

- [ ] **Step 4: 타입체크/빌드**

Run: `cd platform/web && bun run build`
Expected: 본 컴포넌트 타입 OK(IrBuilder 연결은 Task 11).

- [ ] **Step 5: 커밋**

```bash
git add web/src/components/MultiSymbolPicker.tsx
git commit -m "feat(web): 종목추가 팝업에 세부조건 설정(조건 빌더+재선별 시점) 추가"
```

### Task 11: IrBuilder — 체크박스 제거·tradable 유니버스·상태 연결

**Files:**
- Modify: `web/src/pages/IrBuilder.tsx` (state 126-127, hydrate 232-243, build 357-363, panel 600-639)

- [ ] **Step 1: state** — 126-127 교체:
```ts
  const [screenerCond, setScreenerCond] = useState<IrNode | null>(null);
  const [screenerRefresh, setScreenerRefresh] =
    useState<"each_rebalance" | "once_at_start">("each_rebalance");
```
(`useScreener` 제거.)

- [ ] **Step 2: hydrate** — 232-243 교체:
```ts
    const u = def.universe ?? { kind: "single" };
    setUniverseSymbols(u.kind === "all" ? "" : (u.symbols ?? []).join(", "));
    setScreenerCond(u.screener?.condition ?? null);
    setScreenerRefresh(u.screener?.refresh ?? "each_rebalance");
```

- [ ] **Step 3: build** — 357-363 교체:
```ts
    // ── 유니버스 ──
    const universe: Record<string, unknown> = {
      kind: syms.length > 1 ? "list" : syms.length === 1 ? "single" : "all",
      ...(syms.length ? { symbols: syms } : {}),
      ...(syms.length && screenerCond
          ? { screener: { condition: screenerCond, refresh: screenerRefresh } }
          : {}),
    };
```
(이후 `universe` 를 쓰는 라인은 그대로. 기존 358-364의 if/else 블록 전체를 위로 대체.)

- [ ] **Step 4: 패널 UI** — 600-639 영역 교체:
  - 600-613 `{!useScreener && (...)}` 래퍼 제거 → 대상종목 블록 항상 렌더.
  - `MultiSymbolPicker` 에 `scope="tradable"` + 세부조건 props 연결:
```tsx
        <div style={{ marginBottom: 10 }}>
          <div className="muted" style={{ fontSize: 13, marginBottom: 4 }}>대상 종목</div>
          <MultiSymbolPicker symbols={symbols} value={universeSymbols}
                             onChange={setUniverseSymbols} scope="tradable"
                             strategies={strategies.filter(
                               (s) => s.engine !== "operand" && String(s.id) !== editId)}
                             screener={screenerCond} onScreenerChange={setScreenerCond}
                             screenerRefresh={screenerRefresh}
                             onScreenerRefreshChange={setScreenerRefresh}
                             catalog={catalog} selfIndicators={selfIndicators} />
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            비우면 전체 종목이 유니버스가 됩니다. 매수/매도 가능 종목에서 선택 ·
            "내 전략" 탭에서 저장한 전략을 자산으로 골라 전략끼리 조합할 수 있습니다.
          </p>
        </div>
```
  - 620-639 체크박스 + 스크리너 조건 영역 **전체 삭제**.

(`catalog`·`selfIndicators` 는 IrBuilder에 이미 존재 — 634행에서 SentenceTree에 넘기던 것과 동일.)

- [ ] **Step 5: 타입체크/빌드**

Run: `cd platform/web && bun run build`
Expected: PASS (kind==="screener" 잔여 비교 없음 — 모두 제거됨)

- [ ] **Step 6: 커밋**

```bash
git add web/src/pages/IrBuilder.tsx
git commit -m "feat(web): 유니버스 tradable 전용·스크리너 체크박스 제거·세부조건 팝업 연결"
```

---

## Part 8 — 마지막: kind enum에서 screener 제거 (clean removal)

### Task 12: core spec.py Universe.kind enum 정리 + test_screener 마이그레이션

**Files:**
- Modify: `core/quant_core/ir_engine/spec.py:33,35`
- Modify: `tests/test_screener.py` (kind="screener" → list+screener)
- 잔여 `kind == "screener"` 전수 제거

**전제:** Part 0 사전 조회 통과(실사용 kind=screener 전략 없음 확인).

- [ ] **Step 1: 잔여 참조 확인**

Run: `cd platform && grep -rn "screener" core --include=*.py | grep "kind"`
Expected: Part 1-5 후 남는 것은 enum 정의(33행)와 stale 주석뿐이어야. 남은 `kind == "screener"` 가 있으면 모두 처리.

- [ ] **Step 2: test_screener.py 마이그레이션** — `_spec()` 빌더(61-67) 교체:
```python
def _spec(condition) -> dict:
    return {"signal": {"op": "data", "params": {"ref": "momentum_12_1m"}},
            "universe": {"kind": "list", "symbols": ["A", "B", "C", "D"],
                         "screener": ({"condition": condition} if condition else None)},
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 2}},
            "simulation": {"initial_capital": 1e7}}
```
`test_screener_requires_condition` 은 screener=None 이면 "세부조건 없음"이라 이제 에러가 아님 → 이 테스트는 `test_screener_requires_symbols`(Task 4)로 대체됐으므로 삭제하거나, "condition 키만 있고 빈 값" 케이스로 변경. (삭제 권장 — symbols 필수 테스트가 커버.)
`test_screener_rejects_on_signal` 은 list+screener+on_signal 이 이제 **허용**되므로 삭제(Task 4 `test_event_with_screener_allowed_in_backtest` 가 대체).

- [ ] **Step 3: enum 제거** — `spec.py` Universe(33-35):
```python
class Universe(BaseModel):
    kind: Literal["single", "list", "all"] = "single"
    symbols: list[str] = Field(default_factory=list)
    screener: Optional[dict] = None                    # {"condition": Node, "refresh": str}
    exclude_macro: bool = True
```
run.py:115-116 stale 주석("_run_rebalance의 _screener_mask") 정리.

- [ ] **Step 4: 전체 테스트**

Run: `cd platform && pytest tests/ -q`
Expected: PASS (전부)

- [ ] **Step 5: 서버 테스트**

Run: `cd platform && pytest server/tests/ -q`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add core/quant_core/ir_engine/spec.py core/quant_core/ir_engine/run.py tests/test_screener.py
git commit -m "refactor: kind enum에서 screener 제거(clean removal) + 테스트 마이그레이션"
```

---

## Part 9 — 통합 검증

### Task 13: 백테스트 골든 회귀 + 웹 브라우저 검증

**Files:** 없음(검증).

- [ ] **Step 1: 코어 전체 + 골든**

Run: `cd platform && pytest tests/ -q`
Expected: PASS. 특히 `test_backtest_golden.py` 회귀 무변경 확인.

- [ ] **Step 2: 웹 빌드**

Run: `cd platform/web && bun run build`
Expected: 타입·빌드 PASS.

- [ ] **Step 3: 브라우저 검증** — `cd platform/web && bun run dev` 후 전략 연구소(IrBuilder)에서:
  - 대상 종목 피커가 **tradable만** 노출(지수·VIX 미표시) 확인.
  - 종목 2~3개 선택 → "세부조건 설정" 펼침 → 조건 입력 → 동적/정적 라디오 토글.
  - 0개 선택 시 세부조건 비활성 확인.
  - 저장 → 재로드 시 세부조건·refresh 라운드트립 확인.
  - 백테스트 실행되어 결과 표시 확인.

- [ ] **Step 4: 서버 가드 수동 확인** — 비매매 종목/이벤트+세부조건 전략을 paper로 저장 시도 → 422 + 친화 메시지.

- [ ] **Step 5: 최종 커밋(있으면)** — 없으면 생략.

---

## Self-Review 체크 (작성자 기록)

- **Spec 커버리지:** §2 모델→T9/T12, §3 UI→T9-11, §4 엔진→T1-3, §5 검증→T4, §5.1 데이터→T5, §6 NL→T6, §7 서버가드→T7-8, §8 마이그레이션→T0/T12, §10 테스트→각 Task, §11 파일→전 Task. ✅
- **순서 안전:** 게이트 일반화(T1-5)는 하위호환 → 각 커밋 그린. enum 제거(T12)는 마지막. ✅
- **타입 일관성:** `screener.refresh` 기본 `each_rebalance` 전 계층 일관. `_apply_refresh(mask, refresh, start)` T1 정의·T2/T3 사용 동일 시그니처. `tradable_symbols()` T7 정의·T8 사용. ✅
- **확인 필요(구현 중):** capabilities 함수명(T6 Step2), test_strategies_ir fixture/`_IR_DEF` 시그니처(T8), 비매매 예시 심볼 실제 코드(T8), NL 쿡북 파일 위치(T6 Step1).

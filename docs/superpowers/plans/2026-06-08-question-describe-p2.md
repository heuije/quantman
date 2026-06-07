# P2 (대상 확장: 단일종목 360 리포트 + 포트폴리오 진단) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** DESCRIBE 동사를 두 신규 대상(축A)으로 확장 — 단일종목 360 리포트와 포트폴리오 진단 — 모두 보유 데이터(가격/펀더멘털/분류)에서 결정적으로 계산하는 데이터-기반 답변 슬라이스.

**Architecture:** `query="describe"`를 `universe.kind`로 분기: single→`run_describe_report`(360 스냅샷), portfolio→`run_portfolio_diagnosis`(집중·섹터·밸류·리스크), all/list→기존 `_run_signal_study`(팩터 분포, 무변경). 기존 엔진 헬퍼(`_universe_symbols`·`get_symbol_group`) 재사용, 새 평가기 없음. 가산적 — simulate/select 경로 무영향(골든 14 무변경).

**Tech Stack:** Python(pydantic·pandas·numpy), pytest 골든, FastAPI 서버(NL 컴파일러), React/TS(web 타입).

---

## 컨텍스트 (P0·P1 위에 스택 — feature/question-plane-p0)

- `StrategyIR`(spec.py:196): `query: Literal["select","describe","relate","simulate"]="simulate"` 존재. `Universe`(spec.py:33): `kind: Literal["single","list","all"]`, `symbols`, `screener`, `exclude_macro`.
- `run_query`(run.py:111)가 동사 분기 — 현재 `describe`→`_run_signal_study`(팩터 분포, study.target_node 필요).
- `_universe_symbols`(run.py:147): kind in ("single","list")→symbols 필터; "all"→전 종목.
- 헬퍼: `get_symbol_group(sym,"Sector")`(expression_parser.py:15 — 분류 사이드카, 미수급=KR"기타"/그외"Other"). `_empty(msg)`(backtest) → `{"success":False,...}`. `TRADING_DAYS=252`(run.py:33).
- `validate_strategy`(spec.py:303): query="describe" 또는 IC면 `study.target_node` 요구(spec.py:498-499). SELECT는 S-SEL(score+top_n/pct).
- `test_capability_coverage`(core/tests): StrategyIR 트리의 **모든 Literal**이 `capability_spec()`에 `value`로 등장해야 PASS — `kind`에 "portfolio" 추가 시 capability에도 추가 필수.
- 골든 앵커: `tests/test_backtest_golden.py`(GOLDEN dict **절대 수정 금지**).

## 범위 밖 / 홀드 (기록만 — 진행하며 닫지 않음)
- **360 리포트 데이터-게이트 facet**(왜 올랐나=뉴스·성장전망=추정치·실적후확률=이벤트) → **P3**. P2 리포트는 가격/펀더멘털/분류만.
- **동종 대비 백분위(peer-relative)** 컨텍스트 → 후속(또는 SELECT로 별도 질의).
- **포트폴리오 보유 SOURCE**(실제 KIS 계좌 포지션 배선) → 3계층/자동매매 브리지, 별개. 엔진은 명시 holdings 입력만.
- **베타(시장 대비)** → 벤치마크 선택 협의 필요 → 홀드(vol+상관으로 충분).
- **signal이 분석 동사(describe/relate)에서 required-but-unused** → 선존 부채, **P5**에서 분석동사 스키마 재방문 시 해소(P2에서 확장 금지 — 12개소 ripple·simulate hot path 위험).
- **NL 라이브 산출**(LLM이 실제 describe+single/portfolio emit) → ANTHROPIC_API_KEY eval 필요(구조·idiom만 P2).

## 제약·원칙
- atomic(기존 헬퍼·블록 재사용, 전용필드 최소 — `Universe.weights` 1개만 추가). 4계층 계약(리서치 query=3계층, 자동매매 N/A). 검증된 해결책만(로컬 골든 게이트). simulate hot path·골든 GOLDEN 무수정.
- 금지 enum 가드 존중: `reduction`에 extremize(P4) 추가 금지. P2는 `universe.kind`에 portfolio만 추가.
- subagent는 **편집·테스트만, git commit/push 금지**(메인 세션이 검토 후 커밋·푸시). 범위 밖 파일 수정 금지. 공유 파일(spec.py·run.py) 수정 전 현재 라인 재확인.

---

## 파일 구조

| 파일 | 책임 | 변경 |
|---|---|---|
| `core/quant_core/ir_engine/spec.py` | IR 스키마·검증 | `Universe.kind`+portfolio·`Universe.weights`; `validate_strategy` describe 분기·S-PORT |
| `core/quant_core/ir_engine/run.py` | 디스패치·러너 | `run_query` describe 분기; `_universe_symbols` portfolio; `run_describe_report`·`run_portfolio_diagnosis` 신규 |
| `core/quant_core/ir_engine/capabilities.py` | 능력 자기서술 | universe_kind+portfolio; query describe use_for; weights 노트 |
| `core/quant_core/ir_engine/__init__.py` | 패키지 export | 신규 러너 2개·`SelectSpec` export |
| `core/tests/test_question_describe_p2.py` | **신규 골든** | 리포트·진단·검증 결정적 단언 |
| `server/app/ir_compiler.py` | NL 컴파일러 | query 스키마 문구·idiom #8(리포트·진단) |
| `server/evals/compile_archetypes.py` | NL eval | archetype 2종(삼성 어때·포트 진단) |
| `web/src/types.ts` | 웹 타입 | universe.kind+portfolio·weights; 결과 타입 |

---

### Task 1: 스키마 — Universe portfolio kind + weights + capability 노출

**Files:**
- Modify: `core/quant_core/ir_engine/spec.py:33-37` (Universe)
- Modify: `core/quant_core/ir_engine/capabilities.py:16-23` (universe_kind), `:164-168` (query describe)
- Test: `core/tests/test_capability_coverage.py` (기존 — 통과 확인)

- [ ] **Step 1: Universe에 portfolio kind + weights 추가**

`spec.py` Universe 클래스를 교체:
```python
class Universe(BaseModel):
    kind: Literal["single", "list", "all", "portfolio"] = "single"
    symbols: list[str] = Field(default_factory=list)   # single(1)/list·portfolio(다수)
    screener: Optional[dict] = None                    # 선택 종목 2차 필터: {"condition": Node, "refresh": str}
    exclude_macro: bool = True                         # all: 매크로/자산 지수 제외
    # portfolio 전용 — 보유 비중 {symbol: weight}. 없으면 동일가중. 이것은 "내 실제 보유"(진단 대상,
    # 축A)로, position.sizing.weights("전략 목표배분")와 평면이 다르다(진단엔 position 자체가 없음).
    weights: Optional[dict] = None
```

- [ ] **Step 2: capability_spec에 portfolio + describe 용례 노출**

`capabilities.py` `universe_kind` 리스트(현 :16-23) 끝에 항목 추가:
```python
            {"value": "portfolio", "does": "내 보유 종목 집합(진단 대상). universe.weights로 비중(없으면 동일가중)",
             "use_for": "포트폴리오 진단 — 집중도(HHI)·섹터 노출·가중 밸류·포트 변동성. query=describe와 함께."},
```

`capabilities.py` `query` 리스트의 describe 항목(현 :164-165)을 교체:
```python
            {"value": "describe", "does": ("살펴보기 — 대상에 따라: 단일종목(universe.kind=single)=가격·수익·"
                                           "리스크·밸류·섹터 360 리포트; 포트폴리오(kind=portfolio)=집중·섹터노출·"
                                           "리스크 진단; 종목군(kind=all/list)=임의 score 노드 값의 분포·요약(study.target_node)"),
             "use_for": "'이 종목 어때'(single)·'내 포트폴리오 진단'(portfolio)·신호 분포 연구(all/list+target_node)."},
```

- [ ] **Step 3: capability 커버리지 가드 통과 확인**

Run: `cd core && python -m pytest tests/test_capability_coverage.py -q`
Expected: PASS (새 Literal "portfolio"가 universe_kind value로 노출됨).

- [ ] **Step 4: Commit 없음** — 메인 세션이 검토 후 커밋. 변경 파일만 보고.

---

### Task 2: 디스패치 배선 — run_query describe 분기 + _universe_symbols portfolio + 러너 스텁

**Files:**
- Modify: `core/quant_core/ir_engine/run.py:111-129` (run_query), `:147-150` (_universe_symbols)
- Test: `core/tests/test_question_describe_p2.py` (신규 — 디스패치 스모크)

- [ ] **Step 1: 실패 테스트 작성 — 디스패치가 신규 러너로 라우팅**

`core/tests/test_question_describe_p2.py` 생성(상단 공통):
```python
"""P2 골든 — 단일종목 360 리포트 + 포트폴리오 진단 (run_describe_report·run_portfolio_diagnosis).

합성 픽스처로 결정적 단언(test_backtest_golden 스타일). describe를 universe.kind로 분기:
single→리포트, portfolio→진단, all/list→기존 팩터 분포(무변경).

    cd platform && pytest core/tests/test_question_describe_p2.py -v
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.ir_engine import run_query
from quant_core.ir_engine.spec import StrategyIR

_CLOSE = {"op": "data", "params": {"ref": "__SELF__.Close"}}   # 분석동사 명목 신호


def _single_df(prices, pb=None, n_pad=0):
    n = len(prices)
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    cols = {"Open": prices, "High": [p * 1.01 for p in prices],
            "Low": [p * 0.99 for p in prices], "Close": prices,
            "Volume": np.full(n, 1e6)}
    if pb is not None:
        cols["pb_ratio"] = np.full(n, pb)
    return pd.DataFrame(cols, index=idx)


# 단일종목 픽스처: 첫 130일 100, 다음 130일 200 (단조비감소). 260포인트.
_RAMP = [100.0] * 130 + [200.0] * 130
_DS_SINGLE = {"AAA": _single_df(_RAMP, pb=1.5)}


def _report_ir():
    return StrategyIR.model_validate({
        "universe": {"kind": "single", "symbols": ["AAA"]},
        "signal": _CLOSE, "query": "describe"})


def test_describe_single_routes_to_report():
    res = run_query(_report_ir(), _DS_SINGLE)
    assert res["success"] and res["query"] == "describe" and res["report"] == "single"
    assert res["symbol"] == "AAA"
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

Run: `cd platform && pytest core/tests/test_question_describe_p2.py::test_describe_single_routes_to_report -v`
Expected: FAIL — `run_describe_report` 미정의 또는 `res["report"]` KeyError.

- [ ] **Step 3: run_query 분기 + _universe_symbols portfolio + 러너 스텁**

`run.py` `run_query`(현 :119-120)의 describe 분기를 교체:
```python
    if q == "describe":
        u = strategy.universe
        if u.kind == "single":
            return run_describe_report(strategy, dataset)
        if u.kind == "portfolio":
            return run_portfolio_diagnosis(strategy, dataset)
        return _run_signal_study(strategy, dataset)
```

`run.py` `_universe_symbols`(현 :149)의 분기에 portfolio 추가:
```python
    if u.kind in ("single", "list", "portfolio"):
        return [s for s in u.symbols if s in dataset and not dataset[s].empty]
```

`run.py`에 신규 러너 스텁 2개 추가(run_select 다음, :453 부근 — 이후 Task 3·4에서 채움):
```python
def run_describe_report(strategy: StrategyIR, dataset: dict) -> dict:
    return _empty("미구현")   # Task 3


def run_portfolio_diagnosis(strategy: StrategyIR, dataset: dict) -> dict:
    return _empty("미구현")   # Task 4
```

- [ ] **Step 4: 테스트 실행 — 라우팅은 되나 스텁이라 success=False**

Run: `cd platform && pytest core/tests/test_question_describe_p2.py::test_describe_single_routes_to_report -v`
Expected: FAIL — `res["success"]`가 False(스텁). 라우팅 자체는 도달(다음 태스크에서 통과). 진행.

- [ ] **Step 5: Commit 없음** — 변경 파일만 보고.

---

### Task 3: run_describe_report (단일종목 360 리포트) — TDD 골든

**Files:**
- Modify: `core/quant_core/ir_engine/run.py` (run_describe_report 스텁 채움)
- Test: `core/tests/test_question_describe_p2.py` (골든 추가)

- [ ] **Step 1: 골든 테스트 작성**

`test_question_describe_p2.py`에 추가:
```python
def test_single_report_golden():
    res = run_query(_report_ir(), _DS_SINGLE)
    assert res["success"] and res["report"] == "single"
    assert res["sector"] == "Other"            # 합성 심볼 폴백
    assert res["data_points"] == 260
    p = res["price"]
    assert p["last"] == 200.0
    assert p["returns"]["12m"] == 1.0          # 200/100-1 (close[-253]=100)
    assert p["returns"]["6m"] == 0.0           # close[-127]=200
    assert p["high_52w"] == 200.0 and p["low_52w"] == 100.0
    assert res["risk"]["max_drawdown"] == 0.0  # 단조비감소
    assert res["risk"]["vol_annualized"] > 0   # 점프 1회로 변동성>0
    assert res["fundamentals"]["pb_ratio"] == 1.5
    assert res["fundamentals"]["trailing_pe"] is None   # 미수집=정직 None


def test_single_report_short_history():
    """짧은 이력 — 긴 윈도 수익은 None(크래시 금지)."""
    ds = {"BBB": _single_df([100.0, 101.0, 102.0])}
    ir = StrategyIR.model_validate({"universe": {"kind": "single", "symbols": ["BBB"]},
                                    "signal": _CLOSE, "query": "describe"})
    res = run_query(ir, ds)
    assert res["success"] and res["price"]["returns"]["12m"] is None
    assert res["price"]["last"] == 102.0


def test_single_report_missing_symbol():
    res = run_query(_report_ir(), {})          # 빈 데이터셋
    assert res["success"] is False
```

- [ ] **Step 2: 실패 확인**

Run: `cd platform && pytest core/tests/test_question_describe_p2.py -k single -v`
Expected: FAIL (스텁 _empty).

- [ ] **Step 3: run_describe_report 구현**

`run.py` `run_describe_report` 스텁을 교체:
```python
def run_describe_report(strategy: StrategyIR, dataset: dict) -> dict:
    """단일종목 360 리포트 — 한 종목의 가격·수익·리스크·밸류·섹터 스냅샷(DESCRIBE+단일대상).

    시계열 시뮬·신호 평가 없음(signal은 분석동사 명목값). 보유 데이터(가격/펀더멘털/분류)에서
    결정적 요약을 조립. 미수집 펀더멘털은 None으로 정직 표기(가짜 채움 금지). 데이터 기반 답변
    패러다임의 단일대상 슬라이스 — 뉴스/추정치/이벤트 기반 facet(왜 올랐나·성장전망·실적후확률)은 P3.
    """
    from ..expression_parser import get_symbol_group
    syms = _universe_symbols(strategy, dataset)
    if not syms:
        return _empty("리포트 대상 종목 데이터가 없습니다.")
    sym = syms[0]
    df = dataset.get(sym)
    if df is None or "Close" not in df.columns or df["Close"].dropna().empty:
        return _empty(f"{sym} 가격 데이터가 없습니다.")
    close = df["Close"].astype(float).dropna()
    asof = close.index[-1]
    last = float(close.iloc[-1])

    def _ret(days):
        if len(close) <= days:
            return None
        prev = float(close.iloc[-1 - days])
        return (last / prev - 1.0) if prev > 0 else None
    returns = {"1m": _ret(21), "3m": _ret(63), "6m": _ret(126), "12m": _ret(252)}

    win = close.iloc[-252:]
    hi_52w, lo_52w = float(win.max()), float(win.min())
    pct_from_high = (last / hi_52w - 1.0) if hi_52w > 0 else None

    rets = close.pct_change().dropna()
    rwin = rets.iloc[-252:]
    vol_ann = (float(rwin.std()) * (TRADING_DAYS ** 0.5)) if len(rwin) > 1 else None
    dd = close / close.cummax() - 1.0
    max_dd = float(dd.min()) if len(dd) else None

    fundamentals = {}
    for col in ("pb_ratio", "trailing_pe", "ev_ebitda"):
        if col in df.columns:
            s = df.loc[df.index <= asof, col].dropna()
            fundamentals[col] = float(s.iloc[-1]) if len(s) else None
        else:
            fundamentals[col] = None

    return {
        "success": True, "query": "describe", "report": "single",
        "symbol": sym, "sector": get_symbol_group(sym, "Sector"),
        "as_of": str(asof)[:10], "data_points": int(len(close)),
        "price": {"last": last, "returns": returns, "high_52w": hi_52w,
                  "low_52w": lo_52w, "pct_from_52w_high": pct_from_high},
        "risk": {"vol_annualized": vol_ann, "max_drawdown": max_dd},
        "fundamentals": fundamentals,
    }
```

- [ ] **Step 4: 통과 확인**

Run: `cd platform && pytest core/tests/test_question_describe_p2.py -k single -v`
Expected: PASS (3건).

- [ ] **Step 5: Commit 없음** — 변경 파일만 보고.

---

### Task 4: run_portfolio_diagnosis (포트폴리오 진단) — TDD 골든

**Files:**
- Modify: `core/quant_core/ir_engine/run.py` (run_portfolio_diagnosis 스텁 채움)
- Test: `core/tests/test_question_describe_p2.py` (골든 추가)

- [ ] **Step 1: 골든 테스트 작성**

`test_question_describe_p2.py`에 추가:
```python
# 포트폴리오 픽스처: 변동성 있는 동일 경로(상관=1), pb 1/2/3.
_PATH = [100.0 + (i % 5) for i in range(80)]


def _pf_df(pb):
    return _single_df(_PATH, pb=pb)


_DS_PF = {"AAA": _pf_df(1.0), "BBB": _pf_df(2.0), "CCC": _pf_df(3.0)}


def _diag_ir(weights=None):
    u = {"kind": "portfolio", "symbols": ["AAA", "BBB", "CCC"]}
    if weights is not None:
        u["weights"] = weights
    return StrategyIR.model_validate({"universe": u, "signal": _CLOSE, "query": "describe"})


def test_portfolio_diagnosis_golden(monkeypatch):
    import quant_core.expression_parser as ep
    monkeypatch.setattr(ep, "get_symbol_group",
                        lambda s, g="Industry": {"AAA": "반도체", "BBB": "반도체",
                                                 "CCC": "자동차"}.get(s, "Other"))
    res = run_query(_diag_ir({"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}), _DS_PF)
    assert res["success"] and res["report"] == "portfolio" and res["n_holdings"] == 3
    c = res["concentration"]
    assert abs(c["hhi"] - 0.38) < 1e-9            # .25+.09+.04
    assert abs(c["effective_n"] - 1 / 0.38) < 1e-6
    assert c["top_weight"] == 0.5 and abs(c["top3_weight"] - 1.0) < 1e-9
    assert abs(res["sector_exposure"]["반도체"] - 0.8) < 1e-9
    assert abs(res["sector_exposure"]["자동차"] - 0.2) < 1e-9
    assert abs(res["valuation"]["weighted_pb"] - 1.7) < 1e-9   # .5*1+.3*2+.2*3
    assert res["valuation"]["weighted_pe"] is None
    assert abs(res["risk"]["avg_pairwise_corr"] - 1.0) < 1e-9  # 동일 경로
    assert res["risk"]["portfolio_vol_annualized"] > 0
    assert res["coverage"]["with_fundamentals"] == 3


def test_portfolio_equal_weight_default(monkeypatch):
    import quant_core.expression_parser as ep
    monkeypatch.setattr(ep, "get_symbol_group", lambda s, g="Industry": "Other")
    res = run_query(_diag_ir(), _DS_PF)            # weights 미지정 → 동일가중
    assert res["success"]
    for h in res["holdings"]:
        assert abs(h["weight"] - 1 / 3) < 1e-9
    assert abs(res["concentration"]["hhi"] - 1 / 3) < 1e-9


def test_portfolio_empty_rejected():
    ir = StrategyIR.model_validate({"universe": {"kind": "portfolio", "symbols": ["ZZZ"]},
                                    "signal": _CLOSE, "query": "describe"})
    res = run_query(ir, _DS_PF)                    # ZZZ 데이터 없음
    assert res["success"] is False
```

- [ ] **Step 2: 실패 확인**

Run: `cd platform && pytest core/tests/test_question_describe_p2.py -k portfolio -v`
Expected: FAIL (스텁).

- [ ] **Step 3: run_portfolio_diagnosis 구현**

`run.py` `run_portfolio_diagnosis` 스텁을 교체:
```python
def run_portfolio_diagnosis(strategy: StrategyIR, dataset: dict) -> dict:
    """포트폴리오 진단 — 보유 종목의 집중도·섹터노출·가중밸류·리스크 스냅샷(DESCRIBE+포트폴리오 대상).

    universe.kind="portfolio", symbols=보유, universe.weights(없으면 동일가중). 시뮬 없음.
    집중도(HHI·유효종목수)·섹터노출(분류)·가중 밸류·포트 변동성/평균상관을 보유 데이터로 결정적 계산.
    미수집은 coverage로 정직 표기. 실제 계좌 포지션 배선은 별개(엔진은 명시 holdings 입력).
    """
    from ..expression_parser import get_symbol_group
    u = strategy.universe
    holdings = [s for s in u.symbols if s in dataset and dataset[s] is not None
                and not dataset[s].empty and "Close" in dataset[s].columns]
    if not holdings:
        return _empty("진단할 보유 종목 데이터가 없습니다.")
    raw = u.weights or {}
    w = {s: float(raw[s]) for s in holdings if s in raw and float(raw[s]) > 0}
    if not w:
        w = {s: 1.0 for s in holdings}
    tot = sum(w.values())
    weights = {s: w.get(s, 0.0) / tot for s in holdings}
    asof = max(dataset[s]["Close"].dropna().index[-1] for s in holdings)

    wv = np.array([weights[s] for s in holdings], dtype=float)
    hhi = float((wv ** 2).sum())
    ws = sorted(wv, reverse=True)
    concentration = {"hhi": hhi, "effective_n": (float(1.0 / hhi) if hhi > 0 else None),
                     "top_weight": float(ws[0]), "top3_weight": float(sum(ws[:3]))}

    sector_exposure: dict = {}
    holdings_out = []
    for s in holdings:
        sec = get_symbol_group(s, "Sector")
        sector_exposure[sec] = sector_exposure.get(sec, 0.0) + weights[s]
        holdings_out.append({"symbol": s, "weight": weights[s], "sector": sec})

    def _wavg(col):
        num = wsum = 0.0
        for s in holdings:
            df = dataset[s]
            if col in df.columns:
                v = df.loc[df.index <= asof, col].dropna()
                if len(v):
                    num += weights[s] * float(v.iloc[-1]); wsum += weights[s]
        return (num / wsum) if wsum > 0 else None
    valuation = {"weighted_pb": _wavg("pb_ratio"), "weighted_pe": _wavg("trailing_pe")}

    closes = pd.DataFrame({s: dataset[s]["Close"].astype(float) for s in holdings})
    rets = closes.pct_change().dropna(how="any")
    risk = {"portfolio_vol_annualized": None, "avg_pairwise_corr": None}
    if rets.shape[0] > 1:
        cov = rets[holdings].cov().to_numpy() * TRADING_DAYS
        pvar = float(wv @ cov @ wv)
        risk["portfolio_vol_annualized"] = float(pvar ** 0.5) if pvar >= 0 else None
        if len(holdings) >= 2:
            cc = rets[holdings].corr().to_numpy()
            iu = np.triu_indices(len(holdings), k=1)
            vals = cc[iu][np.isfinite(cc[iu])]
            risk["avg_pairwise_corr"] = float(vals.mean()) if vals.size else None

    return {
        "success": True, "query": "describe", "report": "portfolio",
        "as_of": str(asof)[:10], "n_holdings": len(holdings), "holdings": holdings_out,
        "concentration": concentration, "sector_exposure": sector_exposure,
        "valuation": valuation, "risk": risk,
        "coverage": {"with_price": len(holdings),
                     "with_fundamentals": sum(1 for s in holdings if "pb_ratio" in dataset[s].columns
                                              and dataset[s]["pb_ratio"].dropna().shape[0] > 0)},
    }
```

- [ ] **Step 4: 통과 확인**

Run: `cd platform && pytest core/tests/test_question_describe_p2.py -k portfolio -v`
Expected: PASS (3건).

- [ ] **Step 5: Commit 없음** — 변경 파일만 보고.

---

### Task 5: 검증 규칙 — describe를 kind로 분기 + S-PORT

**Files:**
- Modify: `core/quant_core/ir_engine/spec.py:496-521` (분석 target_node 요구), 새 S-PORT 블록
- Test: `core/tests/test_question_describe_p2.py` (검증 단언)

- [ ] **Step 1: 검증 테스트 작성**

`test_question_describe_p2.py`에 추가:
```python
from quant_core.ir_engine.spec import validate_strategy

_FACTOR = {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}}


def _errs(d):
    return [i.rule for i in validate_strategy(StrategyIR.model_validate(d)) if i.is_error]


def test_validate_single_report_no_target_node_ok():
    # describe+single = 360 리포트 → target_node 불필요(분석 분포 아님).
    errs = _errs({"universe": {"kind": "single", "symbols": ["AAA"]},
                  "signal": _CLOSE, "query": "describe"})
    assert "S-target" not in errs


def test_validate_portfolio_ok():
    errs = _errs({"universe": {"kind": "portfolio", "symbols": ["AAA", "BBB"],
                               "weights": {"AAA": 0.6, "BBB": 0.4}},
                  "signal": _CLOSE, "query": "describe"})
    assert "S-PORT" not in errs and "S-target" not in errs


def test_validate_portfolio_requires_describe():
    errs = _errs({"universe": {"kind": "portfolio", "symbols": ["AAA"]},
                  "signal": _CLOSE, "query": "simulate"})
    assert "S-PORT" in errs        # portfolio는 진단(describe) 전용


def test_validate_portfolio_bad_weights():
    errs = _errs({"universe": {"kind": "portfolio", "symbols": ["AAA", "BBB"],
                               "weights": {"AAA": -0.5, "BBB": 0.4}},
                  "signal": _CLOSE, "query": "describe"})
    assert "S-PORT" in errs        # 음수 비중


def test_validate_universe_describe_still_requires_target_node():
    # describe+all = 팩터 분포 → target_node 여전히 필요(무변경).
    errs = _errs({"universe": {"kind": "all"}, "signal": _CLOSE, "query": "describe",
                  "position": {"entry": {"mode": "scheduled"}}})
    assert "S-target" in errs
```

- [ ] **Step 2: 실패 확인**

Run: `cd platform && pytest core/tests/test_question_describe_p2.py -k validate -v`
Expected: FAIL — portfolio S-PORT 미구현; describe+single이 현재 target_node 요구(S-target 오발).

- [ ] **Step 3: validate_strategy 수정**

`spec.py` 현 :498-499:
```python
    is_ic = s.query == "relate" and st.event is None
    if s.query == "describe" or is_ic:
```
를 교체(describe 중 *종목군 분포*만 target_node 요구 — single/portfolio는 별도 러너):
```python
    is_ic = s.query == "relate" and st.event is None
    describe_dist = s.query == "describe" and u.kind in ("all", "list")
    if describe_dist or is_ic:
```

`spec.py` 유니버스 검증 블록 끝(현 :394 `list` 검증 다음, screener 검증 `sc = u.screener or {}` 이전)에 S-PORT 추가:
```python
    # 포트폴리오 진단 — describe 전용 대상. 보유 종목 필요, 비중 정합(축A — position 없음).
    if u.kind == "portfolio":
        if s.query != "describe":
            issues.append(Issue("S-PORT", SEV_ERROR,
                                "portfolio 유니버스는 진단(query=describe) 전용입니다 — "
                                "보유 기반 시뮬은 별도 대상입니다.", "universe"))
        if not u.symbols:
            issues.append(Issue("S-PORT", SEV_ERROR,
                                "포트폴리오 진단은 보유 종목(symbols)이 1개 이상 필요합니다.", "universe"))
        if u.weights:
            if any(float(v) <= 0 for v in u.weights.values()):
                issues.append(Issue("S-PORT", SEV_ERROR,
                                    "보유 비중(weights)은 양수여야 합니다.", "universe.weights"))
            stray = set(u.weights) - set(u.symbols)
            if stray:
                issues.append(Issue("M-vacuous", SEV_INTEGRITY_WARN,
                                    f"비중에 보유 외 종목이 있습니다: {sorted(stray)} — 무시됩니다.",
                                    "universe.weights"))
```

- [ ] **Step 4: 통과 확인**

Run: `cd platform && pytest core/tests/test_question_describe_p2.py -k validate -v`
Expected: PASS (5건).

- [ ] **Step 5: Commit 없음** — 변경 파일만 보고.

---

### Task 6: 패키지 export — 신규 러너 + SelectSpec 갭 해소

**Files:**
- Modify: `core/quant_core/ir_engine/__init__.py`

- [ ] **Step 1: run import에 신규 러너 추가**

`__init__.py` run import 줄을 교체:
```python
from .run import (  # noqa: F401
    run_describe_report, run_period_split, run_portfolio_diagnosis, run_query,
    run_select, run_strategy_ir, run_sweep,
)
```

`__init__.py` spec import에 `SelectSpec` 추가(`Study, Universe` 옆 — 알파벳 위치):
```python
from .spec import (  # noqa: F401
    Entry, Exit, Overlays, ParamAxis, PositionSpec, SelectSpec, Sizing, SimSpec,
    StrategyIR, Study, Universe, field_contract, needed_columns, needed_symbols,
    signal_out_type, validate_strategy,
)
```

`__init__.py` `__all__` 리스트에 `"run_describe_report"`, `"run_portfolio_diagnosis"`, `"SelectSpec"` 추가(각각 run·spec 그룹).

- [ ] **Step 2: import 스모크**

Run: `cd platform && python -c "from quant_core.ir_engine import run_describe_report, run_portfolio_diagnosis, SelectSpec; print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit 없음** — 변경 파일만 보고.

---

### Task 7: NL 컴파일러 — describe 리포트·진단 idiom + 스키마 문구

**Files:**
- Modify: `server/app/ir_compiler.py:164` (query 스키마 문구), `:222-223` (idiom 추가)
- Modify: `server/evals/compile_archetypes.py` (archetype 2종)

- [ ] **Step 1: query 스키마 문구 갱신 (select 누락 + describe 대상 분기 명시)**

`ir_compiler.py:164`를 교체:
```python
  "query": "simulate|select|describe|relate",   // 무엇을 묻는가(기본 simulate=손익 백테스트). select=현시점 스크리닝, describe=살펴보기(단일종목 리포트·포트폴리오 진단·신호 분포), relate=관계/이벤트.
```

- [ ] **Step 2: idiom #8 추가 (리포트·진단)**

`ir_compiler.py` `<idioms>` 블록 내 레시피 7 다음, `</idioms>`(현 :223) 직전에 추가:
```python
8. [단일종목 360 리포트] "삼성전자 어때"·"이 종목 분석/요약"처럼 *한 종목의 현황*이 답이면 →
   query="describe" + universe.kind="single" + symbols=[그 종목] + signal=data("__SELF__.Close")
   (분석 동사라 신호는 명목 — 엔진이 가격·수익·리스크·밸류·섹터를 자동 조립). study 불필요.
   (※ '왜 올랐나/성장전망/실적후확률'은 뉴스·추정치·이벤트 데이터 필요 — 아직 미지원이면 assumptions에 명시.)
9. [포트폴리오 진단] "내 포트폴리오 진단"·"보유종목 집중·리스크 봐줘"처럼 *보유 집합의 진단*이면 →
   query="describe" + universe.kind="portfolio" + symbols=[보유들] + (보유 비중 알면 universe.weights={{종목:비중}}, 없으면 동일가중)
   + signal=data("__SELF__.Close")(명목). 엔진이 집중도(HHI)·섹터노출·가중밸류·포트 변동성 산출. study 불필요.
```

- [ ] **Step 3: archetype 2종 추가**

`server/evals/compile_archetypes.py`에 기존 패턴(저평가 반도체주 3개 archetype) 양식으로 추가. 단언:
- "삼성전자 어때?" → `query=="describe"` ∧ `universe.kind=="single"` ∧ `"005930" in universe.symbols`.
- "내 포트폴리오 진단해줘 (삼성전자, SK하이닉스, NAVER)" → `query=="describe"` ∧ `universe.kind=="portfolio"` ∧ `len(universe.symbols)>=2`.

기존 archetype 구조(딕트 필드명·단언 헬퍼)를 파일에서 확인 후 동일 형식으로 작성. **라이브 산출은 API키 필요 → 구조만, 라이브 단언은 키 환경 별도(홀드).**

- [ ] **Step 4: 컴파일러 모듈 import 스모크**

Run: `cd platform && python -c "import sys; sys.path.insert(0,'server'); sys.path.insert(0,'core'); import app.ir_compiler; print('ok')"`
Expected: `ok` (구문 오류 없음). ⚠ 키 없으면 라이브 호출 미실행.

- [ ] **Step 5: Commit 없음** — 변경 파일만 보고.

---

### Task 8: 웹 타입 — portfolio kind + weights + 결과 타입

**Files:**
- Modify: `web/src/types.ts:243-251` (universe), 결과 타입 추가

- [ ] **Step 1: universe.kind + weights**

`types.ts` IrStrategyDef의 universe(현 :243-251)를 교체:
```typescript
  universe: {
    kind: "single" | "list" | "all" | "portfolio";
    symbols?: string[];
    screener?: {
      condition: IrNode;
      refresh: "each_rebalance" | "once_at_start";
    } | null;
    exclude_macro?: boolean;
    weights?: Record<string, number> | null;   // portfolio 전용 — 보유 비중(없으면 동일가중)
  };
```

- [ ] **Step 2: 결과 타입 추가**

`types.ts` IrStrategyDef 끝(`}` 다음, IrSweepBucket 이전 :308 부근)에 추가:
```typescript
// query="describe" + universe.kind="single" — 단일종목 360 리포트 결과.
export interface IrSingleReport {
  success: boolean; query: "describe"; report: "single";
  symbol: string; sector: string; as_of: string; data_points: number;
  price: {
    last: number;
    returns: Record<"1m" | "3m" | "6m" | "12m", number | null>;
    high_52w: number; low_52w: number; pct_from_52w_high: number | null;
  };
  risk: { vol_annualized: number | null; max_drawdown: number | null };
  fundamentals: Record<"pb_ratio" | "trailing_pe" | "ev_ebitda", number | null>;
}

// query="describe" + universe.kind="portfolio" — 포트폴리오 진단 결과.
export interface IrPortfolioDiagnosis {
  success: boolean; query: "describe"; report: "portfolio";
  as_of: string; n_holdings: number;
  holdings: { symbol: string; weight: number; sector: string }[];
  concentration: { hhi: number; effective_n: number | null; top_weight: number; top3_weight: number };
  sector_exposure: Record<string, number>;
  valuation: { weighted_pb: number | null; weighted_pe: number | null };
  risk: { portfolio_vol_annualized: number | null; avg_pairwise_corr: number | null };
  coverage: { with_price: number; with_fundamentals: number };
}
```

- [ ] **Step 3: 웹 빌드**

Run: `cd platform/web && npm run build`
Expected: 빌드 성공(타입 에러 0). ⚠ 워크트리 web 경로는 `C:\Users\USER\_wt_futures\web`.

- [ ] **Step 4: Commit 없음** — 변경 파일만 보고.

---

### Task 9: 전체 회귀 검증 (게이트)

**Files:** 없음 (검증 전용)

- [ ] **Step 1: P2 골든 전체**

Run: `cd platform && pytest core/tests/test_question_describe_p2.py -v`
Expected: 전건 PASS(리포트 3 + 진단 3 + 검증 5 + 라우팅 1 = 12건 내외).

- [ ] **Step 2: capability 커버리지 + 마이그레이션 가드**

Run: `cd platform && pytest core/tests/test_capability_coverage.py core/tests/test_question_migration.py core/tests/test_question_select.py -v`
Expected: 전건 PASS(portfolio 노출·레거시 거부·select 무변경).

- [ ] **Step 3: 골든 백테스트 무변경 (핵심 불변식)**

Run: `cd platform && pytest tests/test_backtest_golden.py -v`
Expected: 14건 PASS, GOLDEN 값 무변경(simulate 무영향 확인).

- [ ] **Step 4: 전체 스위트 회귀**

Run: `cd platform && pytest core/tests tests server/tests -q`
Expected: 새 실패 0(P1 베이스 594 passed 기준 + P2 신규만 증가). 실패 시 **근본원인 조사 후 보고**(추측 수정 금지).

- [ ] **Step 5: 결과 보고** — 통과/실패 카운트, 골든 무변경 확인, 미해결 위험. Commit 없음.

---

## Self-Review

**Spec coverage:** P2 매트릭스 2칸(단일종목×DESCRIBE 리포트·포트폴리오×DESCRIBE 진단) → Task 3·4. universe.portfolio 1급화(spec §2.3) → Task 1. 게이트 "리포트 골든+진단 정합" → Task 3·4·9. NL V/T/E(spec §8) → Task 1(V capability)·7(T idiom·E archetype). 4계층(spec §7): UI=NL(Task7)+웹타입(Task8), 엔진=러너(Task3·4), 데이터=가격/펀더멘털/분류(보유), 자동매매=N/A(리서치). ✅

**Placeholder scan:** 모든 코드 스텝에 실제 코드. archetype(Task7-3)만 "기존 파일 형식 확인 후 작성" — compile_archetypes.py 구조가 파일별로 다를 수 있어 의도적(구현자가 기존 archetype 1개를 복제·수정). 단언 조건은 명시.

**Type consistency:** `run_describe_report`·`run_portfolio_diagnosis` 시그니처 `(strategy, dataset)→dict` 일관(Task2 스텁→3·4 채움→6 export). 결과 dict 키(report/symbol/price/risk/fundamentals·concentration/sector_exposure/valuation/risk/coverage)가 골든 단언(Task3·4)·웹 타입(Task8)과 일치. `Universe.weights: Optional[dict]`(spec)↔`weights?: Record<string,number>|null`(web) 일치. 검증 규칙 ID S-PORT·describe_dist 분기(Task5)가 골든 단언(Task5 테스트)과 일치.

**검증 가능성:** 모든 수치 단언이 손계산 가능(ramp 100→200: 12m=1.0·6m=0.0·maxDD=0; 진단 hhi=.38·weighted_pb=1.7·corr=1.0). 섹터는 monkeypatch로 결정적. 골든 무변경=불변식 게이트.

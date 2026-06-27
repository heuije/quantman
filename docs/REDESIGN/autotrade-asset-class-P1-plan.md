# P1 — 사이클 커버리지 게이트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`(권장) 또는
> `superpowers:executing-plans`로 task 단위 구현. 스텝은 체크박스(`- [ ]`)로 추적.
> 상위 설계: [autotrade-asset-class-redesign.md](autotrade-asset-class-redesign.md) (P1 = 자금안전 긴급).

**Goal.** 자동매매 사이클에 "전략/포지션이 요구하는 자산군의 자격증명이 로컬에 등록돼 있는지"
검사하는 결정적 커버리지 게이트를 신설해 **silent 오라우팅(C1)·naked-leg(C3)·미청산 침묵(C5)**을
한 진입점에서 차단한다.

**Architecture.** 새 `local/localapp/coverage.py`가 "활성 브로커 + 등록 슬롯 → 커버 자산군 집합"을
단일 출처로 제공(core `instrument_category` 재사용). 진입 패스(`_enter_from_preview`)는 전략의
후보 자산군이 미커버면 전략을 통째 skip, 청산 패스(`_cycle_body`)는 미커버 포지션을 조용히 skip하지
않고 `orphan_uncovered`로 표면화. 라우터/브로커 구조는 건드리지 않는다(P2 영역).

**Tech Stack.** Python(로컬앱 Tkinter/엔진), pytest, SimBroker 시나리오 테스트. core는 `pip install -e`
editable(이 worktree는 core 무변경 — 기존 `quant_core.exec_defaults.instrument_category` 사용).

**불변식.** KIS byte-identical(자격증명 등록 시 게이트는 통과 → 기존 동작) · 보안(자격증명 값 미노출).
**범위 밖(4원칙):** C6 청산 루프 광범위 try/except는 추가하지 않는다 — `_submit_sell`/`_submit_close_short`
가 이미 브로커 발주 예외를 잡아 `error` decision으로 처리하므로(trader.py:909-916), 블랭킷 가드는
증상 봉합(PR-1)이다. 잔여 헬퍼 취약점은 관측 시 해당 지점 근본수정.

---

## File Structure

- **Create** `local/localapp/coverage.py` — 커버리지 단일 출처(`covered_categories`, `missing_categories`).
  자격증명(secrets_store)에 의존하므로 core가 아닌 local. 한 책임(커버리지 판정).
- **Create** `local/tests/test_coverage.py` — coverage 모듈 단위 테스트(keyring 스텁).
- **Create** `local/tests/scenarios/test_coverage_gate.py` — 진입/청산/요약 통합 테스트(SimBroker).
- **Modify** `local/localapp/trader.py` — import 추가, 진입 게이트(`_enter_from_preview`),
  청산 게이트(`_cycle_body` 청산 루프), `cycle_summary` 카운트.
- **Modify** `local/tests/scenarios/conftest.py` — `isolated_trader` 픽스처에 coverage 스텁
  (SimBroker = 전 자산군 커버) 추가 → 기존 시나리오 테스트 무영향 보장.

---

## Task 1: coverage 모듈 (단일 출처)

**Files:**
- Create: `local/localapp/coverage.py`
- Test: `local/tests/test_coverage.py`

- [ ] **Step 1: 실패 테스트 작성** — `local/tests/test_coverage.py`

```python
"""coverage 모듈 단위 테스트 — 자격증명 슬롯 → 커버 자산군 매핑 (keyring 불요)."""
import pytest

from localapp import coverage, secrets_store


@pytest.fixture
def stub_slots(monkeypatch):
    """secrets_store 로더를 메모리 state로 스텁. 각 테스트가 state 키를 채워 슬롯 시뮬레이트."""
    state = {"broker": "kis", "kis": None, "kis_fut": None, "kis_ovf": None,
             "ls": None, "ls_fut": None, "ls_ovf": None}
    monkeypatch.setattr(secrets_store, "get_active_broker", lambda: state["broker"])
    monkeypatch.setattr(secrets_store, "load_kis", lambda: state["kis"])
    monkeypatch.setattr(secrets_store, "load_kis_futures", lambda: state["kis_fut"])
    monkeypatch.setattr(secrets_store, "load_kis_overseas_futures", lambda: state["kis_ovf"])
    monkeypatch.setattr(secrets_store, "load_ls", lambda: state["ls"])
    monkeypatch.setattr(secrets_store, "load_ls_futures", lambda: state["ls_fut"])
    monkeypatch.setattr(secrets_store, "load_ls_overseas_futures", lambda: state["ls_ovf"])
    return state


def test_kis_stock_covers_both_equity(stub_slots):
    stub_slots["kis"] = {"app_key": "x"}
    assert coverage.covered_categories() == {"kr_equity", "us_equity"}


def test_kis_stock_plus_kr_futures(stub_slots):
    stub_slots["kis"] = {"app_key": "x"}
    stub_slots["kis_fut"] = {"app_key": "y"}
    assert coverage.covered_categories() == {"kr_equity", "us_equity", "kr_futures"}


def test_kis_kr_futures_only(stub_slots):
    stub_slots["kis_fut"] = {"app_key": "y"}
    assert coverage.covered_categories() == {"kr_futures"}


def test_kis_overseas_futures_only(stub_slots):
    stub_slots["kis_ovf"] = {"app_key": "z"}
    assert coverage.covered_categories() == {"us_futures"}


def test_ls_stock_is_kr_equity_only(stub_slots):
    stub_slots["broker"] = "ls"
    stub_slots["ls"] = {"app_key": "x"}
    assert coverage.covered_categories() == {"kr_equity"}


def test_ls_kr_futures_only(stub_slots):
    stub_slots["broker"] = "ls"
    stub_slots["ls_fut"] = {"app_key": "y"}
    assert coverage.covered_categories() == {"kr_futures"}


def test_no_credentials_covers_nothing(stub_slots):
    assert coverage.covered_categories() == set()


def test_missing_detects_uncovered_futures(stub_slots):
    stub_slots["kis"] = {"app_key": "x"}   # 주식만
    # 005930=kr_equity(커버), 코스피200선물=kr_futures(미커버)
    assert coverage.missing_categories(["005930", "코스피200선물"]) == {"kr_futures"}


def test_missing_empty_when_all_covered(stub_slots):
    stub_slots["kis"] = {"app_key": "x"}
    stub_slots["kis_fut"] = {"app_key": "y"}
    assert coverage.missing_categories(["005930", "코스피200선물"]) == set()


def test_missing_ignores_empty_symbols(stub_slots):
    stub_slots["kis"] = {"app_key": "x"}
    assert coverage.missing_categories(["", "005930"]) == set()
```

- [ ] **Step 2: 실패 확인**

Run: `cd local && python -m pytest tests/test_coverage.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'localapp.coverage'`

- [ ] **Step 3: 최소 구현** — `local/localapp/coverage.py`

```python
"""자산군 커버리지 — 전략·포지션이 요구하는 자산군의 자격증명이 로컬에 있는지.

자동매매 사이클의 "커버리지 게이트"가 쓰는 단일 출처(SSOT). 자격증명은 로컬 keyring
(secrets_store)에만 있으므로 이 판정은 로컬 전용(서버·코어는 자격증명을 모른다 — 보안 불변식).

카테고리 어휘는 core의 instrument_category와 동일: kr_equity | kr_futures | us_equity | us_futures.
계좌(자격증명 슬롯) → 커버 카테고리:
  - 주식 계좌: KIS(load_kis)는 국내+미국 주식을 한 계좌로 처리 → {kr_equity, us_equity}.
              LS(load_ls)는 국내주식만(해외주식 미제공) → {kr_equity}.
  - 국내선물 계좌(load_kis_futures / load_ls_futures)               → {kr_futures}.
  - 해외선물 계좌(load_kis_overseas_futures / load_ls_overseas_futures) → {us_futures}.
"""
from __future__ import annotations

from collections.abc import Iterable

from quant_core.exec_defaults import instrument_category

from . import secrets_store


def covered_categories() -> set[str]:
    """활성 브로커 + 등록된 자격증명 슬롯이 커버하는 자산군 집합."""
    cov: set[str] = set()
    if secrets_store.get_active_broker() == "ls":
        if secrets_store.load_ls():
            cov.add("kr_equity")          # LS 주식계좌 = 국내주식(해외주식 미제공)
        if secrets_store.load_ls_futures():
            cov.add("kr_futures")
        if secrets_store.load_ls_overseas_futures():
            cov.add("us_futures")
    else:  # kis (기본)
        if secrets_store.load_kis():
            cov.update(("kr_equity", "us_equity"))   # KIS 주식계좌 = 국내+미국주식
        if secrets_store.load_kis_futures():
            cov.add("kr_futures")
        if secrets_store.load_kis_overseas_futures():
            cov.add("us_futures")
    return cov


def missing_categories(symbols: Iterable[str]) -> set[str]:
    """주어진 심볼들이 요구하는 자산군 중 자격증명 미등록(미커버)인 집합.

    빈 집합 = 전부 커버. instrument_category는 core 순수함수(데이터셋 심볼 분류)."""
    required = {instrument_category(s) for s in symbols if s}
    return required - covered_categories()
```

- [ ] **Step 4: 통과 확인**

Run: `cd local && python -m pytest tests/test_coverage.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: 커밋**

```bash
git add local/localapp/coverage.py local/tests/test_coverage.py
git commit -m "feat(autotrade): 자산군 커버리지 단일 출처 coverage 모듈 (P1)"
```

---

## Task 2: 진입 커버리지 게이트 (C1·C3) + 테스트 하니스 스텁

**Files:**
- Modify: `local/tests/scenarios/conftest.py` (isolated_trader 픽스처에 coverage 스텁)
- Modify: `local/localapp/trader.py:32`(import), `:1256`(게이트)
- Test: `local/tests/scenarios/test_coverage_gate.py` (신규)

- [ ] **Step 1: conftest 스텁 추가** — `local/tests/scenarios/conftest.py`의 `isolated_trader`
  픽스처에서 `broker = SimBroker()` 직전(또는 `return` 직전)에 추가. SimBroker는 무엇이든 거래
  가능하므로 테스트 세계에선 "전 자산군 커버"가 올바른 의미(실 keyring이 비어 게이트가 오발동하는 것 방지).

```python
    from localapp import coverage
    monkeypatch.setattr(coverage, "covered_categories",
                        lambda: {"kr_equity", "us_equity", "kr_futures", "us_futures"})
```

- [ ] **Step 2: 실패 테스트 작성** — `local/tests/scenarios/test_coverage_gate.py`

```python
"""커버리지 게이트 시나리오 — 미커버 자산군 전략 skip(진입)·포지션 orphan(청산) (P1).

    cd platform/local && python -m pytest tests/scenarios/test_coverage_gate.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from sim import invariants  # noqa: E402

_DUMMY_SIGNAL = {"all": [{"left": {"kind": "price"}, "op": ">", "right": {"kind": "const", "value": 0}}]}


def _ir_def(universe):
    return {
        "name": "전략", "engine": "ir", "universe": universe, "signal": _DUMMY_SIGNAL,
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 10},
                     "entry": {"mode": "on_signal"}, "exit": {}, "overlays": {}},
        "simulation": {},
    }


def _ds(closes):
    idx = pd.date_range("2026-05-01", periods=len(closes), freq="B")
    return {"005930": pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)}


_DS = _ds([70000, 70000, 70000, 70000, 70000])


def test_entry_gate_skips_uncovered_strategy(isolated_trader, monkeypatch):
    """선물 전략인데 선물 자격증명 미커버 → 전략 통째 skip, 발주 0 (C1·C3)."""
    from localapp import coverage
    t, broker = isolated_trader
    monkeypatch.setattr(coverage, "covered_categories", lambda: {"kr_equity", "us_equity"})
    sid = "futstrat"
    strategies = [{"id": sid, "name": "선물전략",
                   "definition": _ir_def({"kind": "single", "symbols": ["코스피200선물"]})}]
    by_strategy = [{"strategy_id": sid, "candidates": [{"symbol": "코스피200선물"}]}]
    decisions: list[dict] = []
    t._enter_from_preview(by_strategy, strategies, _DS, 10_000_000.0,
                          decisions, set(), market="KRX", catchup=False)
    assert broker.submitted == [], broker.submitted
    uncov = [d for d in decisions if d["action"] == "skip_uncovered"]
    assert len(uncov) == 1, decisions
    assert uncov[0]["strategy_id"] == sid


def test_entry_gate_allows_covered_strategy(isolated_trader, monkeypatch):
    """커버된 주식 전략은 게이트 통과 → 정상 발주 (회귀 가드)."""
    from localapp import coverage
    t, broker = isolated_trader
    monkeypatch.setattr(coverage, "covered_categories", lambda: {"kr_equity", "us_equity"})
    broker._prices["005930"] = 70000
    sid = "eq"
    strategies = [{"id": sid, "name": "주식전략",
                   "definition": _ir_def({"kind": "single", "symbols": ["005930"]})}]
    by_strategy = [{"strategy_id": sid, "candidates": [{"symbol": "005930"}]}]
    decisions: list[dict] = []
    t._enter_from_preview(by_strategy, strategies, _DS, 10_000_000.0,
                          decisions, set(), market="KRX", catchup=False)
    assert len(broker.submitted) == 1, decisions
    assert [d for d in decisions if d["action"] == "skip_uncovered"] == []
    invariants.check_all(t)
```

- [ ] **Step 3: 실패 확인**

Run: `cd local && python -m pytest tests/scenarios/test_coverage_gate.py -q`
Expected: FAIL — `test_entry_gate_skips_uncovered_strategy`에서 `broker.submitted`가 비지 않음
(게이트 미구현이라 코스피200선물을 발주 시도하거나 dataset 부재로 다른 경로) + `skip_uncovered` 0건.

- [ ] **Step 4: import 추가** — `local/localapp/trader.py:32`

기존:
```python
from . import analytics, intents, killswitch, order_log, state_store
```
변경:
```python
from . import analytics, coverage, intents, killswitch, order_log, state_store
```

- [ ] **Step 5: 진입 게이트 구현** — `local/localapp/trader.py`, `_enter_from_preview` 안
  `strat_name, strat_def = name_def`(현재 :1256) 바로 다음 줄에 삽입:

```python
            strat_name, strat_def = name_def
            # P1 커버리지 게이트 — 이 전략 후보가 요구하는 자산군 중 자격증명 미등록이 있으면
            # 전략을 통째 skip(naked-leg·오라우팅 차단). 한 leg만 발주하지 않는다.
            missing = coverage.missing_categories(c.get("symbol", "") for c in cands)
            if missing:
                decisions.append(order_log.decision(
                    "skip_uncovered", sid, strat_name, "",
                    f"자격증명 미등록 자산군: {', '.join(sorted(missing))} — 전략 skip"))
                continue
```

- [ ] **Step 6: 통과 확인 + 회귀**

Run: `cd local && python -m pytest tests/scenarios/test_coverage_gate.py tests/scenarios/test_ir_strategy_cycle.py -q`
Expected: PASS (신규 2 + 기존 IR 사이클 테스트 — conftest 스텁 덕에 기존 무영향)

- [ ] **Step 7: 커밋**

```bash
git add local/localapp/trader.py local/tests/scenarios/test_coverage_gate.py local/tests/scenarios/conftest.py
git commit -m "feat(autotrade): 진입 커버리지 게이트 — 미커버 전략 통째 skip (P1 C1·C3)"
```

---

## Task 3: 청산 커버리지 게이트 (C5)

**Files:**
- Modify: `local/localapp/trader.py` 청산 루프(현재 :1755-1756 직후)
- Test: `local/tests/scenarios/test_coverage_gate.py` (추가)

- [ ] **Step 1: 실패 테스트 추가** — `test_coverage_gate.py`에 함수 추가

```python
def test_cleanup_orphan_uncovered_position(isolated_trader, monkeypatch):
    """미커버 자산군 보유 포지션은 청산 skip(외부매도 오진) 대신 orphan_uncovered 표면화 (C5)."""
    from localapp import coverage
    t, broker = isolated_trader
    monkeypatch.setattr(coverage, "covered_categories", lambda: {"kr_equity", "us_equity"})
    # 선물 포지션(kr_futures, 미커버) 원장 주입 — 진입 경로 우회.
    t.ledger["heldfut"] = {
        "symbol": "코스피200선물", "qty": 1, "entry_date": "2026-05-20",
        "entry_price": 350.0, "peak_price": 350.0, "side": "long",
        "strategy_name": "선물전략", "definition": _ir_def(
            {"kind": "single", "symbols": ["코스피200선물"]})}
    payload = t.cycle(strategies=[], dataset=_DS, buy_candidates=[],
                      risk_limits={"kill_switch_daily_loss_pct": 3.0}, market="KRX")
    orphans = [d for d in payload["decisions"] if d["action"] == "orphan_uncovered"]
    assert len(orphans) == 1, payload["decisions"]
    assert orphans[0]["symbol"] == "코스피200선물"
    # 청산 발주 안 함 — 포지션 유지
    assert "heldfut" in t.ledger
    assert [s for s in broker.submitted if s.get("side") == "sell"] == []
```

- [ ] **Step 2: 실패 확인**

Run: `cd local && python -m pytest tests/scenarios/test_coverage_gate.py::test_cleanup_orphan_uncovered_position -q`
Expected: FAIL — `orphan_uncovered` 0건(게이트 미구현 → 다른 분기/skip_oversell로 감).

- [ ] **Step 3: 청산 게이트 구현** — `local/localapp/trader.py` 청산 루프, 시장 배칭
  `continue`(현재 :1755-1756) 바로 다음에 삽입:

```python
            if _market_group_safe(pos["symbol"]) != market:
                continue
            # P1 커버리지 게이트 — 이 포지션 자산군의 자격증명이 미등록이면 브로커가 보유를
            # 볼 수 없어 청산이 불가능하다. 조용한 skip(외부매도 오진) 대신 명시 경고로 표면화.
            if coverage.missing_categories([pos["symbol"]]):
                decisions.append(order_log.decision(
                    "orphan_uncovered", sid, pos.get("strategy_name", ""), pos["symbol"],
                    "자격증명 미등록 자산군 — 청산 불가(수동 정리 필요)"))
                continue
```

- [ ] **Step 4: 통과 확인 + 회귀**

Run: `cd local && python -m pytest tests/scenarios/test_coverage_gate.py tests/scenarios/test_unparseable_orphan.py -q`
Expected: PASS (신규 청산 테스트 + 기존 unparseable_orphan 무영향 — conftest 스텁으로 005930 커버됨)

- [ ] **Step 5: 커밋**

```bash
git add local/localapp/trader.py local/tests/scenarios/test_coverage_gate.py
git commit -m "feat(autotrade): 청산 커버리지 게이트 — 미커버 포지션 orphan_uncovered 표면화 (P1 C5)"
```

---

## Task 4: cycle_summary 카운트 표면화

**Files:**
- Modify: `local/localapp/trader.py` `cycle_summary` dict(현재 :1909-1910 인접)
- Test: `local/tests/scenarios/test_coverage_gate.py` (추가)

- [ ] **Step 1: 실패 테스트 추가** — `test_coverage_gate.py`에 함수 추가

```python
def test_cycle_summary_counts_uncovered(isolated_trader, monkeypatch):
    """cycle_summary가 진입 skip_uncovered·청산 orphan_uncovered를 카운트로 표면화."""
    from localapp import coverage
    t, broker = isolated_trader
    monkeypatch.setattr(coverage, "covered_categories", lambda: {"kr_equity", "us_equity"})
    # 청산측: 미커버 선물 포지션 주입
    t.ledger["heldfut"] = {
        "symbol": "코스피200선물", "qty": 1, "entry_date": "2026-05-20",
        "entry_price": 350.0, "peak_price": 350.0, "side": "long",
        "strategy_name": "보유선물", "definition": _ir_def(
            {"kind": "single", "symbols": ["코스피200선물"]})}
    # 진입측: 미커버 선물 전략 후보
    strategies = [{"id": "newfut", "name": "신규선물",
                   "definition": _ir_def({"kind": "single", "symbols": ["코스피200선물"]})}]
    by_strategy = [{"strategy_id": "newfut", "candidates": [{"symbol": "코스피200선물"}]}]
    payload = t.cycle(strategies=strategies, dataset=_DS, buy_candidates=by_strategy,
                      risk_limits={"kill_switch_daily_loss_pct": 3.0}, market="KRX")
    cs = payload["cycle_summary"]
    assert cs["n_skip_uncovered"] == 1, payload["decisions"]
    assert cs["n_orphan_uncovered"] == 1, payload["decisions"]
```

- [ ] **Step 2: 실패 확인**

Run: `cd local && python -m pytest tests/scenarios/test_coverage_gate.py::test_cycle_summary_counts_uncovered -q`
Expected: FAIL — `KeyError: 'n_skip_uncovered'` (카운트 미추가).

- [ ] **Step 3: 카운트 추가** — `local/localapp/trader.py` `cycle_summary` dict 안,
  `"n_unparseable_orphan"` 항목(현재 :1909-1910) 바로 다음에 삽입:

```python
            "n_unparseable_orphan": sum(
                1 for d in decisions if d["action"] == "unparseable_orphan"),
            "n_skip_uncovered": sum(
                1 for d in decisions if d["action"] == "skip_uncovered"),
            "n_orphan_uncovered": sum(
                1 for d in decisions if d["action"] == "orphan_uncovered"),
```

- [ ] **Step 4: 통과 확인**

Run: `cd local && python -m pytest tests/scenarios/test_coverage_gate.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: 전체 로컬 회귀**

Run: `cd local && python -m pytest tests/ -q`
Expected: PASS (전 시나리오·단위 테스트 — 신규 게이트가 기존 동작을 깨지 않음)

- [ ] **Step 6: 커밋**

```bash
git add local/localapp/trader.py local/tests/scenarios/test_coverage_gate.py
git commit -m "feat(autotrade): cycle_summary에 미커버 카운트 표면화 (P1)"
```

---

## Self-Review (작성자 체크)

**1. Spec coverage:** P1 설계(§3.3 커버리지 게이트 = C1·C3·C5) → Task 2(진입=C1·C3)·Task 3(청산=C5)·
Task 4(표면화)·Task 1(단일 출처)로 전부 커버. **C6(청산 루프 격리)는 P1에서 제외** — 코드 확인 결과
`_submit_sell`이 이미 브로커 발주 예외를 잡아 처리하므로 블랭킷 가드는 4원칙 위반(plan 상단 명시).
→ 설계서 §5 P1 행에서 C6 제거 필요(plan 적용 후 spec 동기화).
**2. Placeholder scan:** TBD/임의 가드/미정의 참조 없음. 모든 코드 스텝에 실제 코드.
**3. Type consistency:** `covered_categories() -> set[str]`·`missing_categories(Iterable[str]) -> set[str]`
일관. decision action 문자열 `skip_uncovered`/`orphan_uncovered`가 Task 2·3·4에서 동일.
**검증 한계:** instrument_category("코스피200선물")=="kr_futures"는 core 확인됨. LS us_equity=미제공
가정은 메모리 노트 기반 — Task 1 구현 시 `ls_broker.py` overseas 메서드 유무로 재확인(있으면 매핑 조정).
**라이브:** P1 통과 후 사장님 모의 1회(선물 키 미등록 상태에서 선물 전략 적용 → 로컬 로그에
skip_uncovered 확인)로 E2E 검증 권장.

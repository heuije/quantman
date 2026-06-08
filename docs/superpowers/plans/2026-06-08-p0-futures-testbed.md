# P0 — 테스트베드 선물화 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SimBroker 테스트베드가 선물(계약수·증거금·롱숏·승수·정산손익)을 모델링해, 선물 자동매매 로직을 오프라인 결정론으로 검증할 수 있게 한다.

**Architecture:** 선물 회계는 이미 `quant_core.exec_defaults.instrument_spec`(승수·증거금률 단일출처)와 `ir_engine.live.event_buy_qty`(선물 계약수 사이징)에 존재한다 — **재사용**한다. P0는 *테스트베드 측*만 추가한다: 순수 선물 헬퍼(`sim/futures.py`), SimBroker 증거금 잔고, 선물 머니패스 불변식, 선물 시나리오. 프로덕션 Trader의 ledger/_apply_fill/reconcile 선물화는 **P3**(라우팅 배선과 함께). 기존 32개 주식 시나리오 테스트는 무변경 green 유지.

**Tech Stack:** Python 3.11/3.12, pytest. 기존: `local/sim/`(broker·invariants·scenario), `local/tests/scenarios/`, `core/quant_core/exec_defaults.py`(instrument_spec), `docs/INVARIANTS.md`.

**규칙:** 테스트는 `cd platform/local && python -m pytest ...`. 커밋은 worktree(`C:/Users/USER/_wt-p0`, 브랜치 `plan/p0-futures-testbed`)에서. main 직접 push 금지(PR). 선물 포지션 정규형 side ∈ {"long","short"}(브로커 파서의 KIS 매수/매도→long/short 정규화는 P3).

---

## 파일 구조

| 파일 | 책임 | 변경 |
|---|---|---|
| `local/sim/futures.py` | 선물 테스트베드 순수 헬퍼(포지션 빌더·정산손익·증거금) — instrument_spec 재사용 | **신규** |
| `local/sim/broker.py` | SimBroker에 증거금 잔고(set_margin) + account_snapshot margin 통과 | 수정 |
| `local/sim/invariants.py` | 선물 머니패스 불변식(sign·정산손익·증거금) | 수정 |
| `docs/INVARIANTS.md` | INV-FUT-1/2/3 추가 | 수정 |
| `local/tests/test_sim_futures.py` | 순수 헬퍼·SimBroker·불변식 단위테스트 | **신규** |
| `local/tests/scenarios/test_futures_sim.py` | 롱·숏 라운드트립 정산손익 + 증거금 시나리오 | **신규** |

---

### Task 1: `sim/futures.py` — 선물 테스트베드 순수 헬퍼

**Files:**
- Create: `local/sim/futures.py`
- Test: `local/tests/test_sim_futures.py`

선물 회계는 `instrument_spec`(승수·증거금률)에서 가져온다. 정산손익 = (exit−entry)×qty×승수×(롱+1/숏−1). 증거금 = notional×개시증거금률.

- [ ] **Step 1: 실패 테스트 작성** — `local/tests/test_sim_futures.py`

```python
"""sim 선물 헬퍼 단위검증 — instrument_spec(승수·증거금) 재사용."""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent   # tests → local
for _p in (str(_LOCAL), str(_LOCAL.parent / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sim.futures import settlement_pnl, required_margin, make_futures_position


def test_settlement_pnl_long():
    # 코스피200선물: 승수 250_000. 롱 2계약, 375.0→377.0 = +2pt×2×250k = +1,000,000
    assert settlement_pnl("코스피200선물", "long", 2, 375.0, 377.0) == 1_000_000.0


def test_settlement_pnl_short_profits_on_drop():
    # 숏 1계약, 377.0→375.0(하락) = +2pt 이익 ×250k = +500,000
    assert settlement_pnl("코스피200선물", "short", 1, 377.0, 375.0) == 500_000.0


def test_required_margin():
    # notional = 375×2×250_000 = 187_500_000; 개시증거금률 0.10 → 18_750_000
    assert required_margin("코스피200선물", 2, 375.0) == 18_750_000.0


def test_make_futures_position_shape():
    p = make_futures_position("코스피200선물", "long", 2, 375.0, 377.0)
    assert p["symbol"] == "코스피200선물" and p["side"] == "long" and p["qty"] == 2
    assert p["avg_price"] == 375.0 and p["eval_price"] == 377.0
    assert p["multiplier"] == 250_000.0
    assert p["margin_requirement"] == 18_750_000.0
    assert p["eval_pnl"] == 1_000_000.0
```

- [ ] **Step 2: 실패 확인**

Run: `cd C:/Users/USER/_wt-p0/local && python -m pytest tests/test_sim_futures.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sim.futures'`

- [ ] **Step 3: 구현** — `local/sim/futures.py`

```python
"""선물 테스트베드 순수 헬퍼. 선물 회계(승수·증거금)는 quant_core 단일출처를 재사용.

정산손익 = (exit−entry)×qty×승수×부호(롱+1/숏−1). 증거금 = notional×개시증거금률.
side 정규형 "long"|"short"(브로커 파서의 KIS 매수/매도→정규화는 P3).
"""
from __future__ import annotations

from quant_core.exec_defaults import instrument_spec

_SIGN = {"long": 1.0, "short": -1.0}


def settlement_pnl(symbol: str, side: str, qty: int, entry: float, exit_: float) -> float:
    """정산/실현 손익(통화단위). side: long|short."""
    mult = instrument_spec(symbol).multiplier
    return (exit_ - entry) * qty * mult * _SIGN[side]


def required_margin(symbol: str, qty: int, price: float) -> float:
    """개시증거금 = notional × 개시증거금률."""
    spec = instrument_spec(symbol)
    return price * qty * spec.multiplier * spec.init_margin_rate


def make_futures_position(symbol: str, side: str, qty: int,
                          entry_price: float, now_price: float) -> dict:
    """SimBroker account_snapshot positions에 넣을 선물 포지션 dict(정규형)."""
    return {
        "symbol": symbol,
        "side": side,                       # "long" | "short"
        "qty": qty,                         # 계약수(양수)
        "avg_price": entry_price,
        "eval_price": now_price,
        "multiplier": instrument_spec(symbol).multiplier,
        "margin_requirement": required_margin(symbol, qty, entry_price),
        "eval_pnl": settlement_pnl(symbol, side, qty, entry_price, now_price),
    }
```

- [ ] **Step 4: 통과 확인**

Run: `cd C:/Users/USER/_wt-p0/local && python -m pytest tests/test_sim_futures.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: 커밋**

```bash
cd C:/Users/USER/_wt-p0
git add local/sim/futures.py local/tests/test_sim_futures.py
git commit -m "feat(sim): 선물 테스트베드 순수 헬퍼(정산손익·증거금·포지션 빌더)"
```

---

### Task 2: SimBroker 증거금 잔고

**Files:**
- Modify: `local/sim/broker.py`
- Test: `local/tests/test_sim_futures.py` (추가)

SimBroker는 set_positions로 임의 포지션을 이미 보유한다(선물 dict 통과 OK). 부족분은 **증거금 잔고**뿐 — `set_margin(total, available)` + account_snapshot에 `margin` 키 통과. 주식 동작 무변경(margin 미설정시 키 없음).

- [ ] **Step 1: 실패 테스트 추가** — `local/tests/test_sim_futures.py` 끝에

```python
from sim.broker import SimBroker


def test_simbroker_holds_futures_positions_and_margin():
    b = SimBroker()
    b.set_positions([make_futures_position("코스피200선물", "long", 2, 375.0, 377.0)])
    b.set_margin(total_margin=18_750_000.0, available_margin=100_000_000.0)
    snap = b.account_snapshot()
    assert snap["positions"][0]["side"] == "long"
    assert snap["positions"][0]["multiplier"] == 250_000.0
    assert snap["margin"] == {"total_margin": 18_750_000.0, "available_margin": 100_000_000.0}


def test_simbroker_stock_snapshot_has_no_margin_key():
    # 주식 회귀: margin 미설정이면 margin 키 없음(기존 동작 보존).
    b = SimBroker()
    assert "margin" not in b.account_snapshot()
```

- [ ] **Step 2: 실패 확인**

Run: `cd C:/Users/USER/_wt-p0/local && python -m pytest tests/test_sim_futures.py -q`
Expected: FAIL — `AttributeError: 'SimBroker' object has no attribute 'set_margin'`

- [ ] **Step 3: 구현** — `local/sim/broker.py`

`__init__`의 `self._positions: list[dict] = []` 다음 줄에 추가:

```python
        self._margin: dict | None = None
```

`account_snapshot` 메서드를 아래로 교체:

```python
    def account_snapshot(self, overseas: bool = True) -> dict:
        # overseas — 실 KisBroker는 국내/해외 조회를 가르지만 Sim은 단일 잔고. 인자만 수용.
        snap = {"balance": dict(self._balance), "positions": list(self._positions)}
        if self._margin is not None:          # 선물 모드에서만 증거금 노출(주식 무변경)
            snap["margin"] = dict(self._margin)
        return snap
```

`set_positions` 메서드 다음에 추가:

```python
    def set_margin(self, total_margin: float, available_margin: float) -> None:
        """선물 시나리오용 증거금 잔고 설정. account_snapshot()['margin']로 노출."""
        self._margin = {"total_margin": float(total_margin),
                        "available_margin": float(available_margin)}
```

- [ ] **Step 4: 통과 확인**

Run: `cd C:/Users/USER/_wt-p0/local && python -m pytest tests/test_sim_futures.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: 주식 회귀 확인**

Run: `cd C:/Users/USER/_wt-p0/local && python -m pytest tests/scenarios/ -q`
Expected: PASS (32 passed — 기존 주식 시나리오 무변경)

- [ ] **Step 6: 커밋**

```bash
cd C:/Users/USER/_wt-p0
git add local/sim/broker.py local/tests/test_sim_futures.py
git commit -m "feat(sim): SimBroker 증거금 잔고(set_margin) — 선물 모드에서만 노출"
```

---

### Task 3: 선물 머니패스 불변식

**Files:**
- Modify: `local/sim/invariants.py`
- Modify: `docs/INVARIANTS.md`
- Test: `local/tests/test_sim_futures.py` (추가)

선물 3대 불변식: 포지션 부호 정합(INV-FUT-1), 정산손익 공식(INV-FUT-2), 증거금 비초과(INV-FUT-3).

- [ ] **Step 1: 실패 테스트 추가** — `local/tests/test_sim_futures.py` 끝에

```python
import pytest

from sim import invariants


def test_inv_fut_sign_ok():
    pos = [make_futures_position("코스피200선물", "long", 2, 375.0, 377.0),
           make_futures_position("금선물", "short", 1, 2000.0, 1990.0)]
    invariants.check_futures_sign(pos)   # 위반 없음


def test_inv_fut_sign_rejects_bad_side():
    with pytest.raises(AssertionError, match="INV-FUT-1"):
        invariants.check_futures_sign([{"symbol": "코스피200선물", "side": "up", "qty": 1}])


def test_inv_fut_pnl_ok():
    invariants.check_futures_pnl([make_futures_position("코스피200선물", "long", 2, 375.0, 377.0)])


def test_inv_fut_pnl_rejects_wrong_pnl():
    bad = make_futures_position("코스피200선물", "long", 2, 375.0, 377.0)
    bad["eval_pnl"] = 999.0
    with pytest.raises(AssertionError, match="INV-FUT-2"):
        invariants.check_futures_pnl([bad])


def test_inv_fut_margin_ok():
    snap = {"margin": {"total_margin": 18_750_000.0, "available_margin": 100_000_000.0}}
    invariants.check_futures_margin(snap)


def test_inv_fut_margin_rejects_overleverage():
    snap = {"margin": {"total_margin": 120_000_000.0, "available_margin": 100_000_000.0}}
    with pytest.raises(AssertionError, match="INV-FUT-3"):
        invariants.check_futures_margin(snap)
```

- [ ] **Step 2: 실패 확인**

Run: `cd C:/Users/USER/_wt-p0/local && python -m pytest tests/test_sim_futures.py -q`
Expected: FAIL — `AttributeError: module 'sim.invariants' has no attribute 'check_futures_sign'`

- [ ] **Step 3: 구현** — `local/sim/invariants.py` 끝에 추가

```python
def check_futures_sign(positions) -> None:
    """INV-FUT-1: 선물 포지션 side는 long|short, qty>0(flat은 미보유=목록부재)."""
    for p in positions:
        assert p.get("side") in ("long", "short"), \
            f"INV-FUT-1 위반: {p.get('symbol')} side={p.get('side')}"
        assert int(p.get("qty", 0)) > 0, \
            f"INV-FUT-1 위반: {p.get('symbol')} qty={p.get('qty')}"


def check_futures_pnl(positions) -> None:
    """INV-FUT-2: eval_pnl = (eval−avg)×qty×승수×부호(롱+1/숏−1)."""
    from .futures import settlement_pnl
    for p in positions:
        exp = settlement_pnl(p["symbol"], p["side"], p["qty"], p["avg_price"], p["eval_price"])
        assert abs(float(p["eval_pnl"]) - exp) < 1e-6, \
            f"INV-FUT-2 위반: {p['symbol']} eval_pnl={p['eval_pnl']} 기대={exp}"


def check_futures_margin(snapshot) -> None:
    """INV-FUT-3: 점유 증거금 ≤ 가용 증거금(과레버리지 차단)."""
    m = snapshot.get("margin")
    if not m:
        return
    total, avail = float(m["total_margin"]), float(m["available_margin"])
    assert total <= avail, f"INV-FUT-3 위반: 증거금 {total} > 가용 {avail}"
```

- [ ] **Step 4: 통과 확인**

Run: `cd C:/Users/USER/_wt-p0/local && python -m pytest tests/test_sim_futures.py -q`
Expected: PASS (12 passed)

- [ ] **Step 5: INVARIANTS.md 문서화** — `docs/INVARIANTS.md`의 "A7 리스크·안전" 섹션 표 끝(또는 A6 계좌·원장 정합성 표 끝)에 행 추가

```markdown
| **INV-FUT-1** | A6 계좌·원장 정합성 | 선물 포지션 side는 long\|short, qty>0(flat은 미보유) |
| **INV-FUT-2** | A6 | 선물 정산손익 = (정산가−진입가)×계약수×승수×부호(롱+1/숏−1) |
| **INV-FUT-3** | A7 리스크·안전 | 점유 증거금 ≤ 가용 증거금(과레버리지 차단) |
```

- [ ] **Step 6: 커밋**

```bash
cd C:/Users/USER/_wt-p0
git add local/sim/invariants.py docs/INVARIANTS.md local/tests/test_sim_futures.py
git commit -m "feat(sim): 선물 머니패스 불변식 INV-FUT-1/2/3 + 문서"
```

---

### Task 4: 선물 시나리오 — 롱·숏 라운드트립 + 증거금

**Files:**
- Create: `local/tests/scenarios/test_futures_sim.py`

SimBroker로 선물 보유→정산을 구동하고 불변식·정산손익을 단언한다. 사이징(event_buy_qty)도 선물 계약수 산식을 1회 검증(재사용 회귀).

- [ ] **Step 1: 시나리오 테스트 작성** — `local/tests/scenarios/test_futures_sim.py`

```python
"""선물 테스트베드 시나리오 — SimBroker 위 롱·숏 라운드트립 정산손익 + 증거금 불변식.

프로덕션 Trader ledger의 선물화는 P3(라우팅 배선과 함께). P0는 테스트베드 측 검증.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent.parent
for _p in (str(_LOCAL), str(_LOCAL.parent / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sim import invariants
from sim.broker import SimBroker
from sim.futures import make_futures_position, required_margin, settlement_pnl


def test_long_roundtrip_settlement_pnl():
    # 롱 2계약 375→377 보유 → 정산손익 +1,000,000. 불변식 통과.
    b = SimBroker()
    b.set_positions([make_futures_position("코스피200선물", "long", 2, 375.0, 377.0)])
    b.set_margin(required_margin("코스피200선물", 2, 375.0), 100_000_000.0)
    snap = b.account_snapshot()
    invariants.check_futures_sign(snap["positions"])
    invariants.check_futures_pnl(snap["positions"])
    invariants.check_futures_margin(snap)
    assert snap["positions"][0]["eval_pnl"] == 1_000_000.0


def test_short_roundtrip_profits_on_drop():
    # 숏 1계약 377→375(하락) → 정산손익 +500,000(숏 이익).
    b = SimBroker()
    b.set_positions([make_futures_position("코스피200선물", "short", 1, 377.0, 375.0)])
    b.set_margin(required_margin("코스피200선물", 1, 377.0), 100_000_000.0)
    snap = b.account_snapshot()
    invariants.check_futures_sign(snap["positions"])
    invariants.check_futures_pnl(snap["positions"])
    assert snap["positions"][0]["eval_pnl"] == 500_000.0


def test_overleverage_caught_by_invariant():
    import pytest
    b = SimBroker()
    b.set_margin(total_margin=120_000_000.0, available_margin=100_000_000.0)
    with pytest.raises(AssertionError, match="INV-FUT-3"):
        invariants.check_futures_margin(b.account_snapshot())


def test_event_buy_qty_futures_sizing_reused():
    # 사이징 재사용 회귀: 증거금예산/(px×승수×증거금률) = floor.
    # 코스피200선물 단일 유니버스, 예산=cash(100%). px=375, 승수 250k, 증거금률 0.10.
    # denom = 375×250_000×0.10 = 9_375_000. cash 50,000,000 → floor(50M/9.375M)=5계약.
    from quant_core.ir_engine import StrategyIR
    from quant_core.ir_engine import live as ir_live
    ir = StrategyIR.model_validate({
        "universe": {"kind": "single", "symbols": ["코스피200선물"]},
        "signal": {"kind": "always"},
        "position": {"sizing": {"mode": "pct", "amount_pct": 100.0}},
    })
    qty = ir_live.event_buy_qty(ir, cash=50_000_000.0, prev_close=375.0, capital=50_000_000.0)
    assert qty == 5
```

- [ ] **Step 2: 실행 — 통과 확인**

Run: `cd C:/Users/USER/_wt-p0/local && python -m pytest tests/scenarios/test_futures_sim.py -q`
Expected: PASS (4 passed)

> ⚠ `test_event_buy_qty_futures_sizing_reused`의 IR dict가 `StrategyIR` 스키마와 어긋나면 ValidationError가 난다. 그 경우 `core/tests/test_futures_ir.py`에서 유효한 선물 IR 예시를 찾아 `universe/signal/position` 최소 형태를 맞춘 뒤 재실행(스키마 필드명·required는 그 파일이 단일 출처). qty 산식(=5)은 불변.

- [ ] **Step 3: 커밋**

```bash
cd C:/Users/USER/_wt-p0
git add local/tests/scenarios/test_futures_sim.py
git commit -m "test(sim): 선물 롱·숏 라운드트립 정산손익 + 증거금 + 사이징 재사용 시나리오"
```

---

### Task 5: 전체 회귀 + 마무리

**Files:** (없음 — 검증·문서만)

- [ ] **Step 1: 전체 local 테스트 green**

Run: `cd C:/Users/USER/_wt-p0/local && python -m pytest tests/ -q`
Expected: PASS — 기존 + 신규(test_sim_futures 12 + test_futures_sim 4) 모두 통과, 0 실패. 주식 시나리오 32개 무변경.

- [ ] **Step 2: PR 생성·머지**

```bash
cd C:/Users/USER/_wt-p0
git push -u origin plan/p0-futures-testbed
gh pr create --base main --head plan/p0-futures-testbed \
  --title "feat(sim): P0 테스트베드 선물화 — 증거금·롱숏·정산손익 + INV-FUT" \
  --body "선물 자동매매 설계(docs/superpowers/specs/2026-06-08-futures-auto-trading-design.md) P0. SimBroker 선물 헬퍼·증거금 잔고·INV-FUT-1/2/3·롱숏 라운드트립 시나리오. 선물 회계는 exec_defaults.instrument_spec·event_buy_qty 재사용. 주식 시나리오 32개 무변경 green. 🤖 Generated with Claude Code"
gh pr merge --merge --delete-branch
```

- [ ] **Step 3: 워크트리 정리**

```bash
cd "C:/Users/USER/Desktop/창업/퀀트/platform"
git worktree remove C:/Users/USER/_wt-p0 --force
git worktree prune
```

---

## P0 완료 기준 (Definition of Done)

- `sim/futures.py`·SimBroker 증거금·INV-FUT-1/2/3·선물 시나리오 모두 green.
- 기존 32개 주식 시나리오 무변경 green(회귀 0).
- 선물 회계는 quant_core 재사용(중복 구현 없음).
- **다음(P3 의존)**: 프로덕션 Trader ledger/_apply_fill/reconcile 선물화(side·정산·숏 reconcile)는 이 테스트베드 위에서 검증. P0는 그 토대.

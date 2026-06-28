# P2 — 브로커 N-leg (선물 단독 + per-leg 예산) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
> 상위 설계: [autotrade-asset-class-redesign.md](autotrade-asset-class-redesign.md) §3.2.
> **Task 1(골든 잠금)을 반드시 먼저** — 이후 모든 변경의 byte-identical 안전망.

**Goal.** `make_broker`/`BrokerRouter`의 "주식=필수 베이스" 가정을 풀어 **선물 단독 자동매매**를
가능하게 하고, 다계좌 선물 **per-market 예산 분리**로 과대사이징(C4)을 차단한다.

**Architecture.** 라우터의 stock leg를 **optional**로 만드는 **additive** 접근 — `_stock` 있으면
모든 분기가 기존과 동일(INV-KIS byte-identical), `_stock=None` 분기만 신설. `make_broker`는 존재하는
leg만 선언적으로 구성(stock-only→bare 유지, 선물 포함→`BrokerRouter(stock_or_None, futures)`).

**Tech Stack.** Python, pytest, SimBroker. core 무변경.

**불변식 INV-KIS.** 기존 도달 가능 KIS 조합(주식 단독 / 주식+국내선물)의 `make_broker` 반환 객체
그래프·`account_snapshot` 출력·사이징 수치가 **불변**. Task 1 골든이 잠근다. LS 주식+선물도 동일 보존.

---

## 현재 코드 (변경 기준점, origin/main 01f4c3e)

`runner.py:make_broker()` (요지):
- KIS: `if load_kis() is None: raise RuntimeError(...)` → `stock=KisBroker()` →
  `if not (load_kis_futures() or load_kis_overseas_futures()): return stock` →
  `cr=ContractResolver(); return BrokerRouter(stock, KisFuturesBroker(), resolve=cr.resolve, resolve_expiry=cr.resolve_expiry)`.
- LS: `if load_ls() is None: raise` → `stock=LsBroker()` →
  `if not (load_ls_futures() or load_ls_overseas_futures()): return stock` →
  `r=LsContractResolver(LsFuturesBroker()); return BrokerRouter(stock, r.broker, resolve=r.resolve, resolve_expiry=r.resolve_expiry, dataset_for_code=r.dataset_for_code)`.

`broker_router.py:BrokerRouter`:
- `__init__(self, stock, futures, *, resolve, resolve_expiry=None, dataset_for_code=None)` — `self._stock=stock`.
- `_broker(symbol)` (≈52): `return self._futures if self._is_fut(symbol) else self._stock`.
- `account_snapshot(overseas=True)` (≈138): `snap = self._stock.account_snapshot(overseas)` 로 시작 →
  선물 병합 루프(≈151-196). 선물 예산 합산: `out["balance"]["futures_order_cash"] += float(fut_cash)` (≈184-187).
- `__getattr__` (≈215-219): `if name.startswith("_"): raise AttributeError(name); return getattr(self._stock, name)`.

`trader.py` 선물 사이징 (≈1162): `cash = float(bal.get("futures_order_cash") or bal.get("cash") or 0)`.

---

## File Structure

- **Create** `local/tests/test_make_broker_golden.py` — INV-KIS 골든(객체 그래프).
- **Modify** `local/localapp/broker_router.py` — `_stock=None` 분기 + per-market 예산 키.
- **Modify** `local/localapp/runner.py` — `make_broker` 선언적화(존재 leg 구성).
- **Modify** `local/localapp/trader.py` — 선물 사이징이 per-market 예산 키 참조.
- **Create** `local/tests/test_broker_nleg.py` — 선물 단독 라우터 + per-market 예산 단위 테스트.

---

## Task 1: INV-KIS 골든 잠금 (먼저!)

기존 KIS 2조합의 `make_broker` 반환 구조를 캡처해 이후 변경의 회귀를 검출. **production 코드 변경 0.**

**Files:** Create `local/tests/test_make_broker_golden.py`

- [ ] **Step 1: 골든 테스트 작성** — 자격증명 로더를 스텁해 네트워크 없이 구조만 검증.

```python
"""make_broker 객체 그래프 골든 — KIS byte-identical 불변식(P2 회귀 가드).

자격증명 로더를 스텁해 네트워크 없이 make_broker의 반환 '구조'(타입·leg·resolve 콜백)를
잠근다. P2(라우터 optional stock leg)가 기존 KIS 2조합을 한 바이트도 안 바꾸는지 보장.
"""
from localapp import runner, secrets_store
from localapp.broker_router import BrokerRouter
from localapp.kis_broker import KisBroker


def _stub(monkeypatch, *, kis=None, kis_fut=None, kis_ovf=None, broker="kis"):
    monkeypatch.setattr(secrets_store, "get_active_broker", lambda: broker)
    monkeypatch.setattr(secrets_store, "load_kis", lambda: kis)
    monkeypatch.setattr(secrets_store, "load_kis_futures", lambda: kis_fut)
    monkeypatch.setattr(secrets_store, "load_kis_overseas_futures", lambda: kis_ovf)


_CREDS = {"app_key": "k", "app_secret": "s", "account_no": "123-01", "virtual": True}


def test_kis_stock_only_returns_bare_kisbroker(monkeypatch):
    _stub(monkeypatch, kis=_CREDS)
    b = runner.make_broker()
    assert type(b) is KisBroker            # bare — 라우터 미경유 (무변경)


def test_kis_stock_plus_kr_futures_returns_router(monkeypatch):
    from localapp.kis_futures_broker import KisFuturesBroker
    from quant_core.futures_contract import dataset_for_contract
    _stub(monkeypatch, kis=_CREDS, kis_fut=_CREDS)
    b = runner.make_broker()
    assert type(b) is BrokerRouter
    assert type(b._stock) is KisBroker
    assert type(b._futures) is KisFuturesBroker
    assert b._d4c is dataset_for_contract  # dataset_for_code 미주입 = 기본값 (불변식)


def test_kis_no_credentials_raises(monkeypatch):
    _stub(monkeypatch)                     # 자격증명 전무
    import pytest
    with pytest.raises(RuntimeError):
        runner.make_broker()
```

- [ ] **Step 2: 실행 — 현재(P2 전) 통과 확인** (골든은 *현재 동작*을 캡처하므로 지금 PASS여야 함)

Run: `cd local && python -m pytest tests/test_make_broker_golden.py -q`
Expected: PASS (3 passed). 만약 `KisBroker()`/`KisFuturesBroker()` __init__이 네트워크를 타서
실패하면 → **STOP, NEEDS_CONTEXT 보고**(그 경우 골든을 "타입 분기 함수"로 재설계 필요).

- [ ] **Step 3: 커밋**

```bash
git add local/tests/test_make_broker_golden.py
git commit -m "test(autotrade): make_broker INV-KIS 골든 잠금 (P2 회귀 가드)"
```

---

## Task 2: BrokerRouter `_stock=None` 지원 (additive)

`_stock` 있으면 기존 경로 무변경, `None`이면 선물 leg로 처리하는 분기만 추가.

**Files:** Modify `local/localapp/broker_router.py`; Test `local/tests/test_broker_nleg.py`

- [ ] **Step 1: 실패 테스트 작성** — `local/tests/test_broker_nleg.py`

```python
"""BrokerRouter 선물 단독(_stock=None) + per-market 예산 단위 테스트 (P2)."""
import pytest

from localapp.broker_router import BrokerRouter


class _FakeFutures:
    """국내선물 leg 더블 — account_snapshot/pending_orders/buy 최소 구현."""
    domestic_configured = True
    overseas_configured = False

    def __init__(self):
        self.bought = []

    def account_snapshot(self):
        return {"account": {"equity": 1_000_000.0, "order_cash": 500_000.0},
                "positions": [{"symbol": "A01609", "qty": 1}]}

    def pending_orders(self):
        return [{"order_no": "1", "symbol": "A01609"}]

    def buy(self, code, qty):
        self.bought.append((code, qty)); return {"order_no": "9"}


def _resolve(sym):
    return "A01609" if sym == "코스피200선물" else None


def _d4c(code):
    return "코스피200선물" if code == "A01609" else None


def test_futures_only_account_snapshot_no_stock():
    """_stock=None이어도 account_snapshot이 선물만으로 동작(AttributeError 없음)."""
    r = BrokerRouter(None, _FakeFutures(), resolve=_resolve, dataset_for_code=_d4c)
    snap = r.account_snapshot()
    assert "balance" in snap and "positions" in snap
    # 선물 포지션이 데이터셋 심볼로 정규화돼 병합됨
    assert any(p["symbol"] == "코스피200선물" for p in snap["positions"])


def test_futures_only_pending_orders_routes_to_futures():
    r = BrokerRouter(None, _FakeFutures(), resolve=_resolve, dataset_for_code=_d4c)
    assert r.pending_orders() == [{"order_no": "1", "symbol": "A01609"}]


def test_futures_only_buy_routes_to_futures():
    fut = _FakeFutures()
    r = BrokerRouter(None, fut, resolve=_resolve, dataset_for_code=_d4c)
    r.buy("코스피200선물", 1)
    assert fut.bought == [("A01609", 1)]


def test_futures_only_stock_symbol_raises_clear():
    """주식 심볼인데 stock leg 없음 → 명확한 에러(커버리지 게이트가 잡도록)."""
    r = BrokerRouter(None, _FakeFutures(), resolve=_resolve, dataset_for_code=_d4c)
    with pytest.raises(RuntimeError):
        r.buy("005930", 1)
```

- [ ] **Step 2: 실패 확인**

Run: `cd local && python -m pytest tests/test_broker_nleg.py -q`
Expected: FAIL — `_stock=None`에서 `account_snapshot`/`__getattr__`가 `AttributeError`.

- [ ] **Step 3: 구현** — `local/localapp/broker_router.py` 4개 지점에 `_stock is None` 분기 추가.

(a) `account_snapshot` 시작부 — `snap = self._stock.account_snapshot(overseas)` 를:
```python
        snap = (self._stock.account_snapshot(overseas)
                if self._stock is not None else {"balance": {}, "positions": []})
```

(b) `__getattr__` — `return getattr(self._stock, name)` 를:
```python
        target = self._stock if self._stock is not None else self._futures
        return getattr(target, name)
```

(c) `_broker(symbol)` — 비선물 심볼인데 stock 없으면 명확 에러:
```python
    def _broker(self, symbol):
        if self._is_fut(symbol):
            return self._futures
        if self._stock is None:
            raise RuntimeError(
                f"주식 자격증명 미등록 — 주식 심볼 {symbol} 거래 불가(커버리지 게이트가 차단해야 함)")
        return self._stock
```

(d) `order_status(order_no, symbol=None, hint=None)` — symbol=None이고 stock 없으면 선물로:
`self._stock.order_status(...)` 호출부(비선물 경로, ≈134)를:
```python
        base = self._stock if self._stock is not None else self._futures
        if hint is None:
            return base.order_status(order_no, symbol)
        return base.order_status(order_no, symbol, hint=hint)
```
(`pending_orders`는 미오버라이드 → (b) `__getattr__`가 자동으로 futures로 위임.)

- [ ] **Step 4: 통과 + 골든 회귀**

Run: `cd local && python -m pytest tests/test_broker_nleg.py tests/test_make_broker_golden.py -q`
Expected: PASS (선물단독 4 + 골든 3). `_stock` 있는 경로 무변경 확인.

- [ ] **Step 5: 커밋**

```bash
git add local/localapp/broker_router.py local/tests/test_broker_nleg.py
git commit -m "feat(autotrade): BrokerRouter optional stock leg — 선물 단독 지원 (P2)"
```

---

## Task 3: make_broker 선언적화 (선물 단독 구성)

**Files:** Modify `local/localapp/runner.py`; Test `local/tests/test_make_broker_golden.py` (추가)

- [ ] **Step 1: 실패 테스트 추가** — `test_make_broker_golden.py`에 선물 단독 케이스 (네트워크 회피
  위해 broker 클래스를 스텁):

```python
def test_kis_futures_only_returns_router_no_stock(monkeypatch):
    """주식 미등록 + 국내선물 등록 → BrokerRouter(_stock=None) (선물 단독 가능)."""
    import localapp.runner as R
    # 네트워크 회피: 브로커/리졸버를 가벼운 더블로 치환
    monkeypatch.setattr(R, "load_kis", lambda: None, raising=False)
    _stub(monkeypatch, kis=None, kis_fut=_CREDS)
    monkeypatch.setattr("localapp.kis_futures_broker.KisFuturesBroker.__init__",
                        lambda self: None)
    monkeypatch.setattr("localapp.futures_contracts.ContractResolver.__init__",
                        lambda self: None)
    b = runner.make_broker()
    assert type(b) is BrokerRouter
    assert b._stock is None
```
(구현자: 위 monkeypatch 경로가 실제 import 구조와 맞는지 확인 후 조정. 핵심 단언 = `_stock is None`.)

- [ ] **Step 2: 실패 확인**

Run: `cd local && python -m pytest tests/test_make_broker_golden.py::test_kis_futures_only_returns_router_no_stock -q`
Expected: FAIL — 현재 `make_broker`는 `load_kis() is None`이면 RuntimeError.

- [ ] **Step 3: 구현** — `local/localapp/runner.py` `make_broker`. KIS·LS 분기를 "존재 leg 구성"으로:

KIS 분기:
```python
    # ── KIS 경로 ── 존재하는 leg만 구성(주식 단독=bare 유지=무변경, 선물 포함=라우터) ──
    from .secrets_store import load_kis_futures, load_kis_overseas_futures
    stock = None
    if load_kis() is not None:
        from .kis_broker import KisBroker
        stock = KisBroker()
    has_fut = bool(load_kis_futures() or load_kis_overseas_futures())
    if stock is None and not has_fut:
        raise RuntimeError(
            "KIS 자격증명이 등록되지 않았습니다. setup을 실행해 페어링·KIS 키를 "
            "먼저 등록하세요. (KIS 모의투자 가입은 무료이며 즉시 발급됩니다.)")
    if not has_fut:
        return stock                           # 주식 단독 — bare (무변경)
    from .kis_futures_broker import KisFuturesBroker
    from .futures_contracts import ContractResolver
    from .broker_router import BrokerRouter
    cr = ContractResolver()
    return BrokerRouter(stock, KisFuturesBroker(),
                        resolve=cr.resolve, resolve_expiry=cr.resolve_expiry)
```
LS 분기도 동형 — `stock = LsBroker() if load_ls() else None`; `has_fut = load_ls_futures() or load_ls_overseas_futures()`; 둘 다 없으면 RuntimeError(기존 LS 메시지); 선물 없으면 `return stock`; 있으면
`r=LsContractResolver(LsFuturesBroker()); return BrokerRouter(stock, r.broker, resolve=r.resolve, resolve_expiry=r.resolve_expiry, dataset_for_code=r.dataset_for_code)`.
(기존 분기 구조를 그대로 두고 `stock` 생성을 조건부로만 바꾸는 것 — `stock` 있을 때 반환값 동일.)

- [ ] **Step 4: 통과 + 골든 전체**

Run: `cd local && python -m pytest tests/test_make_broker_golden.py -q`
Expected: PASS (기존 골든 3 + 선물단독 1 = 4). 기존 KIS 2조합 골든 무변경 = INV-KIS.

- [ ] **Step 5: 커밋**

```bash
git add local/localapp/runner.py local/tests/test_make_broker_golden.py
git commit -m "feat(autotrade): make_broker 선언적화 — 선물 단독 구성 가능 (P2)"
```

---

## Task 4: per-market 선물 예산 분리 (C4)

다계좌 선물 합산 → 시장별 분리 키. 단일 시장 계좌에선 값 동일(INV 보존).

**Files:** Modify `local/localapp/broker_router.py`(account_snapshot 병합), `local/localapp/trader.py`(사이징);
Test `local/tests/test_broker_nleg.py`(추가)

- [ ] **Step 1: 실패 테스트 추가** — `test_broker_nleg.py`. 국내+해외선물 둘 다 구성 시 예산이 합산이
  아니라 시장별 분리됨을 검증.

```python
class _DualFutures:
    domestic_configured = True
    overseas_configured = True

    def account_snapshot(self):
        return {"account": {"equity": 1_000_000.0, "order_cash": 300_000.0}, "positions": []}

    def overseas_account_snapshot(self):
        return {"account": {"equity": 2_000_000.0, "order_cash": 700_000.0}, "positions": []}


def test_per_market_budget_not_summed():
    r = BrokerRouter(None, _DualFutures(), resolve=_resolve, dataset_for_code=_d4c)
    bal = r.account_snapshot()["balance"]
    assert bal.get("futures_order_cash_kr") == 300_000.0
    assert bal.get("futures_order_cash_us") == 700_000.0
    # 합산 단일 키로 과대사이징되지 않음
    assert bal.get("futures_order_cash") in (None, 300_000.0)  # 합산(1,000,000) 아님
```

- [ ] **Step 2: 실패 확인**

Run: `cd local && python -m pytest tests/test_broker_nleg.py::test_per_market_budget_not_summed -q`
Expected: FAIL — 현재 `futures_order_cash`로 합산(1,000,000).

- [ ] **Step 3: 구현 (broker_router.py)** — `account_snapshot` 병합 루프에서 getter별로 시장 키 부여.
  현재 루프 튜플 `("account_snapshot", "domestic_configured", "futures")` / `("overseas_account_snapshot",
  "overseas_configured", "futures_overseas")`에 시장 태그를 추가하고, `order_cash`를 합산 대신
  `futures_order_cash_{kr|us}`로 기록:
```python
        for getter, cfg_attr, marker, mkt in (
                ("account_snapshot", "domestic_configured", "futures", "kr"),
                ("overseas_account_snapshot", "overseas_configured", "futures_overseas", "us")):
            ...
            fut_cash = fut_acct.get("order_cash")
            if fut_cash:
                out["balance"][f"futures_order_cash_{mkt}"] = float(fut_cash)
```
  (equity 합산 `futures_eval_krw`는 보수적이라 유지 — 변경하지 않음.)

- [ ] **Step 4: 구현 (trader.py)** — 선물 사이징(≈1162)이 시장별 키를 참조:
```python
                if qc.is_futures(symbol):
                    # 선물 주문은 해당 시장 선물계좌 가용증거금으로 사이징(시장별 분리 —
                    # 다계좌 합산 과대사이징 방지). 미배선/구브로커면 주식 cash로 graceful fallback.
                    from quant_core.futures_contract import futures_market
                    _mkt = "us" if futures_market(symbol) == "CME" else "kr"
                    cash = float(bal.get(f"futures_order_cash_{_mkt}")
                                 or bal.get("futures_order_cash")   # 구 단일 키 호환
                                 or bal.get("cash") or 0)
```
  (구 단일 키 fallback 유지 → 구 스냅샷·단일 시장 호환. 단일 kr 시장에선 `futures_order_cash_kr`==기존값.)

- [ ] **Step 5: 통과 + 전체 회귀**

Run: `cd local && python -m pytest tests/ -q`
Expected: PASS (전체). 골든 무변경 = INV-KIS.

- [ ] **Step 6: 커밋**

```bash
git add local/localapp/broker_router.py local/localapp/trader.py local/tests/test_broker_nleg.py
git commit -m "feat(autotrade): per-market 선물 예산 분리 — 다계좌 과대사이징 차단 (P2 C4)"
```

---

## Self-Review

**1. Spec coverage:** §3.2 = (a) make_broker 선언적(Task 3)·(b) router optional stock leg(Task 2)·
(c) per-leg 예산(Task 4)·INV-KIS 골든(Task 1). 전부 커버.
**2. Placeholder scan:** 없음. Task 3 monkeypatch 경로는 구현자 확인 단서 명시(핵심 단언 고정).
**3. Type consistency:** `futures_order_cash_kr`/`_us` 키·`futures_market`→kr/us 매핑이 Task 4 내 일관.
**검증 한계:** Task 1 골든이 네트워크 없이 통과하는지는 KisBroker.__init__ 거동에 의존 — 실패 시
NEEDS_CONTEXT(골든을 분기-함수로 재설계). per-market 키 변경은 단일 kr 계좌에서 기존값과 동일해야
INV-KIS 보존(Task 5 전체 회귀로 확인). 선물 단독 자동매매 실거동은 사장님 모의 1회 필요(P2 완료 후).

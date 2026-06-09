"""BrokerRouter — 주식/선물 심볼 라우팅 + 선물 계약코드 해석 단위검증(M3).

거래 메서드(buy/sell/limit/price/today_open/cancel/order_status)는 심볼로 라우팅하고,
선물은 데이터셋 심볼(한글 상품명)을 계약코드로 해석해 전달. 주식 전용 메서드
(account_snapshot 변형·buying_power_usd 등)는 stock 브로커로 위임(__getattr__).
가짜 브로커 + 스텁 resolver로 네트워크 없이 검증.
"""
from __future__ import annotations

from datetime import date

import pytest

from localapp.broker_router import BrokerRouter


class _Fake:
    def __init__(self, tag):
        self.tag = tag
        self.calls = []

    def buy(self, symbol, qty):
        self.calls.append(("buy", symbol, qty)); return {"tag": self.tag}

    def sell(self, symbol, qty):
        self.calls.append(("sell", symbol, qty)); return {"tag": self.tag}

    def buy_limit(self, symbol, qty, limit_price):
        self.calls.append(("buy_limit", symbol, qty, limit_price)); return {"tag": self.tag}

    def sell_limit(self, symbol, qty, limit_price):
        self.calls.append(("sell_limit", symbol, qty, limit_price)); return {"tag": self.tag}

    def buy_resv_limit(self, symbol, qty, limit_price):
        self.calls.append(("buy_resv_limit", symbol, qty, limit_price)); return {"tag": self.tag}

    def sell_resv_moo(self, symbol, qty):
        self.calls.append(("sell_resv_moo", symbol, qty)); return {"tag": self.tag}

    def price(self, symbol):
        self.calls.append(("price", symbol)); return 100.0

    def today_open(self, symbol):
        self.calls.append(("today_open", symbol)); return 99.0

    def cancel(self, order_no, symbol, qty):
        self.calls.append(("cancel", order_no, symbol, qty)); return {"tag": self.tag}


class _FakeStock(_Fake):
    # 주식 전용 메서드 + order_status 2-arg
    def account_snapshot(self, overseas=True):
        self.calls.append(("account_snapshot", overseas)); return {"balance": {"cash": 1}, "positions": []}

    def buying_power_usd(self, symbol, ref_price):
        self.calls.append(("buying_power_usd", symbol, ref_price)); return {"usd_orderable": 0}

    def order_status(self, order_no, symbol=None):
        self.calls.append(("order_status", order_no, symbol)); return {"tag": "stock"}

    def pending_orders(self):
        self.calls.append(("pending_orders",)); return []


class _FakeFutures(_Fake):
    # 선물 order_status는 1-arg
    def __init__(self, tag):
        super().__init__(tag)
        self.dom_positions: list[dict] = []   # 국내선물 잔고(계약코드 키)
        self.ov_positions: list[dict] = []     # 해외선물 잔고(globex 키)

    def order_status(self, order_no):
        self.calls.append(("order_status", order_no)); return {"tag": "fut"}

    def overseas_order_status(self, order_no):
        self.calls.append(("overseas_order_status", order_no)); return {"tag": "ov"}

    def overseas_price(self, symbol, scalc_desz=0):
        self.calls.append(("overseas_price", symbol)); return 1950.0

    # 해외선물 주문 메서드(P2) — CME 심볼은 라우터가 이쪽으로 dispatch
    def overseas_buy(self, symbol, qty):
        self.calls.append(("overseas_buy", symbol, qty)); return {"tag": self.tag}

    def overseas_sell(self, symbol, qty):
        self.calls.append(("overseas_sell", symbol, qty)); return {"tag": self.tag}

    def overseas_buy_limit(self, symbol, qty, limit_price):
        self.calls.append(("overseas_buy_limit", symbol, qty, limit_price)); return {"tag": self.tag}

    def overseas_sell_limit(self, symbol, qty, limit_price):
        self.calls.append(("overseas_sell_limit", symbol, qty, limit_price)); return {"tag": self.tag}

    # 잔고(국내=parse_futures_balance shape, 해외=parse_overseas_balance shape)
    def account_snapshot(self):
        self.calls.append(("fut_account_snapshot",)); return {"positions": list(self.dom_positions), "account": {}}

    def overseas_account_snapshot(self):
        self.calls.append(("ov_account_snapshot",)); return {"positions": list(self.ov_positions)}


def _router(resolve_expiry=None):
    stock, fut = _FakeStock("stock"), _FakeFutures("fut")
    # 스텁 resolver: 선물 한글명 → 계약코드
    codes = {"금선물": "GCM26", "코스피200선물": "A01606"}
    return (BrokerRouter(stock, fut, resolve=lambda s: codes.get(s),
                         resolve_expiry=resolve_expiry),
            stock, fut)


# ── 라우팅 ────────────────────────────────────────────────────────────────────
def test_equity_buy_routes_to_stock_unchanged():
    r, stock, fut = _router()
    r.buy("005930", 10)
    assert ("buy", "005930", 10) in stock.calls
    assert fut.calls == []


def test_domestic_futures_buy_routes_to_domestic_method():
    # 국내선물(코스피200·KRX) → domestic buy + 계약코드 해석
    r, stock, fut = _router()
    r.buy("코스피200선물", 2)
    assert ("buy", "A01606", 2) in fut.calls
    assert stock.calls == []


def test_overseas_futures_buy_routes_to_overseas_method():
    # 해외선물(금선물·CME) → overseas_buy(해외 컨텍스트) — domestic buy 오발주 방지(M7 갭 수정)
    r, stock, fut = _router()
    r.buy("금선물", 2)
    assert ("overseas_buy", "GCM26", 2) in fut.calls
    assert ("buy", "GCM26", 2) not in fut.calls
    assert stock.calls == []


def test_overseas_futures_limit_routes_to_overseas():
    r, stock, fut = _router()
    r.buy_limit("금선물", 1, 1950.0)
    r.sell_limit("금선물", 1, 1960.0)
    assert ("overseas_buy_limit", "GCM26", 1, 1950.0) in fut.calls
    assert ("overseas_sell_limit", "GCM26", 1, 1960.0) in fut.calls


def test_domestic_futures_limit_and_price_resolve_code():
    r, stock, fut = _router()
    r.buy_limit("코스피200선물", 1, 350.0)
    r.price("코스피200선물")
    assert ("buy_limit", "A01606", 1, 350.0) in fut.calls
    assert ("price", "A01606") in fut.calls


def test_futures_resolve_failure_raises_skip():
    r, stock, fut = _router()
    with pytest.raises(RuntimeError):
        r.buy("원유선물", 1)        # resolver 스텁에 없음 → None → 발주 skip


def test_cancel_and_order_status_routing():
    r, stock, fut = _router()
    # 주식·국내선물(코스피200·KRX) cancel은 그대로 라우팅
    r.cancel("ORD1", "005930", 10)
    r.cancel("ORD3", "코스피200선물", 1)
    assert ("cancel", "ORD1", "005930", 10) in stock.calls
    assert ("cancel", "ORD3", "A01606", 1) in fut.calls
    # order_status: 주식 2-arg, 국내선물 1-arg
    r.order_status("ORD1", "005930")
    r.order_status("ORD3", "코스피200선물")
    assert ("order_status", "ORD1", "005930") in stock.calls
    assert ("order_status", "ORD3") in fut.calls


# ── 해외선물(CME) phase2 dispatch — order_status·cancel·price (M10 라이브 완성) ────
def test_overseas_order_status_routes_to_overseas():
    r, stock, fut = _router()
    r.order_status("ORD2", "금선물")
    assert ("overseas_order_status", "ORD2") in fut.calls   # CME → inquire-ccld
    assert ("order_status", "ORD2") not in fut.calls         # 국내 ccnl 아님


def test_overseas_cancel_blocked_needs_orgn_ord_dt():
    # 해외선물 취소는 ORGN_ORD_DT 필요 → 라우터 취소는 명시 차단(국내로 오라우팅 방지)
    import pytest
    r, stock, fut = _router()
    with pytest.raises(NotImplementedError):
        r.cancel("ORD2", "금선물", 1)


def test_overseas_price_routes_to_overseas():
    r, stock, fut = _router()
    px = r.price("금선물")
    assert ("overseas_price", "GCM26") in fut.calls and px == 1950.0
    assert ("price", "GCM26") not in fut.calls               # 국내 시세 아님


def test_domestic_futures_price_unchanged():
    r, stock, fut = _router()
    r.price("코스피200선물")
    assert ("price", "A01606") in fut.calls                  # KRX는 기존 domestic 시세


# ── 주식 전용 메서드 위임 (__getattr__) ────────────────────────────────────────
def test_account_snapshot_delegated_to_stock_with_kwarg():
    r, stock, fut = _router()
    r.account_snapshot(overseas=False)
    assert ("account_snapshot", False) in stock.calls


def test_buying_power_and_pending_delegated_to_stock():
    r, stock, fut = _router()
    r.buying_power_usd("AAPL", 100.0)
    r.pending_orders()
    assert ("buying_power_usd", "AAPL", 100.0) in stock.calls
    assert ("pending_orders",) in stock.calls


def test_no_futures_broker_routes_all_to_stock():
    stock = _FakeStock("stock")
    r = BrokerRouter(stock, None, resolve=lambda s: None)
    r.buy("금선물", 1)              # 선물브로커 없으면 선물도 stock으로(라우터 미사용 상정 방어)
    assert ("buy", "금선물", 1) in stock.calls


# ── account_snapshot 병합 + 심볼 정규화 (M7 — 선물 청산/reconcile이 인식하도록) ────
def test_snapshot_merges_domestic_futures_normalized_to_dataset_symbol():
    r, stock, fut = _router()
    fut.dom_positions = [{"symbol": "A01606", "side": "buy", "qty": 2, "avg_price": 350.0}]
    snap = r.account_snapshot()
    pos = [p for p in snap["positions"] if p["symbol"] == "코스피200선물"]
    assert len(pos) == 1                                  # 계약코드 A01606 → 데이터셋 심볼
    assert pos[0]["contract_code"] == "A01606" and pos[0]["qty"] == 2
    assert pos[0]["asset_class"] == "futures"


def test_snapshot_merges_overseas_futures_normalized():
    r, stock, fut = _router()
    fut.ov_positions = [{"symbol": "GCM26", "side": "short", "qty": 1, "avg_price": 1950.0}]
    snap = r.account_snapshot()
    pos = [p for p in snap["positions"] if p["symbol"] == "금선물"]
    assert len(pos) == 1 and pos[0]["contract_code"] == "GCM26" and pos[0]["side"] == "short"


def test_snapshot_merges_futures_order_cash_and_equity():
    # 선물계좌 잔고의 equity(추정예탁자산)·order_cash(주문가능증거금현금)를 balance에 노출.
    # order_cash = 선물 사이징 예산 base(trader). equity = kill-switch 통합자산. 주식 현금과 분리.
    r, stock, fut = _router()
    fut.account_snapshot = lambda: {"positions": [],
                                    "account": {"equity": 5.0e8, "order_cash": 4.4e8}}
    bal = r.account_snapshot()["balance"]
    assert bal["futures_eval_krw"] == 5.0e8
    assert bal["futures_order_cash"] == 4.4e8
    assert bal["cash"] == 1                # 주식 현금은 별개로 보존(선물 사이징에 안 씀)


def test_snapshot_no_futures_broker_unchanged():
    stock = _FakeStock("stock")
    r = BrokerRouter(stock, None, resolve=lambda s: None)
    assert r.account_snapshot() == {"balance": {"cash": 1}, "positions": []}   # stock 그대로


def test_snapshot_skips_failed_futures_context_without_crash():
    # 미구성/통신 실패 컨텍스트(예외) → 그 컨텍스트만 skip, 국내는 병합·크래시 없음
    r, stock, fut = _router()
    fut.dom_positions = [{"symbol": "A01606", "side": "buy", "qty": 1}]

    def _boom():
        raise RuntimeError("해외선물 미구성")
    fut.overseas_account_snapshot = _boom
    snap = r.account_snapshot()
    assert any(p["symbol"] == "코스피200선물" for p in snap["positions"])


# ── contract_expiry — 진입 시 ledger 기록용 (계약코드+만기일, M6) ──────────────
def test_contract_expiry_futures_returns_code_and_date():
    exp = {"금선물": ("GCM26", date(2026, 6, 26))}
    r, _, _ = _router(resolve_expiry=lambda s: exp.get(s, (None, None)))
    assert r.contract_expiry("금선물") == ("GCM26", date(2026, 6, 26))


def test_contract_expiry_equity_returns_none():
    # 주식은 선물 아님 → (None, None) (resolver 호출 안 함)
    r, _, _ = _router(resolve_expiry=lambda s: ("X", date(2026, 1, 1)))
    assert r.contract_expiry("005930") == (None, None)


def test_contract_expiry_without_resolver_returns_none():
    # resolve_expiry 미주입(구 배선 호환) → (None, None) → Trader는 만기 미기록
    r, _, _ = _router(resolve_expiry=None)
    assert r.contract_expiry("금선물") == (None, None)


def test_contract_expiry_swallows_resolver_error():
    def _boom(s):
        raise RuntimeError("마스터 다운로드 실패")
    r, _, _ = _router(resolve_expiry=_boom)
    assert r.contract_expiry("금선물") == (None, None)   # 실패해도 진입 자체는 막지 않음

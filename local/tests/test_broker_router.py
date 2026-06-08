"""BrokerRouter — 주식/선물 심볼 라우팅 + 선물 계약코드 해석 단위검증(M3).

거래 메서드(buy/sell/limit/price/today_open/cancel/order_status)는 심볼로 라우팅하고,
선물은 데이터셋 심볼(한글 상품명)을 계약코드로 해석해 전달. 주식 전용 메서드
(account_snapshot 변형·buying_power_usd 등)는 stock 브로커로 위임(__getattr__).
가짜 브로커 + 스텁 resolver로 네트워크 없이 검증.
"""
from __future__ import annotations

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
    def order_status(self, order_no):
        self.calls.append(("order_status", order_no)); return {"tag": "fut"}


def _router():
    stock, fut = _FakeStock("stock"), _FakeFutures("fut")
    # 스텁 resolver: 선물 한글명 → 계약코드
    codes = {"금선물": "GCM26", "코스피200선물": "A01606"}
    return BrokerRouter(stock, fut, resolve=lambda s: codes.get(s)), stock, fut


# ── 라우팅 ────────────────────────────────────────────────────────────────────
def test_equity_buy_routes_to_stock_unchanged():
    r, stock, fut = _router()
    r.buy("005930", 10)
    assert ("buy", "005930", 10) in stock.calls
    assert fut.calls == []


def test_futures_buy_routes_to_futures_with_contract_code():
    r, stock, fut = _router()
    r.buy("금선물", 2)
    assert ("buy", "GCM26", 2) in fut.calls      # 한글명→계약코드 해석
    assert stock.calls == []


def test_futures_limit_and_price_resolve_code():
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
    r.cancel("ORD1", "005930", 10)
    r.cancel("ORD2", "금선물", 2)
    assert ("cancel", "ORD1", "005930", 10) in stock.calls
    assert ("cancel", "ORD2", "GCM26", 2) in fut.calls
    # order_status: 주식 2-arg, 선물 1-arg
    r.order_status("ORD1", "005930")
    r.order_status("ORD2", "금선물")
    assert ("order_status", "ORD1", "005930") in stock.calls
    assert ("order_status", "ORD2") in fut.calls


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

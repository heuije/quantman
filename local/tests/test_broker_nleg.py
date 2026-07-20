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

    def pending_orders(self, *, strict: bool = False):
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
    assert any(p["symbol"] == "코스피200선물" for p in snap["positions"])


def test_futures_only_pending_orders_routes_to_futures():
    # R2-③: pending도 positions(account_snapshot)와 같은 규약 — 계약코드를
    # 데이터셋 심볼로 정규화해 반환하되, §19 A1 키 매칭용 원시 계약코드를
    # contract_code로 보존한다(C1 — 2026-07-19 감사: 정규화가 키를 지워
    # A1이 라이브 무동작이었다).
    r = BrokerRouter(None, _FakeFutures(), resolve=_resolve, dataset_for_code=_d4c)
    assert r.pending_orders() == [{"order_no": "1", "symbol": "코스피200선물",
                                   "contract_code": "A01609"}]


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
    # 합산 단일 키로 과대사이징되지 않음 (1,000,000 합산 아님)
    assert bal.get("futures_order_cash") in (None, 300_000.0)

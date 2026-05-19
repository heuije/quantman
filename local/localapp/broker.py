"""브로커 추상화.

Trader는 Broker 인터페이스에만 의존한다 — 실거래(KisBroker)와
체험/검증(MockBroker)을 같은 트레이딩 로직으로 구동한다.
"""

from __future__ import annotations

from typing import Callable, Protocol


class Broker(Protocol):
    def account_snapshot(self) -> dict: ...
    def price(self, symbol: str) -> float: ...
    def buy(self, symbol: str, qty: int) -> dict: ...
    def sell(self, symbol: str, qty: int) -> dict: ...


class MockBroker:
    """메모리 기반 모의 브로커 — 검증 및 'KIS 연결 없이 체험' 모드용."""

    def __init__(self, cash: float, price_fn: Callable[[str], float]):
        self._cash = float(cash)
        self._price_fn = price_fn
        self._positions: dict[str, dict] = {}   # symbol -> {qty, avg_price}

    def price(self, symbol: str) -> float:
        return float(self._price_fn(symbol))

    def buy(self, symbol: str, qty: int) -> dict:
        px = self.price(symbol)
        cost = px * qty
        if cost > self._cash:
            return {"success": False, "message": "예수금 부족"}
        self._cash -= cost
        pos = self._positions.get(symbol, {"qty": 0, "avg_price": 0.0})
        total = pos["qty"] + qty
        pos["avg_price"] = (pos["avg_price"] * pos["qty"] + cost) / total
        pos["qty"] = total
        self._positions[symbol] = pos
        return {"success": True, "message": "체결", "price": px, "qty": qty}

    def sell(self, symbol: str, qty: int) -> dict:
        pos = self._positions.get(symbol)
        if not pos or pos["qty"] < qty:
            return {"success": False, "message": "보유 수량 부족"}
        px = self.price(symbol)
        self._cash += px * qty
        pos["qty"] -= qty
        if pos["qty"] == 0:
            del self._positions[symbol]
        return {"success": True, "message": "체결", "price": px, "qty": qty}

    def account_snapshot(self) -> dict:
        positions = []
        eval_total = self._cash
        for sym, pos in self._positions.items():
            px = self.price(sym)
            eval_total += px * pos["qty"]
            positions.append({
                "symbol": sym, "qty": pos["qty"],
                "avg_price": round(pos["avg_price"], 2),
                "eval_price": round(px, 2),
            })
        return {
            "balance": {"cash": round(self._cash), "total_eval": round(eval_total)},
            "positions": positions,
        }

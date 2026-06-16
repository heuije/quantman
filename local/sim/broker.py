"""SimBroker — Broker Protocol의 결정론적 구현(오프라인 테스트용).

발주를 기록하고 KIS 형식(zero-padded-10) ODNO를 발급한다. 체결은 자동으로
일어나지 않으며, 시나리오가 exec_event()(WS 통보 주입용) 또는 mark_filled()
(REST 조회 응답용)로 명시 구동한다 — 결정론 보장. 실제 KIS 응답 형식이 확정되면
fixtures/로 교체(record-replay, P3).
"""
from __future__ import annotations


class SimBroker:
    def __init__(self, balance: dict | None = None, prices: dict | None = None):
        self._balance = balance or {"cash": 10_000_000, "total_eval": 10_000_000,
                                     "foreign_eval_krw": 0, "cash_usd": 0, "fx_usdkrw": 0}
        self._prices = dict(prices or {})
        self._positions: list[dict] = []
        self._margin: dict | None = None
        self.submitted: list[dict] = []
        self._statuses: dict[str, dict] = {}
        self._n = 0

    def _order(self, side: str, symbol: str, qty: int, limit: float = 0) -> dict:
        self._n += 1
        odno = f"{self._n:010d}"
        self.submitted.append({"side": side, "symbol": symbol, "qty": qty,
                                "limit": limit, "order_no": odno})
        self._statuses[odno] = {"order_no": odno, "status": "submitted",
                                 "filled_qty": 0, "remain_qty": qty, "fill_price": 0.0}
        return {"success": True, "order_no": odno, "filled_qty": 0, "price": 0}

    # ── Broker Protocol ──
    def account_snapshot(self, overseas: bool = True) -> dict:
        # overseas — 실 KisBroker는 국내/해외 조회를 가르지만 Sim은 단일 잔고. 인자만 수용.
        snap = {"balance": dict(self._balance), "positions": list(self._positions)}
        if self._margin is not None:          # 선물 모드에서만 증거금 노출(주식 무변경)
            snap["margin"] = dict(self._margin)
        return snap

    def price(self, symbol: str) -> float:
        return float(self._prices.get(symbol, 0.0))

    def today_open(self, symbol: str) -> float:
        return float(self._prices.get(symbol, 0.0))

    def buy(self, symbol, qty): return self._order("buy", symbol, qty)
    def sell(self, symbol, qty): return self._order("sell", symbol, qty)
    def buy_limit(self, symbol, qty, limit_price): return self._order("buy", symbol, qty, limit_price)
    def sell_limit(self, symbol, qty, limit_price): return self._order("sell", symbol, qty, limit_price)
    def buy_resv_limit(self, symbol, qty, limit_price): return self._order("buy", symbol, qty, limit_price)
    def sell_resv_limit(self, symbol, qty, limit_price): return self._order("sell", symbol, qty, limit_price)

    def cancel(self, order_no, symbol, qty) -> dict:
        if order_no in self._statuses:
            self._statuses[order_no]["status"] = "cancelled"
        return {"success": True, "order_no": order_no}

    def order_status(self, order_no, symbol=None, hint=None) -> dict:
        # hint — 해외 예약주문 매칭 보조(KisBroker 전용). Sim은 단일 번호공간이라 무시.
        return self._statuses.get(order_no, {"order_no": order_no, "status": "unknown",
                                              "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0})

    def pending_orders(self) -> list[dict]:
        return [dict(s) for s in self._statuses.values()
                if s["status"] in ("submitted", "partial")]

    # ── 시나리오 제어 ──
    def exec_event(self, order_no: str, qty: int, price: float,
                    hour: str = "100000") -> dict:
        """_on_exec_event에 주입할 H0STCNI0 체결 통보 이벤트 구성."""
        o = next(s for s in self.submitted if s["order_no"] == order_no)
        return {"CNTG_YN": "2", "ODER_NO": order_no, "STCK_SHRN_ISCD": o["symbol"],
                "CNTG_QTY": str(qty), "CNTG_UNPR": str(price),
                "STCK_CNTG_HOUR": hour, "RFUS_YN": ""}

    def mark_filled(self, order_no: str, qty: int, price: float) -> None:
        """REST 조회(order_status)가 filled를 반환하도록 상태 갱신."""
        s = self._statuses[order_no]
        s.update(status="filled", filled_qty=qty, remain_qty=0, fill_price=price)

    def set_positions(self, positions: list[dict]) -> None:
        """account_snapshot()이 반환할 KIS 보유 포지션을 설정(reconcile 시나리오용).

        각 항목: {symbol, qty, avg_price, ...} — analytics.reconcile_ledger가 symbol로 매칭.
        """
        self._positions = list(positions)

    def set_margin(self, total_margin: float, available_margin: float) -> None:
        """선물 시나리오용 증거금 잔고 설정. account_snapshot()['margin']로 노출."""
        self._margin = {"total_margin": float(total_margin),
                        "available_margin": float(available_margin)}

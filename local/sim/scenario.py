"""시나리오 구동 helper — 주어진 (trader, SimBroker)를 하루 흐름으로 움직인다.

production 코드를 그대로 호출한다: _on_exec_event(실 WS 경로), trader._resolve_pending /
reconcile_with_kis(실 settlement 경로). sim은 seam에만 끼며 로직을 재구현하지 않는다.
"""
from __future__ import annotations

from localapp import intraday_loop


def inject_ws_fill(trader, broker, order_no: str, qty: int, price: float,
                    hour: str = "100000") -> None:
    """체결 통보 WS 경로로 체결을 반영(실 _on_exec_event 구동)."""
    evt = broker.exec_event(order_no, qty, price, hour)
    intraday_loop._on_exec_event(trader, broker, evt)


def buy_and_fill(trader, broker, sid: str, symbol: str, qty: int, price: float,
                  strat_name: str = "T") -> str:
    """매수 발주(SUBMITTED→PENDING) → WS 전량 체결(PENDING→FILLED). order_no 반환."""
    r = broker.buy_limit(symbol, qty, int(price))
    trader._after_submit(r, sid, strat_name, {}, symbol, "buy", qty, price, int(price),
                          {"use_limit": True, "buy_tolerance_pct": 1.0}, [], reason="매수신호")
    inject_ws_fill(trader, broker, r["order_no"], qty, price)
    return r["order_no"]


def strategy_buy_and_fill(trader, broker, sid: str, strat_def: dict, symbol: str,
                          dataset: dict, fill_price: float,
                          equity: float = 10_000_000.0):
    """**결정경로** 매수 — `_try_buy_one_symbol`(IR 사이징)을 실제로 구동.

    buy_and_fill은 선정·사이징을 건너뛰고 직접 발주하지만, 이 헬퍼는 사이징까지 production
    코드로 돌려 IR `event_buy_qty`를 검증한다. 발주되면 전량 WS 체결까지 진행.
    (order_no, decisions) 반환 — 발주 안 됐으면 order_no=None.
    """
    decisions: list[dict] = []
    # 가격 평면 정책 — 즉시 지정가는 broker 실시간 시세(quote_band) 기준이라
    # sim 시세 미설정이면 발주가 정당하게 보류된다. 시나리오의 체결가를 시세로 등록.
    broker._prices.setdefault(symbol, fill_price)
    n_before = len(broker.submitted)
    trader._try_buy_one_symbol(sid, sid, strat_def.get("name", ""), strat_def,
                               symbol, dataset, equity, decisions)
    if len(broker.submitted) == n_before:
        return None, decisions
    last = broker.submitted[-1]
    inject_ws_fill(trader, broker, last["order_no"], last["qty"], fill_price)
    return last["order_no"], decisions


def run_settlement(trader, broker, today_iso: str) -> None:
    """장 마감 후 정산 경로(실 trader 메서드 구동)."""
    decisions: list[dict] = []
    trader._resolve_pending(decisions)
    trader.reconcile_with_kis(today_iso=today_iso)

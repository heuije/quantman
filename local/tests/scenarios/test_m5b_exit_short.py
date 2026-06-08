"""M5b — 숏 청산 방향(buy-to-close) + _is_reserved_us 선물 제외 + held_qty side 인지.

숏 포지션 청산은 매수(환매). 선물은 예약주문 미사용(버그 수정). over-close 클램프 side 인지.
"""
from __future__ import annotations

from quant_core.exec_defaults import merged_execution

from localapp.trader import held_qty_from_snapshot


def test_is_reserved_us_excludes_futures(isolated_trader):
    trader, _ = isolated_trader
    trader._reserved_us = True
    # 선물(USD 해외·국내 모두)은 예약 제외 — 미국주식은 예약 유지(버그 수정 전엔 해외선물이 걸려 깨짐).
    assert trader._is_reserved_us("금선물") is False
    assert trader._is_reserved_us("코스피200선물") is False
    assert trader._is_reserved_us("AAPL") is True


def test_held_qty_side_aware():
    snap = {"positions": [
        {"symbol": "금선물", "qty": 2, "side": "long"},
        {"symbol": "금선물", "qty": 1, "side": "short"},
    ]}
    assert held_qty_from_snapshot(snap, "금선물", "long") == 2
    assert held_qty_from_snapshot(snap, "금선물", "short") == 1
    # 주식(side 키 없음) → long 기본 포함 (종전 동작 보존)
    assert held_qty_from_snapshot({"positions": [{"symbol": "005930", "qty": 10}]}, "005930") == 10


def test_submit_close_short_places_buy_and_closes(isolated_trader):
    trader, broker = isolated_trader
    # 숏 포지션을 ledger에 직접 구성(M5c 진입 배선 전이라 합성).
    trader.ledger["s2"] = {"symbol": "금선물", "qty": 1, "entry_price": 1950.0,
                            "side": "short", "entry_date": "2026-06-01",
                            "peak_price": 1950.0, "strategy_name": "t", "definition": {}}
    policy = merged_execution(None)
    trader._submit_close_short("s2", "t", "금선물", 1, 1900.0, policy, "청산", [])

    # 환매 = 매수 주문이 pending에 등록(side="buy")
    buys = [p for p in trader.pending.values() if p["side"] == "buy" and p["symbol"] == "금선물"]
    assert len(buys) == 1
    order_no = buys[0]["order_no"]

    # 체결 → buy-to-close: 숏 청산. ledger 제거(M4 _apply_fill이 숏 환매로 해석).
    broker.mark_filled(order_no, 1, 1900.0)
    trader._resolve_pending([])
    assert "s2" not in trader.ledger

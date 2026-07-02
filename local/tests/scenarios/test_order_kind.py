"""주문 로그의 진입/청산(kind) 구조적 기록 — 발주 메서드가 명시하고 체결까지 전파.

side(매수/매도)만으론 숏 진입=매도·롱 청산=매도라 구분 불가하므로, 4개 발주 메서드가
kind를 명시한다: _submit_buy·_submit_open_short=진입, _submit_sell·_submit_close_short=청산.
orders.jsonl 각 행에 실려 주문 내역 화면이 행 색(진입/청산)에 쓴다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import order_log
from sim import scenario

_POLICY = {"buy_tolerance_pct": 1.0, "sell_tolerance_pct": 1.0}


def test_submit_methods_record_kind(isolated_trader):
    """4개 발주 메서드 각각의 submitted 행에 진입/청산 kind가 기록된다."""
    t, broker = isolated_trader
    broker._prices["005930"] = 70000.0
    broker._prices["코스피200선물"] = 375.0
    dec: list = []
    t._submit_buy("s1", "롱전략", {}, "005930", 1, 70000.0, _POLICY, dec)          # 진입
    t._submit_sell("s1", "롱전략", "005930", 1, 70000.0, _POLICY, "당일청산", dec)  # 청산
    t._submit_open_short("s2", "숏전략", {}, "코스피200선물", 1, 375.0, _POLICY, dec)  # 진입
    t._submit_close_short("s2", "숏전략", "코스피200선물", 1, 375.0, _POLICY,
                          "보유기간", dec)                                          # 청산

    kind_by_reason = {r["reason"]: r.get("kind")
                      for r in order_log.read_orders(50) if r["event"] == "submitted"}
    assert kind_by_reason.get("매수신호") == "진입", kind_by_reason
    assert kind_by_reason.get("당일청산") == "청산", kind_by_reason
    assert kind_by_reason.get("숏진입") == "진입", kind_by_reason
    assert kind_by_reason.get("보유기간") == "청산", kind_by_reason


def test_kind_propagates_to_fill(isolated_trader):
    """진입 발주가 체결되면 filled 행에도 kind='진입'이 전파된다(pending 레코드 경유)."""
    t, broker = isolated_trader
    broker._prices["005930"] = 70000.0
    t._submit_buy("s1", "롱전략", {}, "005930", 1, 70000.0, _POLICY, [])
    order = broker.submitted[-1]
    scenario.inject_ws_fill(t, broker, order["order_no"], 1, 70000.0)
    filled = [r for r in order_log.read_orders(50) if r["event"] == "filled"]
    assert filled and filled[0].get("kind") == "진입", filled

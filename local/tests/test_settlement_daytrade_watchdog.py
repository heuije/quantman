"""정산 당일매매 잔존 감시(I5) — Trader.daytrade_unclosed.

불변식: 장 마감 후 정산 시점에 당일매매(hold_days==0) 포지션이 원장에 남아 있으면 안 된다.
2026-07-02 KRX 종가창 cron 미발화가 무감지로 지나가 일중 숏이 의도치 않게 오버나이트로
넘어간 인시던트의 감지 회귀 테스트. 상태 기반이라 미실행·발주 거부 등 원인 무관하게 잡는다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp.trader import Trader


def _t():
    return Trader(broker=None)     # daytrade_unclosed는 ledger만 읽는다


def _pos(symbol, qty, hold_days, side="short"):
    return {"symbol": symbol, "qty": qty, "side": side,
            "definition": {"position": {"exit": {"hold_days": hold_days}}},
            "strategy_name": "테스트", "entry_date": "2026-07-02", "entry_price": 1300.0}


def test_hold0_remaining_at_settlement_detected():
    t = _t()
    t.ledger = {"17": _pos("코스피200선물", 4, hold_days=0)}
    out = t.daytrade_unclosed("KRX")
    assert [(p["sid"], p["symbol"], p["qty"]) for p in out] == [("17", "코스피200선물", 4)]


def test_holding_period_positions_not_flagged():
    # hold_days≥1(보유기간)·hold_days 미지정(상시 보유)은 정산 잔존이 정상.
    t = _t()
    t.ledger = {"18": _pos("코스피200선물", 7, hold_days=1),
                "s1": {"symbol": "005930", "qty": 10, "side": "long",
                       "definition": {"position": {"exit": {}}}}}
    assert t.daytrade_unclosed("KRX") == []


def test_other_market_not_flagged():
    # US hold0 포지션은 KRX 정산 감시 대상이 아니다(시장별 정산이 각자 검사).
    t = _t()
    t.ledger = {"u1": _pos("AAPL", 5, hold_days=0, side="long")}
    assert t.daytrade_unclosed("KRX") == []
    assert [p["sid"] for p in t.daytrade_unclosed("US")] == ["u1"]


def test_zero_qty_not_flagged():
    t = _t()
    t.ledger = {"17": _pos("코스피200선물", 0, hold_days=0)}
    assert t.daytrade_unclosed("KRX") == []

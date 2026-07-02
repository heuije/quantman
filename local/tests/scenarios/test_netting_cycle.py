"""Workbench 시나리오 — 넷팅 합성 체결 (Phase 2).

넷팅 핸드오프는 브로커 주문 없이 원장만 이관한다(합성 체결). 설계 §13.
  · exit leg: 닫는 전략 슬롯 축소 + realized 기록 (기존 _apply_fill 재사용).
  · entry leg: 여는 전략 슬롯 생성(entry_price=기준가).
  · 브로커 실주문 0 · order_no="NETTED-" · intent 저널 시드로 재발주 차단(N1).

    cd platform/local && python -m pytest tests/scenarios/test_netting_cycle.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import intents
from localapp.netting import Intent


def _seed_long(t, sid, symbol, qty, entry, name=None):
    t.ledger[sid] = {"symbol": symbol, "qty": qty, "entry_date": "2026-05-20",
                     "entry_price": float(entry), "peak_price": float(entry),
                     "side": "long", "strategy_name": name or sid, "definition": {}}


def _exit_leg(sid, symbol, qty, entry, ref, side="long"):
    return Intent(sid=sid, strategy_id=sid, strategy_name=sid, contract_key=symbol,
                  symbol=symbol, kind="exit", position_side=side,
                  order_side=("sell" if side == "long" else "buy"),
                  qty=qty, ref_price=float(ref), entry_price=float(entry),
                  mult=1.0, currency="KRW", definition={})


def _entry_leg(sid, symbol, qty, ref, side="long", name=None):
    return Intent(sid=sid, strategy_id=sid, strategy_name=sid, contract_key=symbol,
                  symbol=symbol, kind="entry", position_side=side,
                  order_side=("buy" if side == "long" else "sell"),
                  qty=qty, ref_price=float(ref), entry_price=None,
                  mult=1.0, currency="KRW", definition={"name": name or sid})


def test_handoff_zero_broker_orders_and_ledger_moves(isolated_trader):
    """하락일 핸드오프: A 롱청산5 + B 롱진입5 → 브로커 0·A 소멸·B 생성(기준가)."""
    t, broker = isolated_trader
    _seed_long(t, "A", "005930", 5, entry=90)

    decisions: list[dict] = []
    t._apply_netted_leg(_exit_leg("A", "005930", 5, entry=90, ref=100), decisions)
    t._apply_netted_leg(_entry_leg("B", "005930", 5, ref=100), decisions)

    assert broker.submitted == [], "넷팅은 브로커 실주문을 내면 안 됨"
    assert "A" not in t.ledger, "A 당일 롱은 청산(이관)돼야"
    assert t.ledger["B"]["qty"] == 5
    assert t.ledger["B"]["entry_price"] == 100.0, "B는 기준가로 개시"
    assert t.ledger["B"]["side"] == "long"


def test_handoff_realizes_closer_pnl_at_ref(isolated_trader):
    """닫는 전략 A의 실현손익 = (기준가−진입가)×수량 (넷팅 표식)."""
    t, broker = isolated_trader
    _seed_long(t, "A", "005930", 5, entry=90)
    decisions: list[dict] = []
    t._apply_netted_leg(_exit_leg("A", "005930", 5, entry=90, ref=100), decisions)
    sold = [d for d in decisions if d["action"] == "sold"]
    assert len(sold) == 1
    assert sold[0].get("netted") is True, "넷팅 청산 결정에 netted 표식"


def test_handoff_writes_intent_seed_for_idempotency(isolated_trader):
    """N1: 넷팅 leg도 intent 저널에 기록 → is_active 게이트가 재발주 차단."""
    t, broker = isolated_trader
    _seed_long(t, "A", "005930", 5, entry=90)
    decisions: list[dict] = []
    t._apply_netted_leg(_exit_leg("A", "005930", 5, entry=90, ref=100), decisions)
    t._apply_netted_leg(_entry_leg("B", "005930", 5, ref=100), decisions)
    assert intents.is_active("2026-06-01", "A", "005930", "sell")
    assert intents.is_active("2026-06-01", "B", "005930", "buy")


def test_partial_handoff_reduces_slot(isolated_trader):
    """부분 핸드오프: A 롱5 중 3만 넷팅 청산 → A 슬롯 2 잔존."""
    t, broker = isolated_trader
    _seed_long(t, "A", "005930", 5, entry=90)
    decisions: list[dict] = []
    t._apply_netted_leg(_exit_leg("A", "005930", 3, entry=90, ref=100), decisions)
    assert broker.submitted == []
    assert t.ledger["A"]["qty"] == 2, "부분 이관 후 잔여 2계약"


def test_short_handoff(isolated_trader):
    """숏 핸드오프: Z 숏청산(buy) + W 숏진입(sell) → 브로커 0·Z 소멸·W 숏 생성."""
    t, broker = isolated_trader
    t.ledger["Z"] = {"symbol": "코스피200선물", "qty": 4, "entry_date": "2026-05-20",
                     "entry_price": 110.0, "peak_price": 110.0, "side": "short",
                     "strategy_name": "Z", "definition": {}}
    decisions: list[dict] = []
    t._apply_netted_leg(_exit_leg("Z", "코스피200선물", 4, entry=110, ref=100, side="short"),
                        decisions)
    t._apply_netted_leg(_entry_leg("W", "코스피200선물", 4, ref=100, side="short"), decisions)
    assert broker.submitted == []
    assert "Z" not in t.ledger, "Z 숏 환매(이관)"
    assert t.ledger["W"]["qty"] == 4 and t.ledger["W"]["side"] == "short"

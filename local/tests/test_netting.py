"""순수 넷팅 함수 — (contract_key, position_side)별 same-side open↔close 상쇄.

브로커/IO 미의존(순수 함수)이라 위젯·계좌 없이 검증한다. 설계 §13(order-netting-design.md).
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp.netting import Intent, net_window


def _entry(sid, ck, side, qty, ref=100.0, mult=1.0):
    return Intent(sid=sid, strategy_id=sid, strategy_name=sid, contract_key=ck,
                  symbol=ck, kind="entry", position_side=side,
                  order_side=("buy" if side == "long" else "sell"),
                  qty=qty, ref_price=ref, entry_price=None, mult=mult,
                  currency="KRW", definition={})


def _exit(sid, ck, side, qty, entry=90.0, ref=100.0, mult=1.0):
    return Intent(sid=sid, strategy_id=sid, strategy_name=sid, contract_key=ck,
                  symbol=ck, kind="exit", position_side=side,
                  order_side=("sell" if side == "long" else "buy"),
                  qty=qty, ref_price=ref, entry_price=entry, mult=mult,
                  currency="KRW", definition={})


# ── Task 1.1 — no-op ────────────────────────────────────────────────────────────

def test_no_overlap_is_noop():
    ins = [_entry("B", "K200", "long", 3)]
    r = net_window(ins)
    assert r.broker_orders == ins
    assert r.book_legs == []
    assert r.netted == []


def test_empty_is_noop():
    r = net_window([])
    assert r.broker_orders == [] and r.book_legs == [] and r.netted == []


# ── Task 1.2 — same-side full handoff ───────────────────────────────────────────

def test_same_side_full_handoff():
    ins = [_exit("A", "K200", "long", 5, entry=90, ref=100),
           _entry("B", "K200", "long", 5, ref=100)]
    r = net_window(ins)
    assert r.broker_orders == []               # 순증분 0 → 실주문 없음
    assert len(r.book_legs) == 2               # A exit + B entry 합성체결
    assert {l.sid for l in r.book_legs} == {"A", "B"}
    assert r.netted == [{"contract_key": "K200", "symbol": "K200",
                          "position_side": "long", "netted_qty": 5}]


# ── Task 1.3 — quantity mismatch → residual ─────────────────────────────────────

def test_qty_mismatch_residual_entry_excess():
    ins = [_exit("A", "K200", "long", 3, ref=100),
           _entry("B", "K200", "long", 5, ref=100)]
    r = net_window(ins)
    assert sum(l.qty for l in r.book_legs if l.kind == "exit") == 3
    assert sum(l.qty for l in r.book_legs if l.kind == "entry") == 3
    assert len(r.broker_orders) == 1
    assert r.broker_orders[0].sid == "B" and r.broker_orders[0].qty == 2
    assert r.broker_orders[0].kind == "entry"
    assert r.netted[0]["netted_qty"] == 3


def test_qty_mismatch_residual_exit_excess():
    ins = [_exit("A", "K200", "long", 5, ref=100),
           _entry("B", "K200", "long", 2, ref=100)]
    r = net_window(ins)
    assert sum(l.qty for l in r.book_legs if l.kind == "exit") == 2
    assert sum(l.qty for l in r.book_legs if l.kind == "entry") == 2
    assert len(r.broker_orders) == 1
    assert r.broker_orders[0].sid == "A" and r.broker_orders[0].qty == 3
    assert r.broker_orders[0].kind == "exit"
    assert r.netted[0]["netted_qty"] == 2


# ── Task 1.4 — cross-side never nets (§13.4) ────────────────────────────────────

def test_cross_side_entries_never_net():
    # 롱진입 + 숏진입 (같은 계약) → 지속 실물 없음 → 상쇄 금지
    ins = [_entry("X", "K200", "long", 5), _entry("Y", "K200", "short", 5)]
    r = net_window(ins)
    assert r.book_legs == [] and len(r.broker_orders) == 2 and r.netted == []


def test_cross_side_exits_never_net():
    # 롱청산 + 숏청산 → 상쇄 금지
    ins = [_exit("X", "K200", "long", 5), _exit("Y", "K200", "short", 5)]
    r = net_window(ins)
    assert r.book_legs == [] and len(r.broker_orders) == 2 and r.netted == []


# ── Task 1.5 — contract-key isolation (E6) ──────────────────────────────────────

def test_different_contract_keys_isolated():
    # 같은 상품이라도 계약코드(만기물)가 다르면 상쇄 금지(롤 경계 방어)
    ins = [_exit("A", "A01606", "long", 5), _entry("B", "A01609", "long", 5)]
    r = net_window(ins)
    assert r.book_legs == [] and len(r.broker_orders) == 2 and r.netted == []


def test_multi_symbol_independent():
    ins = [_exit("A", "K200", "long", 5), _entry("B", "K200", "long", 5),
           _entry("C", "SEC", "long", 4)]  # SEC 진입은 상쇄 대상 없음
    r = net_window(ins)
    assert len(r.book_legs) == 2                     # K200만 핸드오프
    assert [o.sid for o in r.broker_orders] == ["C"]  # SEC 진입은 실주문
    assert r.netted == [{"contract_key": "K200", "symbol": "K200",
                          "position_side": "long", "netted_qty": 5}]


# ── Task 1.6 — short-side handoff + FIFO determinism (E10) ───────────────────────

def test_short_side_handoff():
    ins = [_exit("Z", "K200", "short", 4, entry=110, ref=100),
           _entry("W", "K200", "short", 4, ref=100)]
    r = net_window(ins)
    assert r.broker_orders == [] and len(r.book_legs) == 2
    assert r.netted[0]["position_side"] == "short" and r.netted[0]["netted_qty"] == 4


def test_fifo_determinism_by_strategy_id():
    # 청산 A2 + C2 + 진입 B3 → handoff3 = A2 + C1(strategy_id 정렬), 잔여 C1 실청산
    ins = [_exit("C", "K200", "long", 2, ref=100),
           _exit("A", "K200", "long", 2, ref=100),
           _entry("B", "K200", "long", 3, ref=100)]
    r = net_window(ins)
    booked_exit = sorted((l.sid, l.qty) for l in r.book_legs if l.kind == "exit")
    assert booked_exit == [("A", 2), ("C", 1)]                 # FIFO by strategy_id
    assert [(o.sid, o.qty) for o in r.broker_orders] == [("C", 1)]
    assert sum(l.qty for l in r.book_legs if l.kind == "entry") == 3


def test_same_direction_orders_do_not_net():
    # 롱청산(sell) + 숏진입(sell): 둘 다 매도지만 서로 다른 (side) 그룹이라 핸드오프 없음
    ins = [_exit("A", "K200", "long", 5), _entry("Y", "K200", "short", 5)]
    r = net_window(ins)
    assert r.book_legs == [] and len(r.broker_orders) == 2 and r.netted == []

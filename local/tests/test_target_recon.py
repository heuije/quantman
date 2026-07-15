"""목표상태 수렴 순수 계획(target_recon.build_symbol_plans) 단위 — 설계 §2·§13·§14.

07-14 mwmw 사고의 계획 수준 재현 + 검산 항등식(모듈 docstring ①②③) 강제.
시나리오 레벨(원장·발주·체결)은 tests/scenarios/test_target_reconciliation.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL_DIR = Path(__file__).resolve().parent.parent
_CORE_DIR = _LOCAL_DIR.parent / "core"
for _p in (str(_LOCAL_DIR), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from localapp.netting import Intent
from localapp.target_recon import build_symbol_plans, signed_qty

S = "코스닥150선물"


def _leg(kind: str, qty: int, *, side: str = "long", sym: str = S,
         sid: str = "A", ref: float = 1090.0, entry: float | None = None) -> Intent:
    order_side = ("buy" if side == "long" else "sell") if kind == "entry" \
        else ("sell" if side == "long" else "buy")
    return Intent(sid=sid, strategy_id=sid, strategy_name=sid, contract_key=sym,
                  symbol=sym, kind=kind, position_side=side, order_side=order_side,
                  qty=qty, ref_price=ref,
                  entry_price=(entry if kind == "exit" else None),
                  mult=250_000.0, currency="KRW", definition={})


def _check_identities(plan, ledger: int):
    """docstring 항등식 ①②③ — 모든 계획이 만족해야 하는 보존 법칙."""
    legs_sum = sum(signed_qty(l) for l in plan.book_legs + plan.order_legs)
    assert plan.target == ledger + legs_sum, "① leg 보존"
    assert plan.net == sum(signed_qty(l) for l in plan.order_legs) + plan.drift_qty, \
        "② 물리 보존"
    for leg in plan.order_legs:
        assert (signed_qty(leg) > 0) == (plan.net > 0), "③ 단일 순방향"


def test_pure_entry_no_drift():
    """평시 진입: 원장·브로커 빈 상태 진입 5 → 실발주 5, drift 0."""
    [p] = build_symbol_plans([_leg("entry", 5)], {}, {}, set())
    assert (p.target, p.net, p.drift_qty) == (5, 5, 0)
    assert sum(l.qty for l in p.order_legs) == 5 and not p.book_legs
    _check_identities(p, 0)


def test_entry_absorbs_manual_holding():
    """§6 07-13 15:40: 수동 롱 4 보유 중 진입 5 → 순매수 1(수동 4는 전략이 인수·fresh 기장)."""
    [p] = build_symbol_plans([_leg("entry", 5)], {}, {S: 4}, set())
    assert (p.target, p.broker, p.net) == (5, 4, 1)
    assert sum(l.qty for l in p.order_legs) == 1
    assert sum(l.qty for l in p.book_legs) == 4          # 진입 4는 합성 정산(인수)
    assert p.drift_qty == 0
    _check_identities(p, 0)


def test_manual_presold_roll_restores():
    """§6 07-14 08:55: 원장 5(청산예정)·수동선매도로 브로커 0·진입 6
    → 상쇄 5(handoff) + 잔여진입 1 실발주 + drift 복원 5 = 순매수 6."""
    legs = [_leg("exit", 5, sid="ex29", entry=1090.0), _leg("entry", 6, sid="in27")]
    [p] = build_symbol_plans(legs, {S: 5}, {S: 0}, set())
    assert (p.target, p.broker, p.net) == (6, 0, 6)
    assert p.offset_qty == 5                              # 기존 넷팅 handoff
    assert sum(l.qty for l in p.order_legs) == 1          # 잔여 진입만 leg 발주
    assert p.drift_qty == 5                               # 수동매도 복원(원장 불변)
    # 정산: 청산 5(fresh 실현손익) + 진입 5(fresh 진입가) = book 10
    assert sum(l.qty for l in p.book_legs) == 10
    _check_identities(p, 5)


def test_exit_shortfall_no_oversell():
    """order 355 부류: 원장 5 청산인데 브로커 2(수동매도 3) → 매도 2만 발주,
    3은 fresh 정산 — 오버셀·skip_oversell 개념 자체 소멸(§14)."""
    [p] = build_symbol_plans([_leg("exit", 5, entry=1080.0)], {S: 5}, {S: 2}, set())
    assert (p.target, p.net) == (0, -2)
    assert sum(l.qty for l in p.order_legs) == 2
    assert sum(l.qty for l in p.book_legs) == 3
    assert p.drift_qty == 0
    _check_identities(p, 5)


def test_exit_fully_absorbed_net_zero():
    """수동이 전량 선매도(브로커 0) → net 0: 실주문 없이 전량 fresh 정산(워시 매도 0건)."""
    [p] = build_symbol_plans([_leg("exit", 5, entry=1080.0)], {S: 5}, {S: 0}, set())
    assert (p.target, p.net, p.drift_qty) == (0, 0, 0)
    assert not p.order_legs and sum(l.qty for l in p.book_legs) == 5
    _check_identities(p, 5)


def test_external_symbol_liquidated():
    """비전략 심볼(§9③): 원장·계획 없음 + 브로커 3 → target 0 → drift 매도 3(원장 불변)."""
    [p] = build_symbol_plans([], {}, {"외부심볼": 3}, set())
    assert (p.symbol, p.target, p.net, p.drift_qty) == ("외부심볼", 0, -3, -3)
    assert not p.order_legs and not p.book_legs
    _check_identities(p, 0)


def test_manual_extra_on_held_symbol_sold():
    """전략 보유 5 + 수동매수 3(브로커 8) → target 5 → drift 매도 3(원장 5 불변)."""
    plans = build_symbol_plans([], {S: 5}, {S: 8}, set())
    [p] = plans
    assert (p.target, p.net, p.drift_qty) == (5, -3, -3)
    _check_identities(p, 5)


def test_entry_exceeded_by_manual_inventory():
    """진입 6인데 수동 보유 10 → 진입 전량 fresh 인수 + 초과 4 drift 매도(단일 순방향)."""
    [p] = build_symbol_plans([_leg("entry", 6)], {}, {S: 10}, set())
    assert (p.target, p.net) == (6, -4)
    assert not p.order_legs                               # 매수 leg인데 net은 매도 → 전량 book
    assert sum(l.qty for l in p.book_legs) == 6
    assert p.drift_qty == -4
    _check_identities(p, 0)


def test_short_position_restored():
    """숏 유지 −3인데 수동 환매로 브로커 0 → drift 매도 3(재숏 복원·부호 정합)."""
    [p] = build_symbol_plans([], {S: -3}, {S: 0}, set())
    assert (p.target, p.net, p.drift_qty) == (-3, -3, -3)
    _check_identities(p, -3)


def test_indeterminate_symbol_held():
    """§13 목표 없음 ≠ 목표 0: 판정불가 심볼은 계획 자체가 없다(전량청산 오인 불가)."""
    plans = build_symbol_plans([_leg("exit", 5, entry=1080.0)], {S: 5}, {S: 5}, {S})
    assert plans == []


def test_converged_symbol_no_action():
    """이미 목표 상태(원장=브로커·계획 없음) → 계획 없음(무행동)."""
    assert build_symbol_plans([], {S: 5}, {S: 5}, set()) == []


def test_sells_ordered_before_buys():
    """청산 먼저 → 진입 나중 불변식을 심볼 단위로 보존(net≤0 먼저)."""
    legs = [_leg("entry", 2, sym="매수심볼", sid="B"),
            _leg("exit", 2, sym="매도심볼", sid="A", entry=100.0)]
    plans = build_symbol_plans(legs, {"매도심볼": 2}, {"매도심볼": 2}, set())
    assert [p.net <= 0 for p in plans] == sorted(
        [p.net <= 0 for p in plans], reverse=True)


def test_roll_boundary_no_cross_contract_cancellation():
    """E6 롤 경계 — 구계약 청산 + 신계약 진입(같은 상품명)은 절대 상쇄되지 않는다.

    심볼 단위로 net을 계산하면 target=ledger−5+5, broker=5 → net 0으로 물리 롤이
    실행되지 않는 치명 결함(적대 자체검토 발견). 키(계약코드) 단위 net이 근본 해결:
    구계약 매도 5 + 신계약 매수 5 둘 다 실발주된다."""
    old_exit = Intent(sid="A", strategy_id="A", strategy_name="A",
                      contract_key="OLD코드", symbol=S, kind="exit",
                      position_side="long", order_side="sell", qty=5,
                      ref_price=1090.0, entry_price=1080.0, mult=250_000.0,
                      currency="KRW", definition={})
    new_entry = Intent(sid="A", strategy_id="A", strategy_name="A",
                       contract_key="NEW코드", symbol=S, kind="entry",
                       position_side="long", order_side="buy", qty=5,
                       ref_price=1090.0, entry_price=None, mult=250_000.0,
                       currency="KRW", definition={})
    plans = build_symbol_plans(
        [old_exit, new_entry],
        {"OLD코드": 5}, {"OLD코드": 5}, set(),
        symbol_of={"OLD코드": S, "NEW코드": S})
    assert len(plans) == 2, "구·신 계약이 별도 계획(오상계 금지)"
    by_key = {p.key: p for p in plans}
    assert by_key["OLD코드"].net == -5
    assert sum(l.qty for l in by_key["OLD코드"].order_legs) == 5   # 실매도 5
    assert by_key["NEW코드"].net == 5
    assert sum(l.qty for l in by_key["NEW코드"].order_legs) == 5   # 실매수 5
    assert by_key["OLD코드"].offset_qty == 0 and by_key["NEW코드"].offset_qty == 0
    for p in plans:
        _check_identities(p, 5 if p.key == "OLD코드" else 0)
    # 매도 계획이 매수보다 먼저(증거금 선회수)
    assert plans[0].key == "OLD코드"


def test_cross_strategy_fifo_split():
    """전략 2개 잔여 leg에 용량 FIFO(strategy_id 순) 배분 — 경계 leg은 분할."""
    legs = [_leg("entry", 3, sid="A"), _leg("entry", 3, sid="B")]
    [p] = build_symbol_plans(legs, {}, {S: 2}, set())      # net = 6-2 = 4
    assert (p.net, p.drift_qty) == (4, 0)
    ordered = {(l.strategy_id, l.qty) for l in p.order_legs}
    assert ordered == {("A", 3), ("B", 1)}                 # FIFO: A 전량 → B 부분
    assert {(l.strategy_id, l.qty) for l in p.book_legs} == {("B", 2)}
    _check_identities(p, 0)

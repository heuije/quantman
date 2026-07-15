"""목표상태 수렴(Target Reconciliation) — 순수 계획 함수 (브로커/IO 없음).

설계: docs/REDESIGN/kr-target-reconciliation.md §2~§14.

매 사이클, 심볼별로 "autotrade가 의도하는 순 포지션(target)"으로 브로커를 수렴시킨다:

    target = ledger_signed + Σ(계획 leg의 물리 부호합)      # 유지분 + 진입 − 청산
    net    = target − broker_signed                          # +매수 / −매도

net이 곧 이 심볼의 물리 주문 총량이다. 수동매매로 생긴 drift(ledger≠broker)는
net에 자동 흡수된다 — 별도 되돌림 주문 없이 순주문으로 교정(§2).

## 이중발주 방지 (§14 pending 멱등 — 구현 확정)

`net = target − (broker + pending)` 산술은 **기각**했다: in-flight 주문의 leg는
멱등 게이트(L-01)가 재계획에서 제외하는데 pending 산술만 남으면 자기 주문을
상쇄하는 워시 주문이 나간다(예: 미체결 매수 6 → net −6 매도). 대신:
- **plan-time**: in-flight 주문(활성 intent)이 있는 심볼은 호출자(trader)가
  indeterminate로 제외 — 이번 사이클 그 심볼 수렴 보류(hold).
- **submit-time**: 모든 발주가 intent 저널 게이트(L-01·DRIFT 키)를 통과.
저널은 fsync·크래시 재기동 복구(reconcile_submitting)까지 검증된 기존 기계다.

## 정산(booking) 분배 — 전략별 P&L 귀속(§3.1)

계획 leg(진입/청산 Intent)는 두 부류로 나뉜다:
- **book_legs**: fresh 참조가로 합성 정산(원장만 반영·브로커 미접촉). ① 같은 심볼의
  진입↔청산 상쇄분(기존 넷팅 handoff — net_window 재사용), ② drift가 흡수한 leg
  (수동매매가 이미 물리 작업을 했으므로 발주 없이 정산 — 수동 손익은 계좌 자본에
  흡수되고 전략은 fresh 시장가로 기장, §9 Q1).
- **order_legs**: 실발주(체결 시 기존 _apply_fill로 원장 반영). 물리 방향은 항상
  sign(net)과 같다 — **심볼당 단일 순방향 불변식**(매수·매도 동시 발주 불가 = 오버셀·
  wash 구조적 소멸).

order_legs 합계가 |net|에 못 미치는 잔여 = **drift_qty**: 원장 불변 drift 교정 주문
(수동매매 되돌림·비전략 심볼 청산). 체결돼도 원장을 건드리지 않는다(원장=의도는
leg booking이 이미 반영 — trader의 pending drift 플래그 참조).

## 검산 항등식 (모든 SymbolPlan이 만족·테스트 강제)

    ① target == ledger + Σ_signed(book_legs) + Σ_signed(order_legs)   # leg 보존
    ② net == Σ_signed(order_legs) + drift_qty                          # 물리 보존
    ③ sign(signed_qty(leg)) == sign(net) for all order_legs            # 단일 순방향

## 목표 없음 ≠ 목표 0 (§13 — 필수 규칙)

이 모듈은 target을 항상 정수로 계산한다. "판정 불가"(stale·파싱 실패·조회 실패·
in-flight 주문)는 **호출자(trader)가 indeterminate 심볼 집합으로 사전에 제외**한다 —
그 심볼은 SymbolPlan 자체가 생성되지 않아 이번 사이클 수렴을 건너뛴다(hold).
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from .netting import Intent, net_window  # noqa: F401 — Intent는 호출자 편의 재노출


def signed_qty(leg) -> int:
    """leg의 물리 방향 부호수량 — buy=+qty, sell=−qty."""
    return int(leg.qty) if leg.order_side == "buy" else -int(leg.qty)


@dataclass(frozen=True)
class SymbolPlan:
    """한 심볼의 수렴 계획 — trader가 book_legs 정산 → order_legs·drift 발주."""
    symbol: str
    target: int              # 부호 순 목표 (롱 +, 숏 −)
    broker: int              # 부호 실보유 (스냅샷)
    net: int                 # target − broker
    book_legs: list          # list[Intent] — fresh 참조가 합성 정산(원장만)
    order_legs: list         # list[Intent] — 실발주(체결 시 원장 반영)
    drift_qty: int           # 부호 drift 교정 주문량(원장 불변) — sign == sign(net)
    offset_qty: int          # 진입↔청산 상쇄량(수수료 절감 집계용·기존 n_netted)


def build_symbol_plans(
    plan_intents: list,
    ledger_signed: dict[str, int],
    broker_signed: dict[str, int],
    indeterminate: set[str],
) -> list[SymbolPlan]:
    """심볼별 수렴 계획 산출 (순수).

    plan_intents: 이 사이클의 진입/청산 의도(Intent) — 신선도·게이트·멱등은 호출자가
        이미 통과시킴(ref_price는 fresh 또는 live).
    ledger_signed: 이번 사이클 범위(시장·자산군) 원장 포지션의 심볼별 부호합
        (유지+청산예정 전부 — target 항등식의 기저).
    broker_signed: 브로커 실보유의 심볼별 부호합(스냅샷·같은 범위).
    indeterminate: 판정 불가 심볼(§13 — target=None 의미). 계획에서 통째 제외(hold).

    반환 순서: net≤0(매도·정산-only) 심볼 먼저, net>0(매수) 나중 — 청산이 증거금을
    먼저 푸는 기존 불변식(청산 먼저 → 진입 나중)을 심볼 단위로 보존.
    """
    by_symbol: dict[str, list] = {}
    for it in plan_intents:
        by_symbol.setdefault(it.symbol, []).append(it)

    symbols = (set(by_symbol) | set(ledger_signed) | set(broker_signed)) \
        - set(indeterminate)

    plans: list[SymbolPlan] = []
    for sym in sorted(symbols):
        legs = by_symbol.get(sym, [])
        ledger = int(ledger_signed.get(sym, 0))
        broker = int(broker_signed.get(sym, 0))

        target = ledger + sum(signed_qty(l) for l in legs)
        net = target - broker

        # ① 진입↔청산 상쇄(handoff) — 기존 넷팅 그대로 재사용.
        #    (contract_key, position_side) 그룹핑이라 롤 경계(E6)·교차 side 보호 유지.
        nr = net_window(legs)
        book_legs: list = list(nr.book_legs)
        offset_qty = sum(n["netted_qty"] for n in nr.netted)

        # ② 잔여 leg에 |net| 물리 용량을 FIFO 배분 — net과 같은 방향 leg만 실발주,
        #    나머지는 drift(수동매매)가 이미 물리 작업을 한 것이므로 fresh 정산(book).
        order_legs: list = []
        capacity = abs(net)
        net_sign = 1 if net > 0 else (-1 if net < 0 else 0)
        for leg in sorted(nr.broker_orders, key=lambda x: x.strategy_id):
            s = signed_qty(leg)
            if net_sign != 0 and (s > 0) == (net_sign > 0):
                take = min(int(leg.qty), capacity)
                if take > 0:
                    order_legs.append(leg if take == leg.qty
                                      else replace(leg, qty=take))
                    capacity -= take
                if take < leg.qty:
                    book_legs.append(replace(leg, qty=int(leg.qty) - take))
            else:
                # net과 반대 방향(또는 net=0) — 물리 주문 없이 drift가 흡수 → 정산만.
                book_legs.append(leg)

        drift_qty = net_sign * capacity   # leg 배분 후 남은 물리 잔여(원장 불변 교정)

        if not book_legs and not order_legs and drift_qty == 0:
            continue                       # 이미 목표 상태 — 무행동

        plans.append(SymbolPlan(
            symbol=sym, target=target, broker=broker, net=net,
            book_legs=book_legs, order_legs=order_legs,
            drift_qty=drift_qty, offset_qty=offset_qty))

    # 매도(net≤0) 먼저 → 매수(net>0) 나중
    plans.sort(key=lambda p: (0 if p.net <= 0 else 1, p.symbol))
    return plans

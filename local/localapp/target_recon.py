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


def external_pending_by_key(pending, own_order_nos, scope_ok, key_of):
    """외부(수동) 미체결을 계약 키별 부호수량 + (심볼,side)별 잔량으로 집계 (§19 A1·순수).

    동시호가 창에 유저가 발주한 미체결은 08:45/15:45 체결되므로 자동 발주(08:35/15:40)
    시점에 effective current로 반영해야 과매수하지 않는다. 자기 주문은 반드시 제외한다 —
    포함하면 08:40/42 재시도가 자기 미체결을 상쇄하는 워시 발주가 나간다(§17.1 기각 사유).

    pending: broker.pending_orders() — [{order_no, symbol, side, remain_qty|qty, ...}].
    own_order_nos: 자기 발주 order_no 집합(intents 저널 mark_submitted). 이 order_no는 제외.
    scope_ok(p)→bool: 이 미체결이 이번 사이클 범위(시장·자산군)인가 — 호출자가 dict 필드
        (market·asset_class)로 판정(심볼 파싱 대신). 키는 행의 contract_code(라우터가
        정규화 전 보존한 원시 계약코드 — 선물)를 우선하고, 없으면 key_of(sym)(주식=심볼).
        표시심볼로 키잉하면 plan 키(계약코드)와 영원히 miss라 A1이 라이브에서
        무동작이었다(2026-07-19 감사 C1).
    반환 (signed_by_key, remain_by_key_side): 넷팅용 부호수량 + 사이징 크레딧용 잔량 —
        둘 다 같은 계약 키로 키잉(크레딧 역매핑 _code2disp도 계약 키를 기대).
    """
    signed_by_key: dict[str, int] = {}
    remain_by_key_side: dict[tuple[str, str], int] = {}
    for p in pending or []:
        ono = str(p.get("order_no") or "")
        if ono and ono in own_order_nos:
            continue                                   # 자기 주문 — 워시 방지
        side = p.get("side")
        if side not in ("buy", "sell") or not scope_ok(p):
            continue
        sym = str(p.get("symbol") or "").strip()
        rem = int(p.get("remain_qty") or p.get("qty") or 0)
        if not sym or rem <= 0:
            continue
        key = str(p.get("contract_code") or "").strip() or key_of(sym)
        signed_by_key[key] = signed_by_key.get(key, 0) + (rem if side == "buy" else -rem)
        pos_side = "long" if side == "buy" else "short"
        remain_by_key_side[(key, pos_side)] = \
            remain_by_key_side.get((key, pos_side), 0) + rem
    return signed_by_key, remain_by_key_side


@dataclass(frozen=True)
class SymbolPlan:
    """한 **계약 키**의 수렴 계획 — trader가 book_legs 정산 → order_legs·drift 발주.

    key = 물리 계약 식별자(선물=만기 계약코드, 주식=종목코드 — netting Intent의
    contract_key와 동일 규약). **심볼이 아닌 키 단위로 net을 계산해야** 롤 경계에서
    구계약 청산과 신계약 진입이 같은 상품명으로 오상계(물리 롤 미실행)되지 않는다(E6).
    symbol = 발주 라우팅용 상품명/종목코드(주문 API는 심볼 단위).
    """
    key: str
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
    symbol_of: dict[str, str] | None = None,
    external_pending: dict[str, int] | None = None,
) -> list[SymbolPlan]:
    """계약 키별 수렴 계획 산출 (순수).

    plan_intents: 이 사이클의 진입/청산 의도(Intent) — 신선도·게이트·멱등은 호출자가
        이미 통과시킴(ref_price는 fresh 또는 live). 그룹 키 = it.contract_key.
    ledger_signed / broker_signed: 이번 사이클 범위 원장·브로커 포지션의 **계약 키별**
        부호합(주식=심볼, 선물=contract_code-or-심볼 — 호출자가 같은 규약으로 키잉).
    indeterminate: 판정 불가 키(§13 — target=None 의미). 계획에서 통째 제외(hold).
    symbol_of: 키→발주 심볼 매핑(누락 키는 키 자신 — 주식·fallback 규약과 일치).
    external_pending: §19 A1 — 계약 키별 **외부(수동) 미체결** 부호수량(자기 주문 제외).
        동시호가 창에 유저가 발주한 미체결은 08:45/15:45 체결되므로 effective current로
        반영해야 자동이 과매수하지 않는다. 단, net 부호를 뒤집지 않게 **같은 방향으로
        intended net을 부분선매수한 만큼만(cap |intended|)** 포함 — 자동 net이 수동과
        반대 방향이 되면 같은 계좌 자전·증거금 상충이라, 반대/초과분은 제외하고 방향무관
        개장후 수렴(08:50)이 처리한다(§19.2). None=미반영(기존 net=target−broker).

    반환 순서: net≤0(매도·정산-only) 먼저, net>0(매수) 나중 — 청산이 증거금을
    먼저 푸는 기존 불변식(청산 먼저 → 진입 나중)을 키 단위로 보존.
    """
    symbol_of = symbol_of or {}
    by_key: dict[str, list] = {}
    for it in plan_intents:
        by_key.setdefault(it.contract_key, []).append(it)

    keys = (set(by_key) | set(ledger_signed) | set(broker_signed)) \
        - set(indeterminate)

    plans: list[SymbolPlan] = []
    for key in sorted(keys):
        legs = by_key.get(key, [])
        ledger = int(ledger_signed.get(key, 0))
        broker = int(broker_signed.get(key, 0))

        target = ledger + sum(signed_qty(leg) for leg in legs)
        # §19 A1 — 외부(수동) 미체결을 effective current에 반영. intended(=target−broker)와
        # 같은 방향으로 부분선매수한 만큼만 포함(cap |intended|)해 net 부호를 뒤집지 않는다:
        # 자동 net이 수동과 반대 방향이 되면 동시호가 자전·증거금 상충이므로, 반대·초과분은
        # 제외(→ 방향무관 08:50 수렴이 처리). 예약 산술이 아니라 "체결될 미체결 = 현재보유"
        # 취급이라 자기 주문 워시(§17.1 기각) 재발 없음 — 호출자가 external을 자기 order_no로
        # 필터해 넘긴다.
        intended = target - broker
        _pend = int((external_pending or {}).get(key, 0))
        if intended != 0 and (_pend > 0) == (intended > 0):
            incl = (1 if intended > 0 else -1) * min(abs(_pend), abs(intended))
        else:
            incl = 0
        net = target - (broker + incl)

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
            key=key,
            symbol=(legs[0].symbol if legs else symbol_of.get(key, key)),
            target=target, broker=broker, net=net,
            book_legs=book_legs, order_legs=order_legs,
            drift_qty=drift_qty, offset_qty=offset_qty))

    # 매도(net≤0) 먼저 → 매수(net>0) 나중
    plans.sort(key=lambda p: (0 if p.net <= 0 else 1, p.key))
    return plans

"""발주창 넷팅 — 순수 함수 (브로커/IO 없음).

설계: docs/REDESIGN/order-netting-design.md §13.

한 발주창에서 같은 **물리 계약**(`contract_key`)·같은 **포지션 side**(long/short)의
open(진입)↔close(청산)이 겹치면, 겹치는 수량만큼은 브로커 주문 없이 원장만 이관한다
(book_legs). 나머지 순증분만 실제 브로커로 발주한다(broker_orders).

**왜 (contract_key, position_side)로만 상쇄하나 (§13.4):** 넷팅의 정당성은 "물리적 포지션이
지속되고 소유 전략만 바뀐다"는 데 있다. 같은 side의 open↔close만 이 조건을 만족한다. 교차
side(롱↔숏)는 지속 포지션이 없어(선물 계좌는 net 보유) 넷팅 시 실재하지 않는 손익을 만들고,
넷팅하지 않아야 reconcile이 그 병리 설정을 표면화한다. 그래서 그룹 키에 position_side를 넣어
교차 side가 구조적으로 절대 상쇄되지 않게 한다.

commission(KRW) 계산은 순수성 유지를 위해 여기서 하지 않는다 — netted 수량만 반환하고
APPLY/REPORT 단계(trader)가 수수료율로 절약액을 계산한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional


@dataclass(frozen=True)
class Intent:
    """한 전략이 이 발주창에서 한 물리 계약에 대해 내려는 주문 의도."""
    sid: str                      # 원장 키 (strategy:symbol[:idx])
    strategy_id: str
    strategy_name: str
    contract_key: str             # 물리 계약: 선물=계약코드, 주식=종목코드
    symbol: str                   # 상품명/종목코드 (로그·참조가 조회용)
    kind: str                     # "entry" | "exit"
    position_side: str            # "long" | "short" — 이 주문이 열거나 닫는 포지션 방향
    order_side: str               # "buy" | "sell"
    qty: int
    ref_price: float
    entry_price: Optional[float]  # exit일 때 실현손익용 진입가 (entry면 None)
    mult: float
    currency: str                 # "KRW" | "USD"
    definition: dict = field(default_factory=dict)  # entry일 때 원장 슬롯 생성용


@dataclass(frozen=True)
class NetResult:
    broker_orders: list           # list[Intent] — 잔여, 기존 _submit_*로 실발주
    book_legs: list               # list[Intent] — 핸드오프, 합성 체결로 원장 이관(수수료 0)
    netted: list                  # list[dict] {contract_key, symbol, position_side, netted_qty}


def _split(legs: list, take: int) -> tuple[list, list]:
    """legs를 strategy_id FIFO로 정렬해 앞에서 `take`만큼 booked, 나머지 residual.

    한 leg이 경계에 걸치면 qty를 쪼개 booked/residual 양쪽에 복제한다(결정론적).
    """
    booked: list = []
    residual: list = []
    remaining = take
    for leg in sorted(legs, key=lambda x: x.strategy_id):
        if remaining <= 0:
            residual.append(leg)
        elif leg.qty <= remaining:
            booked.append(leg)
            remaining -= leg.qty
        else:
            booked.append(replace(leg, qty=remaining))
            residual.append(replace(leg, qty=leg.qty - remaining))
            remaining = 0
    return booked, residual


def net_window(intents: list) -> NetResult:
    """발주 의도 리스트를 (contract_key, position_side)별로 상쇄.

    같은 그룹 안에서 open(entry)과 close(exit)이 함께 있으면 min(Σopen,Σclose)만큼 핸드오프
    (book_legs). 초과분·상쇄 대상 없는 의도는 broker_orders. 겹침이 없으면 입력을 그대로
    broker_orders로 반환한다(no-op = 현행 보존).
    """
    # 그룹 키 순서 = 첫 등장 순서(결정론). 그룹 내부는 _split이 strategy_id FIFO.
    groups: dict[tuple[str, str], list] = {}
    for it in intents:
        groups.setdefault((it.contract_key, it.position_side), []).append(it)

    broker_orders: list = []
    book_legs: list = []
    netted: list = []

    for (contract_key, position_side), legs in groups.items():
        opens = [l for l in legs if l.kind == "entry"]
        closes = [l for l in legs if l.kind == "exit"]
        open_qty = sum(l.qty for l in opens)
        close_qty = sum(l.qty for l in closes)
        handoff = min(open_qty, close_qty)

        if handoff <= 0:
            # 상쇄 대상 없음(한쪽만 있거나 빈 쪽) → 전부 실주문 (no-op 보존)
            broker_orders.extend(legs)
            continue

        booked_opens, resid_opens = _split(opens, handoff)
        booked_closes, resid_closes = _split(closes, handoff)
        book_legs.extend(booked_opens)
        book_legs.extend(booked_closes)
        broker_orders.extend(resid_opens)
        broker_orders.extend(resid_closes)
        netted.append({
            "contract_key": contract_key,
            "symbol": legs[0].symbol,
            "position_side": position_side,
            "netted_qty": handoff,
        })

    return NetResult(broker_orders=broker_orders, book_legs=book_legs, netted=netted)

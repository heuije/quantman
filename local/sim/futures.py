"""선물 테스트베드 순수 헬퍼. 선물 회계(승수·증거금)는 quant_core 단일출처를 재사용.

정산손익 = (exit−entry)×qty×승수×부호(롱+1/숏−1). 증거금 = notional×개시증거금률.
side 정규형 "long"|"short"(브로커 파서의 KIS 매수/매도→정규화는 P3).
"""
from __future__ import annotations

from quant_core.exec_defaults import instrument_spec

_SIGN = {"long": 1.0, "short": -1.0}


def settlement_pnl(symbol: str, side: str, qty: int, entry: float, exit_: float) -> float:
    """정산/실현 손익(통화단위). side: long|short."""
    mult = instrument_spec(symbol).multiplier
    return (exit_ - entry) * qty * mult * _SIGN[side]


def required_margin(symbol: str, qty: int, price: float) -> float:
    """개시증거금 = notional × 개시증거금률."""
    spec = instrument_spec(symbol)
    return price * qty * spec.multiplier * spec.init_margin_rate


def make_futures_position(symbol: str, side: str, qty: int,
                          entry_price: float, now_price: float) -> dict:
    """SimBroker account_snapshot positions에 넣을 선물 포지션 dict(정규형)."""
    return {
        "symbol": symbol,
        "side": side,                       # "long" | "short"
        "qty": qty,                         # 계약수(양수)
        "avg_price": entry_price,
        "eval_price": now_price,
        "multiplier": instrument_spec(symbol).multiplier,
        "margin_requirement": required_margin(symbol, qty, entry_price),
        "eval_pnl": settlement_pnl(symbol, side, qty, entry_price, now_price),
    }

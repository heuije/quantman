"""
전략 실행 어댑터.

Strategy 객체를 받아 백테스트 엔진/분석 엔진의 함수형 API로 연결한다.
백테스트와 실전이 동일한 Strategy를 쓰도록 보장하는 단일 경유지.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from .analysis import build_signal_mask, run_analysis
from .backtest import run_backtest
from .strategy import Strategy


def _conds(group) -> list[dict]:
    return [c.model_dump() for c in group.conditions] if group else []


def run_strategy_backtest(
    strategy: Strategy,
    data: dict[str, pd.DataFrame],
    initial_capital: float = 10_000_000.0,
    start=None,
    end=None,
) -> dict:
    """Strategy를 과거 데이터로 시뮬레이션한다."""
    ex = strategy.exit_rules
    return run_backtest(
        data=data,
        trade_symbol=strategy.trade_symbol,
        buy_conditions=_conds(strategy.buy),
        buy_logic=strategy.buy.logic,
        hold_days=ex.hold_days,
        take_profit=ex.take_profit,
        stop_loss=ex.stop_loss,
        trail_atr_mult=ex.trail_atr_mult,
        trail_pct=ex.trail_pct,
        sell_conditions=_conds(strategy.sell) if strategy.sell else None,
        sell_logic=strategy.sell.logic if strategy.sell else "AND",
        fill=strategy.fill,
        commission=strategy.commission,
        slippage=strategy.slippage,
        initial_capital=initial_capital,
        start=start,
        end=end,
    )


def evaluate_buy_signal(strategy: Strategy, data: dict[str, pd.DataFrame]) -> bool:
    """가장 최근 거래일 기준으로 매수 조건 충족 여부를 반환한다 (모의/실전 공용)."""
    mask = build_signal_mask(data, _conds(strategy.buy), strategy.buy.logic)
    if mask.empty:
        return False
    return bool(mask.iloc[-1])

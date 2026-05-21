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
from .exec_defaults import merged_execution
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
    """Strategy를 과거 데이터로 시뮬레이션한다.

    ExecutionPolicy(있으면 글로벌 default와 병합)의 비용·갭 가정을 적용한다.
    레거시 commission/slippage 필드는 ExecutionPolicy가 없을 때만 fallback.
    """
    sr = strategy.sell_rules
    pol_dict = strategy.execution.model_dump() if strategy.execution else None
    pol = merged_execution(pol_dict)
    # 편도 비용을 bps → 비율로 변환. 정책 우선, fallback은 strategy 필드.
    commission = pol["bt_commission_bps"] / 10_000.0
    slippage = pol["bt_slippage_bps"] / 10_000.0
    sell_conds = [c.model_dump() for c in sr.conditions] if sr.conditions else None
    return run_backtest(
        data=data,
        trade_symbol=strategy.trade_symbol,
        buy_conditions=_conds(strategy.buy),
        buy_logic=strategy.buy.logic,
        hold_days=sr.hold_days,
        take_profit=sr.take_profit,
        stop_loss=sr.stop_loss,
        trail_atr_mult=sr.trail_atr_mult,
        trail_pct=sr.trail_pct,
        sell_conditions=sell_conds,
        sell_logic=sr.logic,
        fill=strategy.fill,
        commission=commission,
        slippage=slippage,
        initial_capital=initial_capital,
        start=start,
        end=end,
        gap_extra_cost=bool(pol["bt_gap_extra_cost"]),
        gap_threshold_pct=float(pol["bt_gap_threshold_pct"]),
    )


def evaluate_buy_signal(strategy: Strategy, data: dict[str, pd.DataFrame],
                         current_symbol: str | None = None) -> bool:
    """가장 최근 거래일 기준으로 매수 조건 충족 여부를 반환한다 (모의/실전 공용).

    Phase 41 — current_symbol이 주어지면 [이 종목] placeholder가 그 종목으로
    치환된다. preview_engine / trader가 종목별 평가용으로 사용.
    """
    mask = build_signal_mask(data, _conds(strategy.buy), strategy.buy.logic,
                              current_symbol=current_symbol)
    if mask.empty:
        return False
    return bool(mask.iloc[-1])

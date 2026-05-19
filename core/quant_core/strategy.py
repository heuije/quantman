"""
통합 전략 스키마.

백테스트 · 모의투자 · 실전투자가 모두 이 Strategy 객체 하나를 공유한다.
"발견(데이터분석) → 검증(백테스트) → 실행(모의/실전)" 서사의 단일 진실 공급원.
"""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field

Op = Literal[">", ">=", "<", "<=", "between"]
Logic = Literal["AND", "OR"]
Fill = Literal["next_open", "close"]


class Condition(BaseModel):
    """단일 조건 — '{symbol}의 {indicator}가 {value}{op}일 때'."""
    symbol: str          # 시그널 심볼 (예: "S&P500")
    indicator: str       # 지표 컬럼명 (예: "return_1d")
    op: Op
    value: Union[float, list[float]]   # between이면 [min, max]


class ConditionGroup(BaseModel):
    conditions: list[Condition] = Field(default_factory=list)
    logic: Logic = "AND"


class ExitRules(BaseModel):
    """청산 규칙 — 먼저 트리거되는 것으로 청산."""
    hold_days: Optional[int] = None
    take_profit: Optional[float] = None      # %
    stop_loss: Optional[float] = None        # % (음수)
    trail_atr_mult: Optional[float] = None
    trail_pct: Optional[float] = None        # %


class Strategy(BaseModel):
    """매매 전략 — 백테스트/모의/실전 공용."""
    name: str = "새 전략"
    enabled: bool = True
    trade_symbol: str                                  # 매수 대상 종목
    buy: ConditionGroup = Field(default_factory=ConditionGroup)
    sell: Optional[ConditionGroup] = None
    exit_rules: ExitRules = Field(default_factory=ExitRules)
    amount_pct: float = 100.0                          # 자본 대비 투입 비율(%)
    fill: Fill = "next_open"                           # 백테스트 체결 모델
    commission: float = 0.00015
    slippage: float = 0.0005

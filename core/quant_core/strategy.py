"""
통합 전략 스키마.

백테스트 · 모의투자 · 실전투자가 모두 이 Strategy 객체 하나를 공유한다.
"발견(데이터분석) → 검증(백테스트) → 실행(모의/실전)" 서사의 단일 진실 공급원.

조건 프레임워크
---------------
조건 = ``좌변(Operand)`` · ``연산자(Op)`` · ``우변(Operand)`` · ``수식어(Modifier)``
  - Operand: 지표 / 숫자 / 지표 이력통계(롤링 최소·최대·평균·백분위·N일전값)
  - Op: 수준 비교(>, >=, <, <=, between) + 크로스(cross_up, cross_down)
  - Modifier: streak(N일 연속) / within(최근 N일 내)
구버전 조건 {symbol, indicator, op, value}는 검증기로 자동 변환된다.
"""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator

Op = Literal[">", ">=", "<", "<=", "between", "cross_up", "cross_down"]
Logic = Literal["AND", "OR"]
Fill = Literal["next_open", "close"]
OperandKind = Literal["indicator", "constant", "history"]
Stat = Literal["min", "max", "mean", "percentile", "lag"]
ModifierKind = Literal["streak", "within"]


class Operand(BaseModel):
    """비교 피연산자 — 지표 / 숫자 / 지표 이력통계."""
    kind: OperandKind = "indicator"
    # kind="indicator" 또는 "history": 시그널 종목·지표
    symbol: Optional[str] = None
    indicator: Optional[str] = None
    # kind="constant": 고정 숫자 (op="between"이면 [min, max])
    value: Optional[Union[float, list[float]]] = None
    # kind="history": 롤링 이력통계
    stat: Optional[Stat] = None            # min/max/mean/percentile/lag
    window: Optional[int] = None           # 롤링 기간(일). lag면 N일 전
    percentile: Optional[float] = None     # stat="percentile"일 때 0~100


class Modifier(BaseModel):
    """조건 수식어 — 지속성·최근성 필터."""
    kind: ModifierKind          # streak: N일 연속 참 / within: 최근 N일 내 1회 이상
    days: int = 1


class Condition(BaseModel):
    """단일 조건 — '{좌변}이(가) {우변}{연산자} ({수식어})'."""
    left: Operand
    op: Op
    right: Optional[Operand] = None
    modifier: Optional[Modifier] = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, data):
        """구버전 조건 {symbol, indicator, op, value}를 새 구조로 변환한다."""
        if isinstance(data, dict) and "left" not in data and "symbol" in data:
            return {
                "left": {"kind": "indicator",
                         "symbol": data.get("symbol"),
                         "indicator": data.get("indicator")},
                "op": data.get("op", ">"),
                "right": {"kind": "constant", "value": data.get("value")},
                "modifier": None,
            }
        return data


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

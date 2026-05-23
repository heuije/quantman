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

# Phase 41 — Operand.symbol에 이 sentinel이 들어있으면 "각 매수 대상 종목"
# placeholder. 평가 엔진이 current_symbol로 치환한다 (analysis._resolve_operand).
# 빈칸 채우기 메타포 확장: 사용자가 좌변 종목 드롭다운에서 "[이 종목]"을 고르면
# UI가 이 값을 전송. 한 전략 안에서 공통 종목(명시) + [이 종목] 자유 혼용 가능.
SELF_SYMBOL = "__SELF__"


def is_self_ref(op: "Operand | dict | None") -> bool:
    """Operand가 [이 종목] placeholder를 참조하는지."""
    if op is None:
        return False
    sym = op.get("symbol") if isinstance(op, dict) else getattr(op, "symbol", None)
    return sym == SELF_SYMBOL


class Operand(BaseModel):
    """비교 피연산자 — 지표 / 숫자 / 지표 이력통계.

    Phase 41 — symbol에 SELF_SYMBOL("__SELF__") sentinel을 넣으면 "각 매수 대상
    종목" placeholder. 평가 엔진(analysis._resolve_operand)이 current_symbol로
    치환한다. 좌변·우변 모두 placeholder 사용 가능 (둘 다 같은 종목으로 치환됨).
    """
    kind: OperandKind = "indicator"
    # kind="indicator" 또는 "history": 시그널 종목·지표 (SELF_SYMBOL이면 placeholder)
    symbol: Optional[str] = None
    indicator: Optional[str] = None
    # kind="constant": 고정 숫자 (op="between"이면 [min, max])
    value: Optional[Union[float, list[float]]] = None
    # kind="history": 롤링 이력통계
    stat: Optional[Stat] = None            # min/max/mean/percentile/lag
    window: Optional[int] = None           # 롤링 기간(일). lag면 N일 전
    percentile: Optional[float] = None     # stat="percentile"일 때 0~100
    # G1 — 아핀 변환: 해석된 시계열에 (× mul + add) 적용. None이면 무변환.
    # 예: MA20 × 1.05 (mul=1.05), 전일종가 × 0.95, 등락률 + 2 (add=2).
    # indicator/history에만 적용. constant에는 무시 (값 자체로 표현).
    mul: Optional[float] = None
    add: Optional[float] = None


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
    """조건 묶음 — AND/OR 결합.

    G2 — items는 단일 조건(Condition) 또는 하위 그룹(ConditionGroup)이 섞일 수
    있다. 이를 통해 ``(A AND B) OR C`` 같은 중첩 결합을 표현한다. 평가 엔진
    (analysis.build_signal_mask)이 재귀적으로 마스크를 결합한다.
    하위호환: 기존 flat 구조(모든 원소가 단일 조건)는 동작·결과가 동일하다.
    """
    conditions: list[Union["Condition", "ConditionGroup"]] = Field(
        default_factory=list)
    logic: Logic = "AND"


# 재귀 self-ref(ConditionGroup 안의 ConditionGroup) 해소
ConditionGroup.model_rebuild()


class ExitRules(BaseModel):
    """[DEPRECATED — SellRules로 통합됨] 청산 규칙. 호환성을 위해 유지."""
    hold_days: Optional[int] = None
    take_profit: Optional[float] = None      # %
    stop_loss: Optional[float] = None        # % (음수)
    trail_atr_mult: Optional[float] = None
    trail_pct: Optional[float] = None        # %


class SellRules(BaseModel):
    """매도 규칙 — 익절/손절/트레일링/보유기간 + 자유 매도 조건 통합.

    Phase 32: 기존 sell(ConditionGroup) + exit_rules(익절/손절/...)가 같은
    "매도" 개념의 두 측면이라 하나로 일원화. 먼저 트리거되는 규칙으로 매도.
    """
    # 가격 기반 트리거
    take_profit: Optional[float] = None      # 익절선 (%, 양수)
    stop_loss: Optional[float] = None        # 손절선 (%, 음수)
    trail_pct: Optional[float] = None        # 트레일링 (진입 후 고점 대비 %)
    trail_atr_mult: Optional[float] = None   # ATR 트레일링 (× ATR_14)
    # 시간 기반 트리거
    hold_days: Optional[int] = None          # 보유 일수 초과 시
    # 조건 기반 트리거 (dataset 평가) — G2 중첩 그룹 허용
    conditions: list[Union[Condition, ConditionGroup]] = Field(
        default_factory=list)
    logic: Logic = "AND"
    # 매도 시 청산 비율
    sell_amount_pct: float = 100.0           # 100 = 전량 매도


class ExecutionPolicy(BaseModel):
    """체결 정책 — 모든 필드가 Optional. None이면 글로벌 default 사용.

    quant_core.exec_defaults.DEFAULT_EXECUTION에 정의된 default와 병합된다.
    """
    # 주문 유형
    use_limit: Optional[bool] = None
    buy_tolerance_pct: Optional[float] = None
    # Phase 38.9 — sell/exit 통합. exit_tolerance_pct는 legacy 호환만.
    sell_tolerance_pct: Optional[float] = None
    exit_tolerance_pct: Optional[float] = None  # [DEPRECATED — merged_execution이 흡수]
    unfilled_timeout_sec: Optional[int] = None
    poll_interval_sec: Optional[int] = None
    # 갭 필터
    gap_filter_pct: Optional[float] = None
    # 사이징 (Phase 47 — fixed_amount·equal_weight 추가)
    sizing_mode: Optional[str] = None              # "fixed_amount" | "pct_cash" | "equal_weight" | "atr_risk"
    amount_krw: Optional[float] = None             # fixed_amount 모드: 한 종목당 원 단위 금액
    atr_risk_pct: Optional[float] = None
    atr_mult: Optional[float] = None
    max_position_pct: Optional[float] = None
    # 시스템 리스크
    daily_loss_limit_pct: Optional[float] = None
    max_drawdown_pct: Optional[float] = None
    # 백테스트 비용 가정 (C-01 — sell_tax 분리)
    bt_commission_bps: Optional[float] = None
    bt_sell_tax_bps: Optional[float] = None
    bt_slippage_bps: Optional[float] = None
    bt_gap_extra_cost: Optional[bool] = None
    bt_gap_threshold_pct: Optional[float] = None


def parse_trade_symbols(trade_symbol: str) -> tuple[str, list[str]]:
    """trade_symbol 문자열을 (mode, symbols)로 파싱.

    - "screener:marcap_top" → ("screener", ["marcap_top"])  (preset_key를 단일 항목으로)
    - "005930"               → ("manual",   ["005930"])
    - "005930,000660,035420" → ("manual",   ["005930", "000660", "035420"])

    공백·빈 토큰은 무시. 자동 선택과 수동 다중은 혼합 불가.
    """
    s = (trade_symbol or "").strip()
    if s.startswith("screener:"):
        return ("screener", [s[len("screener:"):]])
    parts = [p.strip() for p in s.split(",") if p.strip()]
    return ("manual", parts)


class Rebalance(BaseModel):
    """자동 선택 리밸런싱 — 상위 N 탈락 보유분 매도→교체.

    라이브 전용 (백테스트는 자동 선택 미지원). enabled=False면 기존 동작
    (빈 슬롯만 채움, 보유분은 sell_rules로만 매도)과 동일하다.
    """
    enabled: bool = False
    period: Literal["daily", "weekly", "monthly"] = "daily"


class Strategy(BaseModel):
    """매매 전략 — 백테스트/모의/실전 공용.

    Phase 32: sell + exit_rules + sell_amount_pct가 sell_rules로 통합됨.
    legacy 필드도 유지하되 _migrate_legacy validator가 sell_rules를 정규화한다.
    """
    name: str = "새 전략"
    enabled: bool = True
    trade_symbol: str                                  # 매수 대상 종목 — 단일/콤마 다중/screener:
    buy: ConditionGroup = Field(default_factory=ConditionGroup)
    # Phase 32 — 매도/청산 통합
    sell_rules: SellRules = Field(default_factory=SellRules)
    # [DEPRECATED — 호환성용. _migrate_legacy가 sell_rules로 흡수]
    sell: Optional[ConditionGroup] = None
    exit_rules: ExitRules = Field(default_factory=ExitRules)
    sell_amount_pct: float = 100.0
    amount_pct: float = 100.0                          # 자본 대비 매수 투입 비율(%)
    # 자동 선택 (trade_symbol='screener:<key>') 한도 — 1이면 한 번에 1종목, N이면 N종목까지
    screener_limit: int = 5
    # 커스텀 스크리너 스펙 — trade_symbol='screener:custom'일 때 프리셋 대신 이 spec 사용.
    # screener.parse_spec이 받는 dict ({rules, sort, markets, limit, ...}). None이면 프리셋.
    screener_spec: Optional[dict] = None
    # 자동 선택 리밸런싱 — 켜면 상위 N 탈락 보유분을 매도→교체 (라이브 전용).
    rebalance: Rebalance = Field(default_factory=Rebalance)
    fill: Fill = "next_open"                           # 백테스트 체결 모델
    commission: float = 0.00015
    slippage: float = 0.0005
    # 체결 정책 — None이면 글로벌 default 적용
    execution: Optional[ExecutionPolicy] = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy(cls, values):
        """legacy sell/exit_rules/sell_amount_pct를 sell_rules로 흡수.

        - sell_rules 명시 → 그대로 사용 (legacy 필드 무시)
        - sell_rules 없음 → legacy 필드들을 합쳐 sell_rules 구성
        """
        if not isinstance(values, dict):
            return values
        if values.get("sell_rules"):
            return values

        sr: dict = {}
        er = values.get("exit_rules") or {}
        if isinstance(er, dict):
            for k in ("take_profit", "stop_loss", "trail_pct",
                       "trail_atr_mult", "hold_days"):
                if er.get(k) is not None:
                    sr[k] = er[k]
        sell = values.get("sell")
        if isinstance(sell, dict):
            conds = sell.get("conditions") or []
            if conds:
                sr["conditions"] = conds
                sr["logic"] = sell.get("logic", "AND")
        if values.get("sell_amount_pct") is not None:
            sr["sell_amount_pct"] = values["sell_amount_pct"]
        if sr:
            values["sell_rules"] = sr
        return values

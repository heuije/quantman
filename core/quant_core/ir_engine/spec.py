"""StrategyIR — 전략 수립 로직의 완전한 통합 구조.

명세 §7(1회 백테스트 5단계)·§3.3(포지션 4부품). 비전의 데이터→신호→포지션→
성과→시뮬을 한 스키마로 통합한다. 핵심: **포지션 레이어가 룰·팩터 전략을 통일**.

  StrategyIR = universe + signal(Node) + position(4부품+오버레이) + simulation + query/study

신호(signal)는 condition(룰 트리거) 또는 score(팩터 알파) — 둘 다 같은 블록 트리.
엔진은 entry.mode × signal 타입으로 디스패치:
  - on_signal + condition        → 이벤트 드리븐(룰) 백테스트
  - scheduled/always + score|cond → 리밸런스/팩터 백테스트(횡단·포트폴리오)

데이터 연동이 아직 얇아도 이 구조 자체는 다양·복잡한 전략을 빠짐없이 표현한다.
"""

from __future__ import annotations

import types
import typing
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from ..blocks.catalog import get, has
from ..blocks.integrity import DatasetMeta, integrity_issues
from ..blocks.node import Node, referenced_columns, referenced_symbols
from ..blocks.validate import (SEV_ERROR, SEV_INTEGRITY_WARN, Issue, has_market_source,
                               meaningfulness_issues, prioritize, validate)
from ..exec_defaults import is_futures

# ── 유니버스 (대상 종목 집합) ─────────────────────────────────────────────────

class Universe(BaseModel):
    kind: Literal["single", "list", "all", "portfolio"] = "single"
    symbols: list[str] = Field(default_factory=list)   # single(1)/list·portfolio(다수)
    screener: Optional[dict] = None                    # 선택 종목 2차 필터: {"condition": Node, "refresh": str}
    exclude_macro: bool = True                         # all: 매크로/자산 지수 제외
    # portfolio 전용 — 보유 비중 {symbol: weight}. 없으면 동일가중. 이것은 "내 실제 보유"(진단 대상,
    # 축A)로, position.sizing.weights("전략 목표배분")와 평면이 다르다(진단엔 position 자체가 없음).
    weights: Optional[dict] = None


# ── 포지션 4부품 (비전 §3.3) ──────────────────────────────────────────────────

class Sizing(BaseModel):
    """② 크기 — 얼마나."""
    mode: Literal[
        "equal_weight",         # 동일가중 (횡단)
        "signal_proportional",  # 신호(score)비례 (횡단)
        "vol_inverse",          # 변동성 역가중 (횡단)
        "target_vol",           # 목표변동성(per-name 연변동성 타겟 — 레버리지 동반, 횡단)
        "fixed_weight",         # 정적 배분(사용자 지정 per-symbol 비중, 횡단)
        "fixed_amount",         # 종목당 고정 금액 (이벤트 진입 예산)
        "pct_cash",             # 자본 대비 % (이벤트 진입 예산)
    ] = "equal_weight"
    # (제거됨: fixed_risk·kelly — IR 엔진 미구현 모드를 enum에 두지 않는다. ATR위험·켈리는
    #  소비 경로·입력 어휘가 갖춰지고 수요가 생기면 sizer registry에 등록해 부활.)
    amount_pct: float = 10.0           # pct_cash / per-name 기본
    amount_krw: Optional[float] = None  # fixed_amount
    target_vol_pct: Optional[float] = None  # target_vol: 목표 연변동성(%)
    weights: Optional[dict] = None      # fixed_weight: {symbol: 비중}
    vol_window: int = 20                # vol_inverse·target_vol 변동성 창
    # 종목당 상한(%) — opt-in. 기본 100=무제한(집중 사이징 보존). 분산 원하면 낮춤.
    max_position_pct: float = 100.0


class Entry(BaseModel):
    """③ 진입 — 언제 들어가나. 주기(캘린더 규칙) ⊕ 이벤트(+지연) — 명세 §7.6."""
    mode: Literal["on_signal", "scheduled", "always"] = "on_signal"
    rebalance: Literal["daily", "weekly", "monthly", "quarterly", "annual",
                       "every_n_days"] = "weekly"
    every_n_days: Optional[int] = None
    top_n: Optional[int] = None         # scheduled/팩터: score 상위 N 선택
    top_pct: Optional[float] = None     # scheduled/팩터: score 상위 X% 선택(top_n 대안)
    # 임계 선택 — score 신호에서 절대 임계로 집합 선정(횡단 랭킹과 직교한 대안 선택자).
    # 설정 시 top_n/top_pct 대신 사용: 롱={score>threshold}·숏={score<threshold}.
    # 시계열 모멘텀(TSMOM: 자기 추세 부호로 독립 롱/숏)처럼 순위가 아닌 부호·수준 기준 선택에 필수.
    threshold: Optional[float] = None
    # 중간 청산 후 빈 슬롯 처리 — cash: 현금 유지(다음 리밸런스까지) · replace: 차순위 즉시 충원.
    refill: Literal["cash", "replace"] = "cash"


class Exit(BaseModel):
    """④ 청산 — 언제 나오나. 설정한 규칙들은 OR로 결합(가장 먼저 충족되는 것이 청산 발동).

    익절(take_profit)·손절(stop_loss)·보유기간(hold_days)·트레일링(trail_pct·trail_atr_mult)·
    매도조건(condition)을 독립적으로 켠다. 별도 '방식' 선택 없이 채워진 규칙만 활성 —
    예: 보유기간+손절을 함께 설정하면 N일 경과 또는 손절 중 먼저 닿는 쪽에서 청산.
    """
    hold_days: Optional[int] = None
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    trail_pct: Optional[float] = None
    trail_atr_mult: Optional[float] = None
    condition: Optional[Node] = None    # 매도 신호(condition Node)


class Overlays(BaseModel):
    """전역 오버레이."""
    vol_target: Optional[float] = None        # 연율화 변동성 타겟(%)
    turnover_damp: Optional[float] = None      # 가중치 변동 억제 임계(hump)
    # 낙폭 제어 — hard: 완전 청산 낙폭(%, kill). soft: 디리스킹 시작 낙폭(%).
    # soft~hard 구간에서 노출을 선형 축소, hard에서 0. soft 미지정(또는 ≥hard)이면 binary kill.
    max_drawdown_stop: Optional[float] = None  # = hard
    max_drawdown_soft: Optional[float] = None  # 부분 디리스킹 시작점(없으면 binary)
    # 그룹 노출 캡 — group_label(섹터·bucket 등)별 |비중| 합이 max_group_pct 초과 시 해당 그룹 축소.
    # per-name 캡(sizing.max_position_pct)의 그룹 일반화. 초과분은 현금 버퍼로(재정규화 안 함).
    max_group_pct: Optional[float] = None
    group_label: Optional[Node] = None         # 그룹 라벨 블록 — out_type=label (§5.4 루트 경계)


class PositionSpec(BaseModel):
    """포지션 레이어 — 신호를 실제 매매로 번역하는 4부품 + 오버레이."""
    direction: Literal["long", "short", "long_short"] = "long"   # ① 방향
    sizing: Sizing = Field(default_factory=Sizing)               # ②
    entry: Entry = Field(default_factory=Entry)                  # ③
    exit: Exit = Field(default_factory=Exit)                     # ④
    overlays: Overlays = Field(default_factory=Overlays)


# ── 시뮬레이션 (비전 §3.5) ────────────────────────────────────────────────────

class SimSpec(BaseModel):
    initial_capital: float = 10_000_000.0
    delay: int = 1                      # 신호→체결 지연(거래일). look-ahead 방지.
    # next_open=익일 시가, close=당일 종가, typical=당일 (고+저+종)/3 일봉 VWAP 근사.
    # (진짜 intraday VWAP·N분 평균은 분봉 데이터 필요 — 현재 데이터 범위 밖, 가짜 폴백 안 함.)
    fill: Literal["next_open", "close", "typical"] = "next_open"
    commission: Optional[float] = None
    slippage: Optional[float] = None
    sell_tax: Optional[float] = None
    leverage: float = 1.0
    # 연율 비용(%) — 숏 차입(short_borrow_pct)·레버리지 펀딩(funding_cost_pct)·현금 무위험수익(rfr_pct).
    # 종목별 차입가능 여부는 데이터 부재로 미모델(정직한 한계 — §7.6).
    short_borrow_pct: Optional[float] = None
    funding_cost_pct: Optional[float] = None
    rfr_pct: Optional[float] = None
    # 유지증거금률(%) — 레버리지 포지션의 자기자본비율(nav/gross)이 이 값 미만이면 마진콜:
    # 목표 레버리지(nav×L)로 강제 복원(nav≤0이면 전량 청산). None=꺼짐.
    # 일봉 종가 기준 체크 — intraday 데이터 없음(가짜 폴백 안 함, §7.6 정직한 한계).
    maintenance_margin_pct: Optional[float] = None
    start: Optional[str] = None
    end: Optional[str] = None
    # ── 선물 연속물 구성 (equity 심볼이면 무시) ──────────────────────────────────
    # 선물은 만기물 체인 → 단일 연속 시계열로 이어붙여 백테스트한다. 미지정(None)이면 상품
    # 카탈로그(exec_defaults.instrument_spec)의 default_roll/조정을 사용. 결과에 영향하는
    # 백테스트 선택이라 param_grid(path="simulation.roll_method")로 민감도 sweep 가능.
    #   roll_method: 언제 근월→차월로 옮겨탈지. days_before_N=만기 N영업일 전, volume_cross=
    #                거래량 역전 시, oi_cross=미결제 역전 시.
    #   series_adjust: 롤 시점 가격 점프 처리. none=원본 이어붙임(갭 유지), back_adjust=과거를
    #                  차감 조정(연속 수익 일관), ratio=비율 조정.
    roll_method: Optional[Literal["days_before_5", "days_before_1",
                                  "volume_cross", "oi_cross"]] = None
    series_adjust: Optional[Literal["none", "back_adjust", "ratio"]] = None
    # 만기물별 term-structure 데이터가 없을 때의 정직한 롤비용 폴백(%/롤, 양수=비용).
    # 실제 근월-원월 가격차 데이터가 있으면 그걸 쓰고 이 값은 무시(데이터 우선 — §7.6).
    roll_cost_pct: Optional[float] = None
    # 해외선물 손익 환산 기준 통화(상품 통화가 account와 다르면 환율 환산). 국내선물=KRW면 무환산.
    account_currency: Literal["KRW", "USD"] = "KRW"


# ── 펼침 (비전 §4) ────────────────────────────────────────────────────────────

class ParamAxis(BaseModel):
    """파라미터 격자의 한 축 — 경로 1개 × 값 목록."""
    path: str                           # "simulation.commission" 등 점경로
    values: list = Field(default_factory=list)


class Objective(BaseModel):
    """reduction=extremize 전용 목적함수. metric은 summarize_returns 산출 키만(정직)."""
    # ⚠ mdd는 음수%(낙폭) — "낙폭 최소화"는 direction="max"(0에 가까울수록 좋음).
    metric: Literal["sharpe", "sortino", "cagr", "cum_return", "mdd"] = "sharpe"
    direction: Literal["max", "min"] = "max"
    oos_guard: bool = True   # in-sample 최적을 시간폴드 OOS 일관성으로 교차검증(과최적화 가드)


class Study(BaseModel):
    """평면4 — 질문을 한 축으로 펼치고 환원(옛 SweepSpec + period_split 흡수)."""
    axis: Literal["none", "parameter", "entity", "label", "time_fold"] = "none"
    reduction: Literal["enumerate", "contrast", "consistency", "extremize"] = "enumerate"
    param_grid: list[ParamAxis] = Field(default_factory=list)
    assets: list[str] = Field(default_factory=list)
    label: Optional[Node] = None
    folds: int = 4
    split_dates: list[str] = Field(default_factory=list)
    target_node: Optional[Node] = None
    relation_kind: Literal["ic"] = "ic"
    event: Optional[Node] = None
    windows: list[int] = Field(default_factory=lambda: [5, 10, 20])
    event_basis: Literal["close", "intraday", "excess"] = "close"
    objective: Optional[Objective] = None   # reduction=extremize 목적함수(없으면 sharpe-max 기본)


# ── 선택 (SELECT 동사 — as-of 횡단 랭킹 스크리닝) ─────────────────────────────

class SelectSpec(BaseModel):
    """SELECT 동사 — as-of 스냅샷 횡단 랭킹 선별 설정."""
    as_of: str = "latest"               # "latest" 또는 ISO 날짜(그 시점 이하 마지막 단면)
    top_n: Optional[int] = None         # 상위 N (top_pct와 둘 중 하나)
    top_pct: Optional[float] = None     # 또는 상위 %(0<pct<=100)
    descending: bool = True             # score 큰 순(False=작은 순 — 예: 저PER)
    display: list[str] = Field(default_factory=list)   # 결과에 붙일 지표 컬럼(pb_ratio 등)


# ── 전략 (통합) ───────────────────────────────────────────────────────────────

class StrategyIR(BaseModel):
    name: str = "새 전략"
    universe: Universe = Field(default_factory=Universe)
    signal: Node                        # condition(룰) 또는 score(팩터)
    position: PositionSpec = Field(default_factory=PositionSpec)
    simulation: SimSpec = Field(default_factory=SimSpec)
    query: Literal["select", "describe", "relate", "simulate"] = "simulate"
    study: Study = Field(default_factory=Study)
    select: Optional[SelectSpec] = None    # query="select" 전용

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy(cls, data):
        """레거시 sweep/period_split 키를 명시 거부 — 클린 컷오버(번역 셰임 없음).

        StrategyIR은 extra="ignore"(pydantic 기본)라 제거된 sweep/period_split을 누가
        넘기면 *조용히 무시→study 기본값*이 되어 silent 오작동(NL repair loop도 못 잡음).
        따라서 alias 번역이 아니라 검증 에러로 loud하게 거부한다 — LLM이 레거시 산출 시
        repair loop가 교정하게.
        """
        if isinstance(data, dict):
            sim = data.get("simulation")
            if "sweep" in data or (isinstance(sim, dict)
                                   and ("period_split" in sim or "split_dates" in sim)):
                raise ValueError("레거시 'sweep'/'period_split' 제거됨 — 'query'/'study' 사용")
        return data


# ── 스키마 인트로스펙션 (LLM 교정 피드백용) ──────────────────────────────────────
# 검증 에러를 "fixable"하게 만들기 위해, 에러 위치(loc)의 모델 계약(필드 모양)을 스키마에서
# 자동 파생한다. 케이스별 하드코딩이 아니라 StrategyIR 모델을 걸어가며 그 자리의 필수·옵션
# 필드와 타입을 추출 → NL→IR 컴파일러 repair 루프가 그대로 인용해 LLM이 고칠 수 있게 한다.

def _unwrap_optional(ann):
    """Optional[X]/Union[...]에서 None을 빼고 대표 타입(BaseModel 우선)을 고른다."""
    origin = typing.get_origin(ann)
    if origin is typing.Union or origin is getattr(types, "UnionType", None):
        args = [a for a in typing.get_args(ann) if a is not type(None)]
        for a in args:
            if isinstance(a, type) and issubclass(a, BaseModel):
                return a
        return args[0] if args else ann
    return ann


def _type_label(ann) -> str:
    """타입 주석을 짧은 표기로(str·int·number·bool·list[...]·a|b·ModelName)."""
    ann = _unwrap_optional(ann)
    origin = typing.get_origin(ann)
    if origin is typing.Literal:
        return "|".join(str(v) for v in typing.get_args(ann))
    if origin in (list, tuple, set):
        args = typing.get_args(ann)
        return f"list[{_type_label(args[0])}]" if args else "list"
    if origin is dict:
        return "object"
    if isinstance(ann, type):
        return {str: "str", int: "int", float: "number", bool: "bool"}.get(ann, ann.__name__)
    return str(ann)


def _model_at(loc: tuple) -> Optional[type]:
    """StrategyIR 기준 loc(점경로 튜플)이 가리키는 위치의 컨테이너 BaseModel을 반환(아니면 None).
    BaseModel 필드·list 원소·Union을 따라 내려간다."""
    cur: object = StrategyIR
    for key in loc:
        cur = _unwrap_optional(cur)
        origin = typing.get_origin(cur)
        if isinstance(cur, type) and issubclass(cur, BaseModel):
            fi = cur.model_fields.get(str(key))
            if fi is None:
                return None
            cur = fi.annotation
        elif origin in (list, tuple, set):
            args = typing.get_args(cur)
            cur = args[0] if args else None
        elif origin is dict:
            args = typing.get_args(cur)
            cur = args[1] if len(args) == 2 else None
        else:
            return None
        if cur is None:
            return None
    cur = _unwrap_optional(cur)
    return cur if isinstance(cur, type) and issubclass(cur, BaseModel) else None


def field_contract(loc) -> Optional[str]:
    """loc 위치 모델의 계약을 한 줄로(LLM 교정 힌트).

    예: field_contract(("study","param_grid",0)) → "ParamAxis = { path: str(필수), values: list }".
    누락·타입 오류 시 _validate가 이 컨테이너 계약을 에러 메시지에 덧붙여 LLM이 모양을 고치게 한다.
    """
    model = _model_at(tuple(loc))
    if model is None:
        return None
    fields = [f"{n}: {_type_label(fi.annotation)}{'(필수)' if fi.is_required() else ''}"
              for n, fi in model.model_fields.items()]
    return f"{model.__name__} = {{ " + ", ".join(fields) + " }"


# ── 정합성 검증 ───────────────────────────────────────────────────────────────

def signal_out_type(node: Node) -> Optional[str]:
    return get(node.op).out_type.value if has(node.op) else None


def validate_strategy(s: StrategyIR, valid_refs: Optional[set] = None,
                      meta: Optional[DatasetMeta] = None) -> list[Issue]:
    """StrategyIR 정합성 — 블록 메타규칙 + 구조 규칙(신호타입×진입 등) + 무결성."""
    issues: list[Issue] = list(validate(s.signal, valid_refs))
    issues += meaningfulness_issues(s.signal, "signal")   # M2·M3 — 동어반복·모순·퇴화 윈도우
    st = signal_out_type(s.signal)
    ent, pos, u = s.position.entry, s.position, s.universe

    # 최상위 신호는 매매 가능한 타입이어야 — condition(룰 트리거) 또는 score(팩터 알파).
    # label(bucket·calendar)·scalar는 그룹라벨·국면 등 보조 역할일 뿐 신호 자체가 될 수 없다.
    # (엔진은 condition 외 전부를 score로 취급하므로 label 코드가 알파로 오해석되는 silent 결함 차단.)
    if st is not None and st not in ("condition", "score"):
        issues.append(Issue("S-signal", SEV_ERROR,
                            "최상위 신호는 condition(참/거짓) 또는 score(점수) 블록이어야 합니다.", "signal"))

    # M1 — 신호는 시장 데이터를 ≥1회 참조해야 한다. 순수 상수·산술식은 시장에 반응하지
    # 않아(예: const(5)>const(0) → 매일 매수) 백테스트가 무의미하다.
    if st in ("condition", "score") and not has_market_source(s.signal):
        issues.append(Issue("M-const", SEV_ERROR,
                            "신호가 시장 데이터를 참조하지 않습니다 — 상수·산술만으론 시장에 "
                            "반응하지 않아 백테스트가 무의미합니다.", "signal"))

    # 신호 타입 × 진입/방향 호환
    if ent.mode == "on_signal" and st != "condition":
        issues.append(Issue("S-entry", SEV_ERROR,
                            "on_signal 진입은 신호가 condition(참/거짓)이어야 합니다.", "signal"))
    if pos.direction == "long_short" and st != "score":
        issues.append(Issue("S-dir", SEV_ERROR,
                            "long_short는 신호가 score(점수)여야 순위로 롱·숏을 가릅니다.", "signal"))
    if pos.sizing.mode == "signal_proportional" and st != "score":
        issues.append(Issue("S-size", SEV_ERROR,
                            "신호비례 사이징은 신호가 score여야 합니다.", "position.sizing"))
    if ent.threshold is not None and st != "score":
        issues.append(Issue("S-select", SEV_ERROR,
                            "임계 선택(threshold)은 신호가 score(점수)여야 합니다 — "
                            "참/거짓 신호는 그 자체가 임계 선택이므로 condition 신호를 쓰세요.",
                            "position.entry"))

    # SELECT 동사 — 랭킹이므로 score 신호 필요 + select 설정 정합.
    if s.query == "select":
        if st != "score":
            issues.append(Issue("S-SEL", SEV_ERROR,
                                "select(스크리닝)은 랭킹용 score 신호가 필요합니다(condition 불가).",
                                "signal"))
        sel = s.select
        if sel is None:
            issues.append(Issue("S-SEL", SEV_ERROR,
                                "select 질의는 select 설정(top_n 또는 top_pct)이 필요합니다.",
                                "select"))
        else:
            if (sel.top_n is None) == (sel.top_pct is None):
                issues.append(Issue("S-SEL", SEV_ERROR,
                                    "top_n과 top_pct 중 정확히 하나를 지정하세요.", "select"))
            if sel.top_n is not None and sel.top_n < 1:
                issues.append(Issue("S-SEL", SEV_ERROR,
                                    "top_n은 1 이상이어야 합니다.", "select.top_n"))
            if sel.top_pct is not None and not (0 < sel.top_pct <= 100):
                issues.append(Issue("S-SEL", SEV_ERROR,
                                    "top_pct는 0 초과 100 이하여야 합니다.", "select.top_pct"))

    # M4 — 선택 파라미터 범위 (퇴화 선택 방지: top_n=0/음수, top_pct 범위밖이면 조용한 무거래)
    if ent.top_n is not None and ent.top_n < 1:
        issues.append(Issue("M-select", SEV_ERROR, "top_n은 1 이상이어야 합니다.", "position.entry"))
    if ent.top_pct is not None and not (0 < ent.top_pct <= 100):
        issues.append(Issue("M-select", SEV_ERROR, "top_pct는 0 초과 100 이하여야 합니다.", "position.entry"))

    # M6 — 유니버스/사이징 공허 조합 (경고: 선택이 무의미하거나 매수가 안 됨)
    if u.kind in ("single", "list") and ent.top_n is not None and ent.top_n > len(u.symbols):
        issues.append(Issue("M-vacuous", SEV_INTEGRITY_WARN,
                            f"top_n({ent.top_n})이 유니버스 종목 수({len(u.symbols)})보다 많습니다 — 전체 선택과 동일.",
                            "position.entry"))
    if (pos.sizing.mode == "fixed_weight" and pos.sizing.weights
            and u.kind in ("single", "list") and not (set(pos.sizing.weights) & set(u.symbols))):
        issues.append(Issue("M-vacuous", SEV_INTEGRITY_WARN,
                            "fixed_weight 가중치에 유니버스 종목이 하나도 없습니다 — 매수되지 않습니다.",
                            "position.sizing"))

    # 포지션 짝 제약 (비전 §3.3)
    if ent.mode in ("scheduled", "always") and pos.sizing.mode in ("fixed_amount", "pct_cash"):
        issues.append(Issue("S-pair", SEV_INTEGRITY_WARN,
                            "종목당 예산 사이징(fixed_amount·pct_cash)은 이벤트(on_signal) 진입용 — "
                            "스케줄·상시 진입에선 동일가중으로 처리됩니다.", "position.sizing"))
    if ent.mode == "always" and any((pos.exit.hold_days, pos.exit.take_profit, pos.exit.stop_loss,
                                      pos.exit.trail_pct, pos.exit.trail_atr_mult, pos.exit.condition)):
        issues.append(Issue("S-pair", SEV_INTEGRITY_WARN,
                            "상시 진입은 매일 리밸런싱이라 설정한 청산 규칙이 무시됩니다.", "position.exit"))

    # 유니버스
    if u.kind == "single" and len(u.symbols) != 1:
        issues.append(Issue("S-univ", SEV_ERROR, "단일 유니버스는 종목 1개가 필요합니다.", "universe"))
    if u.kind == "list" and not u.symbols:
        issues.append(Issue("S-univ", SEV_ERROR, "리스트 유니버스는 종목이 1개 이상 필요합니다.", "universe"))
    if ent.mode == "on_signal" and u.kind == "all":
        issues.append(Issue("S-univ", SEV_ERROR,
                            "전체 종목 유니버스는 정기리밸런싱(scheduled)·상시(always) 진입과 함께 쓰세요.",
                            "universe"))
    # 포트폴리오 진단 — describe 전용 대상. 보유 종목 필요, 비중 정합(축A — position 없음).
    if u.kind == "portfolio":
        if s.query != "describe":
            issues.append(Issue("S-PORT", SEV_ERROR,
                                "portfolio 유니버스는 진단(query=describe) 전용입니다 — "
                                "보유 기반 시뮬은 별도 대상입니다.", "universe"))
        if not u.symbols:
            issues.append(Issue("S-PORT", SEV_ERROR,
                                "포트폴리오 진단은 보유 종목(symbols)이 1개 이상 필요합니다.", "universe"))
        if u.weights:
            if any(float(v) <= 0 for v in u.weights.values()):
                issues.append(Issue("S-PORT", SEV_ERROR,
                                    "보유 비중(weights)은 양수여야 합니다.", "universe.weights"))
            stray = set(u.weights) - set(u.symbols)
            if stray:
                issues.append(Issue("M-vacuous", SEV_INTEGRITY_WARN,
                                    f"비중에 보유 외 종목이 있습니다: {sorted(stray)} — 무시됩니다.",
                                    "universe.weights"))
    sc = u.screener or {}
    cond = sc.get("condition")
    if cond:
        # 세부조건 = 선택 종목에 얹는 자격 필터. 필터·횡단순위를 AND/OR로 조합한 단일 condition.
        if not u.symbols:
            issues.append(Issue("S-univ", SEV_ERROR,
                                "세부조건은 선택한 종목이 있을 때만 설정할 수 있습니다.", "universe"))
        if sc.get("refresh", "each_rebalance") not in ("each_rebalance", "once_at_start"):
            issues.append(Issue("S-univ", SEV_ERROR,
                                "세부조건 재선별 시점이 올바르지 않습니다.", "universe"))
        try:
            cnode = Node.model_validate(cond)
        except Exception:                       # noqa: BLE001 — 잘못된 트리
            issues.append(Issue("S-univ", SEV_ERROR, "세부조건이 유효한 블록이 아닙니다.", "universe"))
        else:
            issues += list(validate(cnode, valid_refs))
            issues += meaningfulness_issues(cnode, "universe.condition")   # M2·M3
            if signal_out_type(cnode) != "condition":
                issues.append(Issue("S-univ", SEV_ERROR,
                                    "세부조건은 condition(참/거짓) 블록이어야 합니다 "
                                    "(예: 횡단순위(시총)≤50, 거래대금>임계).", "universe"))
            if not has_market_source(cnode):                              # M1
                issues.append(Issue("M-const", SEV_ERROR,
                                    "세부조건이 시장 데이터를 참조하지 않습니다.", "universe"))

    # 매도 조건 노드
    if pos.exit.condition is not None:
        issues += list(validate(pos.exit.condition, valid_refs))
        issues += meaningfulness_issues(pos.exit.condition, "exit.condition")   # M2·M3
        if not has_market_source(pos.exit.condition):                          # M1
            issues.append(Issue("M-const", SEV_ERROR,
                                "매도 조건이 시장 데이터를 참조하지 않습니다 — 항상 청산/미청산되어 무의미합니다.",
                                "exit.condition"))
        if signal_out_type(pos.exit.condition) != "condition":
            issues.append(Issue("S-exit", SEV_ERROR, "매도 조건은 condition 블록이어야 합니다.", "exit.condition"))

    # M5 — 청산 규칙 부호·범위 (관례: 익절>0%, 손절<0%, 보유기간≥1, 트레일링>0).
    # 부호가 틀리면 즉시청산/미청산으로 조용히 퇴화한다.
    ex = pos.exit
    if ex.hold_days is not None and ex.hold_days < 1:
        issues.append(Issue("M-exit", SEV_ERROR, "보유기간(hold_days)은 1 이상이어야 합니다.", "position.exit"))
    if ex.take_profit is not None and ex.take_profit <= 0:
        issues.append(Issue("M-exit", SEV_ERROR, "익절(take_profit)은 양수(%)여야 합니다.", "position.exit"))
    if ex.stop_loss is not None and ex.stop_loss >= 0:
        issues.append(Issue("M-exit", SEV_ERROR, "손절(stop_loss)은 음수(%)여야 합니다.", "position.exit"))
    if ex.trail_pct is not None and ex.trail_pct <= 0:
        issues.append(Issue("M-exit", SEV_ERROR, "트레일링(trail_pct)은 양수(%)여야 합니다.", "position.exit"))
    if ex.trail_atr_mult is not None and ex.trail_atr_mult <= 0:
        issues.append(Issue("M-exit", SEV_ERROR, "ATR 트레일링 배수(trail_atr_mult)는 양수여야 합니다.", "position.exit"))

    # 오버레이 (그룹 캡·낙폭 제어)
    ov = pos.overlays
    if ov.group_label is not None:
        issues += list(validate(ov.group_label, valid_refs))
        if signal_out_type(ov.group_label) != "label":
            issues.append(Issue("S-overlay", SEV_ERROR,
                                "그룹 노출 라벨(group_label)은 label 블록(구간분할·달력)이어야 합니다.",
                                "position.overlays"))
    if ov.max_group_pct is not None and ov.group_label is None:
        issues.append(Issue("S-overlay", SEV_ERROR,
                            "그룹 노출 캡(max_group_pct)은 group_label 블록이 필요합니다.", "position.overlays"))
    if (ov.max_drawdown_soft is not None and ov.max_drawdown_stop is not None
            and abs(ov.max_drawdown_soft) >= abs(ov.max_drawdown_stop)):
        issues.append(Issue("S-overlay", SEV_INTEGRITY_WARN,
                            "낙폭 soft가 hard 이상 — 부분 디리스킹 없이 binary kill로 동작합니다.",
                            "position.overlays"))

    # 펼침 (query=simulate의 study.axis 분기 + describe/relate 분석)
    st = s.study
    if st.axis == "label" and st.label is None:
        issues.append(Issue("S-sweep", SEV_ERROR, "조건축 펼침은 라벨 블록이 필요합니다.", "study"))
    if st.axis == "parameter" and (not st.param_grid
                                   or any(not ax.values for ax in st.param_grid)):
        issues.append(Issue("S-sweep", SEV_ERROR,
                            "파라미터축 펼침은 param_grid(경로·값 목록)가 필요합니다.", "study"))
    if st.axis == "entity" and not st.assets:
        issues.append(Issue("S-sweep", SEV_ERROR, "자산축 펼침은 assets(종목 목록)가 필요합니다.", "study"))
    # extremize(최적화) — 검색공간 축(parameter/entity) + simulate 동사 필요.
    if st.reduction == "extremize":
        if s.query != "simulate":
            issues.append(Issue("S-OPT", SEV_ERROR,
                                "extremize(최적해)는 손익 지표 기반이라 simulate 동사에서만 동작합니다.", "study"))
        if st.axis not in ("parameter", "entity"):
            issues.append(Issue("S-OPT", SEV_ERROR,
                                "extremize는 검색공간(axis=parameter 또는 entity)이 필요합니다.", "study"))
    # relate + event 설정 = 이벤트 스터디 모드(옛 axis="time").
    if s.query == "relate" and st.event is not None:
        if signal_out_type(st.event) != "condition":
            issues.append(Issue("S-event", SEV_ERROR,
                                "이벤트 분석은 이벤트 신호가 condition(발생 여부)이어야 합니다.", "study.event"))
        if not st.windows:
            issues.append(Issue("S-event", SEV_ERROR, "이벤트 분석은 forward 윈도우가 필요합니다.", "study.windows"))
        if st.event_basis == "excess" and u.kind == "single":
            issues.append(Issue("S-event", SEV_ERROR,
                                "초과수익(excess) 기준은 시장 지수 생성을 위해 종목이 2개 이상이어야 합니다.",
                                "study.event_basis"))
    if st.label is not None:
        issues += list(validate(st.label, valid_refs))
        if signal_out_type(st.label) != "label":
            issues.append(Issue("S-sweep", SEV_ERROR,
                                "펼침 분할 라벨(study.label)은 label 블록(구간분할·국면·달력)이어야 합니다.",
                                "study.label"))
    if st.event is not None:
        issues += list(validate(st.event, valid_refs))

    # 분석(query=describe·relate) — 분석 노드(target_node) 필요·타입·시장참조 검증.
    # (relate + event는 이벤트 스터디라 target_node 없이 동작 → IC 모드일 때만 target_node 요구.)
    is_ic = s.query == "relate" and st.event is None
    describe_dist = s.query == "describe" and u.kind in ("all", "list")
    if describe_dist or is_ic:
        tn = st.target_node
        if tn is None:
            issues.append(Issue("S-target", SEV_ERROR,
                                "신호값/관계 분석은 분석 노드(target_node)가 필요합니다.",
                                "study.target_node"))
        else:
            issues += list(validate(tn, valid_refs))
            issues += meaningfulness_issues(tn, "study.target_node")
            if signal_out_type(tn) not in ("score", "condition"):
                issues.append(Issue("S-target", SEV_ERROR,
                                    "분석 노드는 score(점수) 또는 condition 블록이어야 합니다.",
                                    "study.target_node"))
            if not has_market_source(tn):
                issues.append(Issue("M-const", SEV_ERROR,
                                    "분석 노드가 시장 데이터를 참조하지 않습니다.", "study.target_node"))
        if is_ic:
            if not st.windows:
                issues.append(Issue("S-target", SEV_ERROR,
                                    "IC 분석은 forward 윈도우(windows)가 필요합니다.", "study.windows"))
            if s.universe.kind == "single":
                issues.append(Issue("S-target", SEV_ERROR,
                                    "IC(횡단 상관) 분석은 종목이 2개 이상이어야 합니다.", "universe"))

    # 기간분할 × 펼침 동시 사용 금지 (2D 모호성 차단). study가 평면이라 time_fold가 다른
    # 펼침/분석 필드(param_grid·assets·label·target_node·event)나 비-simulate 동사와
    # 공존하면 2D 모호성이 생기므로 거부 — time_fold는 단독으로만 쓴다.
    if st.axis == "time_fold" and (st.label is not None or st.param_grid or st.assets
                                   or st.target_node is not None or st.event is not None
                                   or s.query != "simulate"):
        issues.append(Issue("S-split", SEV_ERROR,
                            "기간분할과 펼침/분석은 동시에 쓸 수 없습니다 — 하나만 선택하세요.", "simulation"))

    # 선물 — 연속물 설정(roll_method·series_adjust·roll_cost_pct)이 비선물 유니버스에 걸리면
    # 조용히 무시된다 → 경고(silent no-op 방지, M-vacuous 계열). 단일·리스트 유니버스에서만 판정.
    sim = s.simulation
    if (sim.roll_method is not None or sim.series_adjust is not None
            or sim.roll_cost_pct is not None) and u.kind in ("single", "list"):
        if not any(is_futures(x) for x in u.symbols):
            issues.append(Issue("S-futures", SEV_INTEGRITY_WARN,
                                "선물 연속물 설정(roll_method·series_adjust·roll_cost_pct)은 선물 심볼에만 "
                                "적용됩니다 — 현재 유니버스에 선물이 없어 무시됩니다.", "simulation"))

    # 혼합 증거금 — 한 유니버스에 주식과 선물이 섞이면 증거금 회계가 최저 증거금률 기준 단일
    # 레버리지(lev_eff=lev/min(margin_rate))로 근사된다. 사이징은 심볼별 증거금률로 정확하나
    # 마진콜 복원·floor_cash 한도는 포트폴리오 단일값이라 주식 다리를 과복원할 수 있음 → 경고.
    if u.kind in ("single", "list") and u.symbols:
        n_fut = sum(1 for x in u.symbols if is_futures(x))
        if 0 < n_fut < len(u.symbols):
            issues.append(Issue("S-futures-mix", SEV_INTEGRITY_WARN,
                                "주식과 선물이 한 유니버스에 섞여 있습니다 — 증거금 회계가 최저 증거금률 "
                                "기준 단일 레버리지로 근사되어(마진콜 복원이 주식 다리를 과복원) 부정확할 수 "
                                "있습니다. 동질(전부 선물 또는 전부 주식) 유니버스를 권장합니다.", "universe"))

    # (E1b 이후 선물 on_signal 진입이 증거금 보유 포지션으로 지원됨 — S-futures-event 경고 제거.)

    # 무결성
    if meta is None:
        meta = DatasetMeta(delay=s.simulation.delay)
    issues += list(integrity_issues(s.signal, meta))
    return prioritize(issues)


def needed_symbols(s: StrategyIR) -> Optional[set[str]]:
    """전략이 실제로 참조하는 심볼 집합 — 부분집합 로드용. None=전 유니버스 필요.

    single/list: universe.symbols ∪ 모든 노드(신호·매도조건·그룹라벨·펼침)가 명시
    참조한 외부심볼("VIX.Close" 등). 이 집합만 로드·지표계산하면 전 유니버스(수천)
    통째 빌드 없이 동일 결과를 얻는다(test_backtest_golden의 차등 불변식이 보장).

    all: 횡단 랭킹이라 후보 전체가 필요 → None(전 유니버스 로드).
    엔진의 _universe_symbols(all 경로)가 dataset 전체를 후보로 보는 것과 같은 경계.
    """
    if s.universe.kind == "all":
        return None
    syms: set[str] = set(s.universe.symbols)
    nodes = [s.signal, s.position.exit.condition, s.position.overlays.group_label,
             s.study.label, s.study.event, s.study.target_node]
    sc = (s.universe.screener or {}).get("condition")
    if sc is not None:
        try:
            nodes.append(Node.model_validate(sc))
        except Exception:                      # noqa: BLE001
            return None                        # 잘못된 트리면 안전하게 전체
    for nd in nodes:
        if nd is not None:
            syms |= referenced_symbols(nd)
    # 전략 조합(strat:<id>) 참조가 있으면 자식 전략이 임의 데이터(전 유니버스 팩터 등)를
    # 필요로 할 수 있어 부분집합으로 좁힐 수 없다 → 전체 로드(None)로 안전하게 폴백.
    if any(str(x).startswith("strat:") for x in syms):
        return None
    return syms


def needed_columns(s: StrategyIR) -> Optional[set[str]]:
    """전략이 참조하는 지표 컬럼 집합 — 컬럼 프로젝션(compute_columns)용. None=전 컬럼 필요.

    명시 참조(신호·매도조건·그룹라벨·스크리너조건·펼침 노드) ∪ 암묵 의존. 엔진이
    데이터-ref 밖에서 직접 읽는 컬럼은 **atr_14(ATR 트레일링 스톱)** 뿐임을 확인했다
    (engine.py·backtest.py). 사이징 변동성은 Close에서 즉석 계산이라 저장 컬럼 의존 없음.

    이 집합을 compute_columns(df, cols)에 주면 그 지표만 계산 — 값은 compute_all과
    byte 동일(각 add_*가 순수, test_backtest_golden의 프로젝션 불변식이 보장).
    strat:<id> 조합은 자식이 임의 지표를 쓸 수 있어 None(전체)로 안전 폴백.
    """
    if any(str(x).startswith("strat:") for x in s.universe.symbols):
        return None
    nodes = [s.signal, s.position.exit.condition, s.position.overlays.group_label,
             s.study.label, s.study.event, s.study.target_node]
    # 스크리너 선별 조건도 참조 컬럼(후보를 이 조건으로 거른다).
    if s.universe.screener:
        cond = s.universe.screener.get("condition")
        if cond is not None:
            try:
                nodes.append(Node.model_validate(cond))
            except Exception:                    # noqa: BLE001 — 잘못된 트리면 안전하게 전체
                return None
    cols: set[str] = set()
    for nd in nodes:
        if nd is not None:
            if any(str(x).startswith("strat:") for x in referenced_symbols(nd)):
                return None
            cols |= referenced_columns(nd)
    # 암묵 의존 — ATR 트레일링은 엔진이 atr_14를 직접 읽는다(데이터-ref 아님).
    if s.position.exit.trail_atr_mult is not None:
        cols.add("atr_14")
    return cols

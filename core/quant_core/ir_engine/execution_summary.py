"""전략 IR + exec_defaults + 상품카탈로그 → 사용자용 실행 명세 4분류 요약.

확정=전략 설정값 · 가정=시스템/백테스트 default(명시) · 발주시점=실시간 결정 · 미지=사후/외부.
엔진 사실의 단일 출처(core) — 웹/서버가 이걸 렌더만 한다(가정값을 TS에 중복하지 않음 → 드리프트 방지).

읽기 전용 파생(전략 변경 0). 가정값(수수료·슬리피지·tolerance·갭필터)은 "가정"이라 명시해
실율·실슬리피지로 오해하지 않게 한다(정직 — 사장님 신뢰 요구의 핵심).
"""
from __future__ import annotations

from ..exec_defaults import instrument_spec, merged_execution
from .spec import StrategyIR

_DIR = {"long": "롱(매수)", "short": "숏(매도)", "long_short": "롱/숏"}

_REBALANCE = {
    "daily": "매일",
    "weekly": "매주",
    "monthly": "매월",
    "quarterly": "분기마다",
    "annual": "매년",
    "every_n_days": "N거래일마다",
}


def execution_summary(strategy_def: dict) -> dict:
    """전략 정의(dict) → 실행 명세 4분류 요약.

    반환: {"confirmed": [{label,value}], "assumed": [{label,value}],
           "at_order": [str], "unknown": [str]}
    """
    ir = StrategyIR.model_validate(strategy_def)
    ex = merged_execution(strategy_def.get("execution"))
    pos = ir.position
    siz = pos.sizing
    exit_ = pos.exit
    syms = ir.universe.symbols or []
    is_fut = any(instrument_spec(s).asset_class == "futures" for s in syms)

    confirmed = [
        {"label": "종목", "value": _universe_desc(ir.universe)},
        {"label": "방향", "value": _DIR.get(pos.direction, pos.direction)},
        {"label": "사이징", "value": _sizing_desc(ir.universe, siz, is_fut)},
        {"label": "청산", "value": _exit_desc(exit_)},
        {"label": "진입 시점", "value": _entry_desc(pos.entry)},
    ]

    assumed = [
        {"label": "수수료",
         "value": f"{_comm_pct(ir, ex):.2f}% (가정 — 실제 계좌 수수료율 아님)"},
        {"label": "매도세", "value": f"{ex['bt_sell_tax_bps'] / 100:.2f}% (가정)"},
        {"label": "슬리피지(백테스트 가정)", "value": f"{ex['bt_slippage_bps'] / 100:.2f}%"},
        {"label": "주문 가격",
         "value": f"국내 시장가(단일가) · 미국 지정가 ±{ex['buy_tolerance_pct']}%"},
        {"label": "갭 필터", "value": f"전일比 {ex['gap_filter_pct']}% 초과 갭 신규진입 폐기"},
        {"label": "가격 제한", "value": "국내 ±30% 클램프"},
    ]
    if is_fut:
        sp = next(instrument_spec(s) for s in syms
                  if instrument_spec(s).asset_class == "futures")
        # 엔진 mr 소비(engine.py 양 경로)와 동일 규칙 — override 지정 시 카탈로그가 아닌
        # 그 값을 서술해야 자기서술이 실제 계산과 일치한다(G3 스모크 실측: 봇이 '미반영' 오판).
        mro = ir.simulation.margin_rate_override
        mr = mro if mro else sp.init_margin_rate
        lev = round(1.0 / mr, 1) if mr else None
        lev_txt = f"최대 {lev}x · " if lev is not None else ""
        src = " (전략 지정 오버라이드)" if mro else ""
        assumed.append({
            "label": "레버리지(선물)",
            "value": f"{lev_txt}개시증거금률 {mr * 100:.0f}%{src} · 승수 {sp.multiplier:,.0f}",
        })

    at_order = [
        "실제 수량/계약수 (발주 시점 주문가능현금으로 계산)",
        "실제 주문 가격 (발주 시점 현재가)",
    ]
    if is_fut:
        at_order.append("선물 명목·증거금 (= 계약수 × 가격 × 승수, 증거금 = 명목 × 증거금률)")

    unknown = [
        "실제 수수료 (실전 청산 후 KIS 조회로 확정)",
        "실제 체결 슬리피지 (체결 후 사후 측정)",
    ]

    return {"confirmed": confirmed, "assumed": assumed,
            "at_order": at_order, "unknown": unknown}


def methodology_brief(strategy_def: dict, period: str | None = None) -> str:
    """백테스트 실행명세 → 모델 식단용 1블록 방법론 텍스트(provenance.ir_summary의 모델 표면).

    모델이 '무엇을 어떻게 돌렸나'(기간·기준자본·종목·방향·사이징·진입(신호기준)·청산·체결가정)를
    보고 답에 백테스트 로직을 서술하게 한다(증상 #7·#3·#1 — 방법론 미서술). execution_summary와
    동일 출처라 드리프트가 없다. 기간은 결과의 실제 구간이라 호출측이 주입한다(여기선 IR만 봄).
    파싱 실패는 빈 문자열(best-effort — 모델 요약을 깨지 않는다).
    """
    try:
        ir = StrategyIR.model_validate(strategy_def)
        spec = execution_summary(strategy_def)
    except Exception:   # noqa: BLE001 — 방법론 주석 실패가 모델 요약을 깨면 안 됨
        return ""
    conf = {d["label"]: d["value"] for d in spec["confirmed"]}
    asm = {d["label"]: d["value"] for d in spec["assumed"]}
    bits: list[str] = []
    if period:
        bits.append(f"기간 {period}")
    cap = ir.simulation.initial_capital
    if cap:
        bits.append(f"기준자본 {cap:,.0f}원")
    for lbl in ("종목", "방향", "사이징", "진입 시점", "청산"):
        if conf.get(lbl):
            bits.append(f"{lbl} {conf[lbl]}")
    for lbl in ("수수료", "슬리피지(백테스트 가정)", "레버리지(선물)"):
        if asm.get(lbl):
            bits.append(f"{lbl.split('(')[0]} {asm[lbl]}")
    return "[분석 방법] " + " · ".join(bits) if bits else ""


# ── 헬퍼 ──────────────────────────────────────────────────────────────────────

def _universe_desc(universe) -> str:
    """유니버스를 평문으로: single→심볼·list→N종목·all/screener→설명."""
    syms = universe.symbols or []
    if universe.screener and universe.screener.get("condition") is not None:
        return "스크리너 선별"
    if universe.kind == "single":
        return syms[0] if syms else "단일 종목"
    if universe.kind == "all":
        return "전체 종목"
    # list / portfolio
    if len(syms) == 1:
        return syms[0]
    return f"{len(syms)}종목"


def _sizing_desc(universe, siz, is_fut: bool) -> str:
    """사이징을 평문으로. 선물=증거금 N% · 주식=현금%/정액/모드명."""
    if is_fut:
        return f"가용현금의 {_fmt(siz.futures_margin_pct)}%(증거금)"
    if siz.mode == "fixed_amount" and siz.amount_krw is not None:
        return f"종목당 {siz.amount_krw:,.0f}원"
    if siz.mode == "pct_cash":
        # 단일 종목 + 자본% = 사실상 현금 전량(1종목에 amount_pct는 의미 약함).
        if universe.kind == "single":
            return "현금 100%"
        return f"자본의 {_fmt(siz.amount_pct)}%"
    return {
        "equal_weight": "동일가중",
        "signal_proportional": "신호비례",
        "vol_inverse": "변동성 역가중",
        "target_vol": "목표변동성",
        "fixed_weight": "정적 배분",
    }.get(siz.mode, siz.mode)


def _exit_desc(exit_) -> str:
    """청산 규칙을 OR 결합 평문으로. 채워진 규칙만. hold_days==0 → 당일 종가 청산."""
    parts: list[str] = []
    if exit_.stop_loss is not None:
        parts.append(f"손절 {_fmt(exit_.stop_loss)}%")
    if exit_.take_profit is not None:
        parts.append(f"익절 +{_fmt(exit_.take_profit)}%")
    if exit_.trail_pct is not None:
        parts.append(f"트레일링 {_fmt(exit_.trail_pct)}%")
    if exit_.trail_atr_mult is not None:
        parts.append(f"ATR 트레일링 ×{_fmt(exit_.trail_atr_mult)}")
    if exit_.hold_days is not None:
        if exit_.hold_days == 0:
            parts.append("당일 종가 청산")
        else:
            parts.append(f"보유 {exit_.hold_days}일")
    if exit_.condition is not None:
        parts.append("매도 조건 충족")
    if not parts:
        return "청산 규칙 없음(수동/리밸런스)"
    return " · ".join(parts)


def _entry_desc(entry) -> str:
    """진입 시점을 평문으로. 신호 평가는 전일 데이터(look-ahead 방지)임을 명시."""
    if entry.mode == "on_signal":
        return "신호 발생 시 (신호는 전일 데이터로 평가)"
    if entry.mode == "always":
        return "상시(매일 리밸런스)"
    # scheduled
    if entry.rebalance == "every_n_days" and entry.every_n_days:
        return f"{entry.every_n_days}거래일마다 리밸런스 (신호는 전일 데이터로 평가)"
    cycle = _REBALANCE.get(entry.rebalance, entry.rebalance)
    return f"{cycle} 리밸런스 (신호는 전일 데이터로 평가)"


def _comm_pct(ir, ex) -> float:
    """수수료율(%). 전략이 simulation.commission을 지정했으면 그 값(×100),
    아니면 백테스트 default(bt_commission_bps/100). 단일 결정(fallback 아님)."""
    if ir.simulation.commission is not None:
        return ir.simulation.commission * 100
    return ex["bt_commission_bps"] / 100


def _fmt(x: float) -> str:
    """정수면 정수로, 아니면 소수 그대로(불필요한 .0 제거)."""
    if x == int(x):
        return str(int(x))
    return str(x)

"""IR 라이브 실행 결정 헬퍼 — 로컬앱 사이클·서버 preview가 공유.

백테스트 엔진과 같은 IR 스펙(position.exit·position.sizing)을 단일 포지션·종목에 적용해
**backtest=live 일치**를 보장한다. 순수 함수 — 브로커·네트워크·DB 의존 없음(단위 검증 가능).

- cycle_exit_reason: EOD 사이클 청산(보유기간 + 매도조건). 가격기반(익절·손절·트레일)은
  operand와 동일하게 intraday_loop가 전담하므로 여기선 평가하지 않는다(EOD 이중발주 방지).
- intraday_exit_reason: 장중 tick 가격청산(익절·손절·트레일) — 엔진 price_exit_reason 공유.
- event_buy_qty: 이벤트(on_signal) 진입 종목당 정수 수량 — 엔진 run_unified의 _budget과 동일.
"""

from __future__ import annotations

from typing import Optional

import pandas as pd

from ..blocks import EvalContext, evaluate
from ..exec_defaults import instrument_spec
from .engine import _scoped, price_exit_reason
from .spec import StrategyIR


def cycle_exit_reason(strategy: StrategyIR, *, held_days: int,
                      dataset: dict, symbol: str,
                      is_close: bool = False) -> Optional[str]:
    """로컬앱 EOD 사이클 청산 사유 — 보유기간 + 매도조건(Node). 없으면 None.

    신호·시간 기반만. 가격기반(tp/sl/trail)은 intraday_loop가 장중 tick으로 전담.
    매도조건 Node는 dataset에서 평가해 symbol의 마지막 바 값을 본다([이 종목]=symbol
    으로 치환; 외부 참조 종목은 _scoped가 보존).

    **당일매매(hold_days==0)**: 백테스트는 진입한 바의 종가에 청산한다(시가 진입 → 같은 날
    종가 청산). 라이브는 사이클이 아침(08:55)·종가(15:25/15:43) 둘로 나뉘므로 호출자가
    `is_close`로 어느 사이클인지 알린다 — 종가 사이클에서만 당일 청산해 backtest=live를 맞춘다.
      · is_close=True  → "당일청산"(종가 사이클: 당일 청산)
      · is_close=False·held_days==0 → None(아침 진입 직후: 종가까지 보유)
      · is_close=False·held_days>=1 → "보유기간"(종가 사이클을 놓쳐 보유가 익일로 넘어감 →
        다음 아침 안전청산)
    ⚠ hold_days==0은 반드시 `== 0`으로 검사한다 — None·>=1은 아래 기존 로직으로 떨어져
    byte-identical(골든·회귀 보존).
    """
    exit_spec = strategy.position.exit
    if exit_spec.hold_days == 0:               # 당일매매(day-trade)
        if is_close:
            return "당일청산"                   # 종가 사이클 — 청산
        if held_days >= 1:
            return "보유기간"                   # 종가 사이클 놓쳐 보유가 넘어감 → 다음 아침 안전청산
        return None                             # 아침 진입 직후 — 종가까지 보유(청산 안 함)
    # (이하 기존 로직 그대로 — hold_days>=1·매도조건)
    if exit_spec.hold_days is not None and held_days >= exit_spec.hold_days:
        return "보유기간"
    cond = exit_spec.condition
    if cond is not None:
        df = dataset.get(symbol)
        if df is not None and not getattr(df, "empty", True):
            ctx = EvalContext.from_dataset(_scoped(dataset, [symbol], cond))
            try:
                panel = evaluate(cond, ctx)
            except Exception:  # noqa: BLE001 — 평가 실패 시 청산 보류(안전)
                panel = None
            series = None
            if isinstance(panel, pd.DataFrame) and symbol in panel.columns:
                series = panel[symbol]
            elif isinstance(panel, pd.Series):
                series = panel
            if series is not None and len(series):
                val = series.iloc[-1]
                if pd.notna(val) and bool(val):
                    return "매도조건"
    return None


def event_buy_qty(strategy: StrategyIR, *, cash: float, prev_close: float,
                  capital: Optional[float] = None, symbol: Optional[str] = None) -> int:
    """이벤트(on_signal) IR 진입 종목당 수량 — 엔진 _open과 동일.

    예산: 주식 = amount_krw(fixed_amount) 또는 cash×amount_pct%(단일 종목은 100% 전액).
    **선물 = cash×futures_margin_pct%(증거금 사용률, 기본 20%)** — 유저가 조절하는 레버리지
    안전상한(100%면 full-margin). 엔진 _budget과 동일 식.
    선물이면 **계약수 = floor(예산/(px×승수×증거금률))**(예산=증거금 → 명목 레버리지; 엔진 E1b
    _open과 일치). 주식이면 정수주 = floor(예산/px). symbol 미지정 시 단일 유니버스면 그 종목으로 추론.
    횡단 사이저(equal/vol/target_vol/fixed_weight)는 스케줄·상시 전용 — 호출자가 preview 수량 사용.
    max_position_pct(<100, 주식만)면 비중 상한 클램프. capital 미지정이면 cash 사용.
    """
    if prev_close <= 0:
        return 0
    sz = strategy.position.sizing
    u = strategy.universe
    single = (u.kind == "single" and len(u.symbols or []) == 1)
    sym = symbol or (u.symbols[0] if (single and u.symbols) else None)
    spec = instrument_spec(sym) if sym else None
    is_fut = spec is not None and spec.asset_class == "futures"
    if sz.mode == "fixed_amount" and sz.amount_krw:
        budget = float(sz.amount_krw)
    elif is_fut:
        # 선물: 예산 = 가용현금 × 증거금 사용률(기본 20%). 엔진 _budget과 동일.
        budget = cash * (sz.futures_margin_pct / 100.0)
    else:
        eff_pct = 100.0 if single else sz.amount_pct
        budget = cash * (eff_pct / 100.0)
    if is_fut:
        # 선물: 예산=증거금 → 계약수=floor(증거금/(px×승수×증거금률)). 엔진 E1b _open과 일치.
        denom = prev_close * spec.multiplier * spec.init_margin_rate
        return max(0, int(budget / denom)) if denom > 0 else 0
    qty = int(budget // prev_close)
    cap = capital if capital is not None else cash
    mp = sz.max_position_pct
    if mp is not None and mp < 100.0 and cap > 0:
        qty = min(qty, int((cap * mp / 100.0) // prev_close))
    return max(0, qty)


def intraday_exit_reason(strategy: StrategyIR, *, cur_price: float, entry_price: float,
                         peak_price: float, atr_v: Optional[float] = None) -> Optional[str]:
    """IR 장중 가격청산 사유(익절·손절·트레일).

    백테스트 엔진과 같은 `price_exit_reason`을 호출 → backtest=live 단일 소스. 라이브 tick은
    단일 peak_price(장중 고가 워터마크)를 ATR·퍼센트 트레일 기준 둘 다에 전달. 보유기간·매도조건은
    EOD `cycle_exit_reason`이 전담하므로 여기선 가격기반만 — 미발동이면 None.
    사유에 "(intraday)" 접미 — 결정 로그에서 EOD 청산과 구분.
    """
    if entry_price <= 0 or cur_price <= 0:
        return None
    cur_ret = (cur_price - entry_price) / entry_price * 100
    r = price_exit_reason(cur_ret=cur_ret, close=cur_price, peak_high=peak_price,
                          peak_close=peak_price, atr_v=atr_v, exits=strategy.position.exit)
    return f"{r}(intraday)" if r else None

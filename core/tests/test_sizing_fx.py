"""사이징 FX(F-01) — fixed_amount(₩정액)의 USD 상품 환산 + 룩업 공유 계약.

2026-06-12 라이브 실증 결함: "구글 일일 100만원 시가매수"가 amount_krw(₩1,000,000)를
통화 환산 없이 USD 가격($353)으로 나눠 2,832주(≈$93만) 발주를 시도했다. 백테스트
엔진(run_unified._budget)과 라이브(event_buy_qty)가 같은 식을 공유해 양쪽이 같은
배수로 깨졌다(패리티 통과 ≠ 정확).

이 파일이 고정하는 수정 계약:
  - 환율 룩업 = dataset["원달러환율"].Close의 as-of(의사결정 시점 마지막 가용) 종가.
    백테스트 `_budget`·라이브 `event_buy_qty`가 같은 `fx_usdkrw_rate`를 공유 →
    backtest=live 패리티가 정의상 성립.
  - 비-KRW(USD) 상품 + fixed_amount → qty = floor((amount_krw / fx) / price).
  - KRW 상품은 byte-identical 무변경(FX 시계열 불요).
  - FX 미가용 시 fallback 금지(추측 환율 금지): 엔진=명시 에러(시계열 부재)/진입 보류
    (해당 바 이전 값 없음), 라이브=0수량(발주 보류 — 호출자가 수량 부족으로 표면화).

    cd platform/core && python -m pytest tests/test_sizing_fx.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.blocks import const, data
from quant_core.blocks.node import Node
from quant_core.ir_engine import (Entry, Exit, PositionSpec, SimSpec, Sizing,
                                   StrategyIR, Universe, needed_symbols,
                                   run_strategy_ir)
from quant_core.ir_engine.live import event_buy_qty

_FX = "원달러환율"          # dataset 매크로 키 (data_fetcher.MACRO_FRED_SYMBOLS → FRED DEXKOUS)
_IDX = pd.date_range("2026-05-27", periods=3, freq="B")


def _bars(open_: float, close: float, idx=_IDX) -> pd.DataFrame:
    o = np.full(len(idx), float(open_))
    c = np.full(len(idx), float(close))
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c),
                         "Close": c, "Volume": 1e6}, index=idx)


def _fx_df(rates, idx=_IDX) -> pd.DataFrame:
    if np.isscalar(rates):
        rates = [float(rates)] * len(idx)
    return pd.DataFrame({"Close": [float(r) for r in rates]}, index=idx)


def _always_true() -> Node:
    return Node(op="compare", params={"op": ">"},
                inputs={"left": data("Close"), "right": const(0)})


def _fixed_amount_ir(symbol: str, amount: float) -> StrategyIR:
    """당일매매(시가진입·종가청산) + 정액(₩) 사이징 — 실증 사고 전략 형태."""
    return StrategyIR(
        signal=_always_true(),
        universe=Universe(kind="single", symbols=[symbol]),
        position=PositionSpec(entry=Entry(mode="on_signal"),
                              sizing=Sizing(mode="fixed_amount", amount_krw=amount),
                              exit=Exit(hold_days=0)),
        simulation=SimSpec(initial_capital=1e7, commission=0.0, slippage=0.0,
                           sell_tax=0.0))


def _entry_qty(res: dict, per_share_loss: float, initial: float = 1e7) -> int:
    """첫 트레이드 수량 역산 — trades에 수량 컬럼이 없어 equity 변화로 도출.

    비용 0 + 당일매매(바1 시가진입·동일바 종가청산)에서
    equity[1] = initial − qty×(진입가−청산가).
    """
    return round((initial - float(res["equity"].iloc[1])) / per_share_loss)


def _strat(sizing: dict, universe: dict) -> StrategyIR:
    return StrategyIR.model_validate({
        "name": "t", "universe": universe, "signal": _always_true().model_dump(),
        "position": {"direction": "long", "sizing": sizing,
                     "entry": {"mode": "on_signal"}, "exit": {}, "overlays": {}},
        "simulation": {},
    })


# ── 룩업 (fx_usdkrw_rate) — 단일 as-of 룩업의 의미 고정 ────────────────────────

def test_fx_symbol_is_data_fetcher_macro_key():
    """환산에 쓰는 심볼 키가 수집 카탈로그(MACRO_FRED_SYMBOLS·DEXKOUS)와 동일 리터럴."""
    from quant_core import data_fetcher
    from quant_core.exec_defaults import FX_USDKRW_SYMBOL
    assert FX_USDKRW_SYMBOL == _FX
    assert FX_USDKRW_SYMBOL in data_fetcher.MACRO_FRED_SYMBOLS   # = "DEXKOUS"
    assert FX_USDKRW_SYMBOL in data_fetcher.MACRO_SYMBOLS        # 라이브 dataset 안전망 포함


def test_fx_rate_asof_lookup():
    from quant_core.ir_engine.engine import fx_usdkrw_rate
    idx = pd.DatetimeIndex(["2026-05-04", "2026-05-06"])
    ds = {_FX: pd.DataFrame({"Close": [1350.0, 1370.0]}, index=idx)}
    # as-of = 해당 시점 이전(≤) 마지막 가용 종가
    assert fx_usdkrw_rate(ds, asof="2026-05-04") == 1350.0
    assert fx_usdkrw_rate(ds, asof="2026-05-05") == 1350.0   # 휴장/미발표일 → 직전값
    assert fx_usdkrw_rate(ds, asof="2026-05-06") == 1370.0
    assert fx_usdkrw_rate(ds) == 1370.0                       # asof 미지정 = 마지막 가용
    # 미가용 → None (fallback 금지 — 호출자가 사이징 보류)
    assert fx_usdkrw_rate(ds, asof="2026-05-01") is None      # 첫 값 이전
    assert fx_usdkrw_rate({}, asof="2026-05-06") is None      # 시계열 없음
    assert fx_usdkrw_rate(None) is None                       # dataset 없음(라이브 미배선)
    assert fx_usdkrw_rate({_FX: pd.DataFrame()}) is None      # 빈 프레임
    zero = {_FX: pd.DataFrame({"Close": [0.0]}, index=idx[:1])}
    assert fx_usdkrw_rate(zero) is None                       # 0/음수 환율은 무효


# ── event_buy_qty (라이브·서버 preview 공유) ──────────────────────────────────

def test_event_qty_usd_fixed_amount_converts():
    """실증 사고 재현: GOOG($353)에 ₩1,000,000 → 2주 (현행 결함은 2,832주)."""
    s = _strat({"mode": "fixed_amount", "amount_krw": 1_000_000},
               {"kind": "single", "symbols": ["GOOG"]})
    ds = {_FX: _fx_df(1370.0)}
    assert event_buy_qty(s, cash=50_000, prev_close=353.0, dataset=ds) == 2


def test_event_qty_usd_fixed_amount_without_fx_holds():
    """FX 미가용 → 0수량(발주 보류). KRW 가정 fallback(1,370배 과대) 금지."""
    s = _strat({"mode": "fixed_amount", "amount_krw": 1_000_000},
               {"kind": "single", "symbols": ["GOOG"]})
    assert event_buy_qty(s, cash=50_000, prev_close=353.0) == 0              # dataset 미전달(현 라이브 배선)
    assert event_buy_qty(s, cash=50_000, prev_close=353.0, dataset={}) == 0  # 시계열 없음


def test_event_qty_krw_fixed_amount_unchanged():
    """KRW 종목 정액 사이징은 FX 유무와 무관하게 기존 그대로 (byte-identical 가드)."""
    s = _strat({"mode": "fixed_amount", "amount_krw": 500_000},
               {"kind": "list", "symbols": ["005930", "000660"]})
    assert event_buy_qty(s, cash=1_000_000, prev_close=100) == 5000
    assert event_buy_qty(s, cash=1_000_000, prev_close=100,
                         dataset={_FX: _fx_df(1370.0)}) == 5000


def test_event_qty_universe_currency_inference():
    """symbol 미지정(레거시 콜시그) 시 유니버스로 통화 판정 — 비-KRW 포함이면 환산 필요.

    fund-safety: KRW 가정으로 1,370배 과대 사이징하느니 보류(0)가 안전.
    explicit symbol 전달 시 그 종목 통화로 정확 판정.
    """
    fx = {_FX: _fx_df(1370.0)}
    all_usd = _strat({"mode": "fixed_amount", "amount_krw": 1_000_000},
                     {"kind": "list", "symbols": ["GOOG", "MSFT"]})
    assert event_buy_qty(all_usd, cash=50_000, prev_close=353.0, dataset=fx) == 2
    assert event_buy_qty(all_usd, cash=50_000, prev_close=353.0) == 0        # FX 없음 → 보류
    mixed = _strat({"mode": "fixed_amount", "amount_krw": 500_000},
                   {"kind": "list", "symbols": ["005930", "GOOG"]})
    assert event_buy_qty(mixed, cash=1_000_000, prev_close=100) == 0          # 모호 → 보류
    assert event_buy_qty(mixed, cash=1_000_000, prev_close=100,
                         symbol="005930") == 5000                             # 명시 KRW → 기존 그대로
    assert event_buy_qty(mixed, cash=50_000, prev_close=353.0,
                         symbol="GOOG", dataset=fx) == 1                      # 명시 USD → 환산(₩50만)


def test_event_qty_futures_fixed_amount_fx():
    """선물 정액: USD 선물(원유)은 환산 후 계약수, KRW 선물(코스피200)은 무환산."""
    fx = {_FX: _fx_df(1370.0)}
    oil = _strat({"mode": "fixed_amount", "amount_krw": 100_000_000},
                 {"kind": "single", "symbols": ["원유선물"]})
    # (1e8/1370) / (60×1,000×0.10) = 72,992.7 / 6,000 = 12계약
    assert event_buy_qty(oil, cash=1e9, prev_close=60.0, dataset=fx) == 12
    assert event_buy_qty(oil, cash=1e9, prev_close=60.0) == 0                 # FX 없음 → 보류
    k200 = _strat({"mode": "fixed_amount", "amount_krw": 100_000_000},
                  {"kind": "single", "symbols": ["코스피200선물"]})
    # 1e8 / (400×250,000×0.10) = 10계약 — KRW 선물은 FX 불요(기존 그대로)
    assert event_buy_qty(k200, cash=1e9, prev_close=400.0) == 10


# ── US-F1: 선물 %(futures_margin_pct) 사이징도 USD면 환산 (라이브 전용) ──────────
#
# 라이브 선물계좌 현금은 KRW인데(trader가 _currency_of=KRW로 보내 KRX 사이징 분기를
# 타고 futures_order_cash[KRW]를 넘김), USD 선물(CME)은 가격·승수가 USD다. fixed_amount
# 경로는 F-01에서 환산을 받았으나 % 경로(futures_margin_pct)는 누락 → KRW 예산을 USD
# 가격으로 나눠 ~1,370배 과대 계약. 백테스트 _budget은 단일통화(cash=종목통화) 가정이라
# 무관·무변경(test_futures_engine이 고정) — 이 수정은 event_buy_qty(라이브) 전용.

def test_event_qty_usd_futures_pct_converts():
    """USD 선물 % 사이징: KRW 선물계좌 현금을 FX로 USD 환산 후 계약수."""
    fx = {_FX: _fx_df(1370.0)}
    oil = _strat({"futures_margin_pct": 20.0},
                 {"kind": "single", "symbols": ["원유선물"]})
    # budget_krw = 1.37e9 × 20% = 2.74e8 → /1370 = 200,000 USD
    # denom = 60 × 1,000 × 0.10 = 6,000 → 200,000/6,000 = 33계약
    assert event_buy_qty(oil, cash=1.37e9, prev_close=60.0, dataset=fx) == 33
    # FX 미가용 → 0(보류). KRW 가정 무환산(2.74e8/6,000=45,666계약) 금지.
    assert event_buy_qty(oil, cash=1.37e9, prev_close=60.0) == 0
    assert event_buy_qty(oil, cash=1.37e9, prev_close=60.0, dataset={}) == 0


def test_event_qty_krw_futures_pct_unchanged():
    """KRW 선물(코스피200) % 사이징은 FX 유무와 무관하게 기존 그대로 (도메스틱 라이브 보존)."""
    k200 = _strat({"futures_margin_pct": 20.0},
                  {"kind": "single", "symbols": ["코스피200선물"]})
    # 1e9 × 20% = 2e8 / (400×250,000×0.10=1e7) = 20계약
    assert event_buy_qty(k200, cash=1e9, prev_close=400.0) == 20
    assert event_buy_qty(k200, cash=1e9, prev_close=400.0,
                         dataset={_FX: _fx_df(1370.0)}) == 20


def test_event_qty_mini_kospi200_uses_50k_multiplier():
    """미니 코스피200선물(승수 50,000 = 정규 1/5) % 사이징 — 같은 예산에 정규의 5배 계약수.

    승수가 instrument_spec에서 동적 전파됨을 고정(미니 자동매매 사이징 검증·T5). 같은 지수(prev_close)
    라도 계약당 증거금이 1/5라 같은 예산으로 5배 계약 = 동일 명목 노출. KRW 선물이라 FX 무관(정규와 동일)."""
    mini = _strat({"futures_margin_pct": 20.0},
                  {"kind": "single", "symbols": ["미니코스피200선물"]})
    # 1e9 × 20% = 2e8 / (400×50,000×0.10=2e6) = 100계약 (정규 20계약의 5배 — 승수 1/5)
    assert event_buy_qty(mini, cash=1e9, prev_close=400.0) == 100
    assert event_buy_qty(mini, cash=1e9, prev_close=400.0,
                         dataset={_FX: _fx_df(1370.0)}) == 100


# ── 백테스트 엔진 (run_unified 이벤트 경로 _budget) ────────────────────────────

def test_engine_usd_fixed_amount_sizing():
    """엔진 동일 결함 수정: GOOG 일일 ₩100만 시가매수 → 2주/일 (현행 결함은 2,832주)."""
    ds = {"GOOG": _bars(353.0, 350.0), _FX: _fx_df(1370.0)}
    res = run_strategy_ir(_fixed_amount_ir("GOOG", 1_000_000), ds)
    assert res["success"], res.get("error")
    assert len(res["trades"]) >= 1
    tr0 = res["trades"].iloc[0]
    assert float(tr0["진입가"]) == 353.0 and float(tr0["청산가"]) == 350.0
    qty = _entry_qty(res, per_share_loss=3.0)
    assert qty == 2, f"USD 정액 사이징 수량 {qty} ≠ 2 (₩1,000,000 / (353×1370))"


def test_engine_krw_fixed_amount_unchanged():
    """KRW 종목 정액 백테스트는 FX 시계열 없이도 기존 그대로 (byte-identical 가드)."""
    ds = {"005930": _bars(70000.0, 69000.0)}        # FX 프레임 없음 — 요구도 없어야 함
    res = run_strategy_ir(_fixed_amount_ir("005930", 1_000_000), ds)
    assert res["success"], res.get("error")
    qty = _entry_qty(res, per_share_loss=1000.0)
    assert qty == 14, f"KRW 정액 사이징 수량 {qty} ≠ 14 (₩1,000,000 // 70,000)"


def test_engine_usd_fixed_amount_missing_fx_fails():
    """USD 종목 + 정액인데 dataset에 원달러환율이 없으면 명시 에러(조용한 무환산 금지)."""
    ds = {"GOOG": _bars(353.0, 350.0)}
    res = run_strategy_ir(_fixed_amount_ir("GOOG", 1_000_000), ds)
    assert not res["success"]
    assert _FX in (res.get("error") or "")


def test_engine_fx_asof_uses_prev_bar():
    """엔진 환산은 의사결정 시점(체결 바 직전) 마지막 가용 종가 — 당일 종가 미참조(PIT).

    FX가 [1000, 2000, 2000]일 때 바1 시가 진입은 바0 환율(1000)을 써야 한다:
    qty = (1e6/1000)//100 = 10. 당일(바1) 환율을 쓰면 5가 되어 미래참조.
    """
    ds = {"GOOG": _bars(100.0, 99.0), _FX: _fx_df([1000.0, 2000.0, 2000.0])}
    res = run_strategy_ir(_fixed_amount_ir("GOOG", 1_000_000), ds)
    assert res["success"], res.get("error")
    qty = _entry_qty(res, per_share_loss=1.0)
    assert qty == 10, f"as-of(직전 바) 환율 미사용: qty={qty} (기대 10, 당일참조면 5)"


# ── needed_symbols — USD+정액이면 환산용 FX 시계열을 dataset 요구 집합에 포함 ──

def test_needed_symbols_includes_fx_for_usd_fixed_amount():
    def _ir(symbols, sizing):
        return StrategyIR.model_validate({
            "name": "t", "universe": {"kind": "list" if len(symbols) > 1 else "single",
                                       "symbols": symbols},
            "signal": _always_true().model_dump(),
            "position": {"direction": "long", "sizing": sizing,
                         "entry": {"mode": "on_signal"}, "exit": {}, "overlays": {}},
            "simulation": {},
        })
    fixed = {"mode": "fixed_amount", "amount_krw": 1_000_000}
    assert _FX in needed_symbols(_ir(["GOOG"], fixed))                       # USD 단일
    assert _FX in needed_symbols(_ir(["005930", "GOOG"], fixed))             # 혼합 리스트
    assert _FX not in needed_symbols(_ir(["005930"], fixed))                 # KRW — 불요
    assert _FX not in needed_symbols(
        _ir(["GOOG"], {"mode": "pct_cash", "amount_pct": 10}))               # USD 주식 %사이징 — 불요(라이브 USD현금)
    # US-F1: USD 선물 %사이징은 FX 필요(라이브 KRW 선물계좌현금→USD 환산)
    assert _FX in needed_symbols(_ir(["원유선물"], {"futures_margin_pct": 20.0}))
    assert _FX not in needed_symbols(
        _ir(["코스피200선물"], {"futures_margin_pct": 20.0}))                 # KRW 선물 — 불요


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))

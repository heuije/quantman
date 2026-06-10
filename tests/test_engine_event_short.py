"""이벤트(on_signal) 단일종목 숏 백테스트 — direction="short".

이벤트 경로(`run_unified`의 _run_on_signal)가 `direction="short"`를 무시(롱으로
계산)하던 갭의 회귀 테스트. 숏 = sell-to-open(open에 거래세)·buy-to-cover.
롱(sign=+1)은 byte-identical(골든 보존), 숏(sign=-1)이 신규.

    cd platform && pytest tests/test_engine_event_short.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.blocks import Node, const, data  # noqa: E402
from quant_core.ir_engine import (  # noqa: E402
    Entry, Exit, PositionSpec, SimSpec, StrategyIR, Universe, run_strategy_ir,
)


# ── 데이터 헬퍼 ───────────────────────────────────────────────────────────────

def _always_signal() -> Node:
    """매 바 참 — __SELF__.always(=1.0) > 0. 시장 데이터(always 컬럼) 참조."""
    return Node(op="compare", params={"op": ">"},
                inputs={"left": data("__SELF__.always"), "right": const(0.0)})


def _trending_data(sym: str = "AAA", n: int = 30) -> dict:
    """상승 추세 종가(100×1.01**i). Open=전일 종가(첫 바=종가)."""
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    close = 100.0 * (1.01 ** np.arange(n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    df = pd.DataFrame({
        "Open": open_, "High": np.maximum(open_, close),
        "Low": np.minimum(open_, close), "Close": close, "Volume": 1e6,
        "always": 1.0,
    }, index=idx)
    return {sym: df}


def _strategy(direction: str, hold_days: int, sym: str = "AAA",
              commission: float = 0.0, slippage: float = 0.0,
              sell_tax: float = 0.0, initial_capital: float = 1e7,
              fill: str = "next_open") -> StrategyIR:
    return StrategyIR(
        signal=_always_signal(),
        universe=Universe(kind="single", symbols=[sym]),
        position=PositionSpec(direction=direction,
                              entry=Entry(mode="on_signal"),
                              exit=Exit(hold_days=hold_days)),
        simulation=SimSpec(initial_capital=initial_capital, fill=fill,
                           slippage=slippage, commission=commission,
                           sell_tax=sell_tax),
    )


# ── 1. 숏 ≠ 롱 (상승 추세) ─────────────────────────────────────────────────────

def test_short_differs_from_long_trending():
    """상승 추세·always-true 신호·단일종목·on_signal·hold_days=5·비용0.
    롱 total_return > 0, 숏 total_return < 0, 둘이 같지 않다.
    (수정 전: direction 무시로 숏==롱 → RED.)"""
    data_ = _trending_data()
    long_res = run_strategy_ir(_strategy("long", hold_days=5), data_)
    short_res = run_strategy_ir(_strategy("short", hold_days=5), data_)
    assert long_res["success"], long_res.get("error")
    assert short_res["success"], short_res.get("error")
    lr = long_res["metrics"]["total_return"]
    sr = short_res["metrics"]["total_return"]
    assert lr > 0, f"상승추세 롱 수익률이 양(+)이 아님: {lr}"
    assert sr < 0, f"상승추세 숏 수익률이 음(-)이 아님 (숏 무시 의심): {sr}"
    assert abs(lr - sr) > 1e-6, f"숏==롱 (direction 무시): long={lr} short={sr}"


# ── 2. 가격 하락 시 숏 이익 (당일매매 hold_days=0) ──────────────────────────────

def test_short_profit_when_price_falls():
    """진입 시가=110·청산 종가=100, 비용0, hold_days=0(당일매매 숏).
    트레이드 수익률 ≈ +9.0909% ((110-100)/110*100), 보유일=0, 진입가≈110·청산가≈100."""
    idx = pd.date_range("2020-01-01", periods=3, freq="B")
    # next_open 진입: i바의 진입은 i바의 Open. hold_days=0 → 같은 바 종가 청산.
    # Open=110, Close=100 인 바에서 숏 진입(110)→커버(100) = +9.09% 수익.
    df = pd.DataFrame({
        "Open": [110.0, 110.0, 110.0],
        "High": [110.0, 110.0, 110.0],
        "Low": [100.0, 100.0, 100.0],
        "Close": [100.0, 100.0, 100.0],
        "Volume": 1e6, "always": 1.0,
    }, index=idx)
    res = run_strategy_ir(_strategy("short", hold_days=0), {"AAA": df})
    assert res["success"], res.get("error")
    trades = res["trades"]
    assert len(trades) > 0, "당일매매 숏이 거래를 한 건도 만들지 못함"
    t = trades.iloc[0]
    assert t["보유일"] == 0, f"보유일이 0이 아님: {t['보유일']}"
    assert abs(t["진입가"] - 110.0) < 1e-6, f"진입가가 시가(110) 아님: {t['진입가']}"
    assert abs(t["청산가"] - 100.0) < 1e-6, f"청산가가 종가(100) 아님: {t['청산가']}"
    expected = (110.0 - 100.0) / 110.0 * 100.0
    assert abs(t["수익률(%)"] - expected) < 1e-4, \
        f"숏 수익률이 {expected:.4f}%가 아님: {t['수익률(%)']}"


# ── 3. 진입 바 NAV 불변식 (sign 회계 검증) ─────────────────────────────────────

def test_short_nav_invariant_at_entry():
    """비용0·진입 바의 종가==진입가일 때, 그 바의 equity ≈ 초기자본(손익 0).
    숏의 NAV가 sign(-1)을 반영함을 증명 — cash += proceeds 한 뒤 -shares×price를 더해
    상쇄돼야 cash0 회복."""
    # fill=close → i바 종가에 진입. Close 일정(==진입가)이면 진입 바 손익 0.
    idx = pd.date_range("2020-01-01", periods=10, freq="B")
    df = pd.DataFrame({
        "Open": 100.0, "High": 100.0, "Low": 100.0, "Close": 100.0,
        "Volume": 1e6, "always": 1.0,
    }, index=idx)
    cap = 1e7
    res = run_strategy_ir(
        _strategy("short", hold_days=5, fill="close", initial_capital=cap),
        {"AAA": df})
    assert res["success"], res.get("error")
    eq = res["equity"]
    # 첫 진입 바(i=0, fill=close 즉시 진입). 종가==진입가라 손익 0 → equity≈cap.
    assert abs(eq.iloc[0] - cap) < cap * 1e-6, \
        f"진입 바 equity가 초기자본({cap})과 다름: {eq.iloc[0]} (NAV sign 누락 의심)"


# ── 4. 롱 경로 불변 (회귀 anchor) ──────────────────────────────────────────────

def test_long_unchanged_smoke():
    """동일 롱 전략(test 1)이 수정 전후 동일 — 손수 계산값과 대조.
    비용0·hold_days=5·상승추세. 골든 스위트가 byte-identity를 별도 보증."""
    data_ = _trending_data()
    res = run_strategy_ir(_strategy("long", hold_days=5), data_)
    assert res["success"], res.get("error")
    # 롱: 100→상승추세, hold_days=5 왕복 반복. 결정적 수치를 고정값에 묶는다.
    tr = res["metrics"]["total_return"]
    # 사전 기록값 — 숏 도입 전(direction 무시 시점)과 동일해야 byte-identity 증명.
    # RED 단계에서 측정한 롱 total_return = 28.207146799999975 (변하지 않아야 함).
    assert abs(tr - 28.207146799999975) < 1e-9, \
        f"롱 total_return이 변함(byte-identity 깨짐): {tr} (기대 28.207146799999975)"
    assert len(res["trades"]) == 5, f"롱 트레이드 수가 5가 아님: {len(res['trades'])}"


# ── 5. 선물 승수 적용 (숏) ─────────────────────────────────────────────────────

def test_short_futures_multiplier():
    """선물(코스피200선물, 승수 250,000) 숏 왕복이 P&L에 승수를 정확히 반영.

    같은 100→90 하락 경로를 주식·선물로 각각 숏. 선물 P&L = (진입−청산)×승수×계약수가
    되어야 하고(승수 250,000), 같은 점수 이동에 대해 주식 숏(승수 1)보다 규모가 훨씬 크다.
    is_futures("코스피200선물")==True 가 전제 — instrument_spec이 선물로 인식."""
    from quant_core.exec_defaults import instrument_spec, is_futures
    assert is_futures("코스피200선물"), "코스피200선물이 선물로 인식되지 않음"
    mult = instrument_spec("코스피200선물").multiplier
    assert mult == 250_000.0

    n = 8
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    close = np.linspace(100.0, 90.0, n)
    open_ = np.concatenate([[close[0]], close[:-1]])

    def _mk(sym):
        df = pd.DataFrame({
            "Open": open_, "High": np.maximum(open_, close),
            "Low": np.minimum(open_, close), "Close": close,
            "Volume": 1e6, "always": 1.0,
        }, index=idx)
        return {sym: df}

    cap = 1e8  # 선물 1계약(명목=100×250,000=2.5e7) 충분히 매도 가능.
    fut = run_strategy_ir(
        _strategy("short", hold_days=5, sym="코스피200선물", initial_capital=cap),
        _mk("코스피200선물"))
    stk = run_strategy_ir(
        _strategy("short", hold_days=5, sym="005930", initial_capital=cap),
        _mk("005930"))
    assert fut["success"], fut.get("error")
    assert stk["success"], stk.get("error")

    # 선물 트레이드 P&L = (진입가−청산가)×승수×계약수 와 정확히 일치(승수 반영 증명).
    ft = fut["trades"].iloc[0]
    fut_realized = fut["equity"].iloc[-1] - cap            # 비용0 → 실현 P&L = equity 변화
    contracts = round(fut_realized / ((ft["진입가"] - ft["청산가"]) * mult))
    assert contracts >= 1, f"선물 계약수 추정 실패: {contracts}"
    expected_fut_pnl = (ft["진입가"] - ft["청산가"]) * mult * contracts
    assert abs(fut_realized - expected_fut_pnl) < 1.0, \
        f"선물 숏 P&L이 (진입−청산)×승수×계약수와 불일치: {fut_realized} vs {expected_fut_pnl}"

    # 같은 점수 하락이지만 주식(승수 1)은 선물(승수 250,000)보다 규모가 훨씬 작다.
    fut_pnl = abs(fut_realized)
    stk_pnl = abs(stk["equity"].iloc[-1] - cap)
    assert fut_pnl > 0 and stk_pnl > 0
    # 선물 한 계약당 점수이동 손익이 승수배라, 같은 자본으로도 선물 규모가 더 크다.
    assert fut_pnl > stk_pnl, \
        f"선물 숏 손익이 승수를 반영하지 않음: fut={fut_pnl} stk={stk_pnl}"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

"""당일매매 — 시가 진입·종가 청산 (hold_days=0).

일봉 백테스트에서 *진입한 바의 종가*에 청산하는 당일 왕복(intraday O→C).
fill=next_open(시가 진입)과 결합하면 시가→종가가 된다. 분봉 불필요 —
일봉 바에 시가(Open)·종가(Close)가 모두 있으므로 데이터 추가 0.

    cd platform && pytest tests/test_intraday_open_close.py -v
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.blocks import Node, const, data  # noqa: E402
from quant_core.ir_engine import (  # noqa: E402
    Entry, Exit, PositionSpec, SimSpec, StrategyIR, Universe,
    explain_ir, run_strategy_ir, validate_strategy,
)


def _open_close_data(n: int = 30, open_px: float = 100.0, close_px: float = 110.0):
    """매일 시가 open_px·종가 close_px 고정 — O→C 왕복 수익이 결정적."""
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    df = pd.DataFrame({
        "Open": open_px, "High": max(open_px, close_px),
        "Low": min(open_px, close_px), "Close": close_px, "Volume": 1e6,
        "always": 1.0,   # 항상 참 신호용 컬럼
    }, index=idx)
    return {"AAA": df}


def _always_signal() -> Node:
    return Node(op="compare", params={"op": ">"},
                inputs={"left": data("__SELF__.always"), "right": const(0.0)})


def _day_trade_strategy(hold_days: int = 0) -> StrategyIR:
    return StrategyIR(
        signal=_always_signal(),
        universe=Universe(kind="single", symbols=["AAA"]),
        position=PositionSpec(entry=Entry(mode="on_signal"),
                              exit=Exit(hold_days=hold_days)),
        # 비용 0 — O→C 메커니즘만 검증(진입가=시가·청산가=종가 정확 대조).
        simulation=SimSpec(initial_capital=1e7, fill="next_open",
                           slippage=0.0, commission=0.0),
    )


def test_hold_days_zero_enters_open_exits_same_day_close():
    """hold_days=0 → 진입한 바의 종가에 청산(같은 날). 보유일=0, 진입일=청산일,
    진입가=시가(100)·청산가=종가(110)."""
    res = run_strategy_ir(_day_trade_strategy(), _open_close_data())
    assert res["success"], res.get("error")
    trades = res["trades"]
    assert len(trades) > 0, "당일매매가 거래를 한 건도 만들지 못함"
    for _, t in trades.iterrows():
        assert t["보유일"] == 0, f"보유일이 0이 아님: {t['보유일']}"
        assert t["진입일"] == t["청산일"], "진입일≠청산일 (같은 날 청산 아님)"
        assert abs(t["진입가"] - 100.0) < 1e-6, f"진입가가 시가(100) 아님: {t['진입가']}"
        assert abs(t["청산가"] - 110.0) < 1e-6, f"청산가가 종가(110) 아님: {t['청산가']}"


def test_validate_accepts_hold_days_zero():
    """검증기가 hold_days=0(당일 청산)을 거부하지 않는다 — M-exit 에러 없음."""
    errors = [i for i in validate_strategy(_day_trade_strategy(0)) if i.is_error]
    m_exit = [i.message for i in errors if i.rule == "M-exit"]
    assert not m_exit, f"hold_days=0이 거부됨: {m_exit}"


def test_validate_rejects_negative_hold_days():
    """음수 보유기간은 여전히 거부 — 0만 허용(의미: 당일 청산)."""
    errors = [i for i in validate_strategy(_day_trade_strategy(-1)) if i.is_error]
    assert any(i.rule == "M-exit" for i in errors), "음수 hold_days가 거부되지 않음"


def test_explain_describes_hold_days_zero_as_day_trade():
    """설명/내러티브가 hold_days=0을 '당일 종가 청산'으로 표현(0거래일 아님)."""
    import json
    blob = json.dumps(explain_ir(_day_trade_strategy(0)), ensure_ascii=False, default=str)
    assert "당일" in blob, "hold_days=0 설명에 '당일'이 없음"
    assert "0거래일" not in blob, "hold_days=0이 '0거래일'로 잘못 표기됨"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

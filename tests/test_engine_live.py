"""Stage 3 — IR 라이브 실행 결정 헬퍼(ir_engine.live) 단위 검증.

cycle_exit_reason(EOD 청산: 보유기간+매도조건)·event_buy_qty(이벤트 진입 사이징)가
백테스트 엔진과 같은 IR 스펙을 단일 포지션·종목에 일관 적용하는지 확인. 순수 함수라
브로커·네트워크 없이 검증 가능(머니패스 결정 로직의 안전망).

    cd platform && pytest tests/test_engine_live.py -v
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.ir_engine import StrategyIR  # noqa: E402
from quant_core.ir_engine.live import (  # noqa: E402
    cycle_exit_reason, event_buy_qty, intraday_exit_reason)

_DUMMY_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "right": {"op": "const", "params": {"value": 0}}},
}
# 매도조건: Close < ts_mean(Close, 3)
_SELL_COND = {
    "op": "compare", "params": {"op": "<"},
    "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "right": {"op": "ts_mean", "params": {"window": 3},
                         "inputs": {"signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}}},
}


def _df(closes):
    idx = pd.date_range("2026-05-01", periods=len(closes), freq="B")
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)


def _strat(*, exit_spec=None, sizing=None, universe=None):
    return StrategyIR.model_validate({
        "name": "t",
        "universe": universe or {"kind": "single", "symbols": ["005930"]},
        "signal": _DUMMY_SIGNAL,
        "position": {
            "direction": "long",
            "sizing": sizing or {"mode": "pct_cash", "amount_pct": 10},
            "entry": {"mode": "on_signal"},
            "exit": exit_spec or {}, "overlays": {},
        },
        "simulation": {},
    })


# ── cycle_exit_reason ──────────────────────────────────────────────────────────

def test_cycle_exit_hold_days():
    s = _strat(exit_spec={"hold_days": 5})
    assert cycle_exit_reason(s, held_days=5, dataset={}, symbol="005930") == "보유기간"
    assert cycle_exit_reason(s, held_days=4, dataset={}, symbol="005930") is None


def test_cycle_exit_condition_true():
    # 마지막 종가 10 < 3일평균(20,18,10)=16 → 매도조건
    ds = {"005930": _df([30, 28, 26, 24, 22, 20, 18, 10])}
    s = _strat(exit_spec={"condition": _SELL_COND})
    assert cycle_exit_reason(s, held_days=1, dataset=ds, symbol="005930") == "매도조건"


def test_cycle_exit_condition_false():
    # 마지막 종가 30 > 3일평균(10,12,30)=17.3 → 청산 없음
    ds = {"005930": _df([5, 6, 7, 8, 9, 10, 12, 30])}
    s = _strat(exit_spec={"condition": _SELL_COND})
    assert cycle_exit_reason(s, held_days=1, dataset=ds, symbol="005930") is None


def test_cycle_exit_none_when_no_rules():
    s = _strat(exit_spec={})
    assert cycle_exit_reason(s, held_days=999, dataset={}, symbol="005930") is None


def test_cycle_exit_hold_precedes_condition():
    """보유기간이 먼저 충족되면 매도조건 평가 전에 '보유기간' 반환."""
    ds = {"005930": _df([5, 6, 7, 8, 9, 10, 12, 30])}  # 조건 거짓
    s = _strat(exit_spec={"hold_days": 3, "condition": _SELL_COND})
    assert cycle_exit_reason(s, held_days=10, dataset=ds, symbol="005930") == "보유기간"


# ── cycle_exit_reason — 당일매매(hold_days=0, Stage B) ─────────────────────────

def test_cycle_exit_reason_day_trade():
    """당일매매(hold_days=0): 아침 사이클은 보유 유지, 종가 사이클에만 청산.

    - is_close=False·held=0 → None (아침 진입 직후 — 종가까지 보유, 같은 날 청산 금지)
    - is_close=True·held=0 → "당일청산" (종가 사이클 — 당일 청산)
    - is_close=False·held=1 → "보유기간" (종가 사이클 놓쳐 보유 넘어감 → 다음 아침 안전청산)
    - hold_days>=1은 is_close와 무관하게 종전 그대로(회귀 가드).
    """
    dt = _strat(exit_spec={"hold_days": 0})
    # 아침 진입 직후 — 종가까지 보유(청산 안 함)
    assert cycle_exit_reason(dt, held_days=0, dataset={}, symbol="005930",
                             is_close=False) is None
    # 종가 사이클 — 당일 청산
    assert cycle_exit_reason(dt, held_days=0, dataset={}, symbol="005930",
                             is_close=True) == "당일청산"
    # 종가 놓쳐 보유 넘어감 → 다음 아침 안전청산
    assert cycle_exit_reason(dt, held_days=1, dataset={}, symbol="005930",
                             is_close=False) == "보유기간"

    # hold_days>=1: 종가 사이클이라도 만기 전이면 건드리지 않음(비당일 보호)
    s3 = _strat(exit_spec={"hold_days": 3})
    assert cycle_exit_reason(s3, held_days=1, dataset={}, symbol="005930",
                             is_close=True) is None
    # 회귀: hold_days>=1 만기 도달은 종전 그대로 "보유기간"(is_close 기본 False)
    assert cycle_exit_reason(s3, held_days=3, dataset={}, symbol="005930",
                             is_close=False) == "보유기간"


# ── event_buy_qty ──────────────────────────────────────────────────────────────

def test_event_qty_pct_cash_list():
    s = _strat(sizing={"mode": "pct_cash", "amount_pct": 10},
               universe={"kind": "list", "symbols": ["005930", "000660"]})
    # 100만 × 10% = 10만 / 100 = 1000주
    assert event_buy_qty(s, cash=1_000_000, prev_close=100) == 1000


def test_event_qty_single_universe_full_invest():
    s = _strat(sizing={"mode": "pct_cash", "amount_pct": 10},
               universe={"kind": "single", "symbols": ["005930"]})
    # 단일 종목 → amount_pct 무시, 100% 전액 = 100만 / 100 = 10000주
    assert event_buy_qty(s, cash=1_000_000, prev_close=100) == 10000


def test_event_qty_fixed_amount():
    s = _strat(sizing={"mode": "fixed_amount", "amount_krw": 500_000},
               universe={"kind": "list", "symbols": ["005930", "000660"]})
    assert event_buy_qty(s, cash=1_000_000, prev_close=100) == 5000


def test_event_qty_max_position_cap():
    s = _strat(sizing={"mode": "pct_cash", "amount_pct": 50, "max_position_pct": 5},
               universe={"kind": "list", "symbols": ["005930", "000660"]})
    # 예산 50만→5000주, 그러나 max_position 5% = 5만/100 = 500주로 클램프
    assert event_buy_qty(s, cash=1_000_000, prev_close=100) == 500


def test_event_qty_zero_when_no_cash():
    s = _strat(universe={"kind": "list", "symbols": ["005930", "000660"]})
    assert event_buy_qty(s, cash=0, prev_close=100) == 0


# ── intraday_exit_reason (장중 가격청산 — 엔진 price_exit_reason 공유) ───────────

def test_intraday_stop_loss():
    s = _strat(exit_spec={"stop_loss": -5.0})
    assert intraday_exit_reason(s, cur_price=94, entry_price=100, peak_price=100) == "손절(intraday)"
    assert intraday_exit_reason(s, cur_price=96, entry_price=100, peak_price=100) is None


def test_intraday_take_profit():
    s = _strat(exit_spec={"take_profit": 10.0})
    assert intraday_exit_reason(s, cur_price=110, entry_price=100, peak_price=110) == "익절(intraday)"
    assert intraday_exit_reason(s, cur_price=109, entry_price=100, peak_price=109) is None


def test_intraday_trail_pct():
    # peak 120, 현재 107 ≤ 120×0.9=108 → 트레일링. 109 > 108 → 미발동.
    s = _strat(exit_spec={"trail_pct": 10.0})
    assert intraday_exit_reason(s, cur_price=107, entry_price=100, peak_price=120) == "트레일링스톱(intraday)"
    assert intraday_exit_reason(s, cur_price=109, entry_price=100, peak_price=120) is None


def test_intraday_none_when_no_rules_or_invalid_price():
    # 규칙 없음 → None
    assert intraday_exit_reason(_strat(exit_spec={}), cur_price=50, entry_price=100, peak_price=100) is None
    # 진입가/현재가 0 이하 → None (안전 — 평가 보류)
    s = _strat(exit_spec={"stop_loss": -5.0})
    assert intraday_exit_reason(s, cur_price=0, entry_price=100, peak_price=100) is None
    assert intraday_exit_reason(s, cur_price=50, entry_price=0, peak_price=0) is None

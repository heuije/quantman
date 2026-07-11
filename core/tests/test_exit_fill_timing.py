# -*- coding: utf-8 -*-
"""exit.fill(청산 체결 시점) — 설계 docs/REDESIGN/exit-fill-timing-redesign.md D1/D2/D6.

핵심 계약:
- None(기본) = legacy(진입 fill 공유) byte-identical — pin 테스트로 고정.
- "next_open" = 보유기간 청산을 익일 시가에, "close" = 해당 바 종가에 체결.
- score(리밸런싱) 경로는 1단계 미지원 — validator가 명시 거부(silent 무시 금지).

픽스처 트릭: Open=100대·Close=200대(서로소 값역) → 체결가의 값역만으로 시가/종가
체결 여부를 견고하게 판별(바 인덱스 가정에 안 기댐).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant_core.ir_engine import StrategyIR
from quant_core.ir_engine.run import run_strategy_ir
from quant_core.ir_engine.spec import validate_strategy

_N = 10
_IDX = pd.bdate_range("2026-01-05", periods=_N)
_OPENS = np.arange(100.0, 100.0 + _N)          # 100..109 → 시가 체결이면 <150
_CLOSES = np.arange(200.0, 200.0 + _N)         # 200..209 → 종가 체결이면 >150


def _ds():
    df = pd.DataFrame({"Open": _OPENS, "High": _CLOSES + 1, "Low": _OPENS - 1,
                       "Close": _CLOSES, "Volume": 1e6}, index=_IDX)
    return {"테스트종목": df}


def _ir(fill: str, exit_fill=None, hold_days=1):
    ex = {"hold_days": hold_days}
    if exit_fill is not None:
        ex["fill"] = exit_fill
    return {
        "name": "t", "engine": "ir", "query": "simulate",
        "universe": {"kind": "single", "symbols": ["테스트종목"]},
        # condition 신호(항상 참) → rule 경로. 상수식 금지(M-const)라 시장참조 비교 사용.
        "signal": {"op": "compare", "inputs": {
            "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
            "right": {"op": "const", "params": {"value": 0}}}, "params": {"op": ">"}},
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 100},
                     "entry": {"mode": "on_signal"},
                     "exit": ex, "overlays": {}},
        "simulation": {"initial_capital": 1_000_000.0, "fill": fill,
                       "commission": 0.0, "slippage": 0.0, "sell_tax": 0.0},
    }


def _first_trade(ir: dict):
    res = run_strategy_ir(StrategyIR.model_validate(ir), _ds())
    tr = res["trades"]
    assert tr is not None and len(tr) >= 1, f"거래 없음: {res.get('error')}"
    row = tr.iloc[0]
    return float(row["진입가"]), float(row["청산가"])


def _is_open(px):   # 값역 판별 — 시가 체결
    return px < 150


# ── pin: legacy(exit.fill 없음)는 현행 그대로 ──────────────────────────────────
def test_pin_legacy_close_entry_exits_at_close():
    e, x = _first_trade(_ir("close"))
    assert not _is_open(e) and not _is_open(x)       # 종가 진입·종가 청산(현행)


def test_pin_legacy_next_open_entry_exits_at_open():
    e, x = _first_trade(_ir("next_open"))
    assert _is_open(e) and _is_open(x)               # 시가 진입·시가 청산(현행)


# ── 신규 조합 ──────────────────────────────────────────────────────────────
def test_close_entry_next_open_exit():
    """종가 매수 → 익일 시가 매도 (#29 의도의 명시 표현 — 백테=라이브 정합)."""
    e, x = _first_trade(_ir("close", exit_fill="next_open"))
    assert not _is_open(e), "진입은 종가"
    assert _is_open(x), "청산은 익일 시가"


def test_next_open_entry_close_exit():
    """시가 매수 → N일 후 종가 매도 (신규 표현)."""
    e, x = _first_trade(_ir("next_open", exit_fill="close"))
    assert _is_open(e), "진입은 시가"
    assert not _is_open(x), "청산은 종가"


def test_close_entry_close_exit_explicit():
    """명시 close→close == legacy close와 동일 결과(오버라이드 일관성)."""
    e1, x1 = _first_trade(_ir("close"))
    e2, x2 = _first_trade(_ir("close", exit_fill="close"))
    assert (e1, x1) == (e2, x2)


# ── validator: score 경로 미지원 명시 거부 ─────────────────────────────────────
def test_score_strategy_rejects_exit_fill():
    ir = _ir("next_open", exit_fill="close")
    ir["signal"] = {"op": "data", "params": {"ref": "__SELF__.Close"}}   # score형
    s = StrategyIR.model_validate(ir)
    issues = validate_strategy(s)
    assert any(getattr(i, "rule", "") == "S-exit-fill" for i in issues), issues

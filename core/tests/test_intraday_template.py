# -*- coding: utf-8 -*-
"""자동매매 템플릿(limit_up_close_v1) — 매처(S-template)·스캔 파라미터·엔진 불간섭.

핵심 계약 (설계 intraday-template-redesign §2.1~2.2):
- template 태그는 엔진에 **불가시** — 태그 유/무 백테스트 결과 동일(byte-identical)을 잠근다.
- 매처는 화이트리스트 패턴만 통과: 정규형 급등 신호 · fill=close · hold 1+next_open ·
  롱 · on_signal · kind=all(+Market 필터만). 벗어나면 S-template 명시 거부.
- scan_params는 IR에서만 파라미터를 읽는다(임계·시장·상한 — 단일 출처).

픽스처 트릭(#358 계승): Open=300대·Close=100대(서로소 값역) → 체결가 값역만으로
종가 진입(100대)·익일 시가 청산(300대)을 견고 판별. day3에 +30% 급등 1회 발생.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant_core.ir_engine import StrategyIR
from quant_core.ir_engine.run import run_strategy_ir
from quant_core.ir_engine.spec import validate_strategy
from quant_core.ir_engine.templates import scan_params

_N = 8
_IDX = pd.bdate_range("2026-01-05", periods=_N)
# Close: 100 유지 → day3 130(+30%) → 이후 130 유지. Open은 300대(값역 분리).
_CLOSES = np.array([100.0, 100.0, 100.0, 130.0, 130.0, 130.0, 130.0, 130.0])
_OPENS = np.arange(300.0, 300.0 + _N)


def _ds():
    df = pd.DataFrame({"Open": _OPENS, "High": np.maximum(_OPENS, _CLOSES) + 1,
                       "Low": np.minimum(_OPENS, _CLOSES) - 1,
                       "Close": _CLOSES, "Volume": 1e6}, index=_IDX)
    df["pct_change_1d"] = df["Close"].pct_change() * 100
    return {"급등주": df}


def _tpl_ir(**over):
    ir = {
        "name": "상한가 마감형", "query": "simulate",
        "universe": {"kind": "all"},
        "signal": {"op": "compare", "params": {"op": ">="}, "inputs": {
            "left": {"op": "data", "params": {"ref": "__SELF__.pct_change_1d"}},
            "right": {"op": "const", "params": {"value": 29.5}}}},
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 100},
                     "entry": {"mode": "on_signal"},
                     "exit": {"hold_days": 1, "fill": "next_open"}},
        "simulation": {"initial_capital": 1_000_000.0, "fill": "close",
                       "commission": 0.0, "slippage": 0.0, "sell_tax": 0.0},
        "template": {"id": "limit_up_close_v1"},
    }
    ir.update(over)
    return ir


def _issues(ir: dict):
    return validate_strategy(StrategyIR.model_validate(ir))


def _s_template(issues):
    return [i for i in issues if getattr(i, "rule", "") == "S-template"]


# ── 매처: 정규형 통과 ─────────────────────────────────────────────────────────

def test_canonical_template_passes():
    assert _s_template(_issues(_tpl_ir())) == []


def test_market_screener_kosdaq_passes():
    ir = _tpl_ir(universe={"kind": "all", "screener": {"condition": {
        "op": "is_in", "params": {"values": ["코스닥"], "match": "contains"},
        "inputs": {"signal": {"op": "attribute", "params": {"attr": "Market"}}}}}})
    assert _s_template(_issues(ir)) == []


# ── 매처: 패턴 이탈 명시 거부 ─────────────────────────────────────────────────

@pytest.mark.parametrize("mutate, path_hint", [
    (lambda ir: ir["simulation"].update(fill="next_open"), "simulation.fill"),
    (lambda ir: ir["position"]["exit"].update(hold_days=0), "position.exit"),
    (lambda ir: ir["position"]["exit"].pop("fill"), "position.exit"),
    (lambda ir: ir["position"].update(direction="short"), "position.direction"),
    (lambda ir: ir["position"]["entry"].update(mode="scheduled"), "position.entry.mode"),
    (lambda ir: ir["position"]["sizing"].update(mode="equal_weight"), "position.sizing.mode"),
    (lambda ir: ir.update(universe={"kind": "single", "symbols": ["005930"]}), "universe"),
    (lambda ir: ir.update(query="describe"), "query"),
])
def test_pattern_deviation_rejected(mutate, path_hint):
    ir = _tpl_ir()
    mutate(ir)
    st = _s_template(_issues(ir))
    assert st, f"패턴 이탈({path_hint})인데 S-template 미발화"
    assert any(path_hint in (i.path or "") or path_hint.startswith("position.exit")
               for i in st)


def test_noncanonical_signal_rejected():
    ir = _tpl_ir(signal={"op": "compare", "params": {"op": ">="}, "inputs": {
        "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
        "right": {"op": "const", "params": {"value": 29.5}}}})
    assert _s_template(_issues(ir))


def test_threshold_out_of_range_rejected():
    ir = _tpl_ir()
    ir["signal"]["inputs"]["right"]["params"]["value"] = 35.0
    assert _s_template(_issues(ir))
    ir["signal"]["inputs"]["right"]["params"]["value"] = 5.0
    assert _s_template(_issues(ir))


def test_non_market_screener_rejected():
    ir = _tpl_ir(universe={"kind": "all", "screener": {"condition": {
        "op": "compare", "params": {"op": ">"}, "inputs": {
            "left": {"op": "data", "params": {"ref": "Close"}},
            "right": {"op": "const", "params": {"value": 1000}}}}}})
    assert _s_template(_issues(ir))


def test_us_market_screener_rejected():
    ir = _tpl_ir(universe={"kind": "all", "screener": {"condition": {
        "op": "is_in", "params": {"values": ["나스닥"], "match": "contains"},
        "inputs": {"signal": {"op": "attribute", "params": {"attr": "Market"}}}}}})
    assert _s_template(_issues(ir))


def test_unknown_template_id_rejected_by_schema():
    with pytest.raises(Exception):
        StrategyIR.model_validate(_tpl_ir(template={"id": "no_such_template"}))


def test_max_daily_entries_bounds():
    with pytest.raises(Exception):
        StrategyIR.model_validate(
            _tpl_ir(template={"id": "limit_up_close_v1", "max_daily_entries": 0}))
    with pytest.raises(Exception):
        StrategyIR.model_validate(
            _tpl_ir(template={"id": "limit_up_close_v1", "max_daily_entries": 6}))


# ── scan_params: IR 단일 출처 추출 ────────────────────────────────────────────

def test_scan_params_extraction():
    s = StrategyIR.model_validate(_tpl_ir())
    p = scan_params(s)
    assert p == {"threshold_pct": 29.5, "markets": ["KOSPI", "KOSDAQ"], "max_entries": 3}


def test_scan_params_market_and_entries():
    ir = _tpl_ir(template={"id": "limit_up_close_v1", "max_daily_entries": 5},
                 universe={"kind": "all", "screener": {"condition": {
                     "op": "is_in", "params": {"values": ["코스닥"], "match": "contains"},
                     "inputs": {"signal": {"op": "attribute",
                                           "params": {"attr": "Market"}}}}}})
    p = scan_params(StrategyIR.model_validate(ir))
    assert p == {"threshold_pct": 29.5, "markets": ["KOSDAQ"], "max_entries": 5}


# ── 엔진 불간섭: 태그 유/무 결과 동일 + 체결 의미(종가 매수→익일 시가 매도) ────

def test_engine_ignores_template_tag():
    ir_tagged = _tpl_ir()
    ir_plain = _tpl_ir()
    ir_plain.pop("template")
    r1 = run_strategy_ir(StrategyIR.model_validate(ir_tagged), _ds())
    r2 = run_strategy_ir(StrategyIR.model_validate(ir_plain), _ds())
    t1, t2 = r1["trades"], r2["trades"]
    assert t1 is not None and len(t1) >= 1, f"거래 없음: {r1.get('error')}"
    pd.testing.assert_frame_equal(t1, t2)
    pd.testing.assert_series_equal(r1["equity"], r2["equity"])


def test_backtest_semantics_close_entry_next_open_exit():
    res = run_strategy_ir(StrategyIR.model_validate(_tpl_ir()), _ds())
    tr = res["trades"]
    assert tr is not None and len(tr) == 1, f"급등 1회=거래 1건이어야: {res.get('error')}"
    row = tr.iloc[0]
    entry, exit_ = float(row["진입가"]), float(row["청산가"])
    assert entry < 200, f"진입은 당일 종가(100대)여야: {entry}"
    assert exit_ >= 300, f"청산은 익일 시가(300대)여야: {exit_}"

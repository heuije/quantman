# -*- coding: utf-8 -*-
"""선물 자본부족 명시 경고 — 초기자본이 1계약 증거금 미만이면 진입이 0건이 되는데, 엔진이
조용히 0거래로 보여 '왜 0인지' 오해를 부르던 것을 service 레이어에서 경고로 표면화한다.

엔진의 증거금 사이징(계약수=int(예산/(가격×승수×개시증거금률)))은 정상·라이브 패리티 —
이 경고는 자본 설정이 계약 명목 대비 과소하다는 안내일 뿐, 사이징 로직은 무수정."""
import numpy as np
import pandas as pd

from quant_core.ir_engine import strategy_from_spec


def _dataset():
    idx = pd.date_range("2020-01-02", periods=250, freq="B")
    m = np.where(np.arange(250) % 2 == 0, 0.015, -0.015)
    c = 300.0 * np.cumprod(1 + m)
    df = pd.DataFrame({"Open": c, "High": c * 1.001, "Low": c * 0.999,
                       "Close": c, "Volume": 1e6}, index=idx)
    df["pct_change_1d"] = df["Close"].pct_change()
    return {"코스피200선물": df}


def _ir(cap):
    return {"universe": {"kind": "single", "symbols": ["코스피200선물"]},
            "signal": {"op": "compare", "params": {"op": ">="}, "inputs": {
                "left": {"op": "data", "params": {"ref": "코스피200선물.pct_change_1d"}},
                "right": {"op": "const", "params": {"value": 0.001}}}},
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "on_signal"}, "exit": {"hold_days": 0}},
            "simulation": {"initial_capital": cap, "fill": "close",
                           "start": "2020-01-02", "end": "2020-12-31"}, "query": "simulate"}


def _warn_text(res):
    return " ".join(w.get("message", "") for w in (res.get("warnings") or []))


def test_insufficient_capital_for_futures_warns():
    # 1천만 × 선물예산 20% = 200만 < 1계약 증거금 750만(300×250,000×0.10) → 0거래 + 경고
    res = strategy_from_spec(_ir(10_000_000), _dataset())
    assert res["success"] is True
    assert (res["metrics"]["n_trades"] or 0) == 0
    txt = _warn_text(res)
    assert "코스피200선물" in txt and "증거금" in txt and "초기자본" in txt


def test_sufficient_capital_no_warning_and_trades():
    # 1억 × 20% = 2천만 → 2계약 충당 → 거래 발생 + 경고 없음
    res = strategy_from_spec(_ir(100_000_000), _dataset())
    assert res["success"] is True
    assert (res["metrics"]["n_trades"] or 0) > 0
    assert "증거금" not in _warn_text(res)

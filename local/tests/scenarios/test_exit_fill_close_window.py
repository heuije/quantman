# -*- coding: utf-8 -*-
"""exit.fill=close 라이브 창 라우팅 (재설계 D3) — 선택자·감시 정합.

계약: 보유기간 만기 청산이
  · exit.fill=close  → 종가창(_plan_exit_intents window="close") 소관·아침 창 제외·I5+ 감시 포함
  · legacy(미지정)   → 아침 창(window="open"·엔진 is_close 게이트) 소관·종가창 제외 (현행 그대로)
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd


def _def(exit_fill=None, hold_days=1):
    ex = {"hold_days": hold_days}
    if exit_fill is not None:
        ex["fill"] = exit_fill
    return {
        "name": "t", "engine": "ir", "query": "simulate",
        "universe": {"kind": "single", "symbols": ["코스피200선물"]},
        "signal": {"op": "compare", "inputs": {
            "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
            "right": {"op": "const", "params": {"value": 0}}}, "params": {"op": ">"}},
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 100},
                     "entry": {"mode": "on_signal"}, "exit": ex, "overlays": {}},
        "simulation": {"fill": "close"},
    }


def _seed(trader, broker):
    sym = "코스피200선물"
    broker._prices[sym] = 300.0
    idx = pd.date_range("2026-05-20", periods=8, freq="B")
    ds = {sym: pd.DataFrame({"Open": [300.0] * 8, "High": [301.0] * 8,
                             "Low": [299.0] * 8, "Close": [300.0] * 8}, index=idx)}
    snap = {"positions": [{"symbol": sym, "side": "long", "qty": 10}]}
    base = {"symbol": sym, "qty": 1, "side": "long", "entry_price": 300.0,
            "peak_price": 300.0, "entry_date": "2026-05-29"}   # kst_today(6/1) 기준 held=3 ≥ 1
    trader.ledger["c1"] = dict(base, strategy_name="종가청산", definition=_def("close"))
    trader.ledger["l1"] = dict(base, strategy_name="레거시", definition=_def(None))
    return ds, snap


def test_close_exit_belongs_to_close_window(isolated_trader):
    trader, broker = isolated_trader
    ds, snap = _seed(trader, broker)
    today = _dt.date(2026, 6, 1)

    morning, _, _ = trader._plan_exit_intents(
        "open", ds, today, "KRX", None, False, [])
    close_w, _, _ = trader._plan_exit_intents(
        "close", ds, today, "KRX", "futures", False, [])

    m_sids = {i.sid for i in morning}
    c_sids = {i.sid for i in close_w}
    assert "l1" in m_sids and "c1" not in m_sids   # 아침 = legacy만(현행)
    assert "c1" in c_sids and "l1" not in c_sids   # 종가창 = exit.fill=close만


def test_due_close_exit_counted_by_watchdog(isolated_trader):
    trader, broker = isolated_trader
    _seed(trader, broker)
    rows = trader.daytrade_unclosed("KRX")
    sids = {r["sid"] for r in rows}
    assert "c1" in sids        # I5+: 만기 도달 close-exit 잔존 = 오버나이트 노출 감시
    assert "l1" not in sids    # legacy hold>=1은 대상 아님(현행)

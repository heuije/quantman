"""R-2(2026-07-10 리뷰) — 국내 당일매매 진입 시간 가드.

낮에 앱을 시작하면 catch-up 사이클이 hold_days==0(개장 진입 설계) 전략을 임의
시각(07-06 12:33 실측)에 신규 진입시켜 백테스트(시가 진입→종가 청산)와 어긋난
반쪽 노출이 됐다. 가드: KRW 당일매매(fill!=close)는 09:30 이후 신규 진입 skip
(skip_late_daytrade decision) — 재시도 사다리(최종 ~09:16)는 포용.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from localapp import trader as tr


def _fut_ds(symbol, px):
    idx = pd.date_range("2026-05-01", periods=5, freq="B")
    return {symbol: pd.DataFrame(
        {"Open": [px] * 5, "High": [px] * 5, "Low": [px] * 5, "Close": [px] * 5}, index=idx)}


def _daytrade_def(symbol, hold_days=0):
    return {
        "name": "당일 선물", "engine": "ir",
        "universe": {"kind": "single", "symbols": [symbol]},
        "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
        "position": {"direction": "long_short",
                     "sizing": {"mode": "pct_cash", "amount_pct": 100,
                                "futures_margin_pct": 100},
                     "entry": {"mode": "on_signal", "threshold": 0.0},
                     "exit": {"hold_days": hold_days}, "overlays": {}},
        "execution": {"use_limit": False},
        "simulation": {},
    }


def _kst(h, m):
    return datetime(2026, 7, 6, h, m, tzinfo=ZoneInfo("Asia/Seoul"))


def test_daytrade_entry_blocked_midday(isolated_trader, monkeypatch):
    """12:33(실측 사건 시각) → 발주 0 + skip_late_daytrade decision."""
    trader, broker = isolated_trader
    monkeypatch.setattr(tr, "kst_now", lambda: _kst(12, 33))
    sym = "코스피200선물"
    broker._prices[sym] = 300.0
    broker._balance["futures_order_cash_kr"] = 50_000_000
    n_before = len(broker.submitted)
    dec: list = []
    ok = trader._try_buy_one_symbol(
        "s1", "s1", "당일", _daytrade_def(sym), sym, _fut_ds(sym, 300.0),
        10_000_000.0, dec, cand_direction="short")
    assert not ok
    assert len(broker.submitted) == n_before                      # 발주 0
    assert any(d.get("action") == "skip_late_daytrade" for d in dec), dec


def test_daytrade_entry_allowed_morning_window(isolated_trader, monkeypatch):
    """08:56(재시도 사다리 내) → 정상 발주."""
    trader, broker = isolated_trader
    monkeypatch.setattr(tr, "kst_now", lambda: _kst(8, 56))
    sym = "코스피200선물"
    broker._prices[sym] = 300.0
    broker._balance["futures_order_cash_kr"] = 50_000_000
    n_before = len(broker.submitted)
    dec: list = []
    ok = trader._try_buy_one_symbol(
        "s2", "s2", "당일", _daytrade_def(sym), sym, _fut_ds(sym, 300.0),
        10_000_000.0, dec, cand_direction="short")
    assert ok, dec
    assert len(broker.submitted) == n_before + 1


def test_overnight_entry_unaffected_midday(isolated_trader, monkeypatch):
    """hold_days=1(오버나이트)은 낮 catch-up 진입 종전대로 허용."""
    trader, broker = isolated_trader
    monkeypatch.setattr(tr, "kst_now", lambda: _kst(12, 33))
    sym = "코스피200선물"
    broker._prices[sym] = 300.0
    broker._balance["futures_order_cash_kr"] = 50_000_000
    n_before = len(broker.submitted)
    dec: list = []
    ok = trader._try_buy_one_symbol(
        "s3", "s3", "오버나이트", _daytrade_def(sym, hold_days=1), sym,
        _fut_ds(sym, 300.0), 10_000_000.0, dec, cand_direction="long")
    assert ok, dec
    assert len(broker.submitted) == n_before + 1

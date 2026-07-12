# -*- coding: utf-8 -*-
"""EntryTriggerManager(워치리스트 장중 돌파 진입) — 발화·멱등·상한·게이트 계약.

계약(장중 템플릿 설계 §4): 판정 = 현재가/전일종가−1 ≥ 임계(IR 정규형과 같은 숫자·
watchlist_params 단일 출처) · 발화 = 전략×종목×일 1회(디스크 기록이 발주보다 먼저 —
저장 실패 시 발주 금지) · 일일 상한(max_daily_entries) · 진입 전 킬스위치/손실한도 게이트
(_close_entry_blocked 재사용) · 진입은 _enter_from_preview(entry_window="intraday") 재사용.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import localapp.entry_trigger as et
from localapp.entry_trigger import EntryTriggerManager

_IDX = pd.bdate_range("2026-01-05", periods=5)


def _df(close: float) -> pd.DataFrame:
    return pd.DataFrame({"Open": np.full(5, close), "High": np.full(5, close),
                         "Low": np.full(5, close), "Close": np.full(5, close),
                         "Volume": 1e6}, index=_IDX)


def _wl_def(thr=10.0, symbols=("123450", "005930"), max_entries=2):
    return {
        "name": "돌파", "query": "simulate",
        "universe": {"kind": "list", "symbols": list(symbols)},
        "signal": {"op": "compare", "params": {"op": ">="}, "inputs": {
            "left": {"op": "data", "params": {"ref": "__SELF__.high_change_1d"}},
            "right": {"op": "const", "params": {"value": thr}}}},
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 100},
                     "entry": {"mode": "on_signal"},
                     "exit": {"hold_days": 3}},
        "simulation": {"fill": "trigger"},
        "template": {"id": "watchlist_trigger_v1", "max_daily_entries": max_entries},
    }


class _FakeTrader:
    def __init__(self):
        self.enter_calls: list[tuple] = []
        self.blocked = False

    def reload_state(self):
        pass

    def _close_entry_blocked(self, equity, snap, rl, decisions):
        if self.blocked:
            decisions.append({"reason": "skip_killswitch"})
            return True
        return False

    def _enter_from_preview(self, by_strategy, strategies, dataset, equity,
                            decisions, sold, market="KRX", entry_window="open", **kw):
        self.enter_calls.append((by_strategy, entry_window))
        decisions.append({"reason": "buy"})


class _FakeBroker:
    def account_snapshot(self, overseas=True):
        return {"balance": {}, "positions": []}


@pytest.fixture()
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(et, "FIRED_PATH", tmp_path / "trigger_fired.json")
    monkeypatch.setattr(et, "_unified_equity_krw", lambda b: 1_000_000.0)
    pushes: list[dict] = []
    monkeypatch.setattr(et, "push_snapshot", lambda p: pushes.append(p))
    trader = _FakeTrader()
    ds = {"123450": _df(100.0), "005930": _df(70_000.0)}
    strategies = [{"id": 9, "name": "돌파", "definition": _wl_def()}]
    mgr = EntryTriggerManager(trader, _FakeBroker(), strategies, ds, {})
    return mgr, trader, pushes


def test_build_watch_and_prev_close(env):
    mgr, _, _ = env
    assert mgr.active and mgr.watch_symbols() == {"123450", "005930"}


def test_symbol_without_close_excluded(monkeypatch, tmp_path):
    monkeypatch.setattr(et, "FIRED_PATH", tmp_path / "f.json")
    strategies = [{"id": 9, "definition": _wl_def(symbols=("123450", "999999"))}]
    mgr = EntryTriggerManager(_FakeTrader(), _FakeBroker(), strategies,
                              {"123450": _df(100.0)}, {})
    assert mgr.watch_symbols() == {"123450"}


def test_below_threshold_no_fire(env):
    mgr, trader, _ = env
    mgr.on_tick("123450", 109.0)                      # +9% < 10%
    assert trader.enter_calls == []


def test_fire_once_idempotent(env):
    mgr, trader, pushes = env
    mgr.on_tick("123450", 110.0)                      # +10% ≥ 10% → 발화
    mgr.on_tick("123450", 115.0)                      # 재틱 — 멱등(무시)
    assert len(trader.enter_calls) == 1
    by_strategy, window = trader.enter_calls[0]
    assert window == "intraday"
    assert by_strategy == [{"strategy_id": "9",
                            "candidates": [{"symbol": "123450", "direction": "long"}]}]
    assert len(pushes) == 1 and pushes[0]["cycle_summary"]["kind"] == "intraday_trigger"


def test_daily_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(et, "FIRED_PATH", tmp_path / "f.json")
    monkeypatch.setattr(et, "_unified_equity_krw", lambda b: 1.0)
    monkeypatch.setattr(et, "push_snapshot", lambda p: None)
    trader = _FakeTrader()
    strategies = [{"id": 9, "definition": _wl_def(max_entries=1)}]
    ds = {"123450": _df(100.0), "005930": _df(70_000.0)}
    mgr = EntryTriggerManager(trader, _FakeBroker(), strategies, ds, {})
    mgr.on_tick("123450", 110.0)                      # 1건 진입(상한 도달)
    mgr.on_tick("005930", 77_000.0)                   # +10%지만 상한 — 무발주
    assert len(trader.enter_calls) == 1
    mgr.on_tick("005930", 78_000.0)                   # capped 기록 후 재틱 — 여전히 1건
    assert len(trader.enter_calls) == 1


def test_killswitch_blocks_entry(env):
    mgr, trader, pushes = env
    trader.blocked = True
    mgr.on_tick("123450", 110.0)
    assert trader.enter_calls == []                   # 게이트가 진입 차단
    assert len(pushes) == 1                           # 차단 사유는 push로 표면화
    # 같은 키 재발화도 없음(blocked 상태 기록)
    mgr.on_tick("123450", 111.0)
    assert trader.enter_calls == [] and len(pushes) == 1


def test_fired_record_survives_manager_rebuild(env, monkeypatch, tmp_path):
    """디스크 SSOT — 루프 재시작(새 매니저)에도 같은 날 재발화 없음(M9 규약)."""
    mgr, trader, _ = env
    mgr.on_tick("123450", 110.0)
    assert len(trader.enter_calls) == 1
    trader2 = _FakeTrader()
    strategies = [{"id": 9, "name": "돌파", "definition": _wl_def()}]
    ds = {"123450": _df(100.0), "005930": _df(70_000.0)}
    mgr2 = EntryTriggerManager(trader2, _FakeBroker(), strategies, ds, {})
    mgr2.on_tick("123450", 112.0)
    assert trader2.enter_calls == []

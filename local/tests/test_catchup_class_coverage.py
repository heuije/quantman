"""catchup KRX 아침 사이클 판정 — 자산군 분리(선물 08:35/주식 08:55) 커버리지 회귀.

문제 10 수정으로 아침이 두 사이클로 나뉘며 "오늘 KRX cycle 있음"만으론 판정 부족:
선물 사이클만 돌고 PC가 꺼진 날 주식 catchup이 억제된다. 완료 판정 = full-scope
(instrument_class 없음 — catchup/구버전) 1회 또는 {stock, futures} 각 1회.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

_LOCAL_DIR = Path(__file__).resolve().parent.parent
_CORE_DIR = _LOCAL_DIR.parent / "core"
for _p in (str(_LOCAL_DIR), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from localapp import catchup

_KST = ZoneInfo("Asia/Seoul")
# 2026-06-01(월) 장중 10:00 — KRX intraday.
_NOW = datetime.datetime(2026, 6, 1, 10, 0, tzinfo=_KST)


def _entry(hh: int, mm: int, instrument_class: str | None) -> dict:
    ts = datetime.datetime(2026, 6, 1, hh, mm, tzinfo=_KST)
    s = {"market": "KRX", "kind": "cycle"}
    if instrument_class is not None:
        s["instrument_class"] = instrument_class
    return {"ts": ts.isoformat(), "summary": s}


def _plan(entries, monkeypatch):
    monkeypatch.setattr(catchup, "_read_recent_cycles", lambda: entries)
    return catchup._decide_catchup_plan(now=_NOW)


def test_futures_only_cycle_does_not_suppress_stock_catchup(monkeypatch):
    """선물 사이클만 돈 날(08:35 후 PC off) → 주식 몫 catchup 필요."""
    plan = _plan([_entry(8, 35, "futures")], monkeypatch)
    assert plan.krx_cycle_needed is True


def test_stock_only_cycle_does_not_suppress_futures_catchup(monkeypatch):
    """주식 사이클만 돈 날(08:35 미발동·08:55만) → 선물 몫 catchup 필요."""
    plan = _plan([_entry(8, 55, "stock")], monkeypatch)
    assert plan.krx_cycle_needed is True


def test_both_classes_done_no_catchup(monkeypatch):
    plan = _plan([_entry(8, 35, "futures"), _entry(8, 55, "stock")], monkeypatch)
    assert plan.krx_cycle_needed is False


def test_full_scope_cycle_covers_both(monkeypatch):
    """full-scope(instrument_class 없음 — catchup/구버전 사이클)면 단독으로 충족."""
    plan = _plan([_entry(9, 10, None)], monkeypatch)
    assert plan.krx_cycle_needed is False


def test_no_cycle_today_needs_catchup(monkeypatch):
    plan = _plan([], monkeypatch)
    assert plan.krx_cycle_needed is True

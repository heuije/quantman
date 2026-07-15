"""무인 자동 업데이트 안전창 판정 — updater.is_safe_update_window.

자동 업데이트는 앱을 재시작하므로 주문·데이터·장중 활동과 겹치면 안 된다. 실제 상태
(장중 세션·미체결)와 보수적 시각창(16~21시 KST) 이중 게이트를 검증한다.
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))

from localapp import updater

_KST = ZoneInfo("Asia/Seoul")


def _at(hh, mm=0):
    return datetime.datetime(2026, 6, 1, hh, mm, tzinfo=_KST)


def test_safe_when_idle_evening():
    ok, _ = updater.is_safe_update_window(_at(17), intraday_running=False, pending_count=0)
    assert ok is True


def test_unsafe_during_intraday_session():
    ok, reason = updater.is_safe_update_window(
        _at(17), intraday_running=True, pending_count=0)
    assert ok is False and "장중" in reason


def test_unsafe_with_pending_orders():
    ok, reason = updater.is_safe_update_window(
        _at(17), intraday_running=False, pending_count=2)
    assert ok is False and "미체결" in reason


def test_unsafe_morning_prewarm_window():
    """08:10 — 아침 pre-warm/선물사이클 구간(장중 플래그 전) → 시각 게이트가 차단."""
    ok, reason = updater.is_safe_update_window(
        _at(8, 10), intraday_running=False, pending_count=0)
    assert ok is False and "16~21" in reason


def test_unsafe_close_settlement_window():
    """15:45 — 종가 사이클·정산 구간 → 시각 게이트 차단(16시 전)."""
    ok, _ = updater.is_safe_update_window(
        _at(15, 45), intraday_running=False, pending_count=0)
    assert ok is False


def test_unsafe_us_prewarm_evening():
    """21:30 — US pre-warm(개장−40분·DST상 이르면 ~21:50) 임박 → 21시 이후 차단."""
    ok, _ = updater.is_safe_update_window(
        _at(21, 30), intraday_running=False, pending_count=0)
    assert ok is False


def test_boundary_16_safe_21_unsafe():
    assert updater.is_safe_update_window(
        _at(16, 0), intraday_running=False, pending_count=0)[0] is True
    assert updater.is_safe_update_window(
        _at(20, 59), intraday_running=False, pending_count=0)[0] is True
    assert updater.is_safe_update_window(
        _at(21, 0), intraday_running=False, pending_count=0)[0] is False

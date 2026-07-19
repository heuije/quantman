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


# ── 로드맵 F — 새벽 안전창 06:10~07:00 ──────────────────────────────────────


def test_safe_dawn_window():
    """06:10~07:00 — 미국 정산(겨울 06:05) 후·아침 pre-warm(08:05) 전 새벽창."""
    ok, _ = updater.is_safe_update_window(
        _at(6, 10), intraday_running=False, pending_count=0)
    assert ok is True
    ok2, _ = updater.is_safe_update_window(
        _at(6, 45), intraday_running=False, pending_count=0)
    assert ok2 is True


def test_unsafe_before_dawn_window():
    """06:05 — 겨울 미국 정산 진행 시각 → 차단."""
    ok, reason = updater.is_safe_update_window(
        _at(6, 5), intraday_running=False, pending_count=0)
    assert ok is False and "06:10" in reason


def test_unsafe_after_dawn_window():
    ok, _ = updater.is_safe_update_window(
        _at(7, 5), intraday_running=False, pending_count=0)
    assert ok is False


# ── #9(2026-07-19) — KR 비거래일(주말·휴장)은 낮 구간도 안전(06:10~21:00) ────


def test_kr_off_day_daytime_safe(monkeypatch):
    from quant_core import market_calendar as mc
    monkeypatch.setattr(mc, "is_session_day", lambda m, d: False)
    ok, _ = updater.is_safe_update_window(
        _at(10, 30), intraday_running=False, pending_count=0)
    assert ok is True


def test_kr_trading_day_daytime_blocked(monkeypatch):
    from quant_core import market_calendar as mc
    monkeypatch.setattr(mc, "is_session_day", lambda m, d: True)
    ok, reason = updater.is_safe_update_window(
        _at(10, 30), intraday_running=False, pending_count=0)
    assert ok is False and "비거래일" in reason


def test_kr_off_day_us_night_still_blocked(monkeypatch):
    """US 야간 구간(21:00~06:10)은 KR 휴장과 무관하게 유지 — US는 따로 개장 가능."""
    from quant_core import market_calendar as mc
    monkeypatch.setattr(mc, "is_session_day", lambda m, d: False)
    assert updater.is_safe_update_window(
        _at(22, 0), intraday_running=False, pending_count=0)[0] is False
    assert updater.is_safe_update_window(
        _at(5, 0), intraday_running=False, pending_count=0)[0] is False


def test_calendar_error_falls_back_to_fixed_windows(monkeypatch):
    """캘린더 조회 불가(범위 밖·미배포) — 기존 고정창만(보수 폴백)."""
    from quant_core import market_calendar as mc

    def _boom(m, d):
        raise RuntimeError("캘린더 stale")

    monkeypatch.setattr(mc, "is_session_day", _boom)
    assert updater.is_safe_update_window(
        _at(10, 30), intraday_running=False, pending_count=0)[0] is False


# ── 버전 파일명 마커(2026-07-19) — 제자리 업데이트의 폴더명 낡음 보완 ─────────


def test_version_marker_written_and_old_removed(monkeypatch, tmp_path):
    """frozen 기동 시 VERSION-<버전>.txt 기록 + 구버전 마커 제거."""
    exe = tmp_path / "MyStock.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(updater.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updater.sys, "executable", str(exe))
    (tmp_path / "VERSION-0.0.1.txt").write_text("0.0.1", encoding="utf-8")
    updater.write_version_marker()
    import localapp
    cur = tmp_path / f"VERSION-{localapp.__version__}.txt"
    assert cur.exists()
    assert cur.read_text(encoding="utf-8").strip() == localapp.__version__
    assert not (tmp_path / "VERSION-0.0.1.txt").exists()


def test_version_marker_noop_in_dev(monkeypatch, tmp_path):
    """개발 환경(비-frozen)은 무동작 — 리포 폴더에 마커를 만들지 않는다."""
    monkeypatch.setattr(updater.sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.delattr(updater.sys, "frozen", raising=False)
    updater.write_version_marker()
    assert list(tmp_path.glob("VERSION-*.txt")) == []

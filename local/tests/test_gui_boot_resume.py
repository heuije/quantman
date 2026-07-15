"""부팅 자동재개 배선 — SettingsApp._resume_autotrade_if_running.

Tkinter 인스턴스 없이 fake self로 배선 로직만 검증: frozen 게이트·resume_plan 분기
(running/paused/stopped)·중복 기동 가드·paused 복원. 결정 자체는 test_auto_state_resume.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import auto_state, gui


class _FakeSched:
    def __init__(self):
        self.running = True
        self.paused = False

    def pause(self):
        self.paused = True


def _fake_self():
    calls = {"start": 0}
    fs = types.SimpleNamespace(
        scheduler=None, auto_paused=None,
        _push_state_async=lambda: None, refresh_status=lambda: None, _calls=calls)

    def _start():
        calls["start"] += 1
        fs.scheduler = _FakeSched()

    fs._start_scheduler = _start
    return fs


def _run(monkeypatch, status, frozen=True):
    # load만 패치 → 실제 resume_plan 로직을 타게 한다(resume_plan 패치는 같은 모듈
    # 재귀라 불가). frozen 게이트는 updater.is_frozen로.
    monkeypatch.setattr(gui.updater, "is_frozen", lambda: frozen)
    monkeypatch.setattr(gui.auto_state, "load", lambda: status)
    fs = _fake_self()
    gui.SettingsApp._resume_autotrade_if_running(fs)
    return fs


def test_boot_resume_running(monkeypatch):
    """running → 스케줄러 기동·paused 아님(유저 클릭 없이 자동매매 이어감)."""
    fs = _run(monkeypatch, "running")
    assert fs._calls["start"] == 1
    assert fs.auto_paused is False and fs.scheduler.paused is False


def test_boot_resume_paused(monkeypatch):
    """paused → 기동 후 pause로 직전 일시정지 보존."""
    fs = _run(monkeypatch, "paused")
    assert fs._calls["start"] == 1
    assert fs.auto_paused is True and fs.scheduler.paused is True


def test_boot_stopped_no_start(monkeypatch):
    """stopped(명시적 중지) → 자동 기동 안 함."""
    fs = _run(monkeypatch, "stopped")
    assert fs._calls["start"] == 0


def test_boot_dev_env_no_start(monkeypatch):
    """개발 환경(미frozen) → stale 상태로 자동 기동 안 함."""
    fs = _run(monkeypatch, "running", frozen=False)
    assert fs._calls["start"] == 0


def test_boot_already_running_guard(monkeypatch):
    """이미 스케줄러 가동 중이면 중복 기동 안 함."""
    monkeypatch.setattr(gui.updater, "is_frozen", lambda: True)
    monkeypatch.setattr(gui.auto_state, "load", lambda: "running")
    fs = _fake_self()
    fs.scheduler = _FakeSched()          # 이미 기동됨
    gui.SettingsApp._resume_autotrade_if_running(fs)
    assert fs._calls["start"] == 0

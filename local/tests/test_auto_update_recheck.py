"""무인 자동 업데이트의 '감지' 무인화 — gui._auto_update_tick 재체크.

감지가 앱 시작 1회+창 포커스에만 의존하면 방치된 무인 운용 PC에선 릴리스가
나와도 감지 자체가 안 돼, 안전창(16~21시 KST) 자동 설치가 영영 안 걸린다
(2026-07-16 실측: 아침 기동 뒤 발행된 v0.9.76을 저녁 내내 미감지). 자동설치
틱(10분)이 1시간 throttle로 감지를 겸하는지 잠근다. 설치 게이트 자체는
test_auto_update_window.py가 담당.
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))

from localapp import gui


class _FakeRoot:
    def __init__(self):
        self.scheduled = []

    def after(self, ms, cb=None, *args):
        self.scheduled.append(ms)


def _make_app(update_info, last_check):
    app = object.__new__(gui.SettingsApp)
    app.root = _FakeRoot()
    app._update_info = update_info
    app._last_update_check = last_check
    app._rechecks = []
    app._check_updates_async = lambda: app._rechecks.append(True)
    return app


def _run_threads_inline(monkeypatch):
    """gui 모듈 안의 threading.Thread만 동기 실행으로 치환(전역 threading 무변경)."""
    started = []

    class _InlineThread:
        def __init__(self, target=None, daemon=None, name=None):
            self._target = target
            self.name = name

        def start(self):
            started.append(self.name)
            if self._target:
                self._target()

    monkeypatch.setattr(gui, "threading",
                        types.SimpleNamespace(Thread=_InlineThread))
    return started


def test_tick_rechecks_when_stale_and_undetected(monkeypatch):
    """미감지 + 마지막 체크 1시간 경과 → 틱이 스스로 재체크(포커스 불필요)."""
    started = _run_threads_inline(monkeypatch)
    app = _make_app(update_info=None, last_check=time.time() - 3700)
    app._auto_update_tick()
    assert app._rechecks == [True]
    assert started == ["update-recheck"]
    assert app.root.scheduled == [600_000]


def test_tick_respects_hourly_throttle(monkeypatch):
    """미감지지만 최근(1시간 이내) 체크했으면 재체크 안 함 — GitHub 호출 낭비 방지."""
    _run_threads_inline(monkeypatch)
    app = _make_app(update_info=None, last_check=time.time() - 3000)
    app._auto_update_tick()
    assert app._rechecks == []
    assert app.root.scheduled == [600_000]


def test_tick_skips_recheck_once_detected(monkeypatch):
    """이미 새 버전을 감지했으면 재체크 없음 — 설치 판정 경로만 탄다.

    (테스트 환경은 non-frozen이라 설치는 skip되고 다음 틱만 예약된다.)"""
    _run_threads_inline(monkeypatch)
    app = _make_app(update_info={"tag": "v99.0.0", "url": "https://x/z.zip"},
                    last_check=0.0)
    app._auto_update_tick()
    assert app._rechecks == []
    assert app.root.scheduled == [600_000]

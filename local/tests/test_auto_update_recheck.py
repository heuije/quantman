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


def test_tick_rechecks_even_after_detection(monkeypatch):
    """N2 — 감지 후에도 throttle이 지나면 재체크한다.

    ⚠ 이 테스트는 종전 `test_tick_skips_recheck_once_detected`를 **대체**한다.
    그 테스트는 "이미 감지했으면 재체크 안 함"을 잠갔는데, 그게 곧 N2 결함이었다:
    `_update_info`를 비우는 경로가 코드베이스에 없어 한 번 채워지면 프로세스 수명
    내내 고정되고, 그 사이 더 새 릴리스가 나와도 영영 못 본다. 실측 2026-07-20 —
    mwmw가 4일 묵은 v0.9.77을 설치하고 **재시작한 뒤에야** v0.9.82를 감지했다
    (클릭 2회). 낭비 방지는 1시간 throttle이 이미 담당하므로 감지 상태를 게이트로
    쓸 이유가 없다.
    """
    _run_threads_inline(monkeypatch)
    app = _make_app(update_info={"tag": "v0.9.77", "url": "https://x/z.zip"},
                    last_check=time.time() - 3700)
    app._auto_update_tick()
    assert app._rechecks == [True], "감지 상태가 재체크를 막으면 한 홉씩만 전진한다"
    assert app.root.scheduled == [600_000]


def test_tick_throttle_applies_after_detection_too(monkeypatch):
    """단, throttle은 그대로 — 감지 여부와 무관하게 1시간 간격."""
    _run_threads_inline(monkeypatch)
    app = _make_app(update_info={"tag": "v0.9.77", "url": "https://x/z.zip"},
                    last_check=time.time() - 3000)
    app._auto_update_tick()
    assert app._rechecks == []
    assert app.root.scheduled == [600_000]


# ── N2 — 더 새 버전이 나오면 캐시를 교체한다(리바인딩) ──────────────────────
def _async_app(monkeypatch, cached, latest_tag):
    from localapp import updater as _up
    app = object.__new__(gui.SettingsApp)
    app.root = _FakeRoot()
    app._update_info = cached
    app._last_update_check = 0.0
    monkeypatch.setattr(_up, "check_latest_version",
                        lambda: {"tag": latest_tag, "url": f"https://x/{latest_tag}.zip"})
    return app


def test_check_replaces_cache_when_newer_release_appears(monkeypatch):
    """캐시가 옛 태그인데 더 새 릴리스가 나오면 캐시를 교체한다.

    ⚠ 이 테스트는 수정 **전에도 통과**한다(종전 코드도 무조건 리바인딩했다) —
    N2의 실제 결함은 재조회 자체가 막혀 이 지점에 **도달하지 못한** 것이고, 그건
    test_tick_rechecks_even_after_detection이 잡는다. 여기서는 상향 교체 계약을
    회귀로 고정하는 역할만 한다(짝: test_check_does_not_downgrade_cached_tag —
    그쪽은 수정 전 실패하는 판별 테스트다).
    """
    app = _async_app(monkeypatch, {"tag": "v0.9.90", "url": "https://x/a.zip"},
                     "v0.9.95")
    app._check_updates_async()
    assert app._update_info["tag"] == "v0.9.95"


def test_check_does_not_downgrade_cached_tag(monkeypatch):
    """더 낮은 태그로는 교체하지 않는다 — GitHub가 옛 릴리스를 먼저 주는 경우."""
    app = _async_app(monkeypatch, {"tag": "v0.9.95", "url": "https://x/a.zip"},
                     "v0.9.90")
    app._check_updates_async()
    assert app._update_info["tag"] == "v0.9.95"


def test_check_rebinds_dict_not_mutates(monkeypatch):
    """반드시 **리바인딩** — 설치 진행 중인 경로가 진입 시점에 바인딩한 dict를
    in-place로 바꾸면 다운로드 URL과 태그가 도중에 갈린다.

    ⚠ 이것도 수정 전 통과한다 — 재조회 빈도를 올리는 수정이 in-flight 설치를 깨지
    않는다는 **불변식 고정**이 목적이다(재조회가 잦아질수록 이 창이 넓어진다).
    """
    cached = {"tag": "v0.9.90", "url": "https://x/a.zip"}
    app = _async_app(monkeypatch, cached, "v0.9.95")
    in_flight = app._update_info          # 설치 경로가 잡아둔 참조
    app._check_updates_async()
    assert in_flight == {"tag": "v0.9.90", "url": "https://x/a.zip"}, (
        "in-flight 설치가 보던 dict가 변조됐다")
    assert app._update_info is not in_flight

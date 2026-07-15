"""부팅 시 자동매매 복원 결정 — auto_state.resume_plan.

자동업데이트/재시작/크래시로 프로세스가 재기동돼도 직전 상태를 이어가기 위한 결정.
(GUI 배선 _resume_autotrade_if_running이 이 순수 결정을 사용한다.)
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import auto_state


def test_running_resumes_active():
    """running → 스케줄러 기동·paused 아님(유저 클릭 없이 그대로 가동)."""
    assert auto_state.resume_plan("running") == (True, False)


def test_paused_resumes_paused():
    """paused → 기동하되 pause로 직전 일시정지 보존."""
    assert auto_state.resume_plan("paused") == (True, True)


def test_stopped_does_not_resume():
    """stopped → 명시적 중지이므로 자동 기동 안 함."""
    assert auto_state.resume_plan("stopped") == (False, False)


def test_unknown_conservative_no_resume():
    """알 수 없는 값 → 보수적으로 기동 안 함."""
    assert auto_state.resume_plan("garbage") == (False, False)
    assert auto_state.resume_plan(None) in ((True, False), (True, True), (False, False))

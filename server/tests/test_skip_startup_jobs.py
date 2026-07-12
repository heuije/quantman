"""QP_SKIP_STARTUP_JOBS 게이트 명세 — dev fast-start(scripts/run_dev_server.py).

lifespan의 startup 1회성 잡(초기 fetch·백필·프리워밍)은 _schedule_startup_jobs
단일 진입점으로만 스케줄된다. env=1(dev)이면 그 호출을 통째로 생략하고,
미설정(프로덕션 Railway) 기본은 전부 실행 — 프로덕션 동작 불변 회귀 잠금.
cron 스케줄러는 게이트와 무관하게 양쪽 경로 모두 구동된다.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import main as appmain


@pytest.fixture
def spies(monkeypatch):
    """DB 초기화·startup 잡·cron 빌드를 스파이로 치환 — 실 I/O 없이 lifespan 분기만 검사."""
    s = SimpleNamespace(db=MagicMock(), jobs=MagicMock(), scheduler=MagicMock())
    monkeypatch.setattr(appmain, "create_db_and_tables", s.db)
    monkeypatch.setattr(appmain, "_schedule_startup_jobs", s.jobs)
    monkeypatch.setattr(appmain, "_build_scheduler", MagicMock(return_value=s.scheduler))
    return s


def _boot():
    app = SimpleNamespace(state=SimpleNamespace())

    async def _run():
        async with appmain.lifespan(app):
            pass

    asyncio.run(_run())
    return app


@pytest.mark.parametrize("env_value", [None, "0"])
def test_default_runs_startup_jobs_and_cron(spies, monkeypatch, env_value):
    """미설정/비-1(프로덕션 경로) = startup 잡 스케줄 + cron 구동 — 기존 동작 불변.

    게이트는 정확히 문자열 "1"만 opt-in — 다른 값으로 바뀌면 프로덕션이 잡을 잃는다.
    """
    if env_value is None:
        monkeypatch.delenv("QP_SKIP_STARTUP_JOBS", raising=False)
    else:
        monkeypatch.setenv("QP_SKIP_STARTUP_JOBS", env_value)
    app = _boot()
    spies.jobs.assert_called_once()
    spies.scheduler.start.assert_called_once()
    assert app.state.scheduler is spies.scheduler


def test_skip_flag_gates_startup_jobs_only(spies, monkeypatch):
    """env=1(dev) = startup 잡 생략 — DB 초기화·cron은 그대로."""
    monkeypatch.setenv("QP_SKIP_STARTUP_JOBS", "1")
    app = _boot()
    spies.jobs.assert_not_called()
    spies.db.assert_called_once()
    spies.scheduler.start.assert_called_once()
    assert app.state.scheduler is spies.scheduler

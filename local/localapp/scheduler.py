"""모의투자 스케줄러 (KST). 평일 장 시작 전 1회 사이클 실행."""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .runner import run_cycle

log = logging.getLogger("localapp.scheduler")


def start(use_mock: bool = False) -> None:
    sched = BlockingScheduler(timezone="Asia/Seoul")
    sched.add_job(
        lambda: run_cycle(use_mock),
        CronTrigger(day_of_week="mon-fri", hour=8, minute=55, timezone="Asia/Seoul"),
        id="paper_cycle", name="모의투자 사이클", misfire_grace_time=300,
    )
    print("=" * 48)
    print("  모의투자 스케줄러 시작 (KST)")
    print("  평일 08:55 — 전략 평가 후 매매")
    print(f"  브로커: {'MockBroker(체험)' if use_mock else 'KIS 모의투자'}")
    print("  Ctrl+C 로 종료")
    print("=" * 48)
    sched.start()

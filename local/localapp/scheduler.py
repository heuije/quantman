"""로컬앱 스케줄러 (KST).

- 평일 08:50 — 장중 loop 시작 (시세+체결통보 WebSocket; 메인 사이클 전에 ready)
- 평일 08:55 — 메인 사이클 (매수 신호 평가 + 발주)
- 평일 15:30 — 장중 loop 종료
- 평일 15:35 — 장 마감 후 settlement (미체결 정리 + 잔고 push)

08:50 → 08:55의 5분 갭은 WebSocket 연결·AES key/iv 수신 안정화 마진.
메인 사이클은 진입 직전 intraday_loop.status()로 체결통보 WebSocket ready를
확인 (runner.run_cycle 내부) — 시초가(09:00) 체결 통보 push 보장.

Phase 38.3: use_mock 파라미터 제거. KIS 자격증명 없으면 runner.make_broker가
명시적 RuntimeError로 거부.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from . import intraday_loop
from .runner import run_cycle, run_post_close_settlement

log = logging.getLogger("localapp.scheduler")


def start() -> None:
    sched = BlockingScheduler(timezone="Asia/Seoul")
    # 장중 loop을 메인 사이클보다 먼저 시작 — 시초가 체결 통보 push 보장
    sched.add_job(
        intraday_loop.start,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=50, timezone="Asia/Seoul"),
        id="intraday_stop_start", name="장중 loop 시작 (WebSocket)",
        misfire_grace_time=600,
    )
    sched.add_job(
        run_cycle,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=55, timezone="Asia/Seoul"),
        id="paper_cycle", name="자동매매 사이클", misfire_grace_time=300,
    )
    sched.add_job(
        intraday_loop.stop,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone="Asia/Seoul"),
        id="intraday_stop_stop", name="장중 stop loss 종료",
        misfire_grace_time=300,
    )
    sched.add_job(
        run_post_close_settlement,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=35, timezone="Asia/Seoul"),
        id="post_close_settlement", name="장 마감 후 settlement",
        misfire_grace_time=600,
    )
    print("=" * 48)
    print("  로컬앱 스케줄러 시작 (KST)")
    print("  평일 08:50 — 장중 loop 시작 (WebSocket)")
    print("  평일 08:55 — 자동매매 사이클")
    print("  평일 15:30 — 장중 loop 종료")
    print("  평일 15:35 — 장 마감 후 미체결 정리")
    print("  브로커: KIS (실전/모의투자)")
    print("  Ctrl+C 로 종료")
    print("=" * 48)
    sched.start()

"""로컬앱 스케줄러 (KST).

국내(KRX) — 고정 cron:
- 평일 08:50  장중 loop 시작 (시세+체결통보 WebSocket)
- 평일 08:55  메인 사이클 (KRX 매수/청산 평가 + 발주)
- 평일 15:30  장중 loop 종료
- 평일 15:35  장 마감 후 settlement

미국(US) — 동적 야간 플래너:
- 매일 12:00  오늘 밤 미국 세션을 시장 캘린더로 계산해 one-shot 잡 등록
  · open−5분  미국 자동매매 사이클 (run_cycle market="US")
  · close+5분 미국 장 마감 후 settlement
  (US 장중 손절 loop start/stop은 P8에서 추가)
- 미국 개장은 DST로 22:30↔23:30 KST 이동 + 휴장일이 한국과 달라 고정 cron이
  불가능하므로, 매일 캘린더에 물어 그날치 잡을 등록한다(휴장이면 무동작).

08:50 → 08:55의 5분 갭은 WebSocket 연결·AES key/iv 수신 안정화 마진.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from quant_core import market_calendar as mc

from . import intraday_loop
from .runner import run_cycle, run_post_close_settlement

log = logging.getLogger("localapp.scheduler")
KST = ZoneInfo("Asia/Seoul")


def _plan_us_session(sched: BlockingScheduler, now: datetime | None = None) -> None:
    """오늘 밤 미국 세션을 계산해 one-shot 잡(사이클·정산)을 등록.

    매일 정오 cron + 기동 시 1회 호출. 동일 id로 replace_existing 하므로 중복
    호출이 안전하다. 휴장이면 무동작. 캘린더 데이터 만료 시 명시적 에러 로그.
    now: 테스트용 주입(기본 현재 KST).
    """
    now = now or datetime.now(KST)
    try:
        sess = mc.next_session_kst("US", now)
    except mc.CalendarError as e:
        log.error("미국 세션 스케줄 실패 — 캘린더 데이터 갱신 필요: %s", e)
        return
    if sess is None:
        log.info("예정된 미국 세션 없음")
        return

    open_kst, close_kst = sess
    # 오늘 밤(약 20시간 이내) 열리는 세션만 지금 스케줄 — 그 이후는 다음 정오 plan이 처리
    if open_kst - now > timedelta(hours=20):
        log.info("다음 미국 세션 %s — 아직 멀어 보류 (다음 정오 재계산)",
                 open_kst.strftime("%m-%d %H:%M"))
        return

    loop_start_at = open_kst - timedelta(minutes=10)
    cycle_at = open_kst - timedelta(minutes=5)
    settle_at = close_kst + timedelta(minutes=5)

    # 손절 loop을 사이클보다 먼저 시작 — WebSocket·체결통보 ready 마진 (KRX와 동일 패턴)
    sched.add_job(
        intraday_loop.start, DateTrigger(run_date=loop_start_at),
        kwargs={"market": "US"}, id="us_loop_start", name="미국 장중 손절 loop 시작",
        replace_existing=True, misfire_grace_time=600)
    sched.add_job(
        run_cycle, DateTrigger(run_date=cycle_at),
        kwargs={"market": "US"}, id="us_cycle", name="미국 자동매매 사이클",
        replace_existing=True, misfire_grace_time=600)
    sched.add_job(
        intraday_loop.stop, DateTrigger(run_date=close_kst),
        id="us_loop_stop", name="미국 장중 손절 loop 종료",
        replace_existing=True, misfire_grace_time=600)
    sched.add_job(
        run_post_close_settlement, DateTrigger(run_date=settle_at),
        kwargs={"market": "US"}, id="us_settlement", name="미국 장마감 정산",
        replace_existing=True, misfire_grace_time=1800)

    log.info("미국 세션 스케줄 — loop %s · 사이클 %s · loop종료 %s · 정산 %s (KST)",
             loop_start_at.strftime("%H:%M"), cycle_at.strftime("%H:%M"),
             close_kst.strftime("%m-%d %H:%M"), settle_at.strftime("%H:%M"))


def start() -> None:
    # Q1: 잔고 push 실패분의 백그라운드 retry thread. 정시 cron의 _flush_pending
    # 첫 시도와 별개로 60초 idle 폴링 + 실패 시 backoff [10~600s] 재시도.
    from . import sync_retry
    sync_retry.start()

    # Q2+Q8: 기동 시 1회 캘린더 sync (백그라운드 — 페어링 안 됐으면 silent fail
    # 후 다음 04:00 cron 재시도).
    from . import calendar_sync
    import threading
    threading.Thread(target=calendar_sync.pull_all, daemon=True,
                      name="calendar-sync-initial").start()

    sched = BlockingScheduler(timezone="Asia/Seoul")

    # ── 국내(KRX) 고정 cron ──────────────────────────────────────────────────
    sched.add_job(
        intraday_loop.start,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=50, timezone="Asia/Seoul"),
        id="krx_loop_start", name="KRX 장중 loop 시작 (WebSocket)",
        misfire_grace_time=600)
    sched.add_job(
        run_cycle, kwargs={"market": "KRX"},
        trigger=CronTrigger(day_of_week="mon-fri", hour=8, minute=55, timezone="Asia/Seoul"),
        id="krx_cycle", name="KRX 자동매매 사이클", misfire_grace_time=300)
    sched.add_job(
        intraday_loop.stop,
        CronTrigger(day_of_week="mon-fri", hour=15, minute=30, timezone="Asia/Seoul"),
        id="krx_loop_stop", name="KRX 장중 loop 종료", misfire_grace_time=300)
    sched.add_job(
        run_post_close_settlement, kwargs={"market": "KRX"},
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=35, timezone="Asia/Seoul"),
        id="krx_settlement", name="KRX 장 마감 후 settlement", misfire_grace_time=600)

    # ── 미국(US) 동적 야간 플래너 ────────────────────────────────────────────
    sched.add_job(
        _plan_us_session, kwargs={"sched": sched},
        trigger=CronTrigger(hour=12, minute=0, timezone="Asia/Seoul"),
        id="us_planner", name="미국 세션 야간 플래너", misfire_grace_time=3600)
    # 기동 시 1회 — 정오를 지나 시작했어도 오늘 밤 세션을 놓치지 않도록
    _plan_us_session(sched)

    # ── Q2+Q8: 캘린더 일일 sync (04:00 KST — 서버 03:00 cron 이후 안전 마진) ─
    sched.add_job(
        calendar_sync.pull_all,
        CronTrigger(hour=4, minute=0, timezone="Asia/Seoul"),
        id="calendar_sync", name="시장 캘린더 일일 sync",
        misfire_grace_time=3600)

    # ── Phase 58 — heartbeat (5분 주기, alive 신호) ─────────────────────────
    # cycle 외 시간(새벽 등)에도 웹앱 "끊김" 표시 회피.
    # KIS API 호출 X — 단순 alive ping. 페어링 안 됐으면 silent fail.
    from . import sync_client
    sched.add_job(
        sync_client.push_heartbeat,
        CronTrigger(minute="*/5", timezone="Asia/Seoul"),
        id="heartbeat", name="로컬앱 alive heartbeat",
        misfire_grace_time=120)
    # 기동 시 1회 — 첫 cron까지 대기 안 하고 즉시 alive 표시
    import threading
    threading.Thread(target=sync_client.push_heartbeat,
                      daemon=True, name="heartbeat-initial").start()

    print("=" * 52)
    print("  로컬앱 스케줄러 시작 (KST)")
    print("  [KRX] 08:50 loop · 08:55 사이클 · 15:30 loop종료 · 15:35 정산")
    print("  [US ] 매일 12:00 야간 플래너 → 세션 open−5분 사이클 / close+5분 정산")
    print("        (DST·휴장 자동 반영, 오늘 밤 세션은 기동 시 즉시 등록)")
    print("  [캘린더] 04:00 시장 캘린더 일일 sync (임시공휴일 반영)")
    print("  브로커: KIS (실전/모의투자)")
    print("  Ctrl+C 로 종료")
    print("=" * 52)
    sched.start()

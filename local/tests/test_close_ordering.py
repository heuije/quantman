"""θ — KRX 정산 cron이 선물 종가청산 이후에 오도록 (역순 해소).

2026-06-12 라이브: 정산(15:35)이 선물 종가청산(15:40)보다 먼저 돌아, 종가 단일가
(~15:45) 체결을 그날 안에 확인할 패스가 없었다. 정산은 모든 KRX 종가창이 끝난 뒤
(선물 장종료 15:45 + 여유) 1회 — 15:50으로 재배치한다. catch-up의 "오늘 정산 대상"
임계도 같은 시각과 정합해야 한다(한쪽만 바꾸면 거짓 catch-up/누락).

    cd platform/local && python -m pytest tests/test_close_ordering.py -q
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

KST = ZoneInfo("Asia/Seoul")


def _job_hm(job) -> tuple[int, int]:
    f = {x.name: str(x) for x in job.trigger.fields}
    return int(f["hour"]), int(f["minute"])


def test_krx_settlement_scheduled_after_futures_close(monkeypatch):
    """krx_settlement(정산)는 krx_close_cycle_futures(15:40) 뒤 — 15:50."""
    from localapp import scheduler as sch
    from localapp import calendar_sync, catchup, datafetch, sync_client

    # register_jobs의 부수효과(네트워크·스레드) 무력화 — cron 등록만 검사
    monkeypatch.setattr(sch, "_plan_us_session", lambda sched, now=None: None)
    monkeypatch.setattr(sync_client, "push_heartbeat", lambda: None)
    monkeypatch.setattr(datafetch, "refresh_market_data", lambda: None)
    monkeypatch.setattr(catchup, "run_catchup_on_startup", lambda: None)
    monkeypatch.setattr(calendar_sync, "pull_all", lambda: None)

    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone="Asia/Seoul")
    sch.register_jobs(sched)          # start() 안 함 — 등록만 검사

    jobs = {j.id: j for j in sched.get_jobs()}
    fut_close = _job_hm(jobs["krx_close_cycle_futures"])
    settle = _job_hm(jobs["krx_settlement"])

    assert fut_close == (15, 40)
    assert settle == (15, 50), \
        f"정산은 선물 종가 단일가(15:35~15:45) 체결 이후여야: {settle}"
    assert settle > fut_close, "정산 cron이 선물 종가청산보다 먼저(역순·θ)"


def test_catchup_settlement_threshold_matches_cron(monkeypatch, tmp_path):
    """catch-up의 '오늘 settlement 대상' 임계 = 정산 cron(15:50)과 정합.

    15:45(선물 종가창 직후·정산 전)에 부팅하면 오늘은 아직 정산 대상이 아니다 —
    옛 15:35 임계면 오늘을 대상으로 잘못 잡아 종가청산 체결 확인 전에 reconcile이
    돈다(θ와 같은 역순을 catch-up 경로로 재생산).
    """
    from localapp import catchup

    monkeypatch.setattr(catchup, "CYCLES_PATH", tmp_path / "cycles.jsonl")
    # 캘린더 의존 제거 — 평일=영업일 단순화, US 경로 차단
    monkeypatch.setattr(catchup.mc, "is_session_day",
                        lambda market, d: d.weekday() < 5)
    monkeypatch.setattr(catchup.mc, "next_session_kst",
                        lambda market, now: None)
    monkeypatch.setattr(catchup, "_is_krx_intraday", lambda now: False)
    monkeypatch.setattr(catchup, "_is_us_intraday", lambda now: False)

    # 화 15:45 — 선물 종가청산(15:40) 직후·정산(15:50) 전
    now = datetime(2026, 6, 2, 15, 45, tzinfo=KST)
    plan = catchup._decide_catchup_plan(now=now)

    assert plan.krx_settlement_needed
    assert plan.krx_settlement_date == "2026-06-01", (
        "15:45 부팅 시 오늘은 아직 정산 전(cron 15:50) — 대상은 직전 영업일이어야. "
        f"plan={plan}")


def test_us_session_schedules_futures_close(monkeypatch):
    """US-F4: _plan_us_session이 해외선물 종가청산 잡(us_close_cycle_futures)도 등록한다.

    종전엔 us_close_cycle(주식)만 등록돼 해외선물 day-trade가 종가에 청산되지 않고
    오버나이트 방치됐다. 폐장−5분에 주식·선물 두 잡 모두 등록돼야 한다."""
    from datetime import timedelta

    from localapp import scheduler as sch

    now = datetime(2026, 6, 2, 12, 0, tzinfo=KST)
    open_kst = now + timedelta(hours=10)            # 오늘 밤(20h 이내) → 즉시 스케줄
    close_kst = now + timedelta(hours=16, minutes=30)
    monkeypatch.setattr(sch.mc, "next_session_kst", lambda m, n: (open_kst, close_kst))

    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(timezone="Asia/Seoul")
    sch._plan_us_session(sched, now=now)

    ids = {j.id for j in sched.get_jobs()}
    assert "us_close_cycle_futures" in ids, f"해외선물 종가청산 미등록: {ids}"
    assert "us_close_cycle" in ids, "주식 종가청산도 유지돼야"
    # 두 종가청산은 같은 폐장−5분 창
    jobs = {j.id: j for j in sched.get_jobs()}
    assert (jobs["us_close_cycle_futures"].trigger.run_date
            == jobs["us_close_cycle"].trigger.run_date)

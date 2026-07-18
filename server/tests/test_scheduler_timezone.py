"""cron 트리거 KST 앵커 명세 — Railway(UTC 컨테이너) +9h 시프트 회귀 방지.

실측(2026-07-12, Railway 배포 3건 로그): BackgroundScheduler(timezone="Asia/Seoul")
여도 timezone 미지정 standalone CronTrigger 인스턴스는 스케줄러 tz를 상속하지
않는다 — apscheduler 3.x `_create_trigger`는 트리거 '인스턴스'를 그대로 통과시키고
(스케줄러 tz 주입은 문자열 트리거 형식에만 적용), `CronTrigger.__init__`은
tzlocal.get_localzone()(컨테이너 로컬 tz=UTC)으로 폴백한다. 그 결과 06:05 KST
의도인 kis_master_1st가 06:05Z(15:05 KST 장중)에 발화하는 등 hour 기반 cron
전부가 +9h 시프트로 돌았다(docs/incidents/2026-07-12-cron-utc-anchor-9h-shift.md).

개발 PC(KST)에선 get_localzone()==Asia/Seoul이라 결함이 재현되지 않으므로,
Railway와 동일하게 로컬 tz=UTC를 시뮬레이션해 전 트리거의 KST 앵커를 강제한다.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers.cron import CronTrigger

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import main as appmain

UTC = timezone.utc


@pytest.fixture
def utc_container_scheduler(monkeypatch):
    """Railway 컨테이너(TZ 미설정 → 로컬 tz=UTC)에서 빌드된 스케줄러."""
    monkeypatch.setattr("apscheduler.triggers.cron.get_localzone",
                        lambda: ZoneInfo("UTC"))
    sched = appmain._build_scheduler()
    sched.start(paused=True)
    yield sched
    sched.shutdown(wait=False)


def test_every_cron_trigger_anchored_to_seoul(utc_container_scheduler):
    """전 cron 트리거(미래 추가분 포함)가 컨테이너 tz와 무관하게 KST 앵커."""
    assert str(utc_container_scheduler.timezone) == "Asia/Seoul"
    jobs = utc_container_scheduler.get_jobs()
    assert jobs, "등록된 cron이 없다"
    wrong = [j.id for j in jobs
             if isinstance(j.trigger, CronTrigger) and str(j.trigger.timezone) != "Asia/Seoul"]
    assert wrong == [], f"KST 앵커가 아닌 cron: {wrong}"


def test_hour_cron_fires_at_kst_wall_clock(utc_container_scheduler):
    """대표 cron의 다음 발화 절대시각이 KST 라벨 시각(=UTC−9h)과 일치."""
    by_id = {j.id: j for j in utc_container_scheduler.get_jobs()}
    now = datetime(2026, 1, 15, 0, 0, tzinfo=UTC)                     # 목 09:00 KST
    expect = {
        "kis_master_1st": datetime(2026, 1, 15, 21, 5, tzinfo=UTC),   # 1/16(금) 06:05 KST
        "bonds_daily": datetime(2026, 1, 15, 22, 40, tzinfo=UTC),     # 1/16(금) 07:40 KST
        "krx_1st": datetime(2026, 1, 15, 6, 50, tzinfo=UTC),          # 당일 15:50 KST
        "naver": datetime(2026, 1, 15, 8, 0, tzinfo=UTC),             # 당일 17:00 KST
        "cot_weekly": datetime(2026, 1, 17, 0, 0, tzinfo=UTC),        # 1/17(토) 09:00 KST
    }
    for job_id, want in expect.items():
        got = by_id[job_id].trigger.get_next_fire_time(None, now)
        assert got == want, f"{job_id}: 다음 발화 {got} ≠ 의도 {want}"

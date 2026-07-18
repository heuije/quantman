"""감사 #3 — 스케줄러 폭주 가드 명세.

APScheduler 기본 misfire_grace_time(≈1s)은 GIL/IO로 1초+ 밀린 무거운 cron(naver 15분·
dataset 수십분)을 조용히 drop시킨다(preview 누락 잠재). 또 _run_with_retry가 retry job에
시도마다 다른 id(retry_{name}_{n})를 부여해 max_instances=1(job id별)이 중복 스폰을
막지 못했다.

명세:
- BackgroundScheduler job_defaults = misfire_grace_time 3600 · coalesce True ·
  max_instances 1 — 등록된 모든 cron에 일괄 적용.
- 04:00 KST db_prune cron 등록(감사 #1 pruning의 트리거).
- 기존 cron 시각(07:30 dataset_global 등)은 불변.
- retry job은 고정 id(retry_{name})로 항상 1개만 존재. 정시 cron 재트리거가 대기
  retry를 cancel하는 기존 동작(main.py 정책 주석)은 유지.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import main as appmain


@pytest.fixture
def built_scheduler():
    """_build_scheduler 결과를 paused 시작 — job_defaults가 실제 job에 적용된 상태로
    검사하되 어떤 job도 실행되지 않는다."""
    sched = appmain._build_scheduler()
    sched.start(paused=True)
    yield sched
    sched.shutdown(wait=False)


def _trigger_fields(job) -> dict[str, str]:
    return {f.name: str(f) for f in job.trigger.fields}


def test_job_defaults_guard_applied_to_every_job(built_scheduler):
    jobs = built_scheduler.get_jobs()
    assert jobs, "등록된 cron이 없다"
    for j in jobs:
        assert j.misfire_grace_time == 3600, f"{j.id}: misfire_grace_time={j.misfire_grace_time}"
        assert j.coalesce is True, f"{j.id}: coalesce={j.coalesce}"
        assert j.max_instances == 1, f"{j.id}: max_instances={j.max_instances}"


def test_db_prune_job_registered_at_0400(built_scheduler):
    by_id = {j.id: j for j in built_scheduler.get_jobs()}
    assert "db_prune" in by_id, "pruning cron(db_prune)이 등록되지 않았다"
    f = _trigger_fields(by_id["db_prune"])
    assert (f["hour"], f["minute"]) == ("4", "0")


def test_existing_cron_times_unchanged(built_scheduler):
    """가드 추가가 기존 cron 시각을 건드리지 않았는지 회귀 잠금(대표 3개)."""
    by_id = {j.id: j for j in built_scheduler.get_jobs()}
    expect = {
        "dataset_global": ("7", "30"),
        "dataset_kr": ("18", "15"),
        "krx_1st": ("15", "50"),
    }
    for job_id, (hour, minute) in expect.items():
        f = _trigger_fields(by_id[job_id])
        assert (f["hour"], f["minute"]) == (hour, minute), job_id


def test_retry_job_fixed_id_no_duplicate_spawn():
    """실패 시 retry job은 고정 id(retry_{name}) 1개 — 재트리거에도 중복 스폰 없음."""
    sched = BackgroundScheduler(timezone="Asia/Seoul")

    def boom():
        raise RuntimeError("외부 소스 실패")

    appmain._run_with_retry("t1", boom, sched)
    assert [j.id for j in sched.get_jobs()] == ["retry_t1"]

    appmain._run_with_retry("t1", boom, sched)      # 정시 cron 재트리거
    assert [j.id for j in sched.get_jobs()] == ["retry_t1"]


def test_cron_retrigger_cancels_pending_retry():
    """정시 cron이 다시 트리거되면 기존 retry 큐를 비우는 동작 유지(성공 시 잔여 0)."""
    sched = BackgroundScheduler(timezone="Asia/Seoul")

    def boom():
        raise RuntimeError("실패")

    appmain._run_with_retry("t2", boom, sched)
    assert [j.id for j in sched.get_jobs()] == ["retry_t2"]

    appmain._run_with_retry("t2", lambda: None, sched)   # 다음 정시 cron은 성공
    assert sched.get_jobs() == []

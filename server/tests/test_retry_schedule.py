"""`_run_with_retry`의 **시도 횟수·시각**을 고정한다 (2026-07-21 C1·C8).

이 파일이 없어서 off-by-one이 살아남았다 — `grep _RETRY_MAX_ATTEMPTS server/tests/`가
0건이었다. 종전 코드는 상한을 **첫 시도까지 포함해** `>=`로 세서 5시도(+14분)에서
멈췄고, backoff 마지막 원소 `10`이 한 번도 안 쓰이는 죽은 값이었다. 그런데 같은 파일의
주석은 `08:02 실패 → 08:04/08:06/08:11/08:16/08:26`(6시도)·"누적 24분 후 포기"라고
기술했다 — **주석이 옳고 코드가 틀렸다.** 여기서 그 계약을 잠근다.

아침 선물(`kospi_futures`, 08:02 발화)의 마지막 시도가 로컬 발주창(08:35) 전에
오는지가 실질 쟁점이므로, 그 예산도 함께 단언한다.

    cd platform/server && python -m pytest tests/test_retry_schedule.py -q
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_SERVER_DIR = Path(__file__).resolve().parent.parent
_CORE_DIR = _SERVER_DIR.parent / "core"
for _p in (str(_CORE_DIR), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from apscheduler.jobstores.base import JobLookupError  # noqa: E402

from app import main as m  # noqa: E402


class _FakeScheduler:
    """add_job을 큐에 쌓기만 하는 더블 — 테스트가 직접 다음 시도를 구동한다."""

    def __init__(self):
        self.queued: list[tuple] = []      # (run_date, func, job_id)

    def remove_job(self, job_id):
        raise JobLookupError(job_id)       # 대기 중 retry 없음(정상 경로)

    def add_job(self, func, *, trigger, run_date, id, replace_existing):
        assert trigger == "date" and replace_existing is True
        self.queued.append((run_date, func, id))


def _drain(fn, *, backoffs=None, max_drives=20):
    """항상 실패하는 fn으로 재시도를 끝까지 구동 → (시도 수, backoff 분 리스트).

    ⚠ `_attempt`는 매번 **그 시점의 실제 now** + backoff로 예약한다(누적 체인이 아니다).
    그래서 offset은 예약 직후의 now와의 차이로 재야 한다 — 테스트는 ms 단위로 도니
    그 차이가 곧 backoff 값이고, round()가 경과 오차를 흡수한다.
    """
    sched = _FakeScheduler()
    offsets: list[int] = []
    m._run_with_retry("testjob", fn, sched, backoffs_min=backoffs)   # 1회차 인라인 실행

    drives = 0
    while sched.queued:
        run_date, func, job_id = sched.queued.pop()
        assert job_id == "retry_testjob", "retry job id는 고정이어야 max_instances=1이 듣는다"
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        offsets.append(round((run_date - now_kst).total_seconds() / 60))
        drives += 1
        assert drives < max_drives, "재시도가 끝나지 않는다(무한 루프)"
        func()

    return 1 + drives, offsets


def test_retry_runs_six_attempts_using_every_backoff():
    """첫 시도 + 재시도 5회 = 6시도. backoff [2,2,5,5,10] 전 원소가 쓰여야 한다."""
    calls = []

    def _always_fail():
        calls.append(1)
        raise RuntimeError("boom")

    attempts, offsets = _drain(_always_fail)

    assert attempts == 6, f"첫 시도 제외 재시도 5회 = 총 6시도여야 함 — 실제 {attempts}"
    assert len(calls) == 6
    assert offsets == m._RETRY_BACKOFFS_MIN, (
        f"backoff 전 원소가 순서대로 쓰여야 함(마지막 원소가 죽은 값이면 회귀) — 실제 {offsets}")
    assert sum(offsets) == 24, "누적 24분 후 포기(주석이 기술한 정책)"


def test_success_stops_retrying():
    """부정 대조 — 성공하면 재시도를 큐에 넣지 않는다."""
    sched = _FakeScheduler()
    m._run_with_retry("testjob", lambda: None, sched)
    assert sched.queued == []


def test_retry_stops_after_first_success():
    """3회차에 성공하면 그 뒤 시도가 없어야 한다(상한만 보고 계속 돌면 회귀)."""
    n = {"i": 0}

    def _fail_twice():
        n["i"] += 1
        if n["i"] <= 2:
            raise RuntimeError("boom")

    attempts, offsets = _drain(_fail_twice)
    assert attempts == 3 and offsets == [2, 2]


def test_morning_futures_last_attempt_precedes_order_window():
    """🔴 아침 선물 예산 — 08:02 발화의 **마지막 시도가 08:35 발주창 전**이어야 한다.

    `kospi_futures`는 기본 backoff를 쓴다(main.py 주석의 08:04/06/11/16/26 스케줄).
    마지막 시도 08:26 이후 번들 재포장·preview까지 끝나야 로컬 08:35 사이클이 신선한
    데이터를 본다 — 그 소요는 코드가 아니라 실측 대상(L1)이므로 여기선 **발화 시각만**
    잠근다. 이 단언이 깨지면 backoff 총합이 늘어난 것이고, 발주창을 넘겼는지 재검토해야 한다.
    """
    fire = datetime(2026, 7, 21, 8, 2)
    cum = 0
    times = []
    for b in m._RETRY_BACKOFFS_MIN:
        cum += b
        times.append(fire + timedelta(minutes=cum))

    assert [t.strftime("%H:%M") for t in times] == ["08:04", "08:06", "08:11", "08:16", "08:26"]
    assert times[-1] < datetime(2026, 7, 21, 8, 35), "마지막 재시도가 발주창(08:35)을 넘었다"

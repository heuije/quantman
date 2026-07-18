"""로드맵 B·E — 스케줄러 타임라인 정비 회귀 가드.

E: 장중 감시 08:30~15:45(장별 공백 해소) · 수렴 개장+1분(선물 08:46·주식 09:01).
B: dataset 예열 08:05/20/32(서버 재포장 07:30·08:02 정렬·08:35 발주 전 마지노선).
등록 시각이 코드 리뷰 없이 회귀하지 않도록 잡 레지스트리를 직접 검증한다.
"""

from __future__ import annotations

from localapp import scheduler as sch


class _FakeSched:
    def __init__(self):
        self.jobs = {}

    def add_job(self, fn, trigger=None, *, id=None, name=None, kwargs=None,
                misfire_grace_time=None, replace_existing=False):
        self.jobs[id] = {"trigger": trigger, "name": name, "kwargs": kwargs}

    def add_listener(self, *a, **k):
        pass


def _cron_hm(trigger):
    f = {x.name: str(x) for x in trigger.fields}
    return f.get("hour"), f.get("minute")


def _register(monkeypatch):
    fake = _FakeSched()
    # 네트워크·스레드 부작용 차단(기동 initial들) — 잡 등록만 검증.
    from localapp import catchup, datafetch, sync_client
    monkeypatch.setattr(sync_client, "push_heartbeat", lambda: None)
    monkeypatch.setattr(datafetch, "refresh_market_data", lambda: None)
    monkeypatch.setattr(catchup, "run_catchup_on_startup", lambda: {})
    monkeypatch.setattr(sch, "_plan_us_session", lambda s, now=None: None)
    sch.register_jobs(fake)
    return fake


def test_intraday_loop_covers_auction_to_futures_close(monkeypatch):
    jobs = _register(monkeypatch).jobs
    assert _cron_hm(jobs["krx_loop_start"]["trigger"]) == ("8", "30")
    assert _cron_hm(jobs["krx_loop_stop"]["trigger"]) == ("15", "45")


def test_converge_at_open_plus_one(monkeypatch):
    jobs = _register(monkeypatch).jobs
    assert _cron_hm(jobs["krx_converge_futures"]["trigger"]) == ("8", "46")
    assert _cron_hm(jobs["krx_converge_stock"]["trigger"]) == ("9", "1")


def test_prewarm_realigned(monkeypatch):
    jobs = _register(monkeypatch).jobs
    ids = [k for k in jobs if k.startswith("dataset_sync_")]
    assert sorted(ids) == ["dataset_sync_0805", "dataset_sync_0820",
                            "dataset_sync_0832"]


def test_auction_guard_windows_registered(monkeypatch):
    """#16 — 동시호가 가드 4창(발주 직후~단일가 수십 초 전) 등록 회귀 잠금."""
    jobs = _register(monkeypatch).jobs
    assert _cron_hm(jobs["guard_open_futures"]["trigger"]) == ("8", "36")
    assert _cron_hm(jobs["guard_open_stock"]["trigger"]) == ("8", "56")
    assert _cron_hm(jobs["guard_close_stock"]["trigger"]) == ("15", "26")
    assert _cron_hm(jobs["guard_close_futures"]["trigger"]) == ("15", "41")
    kw = jobs["guard_close_futures"]["kwargs"]
    assert kw["instrument_class"] == "futures" and kw["window"] == "close"
    assert kw["start_hm"] == (15, 39) and kw["until_hms"] == (15, 44, 30)

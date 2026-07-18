"""로드맵 C — preview pull 실패/없음 구분 계약 + 기준 거래일 검증.

1. pull_preview: 실패(PreviewUnavailable — 재시도 가치)와 명시적 없음(None —
   404/available=False)을 구분한다. 캐시 히트는 실패를 흡수(fallback 유지).
2. _preview_stale_reason: 후보의 data_as_of가 직전 거래일보다 낡으면 사유 반환
   (진입 차단용). 필드 부재(구버전 서버)·캘린더 불가 시 None(over-block 금지).
"""

from __future__ import annotations

from datetime import date

import pytest

from localapp import sync_client
from localapp.runner import _preview_stale_reason
from localapp.sync_client import PreviewUnavailable


class _Resp:
    def __init__(self, status: int, body: dict | None = None, bad_json: bool = False):
        self.status_code = status
        self.ok = status < 400
        self._body = body or {}
        self._bad = bad_json

    def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._body


@pytest.fixture
def no_cache(monkeypatch):
    monkeypatch.setattr(sync_client, "_load_preview_cache", lambda: None)
    saved = {}
    monkeypatch.setattr(sync_client, "_save_preview_cache",
                        lambda d: saved.update({"data": d}))
    monkeypatch.setattr(sync_client, "_headers", lambda: {})
    return saved


def test_network_fail_no_cache_raises(monkeypatch, no_cache):
    def _boom(*a, **k):
        raise OSError("conn refused")

    monkeypatch.setattr(sync_client.requests, "get", _boom)
    with pytest.raises(PreviewUnavailable):
        sync_client.pull_preview()


def test_network_fail_with_cache_returns_cached(monkeypatch, no_cache):
    monkeypatch.setattr(sync_client, "_load_preview_cache",
                        lambda: {"available": True, "by_strategy": []})

    def _boom(*a, **k):
        raise OSError("conn refused")

    monkeypatch.setattr(sync_client.requests, "get", _boom)
    out = sync_client.pull_preview()
    assert out == {"available": True, "by_strategy": []}


def test_404_returns_none_not_retryable(monkeypatch, no_cache):
    monkeypatch.setattr(sync_client.requests, "get", lambda *a, **k: _Resp(404))
    assert sync_client.pull_preview() is None


def test_server_error_no_cache_raises(monkeypatch, no_cache):
    monkeypatch.setattr(sync_client.requests, "get", lambda *a, **k: _Resp(503))
    with pytest.raises(PreviewUnavailable):
        sync_client.pull_preview()


def test_unavailable_flag_returns_none(monkeypatch, no_cache):
    monkeypatch.setattr(sync_client.requests, "get",
                        lambda *a, **k: _Resp(200, {"available": False}))
    assert sync_client.pull_preview() is None


def test_success_returns_and_caches(monkeypatch, no_cache):
    body = {"available": True, "by_strategy": [], "data_as_of": {"KR": "2026-07-17"}}
    monkeypatch.setattr(sync_client.requests, "get", lambda *a, **k: _Resp(200, body))
    out = sync_client.pull_preview()
    assert out == body
    assert no_cache["data"] == body


# ── 기준 거래일 검증 ─────────────────────────────────────────────────────────


def test_stale_reason_blocks_old_as_of(monkeypatch):
    from quant_core import market_calendar as mc
    monkeypatch.setattr(mc, "prev_session_day", lambda m, d: date(2026, 7, 17))
    r = _preview_stale_reason({"data_as_of": {"KR": "2026-07-16"}}, "KRX",
                              today=date(2026, 7, 18))
    assert r is not None and "2026-07-16" in r and "2026-07-17" in r


def test_stale_reason_fresh_passes(monkeypatch):
    from quant_core import market_calendar as mc
    monkeypatch.setattr(mc, "prev_session_day", lambda m, d: date(2026, 7, 17))
    assert _preview_stale_reason({"data_as_of": {"KR": "2026-07-17"}}, "KRX",
                                 today=date(2026, 7, 18)) is None
    # 당일 저녁 재계산분(오늘 봉 포함)도 통과
    assert _preview_stale_reason({"data_as_of": {"KR": "2026-07-18"}}, "KRX",
                                 today=date(2026, 7, 18)) is None


def test_stale_reason_missing_field_passes(monkeypatch):
    """구버전 서버(data_as_of 없음) — 검증 없이 통과(종전 거동·버전 전환기)."""
    assert _preview_stale_reason({"by_strategy": []}, "KRX",
                                 today=date(2026, 7, 18)) is None


def test_stale_reason_us_market_key(monkeypatch):
    from quant_core import market_calendar as mc
    seen = {}

    def fake_prev(market, d):
        seen["market"] = market
        return date(2026, 7, 17)

    monkeypatch.setattr(mc, "prev_session_day", fake_prev)
    r = _preview_stale_reason({"data_as_of": {"US": "2026-07-15"}}, "US",
                              today=date(2026, 7, 18))
    assert r is not None and seen["market"] == "US"


def test_stale_reason_calendar_out_of_range_passes(monkeypatch):
    """캘린더 범위 밖(prev=None) — 차단하지 않음(AL-3 over-block 금지)."""
    from quant_core import market_calendar as mc
    monkeypatch.setattr(mc, "prev_session_day", lambda m, d: None)
    assert _preview_stale_reason({"data_as_of": {"KR": "2020-01-01"}}, "KRX",
                                 today=date(2026, 7, 18)) is None


# ── 공용 재시도 헬퍼 (아침·종가 — 유저 확정: 종가창도 동일 3회) ───────────────


def test_retry_helper_recovers_after_failures(monkeypatch):
    from localapp import runner
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    seq = iter([PreviewUnavailable("x"), PreviewUnavailable("y"),
                {"available": True, "by_strategy": []}])

    def fake_pull():
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(runner, "pull_preview", fake_pull)
    assert runner._pull_preview_with_retry() == {"available": True,
                                                  "by_strategy": []}


def test_retry_helper_definitive_none_no_retry(monkeypatch):
    from localapp import runner
    calls = {"n": 0}

    def fake_pull():
        calls["n"] += 1
        return None                      # 서버가 명시한 '후보 없음'(정상)

    monkeypatch.setattr(runner, "pull_preview", fake_pull)
    assert runner._pull_preview_with_retry() is None
    assert calls["n"] == 1               # 재시도 없음


def test_retry_helper_exhausts_to_none(monkeypatch):
    from localapp import runner
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)

    def fake_pull():
        raise PreviewUnavailable("down")

    monkeypatch.setattr(runner, "pull_preview", fake_pull)
    assert runner._pull_preview_with_retry("[종가] ") is None

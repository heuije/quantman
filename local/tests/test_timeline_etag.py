"""로컬앱 sync_client.pull_timeline — If-None-Match/304 캐시 (egress 후속).

서버 /sync/timeline이 tag-first ETag를 주므로, 로컬앱이 If-None-Match를 보내고
304면 마지막 데이터를 재사용해 Neon egress를 절감한다(60s 폴링이 변경 없으면 0 byte).
인시던트: docs/incidents/2026-06-10-neon-data-transfer-quota.md
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from localapp import sync_client  # noqa: E402


class _Resp:
    def __init__(self, status, etag=None, body=None):
        self.status_code = status
        self.ok = status < 400
        self.headers = {"ETag": etag} if etag else {}
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _install(monkeypatch, responses):
    """requests.get를 순차 응답 스텁으로 교체. 보낸 headers를 calls에 기록."""
    calls: list[dict] = []
    seq = iter(responses)

    def fake_get(url, headers=None, timeout=None):
        calls.append(dict(headers or {}))
        return next(seq)

    monkeypatch.setattr(sync_client, "requests",
                        type("R", (), {"get": staticmethod(fake_get)}))
    monkeypatch.setattr(sync_client, "_headers", lambda: {})
    # 모듈 전역 캐시 초기화(테스트 격리)
    sync_client._timeline_cache = {"etag": "", "data": None}
    return calls


def test_first_call_caches_etag_no_if_none_match(monkeypatch):
    calls = _install(monkeypatch, [
        _Resp(200, etag='W/"100-200"', body={"events": [1], "now": "x"})])
    data = sync_client.pull_timeline()
    assert data == {"events": [1], "now": "x"}
    assert "If-None-Match" not in calls[0]          # 첫 호출 — 캐시 없음
    assert sync_client._timeline_cache["etag"] == 'W/"100-200"'


def test_second_call_sends_if_none_match_and_304_returns_cached(monkeypatch):
    calls = _install(monkeypatch, [
        _Resp(200, etag='W/"100-200"', body={"events": [1]}),
        _Resp(304)])                                 # 변경 없음
    first = sync_client.pull_timeline()
    second = sync_client.pull_timeline()
    assert calls[1].get("If-None-Match") == 'W/"100-200"'   # 캐시된 ETag 송신
    assert second == first == {"events": [1]}              # 304 → 캐시 재사용(None 아님)


def test_200_with_new_etag_updates_cache(monkeypatch):
    calls = _install(monkeypatch, [
        _Resp(200, etag='W/"1-1"', body={"events": ["a"]}),
        _Resp(200, etag='W/"2-2"', body={"events": ["b"]})])  # 변경 발생
    sync_client.pull_timeline()
    data2 = sync_client.pull_timeline()
    assert calls[1].get("If-None-Match") == 'W/"1-1"'
    assert data2 == {"events": ["b"]}
    assert sync_client._timeline_cache["etag"] == 'W/"2-2"'


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

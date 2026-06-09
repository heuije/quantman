"""회귀 — dataset 번들 다운로드는 fast-fail timeout을 써야 한다(정규 cycle hang 방지).

근본 결함: fetch_dataset_bundle의 requests.get(timeout=300)이 stream 중 stall되면 최대
300초 hang. refresh_market_data는 예외 시 캐시로 폴백하지만, 300초 timeout이 먼저
끝나야 그 폴백이 동작 → 정규 cycle(08:55 KRX 등)이 Railway 불안정 시 ~5분 멈춤.
(비상 청산은 #70에서 이 경로를 아예 안 타도록 분리됨 — 이 수정은 정규 cycle robustness용.)

수정: (connect, read) 튜플 timeout으로 stalled stream을 수십초 내 fail → 캐시 폴백 즉시.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import sync_client


def test_bundle_download_uses_fast_fail_tuple_timeout(tmp_path):
    """requests.get timeout이 (connect, read) 튜플이고 read가 합리적으로 짧아야 한다."""
    resp = MagicMock(status_code=304)
    with patch.object(sync_client.requests, "get", return_value=resp) as mget:
        out = sync_client.fetch_dataset_bundle(tmp_path)
    assert out.get("skipped") is True            # 304 → skip 경로
    _, kwargs = mget.call_args
    timeout = kwargs.get("timeout")
    assert isinstance(timeout, tuple), f"단일 long timeout 금지, 튜플이어야: {timeout!r}"
    connect, read = timeout
    assert 0 < connect <= 15, f"connect timeout 과대: {connect}"
    assert 0 < read <= 60, f"read timeout 과대(hang 위험): {read}"


def test_bundle_hang_falls_back_to_cache_without_raising(monkeypatch):
    """스트림 stall(ReadTimeout)이면 refresh_market_data가 캐시 폴백(False) — 예외·hang 없음."""
    from localapp import datafetch

    def _raise_timeout(*a, **k):
        raise requests.exceptions.ReadTimeout("stream stalled")

    monkeypatch.setattr(sync_client.requests, "get", _raise_timeout)
    # ReadTimeout은 ValueError가 아니므로 manifest(sync_dataset) 폴백이 아닌
    # 캐시 폴백 경로 — sync_dataset이 호출되면 안 된다(느린 114분 경로 회피).
    called = {"sync_dataset": False}
    monkeypatch.setattr(sync_client, "sync_dataset",
                        lambda *a, **k: called.__setitem__("sync_dataset", True))
    result = datafetch.refresh_market_data()
    assert result is False                        # 캐시 폴백
    assert called["sync_dataset"] is False         # manifest 느린 경로 안 탐

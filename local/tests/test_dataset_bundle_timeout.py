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


@pytest.fixture(autouse=True)
def _stub_device_token(monkeypatch):
    """이 파일의 모든 테스트는 페어링 토큰과 무관하게 타임아웃 동작만 격리 검증한다.

    fetch_dataset_bundle은 타임아웃 로직 전에 _headers()→load_device_token()을
    호출하므로, 토큰이 키링에 없으면(미페어링 CI·초기화 직후) RuntimeError로
    먼저 깨져(sync_client.py) 정작 검증하려는 타임아웃 경로에 도달하지 못한다.
    load_device_token을 스텁해 그 의존을 끊어 — 페어링 여부와 상관없이 항상
    같은 타임아웃 동작을 검증하게 한다(이 부류 전체를 한 곳에서 닫는다).
    """
    monkeypatch.setattr(sync_client, "load_device_token", lambda: "dummy-token")


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
    result = datafetch.refresh_market_data()
    assert result is False                        # 캐시 폴백


def test_bundle_total_duration_cap(monkeypatch, tmp_path):
    """read timeout이 못 막는 저속 trickle 스트림은 총 시간 상한으로 끊는다.

    requests의 read timeout은 '청크 간 간격'만 제한 — 서버가 degraded 상태로
    바이트를 찔끔찔끔 흘리면 다운로드가 무한정 늘어지며 갱신 스레드를 점유한다
    (2026-06-10 무발주 인시던트 D1-1 부류). 총 시간 상한이 TimeoutError로 끊고,
    호출자(datafetch)는 캐시로 진행한다.
    """
    class _TrickleResp:
        status_code = 200
        headers = {"ETag": '"abc123"'}

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            while True:
                yield b"x"

    monkeypatch.setattr(sync_client.requests, "get",
                        lambda *a, **k: _TrickleResp())
    monkeypatch.setattr(sync_client, "_BUNDLE_TOTAL_TIMEOUT_SEC", 0)
    with pytest.raises(TimeoutError):
        sync_client.fetch_dataset_bundle(tmp_path)

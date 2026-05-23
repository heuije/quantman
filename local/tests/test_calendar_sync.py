"""Q2+Q8 회귀 — localapp.calendar_sync.

서버 /calendars/{market}에서 일일 1회 pull → ~/.quantman/calendars/ 저장 +
market_calendar._load 캐시 무효화. 실패 시 fallback이 동작하므로 sync 실패가
시스템을 막지 않음.

검증:
1. pull_one 성공: 디스크 저장 + 캐시 무효화
2. pull_one HTTP 실패: False 반환 (raise X)
3. pull_one 페어링 안 됨: False 반환
4. pull_all: KR/US 둘 다 시도
5. 디스크 atomic write (tmp → replace)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """USER_CACHE_DIR 격리 + device token mock."""
    user_dir = tmp_path / "calendars"
    monkeypatch.setenv("QUANTMAN_CALENDAR_DIR", str(user_dir))

    import importlib
    from quant_core import market_calendar
    importlib.reload(market_calendar)
    from localapp import calendar_sync
    importlib.reload(calendar_sync)

    # device token mock
    monkeypatch.setattr(calendar_sync, "load_device_token",
                         lambda: "dev-token-123")
    yield user_dir, calendar_sync, market_calendar


def _make_response(status_code: int, json_data: dict | None = None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        resp.raise_for_status.return_value = None
    return resp


def test_pull_one_success_writes_disk(isolated, monkeypatch):
    user_dir, sync, mc = isolated
    payload = {
        "market": "KR", "tz_local": "Asia/Seoul",
        "range": ["2026-01-01", "2028-12-31"],
        "sessions": {"2026-05-23": ["09:00", "15:30"]},
    }
    monkeypatch.setattr(sync, "requests", MagicMock(
        get=lambda *a, **kw: _make_response(200, payload)))
    ok = sync.pull_one("KR")
    assert ok is True
    path = user_dir / "kr_sessions.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == payload


def test_pull_one_http_failure_returns_false(isolated, monkeypatch):
    user_dir, sync, mc = isolated
    monkeypatch.setattr(sync, "requests", MagicMock(
        get=lambda *a, **kw: _make_response(503)))
    ok = sync.pull_one("KR")
    assert ok is False
    # 파일 미생성
    assert not (user_dir / "kr_sessions.json").exists()


def test_pull_one_no_token_returns_false(isolated, monkeypatch):
    user_dir, sync, mc = isolated
    monkeypatch.setattr(sync, "load_device_token", lambda: None)
    ok = sync.pull_one("KR")
    assert ok is False


def test_pull_one_invalidates_load_cache(isolated, monkeypatch):
    """pull 성공 후 _load 캐시 무효화 → 다음 호출이 새 파일 읽음."""
    user_dir, sync, mc = isolated

    # 첫 로드: 번들 (사용자 캐시 비어있음)
    cal_before = mc._load("KR")

    # 사용자 캐시에 새 데이터 pull (range 다름)
    payload = {
        "market": "KR", "tz_local": "Asia/Seoul",
        "range": ["2099-01-01", "2099-12-31"],
        "sessions": {"2099-06-01": ["09:00", "15:30"],
                     "2099-12-30": ["09:00", "15:30"]},
    }
    monkeypatch.setattr(sync, "requests", MagicMock(
        get=lambda *a, **kw: _make_response(200, payload)))
    ok = sync.pull_one("KR")
    assert ok is True

    # 캐시 무효화 후 새 데이터 보여야 함
    cal_after = mc._load("KR")
    assert "2099-06-01" in cal_after["sessions"]
    assert cal_after["sorted_days"][-1] == "2099-12-30"


def test_pull_all_attempts_both_markets(isolated, monkeypatch):
    user_dir, sync, mc = isolated
    payload = {"market": "X", "tz_local": "UTC",
               "range": ["2026-01-01", "2026-12-31"], "sessions": {}}
    monkeypatch.setattr(sync, "requests", MagicMock(
        get=lambda *a, **kw: _make_response(200, payload)))
    result = sync.pull_all()
    assert set(result.keys()) == {"KR", "US"}
    assert result["KR"] is True
    assert result["US"] is True


def test_pull_all_handles_partial_failure(isolated, monkeypatch):
    """KR 성공, US 실패 — 부분 실패도 OK."""
    user_dir, sync, mc = isolated

    calls = {"n": 0}
    def _get(url, *a, **kw):
        calls["n"] += 1
        if "KR" in url:
            return _make_response(200, {
                "market": "KR", "tz_local": "Asia/Seoul",
                "range": ["2026-01-01", "2026-12-31"], "sessions": {}})
        return _make_response(503)

    monkeypatch.setattr(sync, "requests", MagicMock(get=_get))
    result = sync.pull_all()
    assert result["KR"] is True
    assert result["US"] is False


def test_disk_write_is_atomic(isolated, monkeypatch):
    """tmp 파일 + os.replace — 쓰기 도중 손상에도 이전 파일 또는 새 파일만 보임."""
    user_dir, sync, mc = isolated
    user_dir.mkdir(parents=True, exist_ok=True)
    # 기존 파일 있음
    old = {"market": "KR", "tz_local": "Asia/Seoul",
           "range": ["2025-01-01", "2025-12-31"],
           "sessions": {"2025-01-02": ["09:00", "15:30"]}}
    (user_dir / "kr_sessions.json").write_text(json.dumps(old),
                                                  encoding="utf-8")

    payload = {"market": "KR", "tz_local": "Asia/Seoul",
               "range": ["2026-01-01", "2026-12-31"],
               "sessions": {"2026-05-23": ["09:00", "15:30"]}}
    monkeypatch.setattr(sync, "requests", MagicMock(
        get=lambda *a, **kw: _make_response(200, payload)))
    sync.pull_one("KR")

    # 최종 파일은 새 내용
    final = json.loads((user_dir / "kr_sessions.json").read_text(encoding="utf-8"))
    assert final["range"] == ["2026-01-01", "2026-12-31"]
    # tmp 파일 남아있지 않음 (정상 종료 시)
    assert not (user_dir / "kr_sessions.json.tmp").exists()

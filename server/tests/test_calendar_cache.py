"""Q2+Q8 회귀 — server.calendar_cache 빌드/캐시/디스크.

서버 일일 03:00 KST cron이 exchange_calendars로 KR/US 세션 재빌드 →
디스크 + 메모리 캐시. 로컬앱이 /calendars/{market}로 pull.

검증:
1. refresh() KR/US 둘 다 빌드 (실제 exchange_calendars 호출)
2. 디스크 저장 (atomic: tmp + replace)
3. get() 메모리 hit
4. 메모리 miss + 디스크 hit (서버 재시작 시뮬레이션)
5. get_status() 진단 정보
6. 미지원 market은 None 반환

네트워크 무관 — exchange_calendars는 로컬 데이터만 사용.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """캐시 디렉토리 격리 + 메모리 state 초기화."""
    from app import calendar_cache
    monkeypatch.setattr(calendar_cache, "_CACHE_DIR", tmp_path)
    with calendar_cache._lock:
        calendar_cache._state["KR"] = None
        calendar_cache._state["US"] = None
        calendar_cache._state["built_at"] = None
        calendar_cache._state["last_error"] = None
    yield tmp_path, calendar_cache


def test_refresh_builds_both_markets(isolated_cache):
    """exchange_calendars로 KR/US 빌드 후 메모리/디스크 모두 저장."""
    pytest.importorskip("exchange_calendars")
    tmp_path, cc = isolated_cache
    result = cc.refresh()
    assert result["KR"]["ok"] is True
    assert result["US"]["ok"] is True
    assert result["KR"]["n_sessions"] > 100   # 2년치 영업일 ≈ 500
    assert result["US"]["n_sessions"] > 100

    # 디스크 파일 존재
    assert (tmp_path / "kr_sessions.json").exists()
    assert (tmp_path / "us_sessions.json").exists()

    # 메모리 캐시 채워짐
    assert cc._state["KR"] is not None
    assert cc._state["US"] is not None
    assert cc._state["built_at"] is not None


def test_get_returns_cached(isolated_cache):
    pytest.importorskip("exchange_calendars")
    tmp_path, cc = isolated_cache
    cc.refresh()
    data = cc.get("KR")
    assert data is not None
    assert data["market"] == "KR"
    assert "sessions" in data
    # 오늘 또는 근접한 한국 영업일이 있어야 함
    assert len(data["sessions"]) > 100


def test_get_loads_from_disk_when_memory_empty(isolated_cache):
    """메모리 None인데 디스크에 파일 있으면 디스크 로드 (서버 재시작 시뮬레이션)."""
    pytest.importorskip("exchange_calendars")
    tmp_path, cc = isolated_cache
    cc.refresh()
    # 메모리만 비우고 디스크는 유지
    with cc._lock:
        cc._state["KR"] = None
    data = cc.get("KR")
    assert data is not None
    assert data["market"] == "KR"
    # 메모리에 다시 채워짐
    assert cc._state["KR"] is not None


def test_get_returns_none_for_unsupported(isolated_cache):
    tmp_path, cc = isolated_cache
    assert cc.get("BOGUS") is None


def test_get_returns_none_when_not_built(isolated_cache):
    """refresh 호출 전 + 디스크에도 파일 없으면 None."""
    tmp_path, cc = isolated_cache
    assert cc.get("KR") is None


def test_status_reports_state(isolated_cache):
    pytest.importorskip("exchange_calendars")
    tmp_path, cc = isolated_cache
    cc.refresh()
    st = cc.get_status()
    assert st["kr_loaded"] is True
    assert st["us_loaded"] is True
    assert st["built_at"] is not None
    assert st["last_error"] is None


def test_disk_write_uses_atomic_replace(isolated_cache):
    """tmp 파일 잔존 안 함 (정상 종료 시)."""
    pytest.importorskip("exchange_calendars")
    tmp_path, cc = isolated_cache
    cc.refresh()
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []

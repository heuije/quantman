"""로드맵 A — krx_holiday_source(KIS 국내휴장일조회 수집) + calendar_cache 오버레이.

네트워크 없음: requests를 가짜 응답으로 대체. 검증 항목:
1. 자격증명 미설정 → 수집 비활성(네트워크 호출 0)
2. 응답 파싱·연속조회(tr_cont M→D)·형식 이상 fail-loud
3. 저장소 누적(과거 보존)·신선 skip·실패 시 기존 저장소 유지
4. 첫 실행 백필(과거 기준일 추가 조회)
5. calendar_cache._apply_krx_overlay — KIS 권위 적용(제거/추가)+divergence 기록
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))


@pytest.fixture
def hs(monkeypatch, tmp_path):
    """krx_holiday_source 격리 — 저장소 디렉토리·메모리 state·자격증명."""
    from app import krx_holiday_source as h
    monkeypatch.setattr(h, "_STORE_DIR", tmp_path)
    with h._lock:
        h._state["days"] = None
        h._state["checked_at"] = None
        h._state["last_error"] = None
    monkeypatch.setattr(h.settings, "KIS_APPKEY", "test-key")
    monkeypatch.setattr(h.settings, "KIS_APPSECRET", "test-secret")
    yield h


class _FakeResp:
    def __init__(self, body: dict, tr_cont: str = ""):
        self._body = body
        self.headers = {"tr_cont": tr_cont}
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _page(rows, ctx_fk="", ctx_nk=""):
    return {"rt_cd": "0", "msg1": "정상",
            "output": rows, "ctx_area_fk": ctx_fk, "ctx_area_nk": ctx_nk}


def _row(ymd: str, opnd: str):
    return {"bass_dt": ymd, "wday_dvsn_cd": "03", "bzdy_yn": "Y",
            "tr_day_yn": "Y", "opnd_yn": opnd, "sttl_day_yn": "Y"}


def test_inactive_without_credentials(hs, monkeypatch):
    monkeypatch.setattr(hs.settings, "KIS_APPKEY", "")

    def _boom(*a, **k):
        raise AssertionError("자격증명 없이 네트워크 호출 발생")

    monkeypatch.setattr(hs.requests, "get", _boom)
    monkeypatch.setattr(hs.requests, "post", _boom)
    r = hs.refresh_if_stale()
    assert r == {"ok": False, "reason": "credentials_missing"}


def test_fetch_pages_parses_and_paginates(hs, monkeypatch):
    monkeypatch.setattr(hs, "_token", lambda: "T")
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"headers": headers, "params": params})
        if len(calls) == 1:
            return _FakeResp(_page([_row("20260717", "N"), _row("20260718", "Y")],
                                    ctx_fk="FK1", ctx_nk="NK1"), tr_cont="M")
        return _FakeResp(_page([_row("20260720", "Y")]), tr_cont="D")

    monkeypatch.setattr(hs.requests, "get", fake_get)
    days = hs._fetch_pages("20260717")
    assert days == {"2026-07-17": False, "2026-07-18": True, "2026-07-20": True}
    # 연속조회 2번째 호출: tr_cont=N + 직전 ctx 키 전달
    assert calls[1]["headers"]["tr_cont"] == "N"
    assert calls[1]["params"]["CTX_AREA_FK"] == "FK1"
    assert calls[1]["params"]["CTX_AREA_NK"] == "NK1"


def test_fetch_pages_rt_cd_error_raises(hs, monkeypatch):
    monkeypatch.setattr(hs, "_token", lambda: "T")
    monkeypatch.setattr(hs.requests, "get",
                        lambda *a, **k: _FakeResp({"rt_cd": "1", "msg1": "오류"}))
    with pytest.raises(RuntimeError, match="rt_cd=1"):
        hs._fetch_pages("20260717")


def test_fetch_pages_bad_row_raises(hs, monkeypatch):
    monkeypatch.setattr(hs, "_token", lambda: "T")
    monkeypatch.setattr(
        hs.requests, "get",
        lambda *a, **k: _FakeResp(_page([{"bass_dt": "20260717"}])))  # opnd_yn 없음
    with pytest.raises(RuntimeError, match="형식 이상"):
        hs._fetch_pages("20260717")


def test_refresh_accumulates_and_backfills(hs, monkeypatch):
    fetched_bass = []

    def fake_fetch(bass_dt):
        fetched_bass.append(bass_dt)
        if len(fetched_bass) == 1:            # 오늘 기준 forward
            return {"2026-07-17": False, "2026-07-20": True}
        return {"2026-06-02": True}           # 백필(과거 기준일)

    monkeypatch.setattr(hs, "_fetch_pages", fake_fetch)
    r = hs.refresh_if_stale(now=datetime(2026, 7, 18, 0, 0, tzinfo=timezone.utc))
    assert r["ok"] is True and r["n_days"] == 3
    assert len(fetched_bass) == 2             # 첫 실행 = 오늘 + 백필 2콜
    assert fetched_bass[1] < fetched_bass[0]  # 두 번째가 과거 기준일

    # 두 번째 refresh(강제 stale): 새 날짜가 누적되고 과거는 보존
    with hs._lock:
        hs._state["checked_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

    def fake_fetch2(bass_dt):
        return {"2026-07-21": True}

    monkeypatch.setattr(hs, "_fetch_pages", fake_fetch2)
    r2 = hs.refresh_if_stale()
    assert r2["ok"] is True and r2["n_days"] == 4
    days = hs.get_open_days("2026-06-01", "2026-12-31")
    assert days["2026-06-02"] is True and days["2026-07-17"] is False

    # 디스크 저장 확인 (재시작 시 복구 경로)
    saved = json.loads((hs._STORE_DIR / hs._STORE_FILE).read_text(encoding="utf-8"))
    assert saved["days"]["2026-07-21"] is True


def test_refresh_fills_coverage_gap_after_outage(hs, monkeypatch):
    """서버 장기 중단 후: 마지막 커버~오늘 공백을 공백 시작일부터 재조회로 연결."""
    now = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    with hs._lock:
        # 과거는 충분(백필 불필요)하나 07-11~08-31 공백인 저장소
        hs._state["days"] = {"2026-05-01": True, "2026-07-10": True}
        hs._state["checked_at"] = (now - timedelta(days=50)).isoformat()
    fetched_bass = []

    def fake_fetch(bass_dt):
        fetched_bass.append(bass_dt)
        if bass_dt == "20260901":
            return {"2026-09-01": True}
        return {"2026-07-11": True, "2026-08-29": True}   # 공백 구간 재조회

    monkeypatch.setattr(hs, "_fetch_pages", fake_fetch)
    r = hs.refresh_if_stale(now=now)
    assert r["ok"] is True
    assert fetched_bass == ["20260901", "20260711"]       # 오늘 + 공백 시작일
    days = hs.get_open_days("2026-01-01", "2026-12-31")
    assert "2026-08-29" in days and "2026-05-01" in days


def test_refresh_skips_when_fresh(hs, monkeypatch):
    with hs._lock:
        hs._state["days"] = {"2026-07-17": False}
        hs._state["checked_at"] = datetime.now(timezone.utc).isoformat()

    def _boom(*a, **k):
        raise AssertionError("신선한데 재수집 호출")

    monkeypatch.setattr(hs, "_fetch_pages", _boom)
    r = hs.refresh_if_stale()
    assert r["ok"] is True and r["reason"] == "fresh"


def test_refresh_failure_keeps_store_and_reports(hs, monkeypatch):
    with hs._lock:
        hs._state["days"] = {"2026-07-17": False}
        hs._state["checked_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()

    def _fail(bass_dt):
        raise RuntimeError("KIS down")

    monkeypatch.setattr(hs, "_fetch_pages", _fail)
    r = hs.refresh_if_stale()
    assert r["ok"] is False and "KIS down" in r["reason"]
    # 기존 저장소 보존 + last_error 표면화
    assert hs.get_open_days("2026-07-01", "2026-07-31") == {"2026-07-17": False}
    assert "KIS down" in hs.get_status()["last_error"]


# ── calendar_cache 오버레이 ──────────────────────────────────────────────────


def test_apply_krx_overlay_kis_authority(monkeypatch):
    """KIS 휴장→세션 제거(07-17형), KIS 개장→기본 시각 추가, divergence 기록."""
    from app import calendar_cache as cc
    from app import krx_holiday_source as h

    monkeypatch.setattr(
        h, "get_open_days",
        lambda s, e: {"2026-07-16": True,    # 일치(개장) — 무변경
                      "2026-07-17": False,   # 라이브러리 개장 → KIS 휴장: 제거
                      "2026-07-18": True})   # 라이브러리 휴장 → KIS 개장: 추가
    monkeypatch.setattr(h, "get_status", lambda: {"checked_at": "2026-07-18T00:00:00"})

    data = {"market": "KR", "range": ["2026-07-01", "2026-07-31"],
            "sessions": {"2026-07-16": ["09:00", "15:30"],
                          "2026-07-17": ["09:00", "15:30"]}}
    out = cc._apply_krx_overlay(data)
    assert "2026-07-17" not in out["sessions"]
    assert out["sessions"]["2026-07-18"] == ["09:00", "15:30"]
    assert out["sessions"]["2026-07-16"] == ["09:00", "15:30"]
    ov = out["krx_holiday_overlay"]
    assert ov["n_kis_days"] == 3
    dates = {o["date"]: o for o in ov["overrides"]}
    assert set(dates) == {"2026-07-17", "2026-07-18"}
    assert dates["2026-07-17"] == {"date": "2026-07-17",
                                    "library_open": True, "kis_open": False}


def test_apply_krx_overlay_noop_without_kis_data(monkeypatch):
    """KIS 수집분 없음(키 미설정·수집 실패) → 세션 무변경 + 요약만."""
    from app import calendar_cache as cc
    from app import krx_holiday_source as h

    monkeypatch.setattr(h, "get_open_days", lambda s, e: {})
    monkeypatch.setattr(h, "get_status", lambda: {"checked_at": None})
    data = {"market": "KR", "range": ["2026-07-01", "2026-07-31"],
            "sessions": {"2026-07-17": ["09:00", "15:30"]}}
    out = cc._apply_krx_overlay(data)
    assert out["sessions"] == {"2026-07-17": ["09:00", "15:30"]}
    assert out["krx_holiday_overlay"]["overrides"] == []

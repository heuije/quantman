"""Q2+Q8 — KR/US 시장 세션 캘린더 일일 자동 갱신.

문제: quant_core/calendars/{kr,us}_sessions.json은 gen_market_sessions.py(개발
전용)로 빌드된 정적 파일. 임시공휴일(지방선거일, 임시휴장)이나 신규 공휴일이
exchange_calendars 라이브러리에 추가돼도 우리 시스템은 재빌드+재배포 전까지
인지 못함.

해법: 서버에서 매일 03:00 KST에 exchange_calendars 최신 데이터로 KR/US 세션을
재빌드 → 디스크 + 메모리 캐시. 로컬앱은 /calendars/{market}로 일일 1회 pull.

데이터 범위: 오늘-30일 ~ 오늘+2년. 2년 lookahead로 미국·한국 공시된 휴장일을
모두 커버 + 라이브러리 누락 시에도 만료 임박 인지 가능.

수정 로드맵 A (R1 캘린더 단일 오류원 교정): KR 빌드에 KIS 국내휴장일조회
(krx_holiday_source) 신호를 **권위로 덮어쓴다** — 거래소에 직접 물어 아는
브로커 신호가 정적 라이브러리보다 진실에 가깝다(2026-07-17 임시휴장 오인
실측). 라이브러리와 어긋난 날짜는 divergence로 경보(error 로그 + payload
`krx_holiday_overlay`)한다. 추가로 빌드 결과를 서버 자신의
market_calendar 사용자 캐시에도 기록해, 서버측 is_session_day/
prev_session_day(수집 게이트·preview)가 정적 번들이 아닌 같은 교정본을
읽게 한다 — 서버·로컬 전 계층이 하나의 교정된 달력을 공유.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("app.calendar_cache")

# Railway persistent volume (없으면 /tmp fallback — 재시작 시 초기 빌드로 복구)
_CACHE_DIR = Path("/data/calendars") if Path("/data").exists() else Path("/tmp/calendars")

# 시장별 exchange_calendars 코드 + 현지 tz
_MARKETS = {
    "KR": {"calendar": "XKRX", "tz_local": "Asia/Seoul"},
    "US": {"calendar": "XNYS", "tz_local": "America/New_York"},
}

# Lookback / Lookahead — 오늘 기준
_LOOKBACK_DAYS = 30
_LOOKAHEAD_DAYS = 730     # ~2년

_lock = threading.Lock()
_state: dict = {
    "KR": None,           # 빌드된 dict (None이면 미빌드 — get() 시 디스크 로드)
    "US": None,
    "built_at": None,     # ISO timestamp of last successful refresh
    "last_error": None,
}


def _build_one(market: str) -> dict:
    """exchange_calendars로 1개 시장의 세션 dict 빌드.

    반환 구조는 quant_core/calendars/{m}_sessions.json과 동일 형식:
      {"market", "calendar", "tz_local", "generated_at", "range", "sessions"}
    """
    import exchange_calendars as ec

    cfg = _MARKETS[market]
    tz_local = ZoneInfo(cfg["tz_local"])
    today = date.today()
    start = (today - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    end = (today + timedelta(days=_LOOKAHEAD_DAYS)).isoformat()

    cal = ec.get_calendar(cfg["calendar"], start=start, end=end)
    sessions: dict[str, list[str]] = {}
    for ts in cal.sessions:
        d = ts.date()
        # 현지 wall-clock HH:MM만 저장. DST는 클라이언트 런타임 zoneinfo가 처리.
        o_local = cal.session_open(ts).tz_convert(tz_local).strftime("%H:%M")
        c_local = cal.session_close(ts).tz_convert(tz_local).strftime("%H:%M")
        sessions[d.isoformat()] = [o_local, c_local]

    return {
        "market": market,
        "calendar": cfg["calendar"],
        "tz_local": cfg["tz_local"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "range": [start, end],
        "sessions": sessions,
    }


# KIS 신호로 세션을 *추가*할 때 쓰는 KR 정규장 기본 시각. 특수 시각일(수능일
# 등 지연 개장)은 라이브러리가 아는 날이라 이 경로에 오지 않는 게 정상 —
# 온다면 divergence 경보가 함께 남아 운영자가 검증한다.
_KR_DEFAULT_HOURS = ["09:00", "15:30"]


def _apply_krx_overlay(data: dict) -> dict:
    """KR 세션 dict에 KIS 개장일 신호를 권위로 적용 (로드맵 A 이중 신호).

    - KIS 휴장(opnd_yn=N)인데 라이브러리에 세션 있음 → 세션 제거 (07-17형)
    - KIS 개장(opnd_yn=Y)인데 라이브러리에 세션 없음 → 기본 시각으로 추가
    어긋난 날짜마다 error 경보를 남기고, 요약을 payload에 실어 로컬·admin이
    같은 사실을 본다. KIS 수집분이 없으면(키 미설정·수집 실패) 무변경.
    """
    from . import krx_holiday_source

    start, end = data["range"]
    kis_days = krx_holiday_source.get_open_days(start, end)
    overlay = {
        "checked_at": krx_holiday_source.get_status().get("checked_at"),
        "n_kis_days": len(kis_days),
        "overrides": [],
    }
    for d, is_open in sorted(kis_days.items()):
        lib_open = d in data["sessions"]
        if is_open == lib_open:
            continue
        overlay["overrides"].append(
            {"date": d, "library_open": lib_open, "kis_open": is_open})
        if is_open:
            data["sessions"][d] = list(_KR_DEFAULT_HOURS)
        else:
            data["sessions"].pop(d, None)
        log.error("[이중신호] KR 캘린더 어긋남 — %s: 라이브러리=%s KIS=%s → KIS 적용",
                  d, "개장" if lib_open else "휴장", "개장" if is_open else "휴장")
    data["krx_holiday_overlay"] = overlay
    return data


# 서버 자신도 교정본을 읽게 한다 — market_calendar의 사용자 캐시 경로를 서버
# 캐시 디렉토리로 지정(서버 프로세스 한정 · 이 모듈은 서버만 import). 파일명
# 규약(kr_sessions.json/us_sessions.json)이 동일해 별도 기록 없이 _write_disk
# 산출물이 곧 서버측 is_session_day/prev_session_day(수집 게이트·preview)의
# 소스가 된다. 로컬앱의 USER_CACHE_DIR(~/.quantman)은 무영향.
from quant_core import market_calendar as _mc

_mc.USER_CACHE_DIR = _CACHE_DIR


def _write_disk(market: str, data: dict) -> None:
    """디스크 캐시에 atomic write (tmp + replace)."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{market.lower()}_sessions.json"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    import os
    os.replace(tmp, path)


def _read_disk(market: str) -> dict | None:
    """디스크 캐시 로드. 없거나 손상이면 None."""
    path = _CACHE_DIR / f"{market.lower()}_sessions.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("디스크 캐시 손상 [%s]: %s", market, e)
        return None


def refresh() -> dict:
    """KR/US 둘 다 재빌드. 일일 cron + 기동 시 호출.

    반환: 시장별 {ok, n_sessions, range} 또는 {ok=False, error}.
    부분 실패 허용 — KR 실패해도 US는 갱신 시도.

    로드맵 A: KR 빌드 전 KIS 휴장일 신호를 일일 수집(refresh_if_stale — KIS
    권고 1일 1회 cadence)하고, 빌드에 권위로 오버레이한다. KIS 수집 실패는
    수집 모듈이 결과로 흡수(라이브러리 단독 빌드 계속 + 경고 로그) — 외부
    시스템 장애가 캘린더 서빙을 막지 않기 위한 명시적 한계 처리다.
    """
    from . import krx_holiday_source
    kis_result = krx_holiday_source.refresh_if_stale()

    results: dict = {"kis_holiday": kis_result}
    for market in ("KR", "US"):
        try:
            data = _build_one(market)
            if market == "KR":
                data = _apply_krx_overlay(data)
            _write_disk(market, data)
            with _lock:
                _state[market] = data
                _state["built_at"] = data["generated_at"]
                _state["last_error"] = None
            n = len(data["sessions"])
            log.info("[%s] 캘린더 재빌드: %d 세션, 범위 %s ~ %s",
                      market, n, data["range"][0], data["range"][1])
            results[market] = {"ok": True, "n_sessions": n,
                                "range": data["range"]}
            if market == "KR":
                results[market]["kis_overrides"] = len(
                    data.get("krx_holiday_overlay", {}).get("overrides", []))
        except Exception as e:
            log.exception("[%s] 캘린더 재빌드 실패: %s", market, e)
            with _lock:
                _state["last_error"] = f"{market}: {e}"
            results[market] = {"ok": False, "error": str(e)}
    # 서버측 market_calendar 소비자가 새 파일을 읽도록 메모이즈 무효화
    # (USER_CACHE_DIR가 _CACHE_DIR로 지정돼 있어 방금 쓴 파일이 곧 소스).
    _mc._load.cache_clear()
    return results


def get(market: str) -> dict | None:
    """캐시된 세션 데이터 반환.

    우선순위: 메모리 → 디스크 → None.
    None이면 호출자(라우터)가 503 응답.
    """
    m = market.upper()
    if m not in _MARKETS:
        return None
    with _lock:
        if _state[m] is not None:
            return _state[m]
    # 메모리 미스 → 디스크 로드 시도 (서버 재시작 직후 등)
    data = _read_disk(m)
    if data is not None:
        with _lock:
            _state[m] = data
    return data


def get_status() -> dict:
    """진단용. /health 또는 admin 대시보드에서 확인."""
    from . import krx_holiday_source
    with _lock:
        st = {
            "built_at": _state["built_at"],
            "last_error": _state["last_error"],
            "kr_loaded": _state["KR"] is not None,
            "us_loaded": _state["US"] is not None,
            "cache_dir": str(_CACHE_DIR),
        }
    st["kis_holiday"] = krx_holiday_source.get_status()
    return st

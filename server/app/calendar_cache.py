"""Q2+Q8 — KR/US 시장 세션 캘린더 일일 자동 갱신.

문제: quant_core/calendars/{kr,us}_sessions.json은 gen_market_sessions.py(개발
전용)로 빌드된 정적 파일. 임시공휴일(지방선거일, 임시휴장)이나 신규 공휴일이
exchange_calendars 라이브러리에 추가돼도 우리 시스템은 재빌드+재배포 전까지
인지 못함.

해법: 서버에서 매일 03:00 KST에 exchange_calendars 최신 데이터로 KR/US 세션을
재빌드 → 디스크 + 메모리 캐시. 로컬앱은 /calendars/{market}로 일일 1회 pull.

데이터 범위: 오늘-30일 ~ 오늘+2년. 2년 lookahead로 미국·한국 공시된 휴장일을
모두 커버 + 라이브러리 누락 시에도 만료 임박 인지 가능.
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
    """
    results: dict = {}
    for market in ("KR", "US"):
        try:
            data = _build_one(market)
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
        except Exception as e:
            log.exception("[%s] 캘린더 재빌드 실패: %s", market, e)
            with _lock:
                _state["last_error"] = f"{market}: {e}"
            results[market] = {"ok": False, "error": str(e)}
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
    with _lock:
        return {
            "built_at": _state["built_at"],
            "last_error": _state["last_error"],
            "kr_loaded": _state["KR"] is not None,
            "us_loaded": _state["US"] is not None,
            "cache_dir": str(_CACHE_DIR),
        }

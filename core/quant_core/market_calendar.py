"""시장 정규장 캘린더 — 런타임 (의존성: stdlib zoneinfo만).

미국(NYSE/NASDAQ/AMEX)·한국(KRX) 정규장 세션을 두 위치에서 우선순위로 로드:
  1순위: 사용자 캐시 `~/.quantman/calendars/{m}_sessions.json` — 서버에서 일일 pull
         된 최신 (Q2+Q8). 임시공휴일·신규 휴장 반영.
  2순위: 번들 `quant_core/calendars/{m}_sessions.json` — PyInstaller에 포함된
         정적 fallback. 사용자 캐시가 없는 첫 실행 시.

DST는 zoneinfo가 런타임에 처리. JSON은 현지 wall-clock HH:MM만 저장.

서버 preview와 로컬앱 스케줄러가 이 모듈을 공유해 동일 판정.

KST 환산 예:
  - 여름(EDT): 개장 09:30 ET → 22:30 KST, 마감 16:00 ET → 익일 05:00 KST
  - 겨울(EST): 개장 09:30 ET → 23:30 KST, 마감 16:00 ET → 익일 06:00 KST
  - 반일장   : 마감 13:00 ET → 02:00/03:00 KST
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

_BUNDLE_DIR = Path(__file__).parent / "calendars"
_BUNDLE_FILES = {
    "US": _BUNDLE_DIR / "us_sessions.json",
    "KR": _BUNDLE_DIR / "krx_sessions.json",
}

# Q2+Q8 — 사용자 캐시 (서버 pull 결과 저장). 환경변수로 override 가능 (테스트용).
import os as _os
_USER_CACHE_ENV = _os.environ.get("QUANTMAN_CALENDAR_DIR")
USER_CACHE_DIR = (Path(_USER_CACHE_ENV) if _USER_CACHE_ENV
                    else Path.home() / ".quantman" / "calendars")


class CalendarError(RuntimeError):
    """세션 데이터 누락·만료 등 캘린더 사용 불가 상태."""


def _parse(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    tz_local = ZoneInfo(data["tz_local"])
    return {
        "tz_local": tz_local,
        "sessions": data["sessions"],
        "sorted_days": sorted(data["sessions"].keys()),
        "range": data.get("range", []),
    }


@lru_cache(maxsize=4)
def _load(market: str) -> dict:
    """시장 세션 JSON을 로드(메모이즈). 사용자 캐시 우선, 번들 fallback.

    캐시 무효화는 calendar_sync가 _load.cache_clear()를 호출 (pull 직후).
    """
    if market not in _BUNDLE_FILES:
        raise CalendarError(f"지원하지 않는 시장: {market}")
    # 1순위: 사용자 캐시 (서버 pull)
    user_path = USER_CACHE_DIR / f"{market.lower()}_sessions.json"
    if user_path.exists():
        try:
            return _parse(user_path)
        except Exception:
            # 손상 시 번들로 fallback (조용히 무시하지 않고 명시적 폴백)
            import logging as _logging
            _logging.getLogger("quant_core.market_calendar").warning(
                "사용자 캘린더 캐시 손상 [%s] — 번들 fallback", user_path)
    # 2순위: 번들 (PyInstaller 포함)
    bundle_path = _BUNDLE_FILES[market]
    if not bundle_path.exists():
        raise CalendarError(
            f"세션 데이터 없음 (사용자 캐시·번들 모두 부재): {bundle_path}")
    return _parse(bundle_path)


def _to_kst(day: date, hhmm: str, tz_local: ZoneInfo) -> datetime:
    """현지 wall-clock 'HH:MM'을 해당 날짜의 현지 tz로 묶고 KST로 변환.

    datetime.combine + astimezone이 ET→KST의 날짜 넘김(마감이 익일 새벽)과
    DST를 모두 정확히 처리한다.
    """
    h, m = (int(x) for x in hhmm.split(":"))
    local_dt = datetime.combine(day, time(h, m), tz_local)
    return local_dt.astimezone(KST)


def session_kst(market: str, day: date) -> tuple[datetime, datetime] | None:
    """해당 ET 세션일의 (개장, 폐장) KST tz-aware. 휴장이면 None.

    `day`는 미국 현지(ET) 기준 날짜다. 반환되는 폐장 시각은 KST로 익일 새벽이
    될 수 있다(정규장이 자정을 넘김).
    """
    cal = _load(market)
    rec = cal["sessions"].get(day.isoformat())
    if rec is None:
        return None
    o = _to_kst(day, rec[0], cal["tz_local"])
    c = _to_kst(day, rec[1], cal["tz_local"])
    return o, c


def _now_kst(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(KST)
    if now.tzinfo is None:
        return now.replace(tzinfo=KST)
    return now.astimezone(KST)


def is_session_open(market: str, now: datetime | None = None) -> bool:
    """now(기본 현재, KST 가정) 시점에 해당 시장 정규장이 열려 있는가.

    now를 ET 날짜로 환산해 그 세션의 [개장, 폐장] KST 구간에 포함되는지 본다.
    정규장이 자정을 넘기는 경우(KST 새벽)도 ET 날짜가 같으므로 정확하다.
    """
    cal = _load(market)
    now_kst = _now_kst(now)
    et_day = now_kst.astimezone(cal["tz_local"]).date()
    sess = session_kst(market, et_day)
    if sess is None:
        return False
    return sess[0] <= now_kst <= sess[1]


def next_session_kst(market: str,
                     after: datetime | None = None
                     ) -> tuple[datetime, datetime] | None:
    """after(기본 현재, KST 가정) 이후 가장 빠른 세션의 (개장, 폐장) KST.

    개장 시각이 after보다 뒤인 첫 세션을 반환. 데이터가 만료(after가 마지막
    세션일을 지남)됐는데 못 찾으면 CalendarError — 조용히 멈추지 않도록.
    """
    cal = _load(market)
    after_kst = _now_kst(after)
    # ET 날짜 기준으로 후보 범위를 좁힌다(전일부터 — 자정 넘김 세션 포함).
    start_day = (after_kst.astimezone(cal["tz_local"]).date()).isoformat()
    found = None
    for d in cal["sorted_days"]:
        if d < start_day:
            continue
        o, c = session_kst(market, date.fromisoformat(d))
        if o > after_kst:
            found = (o, c)
            break
    if found is None:
        last = cal["sorted_days"][-1] if cal["sorted_days"] else "?"
        raise CalendarError(
            f"{market} 다음 세션을 찾지 못함 — 세션 데이터가 만료됐을 수 있음 "
            f"(마지막 {last}). gen_market_sessions.py로 재생성하세요.")
    return found


def coverage_range(market: str) -> tuple[str, str]:
    """세션 데이터가 커버하는 [시작, 끝] ISO 날짜."""
    cal = _load(market)
    days = cal["sorted_days"]
    return (days[0], days[-1]) if days else ("", "")


def check_fresh(market: str, today: date,
                 lookahead_days: int = 7) -> tuple[bool, str]:
    """Q2+Q8 — 캘린더가 today + lookahead_days 안의 세션을 가지는지 확인.

    AL-3 결정: 만료가 의심되어도 사이클은 차단하지 않는다 (KIS API가 휴장이면
    어차피 거부하므로 피해 없음. 반대로 정규장에 우리가 잘못 차단하면 기회손실).
    호출자는 결과 False 시 로그·진단에만 사용.

    반환: (fresh, message). fresh=False면 message에 사유.
    """
    try:
        cal = _load(market)
    except CalendarError as e:
        return False, f"캘린더 로드 실패: {e}"
    days = cal["sorted_days"]
    if not days:
        return False, "세션 데이터 비어 있음"
    last = days[-1]
    horizon = (today + timedelta(days=lookahead_days)).isoformat()
    if last < horizon:
        return False, (f"{market} 캘린더 만료 임박 — 마지막 {last}, "
                       f"오늘 + {lookahead_days}일({horizon}) 미충족. "
                       f"서버 일일 sync(/calendars/{market}) 또는 라이브러리 갱신 필요.")
    return True, ""


def is_session_day(market: str, day: date) -> bool:
    """해당 날짜가 정규장 거래일인가 (휴장이면 False).

    KRX cron 게이트: 평일이라도 한국 공휴일(설/추석/광복절 등)이면 False.
    'today is open'과 다름 — is_session_open은 현재 시각이 정규장 구간인지 본다.
    L-03 수정: cycle/intraday/settlement 진입 전에 호출해 휴장일 매도·발주 차단.
    """
    cal = _load(market)
    return day.isoformat() in cal["sessions"]

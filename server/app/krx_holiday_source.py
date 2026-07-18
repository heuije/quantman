"""KRX 개장일 브로커 신호 — KIS 국내휴장일조회(CTCA0903R) 일일 수집·누적.

문제(R1·수정 로드맵 A): 전 계층이 exchange_calendars 정적 라이브러리 하나로
KR 휴장일을 판정한다 — 라이브러리가 임시공휴일·임시휴장을 놓치면 서버 수집
게이트·preview·로컬 발주/신선도 게이트가 동시에 같은 방향으로 틀린다
(2026-07-17 실측: 실제 휴장일을 거래일로 오인).

해법: 거래소에 직접 물어 아는 브로커(KIS)의 휴장일 조회를 **독립 신호**로
매일 수집해 볼륨에 누적하고, calendar_cache가 KR 세션 빌드에 이 신호를
권위로 덮어쓴다(어긋나면 경보). 과거는 수집분이 영구 누적되므로 신선도
판정(직전 거래일)도 같은 신호를 상속한다.

API (docs/kis-api/endpoints/CTCA0903R_국내휴장일조회.md):
  GET {real}/uapi/domestic-stock/v1/quotations/chk-holiday  tr_id=CTCA0903R
  파라미터 BASS_DT(YYYYMMDD)·CTX_AREA_FK·CTX_AREA_NK, 연속조회 tr_cont.
  output[]: bass_dt·wday_dvsn_cd·bzdy_yn·tr_day_yn·opnd_yn·sttl_day_yn.
  개장일 여부 = opnd_yn. KIS 권고 "가급적 1일 1회 호출" — 본 모듈이 정확히
  그 cadence(일일 cron 1회, 첫 실행만 과거 백필 +1콜)로 호출한다.

자격증명: 회사(운영자) KIS 실전 앱키 — env QP_KIS_APPKEY/QP_KIS_APPSECRET.
유저 자격증명과 무관하다(유저 키는 로컬 PC 전용 원칙 그대로). 미설정이면
수집만 비활성(is_active=False)이고 캘린더는 라이브러리 단독으로 동작한다.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from .config import settings

log = logging.getLogger("app.krx_holiday")

_KST = ZoneInfo("Asia/Seoul")
_REAL_BASE = "https://openapi.koreainvestment.com:9443"
_PATH = "/uapi/domestic-stock/v1/quotations/chk-holiday"
_TR_ID = "CTCA0903R"

# calendar_cache와 같은 볼륨 디렉토리 규약 (Railway /data, 없으면 /tmp)
_STORE_DIR = Path("/data/calendars") if Path("/data").exists() else Path("/tmp/calendars")
_STORE_FILE = "krx_holidays.json"
_TOKEN_FILE = "kis_server_token.json"

# 하루 1회 cadence — 이 시간 안에 재호출 요청이 오면 skip (KIS "1일 1회" 권고).
_FRESH_HOURS = 20
# 첫 실행 백필 목표 — 신선도 게이트(직전 거래일)가 쓰는 과거 커버.
_BACKFILL_DAYS = 60
# 연속조회 안전 상한 (조회당). 실측 전 보수값 — 페이지당 수십 일이면 1년치도 수 페이지.
_MAX_PAGES = 12

_lock = threading.Lock()
_state: dict = {
    "days": None,          # {"YYYY-MM-DD": bool(개장일)} — None이면 미로드(디스크 lazy)
    "checked_at": None,    # 마지막 성공 수집 ISO timestamp
    "last_error": None,
}


def is_active() -> bool:
    """회사 KIS 앱키가 설정돼 있는가. 미설정이면 수집 전체 비활성."""
    return bool(settings.KIS_APPKEY and settings.KIS_APPSECRET)


# ── 토큰 ────────────────────────────────────────────────────────────────────


def _token() -> str:
    """서버용 KIS access token — 디스크 캐시(만료 30분 마진) 후 재발급.

    KIS는 토큰 재발급을 분당 1회로 제한하므로 재시작·재배포에도 재사용되게
    볼륨에 저장한다. 발급 실패는 예외 전파(호출자가 수집 실패로 기록).
    """
    path = _STORE_DIR / _TOKEN_FILE
    try:
        c = json.loads(path.read_text(encoding="utf-8"))
        if datetime.fromisoformat(c["expires_at"]) > datetime.now(timezone.utc) + timedelta(minutes=30):
            return c["access_token"]
    except Exception:
        pass  # 없음/손상 → 재발급
    r = requests.post(f"{_REAL_BASE}/oauth2/tokenP",
                      json={"grant_type": "client_credentials",
                            "appkey": settings.KIS_APPKEY,
                            "appsecret": settings.KIS_APPSECRET},
                      timeout=10)
    r.raise_for_status()
    d = r.json()
    tok = d["access_token"]
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "access_token": tok,
        "expires_at": (datetime.now(timezone.utc)
                       + timedelta(seconds=int(d.get("expires_in", 86400)))).isoformat(),
    }), encoding="utf-8")
    os.replace(tmp, path)
    return tok


# ── 조회 ────────────────────────────────────────────────────────────────────


def _fetch_pages(bass_dt: str) -> dict[str, bool]:
    """BASS_DT부터의 개장일 표를 연속조회로 전부 수집 → {iso날짜: 개장 여부}.

    응답 검증은 내용 기준: rt_cd=="0" + output 각 행에 bass_dt·opnd_yn 필수.
    형식이 다르면 예외(fail-loud) — 조용한 빈 결과로 캘린더를 오염시키지 않는다.
    """
    token = _token()
    days: dict[str, bool] = {}
    fk, nk, tr_cont = "", "", ""
    for _page in range(_MAX_PAGES):
        r = requests.get(
            f"{_REAL_BASE}{_PATH}",
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": settings.KIS_APPKEY,
                "appsecret": settings.KIS_APPSECRET,
                "tr_id": _TR_ID, "custtype": "P", "tr_cont": tr_cont,
            },
            params={"BASS_DT": bass_dt, "CTX_AREA_FK": fk, "CTX_AREA_NK": nk},
            timeout=10)
        r.raise_for_status()
        d = r.json()
        if d.get("rt_cd") != "0":
            raise RuntimeError(
                f"CTCA0903R rt_cd={d.get('rt_cd')} msg={str(d.get('msg1', '')).strip()}")
        rows = d.get("output") or []
        for row in rows:
            raw = row.get("bass_dt", "")
            opnd = row.get("opnd_yn", "")
            if len(raw) != 8 or opnd not in ("Y", "N"):
                raise RuntimeError(f"CTCA0903R 행 형식 이상: {row}")
            days[f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"] = opnd == "Y"
        # 연속조회 — 응답 헤더 tr_cont F/M=다음 있음, D/E=마지막.
        cont = r.headers.get("tr_cont", "")
        if cont not in ("F", "M"):
            break
        fk = d.get("ctx_area_fk", "")
        nk = d.get("ctx_area_nk", "")
        tr_cont = "N"
    if not days:
        raise RuntimeError("CTCA0903R 응답에 날짜 행이 없음")
    return days


# ── 저장소 ──────────────────────────────────────────────────────────────────


def _load_store() -> dict:
    """볼륨의 누적 저장소 로드 → 메모리. 없으면 빈 구조."""
    with _lock:
        if _state["days"] is not None:
            return {"days": dict(_state["days"]), "checked_at": _state["checked_at"]}
    path = _STORE_DIR / _STORE_FILE
    days: dict[str, bool] = {}
    checked_at = None
    if path.exists():
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            days = {k: bool(v) for k, v in (d.get("days") or {}).items()}
            checked_at = d.get("checked_at")
        except Exception as e:
            log.warning("휴장일 저장소 손상 — 새로 수집으로 재구축: %s", e)
    with _lock:
        _state["days"] = days
        _state["checked_at"] = checked_at
    return {"days": dict(days), "checked_at": checked_at}


def _save_store(days: dict[str, bool], checked_at: str) -> None:
    _STORE_DIR.mkdir(parents=True, exist_ok=True)
    path = _STORE_DIR / _STORE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "source": f"KIS {_TR_ID}", "checked_at": checked_at, "days": days,
    }, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    with _lock:
        _state["days"] = dict(days)
        _state["checked_at"] = checked_at
        _state["last_error"] = None


# ── 공개 API ────────────────────────────────────────────────────────────────


def refresh_if_stale(now: datetime | None = None) -> dict:
    """일일 수집 — 마지막 성공이 _FRESH_HOURS 이내면 skip.

    반환 {ok, reason|n_days, coverage}. 실패는 예외 대신 결과 dict로 돌려
    호출자(calendar_cache)가 라이브러리 단독 빌드를 계속하게 한다 — 외부
    시스템(KIS) 장애가 캘린더 서빙 자체를 막으면 안 되므로. 실패 사실은
    last_error·경고 로그로 표면화한다.
    """
    if not is_active():
        return {"ok": False, "reason": "credentials_missing"}
    now = now or datetime.now(timezone.utc)
    store = _load_store()
    if store["checked_at"]:
        try:
            age = now - datetime.fromisoformat(store["checked_at"])
            if age < timedelta(hours=_FRESH_HOURS):
                return {"ok": True, "reason": "fresh", "n_days": len(store["days"])}
        except Exception:
            pass  # checked_at 손상 → 재수집
    today_kst = now.astimezone(_KST).date()
    try:
        days = dict(store["days"])
        fetched = _fetch_pages(today_kst.strftime("%Y%m%d"))
        days.update(fetched)
        # 과거 커버 확보 — 신선도 게이트("직전 거래일" 판정)가 최근 과거를
        # 필요로 한다. ① 첫 실행(과거 전무·부족)이면 과거 기준일로 백필,
        # ② 서버 장기 중단으로 마지막 커버~오늘 사이가 비면 공백 시작일부터
        # 재조회해 이어붙인다(불연속 커버 방지). 각각 최대 1콜.
        backfill_from = today_kst - timedelta(days=_BACKFILL_DAYS)
        past = sorted(d for d in days if d < today_kst.isoformat())
        if not past or past[0] > backfill_from.isoformat():
            days.update(_fetch_pages(backfill_from.strftime("%Y%m%d")))
        else:
            last_covered = date.fromisoformat(past[-1])
            if last_covered < today_kst - timedelta(days=1):
                days.update(_fetch_pages(
                    (last_covered + timedelta(days=1)).strftime("%Y%m%d")))
        _save_store(days, now.isoformat())
        cov = (min(days), max(days)) if days else ("", "")
        log.info("KRX 휴장일 수집 완료 — %d일 커버 %s~%s (신규 %d)",
                 len(days), cov[0], cov[1], len(fetched))
        return {"ok": True, "n_days": len(days), "coverage": cov}
    except Exception as e:
        with _lock:
            _state["last_error"] = str(e)
        log.warning("KRX 휴장일 수집 실패 — 캘린더는 라이브러리 단독으로 빌드: %s", e)
        return {"ok": False, "reason": str(e)}


def get_open_days(start_iso: str, end_iso: str) -> dict[str, bool]:
    """[start, end] 구간의 KIS 개장일 신호. 수집분이 없으면 빈 dict."""
    store = _load_store()
    return {d: v for d, v in store["days"].items() if start_iso <= d <= end_iso}


def get_status() -> dict:
    """진단용 — /health 계열·admin에서 확인."""
    store = _load_store()
    days = store["days"]
    with _lock:
        err = _state["last_error"]
    return {
        "active": is_active(),
        "checked_at": store["checked_at"],
        "n_days": len(days),
        "coverage": [min(days), max(days)] if days else [],
        "last_error": err,
    }

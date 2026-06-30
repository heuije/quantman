"""KRX 정보데이터시스템 **공식 Open API** (data-dbg.krx.co.kr/svc/apis) 피드.

로그인벽 `data.krx.co.kr` MDC와 **별개** — `AUTH_KEY` 헤더 + `basDd`(하루 1콜=그날 전종목)·
이력 2010~. `KRX_API_KEY` 미설정 시 no-op(비활성·로그 침묵, flow_kr 패턴).

S1 = 시장지표 4종(매크로형 명명 시계열 — VIX·미국채처럼 하루 1값):
  코스피200변동성지수(V-KOSPI) · 옵션풋콜비율 · KRX채권지수 · 국고채3년 · 국고채10년
데이터포인트당 소스 1개 원칙(no-backup) — 이 지표들의 진실원천 = 공식 KRX API.

라이브 검증(2026-06-30): 전 서비스 2010 깊이·필드 확정. 옵션은 하루 ~19k행이라 timeout 90s.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

import pandas as pd
import requests

from ... import data_fetcher as _df

_BASE = "https://data-dbg.krx.co.kr/svc/apis"

# 서비스 경로 (라이브 검증된 slug)
_SVC_DRVPROD = "idx/drvprod_dd_trd"   # 파생상품지수 (V-KOSPI 포함)
_SVC_BOND_IDX = "idx/bon_dd_trd"      # 채권지수
_SVC_KTS = "bon/kts_bydd_trd"         # 국채전문유통시장 일별
_SVC_OPT = "drv/opt_bydd_trd"         # 옵션 일별 (P/C용·무거움)


def _key() -> str:
    return os.getenv("KRX_API_KEY", "").strip()


def is_active() -> bool:
    """AUTH_KEY 존재 여부 — 없으면 feed 비활성(헛호출·경고 스팸 방지)."""
    return bool(_key())


def _fetch_day(service: str, bas_dd: str, timeout: int = 30):
    """공식 KRX API 하루치 호출 → row dict 리스트.

    반환: list(성공·휴장 포함 — 휴장은 []), None(네트워크/HTTP 실패 — 호출자가 재시도).
    """
    key = _key()
    if not key:
        return None
    try:
        r = requests.get(f"{_BASE}/{service}", headers={"AUTH_KEY": key},
                         params={"basDd": bas_dd}, timeout=timeout)
    except Exception:
        return None
    if r.status_code != 200 or "json" not in r.headers.get("content-type", "").lower():
        return None
    j = r.json()
    blk = next((j[k] for k in j if isinstance(j[k], list)), None)
    return blk if blk is not None else []


def _f(x):
    """문자열 숫자 → float. 빈값/'-' → None."""
    if x is None:
        return None
    s = str(x).strip().replace(",", "")
    if s in ("", "-", "nan", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ── 추출기 (순수 함수 — 네트워크 무관·단위테스트 대상) ─────────────────────────

def extract_vkospi(rows: list[dict]):
    """파생상품지수에서 코스피200 변동성지수(V-KOSPI) 종가."""
    for r in rows:
        if r.get("IDX_NM") == "코스피 200 변동성지수":
            return _f(r.get("CLSPRC_IDX"))
    return None


def extract_bond_index(rows: list[dict]):
    """채권지수에서 'KRX 채권지수' 총수익지수."""
    for r in rows:
        if r.get("BND_IDX_GRP_NM") == "KRX 채권지수":
            return _f(r.get("TOT_EARNG_IDX"))
    return None


def extract_ktb_yield(rows: list[dict], maturity: str):
    """국채전문에서 명목 국고채 지표물(benchmark)의 만기별 종가수익률(CLSPRC_YD).

    BND_EXP_TP_NM=만기년수('3','10'…)·GOVBND_ISU_TP_NM='지표'(벤치마크)·
    명목채(ISU_NM '국고…', 물가연동·원금분리 제외).
    """
    for r in rows:
        if (r.get("BND_EXP_TP_NM") == maturity
                and r.get("GOVBND_ISU_TP_NM") == "지표"
                and str(r.get("ISU_NM", "")).startswith("국고")
                and "원금" not in str(r.get("ISU_NM", ""))):
            return _f(r.get("CLSPRC_YD"))
    return None


def extract_putcall(rows: list[dict]):
    """옵션 일별에서 코스피200 옵션(미니 제외) 풋/콜 **거래량** 비율 = PUT_vol / CALL_vol.

    표준 시장심리 P/C(거래량 기준). 코스피200 정규+위클리 포함, 미니·코스닥150 제외.
    """
    call_v = put_v = 0
    for r in rows:
        prod = str(r.get("PROD_NM", ""))
        if not prod.startswith("코스피200") or "미니" in prod:
            continue
        v = _f(r.get("ACC_TRDVOL")) or 0.0
        rt = r.get("RGHT_TP_NM")
        if rt == "CALL":
            call_v += v
        elif rt == "PUT":
            put_v += v
    if call_v <= 0:
        return None
    return round(put_v / call_v, 4)


# ── 시리즈 정의: (시리즈명, 서비스, 추출기) ────────────────────────────────────

# 가벼운 지표(서비스 3종·각 수~수백행) — 한 백필 청크에서 넓은 윈도우 가능
_LIGHT_SERIES = [
    ("코스피200변동성지수", _SVC_DRVPROD, extract_vkospi),
    ("KRX채권지수", _SVC_BOND_IDX, extract_bond_index),
    ("국고채3년", _SVC_KTS, lambda rows: extract_ktb_yield(rows, "3")),
    ("국고채10년", _SVC_KTS, lambda rows: extract_ktb_yield(rows, "10")),
]
# 무거운 지표(옵션 하루 ~19k행·timeout 90s) — 좁은 윈도우로 분리
_PUTCALL_SERIES = ("옵션풋콜비율", _SVC_OPT, extract_putcall)

LIGHT_SYMBOLS = [name for name, _, _ in _LIGHT_SERIES]
PUTCALL_SYMBOL = _PUTCALL_SERIES[0]
ALL_SYMBOLS = LIGHT_SYMBOLS + [PUTCALL_SYMBOL]


def _weekdays(sdate: str, edate: str):
    """[sdate, edate] 평일(월~금) 'YYYYMMDD' 역순 — 휴장은 API가 []."""
    d0 = datetime.strptime(sdate, "%Y%m%d").date()
    d1 = datetime.strptime(edate, "%Y%m%d").date()
    d = d1
    while d >= d0:
        if d.weekday() < 5:
            yield d.strftime("%Y%m%d")
        d -= timedelta(days=1)


def _save_series(name: str, points: dict) -> int:
    """{날짜str: 값} → 명명 parquet(OHLCV-format·매크로 패턴)에 merge-save."""
    points = {k: v for k, v in points.items() if v is not None}
    if not points:
        return 0
    idx = pd.to_datetime(list(points.keys()), format="%Y%m%d")
    val = pd.Series(list(points.values()), index=idx, dtype="float64")
    df = pd.DataFrame({"Open": val, "High": val, "Low": val, "Close": val, "Volume": 0.0})
    df.index = df.index.tz_localize(None)
    merged = _df._merge(_df._load_existing(name), df)
    _df._save(name, merged)
    return len(points)


def fetch_market_indicators(sdate: str, edate: str, fetch=_fetch_day) -> dict:
    """가벼운 시장지표 3서비스(V-KOSPI·채권지수·국고채)를 [sdate,edate] 평일 수집·merge-save.

    하루당 서비스별 1콜(중복 제거)·추출기로 각 시리즈 값 산출. None=네트워크 실패(그날 skip).
    fetch 주입 가능(테스트). 반환 {"ok", "days", "saved": {시리즈: n}}.
    """
    if not is_active():
        return {"inactive": True}
    services = {svc for _, svc, _ in _LIGHT_SERIES}
    points = {name: {} for name, _, _ in _LIGHT_SERIES}
    days = fail = 0
    for bd in _weekdays(sdate, edate):
        rows_by_svc = {}
        day_failed = False
        for svc in services:
            rows = fetch(svc, bd)
            if rows is None:
                day_failed = True
                break
            rows_by_svc[svc] = rows
        if day_failed:
            fail += 1
            continue
        days += 1
        for name, svc, extract in _LIGHT_SERIES:
            v = extract(rows_by_svc[svc])
            if v is not None:
                points[name][bd] = v
    saved = {name: _save_series(name, pts) for name, pts in points.items()}
    if any(saved.values()):
        _df.mark_data_dirty()
    return {"ok": fail == 0, "days": days, "fail": fail, "saved": saved}


def fetch_putcall(sdate: str, edate: str, fetch=_fetch_day) -> dict:
    """옵션 풋콜비율(무거움·timeout 90s)을 [sdate,edate] 평일 수집·merge-save."""
    if not is_active():
        return {"inactive": True}
    name, svc, extract = _PUTCALL_SERIES
    points = {}
    days = fail = 0
    for bd in _weekdays(sdate, edate):
        rows = fetch(svc, bd, 90) if fetch is _fetch_day else fetch(svc, bd)
        if rows is None:
            fail += 1
            continue
        days += 1
        v = extract(rows)
        if v is not None:
            points[bd] = v
    n = _save_series(name, points)
    if n:
        _df.mark_data_dirty()
    return {"ok": fail == 0, "days": days, "fail": fail, "saved": {name: n}}

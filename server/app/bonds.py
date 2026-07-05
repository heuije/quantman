"""국가별 국채 금리(수익률 곡선) — 미국·일본·유럽·한국·중국, 단기~장기 전 만기.

소스(모두 무료·키 불필요, 2026-07-05 실측 검증):
- 미국: FRED fredgraph.csv 멀티시리즈(DGS*) — 일별 풀 커브(1M~30Y).
  ⚠ FRED는 requests **기본 UA**로만 응답 — 브라우저 UA를 붙이면 차단(타임아웃/RST).
- 일본: 재무성(MOF) jgbcme_all.csv — 일별 1Y~40Y (1974~).
- 유럽: ECB Data Portal 유로존 AAA 스팟커브(SR_*) — 일별, 만기 '+'로 1콜.
- 한국: FRED 월간(10Y IRLTLT01KRM156N · 3M IR3TIB01KRM156N).
- 중국: FRED 월간 3M(IR3TTS01CNM156N)만 — **장기물은 FRED 미제공**(무료 신뢰 소스 부재).

캐싱: 국가별 lru_cache(일자 키). 시계열은 최근 10년(payload 억제 — 프론트 기간 컨트롤 범위),
엑셀 추출은 전체 기간.
"""
from __future__ import annotations

import csv
import io
import logging
from datetime import date
from functools import lru_cache

import requests

_log = logging.getLogger("app.bonds")
_YEARS = 10          # 화면 시계열 범위(엑셀은 전체)

# 국가 메타 — (표시명, 빈도 라벨)
COUNTRIES = {
    "US": ("미국", "일별"), "JP": ("일본", "일별"), "EU": ("유로존 AAA 국채(ECB 산출)", "일별"),
    "KR": ("한국", "월별"), "CN": ("중국", "월별"),
}

_US_SERIES = [("1M", "DGS1MO"), ("3M", "DGS3MO"), ("6M", "DGS6MO"), ("1Y", "DGS1"),
              ("2Y", "DGS2"), ("3Y", "DGS3"), ("5Y", "DGS5"), ("7Y", "DGS7"),
              ("10Y", "DGS10"), ("20Y", "DGS20"), ("30Y", "DGS30")]
_JP_MATS = ["1Y", "2Y", "3Y", "4Y", "5Y", "6Y", "7Y", "8Y", "9Y", "10Y",
            "15Y", "20Y", "25Y", "30Y", "40Y"]
_EU_SERIES = [("3M", "SR_3M"), ("6M", "SR_6M"), ("1Y", "SR_1Y"), ("2Y", "SR_2Y"),
              ("3Y", "SR_3Y"), ("5Y", "SR_5Y"), ("7Y", "SR_7Y"), ("10Y", "SR_10Y"),
              ("20Y", "SR_20Y"), ("30Y", "SR_30Y")]
_KR_SERIES = [("3M", "IR3TIB01KRM156N"), ("10Y", "IRLTLT01KRM156N")]
_CN_SERIES = [("3M", "IR3TTS01CNM156N")]


def _num(s):
    try:
        v = float(str(s).strip())
        return round(v, 4)
    except (TypeError, ValueError):
        return None


def _fred_multi(series: list[tuple[str, str]], start: str) -> dict[str, dict[str, float]]:
    """fredgraph.csv 멀티시리즈 1콜 → {date: {만기: 값}}. 기본 UA 필수(브라우저 UA 차단)."""
    ids = ",".join(sid for _, sid in series)
    r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={ids}&cosd={start}",
                     timeout=30)
    r.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(r.text)))
    sid2mat = {sid: m for m, sid in series}
    out: dict[str, dict[str, float]] = {}
    for row in rows:
        d = row.get("observation_date") or row.get("DATE") or ""
        if not d or d < start:          # 멀티시리즈에선 cosd가 무시됨 — 서버에서 컷
            continue
        pt = {}
        for sid, mat in sid2mat.items():
            v = _num(row.get(sid))
            if v is not None:
                pt[mat] = v
        if pt:
            out[d] = pt
    return out


def _jp_curve(start: str) -> dict[str, dict[str, float]]:
    """MOF jgbcme_all.csv — 헤더 2행(제목/컬럼), 날짜 'YYYY/M/D'."""
    r = requests.get("https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
                     "historical/jgbcme_all.csv",
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=40)
    r.raise_for_status()
    lines = r.text.strip().splitlines()
    rd = csv.reader(lines[1:])            # 0행=제목, 1행=컬럼 헤더
    header = next(rd)
    idx = {m: header.index(m) for m in _JP_MATS if m in header}
    out: dict[str, dict[str, float]] = {}
    for row in rd:
        if not row or "/" not in (row[0] or ""):
            continue
        try:
            y, m, d = row[0].split("/")
            iso = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except ValueError:
            continue
        if iso < start:
            continue
        pt = {mat: v for mat, i in idx.items() if (v := _num(row[i])) is not None}
        if pt:
            out[iso] = pt
    return out


def _eu_curve(start: str) -> dict[str, dict[str, float]]:
    """ECB 유로존 AAA 스팟커브 — 전 만기 '+' 결합 1콜(SDMX csvdata)."""
    keys = "+".join(sid for _, sid in _EU_SERIES)
    r = requests.get(f"https://data-api.ecb.europa.eu/service/data/YC/"
                     f"B.U2.EUR.4F.G_N_A.SV_C_YM.{keys}",
                     params={"format": "csvdata", "startPeriod": start}, timeout=40)
    r.raise_for_status()
    sid2mat = {sid: m for m, sid in _EU_SERIES}
    out: dict[str, dict[str, float]] = {}
    for row in csv.DictReader(io.StringIO(r.text)):
        mat = sid2mat.get((row.get("DATA_TYPE_FM") or "").strip())
        d = (row.get("TIME_PERIOD") or "").strip()
        v = _num(row.get("OBS_VALUE"))
        if mat and d and v is not None:
            out.setdefault(d, {})[mat] = v
    return out


def _fetch_country(cc: str, start: str) -> tuple[list[str], dict[str, dict[str, float]]]:
    """국가 코드 → (만기 순서, {date: {만기: 값}})."""
    if cc == "US":
        return [m for m, _ in _US_SERIES], _fred_multi(_US_SERIES, start)
    if cc == "JP":
        return list(_JP_MATS), _jp_curve(start)
    if cc == "EU":
        return [m for m, _ in _EU_SERIES], _eu_curve(start)
    if cc == "KR":
        return [m for m, _ in _KR_SERIES], _fred_multi(_KR_SERIES, start)
    if cc == "CN":
        return [m for m, _ in _CN_SERIES], _fred_multi(_CN_SERIES, start)
    return [], {}


@lru_cache(maxsize=16)
def _country(cc: str, _day: str, years: int = _YEARS) -> dict:
    start = f"{date.today().year - years}-01-01"
    mats, data = _fetch_country(cc, start)
    dates = sorted(data)
    series = [{"date": d, **data[d]} for d in dates]
    # 최신 커브 + 직전 대비 변동(bp)
    latest = {}
    if dates:
        last_d = dates[-1]
        prev_d = dates[-2] if len(dates) >= 2 else last_d
        for m in mats:
            cur = data[last_d].get(m)
            prv = data[prev_d].get(m)
            latest[m] = {"yield": cur,
                         "chg_bp": round((cur - prv) * 100, 1) if (cur is not None and prv is not None) else None}
    name, freq = COUNTRIES[cc]
    return {"country": cc, "name": name, "freq": freq, "maturities": mats,
            "series": series, "latest": latest, "asof": dates[-1] if dates else None}


def country(cc: str) -> dict:
    cc = cc.upper()
    if cc not in COUNTRIES:
        return {"country": cc, "name": cc, "freq": "", "maturities": [], "series": [],
                "latest": {}, "asof": None}
    try:
        return _country(cc, date.today().isoformat())
    except Exception as e:
        _log.warning("국채금리 fetch 실패 %s: %s", cc, e)
        return {"country": cc, "name": COUNTRIES[cc][0], "freq": COUNTRIES[cc][1],
                "maturities": [], "series": [], "latest": {}, "asof": None}


def to_xlsx() -> bytes:
    """전체 국가·전체 기간 국채금리 .xlsx — 국가별 시트(날짜 × 만기)."""
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for cc in COUNTRIES:
        try:
            mats, data = _fetch_country(cc, "1900-01-01")     # 전체 기간
        except Exception as e:
            _log.warning("국채 엑셀 %s 실패: %s", cc, e)
            continue
        ws = wb.create_sheet(f"{COUNTRIES[cc][0]}({cc})"[:28])
        ws.append(["Date", *mats])
        for d in sorted(data):
            ws.append([d, *[data[d].get(m) for m in mats]])
        ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

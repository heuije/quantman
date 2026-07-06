"""macro.bonds (글로벌) 피드 — 국가별 국채 수익률곡선(미·일·유·한·중, 단기~장기 전 만기).

소스(모두 무료·키 불필요, 2026-07-05 실측 검증):
- 미국: FRED fredgraph.csv 멀티시리즈(DGS*) — 일별 풀 커브(1M~30Y).
  ⚠ FRED는 requests **기본 UA**로만 응답 — 브라우저 UA를 붙이면 차단(타임아웃/RST).
- 일본: 재무성(MOF) jgbcme_all.csv — 일별 1Y~40Y (1974~).
- 유럽: ECB Data Portal 유로존 AAA 스팟커브(SR_*) — 일별, 만기 '+'로 1콜.
- 한국: FRED 월간(10Y IRLTLT01KRM156N · 3M IR3TIB01KRM156N).
- 중국: FRED 월간 3M(IR3TTS01CNM156N)만 — **장기물은 FRED 미제공**(무료 신뢰 소스 부재).

**왜 엔진에 두나(서빙 일원화 Phase 6a):** 서버 `app/bonds.py`가 요청당 라이브 크롤(lru만·재배포
증발)하던 것을 데이터엔진이 하루 1회 수집해 볼륨(`bonds/{cc}.parquet`)에 저장 → 서버는 볼륨에서
서빙(재배포 warmup·유저간 재크롤 제거). fetch 로직은 이 피드가 단독 소유(중복 제거) — 서버는
표시용 조립(최신 커브·bp 변동·기간 슬라이스)만 한다.

저장: 국가별 parquet(`bonds/{cc}.parquet`·index=거래일(datetime)·컬럼=만기 라벨·값=금리%). 전체 기간.
실패는 빈결과(가짜 0 금지). 라이브 폴백은 서버 bonds.py가 이 피드 fetch를 재사용(중복 없음).
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date

import pandas as pd

from ..manifest import default_manifest_path
from ...parquet_io import read_parquet_safe, write_parquet_atomic

# 국가 메타 — (표시명, 빈도 라벨)
COUNTRIES: dict[str, tuple[str, str]] = {
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

_MATURITIES: dict[str, list[str]] = {
    "US": [m for m, _ in _US_SERIES], "JP": list(_JP_MATS), "EU": [m for m, _ in _EU_SERIES],
    "KR": [m for m, _ in _KR_SERIES], "CN": [m for m, _ in _CN_SERIES],
}


def maturities(cc: str) -> list[str]:
    return _MATURITIES.get(cc.upper(), [])


# ── 만기별 매크로 심볼 발행 (챗/백테스트가 다른 매크로처럼 참조·표시=챗사용가능 Phase 6a) ──
# 만기 라벨 → 한글 접미(기존 명명 규약 계승: '미국채2년'·'미국채3개월').
_TENOR_KR = {"1M": "1개월", "3M": "3개월", "6M": "6개월", "1Y": "1년", "2Y": "2년",
             "3Y": "3년", "4Y": "4년", "5Y": "5년", "6Y": "6년", "7Y": "7년",
             "8Y": "8년", "9Y": "9년", "10Y": "10년", "15Y": "15년", "20Y": "20년",
             "25Y": "25년", "30Y": "30년", "40Y": "40년"}
# 국가 → 매크로 심볼 접두. **KR 제외** — KRX 국고채3/10년(일별·공식)이 이미 매크로 SSOT라
# 국채 피드 KR(FRED 월간)은 표시 커브 전용(중복·저품질 회피).
_MACRO_PREFIX = {"US": "미국채", "JP": "일본국채", "EU": "유로존국채", "CN": "중국국채"}


def _macro_name(cc: str, tenor: str):
    pre, suf = _MACRO_PREFIX.get(cc.upper()), _TENOR_KR.get(tenor)
    return f"{pre}{suf}" if (pre and suf) else None


def macro_symbols() -> list[str]:
    """국채 피드가 매크로 심볼로 발행하는 만기별 수익률 이름(US/JP/EU/CN 전만기·KR 제외).
    data_fetcher.MACRO_BONDS_SYMBOLS의 진실원천 — 드리프트 가드가 정합을 잠근다."""
    return [n for cc in _MACRO_PREFIX for t in _MATURITIES.get(cc, [])
            if (n := _macro_name(cc, t))]


def _macro_path(name: str):
    return default_manifest_path().parent / f"{name.replace('/', '_')}.parquet"


def _num(s):
    try:
        v = float(str(s).strip())
        return round(v, 4)
    except (TypeError, ValueError):
        return None


def _fred_multi(series: list[tuple[str, str]], start: str) -> dict[str, dict[str, float]]:
    """fredgraph.csv 멀티시리즈 1콜 → {date: {만기: 값}}. 기본 UA 필수(브라우저 UA 차단)."""
    import requests

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
    import requests

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
    import requests

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


def _fetch_dict(cc: str, start: str) -> dict[str, dict[str, float]]:
    """국가 코드 → {date: {만기: 값}} (네트워크). 소스별 분기."""
    if cc == "US":
        return _fred_multi(_US_SERIES, start)
    if cc == "JP":
        return _jp_curve(start)
    if cc == "EU":
        return _eu_curve(start)
    if cc == "KR":
        return _fred_multi(_KR_SERIES, start)
    if cc == "CN":
        return _fred_multi(_CN_SERIES, start)
    return {}


def fetch_curve(cc: str, start: str = "1900-01-01") -> pd.DataFrame:
    """국가 커브 fetch → DataFrame(index=거래일 datetime, 컬럼=만기 순서). 실패·무데이터면 빈 DF.

    서버 라이브 폴백도 이 함수를 재사용(fetch 로직 단일 소유). start로 기간 컷.
    """
    cc = cc.upper()
    try:
        data = _fetch_dict(cc, start)
    except Exception:                            # noqa: BLE001 — transient(가짜 적재 금지)
        return pd.DataFrame()
    if not data:
        return pd.DataFrame()
    mats = _MATURITIES.get(cc, [])
    idx = pd.to_datetime(sorted(data))
    frame = pd.DataFrame([{m: data[d.strftime("%Y-%m-%d")].get(m) for m in mats}
                          for d in idx], index=idx)
    frame.index.name = "date"
    return frame.dropna(how="all")


def _path(cc: str):
    return default_manifest_path().parent / "bonds" / f"{cc.upper()}.parquet"


def load(cc: str) -> pd.DataFrame | None:
    """저장된 국가 커브(없으면 None). 손상 파일은 read_parquet_safe가 격리."""
    p = _path(cc)
    return read_parquet_safe(p) if p.exists() else None


def _write_tenor_series(cc: str, df: pd.DataFrame) -> None:
    """커브의 각 만기 컬럼을 매크로 명명 parquet(Close 단일 컬럼)로 발행 — 챗/백테스트가
    다른 매크로 시계열처럼 로드(load_dataset_for). KR은 _macro_name이 None → 자연 제외."""
    for tenor in df.columns:
        name = _macro_name(cc, tenor)
        if not name:
            continue
        s = df[tenor].dropna()
        if not s.empty:
            write_parquet_atomic(s.to_frame("Close"), _macro_path(name))


def refresh(cc: str) -> pd.DataFrame:
    """전체 기간 fetch 후 커브 스토어 + 만기별 매크로 심볼 발행(스냅샷). 빈결과면 미기록(기존 보존)."""
    df = fetch_curve(cc)
    if df is not None and not df.empty:
        write_parquet_atomic(df, _path(cc))      # 커브 스토어(표시 서빙)
        _write_tenor_series(cc, df)              # 만기별 매크로 심볼(챗/백테스트)
    return df


def refresh_all() -> dict[str, int]:
    """전 국가 수집(일일 cron). {cc: 행수}. 실패 국가는 0(기존 저장본 보존)."""
    out = {}
    for cc in COUNTRIES:
        df = refresh(cc)
        out[cc] = 0 if df is None else len(df)
    return out


def get(cc: str, fresh_days: int = 1) -> pd.DataFrame | None:
    """load-or-fetch — 신선한 저장본이 있으면 반환, 없거나 오래되면 fetch→저장→반환.

    국채는 일별 갱신이라 1일 신선도. fetch 실패면 기존(stale) 저장본으로 graceful(빈 결과 방지).
    서버 서빙이 볼륨 우선으로 호출(볼륨 없을 때만 라이브).
    """
    p = _path(cc)
    if p.exists() and (time.time() - p.stat().st_mtime) < fresh_days * 86400:
        return load(cc)
    df = refresh(cc)
    if df is not None and not df.empty:
        return df
    return load(cc)

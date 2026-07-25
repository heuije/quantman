"""macro.bonds (글로벌) 피드 — 국가별 국채 수익률곡선(미·일·유·한·중, 단기~장기 전 만기).

소스(모두 무료·키 불필요, 2026-07-05 최초 검증 / 2026-07-24 재실측·JP·KR 교정):
- 미국: FRED fredgraph.csv 멀티시리즈(DGS*) — 일별 풀 커브(1M~30Y).
  ⚠ FRED는 requests **기본 UA**로만 응답 — 브라우저 UA를 붙이면 차단(타임아웃/RST).
- 일본: 재무성(MOF) **누적 jgbcme_all.csv + 당월 jgbcme.csv 병합** — 일별 1Y~40Y (1974~).
  ⚠ 누적 파일은 **전월까지만** 담는다 — 당월 파일을 병합 안 하면 매월 최대 한 달 지연
  (2026-07-24 실측: 누적 최신 06-30 vs 당월 파일 07-23 = 24일 지연).
- 유럽: ECB Data Portal 유로존 AAA 스팟커브(SR_*) — 일별, 만기 '+'로 1콜.
- 한국: **KRX 공식 국고채 지표물(3년·10년) 일별 재사용** — krx_openapi 피드가 이미 매일 수집하는
  매크로 시계열(`국고채3년`·`국고채10년`)을 읽는다(네트워크 0·키 0·수집 중복 0).
  종전 FRED 월간(IRLTLT01KRM156N·IR3TIB01KRM156N)은 **월 1회 갱신**이라 표시 품질이 낮았다.
- 중국: **ChinaBond(中债) 국채수익률곡선** 연도별 XLSX — 일별 1M~50Y (2006~·우리 floor 2010).
  종전 FRED 월간 3M(IR3TTS01CNM156N)은 **2023-11-01 이후 갱신 중단**(2026-07-24 실측: HTTP 200에
  2년 8개월 묵은 값 — 빈 응답이 아니라 옛 값이라 예외·행수로는 안 잡혔다). FRED에 살아있는 중국
  *국채* 시리즈는 존재하지 않음을 전수 확인(IRLTLT01CN*·INTGSBCN*·IR10TCN 등 전부 404).
  ChinaBond는 PBOC 지정 국채 등기결산기관(CCDC) 공식 커브로 무키·무가입이며, 같은 날짜 값이
  독립 경로(HTML historyQuery)와 소수 4자리까지 일치·OECD 월평균과도 0.03%p 내 부합(교차검증).
  ⚠ 호스팅이 중국 본토 단독 IP라 해외 접근성이 관건 — **Railway 컨테이너에서 200/2.4s 실측 확인**.

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

# 국가 메타 — (표시명, 빈도 라벨). 빈도는 화면에 그대로 노출되므로 실제 갱신 주기와 일치해야 한다.
COUNTRIES: dict[str, tuple[str, str]] = {
    "US": ("미국", "일별"), "JP": ("일본", "일별"), "EU": ("유로존 AAA 국채(ECB 산출)", "일별"),
    "KR": ("한국(KRX 국고채 지표물)", "일별"), "CN": ("중국(ChinaBond 국채커브)", "일별"),
}

_US_SERIES = [("1M", "DGS1MO"), ("3M", "DGS3MO"), ("6M", "DGS6MO"), ("1Y", "DGS1"),
              ("2Y", "DGS2"), ("3Y", "DGS3"), ("5Y", "DGS5"), ("7Y", "DGS7"),
              ("10Y", "DGS10"), ("20Y", "DGS20"), ("30Y", "DGS30")]
_JP_MATS = ["1Y", "2Y", "3Y", "4Y", "5Y", "6Y", "7Y", "8Y", "9Y", "10Y",
            "15Y", "20Y", "25Y", "30Y", "40Y"]
_EU_SERIES = [("3M", "SR_3M"), ("6M", "SR_6M"), ("1Y", "SR_1Y"), ("2Y", "SR_2Y"),
              ("3Y", "SR_3Y"), ("5Y", "SR_5Y"), ("7Y", "SR_7Y"), ("10Y", "SR_10Y"),
              ("20Y", "SR_20Y"), ("30Y", "SR_30Y")]
# 한국 — KRX 공식 국고채 지표물(krx_openapi가 매일 수집하는 매크로 시계열) 재사용.
# (만기 라벨, 매크로 심볼명). 새 심볼을 만들지 않는다 = ALL_SYMBOLS 불변(자동매매 데이터셋 무영향).
# 만기 2종뿐인 건 krx_openapi가 현재 그 둘만 발행하기 때문 — 커브를 넓히려면 그 피드에 만기를
# 추가해야 하고 그건 매크로 심볼(=ALL_SYMBOLS) 확장이라 별도 판단 사안이다.
_KR_MACRO = [("3Y", "국고채3년"), ("10Y", "국고채10년")]
# 중국 — ChinaBond가 주는 17종 중 표준 만기만 채택(0d·2m·9m 비표준 제외). 라벨은 소스의
# 'Instructions for Standard Terms'(예 '3m'·'10y')를 대문자화한 것과 정확히 일치한다.
_CN_MATS = ["1M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y",
            "30Y", "40Y", "50Y"]
_CN_FIRST_YEAR = 2010          # 정책 floor(CORE_FLOOR) — 소스는 2006부터 제공

_MATURITIES: dict[str, list[str]] = {
    "US": [m for m, _ in _US_SERIES], "JP": list(_JP_MATS), "EU": [m for m, _ in _EU_SERIES],
    "KR": [m for m, _ in _KR_MACRO], "CN": list(_CN_MATS),
}

# 국가별 기대 갱신 주기(일) — 초과 시 stale로 판정해 표면화한다. 중국 시리즈가 2년 반 넘게
# 조용히 멈춰 있었는데 아무도 몰랐던 부류(HTTP 200 + 옛 데이터 = 빈 결과 검사로는 못 잡음)를
# 닫는다. 일별 소스는 연휴·주말을 감안해 넉넉히, 월별은 발표 지연을 감안한다.
_STALE_AFTER_DAYS: dict[str, int] = {"US": 7, "JP": 7, "EU": 7, "KR": 7, "CN": 7}


def maturities(cc: str) -> list[str]:
    return _MATURITIES.get(cc.upper(), [])


# ── 만기별 매크로 심볼 발행 (챗/백테스트가 다른 매크로처럼 참조·표시=챗사용가능 Phase 6a) ──
# 만기 라벨 → 한글 접미(기존 명명 규약 계승: '미국채2년'·'미국채3개월').
_TENOR_KR = {"1M": "1개월", "3M": "3개월", "6M": "6개월", "1Y": "1년", "2Y": "2년",
             "3Y": "3년", "4Y": "4년", "5Y": "5년", "6Y": "6년", "7Y": "7년",
             "8Y": "8년", "9Y": "9년", "10Y": "10년", "15Y": "15년", "20Y": "20년",
             "25Y": "25년", "30Y": "30년", "40Y": "40년", "50Y": "50년"}
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


_JP_ALL_URL = ("https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
               "historical/jgbcme_all.csv")            # 누적(1974~ **전월까지**)
_JP_CURRENT_URL = ("https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/"
                   "jgbcme.csv")                        # 당월(이번 달 영업일)


def _parse_mof_csv(text: str, start: str) -> dict[str, dict[str, float]]:
    """MOF 금리 CSV(누적·당월 **동일 포맷**) → {date: {만기: 값}}.

    0행=제목("Interest Rate (July 2026)" 등), 1행=컬럼 헤더, 이후 'YYYY/M/D' 데이터행.
    말미 안내문·빈 행은 날짜 형식 불일치로 자연 제외된다. 순수함수(네트워크 없음)."""
    lines = text.strip().splitlines()
    rd = csv.reader(lines[1:])            # 0행=제목, 1행=컬럼 헤더
    try:
        header = next(rd)
    except StopIteration:
        return {}
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
        pt = {mat: v for mat, i in idx.items()
              if i < len(row) and (v := _num(row[i])) is not None}
        if pt:
            out[iso] = pt
    return out


def _jp_curve(start: str) -> dict[str, dict[str, float]]:
    """MOF 누적 + 당월 병합 — 누적 파일이 **전월까지만** 담는 구조적 한계를 닫는다.

    실측(2026-07-24): 누적 jgbcme_all.csv 최신=2026-06-30, 당월 jgbcme.csv=2026-07-23.
    누적만 읽으면 매월 1일부터 그 달 내내 최대 한 달 지연되다가 익월에 몰아서 따라잡는
    톱니 패턴이 된다. 당월을 나중에 병합해 같은 날짜는 당월 파일 값이 이긴다(최신 우선).

    당월 파일 실패는 **non-fatal** — 누적본만으로 진행한다(외부 소스 일시 장애에 전체
    수집을 잃지 않기 위함). 이 폴백이 조용히 상시화되면 stale이 되므로 refresh_all의
    staleness 경보가 그것을 표면화한다(무증상 방치 방지)."""
    import requests

    def _get(url: str) -> str:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=40)
        r.raise_for_status()
        return r.text

    out = _parse_mof_csv(_get(_JP_ALL_URL), start)     # 실패 시 예외 전파(전체 fetch 실패)
    try:
        out.update(_parse_mof_csv(_get(_JP_CURRENT_URL), start))
    except Exception:                                  # noqa: BLE001 — 당월만 실패: 누적본 유지
        pass
    return out


_CN_URL = "https://yield.chinabond.com.cn/cbweb-mn/yc/downYearBzqx"
_CN_YCDEF = "2c9081e50a2f9606010a3068cae70001"      # 중채국채수익률곡선(국채) 커브 ID


def parse_cn_year_xlsx(content: bytes) -> dict[str, dict[str, float]]:
    """ChinaBond 연도 XLSX(long 포맷) → {date: {만기: 값}}. 순수함수(네트워크 없음).

    컬럼: Date | Instructions for Standard Terms | Standard Terms(Yrs) | Yield(%).
    행 하나가 (날짜, 만기, 수익률) — 만기 라벨은 '3m'·'10y' 형식이라 대문자화하면 우리 규약과
    같아진다(3M·10Y). 채택 만기(_CN_MATS) 외(0d·2m·9m)는 제외. 만기 구성이 시대별로 다르지만
    (2006~08 14종 → 2015 21종 → 현재 17종) 라벨 기준 필터라 자연 흡수된다.
    openpyxl은 **lazy import**(pandas 경유) — core를 쓰는 로컬앱엔 openpyxl이 없다(과거 인시던트)."""
    df = pd.read_excel(io.BytesIO(content))          # openpyxl은 여기서만 필요(지연 로드)
    cols = list(df.columns)
    if len(cols) < 4:
        return {}
    c_date, c_tenor, c_yield = cols[0], cols[1], cols[3]
    keep = set(_CN_MATS)
    out: dict[str, dict[str, float]] = {}
    for d, t, y in zip(df[c_date], df[c_tenor], df[c_yield]):
        mat = str(t).strip().upper()
        if mat not in keep:
            continue
        v = _num(y)
        if v is None:
            continue
        iso = str(d).strip().replace("/", "-")[:10]
        if len(iso) == 10:
            out.setdefault(iso, {})[mat] = v
    return out


def _cn_curve(start: str) -> dict[str, dict[str, float]]:
    """ChinaBond 국채 커브 — 연도별 XLSX를 floor~올해까지 수집해 병합.

    소스가 연도 단위 다운로드만 제공해 연도당 1콜(2026년 기준 2,346행·17만기·~2s)이다.
    **한 해라도 실패하면 예외를 올려 전체를 실패시킨다** — 중간 연도가 빠진 반쪽 이력으로
    저장본을 덮으면 커브에 구멍이 생기기 때문(부분 전진 금지). 실패 시 refresh가 기존 저장본을
    보존하고, 상시화되면 staleness 경보가 표면화한다."""
    import requests

    y0 = max(_CN_FIRST_YEAR, int(start[:4]) if start[:4].isdigit() else _CN_FIRST_YEAR)
    out: dict[str, dict[str, float]] = {}
    for year in range(y0, date.today().year + 1):
        r = requests.get(_CN_URL, params={"year": year, "wrjxCBFlag": 0, "zblx": "txy",
                                          "ycDefId": _CN_YCDEF, "locale": "en_US"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
        r.raise_for_status()
        year_pts = parse_cn_year_xlsx(r.content)
        out.update({d: pt for d, pt in year_pts.items() if d >= start})
        time.sleep(0.3)                              # 소스 예우(연속 다운로드)
    return out


def _kr_curve(start: str) -> dict[str, dict[str, float]]:
    """한국 — KRX 공식 국고채 지표물(일별) 매크로 시계열 재사용. **네트워크 없음**.

    krx_openapi 피드가 매일 수집·저장하는 `국고채3년`·`국고채10년` parquet(Close)을 읽어
    커브 형태로 조립한다. 별도 수집·API 키·신규 심볼이 없어 ALL_SYMBOLS가 불변이다
    (자동매매 데이터셋 무영향). 파일이 아직 없으면(콜드 볼륨) 그 만기를 건너뛴다 —
    빈 결과면 refresh가 기존 저장본을 보존한다."""
    out: dict[str, dict[str, float]] = {}
    for tenor, macro_name in _KR_MACRO:
        p = _macro_path(macro_name)
        if not p.exists():
            continue
        df = read_parquet_safe(p)
        if df is None or df.empty or "Close" not in df.columns:
            continue
        for ts, val in df["Close"].dropna().items():
            iso = pd.Timestamp(ts).strftime("%Y-%m-%d")
            if iso < start:
                continue
            v = _num(val)
            if v is not None:
                out.setdefault(iso, {})[tenor] = v
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
        return _kr_curve(start)              # KRX 국고채 지표물 재사용(네트워크 없음)
    if cc == "CN":
        return _cn_curve(start)                  # ChinaBond 공식 커브(FRED 시리즈 단종 대체)
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


def stale_countries(today: date | None = None) -> dict[str, dict]:
    """저장본이 기대 갱신 주기를 넘긴 국가 → {cc: {"last", "age_days", "limit"}}.

    **조용한 소스 단종을 잡는 유일한 신호.** 수집이 "실패"로 드러나는 건 빈 응답뿐이라,
    소스가 HTTP 200으로 옛 데이터를 계속 주면(중국 IR3TTS01CNM156N: 2023-11 이후 정지)
    행수·예외 어디에도 이상이 없어 2년 반을 아무도 몰랐다. 저장본의 **마지막 날짜**를
    직접 보고 나이를 재는 것만이 그 부류를 표면화한다. 순수 조회(부작용 없음)."""
    ref = today or date.today()
    out: dict[str, dict] = {}
    for cc in COUNTRIES:
        df = load(cc)
        if df is None or df.empty:
            continue                                  # 미수집은 여기 관심사 아님(수집 로그가 담당)
        last = pd.Timestamp(df.index.max()).date()
        age = (ref - last).days
        limit = _STALE_AFTER_DAYS.get(cc, 7)
        if age > limit:
            out[cc] = {"last": last.isoformat(), "age_days": age, "limit": limit}
    return out


def refresh_all() -> dict[str, int]:
    """전 국가 수집(일일 cron). {cc: 행수}. 실패 국가는 0(기존 저장본 보존).

    수집 후 stale 국가를 함께 반환 키 `_stale`로 실어 호출자(서버 cron)가 로그로 표면화한다."""
    out: dict = {}
    for cc in COUNTRIES:
        df = refresh(cc)
        out[cc] = 0 if df is None else len(df)
    stale = stale_countries()
    if stale:
        out["_stale"] = stale
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

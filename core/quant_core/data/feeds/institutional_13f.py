"""enrich.institutional_13f 피드 — US 기관 13F 보유 (SEC Form 13F Data Sets, 분기).

소스: sec.gov form-13f-data-sets 분기 ZIP(구조화 TSV, 2013Q2~). 명명 혼재 — 구 2023q4_form13f.zip
(분기)/신 01dec2025-28feb2026_form13f.zip(롤링). 인덱스 스크랩으로 URL 취득(파일명 가정 금지).
무키(SEC UA만). 최근 분기 압축 ~86-100MB(비압축 수백만 행 → INFOTABLE 청크 집계로 메모리 상한).

산출(종목별 분기 시계열): 전 신고자 INFOTABLE을 CUSIP별 합산(가치·주식수·기관수) → FTD로
CUSIP→ticker → 종목별 parquet(institutional/{TICKER}.parquet). 소비층(add_institutional_holdings)이
전분기 대비 순증감(qoq)을 파생.

PIT: as_of = 보고분기말 + 45일(제출기한=집계 공개 확정 시점). PERIODOFREPORT로 보고분기 판정,
(CIK,분기) 복수신고(수정신고 13F-HR/A 포함)는 최신 FILING_DATE 1건만(중복계상 방지).
VALUE 단위 자동감지: median(value/shares)<1 이면 천달러 표기(구포맷)→x1000(2023 컷오버 하드코딩 회피).

한계(정직 문서화):
- CUSIP→ticker = SEC FTD(실패발생 종목만 등재) — 유동 US 주식 대부분 커버, 비유동 극소수 미매핑=드랍.
- 옵션(PUTCALL≠'')·비주식(SSHPRNAMTTYPE≠SH) 제외 = 주식 롱 보유만(13F 자체가 롱온리·풋/콜 별개).
- 필러 오기입(값 단위·자릿수) = CUSIP별 내재가격 중앙값 대비 5배 밖 행 제외로 정제(value·shares 동시).
  단 holders=1 종목은 교차검증 불가 → value 잔여 오류 가능(신호는 institutional_holders·qoq 우선).
- 분기·45일 지연. 구조화 이전(2013Q1↓)은 원시파싱 후순위(2010 Core floor 미달분 정직 노출).
- 대표 클래스 외 복수 CUSIP이 한 ticker에 매핑되면 value·shares 합산·holders는 max 근사(이중 클래스 극소).
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timedelta

import pandas as pd

from ... import data_fetcher as _df
from ...parquet_io import read_parquet_safe, write_parquet_atomic

_UA = "quantman-research eogkrvlfrl@gmail.com"   # SEC fair-access: 연락처 포함 UA 필수
_INDEX_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
_SEC_ROOT = "https://www.sec.gov"
_ZIP_RE = re.compile(
    r'href="(/files/structureddata/data/form-13f-data-sets/([^"]+_form13f\.zip))"', re.I)
_FTD_URL = "https://www.sec.gov/files/data/fails-deliver-data/cnsfails{ym}{half}.zip"

INSTITUTIONAL_DIR = _df.DATA_DIR / "institutional"
_CUSIP_MAP_PATH = INSTITUTIONAL_DIR / "_cusip_map.parquet"
_DONE_MARKER = INSTITUTIONAL_DIR / "_done_zips.json"

# 13F 구조화 데이터셋 자연 floor(2013Q2~; 이전은 원시파싱 후순위). Core 2010 미달분은 정직 노출.
SOURCE_FLOOR = "2013-04-01"
# 저장 컬럼 — spec.provides·매니페스트 _FIELD_GROUPS가 참조(단일 출처). qoq는 소비층 파생.
INSTITUTIONAL_COLS = ["institutional_value", "institutional_shares", "institutional_holders"]
_FILING_DEADLINE_DAYS = 45      # 분기말+45일 = 집계 공개 확정 = PIT as_of
_INFO_USECOLS = {"ACCESSION_NUMBER", "CUSIP", "VALUE", "SSHPRNAMT",
                 "SSHPRNAMTTYPE", "PUTCALL"}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ---------------------------------------------------------------- 순수 파서(픽스처 테스트)

def _prep_submissions(sub_df: pd.DataFrame):
    """SUBMISSION DF → (primary_period, keep_acc set). 순수함수.

    primary_period = 최빈 PERIODOFREPORT(13F-HR 계열 기준) = 이 ZIP의 주 보고분기.
    keep_acc = 그 분기 신고 중 (CIK)별 최신 FILING_DATE 1건(수정신고 supersede·중복계상 방지).
    데이터 부족 시 (None, set()).
    """
    if sub_df is None or sub_df.empty or "ACCESSION_NUMBER" not in sub_df.columns:
        return None, set()
    s = sub_df.copy()
    s["_period"] = pd.to_datetime(s.get("PERIODOFREPORT"), format="%d-%b-%Y", errors="coerce")
    s["_filed"] = pd.to_datetime(s.get("FILING_DATE"), format="%d-%b-%Y", errors="coerce")
    s = s.dropna(subset=["_period"])
    if s.empty:
        return None, set()
    subtype = s.get("SUBMISSIONTYPE", pd.Series("", index=s.index)).fillna("")
    hr = s[subtype.str.startswith("13F-HR")]
    base = hr if not hr.empty else s
    mode = base["_period"].mode()
    if mode.empty:
        return None, set()
    pp = mode.iloc[0]
    cur = s[s["_period"] == pp].copy()
    cur["_filed"] = cur["_filed"].fillna(pp)
    cur = cur.sort_values("_filed").drop_duplicates("CIK", keep="last")
    return pp, set(cur["ACCESSION_NUMBER"].astype(str))


def _clean_rows(df: pd.DataFrame, keep_acc: set) -> pd.DataFrame:
    """INFOTABLE 청크(문자열 DF) → 유효 주식보유 행 [CUSIP, value, shares]. 순수함수.

    keep_acc(=primary period·CIK 최신) 신고만. 옵션(PUTCALL≠'')·비주식(SSHPRNAMTTYPE≠SH)·
    무효/무CUSIP 제외. value·shares 숫자화(coerce 실패 행 제외). 집계·이상치 판정 전 단계.
    """
    cols = ["CUSIP", "value", "shares"]
    if df is None or df.empty or "ACCESSION_NUMBER" not in df.columns:
        return pd.DataFrame(columns=cols)
    c = df[df["ACCESSION_NUMBER"].astype(str).isin(keep_acc)]
    if c.empty:
        return pd.DataFrame(columns=cols)
    if "PUTCALL" in c.columns:
        c = c[c["PUTCALL"].fillna("").astype(str).str.strip() == ""]
    if "SSHPRNAMTTYPE" in c.columns:
        t = c["SSHPRNAMTTYPE"].fillna("SH").astype(str).str.strip().str.upper()
        c = c[t.isin(["SH", ""])]
    if c.empty:
        return pd.DataFrame(columns=cols)
    cusip = c["CUSIP"].astype(str).str.strip().str.upper()
    value = pd.to_numeric(c["VALUE"], errors="coerce")
    shares = pd.to_numeric(c["SSHPRNAMT"], errors="coerce")
    ok = value.notna() & shares.notna() & (cusip.str.len() >= 8)
    return pd.DataFrame({"CUSIP": cusip[ok], "value": value[ok], "shares": shares[ok]})


def _outlier_keep_mask(rows: pd.DataFrame, med: dict, band=(0.2, 5.0)) -> pd.Series:
    """행별 유지 마스크 — 내재가격(value/shares)이 CUSIP 중앙값의 band 밖이면 제외. 순수함수.

    13F는 필러 오기입(값 단위 혼동·자릿수 오류)이 흔하다(실측: AAPL 내재 $7,008·WDC 시총초과).
    같은 CUSIP·분기의 내재가격은 사실상 단일값(분기말 시장가)이라, 중앙값 대비 0.2~5배 밖은
    오기입으로 보고 제외 → value·shares 동시 정제. med 없는 CUSIP·shares≤0은 판단불가로 보존.
    """
    if rows.empty or not med:
        return pd.Series(True, index=rows.index)
    m = rows["CUSIP"].map(med)
    ip = rows["value"] / rows["shares"].where(rows["shares"] > 0)
    within = (ip >= band[0] * m) & (ip <= band[1] * m)
    return m.isna() | (rows["shares"] <= 0) | within.fillna(False)


def _apply_scale(agg: pd.DataFrame) -> pd.DataFrame:
    """CUSIP별 합산본(value_raw/shares/holders) → VALUE 단위 감지·달러화·최종 컬럼. 순수함수.

    median(value_raw/shares) < 1 = 천달러 표기(구포맷) → x1000. 아니면 달러(x1). 2023 컷오버
    하드코딩 회피 — 분기별 실측 가격비 중앙값으로 자기보정.
    """
    if agg is None or agg.empty:
        return pd.DataFrame(columns=INSTITUTIONAL_COLS)
    ratio = agg["value_raw"] / agg["shares"].where(agg["shares"] > 0)
    ratio = ratio.replace([float("inf"), -float("inf")], pd.NA).dropna()
    scale = 1000 if (not ratio.empty and ratio.median() < 1.0) else 1
    return pd.DataFrame({
        "institutional_value": agg["value_raw"] * scale,
        "institutional_shares": agg["shares"],
        "institutional_holders": agg["holders"].astype(float),
    }, index=agg.index)


def _parse_ftd(text: str) -> list[tuple[str, str]]:
    """FTD 파일 텍스트 → [(cusip, symbol)]. pipe-delimited(SETTLEMENT DATE|CUSIP|SYMBOL|...). 순수."""
    out: list[tuple[str, str]] = []
    for ln in text.splitlines():
        p = ln.split("|")
        if len(p) < 3 or p[0].strip() == "SETTLEMENT DATE":
            continue
        cusip = p[1].strip().upper()
        sym = p[2].strip().upper()
        if len(cusip) >= 8 and sym and sym.isascii() and " " not in sym:
            out.append((cusip, sym))
    return out


def _label_key(label: str) -> tuple[int, int]:
    """ZIP label → (year, quarter) 정렬키(신·구 명명 통일·시간 단조). 파싱 실패는 (0,0)."""
    lab = label.lower()
    m = re.match(r"(\d{4})q([1-4])", lab)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.match(r"\d{2}([a-z]{3})(\d{4})", lab)      # 01dec2024-28feb2025...
    if m:
        mon = _MONTHS.get(m.group(1), 0)
        return (int(m.group(2)), (mon - 1) // 3 + 1)
    return (0, 0)


# ---------------------------------------------------------------- IO(네트워크)

def _session():
    import requests
    s = requests.Session()
    s.headers.update({"User-Agent": _UA})
    return s


def list_zip_urls(sess=None) -> list[tuple[str, str]]:
    """13F Data Sets 인덱스 스크랩 → [(label, url)] 최신→과거 정렬(신규 우선). 실패 시 []."""
    sess = sess or _session()
    try:
        r = sess.get(_INDEX_URL, timeout=30)
        r.raise_for_status()
    except Exception:                       # noqa: BLE001 — 네트워크/HTTP 실패=[](마커 금지)
        return []
    seen: dict[str, str] = {}
    for m in _ZIP_RE.finditer(r.text):
        path, fname = m.group(1), m.group(2)
        label = fname[:-len("_form13f.zip")] if fname.endswith("_form13f.zip") else fname
        seen[label] = _SEC_ROOT + path
    return sorted(seen.items(), key=lambda x: _label_key(x[0]), reverse=True)


def _member(zf: zipfile.ZipFile, upper_name: str):
    """대소문자 무시 멤버 이름 조회(멤버 확장자 소문자 .tsv)."""
    for n in zf.namelist():
        if n.upper() == upper_name:
            return n
    return None


def _read_tsv(zf: zipfile.ZipFile, upper_name: str) -> pd.DataFrame:
    """zip 멤버 TSV 전체 → 문자열 DataFrame. 없으면 빈 DF. (SUBMISSION 등 소형용)."""
    name = _member(zf, upper_name)
    if not name:
        return pd.DataFrame()
    with zf.open(name) as fh:
        return pd.read_csv(fh, sep="\t", dtype=str, encoding="latin-1",
                           on_bad_lines="skip", quoting=3)     # QUOTE_NONE — 텍스트에 " 흔함


def _iter_infotable(zf: zipfile.ZipFile, chunksize: int = 500_000):
    """INFOTABLE.tsv 청크 이터레이터(usecols만·메모리 상한). 없으면 no yield."""
    name = _member(zf, "INFOTABLE.TSV")
    if not name:
        return
    with zf.open(name) as fh:
        yield from pd.read_csv(fh, sep="\t", dtype=str, encoding="latin-1",
                               on_bad_lines="skip", quoting=3, chunksize=chunksize,
                               usecols=lambda c: c in _INFO_USECOLS)


def _cusip_medians(zf: zipfile.ZipFile, keep_acc: set, per_cusip: int = 6) -> dict:
    """pass1 — CUSIP별 내재가격 중앙값(오기입 탐지 기준). 청크당 CUSIP별 per_cusip 표본만
    모아 메모리 상한(전 행 보관 회피). 반환 {cusip: median_implied_price}."""
    parts = []
    for chunk in _iter_infotable(zf):
        f = _clean_rows(chunk, keep_acc)
        f = f[f["shares"] > 0]
        if f.empty:
            continue
        f = f.assign(ip=f["value"] / f["shares"])
        parts.append(f.groupby("CUSIP", as_index=False).head(per_cusip)[["CUSIP", "ip"]])
    if not parts:
        return {}
    return pd.concat(parts).groupby("CUSIP")["ip"].median().to_dict()


def _aggregate_holdings(zf: zipfile.ZipFile, keep_acc: set, med: dict) -> pd.DataFrame:
    """pass2 — 오기입 제외 후 CUSIP별 합산 [value_raw, shares, holders]. 청크 groupby 결합.

    holders = 정제 후 행 수(keep_acc가 CIK당 1신고 → CUSIP당 행수 = 보유 기관수).
    """
    parts = []
    for chunk in _iter_infotable(zf):
        f = _clean_rows(chunk, keep_acc)
        if f.empty:
            continue
        f = f[_outlier_keep_mask(f, med)]
        if f.empty:
            continue
        parts.append(f.groupby("CUSIP").agg(
            value_raw=("value", "sum"), shares=("shares", "sum"), holders=("value", "size")))
    if not parts:
        return pd.DataFrame(columns=["value_raw", "shares", "holders"])
    return pd.concat(parts).groupby(level=0).sum()


def fetch_quarter(url: str, sess=None):
    """ZIP 1개 다운로드→2-pass(오기입 정제) 주 보고분기 CUSIP 집계. 반환 (period, agg_df)|None.

    agg_df = CUSIP 인덱스·[institutional_value, institutional_shares, institutional_holders].
    실패(네트워크·스키마 이상·빈 SUBMISSION)는 None → 호출자가 마커 금지·재시도.
    zip은 RAM(BytesIO)에 유지 — INFOTABLE을 2회 순회(pass1 중앙값·pass2 정제집계)해도 네트워크 1회.
    """
    sess = sess or _session()
    try:
        r = sess.get(url, timeout=300)
        r.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(r.content))
    except Exception:                       # noqa: BLE001 — 다운로드/압축 실패=None(재시도)
        return None
    try:
        sub = _read_tsv(zf, "SUBMISSION.TSV")
        pp, keep_acc = _prep_submissions(sub)
        if pp is None or not keep_acc:
            return None
        med = _cusip_medians(zf, keep_acc)
        agg = _aggregate_holdings(zf, keep_acc, med)
    except Exception:                       # noqa: BLE001 — 파싱 예외=실패(마커 금지·재시도)
        return None
    return (pp, _apply_scale(agg))


# ---------------------------------------------------------------- CUSIP→ticker 매핑(FTD)

def _recent_ftd_urls(months: int = 24) -> list[str]:
    """최근 months개월의 FTD 반월(a/b) 파일 URL(신규→과거). 서버 런타임 now 사용(스크립트 아님)."""
    now = datetime.now()
    y, mo = now.year, now.month
    urls: list[str] = []
    for _ in range(months):
        ym = f"{y:04d}{mo:02d}"
        urls.append(_FTD_URL.format(ym=ym, half="b"))
        urls.append(_FTD_URL.format(ym=ym, half="a"))
        mo -= 1
        if mo == 0:
            y, mo = y - 1, 12
    return urls


def build_cusip_map(months: int = 24, sess=None, max_files: int | None = None) -> dict:
    """최근 FTD 파일에서 CUSIP→ticker 매핑 축적(CUSIP별 최빈 티커) → parquet 저장.

    FTD는 실패발생 종목만 등재 — 24개월이면 유동 US 주식 대부분 커버(비유동 극소수 미매핑=드랍).
    반환 {"cusips", "files"}.
    """
    from collections import Counter, defaultdict
    sess = sess or _session()
    counts: dict[str, Counter] = defaultdict(Counter)
    n_files = 0
    for url in _recent_ftd_urls(months):
        if max_files and n_files >= max_files:
            break
        try:
            r = sess.get(url, timeout=90)
            if r.status_code != 200:
                continue
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            with zf.open(zf.namelist()[0]) as fh:
                text = io.TextIOWrapper(fh, encoding="latin-1", errors="replace").read()
        except Exception:                   # noqa: BLE001 — 개별 파일 실패는 skip(부분 커버 허용)
            continue
        for cusip, sym in _parse_ftd(text):
            counts[cusip][sym] += 1
        n_files += 1
    if not counts:
        return {"cusips": 0, "files": n_files}
    rows = [{"cusip": c, "symbol": cnt.most_common(1)[0][0]} for c, cnt in counts.items()]
    df = pd.DataFrame(rows).set_index("cusip")
    INSTITUTIONAL_DIR.mkdir(parents=True, exist_ok=True)
    write_parquet_atomic(df, _CUSIP_MAP_PATH)
    return {"cusips": len(rows), "files": n_files}


def load_cusip_map() -> dict:
    """CUSIP→ticker dict. 없으면 빈 dict."""
    if not _CUSIP_MAP_PATH.exists():
        return {}
    df = read_parquet_safe(_CUSIP_MAP_PATH)
    if df is None or df.empty or "symbol" not in df.columns:
        return {}
    return df["symbol"].to_dict()


# ---------------------------------------------------------------- 저장·적재

def _merge_save(sym: str, df_new: pd.DataFrame) -> None:
    """종목별 parquet에 as_of 기준 merge(신규 우선·정정 반영) 후 atomic write. '/'는 '_' 치환.

    존재 체크 후 read — read_parquet_safe는 '손상 격리'용이지 '부재 허용'이 아니라서 부재
    파일에 부르면 격리 시도 소음 로그가 신규 종목 수만큼 쏟아진다(프로덕션 실측·marketcap 규약)."""
    p = INSTITUTIONAL_DIR / f"{sym.replace('/', '_')}.parquet"
    existing = read_parquet_safe(p) if p.exists() else None
    if existing is not None and not existing.empty:
        merged = pd.concat([existing[~existing.index.isin(df_new.index)], df_new]).sort_index()
    else:
        merged = df_new.sort_index()
    write_parquet_atomic(merged, p)


def ingest_quarter(period, agg: pd.DataFrame, cusip_map: dict) -> dict:
    """분기 CUSIP 집계 → ticker 매핑 → 종목별 parquet에 as_of(period+45d) 행 merge.

    미매핑 CUSIP 드랍(정직). 한 ticker에 복수 CUSIP(클래스) 매핑 시 value·shares 합산·
    holders는 max 근사. 반환 {"symbols", "unmapped"}.
    """
    if agg is None or agg.empty:
        return {"symbols": 0, "unmapped": 0}
    as_of = pd.Timestamp(period) + timedelta(days=_FILING_DEADLINE_DAYS)
    g = agg.reset_index().rename(columns={"index": "CUSIP"})
    g["symbol"] = g["CUSIP"].map(cusip_map)
    unmapped = int(g["symbol"].isna().sum())
    mapped = g.dropna(subset=["symbol"])
    if mapped.empty:
        return {"symbols": 0, "unmapped": unmapped}
    by_sym = mapped.groupby("symbol").agg(
        institutional_value=("institutional_value", "sum"),
        institutional_shares=("institutional_shares", "sum"),
        institutional_holders=("institutional_holders", "max"))
    INSTITUTIONAL_DIR.mkdir(parents=True, exist_ok=True)
    for sym, row in by_sym.iterrows():
        df_new = pd.DataFrame([{
            "as_of": as_of, "period_of_report": pd.Timestamp(period),
            "institutional_value": float(row["institutional_value"]),
            "institutional_shares": float(row["institutional_shares"]),
            "institutional_holders": float(row["institutional_holders"])}]).set_index("as_of")
        _merge_save(str(sym), df_new)
    _df.mark_data_dirty()
    return {"symbols": int(len(by_sym)), "unmapped": unmapped}


def backfill_13f(limit: int = 2, sess=None) -> dict:
    """미완료 분기 ZIP을 최신→과거 순으로 limit개 처리(다운로드→집계→종목별 저장·마커).

    CUSIP 맵 없으면 먼저 구축. 실패한 ZIP은 마커 금지(다음 청크 재시도). 반환 진행 통계.
    """
    sess = sess or _session()
    cmap = load_cusip_map()
    if not cmap:
        bm = build_cusip_map(sess=sess)
        cmap = load_cusip_map()
        if not cmap:
            return {"ok": False, "reason": "cusip_map_empty", **bm}
    urls = list_zip_urls(sess)
    if not urls:
        return {"ok": False, "reason": "index_unavailable"}
    done = _df._load_marker_set(_DONE_MARKER)
    processed: list[dict] = []
    for label, url in urls:
        if len(processed) >= limit:
            break
        if label in done:
            continue
        res = fetch_quarter(url, sess)
        if res is None:
            continue                        # 실패 — 마커 금지(다음 청크 재시도)
        period, agg = res
        stats = ingest_quarter(period, agg, cmap)
        done.add(label)
        processed.append({"label": label, "period": str(pd.Timestamp(period).date()), **stats})
    _df._save_marker_set(_DONE_MARKER, done)
    return {"ok": True, "processed": processed, "done_total": len(done),
            "remaining": sum(1 for lbl, _ in urls if lbl not in done)}


def load_institutional(sym: str) -> pd.DataFrame:
    """종목별 13F 기관보유 이력 로드(add_institutional_holdings 입력·raw). 없으면 빈 DF."""
    p = INSTITUTIONAL_DIR / f"{sym.replace('/', '_')}.parquet"
    if not p.exists():
        return pd.DataFrame()
    df = read_parquet_safe(p)
    return df if df is not None else pd.DataFrame()


def load_institutional_all() -> dict[str, pd.DataFrame]:
    """전 종목 13F 패널 — data_cache aux 캐시(디렉터리 주도·내부 `_` 파일 제외)."""
    out: dict[str, pd.DataFrame] = {}
    if INSTITUTIONAL_DIR.exists():
        for p in INSTITUTIONAL_DIR.glob("*.parquet"):
            if p.name.startswith("_"):
                continue                    # _cusip_map 등 내부 파일 제외
            df = read_parquet_safe(p)
            if df is not None and not df.empty:
                out[p.stem] = df
    return out

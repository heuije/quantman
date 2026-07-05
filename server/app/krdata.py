"""한국 종목 부가 데이터 — 무료 공개 소스 크롤링 (Company Analysis 전용).

소스(모두 무료, 한국 종목 6자리 코드 한정):
- 투자자별 순매매: 네이버 금융 frgn (기관·외국인; 개인=−(기관+외국인) 근사)
- 애널리스트 리포트 목록: 네이버 금융 리서치 (작성일·증권사·제목)
- 컨센서스(목표가·투자의견·변동률): 네이버 wisereport
- 추정 실적(매출·영업이익·지배주주·EBITDA): FnGuide + wisereport
- 공시: DART(OpenDartReader, 서버 OPENDART_API_KEY)
- 공매도: 네이버/KRX (배포 환경 검증)

캐싱: lru_cache(일자 키)로 종목·소스별 일 1회만 fetch.
실패는 빈 결과 반환(부분 제공) — 한 소스가 죽어도 나머지는 동작.
"""
from __future__ import annotations

import io
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache

import certifi
import pandas as pd
import requests
from bs4 import BeautifulSoup

_log = logging.getLogger("app.krdata")
_H = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_C = certifi.where()


def _num(v):
    try:
        return None if v is None or pd.isna(v) else float(v)
    except Exception:
        return None


def _int(v):
    n = _num(v)
    return None if n is None else int(n)


def is_kr(symbol: str) -> bool:
    return symbol.strip().isdigit() and len(symbol.strip()) == 6


# 투자의견 표기 통일 — 증권사마다 "매수"/"Buy"/"BUY"가 섞이므로 BUY/HOLD/SELL로 정규화.
_OPINION_MAP = {
    "매수": "BUY", "적극매수": "BUY", "강력매수": "BUY", "buy": "BUY",
    "strongbuy": "BUY", "outperform": "BUY", "비중확대": "BUY", "overweight": "BUY",
    "중립": "HOLD", "보유": "HOLD", "hold": "HOLD", "marketperform": "HOLD",
    "시장수익률": "HOLD", "neutral": "HOLD",
    "매도": "SELL", "sell": "SELL", "비중축소": "SELL", "underperform": "SELL",
}


def _norm_opinion(s) -> str:
    t = str(s).strip()
    if not t or t == "nan":
        return ""
    return _OPINION_MAP.get(t.lower(), t.upper())


# ── 투자자별 순매매 (네이버 frgn) ────────────────────────────────────────────
# 1/5/20/60/120일 창 집계를 위해 최대 120 거래일 확보(페이지당 ~20일 → 페이지네이션).
@lru_cache(maxsize=256)
def _investor(code: str, _day: str) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for page in range(1, 8):              # 7페이지면 120거래일 충분히 커버
        r = requests.get("https://finance.naver.com/item/frgn.naver",
                         params={"code": code, "page": page}, headers=_H, verify=_C, timeout=15)
        r.encoding = "euc-kr"
        tables = pd.read_html(r.text)
        df = next((t for t in tables if t.shape[1] >= 9 and t.shape[0] > 3), None)
        if df is None:
            break
        added = 0
        for _, row in df.iterrows():
            v = row.tolist()
            d = str(v[0])
            if "." not in d or d in seen:     # 날짜 행만 + 페이지 간 중복 제거
                continue
            inst, foreign = _int(v[5]), _int(v[6])
            if inst is None and foreign is None:
                continue
            inst, foreign = inst or 0, foreign or 0
            seen.add(d)
            out.append({"date": d, "inst": inst, "foreign": foreign,
                        "indiv": -(inst + foreign)})  # 개인 ≈ −(기관+외국인)
            added += 1
        if added == 0 or len(out) >= 120:     # 새 행 없으면 마지막 페이지
            break
    return out[:120]


def investor(code: str) -> list[dict]:
    try:
        return _investor(code, date.today().isoformat())
    except Exception as e:
        _log.warning("투자자별 fetch 실패 %s: %s", code, e)
        return []


# ── 애널리스트 리포트 목록 (네이버 리서치) ───────────────────────────────────
_RPT_MONTHS = 12          # 최근 12개월 리포트 열람(뉴스 탭과 동일 범위)
_RPT_MAX_PAGES = 8        # 안전 상한 (30건/page → 최대 ~240건)
_RPT_DETAIL_N = 15        # 리포트 고유 목표가 상세 파싱은 최신 N건만


def _parse_rpt_date(d: str):
    """네이버 리포트 날짜 'YY.MM.DD' → date. 파싱 실패 시 None."""
    try:
        return datetime.strptime(d, "%y.%m.%d").date()
    except ValueError:
        return None


def _fetch_rpt_page(code: str, page: int) -> str:
    r = requests.get("https://finance.naver.com/research/company_list.naver",
                     params={"searchType": "itemCode", "itemCode": code, "page": page},
                     headers=_H, verify=_C, timeout=15)
    r.encoding = "euc-kr"
    return r.text


@lru_cache(maxsize=256)
def _reports(code: str, _day: str) -> list[dict]:
    # 네이버 리서치 목록에서 최근 12개월치 수집(30건/page).
    # 페이지 fetch는 병렬(순차 8페이지 ≈ 8s가 Summary 탭 병목이었음) — 파싱·경계 판단은 순서대로.
    from concurrent.futures import ThreadPoolExecutor
    cutoff = date.today() - timedelta(days=365)
    out = []
    with ThreadPoolExecutor(max_workers=_RPT_MAX_PAGES) as ex:
        pages = list(ex.map(lambda p: _fetch_rpt_page(code, p), range(1, _RPT_MAX_PAGES + 1)))
    for html in pages:
        soup = BeautifulSoup(html, "html.parser")
        page_rows, stop = 0, False
        for tr in soup.select("tr"):
            a = tr.find("a", href=lambda h: h and "company_read" in h)  # 제목→상세 링크 행만
            if a is None:
                continue
            tds = tr.find_all("td")
            if len(tds) < 5:
                continue
            title = a.get_text(strip=True)
            broker = tds[2].get_text(strip=True)
            d = tds[4].get_text(strip=True)
            if not title or "." not in d:
                continue
            page_rows += 1
            dt = _parse_rpt_date(d)
            if dt and dt < cutoff:          # 12개월 경계 도달 — 이후는 버리고 종료
                stop = True
                break
            # 원문: PDF 직접 링크 우선, 없으면 네이버 리포트 상세 페이지
            pdf = tr.find("a", href=lambda h: h and ".pdf" in h.lower())
            detail = "https://finance.naver.com/research/" + a["href"]
            url = pdf["href"] if pdf else detail
            out.append({"date": d, "title": title, "broker": broker, "url": url, "_detail": detail})
        if stop or page_rows == 0:          # 경계 도달 또는 빈 페이지면 중단
            break
    # 리포트 고유 목표주가 — 상세 페이지 병렬 파싱. 12개월 전체는 fetch 비용이 커
    # 최신 N건만 파싱하고, 나머지는 None → 프론트가 브로커 컨센서스 목표가로 폴백.
    from concurrent.futures import ThreadPoolExecutor

    def _tgt(detail_url: str):
        try:
            dd = requests.get(detail_url, headers=_H, verify=_C, timeout=10)
            dd.encoding = "euc-kr"
            t = re.search(r"목표주?가[^\d]{0,12}([\d,]+)",   # '목표가'·'목표주가' 모두
                          BeautifulSoup(dd.text, "html.parser").get_text(" ", strip=True))
            return _int(t.group(1).replace(",", "")) if t else None
        except Exception:
            return None
    head = out[:_RPT_DETAIL_N]
    with ThreadPoolExecutor(max_workers=8) as ex:
        targets = list(ex.map(_tgt, [o["_detail"] for o in head]))
    for o, tg in zip(head, targets):
        o["target"] = tg
    for o in out:                            # 상세 미파싱분은 target=None
        o.setdefault("target", None)
        o.pop("_detail", None)
    return out


def reports(code: str) -> list[dict]:
    try:
        return _reports(code, date.today().isoformat())
    except Exception as e:
        _log.warning("리포트 fetch 실패 %s: %s", code, e)
        return []


# ── 컨센서스 목표가·투자의견 (wisereport) ────────────────────────────────────
@lru_cache(maxsize=256)
def _consensus(code: str, _day: str) -> list[dict]:
    r = requests.get("https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx",
                     params={"cmp_cd": code}, headers=_H, verify=_C, timeout=15)
    tables = pd.read_html(r.text)
    tgt = next((t for t in tables if "목표가" in [str(c) for c in t.columns]
                and "투자의견" in [str(c) for c in t.columns]), None)
    if tgt is None:
        return []
    out = []
    for _, row in tgt.iterrows():
        d = {str(c): row[c] for c in tgt.columns}
        broker = str(d.get("제공처", "")).strip()
        if not broker or broker == "nan":
            continue
        if not broker.endswith("증권"):    # 제공처 약칭(KB·현대차·신한투자…)에 '증권' 보강
            broker += "증권"
        out.append({
            "broker": broker,
            "date": str(d.get("최종일자", "")),
            "target": _int(d.get("목표가")),
            "prev_target": _int(d.get("직전목표가")),
            "change_pct": _num(d.get("변동률(%)")),
            "opinion": _norm_opinion(d.get("투자의견", "")),
        })
        if len(out) >= 12:
            break
    return out


def consensus(code: str) -> list[dict]:
    try:
        return _consensus(code, date.today().isoformat())
    except Exception as e:
        _log.warning("컨센서스 fetch 실패 %s: %s", code, e)
        return []


# ── 추정 실적 (FnGuide 스냅샷 JSON — wcomp.fnguide.com) ──────────────────────
# 손익(억원)·멀티플(PER·PBR)·수익성(ROE)·EPS(PEG 계산용). 연간 5년 확정 + 3년 추정(E).


def _num_cell(s: str):
    s = s.replace(",", "").strip()
    if not s or s in ("-", "N/A", "완전잠식"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


@lru_cache(maxsize=256)
def _earnings(code: str, _day: str) -> dict:
    # FnGuide 신 사이트(wcomp, 2026-06말 개편) 스냅샷 JSON — 5년 확정 + 3년 추정(E).
    # ⚠ 구 comp.fnguide.com/SVO2/ASP/SVD_Main.asp 는 302(도메인 이전)로 폐지 — HTML 파싱이
    #   전 종목 빈 결과를 내던 원인. 신 API는 구조화 JSON이라 파싱도 강건.
    # 출처 그대로 표시: 일부 종목(삼성전자 등)은 FnGuide 원본 추정치가 비정상일 수
    # 있으나 우리 크롤링은 정확. 데이터 제공처 이슈는 출처 명시로 대응한다.
    def _snp(consol: str) -> dict:
        r = requests.get("https://wcomp.fnguide.com/CompanyInfo/getSnpFinancial",
                         params={"cmp_cd": code, "consol_typ": consol, "freq_typ": "Y"},
                         headers=_H, verify=_C, timeout=20)
        return r.json().get("dataset") or {}

    ds = _snp("C")                                   # 연결 우선
    if not (ds.get("data") or []):
        ds = _snp("I")                               # 연결 미작성 법인 — 별도 폴백
    hdr = [h for h in (ds.get("header") or []) if h.get("YYMM")]
    data = ds.get("data") or []
    if not hdr or not data:
        return {"years": [], "rows": {}}
    years = [h["YYMM"] + ("(E)" if (h.get("EP_CHK") or "").strip() == "E" else "") for h in hdr]
    # 새 표 계정명 → 기존 rows 키. '영업이익'은 확정만 있고 추정(E)은 '영업이익(발표기준)'
    # 행에 담기는 종목이 있어, 같은 키의 두 행 중 **값이 더 많은 행**을 채택한다.
    name_map = {"매출액": "매출액", "영업이익": "영업이익", "영업이익(발표기준)": "영업이익",
                "당기순이익": "당기순이익", "당기순이익(지배)": "지배주주",
                "PER": "PER", "PBR": "PBR", "ROE": "ROE", "EPS": "EPS"}
    rows: dict[str, list] = {}
    for rr in data:
        nm = (rr.get("NAME") or "").strip()
        key = name_map.get(nm)
        if not key:
            continue
        vals = [_num_cell("" if rr.get(f"VAL{i + 1}") is None else str(rr.get(f"VAL{i + 1}")))
                for i in range(len(hdr))]
        old = rows.get(key)
        if old is None or sum(v is not None for v in vals) > sum(v is not None for v in old):
            rows[key] = vals
    # 영업이익률·당기순이익률(%) — 최초 1회 계산해 함께 저장(렌더마다 재계산 X). 매출액 대비.
    rev = rows.get("매출액")
    if rev:
        def _margin(num_key: str):
            num = rows.get(num_key)
            if not num:
                return None
            return [round(n / d * 100, 1) if (n is not None and d) else None
                    for n, d in zip(num, rev)]
        m = _margin("영업이익")
        if m:
            rows["영업이익률"] = m
        m = _margin("당기순이익")
        if m:
            rows["당기순이익률"] = m
    return {"years": years, "rows": rows}


# 추정실적 디스크 캐시 — historical은 안 변하니 저장본을 즉시 서빙, 주 1회만 재조회.
_EARN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "earnings")
_EARN_FRESH_DAYS = 7


def _earn_path(code: str) -> str:
    return os.path.join(_EARN_DIR, f"{code}.json")


def earnings(code: str) -> dict:
    """추정실적(컨센서스). 저장본이 신선(7일 이내)하면 즉시 반환(재조회 X), 아니면 FnGuide 조회 후 저장."""
    import json
    path = _earn_path(code)
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
            fetched = cached.get("fetched", "")
            if fetched and (date.today() - date.fromisoformat(fetched)).days < _EARN_FRESH_DAYS:
                return cached["data"]
    except Exception:
        pass
    try:
        data = _earnings(code, date.today().isoformat())
    except Exception as e:
        _log.warning("추정실적 fetch 실패 %s: %s", code, e)
        try:                                            # 만료됐어도 저장본 있으면 fallback
            with open(path, encoding="utf-8") as f:
                return json.load(f)["data"]
        except Exception:
            return {"years": [], "rows": {}}
    if not data.get("years"):
        # 빈 결과는 저장하지 않는다 — 일시 실패(사이트 이전·차단)의 빈 dict가 7일 캐시로
        # 박혀 "컨센서스 있는데 미표시"가 지속되던 원인. 기존 저장본이 있으면 그걸 서빙.
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)["data"]
        except Exception:
            return data
    try:
        os.makedirs(_EARN_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fetched": date.today().isoformat(), "data": data}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass
    return data


# ── 최근 공시 (DART OpenDart API) ────────────────────────────────────────────
# 서버 env(OPENDART_API_KEY) 전용 — 키 없으면 빈 결과(로컬/미설정 시 자연 비활성).
@lru_cache(maxsize=256)
def _disclosures(code: str, _day: str) -> list[dict]:
    key = os.environ.get("OPENDART_API_KEY")
    if not key:
        return []
    import OpenDartReader
    dart = OpenDartReader(key)
    end = date.today()
    # 최근 1개월(30일) 전체 공시 — 건수 제한 없음(사용자 요청).
    df = dart.list(code, start=(end - timedelta(days=30)).isoformat(),
                   end=end.isoformat(), final=True)
    if df is None or df.empty:
        return []
    out = []
    for _, row in df.iterrows():
        rcept = str(row.get("rcept_no", ""))
        out.append({
            "date": str(row.get("rcept_dt", "")),
            "title": str(row.get("report_nm", "")).strip(),
            "submitter": str(row.get("flr_nm", "")).strip(),
            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}" if rcept else "",
        })
    return out


def disclosures(code: str) -> list[dict]:
    try:
        return _disclosures(code, date.today().isoformat())
    except Exception as e:
        _log.warning("공시 fetch 실패 %s: %s", code, e)
        return []


# ── 공매도 잔고 (KRX 정보데이터시스템 — OTP→CSV 다운로드) ──────────────────────
# 공매도는 무료 키 API가 없어 KRX 웹 데이터만이 원천. KRX는 봇 다운로드를 막는데
# (작업 샌드박스에선 download.cmd가 빈 응답) 배포 환경에선 가능할 수 있어 시도하되,
# 실패 시 빈 결과로 우아하게 비활성(다른 소스에 영향 없음). 컬럼은 한글 헤더 substring
# 매칭으로 식별(위치 비의존). bld/응답은 배포 실데이터로 최종 검증·조정 대상.
_KRX_REF = {"Referer": "http://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd"}


def _krx_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_H)
    s.verify = _C
    s.get("http://data.krx.co.kr/contents/MDC/MAIN/main/index.cmd", timeout=15)
    return s


def _short_isin(session: requests.Session, code: str) -> str | None:
    """6자리 종목코드 → KRX ISIN(KR7…). KRX 공매도 종목 finder 사용."""
    r = session.post("http://data.krx.co.kr/comm/finder/finder_srtisu.cmd",
                     data={"locale": "ko_KR", "mktsel": "ALL", "searchText": code},
                     headers=_KRX_REF, timeout=15)
    for row in r.json().get("block1", []):
        if str(row.get("short_code", "")) == code:
            return row.get("full_code")
    return None


@lru_cache(maxsize=256)
def _shorting(code: str, _day: str) -> list[dict]:
    s = _krx_session()
    isin = _short_isin(s, code)
    if not isin:
        return []
    end = date.today()
    params = {
        "bld": "dbms/MDC/STAT/srt/MDCSTAT30501",   # 개별종목 공매도 잔고 추이
        "locale": "ko_KR", "isuCd": isin, "name": "fileDown", "filetype": "csv",
        "strtDd": (end - timedelta(days=45)).strftime("%Y%m%d"),
        "endDd": end.strftime("%Y%m%d"), "share": "1", "money": "1", "csvxls_isNo": "false",
    }
    otp = s.get("http://data.krx.co.kr/comm/fileDn/GenerateOTP/generate.cmd",
                params=params, headers=_KRX_REF, timeout=15).text
    r = s.post("http://data.krx.co.kr/comm/fileDn/download_csv/download.cmd",
               data={"code": otp}, headers=_KRX_REF, timeout=20)
    r.encoding = "euc-kr"
    if not r.text.strip():            # KRX 차단 시 빈 응답 → 비활성
        return []
    df = pd.read_csv(io.StringIO(r.text), thousands=",")

    def col(*keys):                    # 한글 헤더 substring 매칭(컬럼 순서 비의존)
        for c in df.columns:
            s2 = str(c).replace(" ", "")
            if all(k in s2 for k in keys):
                return c
        return None

    c_date = col("일자") or df.columns[0]
    c_qty, c_amt, c_rto = col("잔고", "수량"), col("잔고", "금액"), col("비중")
    out = []
    for _, row in df.iterrows():
        d = str(row[c_date])
        if not any(ch.isdigit() for ch in d):
            continue
        out.append({
            "date": d,
            "bal_qty": _int(row[c_qty]) if c_qty else None,
            "bal_amt": _int(row[c_amt]) if c_amt else None,
            "bal_ratio": _num(row[c_rto]) if c_rto else None,
        })
    return out[:30]


def shorting(code: str) -> list[dict]:
    try:
        return _shorting(code, date.today().isoformat())
    except Exception as e:
        _log.warning("공매도 fetch 실패 %s: %s", code, e)
        return []


# ── 섹터 키워드 뉴스 (Google News RSS — 키 불필요, 국내 ko / 해외 en) ──────────────
def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "").strip()


# 해외 뉴스 제목 영→한 번역 — 무료 Google Translate 엔드포인트(키 불필요). 실패 시 None(영문만 표시).
@lru_cache(maxsize=4096)
def _translate_one(text: str) -> str | None:
    if not text:
        return None
    try:
        r = requests.get("https://translate.googleapis.com/translate_a/single",
                         params={"client": "gtx", "sl": "en", "tl": "ko", "dt": "t", "q": text},
                         headers=_H, timeout=8)
        data = r.json()
        return "".join(seg[0] for seg in data[0] if seg and seg[0]) or None
    except Exception:
        return None


def _translate_many(texts: list[str]) -> list[str | None]:
    from concurrent.futures import ThreadPoolExecutor
    # I/O bound(구글 무료번역 HTTP) — 워커 16으로 100건 ≈ 4~5s(6워커 ~15s가 뉴스탭 병목이었음)
    with ThreadPoolExecutor(max_workers=16) as ex:
        return list(ex.map(_translate_one, texts))


@lru_cache(maxsize=64)
def _news(query: str, region: str, _hour: str) -> list[dict]:
    """Google News RSS 키워드 검색. region='kr'(한국어) / 'global'(영어). 시간당 캐시.

    최근 12개월 기사 전부(최대 100건). 해외(global)는 영문 제목을 국문으로 번역해 title_ko에 담는다.
    """
    import xml.etree.ElementTree as ET
    hl, gl, ceid = ("ko", "KR", "KR:ko") if region == "kr" else ("en-US", "US", "US:en")
    url = (f"https://news.google.com/rss/search?q={requests.utils.quote(query)}"
           f"&hl={hl}&gl={gl}&ceid={ceid}")
    r = requests.get(url, headers=_H, timeout=12)
    root = ET.fromstring(r.content)
    cutoff = datetime.now(timezone.utc) - timedelta(days=365)     # 최근 12개월
    out: list[dict] = []
    for it in root.findall(".//item"):
        title = (it.findtext("title", "") or "").strip()
        src_el = it.find("{*}source")
        src = src_el.text if src_el is not None else ""
        if src and title.endswith(" - " + src):      # 제목 끝 " - 언론사" 제거
            title = title[: -(len(src) + 3)].strip()
        pub = (it.findtext("pubDate", "") or "")
        try:
            dt = parsedate_to_datetime(pub)
            if dt is not None and dt.tzinfo is not None and dt < cutoff:
                continue                              # 12개월 초과 제외
        except Exception:
            pass
        out.append({
            "title": title,
            "summary": _strip_html(it.findtext("description", ""))[:160],
            "source": src or "",
            "url": it.findtext("link", "") or "",
            "date": pub[:16],
        })
        if len(out) >= 100:
            break
    # 해외 — 영문 제목 국문 번역(병렬, 실패 시 영문만)
    if region == "global" and out:
        for o, ko in zip(out, _translate_many([o["title"] for o in out])):
            o["title_ko"] = ko
    return out


def news(kr_keywords: list[str], global_keywords: list[str]) -> dict:
    """섹터 키워드 뉴스 — 국내(kr)·해외(global) 구분 반환. 실패 시 해당 구분 빈 리스트."""
    from datetime import datetime
    hour = datetime.now().strftime("%Y%m%d%H")
    out = {"kr": [], "global": []}
    for region, kws in (("kr", kr_keywords), ("global", global_keywords)):
        kws = [k for k in (kws or []) if k][:8]
        if not kws:
            continue
        q = " OR ".join(f'"{k}"' if " " in k else k for k in kws)[:300]
        try:
            out[region] = _news(q, region, hour)
        except Exception as e:
            _log.warning("뉴스 fetch 실패 (%s): %s", region, e)
    return out


# ── 기업 개요 (FnGuide Snapshot — 설립일·홈페이지·대표) ───────────────────────────
@lru_cache(maxsize=256)
def _profile(code: str, _day: str) -> dict:
    """기업개요 — FnGuide 기업정보(SVD_Corp): 설립일·대표이사·홈페이지·종업원수. 전자공시 기반 핵심정보."""
    r = requests.get("https://comp.fnguide.com/SVO2/ASP/SVD_Corp.asp",
                     params={"pGB": "1", "gicode": f"A{code}", "cID": "", "MenuYn": "Y",
                             "ReportGB": "", "NewMenuID": "102", "stkGb": "701"},
                     headers=_H, verify=_C, timeout=15)
    if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
        r.encoding = r.apparent_encoding
    soup = BeautifulSoup(r.text, "html.parser")

    def _by_th(*keys):
        for th in soup.select("th, td"):
            nm = th.get_text(" ", strip=True).replace("\xa0", " ")
            if any(k in nm for k in keys):
                sib = th.find_next("td")
                if sib:
                    return sib.get_text(" ", strip=True).replace("\xa0", " ").strip()
        return ""
    txt = soup.get_text(" ", strip=True).replace("\xa0", " ")
    # 설립일 — '설립일' th의 값(YYYY/MM/DD)
    est = ""
    raw = _by_th("설립")
    m = re.search(r"([0-9]{4}[./-][0-9]{1,2}[./-][0-9]{1,2})", raw)
    if m:
        est = m.group(1).replace("/", ".").replace("-", ".")
    # 홈페이지
    hp = _by_th("홈페이지")
    if not hp:
        m = re.search(r"goHompage\('([^']+)'\)", r.text)
        hp = m.group(1) if m else ""
    m = re.search(r"(https?://[^\s'\"]+)", hp)
    hp = m.group(1) if m else hp
    # 종업원수
    emp = ""
    m = re.search(r"([0-9][0-9,]{1,8})", _by_th("종업원"))
    if m:
        emp = m.group(1)
    # 대표이사 — 경영진 텍스트에서 '대표이사 홍길동'
    ceo = ""
    m = re.search(r"대표이사\s*([가-힣]{2,4})", txt)
    if m:
        ceo = m.group(1)
    return {"established": est, "homepage": hp, "ceo": ceo, "employees": emp}


def profile(code: str) -> dict:
    try:
        return _profile(code, date.today().isoformat())
    except Exception as e:
        _log.warning("기업개요 fetch 실패 %s: %s", code, e)
        return {"established": "", "homepage": "", "ceo": "", "employees": ""}

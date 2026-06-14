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
from datetime import date, timedelta
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
@lru_cache(maxsize=256)
def _reports(code: str, _day: str) -> list[dict]:
    r = requests.get("https://finance.naver.com/research/company_list.naver",
                     params={"searchType": "itemCode", "itemCode": code},
                     headers=_H, verify=_C, timeout=15)
    r.encoding = "euc-kr"
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
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
        # 원문: PDF 직접 링크 우선, 없으면 네이버 리포트 상세 페이지
        pdf = tr.find("a", href=lambda h: h and ".pdf" in h.lower())
        url = pdf["href"] if pdf else "https://finance.naver.com/research/" + a["href"]
        out.append({"date": d, "title": title, "broker": broker, "url": url})
        if len(out) >= 15:
            break
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


# ── 추정 실적 (FnGuide 연간 Financial Highlight) ─────────────────────────────
_EARNINGS_ITEMS = ("매출액", "영업이익", "당기순이익", "지배주주", "EBITDA")


_YEAR_RE = re.compile(r"(\d{4}/\d{2}(?:\(E\))?)$")  # '2025/12' 또는 '2026/12(E)'


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
    # FnGuide 연간 Financial Highlight(highlight_D_Y) — 5년 확정 + 3년 추정(E).
    # 출처 그대로 표시: 일부 종목(삼성전자 등)은 FnGuide 원본 추정치가 비정상일 수
    # 있으나 우리 크롤링은 정확. 데이터 제공처 이슈는 출처 명시로 대응한다.
    r = requests.get("https://comp.fnguide.com/SVO2/ASP/SVD_Main.asp",
                     params={"pGB": "1", "gicode": f"A{code}", "cID": "",
                             "MenuYn": "Y", "ReportGB": "", "NewMenuID": "11",
                             "stkGb": "701"}, headers=_H, verify=_C, timeout=20)
    soup = BeautifulSoup(r.text, "html.parser")
    div = soup.find(id="highlight_D_Y")          # 연간 전용 (분기 혼입 없음)
    if div is None:
        return {"years": [], "rows": {}}
    years = [m.group(1) for th in div.select("thead th")
             if (m := _YEAR_RE.search(th.get_text(strip=True)))]
    if not years:
        return {"years": [], "rows": {}}
    rows: dict[str, list] = {}
    for tr in div.select("tbody tr"):
        head = tr.find("th")
        if head is None:
            continue
        item = head.get_text(strip=True)
        key = next((k for k in _EARNINGS_ITEMS if k in item), None)
        if key and key not in rows:
            vals = [_num_cell(td.get_text(strip=True)) for td in tr.find_all("td")]
            rows[key] = vals[:len(years)]
    return {"years": years, "rows": rows}


def earnings(code: str) -> dict:
    try:
        return _earnings(code, date.today().isoformat())
    except Exception as e:
        _log.warning("추정실적 fetch 실패 %s: %s", code, e)
        return {"years": [], "rows": {}}


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

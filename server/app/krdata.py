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


# ── 투자자별 순매수 거래대금 — 데이터엔진 우선 서빙(flow_kr 볼륨) ──────────────────
# 데이터엔진 우선 서빙 원칙(CLAUDE.md §2): flow_kr(전종목 일별 순매수 거래대금·원) 볼륨에서 서빙.
# ⚠ 옛 네이버 frgn 라이브 크롤(단위=주식수)은 은퇴 — flow_kr는 전종목 커버(KRX_ID/PW)이고 단위가
# 거래대금(원)이라, 주식수 라이브를 폴백으로 쓸 수 없다(단위 불일치=오표시). 미커버(신규상장 ≤1일)는 빈 결과.
@lru_cache(maxsize=256)
def _investor_cached(code: str, _day: str) -> list[dict]:
    from quant_core import data_fetcher
    df = data_fetcher.load_stock_flow(code)
    if df is None or df.empty:
        return []
    df = df.copy()
    df.index = pd.to_datetime(df.index, errors="coerce")
    df = df[df.index.notna()].sort_index(ascending=False).head(120)   # 최신 120거래일(원본 오래된→최신)
    out: list[dict] = []
    for ts, row in df.iterrows():
        inst, foreign = _int(row.get("inst_net_buy")), _int(row.get("foreign_net_buy"))
        if inst is None and foreign is None:
            continue
        inst, foreign = inst or 0, foreign or 0
        out.append({"date": ts.strftime("%Y.%m.%d"), "inst": inst, "foreign": foreign,
                    "indiv": -(inst + foreign)})   # 개인 ≈ −(기관+외국인) 거래대금
    return out


def investor(code: str) -> list[dict]:
    """flow_kr 볼륨 → 투자자 순매수 거래대금(원) 최근 120거래일 [{date, inst, foreign, indiv}]. 미커버는 []."""
    try:
        return _investor_cached(code, date.today().isoformat())
    except Exception as e:
        _log.warning("수급 피드 서빙 실패 %s: %s", code, e)
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


# ── 애널 리포트 목록: 데이터엔진 우선 서빙(reports_kr 피드) + 라이브 폴백 ──────────────
# 웹 서빙은 데이터엔진 볼륨 SSOT를 primary로 읽는다(CLAUDE.md §2 데이터엔진 우선 서빙 원칙).
# reports_kr(네이버 전종목 피드)를 우선 서빙하고, 피드 미커버(신규상장·종목명 미해결·미백필)
# 코드만 종목당 라이브 크롤로 폴백. 라이브(_reports)는 6.4초(목록8p+상세15콜)라 폴백 전용 강등.
@lru_cache(maxsize=256)
def _reports_from_feed(code: str, _day: str) -> list[dict]:
    """reports_kr 피드 parquet → 웹 KrReport 목록(최근 12개월·최신순).

    각 항목 {date(YY.MM.DD)·title·broker·url(PDF)·target}. 목표가는 피드가 상세에서 수집(enrich)한
    값 — 미수집(백필 진행분·Not Rated)은 None. 파일 없거나 12개월 내 0건이면 [] → 호출자가 라이브 폴백.
    """
    from quant_core import data_fetcher

    df = data_fetcher.load_stock_reports(code)
    if df is None or df.empty:
        return []
    df = df.copy()
    df["_dt"] = pd.to_datetime(df["as_of"], errors="coerce")
    cutoff = pd.Timestamp(date.today() - timedelta(days=365))
    df = df[df["_dt"].notna() & (df["_dt"] >= cutoff)].sort_values("_dt", ascending=False)
    return [{
        "date": r["_dt"].strftime("%y.%m.%d"),
        "title": str(r.get("title", "")),
        "broker": str(r.get("broker", "")),
        "url": str(r.get("url", "")),
        "target": _int(r.get("target")),      # 피드 상세 수집값(없으면 None)
    } for _, r in df.iterrows()]


def _reports_live(code: str) -> list[dict]:
    """폴백 전용 — reports_kr 피드가 안 덮는 코드(신규상장·종목명 미해결·미백필)용 네이버 종목당 라이브."""
    try:
        return _reports(code, date.today().isoformat())
    except Exception as e:
        _log.warning("리포트 라이브 폴백 실패 %s: %s", code, e)
        return []


def reports(code: str) -> list[dict]:
    # 데이터엔진 우선(원칙6): 피드 파생뷰가 비면 네이버 종목당 라이브 폴백.
    try:
        feed = _reports_from_feed(code, date.today().isoformat())
    except Exception as e:
        _log.warning("리포트 피드 서빙 실패 %s: %s", code, e)
        feed = []
    return feed if feed else _reports_live(code)


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


# ── 컨센서스 카드 — 데이터엔진 우선(reports_kr raw 증권사별 standing) + 라이브 폴백 ──────────
_OPINION_STR = {1: "BUY", 0: "HOLD", -1: "SELL"}


@lru_cache(maxsize=256)
def _consensus_from_feed(code: str, _day: str) -> list[dict]:
    """reports_kr raw → 증권사별 최신 목표가/투자의견 standing.

    [{broker, date, target, prev_target, change_pct, opinion}]. 각 증권사 최신 리포트=현 standing·
    직전 리포트=prev_target. 목표가 없는(None) 리포트 제외. 최신 리포트 증권사 우선 12건.
    """
    from quant_core import data_fetcher
    df = data_fetcher.load_stock_reports(code)
    if df is None or df.empty or "target" not in df.columns:
        return []
    df = df.copy()
    df = df[df["target"].notna()]
    df["_dt"] = pd.to_datetime(df["as_of"], errors="coerce")
    df = df[df["_dt"].notna()]
    if df.empty:
        return []
    rows: list[dict] = []
    for broker, g in df.sort_values("_dt").groupby("broker"):
        latest = g.iloc[-1]
        target = _int(latest["target"])
        prev_target = _int(g.iloc[-2]["target"]) if len(g) > 1 else None
        change_pct = (round((target - prev_target) / prev_target * 100, 2)
                      if target and prev_target else None)
        op = latest.get("opinion")
        rows.append({
            "broker": str(broker), "date": latest["_dt"].strftime("%Y.%m.%d"),
            "target": target, "prev_target": prev_target, "change_pct": change_pct,
            "opinion": (_OPINION_STR.get(_int(op), "") if pd.notna(op) else ""),
        })
    rows.sort(key=lambda r: r["date"], reverse=True)   # 최신 리포트 증권사 우선
    return rows[:12]


def _consensus_live(code: str) -> list[dict]:
    """폴백 전용 — reports_kr 미커버 코드용 네이버 wisereport 라이브(목표가 단위 동일=원)."""
    try:
        return _consensus(code, date.today().isoformat())
    except Exception as e:
        _log.warning("컨센서스 라이브 폴백 실패 %s: %s", code, e)
        return []


def consensus(code: str) -> list[dict]:
    # 데이터엔진 우선(원칙6): reports_kr 증권사별 standing. 비면 네이버 wisereport 라이브 폴백(동일 단위).
    try:
        feed = _consensus_from_feed(code, date.today().isoformat())
    except Exception as e:
        _log.warning("컨센서스 피드 서빙 실패 %s: %s", code, e)
        feed = []
    return feed if feed else _consensus_live(code)


# ── 추정 실적 (FnGuide 스냅샷 JSON — 데이터엔진 estimate_kr 피드 SSOT) ──────────────
# 손익(억원)·멀티플(PER·PBR)·수익성(ROE)·EPS(PEG 계산용). 연간 5년 확정 + 3년 추정(E).
# wcomp getSnpFinancial 파싱은 core estimate_kr가 단독 소유(챗 describe·엑셀과 동일 소스) —
# 옛 krdata 로컬 파싱·중복 크롤·별도 json 캐시를 제거하고 여기서 웹 {years(E)/한글 rows}로 어댑트만 한다.

# estimate_kr 프레임 컬럼 → 웹 KrEarnings 한글 행 키(억원 손익·멀티플·마진).
_EARN_COL2KR = {"rev": "매출액", "op": "영업이익", "ni": "당기순이익", "controlling_ni": "지배주주",
                "per": "PER", "pbr": "PBR", "roe": "ROE", "eps": "EPS",
                "op_margin": "영업이익률", "net_margin": "당기순이익률"}


@lru_cache(maxsize=256)
def _earnings_cached(code: str, _day: str) -> dict:
    from quant_core.data.feeds import estimate_kr
    df = estimate_kr.get(code)                        # 7일 parquet 캐시·연결(C)→별도(I) 폴백
    if df is None or df.empty:
        return {"years": [], "rows": {}}
    est = df["is_estimate"].astype(bool).tolist() if "is_estimate" in df.columns else [False] * len(df)
    years = [f"{y}(E)" if e else str(y) for y, e in zip(df.index, est)]   # 추정 연도 (E) 접미
    rows = {kr: [None if pd.isna(v) else float(v) for v in df[col]]
            for col, kr in _EARN_COL2KR.items() if col in df.columns}
    return {"years": years, "rows": rows}


def earnings(code: str) -> dict:
    """추정실적(FnGuide 컨센서스) — 데이터엔진 estimate_kr 피드 SSOT(원칙6).

    wcomp getSnpFinancial JSON 파싱은 core estimate_kr가 단독 소유(챗 describe·엑셀과 동일
    소스) — 옛 krdata 로컬 파싱·별도 json 캐시는 중복이라 제거하고 프레임을 웹 형태로 어댑트만
    한다. estimate_kr.get이 7일 parquet 캐시·연결(C)→별도(I) 폴백 담당. 무데이터면 빈 결과(가짜
    0 금지·라이브 폴백 없음: wcomp가 유일 forward 이익 소스)."""
    return _earnings_cached(code, date.today().isoformat())


# ── 최근 공시(DART) 제거됨(2026-07-07): kr_extras 응답에 담겼으나 웹 어디서도 렌더하지
# 않던 dead field. 매 요청 DART 왕복만 유발해 summary 로딩을 지연시켜 소스에서 제거.
# 다시 필요하면 git 이력(krdata._disclosures)에서 복원 — DART list.json은 전종목 1콜형이라
# 재도입 시엔 종목당 크롤 아닌 데이터엔진 피드로(원칙6·Phase 4 참조).


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

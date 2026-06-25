"""산업(섹터) 밸류체인 분석 — 포트폴리오 대시보드(Streamlit)에서 이식.

industry_<산업>.csv(밸류체인 구분/단계/세부분류 · 기업 · 주요제품 · 매출/영업이익)에
FDR 라이브 시가총액·전일대비 등락률을 병합해 트리맵·기업표 데이터를 만든다.
2차전지부터 구축, 이후 동일 구조로 다른 산업 CSV를 INDUSTRIES에 추가.

캐싱: lru_cache(일자 키)로 산업별 일 1회만 StockListing/CSV 조회.
"""
from __future__ import annotations

import csv
import logging
import os
from datetime import date
from functools import lru_cache

import requests

_log = logging.getLogger("app.industry")
_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# 지원 산업 — CSV 파일 추가 시 여기에 등록(동일 컬럼 구조).
INDUSTRIES = {
    "2차전지": "industry_2차전지.csv",
    "반도체": "industry_반도체.csv",
    "전자부품": "industry_전자부품.csv",
    "건설": "industry_건설.csv",
    "금융": "industry_금융.csv",
    "석유화학": "industry_석유화학.csv",
    "화장품": "industry_화장품.csv",
    "미디어엔터테인먼트": "industry_미디어엔터테인먼트.csv",
    "교육출판업": "industry_교육출판업.csv",
    "식음료": "industry_식음료.csv",
    "건강기능식품": "industry_건강기능식품.csv",
    "유틸리티": "industry_유틸리티.csv",
}


@lru_cache(maxsize=1)
def _product_map() -> dict:
    """티커 → 주요제품(핵심 사업 한 줄 요약). 모든 산업 CSV에서 1회 로드.
    기업개요(/profile)의 '주요 사업' 칸이 산업표와 동일 문구를 쓰도록 공유."""
    m: dict = {}
    for fname in INDUSTRIES.values():
        path = os.path.join(_DIR, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    tk = str(r.get("티커", "")).strip()
                    tk = tk.zfill(6) if tk.isdigit() else tk
                    p = (r.get("주요제품") or "").strip()
                    if tk and p:
                        m[tk] = p
        except Exception:
            pass
    return m


def product_of(ticker: str) -> str:
    tk = str(ticker).strip()
    tk = tk.zfill(6) if tk.isdigit() else tk
    return _product_map().get(tk, "")


def shares_of(ticker: str):
    """상장주식수(코드별). 시총 = 현재가 × 주식수 계산용. 전 KRX 사전(_shares, 일 캐시) 재사용."""
    tk = str(ticker).strip()
    tk = tk.zfill(6) if tk.isdigit() else tk
    return _shares(date.today().isoformat()).get(tk)


@lru_cache(maxsize=1)
def _ticker_industry() -> dict:
    """티커 → 산업명. 모든 산업 CSV에서 1회 로드(같은 종목이 여러 산업이면 첫 등장 우선)."""
    m: dict = {}
    for name, fname in INDUSTRIES.items():
        path = os.path.join(_DIR, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    tk = str(r.get("티커", "")).strip()
                    tk = tk.zfill(6) if tk.isdigit() else tk
                    if tk:
                        m.setdefault(tk, name)
        except Exception:
            pass
    return m


def industry_of(ticker: str) -> str | None:
    """해당 종목이 속한 산업명(없으면 None). HOME 경쟁사분석이 현재 종목의 산업을 자동 인식."""
    tk = str(ticker).strip()
    tk = tk.zfill(6) if tk.isdigit() else tk
    return _ticker_industry().get(tk)

# 밸류체인 정렬 순서 (Upstream → Midstream → Downstream → 단계)
# 단계는 산업마다 다르나 gu가 먼저 정렬되고 각 산업은 자기 단계만 가져 값이 겹쳐도 무방.
_GU_ORDER = {"Upstream": 0, "Midstream": 1, "Downstream": 2}
_DAN_ORDER = {
    # 2차전지
    "원자재": 0, "소재": 1, "배터리": 2, "부품": 3, "장비": 4, "리사이클": 5, "애플리케이션": 6,
    # 반도체
    "설계": 7, "제조": 8, "후공정": 9, "기판": 10,
    # 전자부품
    "기판(PCB)": 0, "수동부품": 1, "접속부품": 2, "카메라·광학": 3, "세트부품": 4,
    # 건설
    "건설자재": 0, "종합건설": 1, "후방·서비스": 2,
    # 금융
    "은행·지주": 0, "증권": 1, "보험": 2, "여신·소비자금융": 3, "금융서비스": 4,
    # 석유화학
    "기초유분": 0, "기초유기": 1, "기초무기": 2,
    "합성수지": 3, "합성고무": 4, "합성섬유": 5, "염료·안료": 6,
    "도료·잉크": 7, "계면활성제·세제": 8, "비료·농약": 9, "유지·바이오": 10, "기능성소재": 11,
    # 화장품
    "원료": 0, "ODM·OEM": 1, "브랜드": 2,
    # 미디어엔터테인먼트
    "제작": 0, "유통·플랫폼": 1, "상영·소비": 2,
    # 교육출판업
    "출판": 0, "교재·콘텐츠": 1, "교육서비스": 2,
}


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


@lru_cache(maxsize=8)
def _shares(_day: str) -> dict:
    """전 KRX 종목 상장주식수 사전(코드→주식수). 시총 = 정확한 종가 × 주식수 계산용.

    fdr.StockListing의 Close·ChagesRatio는 장중·지연 스냅샷이라 일부 종목이 실제 종가/등락과
    다르다(예: LG엔솔 412,500 vs 실종가 410,500). 그래서 시총·등락은 DataReader(_quotes)로 따로
    구하고, 여기선 일중 변하지 않는 상장주식수(Stocks)만 가져온다."""
    import FinanceDataReader as fdr
    out: dict = {}
    try:
        listing = fdr.StockListing("KRX")
        cols = list(listing.columns)
        ccol = "Code" if "Code" in cols else ("Symbol" if "Symbol" in cols else cols[0])
        scol = next((c for c in ["Stocks", "Shares", "ListedShares"] if c in cols), None)
        if scol is not None:
            for _, r in listing.iterrows():
                out[str(r[ccol]).zfill(6)] = _num(r[scol])
    except Exception as e:  # 외부 소스 한계 — 빈 사전(시총 미표시)
        _log.warning("StockListing(주식수) 조회 실패: %s", e)
    return out


@lru_cache(maxsize=4096)
def _closes(tk: str, _day: str):
    """종목 종가 시계열(약 2.2년) — 일 1회 캐시. 전체 이력 대신 범위 제한으로 콜드 로딩 단축.
    as_of(과거일자)는 이 범위 안에서만 슬라이스(더 과거는 무료소스 한계)."""
    import FinanceDataReader as fdr
    from datetime import timedelta
    start = (date.today() - timedelta(days=820)).isoformat()
    try:
        return fdr.DataReader(tk, start)["Close"].dropna()
    except Exception:
        return None


@lru_cache(maxsize=64)
def _returns(tickers: tuple[str, ...], _day: str, as_of: str = "") -> dict[str, dict | None]:
    """기업별 (as_of 시점) 종가·전일대비 등락(%) + 기간 수익률(5/20/60/120/240일). fdr.DataReader 병렬.

    as_of(yyyy-mm-dd) 지정 시 그 날짜 이하 마지막 거래일 기준으로 계산(과거 트리맵 조회). 미지정=최신.
    종가·등락을 DataReader(=네이버 일치)로 구해 StockListing 스냅샷의 부정확성을 회피한다."""
    from concurrent.futures import ThreadPoolExecutor

    def _one(tk: str):
        try:
            c = _closes(tk, _day)
            if c is None or len(c) < 6:
                return tk, None
            if as_of:
                c = c[c.index <= as_of]          # as_of 이하 거래일만(과거 시점 조회)
                if len(c) < 2:
                    return tk, None
            last = float(c.iloc[-1])
            prev = float(c.iloc[-2])

            def r(n: int):
                return round((last / float(c.iloc[-1 - n]) - 1) * 100, 1) if len(c) > n else None
            d1 = round((last / prev - 1) * 100, 2) if prev else None
            return tk, {"d5": r(5), "d20": r(20), "d60": r(60), "d120": r(120), "d240": r(240),
                        "close": last, "chg": d1}
        except Exception:
            return tk, None

    out: dict[str, dict | None] = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        for tk, v in ex.map(_one, tickers):
            out[tk] = v
    return out


@lru_cache(maxsize=8)
def _snapshot(_day: str) -> dict:
    """전 KRX 종목 스냅샷(코드 → {cap, chg}) — StockListing **1회 벌크** 조회. 트리맵 즉시 렌더용.

    종목별 DataReader(820일 × N종목)는 느려 트리맵 라이브 로딩의 병목이었다. StockListing은
    Marcap(시총)·ChagesRatio(전일대비%)를 전 종목 한 번에 준다 → 라이브여도 1콜로 즉시.
    장중엔 지연 스냅샷일 수 있으나 마감 후엔 공식 종가·시총. 정밀 기간수익률은 별도 지연 로딩."""
    import FinanceDataReader as fdr
    out: dict = {}
    try:
        listing = fdr.StockListing("KRX")
        cols = list(listing.columns)
        ccol = "Code" if "Code" in cols else ("Symbol" if "Symbol" in cols else cols[0])
        has_cap = "Marcap" in cols
        for _, r in listing.iterrows():
            code = str(r[ccol]).zfill(6)
            out[code] = {"cap": _num(r["Marcap"]) if has_cap else None,
                         "chg": _num(r.get("ChagesRatio"))}
    except Exception as e:
        _log.warning("StockListing 스냅샷 조회 실패: %s", e)
    return out


_NV_UA = {"User-Agent": "Mozilla/5.0"}
_NV_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/"


@lru_cache(maxsize=16)
def _naver_quotes(codes: tuple[str, ...], _bucket: str) -> dict:
    """네이버 준실시간 시세(키 불필요) — 코드 → {close, chg}. 트리맵 시세를 거의 실시간으로.

    closePrice=현재가(장중), fluctuationsRatio=등락률%. delayTime 0(준실시간). 비공식 스크래핑이라
    40개씩 배치 콤마 호출로 호출 수·차단 위험을 낮춘다(_bucket로 90초 캐시 → 7초 폴링 권고 준수)."""
    out: dict = {}
    for i in range(0, len(codes), 40):
        batch = codes[i:i + 40]
        try:
            r = requests.get(_NV_URL + ",".join(batch), headers=_NV_UA, timeout=8)
            for d in r.json().get("datas", []):
                code = str(d.get("itemCode", "")).zfill(6)
                close = _num(d.get("closePriceRaw") or d.get("closePrice"))
                chg = _num(d.get("fluctuationsRatioRaw") or d.get("fluctuationsRatio"))
                if close is not None:
                    out[code] = {"close": close, "chg": chg}
        except Exception as e:
            _log.warning("네이버 시세 배치 실패(%s종목): %s", len(batch), e)
    return out


@lru_cache(maxsize=64)
def _industry(name: str, _day: str, as_of: str = "") -> list[dict] | None:
    fname = INDUSTRIES.get(name)
    if not fname:
        return None
    path = os.path.join(_DIR, fname)
    if not os.path.exists(path):
        return None
    rows: list[dict] = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            tk = str(r.get("티커", "")).strip()
            tk = tk.zfill(6) if tk.isdigit() else tk
            rev, op = _num(r.get("매출액")), _num(r.get("영업이익"))
            rows.append({
                "gu": r.get("구분", ""), "stage": r.get("단계", ""),
                "detail": r.get("세부분류", ""), "name": r.get("기업명", ""),
                "ticker": tk, "market": r.get("시장", ""), "product": r.get("주요제품", ""),
                "cap": None, "chg": None, "revenue": rev, "op": op,
                "op_margin": (op / rev * 100) if (rev and op is not None and rev > 0) else None,
            })
    # 시총·등락 조회 — 트리맵을 빠르게 띄우는 게 핵심.
    # · 최신(as_of=""): StockListing **벌크 1콜**(_snapshot)으로 즉시. 종목별 DataReader 안 함.
    # · 과거(as_of): StockListing은 today만 줘서 DataReader 경로(느리지만 정확) 사용.
    # 기간수익률(hover용)·EBITDA(D&A)는 둘 다 느려 별도 엔드포인트로 지연 로딩 — 트리맵·표 먼저 뜨게.
    if as_of:
        from concurrent.futures import ThreadPoolExecutor
        tickers = tuple(x["ticker"] for x in rows)
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_quotes = ex.submit(_returns, tickers, _day, as_of)
            f_shares = ex.submit(_shares, _day)
            quotes, shares = f_quotes.result(), f_shares.result()
        for x in rows:
            q = quotes.get(x["ticker"])
            close = q.get("close") if q else None
            x["chg"] = q.get("chg") if q else None
            sh = shares.get(x["ticker"])
            x["cap"] = (close * sh) if (close and sh) else None
            x["ret"] = {k: q[k] for k in ("d5", "d20", "d60", "d120", "d240")} if q else None
            x["da"] = x["ebitda"] = x["ebitda_margin"] = None
    else:
        # 최신: 네이버 준실시간 시세(close·chg) + 상장주식수(일 캐시)로 시총 = close×주식수.
        # 네이버 실패 종목만 StockListing 스냅샷으로 폴백(장외·차단 대비).
        today = date.today().isoformat()
        tickers = tuple(x["ticker"] for x in rows)
        nv = _naver_quotes(tickers, _day)        # _day=시간버킷 → 90초 신선화
        shares = _shares(today)
        need_fb = any(not (nv.get(x["ticker"], {}).get("close") and shares.get(x["ticker"])) for x in rows)
        snap = _snapshot(today) if need_fb else {}
        for x in rows:
            q = nv.get(x["ticker"]) or {}
            sh = shares.get(x["ticker"])
            if q.get("close") is not None and sh:
                x["cap"] = q["close"] * sh
                x["chg"] = q.get("chg")
            else:                                 # 폴백 — StockListing(지연)
                s = snap.get(x["ticker"]) or {}
                x["cap"] = s.get("cap")
                x["chg"] = s.get("chg")
            x["ret"] = None          # 기간수익률은 industry_returns로 지연 로딩
            x["da"] = x["ebitda"] = x["ebitda_margin"] = None
    # 세부분류 내 시총 점유율(M/s) — 분모=같은 세부분류 시총 합계
    det_sum: dict[str, float] = {}
    for x in rows:
        if x["cap"]:
            det_sum[x["detail"]] = det_sum.get(x["detail"], 0.0) + x["cap"]
    for x in rows:
        s = det_sum.get(x["detail"], 0.0)
        x["ms"] = (x["cap"] / s * 100) if (s and x["cap"]) else None
    rows.sort(key=lambda x: (_GU_ORDER.get(x["gu"], 9), _DAN_ORDER.get(x["stage"], 9),
                             x["detail"], -(x["cap"] or 0)))
    return rows


@lru_cache(maxsize=64)
def _as_of(_day: str, req: str = "") -> str:
    """기준 거래일 = req 이하 마지막 거래일(휴장 시 직전). req 미지정=최신. KOSPI 지수 일자 기준."""
    try:
        import FinanceDataReader as fdr
        idx = fdr.DataReader("KS11").index
        if req:
            sub = idx[idx <= req]
            return str(sub[-1].date()) if len(sub) else req
        return str(idx[-1].date())
    except Exception as e:
        _log.warning("기준일(as_of) 조회 실패, 요청일로 폴백: %s", e)
        return req or _day


# ── EBITDA용 상각비(D&A) — FnGuide 현금흐름표(키 불필요, 로컬·배포 모두 동작) ──────────
# DART(fnlttSinglAcntAll)는 OPENDART_API_KEY가 있어야 해 로컬에선 EBITDA가 비었다. FnGuide
# 재무제표(SVD_Finance, divCashY)는 키 없이 현금흐름표 상각비를 주므로 로컬에서도 채워진다.
_FNG_FIN_URL = "https://comp.fnguide.com/SVO2/ASP/SVD_Finance.asp"
_FNG_H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _da_one(code: str) -> float | None:
    """현금흐름표 D&A = 감가상각비(유형·사용권) + 무형자산상각비. FnGuide 재무제표(divCashY).

    최신 연간(/12) 컬럼 값 사용(단위 억원 → 원). 대손상각·사채상각 등은 계정명으로 제외.
    키가 필요 없어 로컬에서도 EBITDA가 채워진다. 데이터 없으면 None(정직)."""
    from bs4 import BeautifulSoup
    import time as _t
    last_err = None
    for attempt in range(2):                          # FnGuide 간헐적 연결 리셋 → 1회 재시도
        try:
            r = requests.get(_FNG_FIN_URL, params={
                "pGB": "1", "gicode": f"A{code}", "cID": "", "MenuYn": "Y",
                "ReportGB": "", "NewMenuID": "103", "stkGb": "701"},
                headers=_FNG_H, timeout=12)
            if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                r.encoding = r.apparent_encoding       # 헤더에 charset 없을 때만 보정
            soup = BeautifulSoup(r.text, "html.parser")
            div = soup.find(id="divCashY")             # 연간 현금흐름표
            if div is None:
                return None
            cols = [th.get_text(strip=True) for th in div.select("thead th")][1:]  # 첫 칸=라벨 제외
            annual = [i for i, y in enumerate(cols) if y.endswith("/12")]
            if not annual:
                return None

            def _row(pred):
                for tr in div.select("tbody tr"):
                    th = tr.find("th")
                    if th is None:
                        continue
                    nm = th.get_text(" ", strip=True).replace(" ", "")
                    if pred(nm):
                        return [td.get_text(strip=True) for td in tr.find_all("td")]
                return None
            dep = _row(lambda s: "감가상각" in s and "대손" not in s)
            amo = _row(lambda s: ("무형자산" in s and "상각" in s) and "대손" not in s)
            for i in reversed(annual):                 # 최신 연간부터 값 있는 해 선택
                d = _num(dep[i]) if (dep and i < len(dep)) else None
                a = _num(amo[i]) if (amo and i < len(amo)) else None
                if d is not None or a is not None:
                    return ((d or 0.0) + (a or 0.0)) * 1e8   # 억원 → 원
            return None
        except Exception as e:                         # 연결 리셋 등 transient만 재시도
            last_err = e
            if attempt == 0:
                _t.sleep(0.6)
    _log.warning("FnGuide D&A 조회 실패 %s: %s", code, last_err)
    return None


@lru_cache(maxsize=4)
def _da_map(tickers: tuple[str, ...], _day: str) -> dict:
    """종목별 D&A 사전(코드→원). FnGuide 병렬 조회·일 1회 캐시."""
    from concurrent.futures import ThreadPoolExecutor
    out: dict = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for tk, v in zip(tickers, ex.map(_da_one, tickers)):
            out[tk] = v
    return out


_SNAP_TTL = 90   # 초 — 최신 트리맵 시총·등락 신선화 주기. 벌크 StockListing 1콜이라 가벼움.


def _bucket() -> str:
    """현재 시각을 _SNAP_TTL초 버킷으로 — lru_cache 키에 넣어 주기적 재조회(실시간 시세) 유도."""
    import time
    return f"t{int(time.time() // _SNAP_TTL)}"


def industry(name: str, as_of: str | None = None) -> list[dict] | None:
    """산업 밸류체인 데이터.

    · as_of 지정(과거 트리맵): 일자 캐시(그 시점 시총·등락 고정).
    · 미지정(최신): **시간 버킷 키**로 캐시 → _SNAP_TTL(90초)마다 _snapshot 재조회해 **시세가
      장중 실시간으로 갱신**된다(이전엔 _day 키로 종일 고정돼 안 바뀌던 문제 해소)."""
    try:
        if as_of:
            return _industry(name, date.today().isoformat(), as_of)
        return _industry(name, _bucket(), "")
    except Exception as e:
        _log.warning("산업 분석 fetch 실패 %s: %s", name, e)
        return None


@lru_cache(maxsize=4)
def _industry_ebitda(name: str, _day: str) -> dict:
    """종목별 EBITDA/EBITDA률/D&A — FnGuide 현금흐름표(느림). 지연 로딩 엔드포인트 전용.
    EBITDA = (CSV 영업이익) + D&A, 이익률 = EBITDA/(CSV 매출액). 시장가 무관이라 as_of 불필요."""
    rows = _industry(name, _day, "")
    if not rows:
        return {}
    da = _da_map(tuple(r["ticker"] for r in rows), _day)
    out: dict = {}
    for r in rows:
        d, op, rev = da.get(r["ticker"]), r["op"], r["revenue"]
        eb = (op + d) if (op is not None and d is not None) else None
        out[r["ticker"]] = {
            "da": d, "ebitda": eb,
            "ebitda_margin": (eb / rev * 100) if (eb is not None and rev and rev > 0) else None,
        }
    return out


def industry_ebitda(name: str) -> dict:
    """지연 로딩: 종목별 EBITDA 사전. 실패 시 빈 dict."""
    try:
        return _industry_ebitda(name, date.today().isoformat())
    except Exception as e:
        _log.warning("EBITDA fetch 실패 %s: %s", name, e)
        return {}


@lru_cache(maxsize=8)
def _return_anchors(name: str, _day: str) -> dict:
    """종목별 기간수익률 '앵커' — N거래일 전 종가(5/20/60/120/240) + 최근종가. DataReader, 일 1회(무거움).

    역사 종가는 일중 불변이라 일 캐시. 현재가는 호출 시 네이버 라이브로 덮어 수익률을 산출
    (DataReader '오늘 종가'가 장중 전일고정이던 문제 해소)."""
    from concurrent.futures import ThreadPoolExecutor
    rows = _industry(name, _day, "")
    if not rows:
        return {}

    def one(tk: str):
        c = _closes(tk, _day)
        if c is None or len(c) < 2:
            return tk, None
        def at(n):
            return float(c.iloc[-1 - n]) if len(c) > n else None
        return tk, {"d5": at(5), "d20": at(20), "d60": at(60), "d120": at(120), "d240": at(240),
                    "last": float(c.iloc[-1])}
    out: dict = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        for tk, v in ex.map(one, [r["ticker"] for r in rows]):
            out[tk] = v
    return out


def industry_returns(name: str) -> dict:
    """지연 로딩: 종목별 기간수익률(5/20/60/120/240일). 역사 앵커(일 캐시) + 네이버 현재가로 라이브 산출."""
    try:
        today = date.today().isoformat()
        anchors = _return_anchors(name, today)
        live = _naver_quotes(tuple(anchors.keys()), _bucket())
        out: dict = {}
        for tk, a in anchors.items():
            cur = ((live.get(tk) or {}).get("close")) or (a or {}).get("last")
            if not a or cur is None:
                out[tk] = None
                continue
            out[tk] = {k: (round((cur / a[k] - 1) * 100, 1) if a.get(k) else None)
                       for k in ("d5", "d20", "d60", "d120", "d240")}
        return out
    except Exception as e:
        _log.warning("기간수익률 fetch 실패 %s: %s", name, e)
        return {}


def industry_stream_index(name: str) -> dict:
    """Stream(Up/Mid/Down)별 **시총가중 누적수익률 지수** 시계열(~2.2년 일별).

    각 종목 종가를 시작=1로 정규화 → 같은 스트림 안에서 시총가중 평균 → (지수-1)×100 = 누적수익률%.
    딱 3개의 선(스트림)으로 직관화. 프런트는 선택 기간의 시작점 기준으로 재정규화해 표시(주가추이와 동일 UX)."""
    try:
        import pandas as pd
        from concurrent.futures import ThreadPoolExecutor
        today = date.today().isoformat()
        rows = _industry(name, today, "")
        if not rows:
            return {"dates": [], "streams": {}}
        targets = [(r["ticker"], r.get("gu") or "") for r in rows if (r.get("gu") or "") in _GU_ORDER]
        with ThreadPoolExecutor(max_workers=20) as ex:
            loaded = dict(ex.map(lambda tk: (tk, _closes(tk, today)), [t for t, _ in targets]))
        series: dict = {}
        caps: dict = {}
        by_gu: dict = {}
        for tk, gu in targets:
            c = loaded.get(tk)
            if c is None or len(c) < 30:
                continue
            sh = shares_of(tk)
            cap = (float(c.iloc[-1]) * sh) if sh else None
            if not cap:
                continue
            series[tk] = c
            caps[tk] = cap
            by_gu.setdefault(gu, []).append(tk)
        if not series:
            return {"dates": [], "streams": {}}
        # 모든 종목 종가를 날짜로 정렬·전진보간 후 공통 구간만(전 종목 데이터 존재 시점부터)
        df = pd.DataFrame(series).sort_index().ffill().dropna()
        if len(df) < 2:
            return {"dates": [], "streams": {}}
        norm = df / df.iloc[0]                        # 종목별 시작=1 정규화
        streams: dict = {}
        for gu, tks in by_gu.items():
            tks = [t for t in tks if t in norm.columns]
            if not tks:
                continue
            tot = sum(caps[t] for t in tks)
            idx = sum(norm[t] * (caps[t] / tot) for t in tks)   # 시총가중 평균
            streams[gu] = [round((float(v) - 1) * 100, 2) for v in idx]
        # 종목별 누적수익률 시계열 — 표가 임의 구간(기간/커스텀)을 직접 산출하도록 제공.
        stocks = {t: [round((float(v) - 1) * 100, 2) for v in norm[t]] for t in norm.columns}
        return {"dates": [d.strftime("%Y-%m-%d") for d in df.index], "streams": streams, "stocks": stocks}
    except Exception as e:
        _log.warning("스트림 지수 fetch 실패 %s: %s", name, e)
        return {"dates": [], "streams": {}}


def as_of(req: str | None = None) -> str | None:
    """산업 시총 데이터의 기준 거래일(yyyy-mm-dd). req 지정 시 req 이하 마지막 거래일."""
    try:
        return _as_of(date.today().isoformat(), req or "")
    except Exception:
        return req or None


def refresh_prices() -> None:
    """주가 관련 캐시만 비움 → 새로고침 시 fdr 재조회로 실시간 시세 반영.
    재무(_da_map)·상장주식수(_shares)는 일중 불변이라 유지(불필요한 재조회 방지)."""
    _closes.cache_clear()
    _returns.cache_clear()
    _snapshot.cache_clear()
    _naver_quotes.cache_clear()
    _industry.cache_clear()
    _return_anchors.cache_clear()
    _as_of.cache_clear()

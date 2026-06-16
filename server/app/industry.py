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
INDUSTRIES = {"2차전지": "industry_2차전지.csv"}

# 밸류체인 정렬 순서 (Upstream → Midstream → Downstream → 단계)
_GU_ORDER = {"Upstream": 0, "Midstream": 1, "Downstream": 2}
_DAN_ORDER = {"원자재": 0, "소재": 1, "셀": 2, "부품": 3, "장비": 4,
              "리사이클": 5, "애플리케이션": 6}


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
    """종목 종가 전체 시계열 — 일 1회 캐시. as_of(과거일자)만 바꿀 땐 재조회 없이 잘라 쓴다."""
    import FinanceDataReader as fdr
    try:
        return fdr.DataReader(tk)["Close"].dropna()
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
    with ThreadPoolExecutor(max_workers=12) as ex:
        for tk, v in ex.map(_one, tickers):
            out[tk] = v
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
    # 종가·등락 = DataReader(as_of 시점, 네이버 일치) / 시총 = 종가 × 상장주식수(StockListing Stocks)
    quotes = _returns(tuple(x["ticker"] for x in rows), _day, as_of)
    shares = _shares(_day)
    for x in rows:
        q = quotes.get(x["ticker"])
        close = q.get("close") if q else None
        x["chg"] = q.get("chg") if q else None
        sh = shares.get(x["ticker"])
        x["cap"] = (close * sh) if (close and sh) else None
        x["ret"] = {k: q[k] for k in ("d5", "d20", "d60", "d120", "d240")} if q else None
    # 세부분류 내 시총 점유율(M/s) — 분모=같은 세부분류 시총 합계
    det_sum: dict[str, float] = {}
    for x in rows:
        if x["cap"]:
            det_sum[x["detail"]] = det_sum.get(x["detail"], 0.0) + x["cap"]
    for x in rows:
        s = det_sum.get(x["detail"], 0.0)
        x["ms"] = (x["cap"] / s * 100) if (s and x["cap"]) else None
    # EBITDA = 영업이익 + 현금흐름표 D&A(감가상각비+무형자산상각비), 이익률 = EBITDA/매출액
    da = _da_map(tuple(x["ticker"] for x in rows), _day)
    for x in rows:
        d, op, rev = da.get(x["ticker"]), x["op"], x["revenue"]
        x["da"] = d
        x["ebitda"] = (op + d) if (op is not None and d is not None) else None
        x["ebitda_margin"] = (x["ebitda"] / rev * 100) if (x["ebitda"] is not None and rev and rev > 0) else None
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
    try:
        r = requests.get(_FNG_FIN_URL, params={
            "pGB": "1", "gicode": f"A{code}", "cID": "", "MenuYn": "Y",
            "ReportGB": "", "NewMenuID": "103", "stkGb": "701"},
            headers=_FNG_H, timeout=20)
        if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
            r.encoding = r.apparent_encoding         # 헤더에 charset 없을 때만 보정(과오버라이드 방지)
        soup = BeautifulSoup(r.text, "html.parser")
        div = soup.find(id="divCashY")               # 연간 현금흐름표
        if div is None:
            return None
        cols = [th.get_text(strip=True) for th in div.select("thead th")][1:]   # 첫 칸=라벨 제외
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
        for i in reversed(annual):                   # 최신 연간부터 값 있는 해 선택
            d = _num(dep[i]) if (dep and i < len(dep)) else None
            a = _num(amo[i]) if (amo and i < len(amo)) else None
            if d is not None or a is not None:
                return ((d or 0.0) + (a or 0.0)) * 1e8   # 억원 → 원
        return None
    except Exception as e:
        _log.warning("FnGuide D&A 조회 실패 %s: %s", code, e)
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


def industry(name: str, as_of: str | None = None) -> list[dict] | None:
    """산업 밸류체인 데이터. as_of(yyyy-mm-dd) 지정 시 그 시점 시총·등락으로 계산(과거 트리맵)."""
    try:
        return _industry(name, date.today().isoformat(), as_of or "")
    except Exception as e:
        _log.warning("산업 분석 fetch 실패 %s: %s", name, e)
        return None


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
    _industry.cache_clear()
    _as_of.cache_clear()

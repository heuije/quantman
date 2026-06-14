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
def _marketcaps(_day: str) -> tuple[dict, dict]:
    """전 KRX 종목 시가총액·전일대비 등락률 사전(코드→값). fdr.StockListing 1회."""
    import FinanceDataReader as fdr
    caps: dict[str, float | None] = {}
    chgs: dict[str, float | None] = {}
    try:
        listing = fdr.StockListing("KRX")
        cols = list(listing.columns)
        ccol = "Code" if "Code" in cols else ("Symbol" if "Symbol" in cols else cols[0])
        mcol = next((c for c in ["Marcap", "MarCap", "Marketcap"] if c in cols), None)
        rcol = next((c for c in ["ChagesRatio", "ChangesRatio", "ChangeRatio"] if c in cols), None)
        for _, r in listing.iterrows():
            code = str(r[ccol]).zfill(6)
            if mcol is not None:
                caps[code] = _num(r[mcol])
            if rcol is not None:
                chgs[code] = _num(r[rcol])
    except Exception as e:  # 외부 소스 한계 — 빈 사전(시총·등락률 미표시)
        _log.warning("StockListing 조회 실패: %s", e)
    return caps, chgs


@lru_cache(maxsize=8)
def _returns(tickers: tuple[str, ...], _day: str) -> dict[str, dict | None]:
    """기업별 기간 주가 수익률(%) — 5일/1개월(20)/3개월(60)/6개월(120)/1년(240). fdr 전체이력 병렬."""
    import FinanceDataReader as fdr
    from concurrent.futures import ThreadPoolExecutor

    def _one(tk: str):
        try:
            c = fdr.DataReader(tk)["Close"].dropna()
            if len(c) < 6:
                return tk, None
            last = float(c.iloc[-1])

            def r(n: int):
                return round((last / float(c.iloc[-1 - n]) - 1) * 100, 1) if len(c) > n else None
            return tk, {"d5": r(5), "d20": r(20), "d60": r(60), "d120": r(120), "d240": r(240)}
        except Exception:
            return tk, None

    out: dict[str, dict | None] = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for tk, v in ex.map(_one, tickers):
            out[tk] = v
    return out


@lru_cache(maxsize=8)
def _industry(name: str, _day: str) -> list[dict] | None:
    fname = INDUSTRIES.get(name)
    if not fname:
        return None
    path = os.path.join(_DIR, fname)
    if not os.path.exists(path):
        return None
    caps, chgs = _marketcaps(_day)
    rows: list[dict] = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            tk = str(r.get("티커", "")).strip()
            tk = tk.zfill(6) if tk.isdigit() else tk
            cap, chg = caps.get(tk), chgs.get(tk)
            rev, op = _num(r.get("매출액")), _num(r.get("영업이익"))
            rows.append({
                "gu": r.get("구분", ""), "stage": r.get("단계", ""),
                "detail": r.get("세부분류", ""), "name": r.get("기업명", ""),
                "ticker": tk, "market": r.get("시장", ""), "product": r.get("주요제품", ""),
                "cap": cap, "chg": chg, "revenue": rev, "op": op,
                "op_margin": (op / rev * 100) if (rev and op is not None and rev > 0) else None,
            })
    # 세부분류 내 시총 점유율(M/s) — 분모=같은 세부분류 시총 합계
    det_sum: dict[str, float] = {}
    for x in rows:
        if x["cap"]:
            det_sum[x["detail"]] = det_sum.get(x["detail"], 0.0) + x["cap"]
    for x in rows:
        s = det_sum.get(x["detail"], 0.0)
        x["ms"] = (x["cap"] / s * 100) if (s and x["cap"]) else None
    # 기간 주가 수익률(호버 시 같은 분류 경쟁사 비교용)
    rets = _returns(tuple(x["ticker"] for x in rows), _day)
    for x in rows:
        x["ret"] = rets.get(x["ticker"])
    rows.sort(key=lambda x: (_GU_ORDER.get(x["gu"], 9), _DAN_ORDER.get(x["stage"], 9),
                             x["detail"], -(x["cap"] or 0)))
    return rows


def industry(name: str) -> list[dict] | None:
    try:
        return _industry(name, date.today().isoformat())
    except Exception as e:
        _log.warning("산업 분석 fetch 실패 %s: %s", name, e)
        return None

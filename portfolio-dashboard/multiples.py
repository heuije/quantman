# -*- coding: utf-8 -*-
"""밸류에이션 Multiple — 과거 5개년(Historical) + Forward(컨센서스).

Historical(자동계산): DART 전체재무제표(fnlttSinglAcntAll, 연결 우선) + FinanceDataReader 주가.
  · PER       = 시가총액 ÷ 당기순이익            (순이익 ≤ 0 → 적자 N/A)
  · P/B       = 시가총액 ÷ 자본총계
  · ROE(%)    = 당기순이익 ÷ 자본총계 × 100
  · EV/EBIT   = EV ÷ 영업이익,  EV = 시가총액 + 순차입금(차입금합계 − 현금및현금성자산)
  · EV/EBITDA = EV ÷ (영업이익 + 감가상각비)   ← D&A는 DART 표준화 미흡으로 상당수 N/A(best-effort)
  · PEG       = PER ÷ 순이익 증가율(%, 전년대비)
  과거 시가총액 = 현재 시총 × (해당연도 연말 종가 ÷ 최신 종가)  (상장주식수 불변 가정)

Forward(컨센서스): 네이버 금융 m.stock API의 추정연도(isConsensus=Y) PER·PBR·ROE·EPS.
  · 애널리스트 커버리지가 있는 (주로 대형)종목만 값이 있고, 미커버 종목은 공란.
  · EV/EBIT·EV/EBITDA Forward는 네이버 미제공 → N/A.
"""
import json
import re
import urllib.request

import numpy as np
import pandas as pd

import dart

_API = "https://opendart.fss.or.kr/api"
_HDR = {"User-Agent": "Mozilla/5.0"}
_METRICS = ["PER", "P/B", "ROE", "EV/EBIT", "EV/EBITDA", "PEG"]
_YEARS = [2021, 2022, 2023, 2024, 2025]


def _num(s):
    try:
        v = float(str(s).replace(",", "").strip())
        return v
    except (ValueError, TypeError):
        return None


def _http(url, timeout=20):
    return urllib.request.urlopen(urllib.request.Request(url, headers=_HDR), timeout=timeout).read()


# ── DART 전체 재무제표 파싱 ───────────────────────────────────────────
def _dart_all(cc, base_year):
    """fnlttSinglAcntAll 1회 호출 → {연도: {계정군}}. 당기·전기 2개년만.
    (손익계산서는 보고서당 2개년만 완전 제공 → 전전기는 불완전하므로 사용하지 않음.)"""
    key = dart.get_key()
    out = {}
    for fs in ("CFS", "OFS"):
        url = (f"{_API}/fnlttSinglAcntAll.json?crtfc_key={key}&corp_code={cc}"
               f"&bsns_year={base_year}&reprt_code=11011&fs_div={fs}")
        d = None
        for _try in range(2):  # 일시적 타임아웃 대비 1회 재시도
            try:
                d = json.loads(_http(url, 30))
                break
            except Exception:
                d = None
        if d is None or d.get("status") != "000" or not d.get("list"):
            continue
        yr = {base_year: {}, base_year - 1: {}}
        # 연도별 누적 집계용 초기화
        for y in yr:
            yr[y] = {"EBIT": None, "NI": None, "EQ": None,
                     "CASH": 0.0, "DEBT": 0.0, "DA": 0.0, "_da_hit": False}
        for r in d["list"]:
            sj = r.get("sj_div")
            nm = (r.get("account_nm") or "").replace(" ", "")
            amts = {base_year: _num(r.get("thstrm_amount")),
                    base_year - 1: _num(r.get("frmtrm_amount"))}
            for y, a in amts.items():
                if a is None:
                    continue
                if sj in ("CIS", "IS") and nm == "영업이익" and yr[y]["EBIT"] is None:
                    yr[y]["EBIT"] = a
                elif sj in ("CIS", "IS") and nm.startswith("당기순이익") and yr[y]["NI"] is None:
                    yr[y]["NI"] = a
                elif sj == "BS" and nm == "자본총계" and yr[y]["EQ"] is None:
                    yr[y]["EQ"] = a
                elif sj == "BS" and nm == "현금및현금성자산":
                    yr[y]["CASH"] += a
                elif sj == "BS" and any(k in nm for k in ("차입금", "사채", "리스부채", "유동성장기부채")):
                    yr[y]["DEBT"] += a
                elif sj == "CF" and ("감가상각비" in nm or "무형자산상각" in nm):
                    yr[y]["DA"] += a
                    yr[y]["_da_hit"] = True
        out = yr
        break  # CFS 성공 시 OFS 생략
    return out


def compute_historical(ticker, cmap, caps):
    """한 종목의 5개년 멀티플 dict. {지표: {연도: 값}}."""
    import FinanceDataReader as fdr
    cc = cmap.get(str(ticker).zfill(6))
    res = {m: {y: np.nan for y in _YEARS} for m in _METRICS}
    if not cc:
        return res
    # 최신 보고서 우선(2025→2024→2023)으로 연도별 데이터 채움(first-valid-year wins).
    # 각 호출이 3개년(당기·전기·전전기)을 주므로 한 호출이 실패해도 다른 호출이 보완.
    fin = {}
    for by in (2025, 2024, 2023, 2022):
        try:
            blk = _dart_all(cc, by)
        except Exception:
            blk = {}
        for y, f in (blk or {}).items():
            if y in _YEARS and y not in fin and (
                    f.get("EBIT") is not None or f.get("NI") is not None or f.get("EQ") is not None):
                fin[y] = f
    if not fin:
        return res
    # 연말 시가총액 프록시
    cap_now = caps.get(str(ticker).zfill(6))
    px = {}
    try:
        c = fdr.DataReader(str(ticker).zfill(6))["Close"].dropna()
        last = float(c.iloc[-1])
        for y in _YEARS:
            s = c[c.index <= f"{y}-12-31"]
            px[y] = float(s.iloc[-1]) if len(s) else np.nan
    except Exception:
        last = np.nan
    for y in _YEARS:
        f = fin.get(y)
        if not f:
            continue
        mcap = (cap_now * px.get(y, np.nan) / last) if (cap_now and last and last > 0) else np.nan
        ebit, ni, eq = f.get("EBIT"), f.get("NI"), f.get("EQ")
        cash, debt, da = f.get("CASH", 0.0), f.get("DEBT", 0.0), f.get("DA", 0.0)
        ev = (mcap + debt - cash) if (mcap == mcap) else np.nan
        if ni and ni > 0 and mcap == mcap:
            res["PER"][y] = mcap / ni
        if eq and eq > 0 and mcap == mcap:
            res["P/B"][y] = mcap / eq
        if eq and eq != 0 and ni is not None:
            res["ROE"][y] = ni / eq * 100
        if ebit and ebit > 0 and ev == ev:
            res["EV/EBIT"][y] = ev / ebit
        if f.get("_da_hit") and ebit is not None and (ebit + da) > 0 and ev == ev:
            res["EV/EBITDA"][y] = ev / (ebit + da)
    # PEG: PER ÷ 순이익 증가율(%) (전년 대비)
    for y in _YEARS:
        per = res["PER"].get(y)
        ni0 = fin.get(y, {}).get("NI") if fin.get(y) else None
        ni1 = fin.get(y - 1, {}).get("NI") if fin.get(y - 1) else None
        if per and per == per and ni0 and ni1 and ni1 > 0 and ni0 > 0:
            g = (ni0 / ni1 - 1) * 100
            if g > 0:
                res["PEG"][y] = per / g
    return res


# ── 네이버 컨센서스(Forward) ──────────────────────────────────────────
def fetch_forward(code):
    """네이버 m.stock 추정연도(isConsensus=Y) PER·PBR·ROE·PEG·연도. 미커버 → 빈 dict."""
    code = re.sub(r"^[A-Za-z]+", "", str(code)).zfill(6)
    try:
        d = json.loads(_http(f"https://m.stock.naver.com/api/stock/{code}/finance/annual"))
    except Exception:
        return {}
    fi = d.get("financeInfo", {})
    tr = sorted(fi.get("trTitleList", []), key=lambda t: t.get("key", ""))
    if not tr:
        return {}
    fwd_i = next((i for i, t in enumerate(tr) if str(t.get("isConsensus")).upper() == "Y"), None)
    if fwd_i is None:
        return {}
    fk = tr[fwd_i]["key"]
    pk = tr[fwd_i - 1]["key"] if fwd_i > 0 else None
    rows = {r.get("title"): r.get("columns", {}) for r in fi.get("rowList", [])}

    def val(title, key):
        c = rows.get(title, {}).get(key)
        if not c:
            return None
        v = _num(c.get("value"))
        if v is not None and c.get("cx") == "minus" and v > 0:
            v = -v
        return v

    out = {"연도": tr[fwd_i].get("title", "").replace(".", "")}
    out["PER"] = val("PER", fk)
    out["P/B"] = val("PBR", fk)
    out["ROE"] = val("ROE", fk)
    # 적자(음수 PER)·이상치는 N/A 처리 (Historical과 동일 규칙)
    if out["PER"] is not None and out["PER"] <= 0:
        out["PER"] = None
    if out["P/B"] is not None and out["P/B"] <= 0:
        out["P/B"] = None
    eps_f, eps_p = val("EPS", fk), (val("EPS", pk) if pk else None)
    per_f = out.get("PER")
    if per_f and eps_f and eps_p and eps_p > 0 and eps_f > 0:
        g = (eps_f / eps_p - 1) * 100
        out["PEG"] = (per_f / g) if g > 0 else None
    else:
        out["PEG"] = None
    out["EV/EBIT"] = None      # 네이버 미제공
    out["EV/EBITDA"] = None
    return out


# ── 산업 전체 → 롱 포맷 테이블 ────────────────────────────────────────
def build_multiple_long(csv_path):
    """industry CSV의 모든 종목에 대해 멀티플(5개년+Forward) 롱 데이터프레임 생성."""
    import os
    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig")
    cmap = dart.corp_map()
    # 현재 시총(코드→Marcap)
    import FinanceDataReader as fdr
    caps = {}
    for mkt in ("KRX",):
        try:
            l = fdr.StockListing(mkt)
            ccol = "Code" if "Code" in l.columns else l.columns[0]
            mcol = next((c for c in ["Marcap", "MarCap", "Marketcap"] if c in l.columns), None)
            if mcol:
                for _, r in l.iterrows():
                    caps[str(r[ccol]).zfill(6)] = r[mcol]
        except Exception:
            pass
    tasks = []
    for _, r in df.iterrows():
        tk = str(r["티커"]).strip()
        tk = tk.zfill(6) if tk.isdigit() else tk
        tasks.append((tk, r["기업명"], r.get("단계", ""), r.get("세부분류", "")))

    def _work(t):
        tk, nm, dan, sec = t
        try:
            hist = compute_historical(tk, cmap, caps)
        except Exception:
            hist = {m: {y: np.nan for y in _YEARS} for m in _METRICS}
        try:
            fwd = fetch_forward(tk)
        except Exception:
            fwd = {}
        out = []
        for m in _METRICS:
            row = {"기업명": nm, "티커": tk, "단계": dan, "세부분류": sec, "지표": m}
            for y in _YEARS:
                row[f"FY{y}"] = hist[m][y]
            row["Forward"] = fwd.get(m) if fwd else np.nan
            out.append(row)
        return out

    from concurrent.futures import ThreadPoolExecutor
    recs = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(_work, tasks):  # 입력 순서 보존
            recs.extend(res)
    return pd.DataFrame(recs)

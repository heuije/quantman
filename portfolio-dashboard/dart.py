# -*- coding: utf-8 -*-
"""DART(전자공시) Open API 연동 — 기업 매출액·영업이익 자동 수집.

API 키 보관(둘 중 하나):
  1) 환경변수 DART_API_KEY
  2) 폴더 내 dart_key.txt 파일에 키 한 줄 (← 권장, .gitignore 처리됨)
키를 코드/대화에 직접 넣지 않는다.
"""
import os
import io
import json
import zipfile
import urllib.request
import xml.etree.ElementTree as ET

import pandas as pd

_BASE = os.path.dirname(os.path.abspath(__file__))
_KEYFILE = os.path.join(_BASE, "dart_key.txt")
_API = "https://opendart.fss.or.kr/api"


def get_key():
    k = os.environ.get("DART_API_KEY")
    if k:
        return k.strip()
    if os.path.exists(_KEYFILE):
        try:
            return open(_KEYFILE, encoding="utf-8").read().strip()
        except Exception:
            pass
    return None


def has_key():
    return bool(get_key())


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def corp_map():
    """stock_code(6자리) → corp_code(8자리) 매핑. DART corpCode.xml(zip)."""
    key = get_key()
    if not key:
        return {}
    try:
        raw = urllib.request.urlopen(f"{_API}/corpCode.xml?crtfc_key={key}", timeout=30).read()
        z = zipfile.ZipFile(io.BytesIO(raw))
        root = ET.fromstring(z.read(z.namelist()[0]))
    except Exception as e:
        print(f"[dart corp_map] 실패: {e}")
        return {}
    m = {}
    for it in root.iter("list"):
        sc = (it.findtext("stock_code") or "").strip()
        cc = (it.findtext("corp_code") or "").strip()
        if sc and len(sc) == 6 and cc:
            m[sc] = cc
    return m


def fetch_financials(stock_code, cmap, years=(2025, 2024)):
    """단일회사 주요계정(연결 우선) → 매출액·영업이익. 최신 사업보고서부터 시도."""
    key = get_key()
    if not key:
        return None
    cc = cmap.get(str(stock_code).zfill(6))
    if not cc:
        return None
    for y in years:
        url = (f"{_API}/fnlttSinglAcnt.json?crtfc_key={key}&corp_code={cc}"
               f"&bsns_year={y}&reprt_code=11011")
        try:
            d = json.loads(urllib.request.urlopen(url, timeout=15).read())
        except Exception:
            continue
        if d.get("status") != "000":
            continue
        rows = d.get("list", [])
        cfs = [r for r in rows if r.get("fs_div") == "CFS"]  # 연결 우선, 없으면 전체
        use = cfs if cfs else rows
        rev = op = None
        for r in use:
            nm = (r.get("account_nm") or "").replace(" ", "")
            amt = _num(r.get("thstrm_amount"))
            if rev is None and nm in ("매출액", "수익(매출액)", "영업수익"):
                rev = amt
            if op is None and nm == "영업이익":
                op = amt
        if rev is not None or op is not None:
            return {"매출액": rev, "영업이익": op, "연도": y}
    return None


def update_industry_csv(csv_path):
    """industry CSV의 매출액·영업이익을 DART로 채워 저장. (종목수, 채운수) 반환."""
    df = pd.read_csv(csv_path, dtype=str, encoding="utf-8-sig")
    cmap = corp_map()
    if not cmap:
        return df, 0, 0
    filled = 0
    for i, r in df.iterrows():
        fin = fetch_financials(r["티커"], cmap)
        if fin:
            if fin["매출액"] is not None:
                df.at[i, "매출액"] = int(fin["매출액"])
            if fin["영업이익"] is not None:
                df.at[i, "영업이익"] = int(fin["영업이익"])
            filled += 1
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    return df, len(df), filled

"""DART 전자공시 OpenAPI — 5개년 연결재무제표(PL·BS·CF). FnGuide(3개년) 한계 보완.

fnlttSinglAcntAll(연결 CFS·사업보고서)은 1콜에 3개년(당기·전기·전전기)을 준다. 최신연도와
그 2년 전을 호출해 5개년을 병합한다. corpCode.xml(1회·디스크 캐시)로 종목코드→corp_code 매핑.
키는 settings.OPENDART_API_KEY(server/.env). 값 단위는 원 → 억원(/1e8)으로 환산해 기존 구조와 통일.

반환은 financials의 구조와 동일한 raw(증감률·이익률 행은 financials에서 일괄 부여):
  {fetched, annual:{PL:{periods,[rows]}, BS, CF}, quarterly:{}}
"""
from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from datetime import date
from functools import lru_cache

import requests

from .config import settings

_log = logging.getLogger("app.dart")
_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_CORP_CACHE = os.path.join(_DATA, "dart_corpcode.json")
_BASE = "https://opendart.fss.or.kr/api"
_UA = {"User-Agent": "Mozilla/5.0"}

# 굵게 표기할 주요 합계 계정(표 가독). 공백 제거 후 비교.
_BOLD = {s.replace(" ", "") for s in {
    "매출액", "수익(매출액)", "영업수익", "매출총이익", "영업이익", "당기순이익",
    "법인세비용차감전순이익", "법인세비용차감전계속영업이익", "총포괄손익", "당기총포괄이익",
    "자산총계", "부채총계", "자본총계", "유동자산", "비유동자산", "유동부채", "비유동부채", "자본금",
    "영업활동현금흐름", "투자활동현금흐름", "재무활동현금흐름",
    "영업활동으로인한현금흐름", "투자활동으로인한현금흐름", "재무활동으로인한현금흐름",
    "기말현금및현금성자산", "기초현금및현금성자산",
}}


def _key() -> str:
    return settings.OPENDART_API_KEY


# DART의 ord(XBRL 요소순)는 표시 순서와 달라(예: 손익에 영업외손익이 최상단) 표준 재무제표
# 순서로 재정렬한다. 계정명(공백제거) 정확매칭 우선순위 → 미매칭은 9000+ord로 맨 뒤.
_ORDER = {
    "PL": ["매출액", "수익(매출액)", "영업수익", "매출원가", "매출총이익", "판매비와관리비", "영업이익", "영업이익(손실)",
           "금융수익", "금융원가", "금융비용", "기타수익", "기타비용", "지분법이익(손실)", "지분법손익",
           "영업외수익", "영업외비용", "영업외손익",
           "법인세비용차감전순이익", "법인세비용차감전계속영업이익", "법인세비용", "법인세비용(수익)",
           "계속영업이익", "중단영업이익", "당기순이익", "당기순이익(손실)",
           "기타포괄손익", "총포괄손익", "당기총포괄이익",
           "지배기업소유주지분", "지배기업의소유주지분", "비지배지분"],
    "BS": ["유동자산", "현금및현금성자산", "단기금융상품", "단기투자자산", "매출채권및기타유동채권", "매출채권", "미수금",
           "미수수익", "선급금", "선급비용", "재고자산", "당기법인세자산", "파생상품자산", "기타유동금융자산", "기타유동자산",
           "비유동자산", "장기금융상품", "장기성매출채권", "유형자산", "무형자산", "영업권", "사용권자산", "투자부동산",
           "관계기업및공동기업투자", "관계기업투자", "종속기업투자", "이연법인세자산", "순확정급여자산",
           "기타비유동금융자산", "기타비유동자산", "기타금융자산", "기타자산", "자산총계",
           "유동부채", "매입채무및기타유동채무", "매입채무", "단기차입금", "미지급금", "미지급비용", "미지급법인세",
           "선수금", "예수금", "당기법인세부채", "유동성장기부채", "유동리스부채", "유동충당부채", "파생상품부채",
           "기타유동금융부채", "기타유동부채",
           "비유동부채", "사채", "장기차입금", "순확정급여부채", "장기충당부채", "이연법인세부채", "비유동리스부채",
           "기타비유동금융부채", "기타비유동부채", "부채총계",
           "지배기업소유주지분", "자본금", "신종자본증권", "주식발행초과금", "자본잉여금", "기타불입자본",
           "기타자본구성요소", "기타자본", "기타포괄손익누계액", "이익잉여금", "결손금", "비지배지분",
           "자본총계", "부채및자본총계", "부채와자본총계", "자본과부채총계"],
    "CF": ["영업활동현금흐름", "영업활동으로인한현금흐름", "투자활동현금흐름", "투자활동으로인한현금흐름",
           "재무활동현금흐름", "재무활동으로인한현금흐름",
           "현금및현금성자산의증가", "현금및현금성자산의순증가", "현금및현금성자산의증가(감소)", "현금및현금성자산의감소",
           "외화환산으로인한현금의변동", "환율변동효과", "기초현금및현금성자산", "기말현금및현금성자산"],
}
_RANK = {sj: {n.replace(" ", ""): i for i, n in enumerate(lst)} for sj, lst in _ORDER.items()}


def _order_rank(sj: str, name: str) -> int:
    """표준 표시순서 우선순위(낮을수록 위). 캐노니컬에 없으면 9000(맨 뒤)."""
    return _RANK.get(sj, {}).get((name or "").replace(" ", ""), 9000)


def _num(s):
    s = str(s or "").replace(",", "").strip()
    if not s or s in ("-", "N/A"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


@lru_cache(maxsize=1)
def _corp_map() -> dict:
    """종목코드(6) → DART corp_code(8). corpCode.xml 1회 다운로드 후 디스크+메모리 캐시."""
    try:
        if os.path.exists(_CORP_CACHE):
            with open(_CORP_CACHE, encoding="utf-8") as f:
                m = json.load(f)
            if m:
                return m
    except Exception:
        pass
    m: dict = {}
    try:
        import xml.etree.ElementTree as ET
        r = requests.get(f"{_BASE}/corpCode.xml", params={"crtfc_key": _key()},
                         headers=_UA, timeout=40)
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        root = ET.fromstring(zf.read(zf.namelist()[0]))
        for el in root.iter("list"):
            sc = (el.findtext("stock_code") or "").strip()
            cc = (el.findtext("corp_code") or "").strip()
            if cc and sc and len(sc) == 6 and sc.isdigit():
                m[sc] = cc
        if m:
            os.makedirs(_DATA, exist_ok=True)
            with open(_CORP_CACHE, "w", encoding="utf-8") as f:
                json.dump(m, f)
    except Exception as e:
        _log.warning("DART corpCode 매핑 실패: %s", e)
    return m


def corp_code(ticker: str):
    return _corp_map().get(str(ticker).strip().zfill(6))


def _fetch_year(cc: str, year: int):
    """fnlttSinglAcntAll 연결(CFS)·사업보고서 1콜. 실패 시 None."""
    try:
        r = requests.get(f"{_BASE}/fnlttSinglAcntAll.json", params={
            "crtfc_key": _key(), "corp_code": cc, "bsns_year": str(year),
            "reprt_code": "11011", "fs_div": "CFS"}, headers=_UA, timeout=20)
        d = r.json()
        return d.get("list", []) if d.get("status") == "000" else None
    except Exception as e:
        _log.warning("DART fnlttSinglAcntAll 실패 %s/%s: %s", cc, year, e)
        return None


def fetch(code: str) -> dict | None:
    """종목 5개년 연결재무제표(억원). 실패 시 None(호출측 FnGuide 폴백)."""
    if not _key():
        return None
    cc = corp_code(code)
    if not cc:
        return None
    # 최신 사업보고서 연도(예: 2026 → 2025). 미제출이면 직전연도로.
    y_hi = date.today().year - 1
    c_hi = _fetch_year(cc, y_hi)
    if not c_hi:
        y_hi -= 1
        c_hi = _fetch_year(cc, y_hi)
    if not c_hi:
        return None
    has_is = any(x.get("sj_div") == "IS" for x in c_hi)

    def sj_of(div: str):
        if div == "BS":
            return "BS"
        if div == "CF":
            return "CF"
        if div == "IS":
            return "PL"
        if div == "CIS":
            return None if has_is else "PL"   # IS 있으면 CIS 무시(중복 방지)
        return None

    # sj → {account_key: {nm, ord, vals:{year:원}}}
    store: dict = {}

    def ingest(items, bsns_year):
        for x in items or []:
            sj = sj_of(x.get("sj_div", ""))
            if not sj:
                continue
            aid = (x.get("account_id") or "").strip()
            key = aid if (aid and aid != "-") else "nm:" + (x.get("account_nm") or "")
            try:
                ordn = int(x.get("ord") or 0)
            except (ValueError, TypeError):
                ordn = 0
            slot = store.setdefault(sj, {}).setdefault(
                key, {"nm": (x.get("account_nm") or "").strip(), "ord": ordn, "vals": {}})
            for fld, yr in (("thstrm_amount", bsns_year), ("frmtrm_amount", bsns_year - 1),
                            ("bfefrmtrm_amount", bsns_year - 2)):
                v = _num(x.get(fld))
                if v is not None and yr not in slot["vals"]:
                    slot["vals"][yr] = v

    ingest(c_hi, y_hi)
    ingest(_fetch_year(cc, y_hi - 2), y_hi - 2)   # 2년 전 콜 → 더 과거 2개년 채움

    years = [y_hi - 4, y_hi - 3, y_hi - 2, y_hi - 1, y_hi]
    periods = [f"{y}/12" for y in years]
    annual: dict = {}
    for sj in ("PL", "BS", "CF"):
        slots = sorted(store.get(sj, {}).values(), key=lambda s: (_order_rank(sj, s["nm"]), s["ord"]))
        rows = []
        for s in slots:
            vals = [(s["vals"].get(y) / 1e8 if s["vals"].get(y) is not None else None) for y in years]
            if all(v is None for v in vals):
                continue
            rows.append({"account": s["nm"],
                         "bold": s["nm"].replace(" ", "") in _BOLD,
                         "parent": False, "child": False, "group": None, "values": vals})
        if rows:
            annual[sj] = {"periods": periods, "rows": rows}
    if not annual.get("PL") and not annual.get("BS"):
        return None
    return {"fetched": date.today().isoformat(), "annual": annual, "quarterly": {}}

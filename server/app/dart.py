"""DART 전자공시 OpenAPI — 5개년 연결재무제표(PL·BS·CF). FnGuide(3개년) 한계 보완.

fnlttSinglAcntAll(연결 CFS·사업보고서)은 1콜에 3개년(당기·전기·전전기)을 준다. 최신연도와
그 2년 전을 호출해 5개년을 병합한다. corpCode.xml(1회·디스크 캐시)로 종목코드→corp_code 매핑.
키는 settings.OPENDART_API_KEY(server/.env). 값 단위는 원 → 억원(/1e8)으로 환산해 기존 구조와 통일.

분기도 DART 분기보고서(11013/11012/11014)+사업보고서로 최근 8분기(단일분기) 제공.
반환은 financials의 구조와 동일한 raw(증감률·이익률 행은 financials에서 일괄 부여):
  {fetched, annual:{PL:{periods,[rows]}, BS, CF}, quarterly:{PL,BS,CF}}
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


# DART는 회사·보고서마다 동일 계정을 다른 이름으로 준다(손실 표기·연결 접두사·분기/반기 접두사 등).
# Home/차트·_add_pl_metrics가 찾는 표준명으로 통일(공백제거 후 정확매칭). 값 매칭이라 안전.
_CANON = {n.replace(" ", ""): c for n, c in {
    "영업이익(손실)": "영업이익",
    "당기순이익(손실)": "당기순이익", "연결당기순이익": "당기순이익",
    "분기순이익": "당기순이익", "반기순이익": "당기순이익",
    "분기순이익(손실)": "당기순이익", "반기순이익(손실)": "당기순이익",
    "분기순손실": "당기순이익", "반기순손실": "당기순이익",
    "법인세비용차감전순이익(손실)": "법인세비용차감전순이익",
    "법인세비용차감전계속영업이익(손실)": "법인세비용차감전계속영업이익",
    "기본주당이익": "기본주당순이익", "기본주당이익(손실)": "기본주당순이익",
    "기본주당순이익(손실)": "기본주당순이익",
    "지배기업소유지분": "지배기업소유주지분",
    "분기총포괄손익": "총포괄손익", "반기총포괄손익": "총포괄손익",
    # 공시자 표준코드 미태깅 시 총포괄손익을 이익/손익/당기·분기 접두 등으로 달리 표기 → 통일.
    "총포괄이익": "총포괄손익", "총포괄이익(손실)": "총포괄손익", "총포괄손익(손실)": "총포괄손익",
    "당기총포괄이익": "총포괄손익", "당기총포괄손익": "총포괄손익",
    "분기총포괄이익": "총포괄손익", "반기총포괄이익": "총포괄손익",
    # 순이익 귀속 분해 명칭 변형 → 표준 귀속명(당기순이익 = 지배 + 비지배 항등식 도출용). ⚠ 반드시
    # '순이익' 문맥 명칭만 — 포괄손익 귀속(지배주주지분)·자본 지분과 충돌하면 안 되므로 바꾸지 않는다.
    "지배회사지분순이익": "지배기업소유주지분", "지배회사지분순이익(손실)": "지배기업소유주지분",
    "지배기업의소유주지분순이익": "지배기업소유주지분", "지배주주지분순이익": "지배기업소유주지분",
    "지배기업소유주지분순이익": "지배기업소유주지분",
    "비지배지분순이익": "비지배지분", "비지배지분순이익(손실)": "비지배지분",
    "비지배주주지분순이익": "비지배지분",
    # '지배기업의 소유주에게 귀속되는 X순이익' / '비지배지분에 귀속되는 X순이익' — 당기·분기·반기 접두 ×
    # (손실) 유무. 분기·반기 보고서는 '분기/반기순이익'으로 표기(같은 계정) → 전부 표준 귀속명으로.
    **{f"지배기업의소유주에게귀속되는{p}순이익{s}": "지배기업소유주지분"
       for p in ("당기", "분기", "반기") for s in ("", "(손실)")},
    **{f"지배기업소유주에게귀속되는{p}순이익{s}": "지배기업소유주지분"
       for p in ("당기", "분기", "반기") for s in ("", "(손실)")},
    **{f"비지배지분에귀속되는{p}순이익{s}": "비지배지분"
       for p in ("당기", "분기", "반기") for s in ("", "(손실)")},
}.items()}


def _canon_account(nm: str) -> str:
    """계정명을 Home이 매칭하는 표준명으로 정규화(미등록은 원본 유지)."""
    return _CANON.get((nm or "").replace(" ", ""), (nm or "").strip())


# account_id → 표준 계정명(정렬·차트매칭용 canon). 회사마다 account_nm이 달라도(영업이익 vs 영업손익,
# 당기순이익 vs 당기순손익 등) account_id는 IFRS/DART 표준이라 일관 — PL 표시순서·차트매칭을 이걸로 정한다.
# (표시는 원본 account_nm 그대로. canon은 순서·매칭 전용.)
_AID_NAME = {
    "ifrs-full_Revenue": "매출액", "ifrs-full_RevenueFromSaleOfGoods": "매출액", "ifrs-full_RevenueFromRenderingOfServices": "매출액",
    "ifrs-full_CostOfSales": "매출원가",
    "ifrs-full_GrossProfit": "매출총이익",
    "dart_TotalSellingGeneralAdministrativeExpenses": "판매비와관리비",
    "ifrs-full_SellingGeneralAndAdministrativeExpense": "판매비와관리비",
    "dart_OperatingIncomeLoss": "영업이익", "ifrs-full_ProfitLossFromOperatingActivities": "영업이익",
    "ifrs-full_FinanceIncome": "금융수익", "ifrs-full_FinanceCosts": "금융원가",
    "ifrs-full_OtherIncome": "기타수익", "dart_OtherGains": "기타수익",
    "ifrs-full_OtherExpenseByNature": "기타비용", "dart_OtherLosses": "기타비용",
    "ifrs-full_ShareOfProfitLossOfAssociatesAndJointVenturesAccountedForUsingEquityMethod": "지분법손익",
    "ifrs-full_ProfitLossBeforeTax": "법인세비용차감전순이익",
    "ifrs-full_IncomeTaxExpenseContinuingOperations": "법인세비용",
    "ifrs-full_ProfitLossFromContinuingOperations": "계속영업이익",
    "ifrs-full_ProfitLossFromDiscontinuedOperations": "중단영업이익",
    "ifrs-full_ProfitLoss": "당기순이익",
    "ifrs-full_ProfitLossAttributableToOwnersOfParent": "지배기업소유주지분",
    "ifrs-full_ProfitLossAttributableToNoncontrollingInterests": "비지배지분",
    "ifrs-full_OtherComprehensiveIncome": "기타포괄손익",
    "ifrs-full_ComprehensiveIncome": "총포괄손익",
    "ifrs-full_BasicEarningsLossPerShare": "기본주당순이익(원)",
    "ifrs-full_DilutedEarningsLossPerShare": "희석주당순이익",
}


def _slot_canon(slot: dict) -> str:
    """slot의 표준명 — account_id 우선(회사 불문 일관), 없으면 계정명 기반 정규화."""
    return _AID_NAME.get(slot.get("aid") or "", "") or _canon_account(slot.get("nm") or "")


# DART가 공시자 미태깅 시 account_id에 넣는 placeholder — 진짜 표준코드가 아니므로 병합키로 쓰면 안 된다.
_AID_PLACEHOLDER = {"", "-", "-표준계정코드 미사용-"}
# 우리가 정규화·매칭하는 표준 계정명 집합(account_id 맵의 값 + 이름 정규화 맵의 값).
_STD_NAMES = frozenset(_AID_NAME.values()) | frozenset(_CANON.values())


def _merge_key(aid: str, nm: str) -> str:
    """보고서 간 계정 병합 키. 인식된 표준계정은 canon으로 묶어, 같은 계정이 보고서마다
    account_id를 다르게 줘도(한 해는 ifrs-full_ProfitLoss, 다른 해는 '-표준계정코드 미사용-')
    하나로 병합한다 — 안 그러면 옛 보고서가 채우는 과거연도가 통째로 유실(노바렉스 당기순이익
    2021·2022 근본수정). 미인식 상세계정은 실 account_id 우선(연도별 계정명 변화에 견고),
    placeholder·누락 id만 계정명으로 — 그래야 placeholder 계정들이 한 키로 뭉치지 않는다."""
    aid = (aid or "").strip()
    canon = _AID_NAME.get(aid)
    if not canon:
        cn = _canon_account(nm)          # 이름 변형 → 표준명(아니면 원본)
        if cn in _STD_NAMES:
            canon = cn
    if canon:
        return "canon:" + canon
    if aid not in _AID_PLACEHOLDER:
        return aid
    return "nm:" + (nm or "").strip()


def _ordered_slots(sj: str, slots):
    """표시 순서 정렬. BS·CF는 DART 문서순서(ord)가 곧 표시순서·계층(소계 바로 아래 하위계정)
    이라 그대로 — 전자공시 그대로의 구조·합계 일치(유동자산=하위계정 합, 활동현금흐름=하위계정 합).
    PL(IS)만 DART ord가 XBRL 요소순이라 뒤죽박죽(매출액이 맨 뒤)이므로 표준 표시순서(_ORDER)."""
    slots = list(slots)
    if sj in ("BS", "CF"):
        return sorted(slots, key=lambda s: s["ord"])
    return sorted(slots, key=lambda s: (_order_rank(sj, _slot_canon(s)), s["ord"]))


def _fill_ni_from_attribution(pl: dict, field: str) -> None:
    """포괄손익계산서가 연결 당기순이익 '합계'를 빼고 귀속 분해(지배기업소유주지분+비지배지분)만
    줄 때(현대차·노바렉스 등, 합계는 자본변동표에만 있어 필터됨) 당기순이익 = 지배 + 비지배
    회계 항등식으로 빈 기간을 채운다. 반기·분기 보고서뿐 아니라 옛 사업보고서 comparatives도
    같은 형태라 연간·분기 store 모두에 적용(field='vals' 연간 / 'cum' 분기). 이미 합계가 있는
    기간은 건드리지 않는다. 지배지분 자체가 없으면(귀속 분해 미제공) 손대지 않는다."""
    own = pl.get("canon:지배기업소유주지분")
    if not (own and own.get(field)):
        return
    nci = pl.get("canon:비지배지분")
    ni = pl.get("canon:당기순이익")
    if ni is None:
        ni = pl["canon:당기순이익"] = {
            "nm": "당기순이익", "aid": "ifrs-full_ProfitLoss", "ord": own["ord"], field: {}}
    tgt = ni.setdefault(field, {})
    nvals = nci.get(field, {}) if nci else {}
    for k, ov in own[field].items():
        if k not in tgt:
            tgt[k] = ov + nvals.get(k, 0.0)


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


def _fetch_report(cc: str, year: int, reprt_code: str):
    """fnlttSinglAcntAll 1콜. reprt_code: 11013(1Q)·11012(반기)·11014(3Q)·11011(사업).

    연결(CFS) 우선, **연결 미작성 법인(카카오뱅크 등 종속회사 없음 — status 013)은 별도(OFS)로
    폴백**한다. CFS 하드코딩 시절엔 이런 회사가 전부 실패해 FnGuide 3개년으로 떨어졌다(재무제표
    계정이 안 나오던 근본 원인). 연결 작성 회사는 전 연도 CFS, 미작성 회사는 전 연도 OFS라
    연도 간 연결/별도가 섞이지 않는다.

    일시적 연결 리셋(ConnectionError)은 1회 재시도 — 연도×보고서 다회 호출 시 끊김 대비."""
    import time
    for fs_div in ("CFS", "OFS"):
        for attempt in range(2):
            try:
                r = requests.get(f"{_BASE}/fnlttSinglAcntAll.json", params={
                    "crtfc_key": _key(), "corp_code": cc, "bsns_year": str(year),
                    "reprt_code": reprt_code, "fs_div": fs_div}, headers=_UA, timeout=20)
                d = r.json()
                if d.get("status") == "000" and d.get("list"):
                    return d.get("list")
                break                      # 013(데이터 없음) 등 — 다음 fs_div 시도
            except requests.exceptions.RequestException as e:
                if attempt == 0:
                    time.sleep(0.5)
                    continue
                _log.warning("DART fnlttSinglAcntAll 실패 %s/%s/%s/%s: %s", cc, year, reprt_code, fs_div, e)
    return None


def _fetch_year(cc: str, year: int):
    """사업보고서(연간) 1콜."""
    return _fetch_report(cc, year, "11011")


# 분기보고서 reprt_code → 분기번호. 11011(사업보고서)=4분기(연말).
_Q_CODE = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}
_Q_MONTH = {1: "03", 2: "06", 3: "09", 4: "12"}


def _fetch_quarterly(cc: str, y_lo: int, y_cur: int) -> dict:
    """분기 연결재무제표(억원, 단일분기). y_lo년 1분기 ~ 가용 최신분기(연간 연도범위 전체 커버).

    DART 분기보고서는 PL/CF를 '누적'으로 준다(반기=1~6월). 단일분기 = 누적[q] − 누적[q-1]
    (q1은 그대로). 값이 정수(원)라 차감 오차 없음. BS는 분기말 잔액 스냅샷이라 차감 없이 그대로.
    PL 누적은 thstrm_add_amount(없으면 thstrm_amount), CF·사업보고서는 thstrm_amount.
    """
    years = list(range(y_lo, y_cur + 1))
    # store[sj][key] = {nm, ord, cum:{(y,q):원}, snap:{(y,q):원}}
    store: dict = {}

    def ingest(items, y, q, canon=False):
        # canon=True(사업보고서)면 계정명을 권위값으로 덮어씀. DART 분기보고서는 순이익을
        # '분기/반기순이익', 지배지분을 '소유지분' 등으로 줘 연간명과 달라 Home 매칭이 깨지는데,
        # account_id는 동일하므로 연간(11011) 계정명으로 통일한다.
        if not items:
            return
        has_is = any(x.get("sj_div") == "IS" for x in items)
        for x in items:
            div = x.get("sj_div", "")
            if div == "IS":
                sj = "PL"
            elif div == "CIS":
                sj = None if has_is else "PL"      # IS 있으면 CIS 무시(중복 방지)
            elif div == "BS":
                sj = "BS"
            elif div == "CF":
                sj = "CF"
            else:
                continue
            if not sj:
                continue
            aid = (x.get("account_id") or "").strip()
            nm = (x.get("account_nm") or "").strip()
            key = _merge_key(aid, nm)
            try:
                ordn = int(x.get("ord") or 0)
            except (ValueError, TypeError):
                ordn = 0
            slot = store.setdefault(sj, {}).setdefault(
                key, {"nm": nm, "aid": aid, "ord": ordn, "cum": {}, "snap": {}})
            if canon and nm:
                slot["nm"] = nm                    # 연간 계정명 우선(분기/반기 접두사 정규화)
            if sj == "BS":
                v = _num(x.get("thstrm_amount"))
                if v is not None:
                    slot["snap"][(y, q)] = v
            else:
                cum = _num(x.get("thstrm_add_amount"))
                if cum is None:
                    cum = _num(x.get("thstrm_amount"))
                if cum is not None:
                    slot["cum"][(y, q)] = cum

    fetched_any = False
    for y in years:
        for code, q in _Q_CODE.items():
            items = _fetch_report(cc, y, code)
            if items:
                fetched_any = True
                ingest(items, y, q, canon=(code == "11011"))
    if not fetched_any:
        return {}

    # 반기·분기 보고서가 연결 당기순이익 합계를 빼고 귀속 분해만 줄 때 항등식으로 채운다(단일분기
    # = 누적 차감의 전제라 누적 슬롯을 채워야 함). 상세는 _fill_ni_from_attribution 참조.
    _fill_ni_from_attribution(store.get("PL", {}), "cum")

    allqs = sorted({yq for sec in store.values() for slot in sec.values()
                    for yq in list(slot["cum"]) + list(slot["snap"])})
    sel = [yq for yq in allqs if yq[0] >= y_lo]    # 연간 연도범위(y_lo~) 전체 분기
    if not sel:
        return {}
    periods = [f"{y}/{_Q_MONTH[q]}" for (y, q) in sel]

    def disc(slot, y, q):
        """단일분기 = 누적[q] − 누적[q-1] (q1은 그대로). 인접 누적 없으면 None."""
        c = slot["cum"].get((y, q))
        if c is None:
            return None
        if q == 1:
            return c
        p = slot["cum"].get((y, q - 1))
        return None if p is None else c - p

    quarterly: dict = {}
    for sj in ("PL", "BS", "CF"):
        slots = _ordered_slots(sj, store.get(sj, {}).values())
        rows = []
        for s in slots:
            vals = []
            for (y, q) in sel:
                v = s["snap"].get((y, q)) if sj == "BS" else disc(s, y, q)
                vals.append(v / 1e8 if v is not None else None)
            if all(v is None for v in vals):
                continue
            raw = s["nm"]
            canon = _slot_canon(s)
            rows.append({"account": raw, "canon": canon,
                         "bold": canon.replace(" ", "") in _BOLD,
                         "parent": False, "child": False, "group": None, "values": vals})
        if rows:
            quarterly[sj] = {"periods": periods, "rows": rows}
    return quarterly


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
            nm = (x.get("account_nm") or "").strip()
            key = _merge_key(aid, nm)
            try:
                ordn = int(x.get("ord") or 0)
            except (ValueError, TypeError):
                ordn = 0
            slot = store.setdefault(sj, {}).setdefault(
                key, {"nm": nm, "aid": aid, "ord": ordn, "vals": {}})
            for fld, yr in (("thstrm_amount", bsns_year), ("frmtrm_amount", bsns_year - 1),
                            ("bfefrmtrm_amount", bsns_year - 2)):
                v = _num(x.get(fld))
                if v is not None and yr not in slot["vals"]:
                    slot["vals"][yr] = v

    ingest(c_hi, y_hi)
    ingest(_fetch_year(cc, y_hi - 2), y_hi - 2)   # 2년 전 콜 → 더 과거 2개년 채움

    # 옛 사업보고서 comparatives도 포괄손익계산서에 연결 당기순이익 합계를 빼고 귀속 분해만 주는
    # 경우가 있어 과거연도 당기순이익이 빈다 — 지배 + 비지배 항등식으로 채운다(분기와 동일 로직).
    _fill_ni_from_attribution(store.get("PL", {}), "vals")

    years = [y_hi - 4, y_hi - 3, y_hi - 2, y_hi - 1, y_hi]
    periods = [f"{y}/12" for y in years]
    annual: dict = {}
    for sj in ("PL", "BS", "CF"):
        slots = _ordered_slots(sj, store.get(sj, {}).values())
        rows = []
        for s in slots:
            vals = [(s["vals"].get(y) / 1e8 if s["vals"].get(y) is not None else None) for y in years]
            if all(v is None for v in vals):
                continue
            raw = s["nm"]                       # 전자공시 원본 계정명 그대로
            canon = _slot_canon(s)               # account_id 기반 표준명(표시는 raw)
            rows.append({"account": raw, "canon": canon,
                         "bold": canon.replace(" ", "") in _BOLD,
                         "parent": False, "child": False, "group": None, "values": vals})
        if rows:
            annual[sj] = {"periods": periods, "rows": rows}
    if not annual.get("PL") and not annual.get("BS"):
        return None
    # 분기는 연간이 보여주는 연도범위(years[0]~) 전체를 단일분기로 — 가용 최신분기까지.
    quarterly = _fetch_quarterly(cc, years[0], date.today().year)
    return {"fetched": date.today().isoformat(), "annual": annual, "quarterly": quarterly}

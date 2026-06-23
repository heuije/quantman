"""static.classification 피드 — KR·US 섹터·업종 (FinanceDataReader).

KR: fdr.StockListing('KRX-DESC')의 Sector(소속부)·Industry(KSIC 업종)를 종목코드별로 저장.
US: fdr.StockListing('S&P500')의 Sector(GICS 섹터)·Industry(GICS 서브산업)를 티커별로 저장.
같은 FDR 라이브러리지만 upstream은 별개(KRX-DESC=한국거래소 / S&P500=Wikipedia GICS) — KRX는
US 종목 섹터를 모른다. 키가 숫자(KR)/알파(US)로 갈려 한 사이드카에 충돌 없이 공존한다.
그룹 블록(get_symbol_group)이 하드코딩 휴리스틱 대신 이 사이드카를 읽는다 — 그룹 기본축은 Industry.

사이드카: 가격 parquet·_manifest.json과 같은 디렉터리(_classification.json).
load 의존: json·pathlib뿐. fetch만 FinanceDataReader를 지연 import.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from ..manifest import default_manifest_path

_SIDECAR = "_classification.json"
_cache: Optional[dict] = None
_cache_mtime: Optional[float] = None


def _path() -> Path:
    return default_manifest_path().parent / _SIDECAR


def _ingest(df, key_col: str, out: dict) -> None:
    """리스팅 DataFrame의 Sector·Industry를 종목 키별로 out에 적재. NaN(float)·빈값 제외."""
    for _, r in df.iterrows():
        key = str(r.get(key_col) or "").strip()
        if not key:
            continue
        rec: dict = {}
        for col in ("Sector", "Industry"):
            v = r.get(col)
            if isinstance(v, str) and v.strip():
                rec[col] = v.strip()
        if rec:
            out[key] = rec


def fetch() -> dict:
    """KR(KRX-DESC)+US(S&P500) Sector·Industry 수급 → 사이드카 저장. 반환 {code/ticker: {...}}.

    KR=KSIC 업종(숫자 종목코드 키·Sector는 소속부), US=GICS Sector/서브산업(알파 티커 키).
    upstream은 별개(KRX-DESC=한국거래소 / S&P500=Wikipedia GICS)지만 둘 다 FDR 단일 호출.
    키가 숫자/알파로 갈려 한 사이드카에 충돌 없이 공존(symbol_group이 키 형태로 시장 구분)."""
    import FinanceDataReader as fdr

    out: dict[str, dict] = {}
    _ingest(fdr.StockListing("KRX-DESC"), "Code", out)     # KR: KSIC 업종 + 소속부
    _ingest(fdr.StockListing("S&P500"), "Symbol", out)     # US: GICS 섹터/서브산업
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def load() -> dict:
    """사이드카 로드(mtime 캐시). cron 갱신 시 자동 재로드. 미수급이면 빈 dict."""
    global _cache, _cache_mtime
    p = _path()
    if not p.exists():
        return {}
    mtime = p.stat().st_mtime
    if _cache is None or mtime != _cache_mtime:
        _cache = json.loads(p.read_text(encoding="utf-8"))
        _cache_mtime = mtime
    return _cache


# ── 표준 섹터(테마) ← KSIC(KR)+GICS(US) 업종 큐레이션 매핑 ──────────────────────
# 사용자가 검색하는 테마("반도체주"·"2차전지주"·"바이오주")를 자유서술 업종에서 정규화한다.
# KR=KSIC 업종("반도체 제조업"), US=GICS 서브산업("Semiconductors") — 한 테마가 양쪽 업종
# 문자열을 모두 나열하면, attribute("Industry")가 KR엔 KSIC·US엔 GICS를 돌려줘도 같은 values
# 리스트로 contains 매칭돼 시장 무관하게 한 번에 잡힌다(예 "반도체"→KSIC+GICS 둘 다 매칭).
# 매핑 안 된 업종은 업종 그대로 폴백(최소 업종 수준 유지).
#
# ⚠ 한계: 한 업종이 여러 테마에 걸치는 혼재 업종(예 KSIC "특수 목적용 기계 제조업"=반도체장비+
# 일반기계)은 못 가르므로 미매핑(종목 단위 보정은 후속). 고수요·명확 매핑만 큐레이션.
# 2차전지·조선은 S&P500에 대응 GICS 서브산업이 없어 KR(KSIC) 전용으로 둔다.
_THEME_TO_INDUSTRIES: dict[str, list[str]] = {
    "반도체": ["반도체 제조업",
              "Semiconductors", "Semiconductor Materials & Equipment"],
    "2차전지": ["일차전지 및 이차전지 제조업"],   # US: S&P500 대응 GICS 없음(KR 전용)
    "제약·바이오": ["의약품 제조업", "기초 의약물질 제조업",
                  "의료용품 및 기타 의약 관련제품 제조업",
                  "Pharmaceuticals", "Biotechnology", "Life Sciences Tools & Services"],
    "의료기기": ["의료용 기기 제조업",
               "Health Care Equipment", "Health Care Supplies"],
    "자동차": ["자동차 신품 부품 제조업", "자동차용 엔진 및 자동차 제조업",
              "자동차 차체나 트레일러 제조업", "자동차 재제조 부품 제조업",
              "Automobile Manufacturers", "Automotive Parts & Equipment",
              "Automotive Retail"],
    "소프트웨어": ["소프트웨어 개발 및 공급업", "컴퓨터 프로그래밍, 시스템 통합 및 관리업",
                "Application Software", "Systems Software",
                "Interactive Home Entertainment"],   # 게임=소프트웨어(KR 동의어와 일치)
    "인터넷": ["자료처리, 호스팅, 포털 및 기타 인터넷 정보매개 서비스업",
             "Interactive Media & Services", "Internet Services & Infrastructure"],
    "전자부품": ["전자부품 제조업",
               "Electronic Components", "Electronic Equipment & Instruments",
               "Electronic Manufacturing Services"],
    "통신장비": ["통신 및 방송 장비 제조업", "Communications Equipment"],
    "철강": ["1차 철강 제조업", "Steel"],
    "비철금속": ["1차 비철금속 제조업", "Copper"],
    "화학": ["기타 화학제품 제조업", "기초 화학물질 제조업",
            "합성고무 및 플라스틱 물질 제조업", "화학섬유 제조업",
            "Specialty Chemicals", "Commodity Chemicals",
            "Fertilizers & Agricultural Chemicals", "Industrial Gases"],
    "조선": ["선박 및 보트 건조업"],                # US: S&P500 대응 GICS 없음(KR 전용)
    "건설": ["건물 건설업", "토목 건설업",
            "Construction & Engineering", "Homebuilding"],
    "방산·항공우주": ["무기 및 총포탄 제조업", "항공기,우주선 및 부품 제조업",
                   "Aerospace & Defense"],
    "은행": ["은행 및 저축기관", "Diversified Banks", "Regional Banks"],
    "보험": ["보험업", "재 보험업",
            "Property & Casualty Insurance", "Life & Health Insurance",
            "Multi-line Insurance", "Insurance Brokers", "Reinsurance"],
    "통신서비스": ["전기 통신업",
                "Integrated Telecommunication Services",
                "Wireless Telecommunication Services"],
    "미디어·엔터": ["영화, 비디오물, 방송프로그램 제작 및 배급업", "텔레비전 방송업",
                  "오디오물 출판 및 원판 녹음업",
                  "Movies & Entertainment", "Broadcasting", "Cable & Satellite",
                  "Publishing"],
    "음식료": ["기타 식품 제조업", "알코올음료 제조업", "비알코올음료 및 얼음 제조업",
              "곡물가공품, 전분 및 전분제품 제조업", "도축, 육류 가공 및 저장 처리업",
              "동물용 사료 및 조제식품 제조업",
              "Packaged Foods & Meats", "Soft Drinks & Non-alcoholic Beverages",
              "Distillers & Vintners"],
}
# 업종 → 테마 역인덱스(한 업종은 한 테마에만 — 큐레이션이 겹치지 않게 보장).
_INDUSTRY_TO_THEME: dict[str, str] = {
    ind: theme for theme, inds in _THEME_TO_INDUSTRIES.items() for ind in inds
}


def symbol_group(sym: str, group_type: str = "Industry") -> Optional[str]:
    """심볼의 산업 분류명. 미수급이면 None. KR(숫자 종목코드)·US(알파 티커)를 키 형태로 가른다.

    group_type="Industry": 업종 원문 — KR=KSIC 업종, US=GICS 서브산업.
    group_type="Sector": 표준 섹터 — 업종(KSIC/GICS)을 큐레이션 테마("반도체" 등)로 정규화.
      미매핑이면 US는 GICS 섹터(rec["Sector"], 표준 11버킷)로, KR은 KSIC 업종으로 폴백.
      KR "Sector" 컬럼(소속부=시장구분)은 산업 섹터가 아니므로 절대 쓰지 않는다(US만 Sector 폴백).
    """
    base = sym.split(".")[0]
    rec = load().get(base)
    if not rec:
        return None
    industry = rec.get("Industry")
    if group_type != "Sector":
        return industry                                     # 업종 원문(KSIC/GICS)
    if industry is not None:
        theme = _INDUSTRY_TO_THEME.get(industry)
        if theme:
            return theme                                    # 큐레이션 테마(KR·US 공통)
    if not base[:1].isdigit():                              # US(알파 시작): GICS 섹터 폴백(소속부 아님)
        return rec.get("Sector") or industry
    return industry                                         # KR(숫자 시작·우선주 00104K 등 포함): KSIC 폴백(소속부 누출 금지)


# ── 사용자 섹터어 → 표준 테마 동의어 (쿼리 정규화) ───────────────────────────────
# 사용자가 쓰는 흔한 표현이 큐레이션 테마명과 다를 때 정규화한다(예: "배터리"≠테마 "2차전지").
# 이게 없으면 챗봇이 "배터리"를 KSIC "일차전지 및 이차전지 제조업"에 contains 매칭 못 해
# 섹터 필터가 침묵의 빈 결과 → "반도체 쏠림"의 진짜 절반. 테마명 자체는 항등, 미상은 원문 유지.
_THEME_SYNONYMS: dict[str, str] = {
    "배터리": "2차전지", "이차전지": "2차전지", "2차전지주": "2차전지", "배터리주": "2차전지",
    "반도체주": "반도체",
    "바이오": "제약·바이오", "제약": "제약·바이오", "바이오주": "제약·바이오",
    "제약주": "제약·바이오", "제약바이오": "제약·바이오",
    "자동차주": "자동차", "완성차": "자동차", "자동차부품": "자동차", "전기차": "자동차",
    "게임": "소프트웨어", "게임주": "소프트웨어", "소프트웨어주": "소프트웨어", "sw": "소프트웨어",
    "인터넷주": "인터넷", "플랫폼": "인터넷",
    "엔터": "미디어·엔터", "엔터테인먼트": "미디어·엔터", "미디어": "미디어·엔터", "방송": "미디어·엔터",
    "방산": "방산·항공우주", "방위산업": "방산·항공우주", "항공우주": "방산·항공우주", "우주": "방산·항공우주",
    "철강주": "철강", "화학주": "화학", "은행주": "은행", "보험주": "보험",
    "통신": "통신서비스", "통신주": "통신서비스", "통신장비주": "통신장비",
    "전자부품주": "전자부품", "조선주": "조선", "건설주": "건설",
    "음식료주": "음식료", "의료기기주": "의료기기", "비철금속주": "비철금속",
}


def available_themes() -> list[str]:
    """챗봇·UI에 노출할 표준 섹터(테마) 목록 — 큐레이션 매핑 키(반도체·2차전지·제약·바이오·…)."""
    return list(_THEME_TO_INDUSTRIES.keys())


def normalize_theme(word: str) -> str:
    """사용자 섹터어 → 표준 테마명. 이미 테마명이면 항등, 동의어면 정규화, 미상이면 원문(소문자도 시도)."""
    w = (word or "").strip()
    if w in _THEME_TO_INDUSTRIES:
        return w
    return _THEME_SYNONYMS.get(w) or _THEME_SYNONYMS.get(w.lower(), w)


def sector_match_values(words: list[str]) -> list[str]:
    """사용자 섹터어 목록 → Industry contains 매칭값 목록(KSIC+GICS). 테마/동의어는 업종으로
    확장(예: '반도체'→'반도체 제조업'(KR)+'Semiconductors'(US)), 미상 단어는 원문 유지(raw 폴백).

    한 values 리스트가 KR(KSIC)·US(GICS) 업종 문자열을 모두 담아 시장 무관하게 섹터 필터가
    빈 결과가 되지 않게 한다 — '반도체 쏠림'/US 미분류 근본수정. 중복 제거·순서 보존."""
    out: list[str] = []
    for word in words:
        w = (word or "").strip()
        if not w:
            continue
        inds = _THEME_TO_INDUSTRIES.get(normalize_theme(w))
        for v in (inds if inds else [w]):
            if v not in out:
                out.append(v)
    return out

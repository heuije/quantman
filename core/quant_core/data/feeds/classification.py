"""static.classification 피드 — KR 섹터·업종 (FinanceDataReader KRX-DESC).

fdr.StockListing('KRX-DESC')의 Sector(소속부)·Industry(업종)를 종목코드별 사이드카로 저장한다.
그룹 블록(get_symbol_group)이 하드코딩 휴리스틱 대신 이 사이드카를 읽는다 — 그룹 기본축은 Industry.
US 섹터는 후속(yfinance .info) — 현재 KR만. 미수급 종목은 소비자가 폴백.

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


def fetch() -> dict:
    """FDR KRX-DESC에서 KR 종목 Sector·Industry 수급 → 사이드카 저장. 반환 {code: {Sector?, Industry?}}."""
    import FinanceDataReader as fdr

    df = fdr.StockListing("KRX-DESC")
    out: dict[str, dict] = {}
    for _, r in df.iterrows():
        code = str(r.get("Code") or "").strip()
        if not code:
            continue
        rec: dict = {}
        for col in ("Sector", "Industry"):
            v = r.get(col)
            if isinstance(v, str) and v.strip():     # NaN(float)·빈값 제외
                rec[col] = v.strip()
        if rec:
            out[code] = rec
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


# ── 표준 섹터(테마) ← KSIC 업종 큐레이션 매핑 ─────────────────────────────────
# 사용자가 검색하는 테마("반도체주"·"2차전지주"·"바이오주")를 KSIC 자유서술 업종
# ("반도체 제조업"·"일차전지 및 이차전지 제조업")에서 정규화한다. KRX-DESC Industry는
# 사용자어와 어휘가 달라("2차전지"⊄"일차전지 및 이차전지 제조업") 부분일치로도 못 잡는 테마가
# 있어 명시 매핑이 필요하다. 매핑 안 된 업종은 KSIC 업종 그대로 폴백(최소 업종 수준 유지).
#
# ⚠ 한계: 한 KSIC가 여러 테마에 걸치는 혼재 업종(예 "특수 목적용 기계 제조업"=반도체장비+
# 일반기계)은 KSIC만으론 못 가르므로 미매핑(종목 단위 보정은 후속). 그래서 한미반도체 등
# 기계로 분류된 반도체 장비주는 아직 "반도체" 테마에 안 들어온다. 고수요·명확 매핑만 큐레이션.
_THEME_TO_INDUSTRIES: dict[str, list[str]] = {
    "반도체": ["반도체 제조업"],
    "2차전지": ["일차전지 및 이차전지 제조업"],
    "제약·바이오": ["의약품 제조업", "기초 의약물질 제조업",
                  "의료용품 및 기타 의약 관련제품 제조업"],
    "의료기기": ["의료용 기기 제조업"],
    "자동차": ["자동차 신품 부품 제조업", "자동차용 엔진 및 자동차 제조업",
              "자동차 차체나 트레일러 제조업", "자동차 재제조 부품 제조업"],
    "소프트웨어": ["소프트웨어 개발 및 공급업", "컴퓨터 프로그래밍, 시스템 통합 및 관리업"],
    "인터넷": ["자료처리, 호스팅, 포털 및 기타 인터넷 정보매개 서비스업"],
    "전자부품": ["전자부품 제조업"],
    "통신장비": ["통신 및 방송 장비 제조업"],
    "철강": ["1차 철강 제조업"],
    "비철금속": ["1차 비철금속 제조업"],
    "화학": ["기타 화학제품 제조업", "기초 화학물질 제조업",
            "합성고무 및 플라스틱 물질 제조업", "화학섬유 제조업"],
    "조선": ["선박 및 보트 건조업"],
    "건설": ["건물 건설업", "토목 건설업"],
    "방산·항공우주": ["무기 및 총포탄 제조업", "항공기,우주선 및 부품 제조업"],
    "은행": ["은행 및 저축기관"],
    "보험": ["보험업", "재 보험업"],
    "통신서비스": ["전기 통신업"],
    "미디어·엔터": ["영화, 비디오물, 방송프로그램 제작 및 배급업", "텔레비전 방송업",
                  "오디오물 출판 및 원판 녹음업"],
    "음식료": ["기타 식품 제조업", "알코올음료 제조업", "비알코올음료 및 얼음 제조업",
              "곡물가공품, 전분 및 전분제품 제조업", "도축, 육류 가공 및 저장 처리업",
              "동물용 사료 및 조제식품 제조업"],
}
# 업종 → 테마 역인덱스(한 업종은 한 테마에만 — 큐레이션이 겹치지 않게 보장).
_INDUSTRY_TO_THEME: dict[str, str] = {
    ind: theme for theme, inds in _THEME_TO_INDUSTRIES.items() for ind in inds
}


def symbol_group(sym: str, group_type: str = "Industry") -> Optional[str]:
    """심볼의 산업 분류명. 미수급이면 None.

    group_type="Sector": 표준 섹터(테마) — KSIC 업종을 큐레이션 테마("반도체" 등)로 정규화,
    미매핑이면 KSIC 업종 폴백. group_type="Industry": KSIC 업종 원문.
    KRX-DESC "Sector" 컬럼(소속부=시장구분)은 산업 섹터가 아니므로 쓰지 않는다.
    """
    rec = load().get(sym.split(".")[0])
    if not rec:
        return None
    industry = rec.get("Industry")
    if industry is None:
        return None
    if group_type == "Sector":
        return _INDUSTRY_TO_THEME.get(industry, industry)   # 테마 정규화, 없으면 업종 폴백
    return industry


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
    """사용자 섹터어 목록 → Industry(KSIC) contains 매칭값 목록. 테마/동의어는 KSIC 업종으로
    확장(예: '배터리'→'일차전지 및 이차전지 제조업'), 미상 단어는 원문 유지(raw 폴백·동작 보존).

    챗봇이 '배터리'라 해도 정확한 KSIC 업종을 잡아 섹터 필터가 빈 결과가 되지 않게 한다 —
    '반도체 쏠림' 근본수정. 중복 제거·순서 보존."""
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

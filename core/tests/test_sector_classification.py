"""섹터 분류 진실원천 — KRX 소속부(시장구분)를 산업 섹터로 쓰지 않음.

FDR KRX-DESC의 "Sector" 컬럼은 *소속부*(우량기업부·벤처기업부·기술성장기업부 등 시장구분)지
산업 섹터가 아니다. 섹터/업종 분류는 "Industry"(KSIC 업종)가 진실원천 — symbol_group이
"Sector" 요청에도 소속부 대신 Industry를 반환해야 표시·필터·그룹이 일관된다.
(prod 실측: "저평가 반도체주" 결과의 섹터 열이 '기술성장기업부'로 떠 반도체주를 오해하게 함.)
"""
from quant_core.data.feeds import classification
from quant_core.expression_parser import get_symbol_group


def test_symbol_group_sector_not_listing_board(monkeypatch):
    # KRX 소속부(기술성장기업부)는 절대 쓰지 않는다 — Industry 기반 표준 섹터(테마)로 답.
    monkeypatch.setattr(classification, "load",
        lambda: {"365590": {"Sector": "기술성장기업부", "Industry": "반도체 제조업"}})
    assert classification.symbol_group("365590", "Industry") == "반도체 제조업"  # 업종 원문
    assert classification.symbol_group("365590", "Sector") == "반도체"           # 표준 섹터 테마
    assert get_symbol_group("365590", "Sector") == "반도체"


def test_symbol_group_none_when_no_industry(monkeypatch):
    # 소속부만 있고 Industry가 없으면 섹터로 쓰지 않는다(소속부 누출 금지) → None.
    monkeypatch.setattr(classification, "load", lambda: {"000001": {"Sector": "우량기업부"}})
    assert classification.symbol_group("000001", "Sector") is None
    assert get_symbol_group("000001", "Sector") == "기타"   # KR 폴백(소속부 아님)


# ── 표준 섹터(테마) 정규화 — KSIC 업종 → 사용자 검색어 테마 ──────────────────

def test_sector_theme_normalizes_ksic_industry(monkeypatch):
    monkeypatch.setattr(classification, "load", lambda: {
        "373220": {"Industry": "일차전지 및 이차전지 제조업"},   # 2차전지
        "207940": {"Industry": "의약품 제조업"},                # 제약·바이오
        "035420": {"Industry": "자료처리, 호스팅, 포털 및 기타 인터넷 정보매개 서비스업"},
    })
    assert classification.symbol_group("373220", "Sector") == "2차전지"
    assert classification.symbol_group("207940", "Sector") == "제약·바이오"
    assert classification.symbol_group("035420", "Sector") == "인터넷"
    # Industry 요청은 KSIC 원문 유지
    assert classification.symbol_group("373220", "Industry") == "일차전지 및 이차전지 제조업"


def test_sector_theme_falls_back_to_industry_when_unmapped(monkeypatch):
    # 혼재 업종(특수 목적용 기계=반도체장비+일반기계)은 미매핑 → KSIC 업종 폴백.
    monkeypatch.setattr(classification, "load",
        lambda: {"042700": {"Industry": "특수 목적용 기계 제조업"}})
    assert classification.symbol_group("042700", "Sector") == "특수 목적용 기계 제조업"


def test_sector_theme_reverse_index_no_duplicate_industry():
    # 한 KSIC 업종이 두 테마에 중복 매핑되면 역인덱스가 모호 — 큐레이션 무결성 가드.
    flat = [ind for inds in classification._THEME_TO_INDUSTRIES.values() for ind in inds]
    assert len(flat) == len(set(flat))

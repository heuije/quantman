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


# ── 사용자 섹터어 동의어 정규화 + KSIC 확장 ("반도체 쏠림" 근본수정) ────────────

def test_normalize_theme_synonyms():
    # 흔한 사용자 표현 → 표준 테마명(테마명≠KSIC 어휘 문제 해소)
    assert classification.normalize_theme("배터리") == "2차전지"
    assert classification.normalize_theme("바이오") == "제약·바이오"
    assert classification.normalize_theme("전기차") == "자동차"
    assert classification.normalize_theme("게임") == "소프트웨어"
    assert classification.normalize_theme("반도체") == "반도체"        # 표준 테마명 항등
    assert classification.normalize_theme("듣보섹터") == "듣보섹터"     # 미상 원문 유지


def test_sector_match_values_expands_to_ksic():
    # 핵심: '배터리'가 KSIC '일차전지 및 이차전지 제조업'으로 확장돼야 섹터 필터가
    # 빈 결과(반도체만 매칭)가 안 된다 — 쏠림 근본수정.
    assert classification.sector_match_values(["배터리", "반도체"]) == \
        ["일차전지 및 이차전지 제조업", "반도체 제조업"]


def test_sector_match_values_unknown_word_raw_fallback():
    # 미상 단어는 원문 유지(raw contains 폴백 — 동작 보존)
    assert classification.sector_match_values(["반도체 제조업"]) == ["반도체 제조업"]
    assert classification.sector_match_values(["듣보"]) == ["듣보"]
    assert classification.sector_match_values([]) == []


def test_available_themes_lists_curated():
    themes = classification.available_themes()
    assert "반도체" in themes and "2차전지" in themes and "제약·바이오" in themes
    assert len(themes) == len(classification._THEME_TO_INDUSTRIES)

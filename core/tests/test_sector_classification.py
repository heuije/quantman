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
    # 반도체는 KSIC(KR)+GICS(US) 서브산업을 모두 반환 → 한 values 리스트로 양 시장 동시 매칭.
    assert classification.sector_match_values(["배터리", "반도체"]) == \
        ["일차전지 및 이차전지 제조업",
         "반도체 제조업", "Semiconductors", "Semiconductor Materials & Equipment"]


def test_sector_match_values_unknown_word_raw_fallback():
    # 미상 단어는 원문 유지(raw contains 폴백 — 동작 보존)
    assert classification.sector_match_values(["반도체 제조업"]) == ["반도체 제조업"]
    assert classification.sector_match_values(["듣보"]) == ["듣보"]
    assert classification.sector_match_values([]) == []


def test_available_themes_lists_curated():
    themes = classification.available_themes()
    assert "반도체" in themes and "2차전지" in themes and "제약·바이오" in themes
    assert len(themes) == len(classification._THEME_TO_INDUSTRIES)


# ── US 섹터 (FDR S&P500 · GICS) — KR과 대칭 ──────────────────────────────────
# US 종목(알파 티커)은 FDR StockListing('S&P500')의 GICS Sector/Industry를 같은
# 사이드카에 저장. Industry=GICS 서브산업(예 'Semiconductors'), Sector=GICS 섹터
# (예 'Information Technology'). symbol_group이 KSIC와 동일 코드경로로 US도 해석한다.

def test_us_symbol_group_industry_and_mapped_theme(monkeypatch):
    """US: Industry=GICS 서브산업 원문, Sector=한글 테마(GICS→테마 역인덱스 매핑)."""
    monkeypatch.setattr(classification, "load", lambda: {
        "NVDA": {"Sector": "Information Technology", "Industry": "Semiconductors"},
        "JPM": {"Sector": "Financials", "Industry": "Diversified Banks"},
    })
    assert classification.symbol_group("NVDA", "Industry") == "Semiconductors"
    assert classification.symbol_group("NVDA", "Sector") == "반도체"   # GICS→한글 테마
    assert classification.symbol_group("JPM", "Sector") == "은행"
    assert get_symbol_group("NVDA", "Sector") == "반도체"


def test_us_unmapped_subindustry_falls_back_to_gics_sector(monkeypatch):
    """미매핑 US 서브산업은 GICS 섹터(표준 11버킷)로 폴백 — '기타'가 아닌 의미있는 섹터.
    KR 소속부 누출 가드(Sector 무시)는 숫자코드(KR) 전용이라 US엔 적용 안 됨."""
    monkeypatch.setattr(classification, "load", lambda: {
        "MMM": {"Sector": "Industrials", "Industry": "Industrial Conglomerates"},
    })
    assert classification.symbol_group("MMM", "Industry") == "Industrial Conglomerates"
    assert classification.symbol_group("MMM", "Sector") == "Industrials"   # GICS 섹터 폴백


def test_us_ticker_absent_other_fallback(monkeypatch):
    """사이드카 미적재 US 티커 → 'Other'(알파), KR 코드 → '기타'(숫자) — 기존 폴백 보존."""
    monkeypatch.setattr(classification, "load", lambda: {})
    assert get_symbol_group("TSLA", "Sector") == "Other"
    assert get_symbol_group("005930", "Sector") == "기타"


def test_sector_match_values_includes_gics_for_us():
    """'반도체'·'소프트웨어'가 GICS 서브산업을 포함 → US 종목(attribute('Industry')=GICS)도
    contains 매칭. 한 values 리스트가 KR(KSIC)·US(GICS) 동시 매칭(시장 무관)."""
    semi = classification.sector_match_values(["반도체"])
    assert "반도체 제조업" in semi and "Semiconductors" in semi      # KR + US
    sw = classification.sector_match_values(["소프트웨어"])
    assert "Application Software" in sw and "Systems Software" in sw


def test_kr_alphanumeric_code_never_leaks_listing_board(monkeypatch):
    """KR 우선주·특수주는 알파 포함 코드('00104K' 등)지만 항상 숫자로 시작 → US로 오인해
    소속부(rec['Sector'])를 섹터로 누출하면 안 된다. 미매핑이면 KSIC 업종으로 폴백."""
    monkeypatch.setattr(classification, "load", lambda: {
        "00104K": {"Sector": "우량기업부", "Industry": "특수 목적용 기계 제조업"},  # 미매핑 KSIC
    })
    assert classification.symbol_group("00104K", "Sector") == "특수 목적용 기계 제조업"  # 소속부 아님
    assert classification.symbol_group("00104K", "Sector") != "우량기업부"

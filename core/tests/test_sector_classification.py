"""섹터 분류 진실원천 — KRX 소속부(시장구분)를 산업 섹터로 쓰지 않음.

FDR KRX-DESC의 "Sector" 컬럼은 *소속부*(우량기업부·벤처기업부·기술성장기업부 등 시장구분)지
산업 섹터가 아니다. 섹터/업종 분류는 "Industry"(KSIC 업종)가 진실원천 — symbol_group이
"Sector" 요청에도 소속부 대신 Industry를 반환해야 표시·필터·그룹이 일관된다.
(prod 실측: "저평가 반도체주" 결과의 섹터 열이 '기술성장기업부'로 떠 반도체주를 오해하게 함.)
"""
from quant_core.data.feeds import classification
from quant_core.expression_parser import get_symbol_group


def test_symbol_group_sector_uses_industry_not_listing_board(monkeypatch):
    monkeypatch.setattr(classification, "load",
        lambda: {"365590": {"Sector": "기술성장기업부", "Industry": "반도체 제조업"}})
    assert classification.symbol_group("365590", "Sector") == "반도체 제조업"
    assert classification.symbol_group("365590", "Industry") == "반도체 제조업"
    assert get_symbol_group("365590", "Sector") == "반도체 제조업"


def test_symbol_group_none_when_no_industry(monkeypatch):
    # 소속부만 있고 Industry가 없으면 섹터로 쓰지 않는다(소속부 누출 금지) → None.
    monkeypatch.setattr(classification, "load", lambda: {"000001": {"Sector": "우량기업부"}})
    assert classification.symbol_group("000001", "Sector") is None
    assert get_symbol_group("000001", "Sector") == "기타"   # KR 폴백(소속부 아님)

"""instrument_region / instrument_category — 심볼→시장/자산군 분류 SSOT.

국장/미장(2분류)이나 sym.isdigit()로는 국내주식·국내선물·해외주식·해외선물 4종을
가를 수 없다(한글 선물 심볼이 비-KR로 오분류). instrument_spec(asset_class×currency)을
단일 출처로 4종을 정확히 구분하는지 검증.
"""
from __future__ import annotations

import pytest

from quant_core.exec_defaults import instrument_category, instrument_region

# (symbol, region, category) — 4종 대표
_CASES = [
    ("005930", "KRX", "kr_equity"),    # 국내주식 (삼성전자)
    ("069500", "KRX", "kr_equity"),    # 국내 ETF (6자리 코드)
    ("코스피200선물", "KRX", "kr_futures"),  # 국내선물
    ("AAPL", "US", "us_equity"),       # 해외주식
    ("NVDA", "US", "us_equity"),       # 해외주식
    ("원유선물", "US", "us_futures"),     # 해외선물 (CME WTI)
    ("금선물", "US", "us_futures"),       # 해외선물 (COMEX GC)
    ("나스닥선물", "US", "us_futures"),    # 해외선물 (CME NQ)
    ("비트코인선물", "US", "us_futures"),  # 해외선물 (CME BTC)
]


@pytest.mark.parametrize("sym,region,category", _CASES)
def test_region_and_category(sym, region, category):
    assert instrument_region(sym) == region, sym
    assert instrument_category(sym) == category, sym


def test_kr_futures_not_classified_as_us():
    """회귀 가드(이번 버그 부류): 한글 선물 심볼이 비-KR/US로 새지 않아야."""
    assert instrument_region("코스피200선물") == "KRX"
    assert instrument_category("코스피200선물") == "kr_futures"


def test_overseas_futures_is_us_not_krx():
    """회귀 가드(잠재 버그): 해외선물(USD)이 안전기본 KRX로 오라우팅되지 않아야."""
    for sym in ("원유선물", "천연가스선물", "금선물", "은선물(COMEX)", "나스닥선물", "비트코인선물"):
        assert instrument_region(sym) == "US", sym
        assert instrument_category(sym) == "us_futures", sym

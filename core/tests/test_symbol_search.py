"""심볼 검색(발견성 — WS5) — search_symbols가 프로덕션 실측 실패 부류를 닫는지.

부류 근거(prod 로그 전수): ① 봇이 종목코드를 추측해 오코드 연쇄(노바렉스·코스맥스엔비티)
② 매크로 심볼이 실재하는데(원달러환율=DEXKOUS) 통용 티커 추측만 하다 포기.
네트워크 0 — 체크인된 core/data/ticker_db.json + data_fetcher 상수만 사용.
"""
from quant_core.ticker_db import search_symbols


def _syms(rows):
    return [r["symbol"] for r in rows]


def test_exact_kr_stock_name_returns_bare_code():
    rows = search_symbols("노바렉스")
    assert rows and rows[0]["symbol"] == "194700"      # .KS 접미 제거된 엔진 심볼키
    assert rows[0]["exchange"] == "KOSDAQ" and rows[0]["kind"] == "주식"


def test_prod_failure_kosmax_nbt_resolves_first_try():
    # prod에서 3연속 오코드(19627·044820…)를 냈던 종목 — 검색 1콜로 해결돼야 한다.
    rows = search_symbols("코스맥스엔비티")
    assert rows and rows[0]["symbol"] == "222040"


def test_sentence_query_contains_name():
    # 봇이 문장째 검색해도(이름⊂질의) 잡혀야 한다 — prod의 research_news 우회를 대체.
    rows = search_symbols("코스맥스엔비티 종목코드")
    assert "222040" in _syms(rows)


def test_macro_symbol_discoverable():
    rows = search_symbols("원달러환율")
    assert rows and rows[0]["symbol"] == "원달러환율" and rows[0]["kind"] == "금리·환율"


def test_partial_macro_query_lists_fx_candidates():
    rows = search_symbols("달러")
    got = _syms(rows)
    assert "원달러환율" in got and ("달러지수" in got or "무역가중달러지수" in got)


def test_exact_beats_prefix():
    rows = search_symbols("삼성전자")
    assert rows[0]["symbol"] == "005930"                # 정확일치가 '삼성전자우'보다 먼저


def test_us_ticker_and_english_name():
    assert _syms(search_symbols("NVDA"))[0] == "NVDA"
    assert "NVDA" in _syms(search_symbols("nvidia"))


def test_futures_and_alias_symbols_indexed():
    assert _syms(search_symbols("코스피200선물"))[0] == "코스피200선물"
    assert "미니코스피200선물" in _syms(search_symbols("미니코스피"))


def test_empty_query_returns_nothing():
    assert search_symbols("") == []
    assert search_symbols("   ") == []


def test_limit_respected():
    assert len(search_symbols("전자", limit=5)) <= 5


def test_sentence_query_prefers_longer_specific_name():
    """이름⊂질의에서 긴(특정적) 이름이 짧은 우연 포함('스맥')을 이긴다 — 실측 랭킹 결함 회귀."""
    rows = search_symbols("코스맥스엔비티 종목코드")
    assert rows[0]["symbol"] == "222040"

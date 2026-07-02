"""coverage_inventory — 검증 매니페스트 → 데이터 유형별 실측 커버리지(챗 화이트리스트) 가드.

챗봇이 "무슨 데이터를 얼마나 갖고 있나"를 하드코딩이 아닌 엔진 실측(매니페스트)에서 안다.
합성 DataManifest로 유형별 뎁스(가격·매크로·sparse 필드)가 뽑히는지, 매니페스트 None(콜드)이
graceful [] 인지 검증한다.

    cd platform && PYTHONPATH=core:server pytest server/tests/test_coverage_inventory.py -q
"""
import pytest

dm = pytest.importorskip("app.data_manifest")
from quant_core.data import DataManifest, SymbolManifest


def _manifest() -> DataManifest:
    """가격(선물·크립토)·매크로(krx·fred)·주식(KR/US)·sparse 필드를 담은 합성 매니페스트."""
    syms: dict[str, SymbolManifest] = {}

    def add(sym, feed=None, first="2015-01-02", last="2024-12-30", n=2500, fc=None):
        syms[sym] = SymbolManifest(symbol=sym, feed=feed, first_date=first,
                                   last_date=last, n_rows=n, field_coverage=fc or {})

    # 가격형(피드 태그) — 주식 KR/US 집계용.
    add("005930", feed="ohlcv.kr", first="2000-01-04", last="2024-12-30", n=6000,
        fc={"pb_ratio": {"first": "2016-03-31", "last": "2024-09-30", "n": 34},
            "inst_net_buy": {"first": "2015-01-02", "last": "2024-12-30", "n": 2400}})
    add("000660", feed="ohlcv.kr", first="2001-06-01", last="2024-12-30", n=5800,
        fc={"pb_ratio": {"first": "2016-03-31", "last": "2024-09-30", "n": 34}})
    add("AAPL", feed="ohlcv.us", first="1990-01-02", last="2024-12-30", n=8000)
    # 가격형 자산(비피드 태그지만 data_type_symbols가 열거) — 선물·크립토.
    add("코스피200선물", first="2010-01-04", last="2024-12-30", n=3600)
    add("S&P500", first="1990-01-02", last="2024-12-30", n=8500)
    add("비트코인", first="2017-08-17", last="2024-12-30", n=2600)
    # 매크로 — krx(상세)·fred·market.
    add("옵션풋콜비율", first="2010-01-04", last="2024-12-27", n=3650)
    add("코스피200변동성지수", first="2010-01-04", last="2024-12-27", n=3650)
    add("VIX", first="1990-01-02", last="2024-12-30", n=8500)
    add("장단기금리차10Y2Y", first="1976-06-01", last="2024-12-30", n=12000)
    return DataManifest(version=1, symbols=syms)


def test_inventory_none_is_graceful_empty():
    """매니페스트 None(콜드스타트)이면 [] — 프롬프트가 인벤토리 섹션 생략."""
    assert dm.coverage_inventory(None) == []


def test_inventory_has_price_and_macro_types():
    inv = dm.coverage_inventory(_manifest())
    by_key = {e["key"]: e for e in inv}
    # 매크로 유형이 실측 뎁스로 잡힌다.
    assert "macro.krx" in by_key
    assert by_key["macro.krx"]["depth"] == "2010-01-04~2024-12-27"
    assert by_key["macro.krx"]["n_symbols"] == 2
    # 가격 자산 유형(선물·크립토).
    assert "ohlcv.futures" in by_key
    assert "ohlcv.crypto" in by_key
    assert by_key["ohlcv.crypto"]["depth"] == "2017-08-17~2024-12-30"


def test_macro_krx_has_per_symbol_detail():
    """매크로는 주요 심볼 개별 뎁스까지 노출(챗이 크로스에셋 참조 시 실측 확인)."""
    inv = dm.coverage_inventory(_manifest())
    krx = next(e for e in inv if e["key"] == "macro.krx")
    detail_syms = {d["symbol"] for d in krx["detail"]}
    assert "옵션풋콜비율" in detail_syms and "코스피200변동성지수" in detail_syms


def test_inventory_aggregates_stocks_kr_us():
    """주식은 KR(숫자코드)/US로 per-symbol 집계된다(동적 유니버스)."""
    inv = dm.coverage_inventory(_manifest())
    by_key = {e["key"]: e for e in inv}
    assert by_key["ohlcv.kr"]["n_symbols"] == 2
    assert by_key["ohlcv.kr"]["depth"] == "2000-01-04~2024-12-30"   # min first ~ max last
    assert by_key["ohlcv.us"]["n_symbols"] == 1


def test_inventory_sparse_fields_have_pct():
    """sparse 필드(펀더/플로우)는 coverage_report pct + range로 노출(null≠0)."""
    inv = dm.coverage_inventory(_manifest())
    by_key = {e["key"]: e for e in inv}
    assert "fundamental.equity" in by_key
    assert by_key["fundamental.equity"].get("pct") is not None
    assert "flow.kr_investor" in by_key


def test_inventory_renders_to_prompt_text():
    """inventory_for_prompt가 유형별 1줄 + 매크로 상세를 문자열로 렌더."""
    inv = dm.coverage_inventory(_manifest())
    txt = dm.inventory_for_prompt(inv)
    assert isinstance(txt, str) and len(txt) > 50
    assert "옵션풋콜비율" in txt        # 매크로 상세 심볼
    assert "종목" in txt                # 종목수 렌더

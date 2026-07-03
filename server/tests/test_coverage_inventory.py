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


# ── 드리프트 가드: 필드형 spec ↔ _FIELD_GROUPS/track_fields 정합 ───────────────
# 심볼형(MACRO/ASSET → data_type_symbols)은 core 가드가 잠근다. 필드형(종목별 컬럼:
# 펀더/수급/컨센서스)은 이 가드가 잠근다 — 새 필드형 피드(예: flow.us_investor)를
# spec에 등록하고 _FIELD_GROUPS에 안 배선하면 CI가 실패한다(공급≫소비 갭의 부류 차단).

def test_every_field_type_spec_wired_to_field_groups():
    from quant_core import get_all_indicator_columns
    from quant_core.data import data_spec
    ind = set(get_all_indicator_columns())
    # 필드형 = per-symbol 지표 컬럼을 제공하는 spec. 판정: pclass P3/P7(펀더·수급 — provides가
    # 참조문자열이어도 필드형) ∪ provides가 실제 지표컬럼과 교집합(예: static.market_cap의
    # trade_value·market_cap). 새 필드형 피드를 spec에 넣고 _FIELD_GROUPS에 안 배선하면 CI 실패.
    field_specs = {s["key"] for s in data_spec()
                   if s["pclass"] in ("P3", "P7") or (set(s.get("provides", [])) & ind)}
    wired = {k for k, _, _ in dm._FIELD_GROUPS}
    missing = field_specs - wired
    assert not missing, (
        f"필드형 spec 중 _FIELD_GROUPS 미배선 — 챗 인벤토리·매니페스트 추적에 안 잡힘:"
        f"\n  {sorted(missing)}\n→ data_manifest._FIELD_GROUPS에 (key, label, cols) 등록하세요."
    )
    ghost = wired - field_specs
    assert not ghost, f"_FIELD_GROUPS에 있으나 spec 미등록(유령 키): {sorted(ghost)}"


def test_field_group_cols_match_indicator_meta():
    """_FIELD_GROUPS의 컬럼들이 지표 컬럼과 일치 — 컬럼 추가/삭제 드리프트 차단.

    추적 컬럼은 **컴퓨티드 데이터셋에 부착되는 지표 컬럼**(원시 피드 컬럼 아님): 공매도는 파생
    short_volume_ratio·시총은 trade_value(market_cap은 펀더 그룹에서 추적)."""
    from quant_core.indicators import (FUND_INDICATOR_COLS, FLOW_INDICATOR_COLS,
                                       CONSENSUS_INDICATOR_COLS, SHORTVOL_INDICATOR_COLS)
    groups = {k: cols for k, _, cols in dm._FIELD_GROUPS}
    assert groups["fundamental.equity"] == set(FUND_INDICATOR_COLS)
    assert groups["flow.kr_investor"] == set(FLOW_INDICATOR_COLS)
    assert groups["estimate.consensus"] == set(CONSENSUS_INDICATOR_COLS)
    assert groups["flow.us_short_volume"] == set(SHORTVOL_INDICATOR_COLS)
    assert groups["static.market_cap"] == {"trade_value"}
    # 추적 대상(TRACK_FIELDS)은 컴퓨티드 데이터셋에 실재하는 지표 컬럼만(원시 피드 컬럼 금지).
    assert set(dm.TRACK_FIELDS) == set().union(*groups.values())
    assert "short_volume" not in dm.TRACK_FIELDS      # 원시 피드 컬럼은 df에 안 붙음


def test_target_floor_surfaced_when_backfill_in_progress():
    """실측 first > spec.floor(Core 2010)이면 target_floor 노출 — 챗이 깊이를 과신하지
    않고 '백필 진행중'을 정직히 안내(위장 truncate 금지 원칙의 표면화)."""
    syms = {"코스피200선물": SymbolManifest(symbol="코스피200선물", first_date="2016-05-25",
                                        last_date="2026-07-01", n_rows=2500,
                                        field_coverage={})}
    inv = dm.coverage_inventory(DataManifest(version=1, symbols=syms))
    fut = next(e for e in inv if e["key"] == "ohlcv.futures")
    assert fut["target_floor"] == "2010-01-01"
    assert "백필 진행중" in dm.inventory_for_prompt(inv)


def test_no_target_floor_when_floor_reached():
    """floor 도달(실측 first ≤ floor)이면 target_floor 미노출 — 불필요한 잡음 없음."""
    inv = dm.coverage_inventory(_manifest())     # 코스피200선물 first=2010-01-04지만
    fut = next(e for e in inv if e["key"] == "ohlcv.futures")
    # futures 그룹 min first = S&P500(1990) ≤ 2010 → 미노출.
    assert "target_floor" not in fut

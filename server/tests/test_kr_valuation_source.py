"""A1 — 스크리너 밸류(PBR/PER) 출처 일원화(OpenDART) 회귀 차단.

스크리너 PBR/PER을 NAVER가 아니라 canonical OpenDART(360·백테스트와 동일 출처)로 채운다.
같은 get_projected 계산을 쓰므로 스크리너 PBR == 360 PBR이 구조적으로 보장된다. 서빙은 그대로
in-memory 스냅샷(속도 무변경) — 일일 크론에서 최신 단면만 materialize해 enrich한다.

    cd platform/server && pytest tests/test_kr_valuation_source.py -v
"""
import pandas as pd
import pytest

krx_cache = pytest.importorskip("app.krx_cache")


def test_merge_fields_only_existing_symbols():
    """스냅샷에 있는 종목만 enrich, 없는 종목은 skip(스크리너 유니버스 밖)."""
    krx_cache._state["metrics"] = {"005930": {"close": 70000.0}}
    n = krx_cache.merge_fields({"005930": {"pbr": 1.2, "per": 9.0},
                                "999999": {"pbr": 5.0}})
    assert n == 1
    assert krx_cache._state["metrics"]["005930"]["pbr"] == 1.2
    assert krx_cache._state["metrics"]["005930"]["per"] == 9.0
    assert "999999" not in krx_cache._state["metrics"]


def test_merge_fields_skips_none():
    """None은 기존 값을 덮지 않는다(부분 갱신 안전)."""
    krx_cache._state["metrics"] = {"005930": {"close": 70000.0, "pbr": 1.2}}
    krx_cache.merge_fields({"005930": {"pbr": None, "per": 9.0}})
    assert krx_cache._state["metrics"]["005930"]["pbr"] == 1.2
    assert krx_cache._state["metrics"]["005930"]["per"] == 9.0


def test_naver_no_longer_sources_pbr_per():
    """PBR/PER은 OpenDART로 이관 — NAVER _FIELD_MAP에서 제거(중복 소스 0)."""
    naver = pytest.importorskip("app.naver_fundamentals")
    assert "per" not in naver._FIELD_MAP
    assert "pbr" not in naver._FIELD_MAP
    for k in ("eps", "bps", "dividend", "dividendYieldRatio", "foreignRate"):
        assert k in naver._FIELD_MAP


def test_materialize_maps_canonical_cols_to_screener_keys(monkeypatch):
    """get_projected의 pb_ratio→pbr, trailing_pe→per 매핑 + 최신 비결측값 적재."""
    main = pytest.importorskip("app.main")

    def fake_projected(cols, symbols=None, recent_days=None):
        idx = pd.date_range("2026-06-01", periods=3, freq="B")
        return {
            "005930": pd.DataFrame({"pb_ratio": [1.0, None, 1.3],
                                    "trailing_pe": [8.0, 9.0, 9.5]}, index=idx),
            "000660": pd.DataFrame({"pb_ratio": [None, None, None],
                                    "trailing_pe": [None, None, 12.0]}, index=idx),
        }
    monkeypatch.setattr(main.data_cache, "get_projected", fake_projected)
    krx_cache._state["metrics"] = {"005930": {"close": 1.0}, "000660": {"close": 1.0}}

    main._materialize_kr_valuation()

    assert krx_cache._state["metrics"]["005930"]["pbr"] == 1.3   # 최신 비결측(마지막)
    assert krx_cache._state["metrics"]["005930"]["per"] == 9.5
    assert "pbr" not in krx_cache._state["metrics"]["000660"]     # pb 전부 결측 → 미적재(정직)
    assert krx_cache._state["metrics"]["000660"]["per"] == 12.0


def test_screener_pbr_equals_describe_pbr(monkeypatch):
    """동일 canonical 단면에서 materialize의 pbr == run_describe_report의 pb_ratio.

    A1의 핵심 가치 — 두 화면의 PBR이 같음을 같은 추출 규칙(최신 비결측)으로 고정한다."""
    from quant_core.ir_engine import run_query
    from quant_core.ir_engine.spec import StrategyIR

    idx = pd.date_range("2025-01-01", periods=300, freq="B")
    prices = [100.0 + i for i in range(300)]
    df = pd.DataFrame({"Open": prices, "High": [p * 1.01 for p in prices],
                       "Low": [p * 0.99 for p in prices], "Close": prices,
                       "Volume": [1e6] * 300, "pb_ratio": [1.5] * 300,
                       "trailing_pe": [10.0] * 300}, index=idx)
    ds = {"005930": df}

    ir = StrategyIR.model_validate({"universe": {"kind": "single", "symbols": ["005930"]},
                                    "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                                    "query": "describe"})
    report = run_query(ir, ds)
    describe_pb = report["fundamentals"]["pb_ratio"]

    main = pytest.importorskip("app.main")
    monkeypatch.setattr(main.data_cache, "get_projected",
                        lambda cols, symbols=None, recent_days=None:
                        {"005930": df[["pb_ratio", "trailing_pe"]]})
    krx_cache._state["metrics"] = {"005930": {"close": prices[-1]}}
    main._materialize_kr_valuation()
    screener_pbr = krx_cache._state["metrics"]["005930"]["pbr"]

    assert screener_pbr == describe_pb == 1.5    # 같은 출처·같은 추출 → 일치

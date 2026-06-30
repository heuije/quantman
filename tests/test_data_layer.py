"""M1.1 — 데이터 계층(DataSpec 요구정의 + DataManifest 실측메타) 회귀.

    cd platform && pytest tests/test_data_layer.py -v
"""

import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.data import (  # noqa: E402
    DataManifest, REGISTRY, build_manifest, coverage_report, data_spec, get,
    load_manifest, save_manifest,
)


# ── M1.1a DataSpec ────────────────────────────────────────────────────────────

def test_dataspec_registry_complete_and_serializable():
    spec = data_spec()
    assert len(spec) >= 15
    keys = [s["key"] for s in spec]
    assert len(keys) == len(set(keys))                      # 키 유일
    assert {"ohlcv.kr", "indicator.derived", "macro.fred"} <= set(keys)
    json.dumps(spec, ensure_ascii=False)                     # 프론트/문서 소비 가능
    # P-class 6종 모두 1개 이상
    assert {s["pclass"] for s in spec} >= {"P1", "P2", "P3", "P4", "P5", "P6"}


def test_dataspec_derived_marks_self_computed():
    d = get("indicator.derived")
    assert d.derivation == "computed" and d.computed_from   # 자체산출(수급 불필요)


def test_dataspec_absent_partial_have_notes():
    for s in REGISTRY.values():
        if s.current_status in ("absent", "partial"):
            assert s.notes, f"{s.key} 갭인데 설명 없음"        # 로드맵 추적성


# ── M1.1b DataManifest ────────────────────────────────────────────────────────

def _dummy_dataset():
    idx = pd.date_range("2021-01-04", periods=120, freq="B")
    return {"000020": pd.DataFrame({"Close": range(120)}, index=idx),
            "AAPL": pd.DataFrame({"Close": range(80)}, index=idx[:80]),
            "EMPTY": pd.DataFrame()}


def test_build_manifest_derives_coverage():
    m = build_manifest(_dummy_dataset(), version=7,
                       symbol_meta={"000020": {"currency": "KRW", "feed": "ohlcv.kr"}},
                       feeds={"ohlcv.kr": {"source": "FinanceDataReader", "adjustment": "raw",
                                           "n_symbols": 1}})
    assert m.version == 7 and m.built_at
    assert m.symbol("000020").n_rows == 120
    assert m.symbol("000020").first_date == "2021-01-04"
    assert m.symbol("000020").currency == "KRW"             # 외부 메타 머지
    assert m.symbol("AAPL").n_rows == 80                    # 짧은 이력 반영
    assert m.symbol("EMPTY").n_rows == 0
    assert m.feed_status("ohlcv.kr") == "ok"
    assert m.feed_status("nonexistent") == "absent"
    assert m.feeds["ohlcv.kr"].adjustment == "raw"


def test_build_manifest_field_coverage_tracks_nonprice():
    """track_fields 주면 비가격 필드의 종목별 비결측 first/last/n 기록(null≠0). 미지정 시 기존동작."""
    import numpy as np
    idx = pd.date_range("2021-01-04", periods=10, freq="B")
    df_a = pd.DataFrame({                                 # 005930: trailing_pe는 뒤 4영업일만
        "Close": range(10),
        "trailing_pe": [np.nan] * 6 + [12.0, 12.5, 13.0, 13.5],
        "inst_net_buy": range(10),                       # 전구간
    }, index=idx)
    df_b = pd.DataFrame({"Close": range(10)}, index=idx)  # AAPL: 비가격 필드 전무
    ds = {"005930": df_a, "AAPL": df_b}
    m = build_manifest(ds, track_fields=["trailing_pe", "inst_net_buy", "pb_ratio"])
    fc = m.symbol("005930").field_coverage
    assert fc["trailing_pe"] == {"first": "2021-01-12", "last": "2021-01-15", "n": 4}
    assert fc["inst_net_buy"]["n"] == 10
    assert "pb_ratio" not in fc                           # 컬럼 없음 → 키 생략(미보유)
    assert m.symbol("AAPL").field_coverage == {}          # 추적 필드 전무
    assert build_manifest(ds).symbol("005930").field_coverage == {}   # 미지정=기존동작


def test_coverage_report_aggregates_global():
    """coverage_report — 필드별 유니버스 커버리지(covered/total/pct) 집계(백필 기준선·P2/P3)."""
    import numpy as np
    idx = pd.date_range("2021-01-04", periods=5, freq="B")
    ds = {
        "A": pd.DataFrame({"Close": range(5), "trailing_pe": [1.0] * 5,
                           "inst_net_buy": [1.0] * 5}, index=idx),
        "B": pd.DataFrame({"Close": range(5),
                           "trailing_pe": [np.nan, np.nan, 2.0, 2.0, 2.0]}, index=idx),
        "C": pd.DataFrame({"Close": range(5)}, index=idx),         # 비가격 필드 전무
    }
    rep = coverage_report(build_manifest(ds, track_fields=["trailing_pe", "inst_net_buy"]))
    assert rep["trailing_pe"]["covered"] == 2 and rep["trailing_pe"]["total"] == 3
    assert rep["trailing_pe"]["pct"] == 66.7
    assert rep["trailing_pe"]["first"] == "2021-01-04" and rep["trailing_pe"]["last"] == "2021-01-08"
    assert rep["inst_net_buy"]["covered"] == 1 and rep["inst_net_buy"]["pct"] == 33.3


def test_manifest_roundtrip(tmp_path):
    m = build_manifest(_dummy_dataset(), version=3)
    p = save_manifest(m, tmp_path / "_manifest.json")
    assert p.exists()
    back = load_manifest(p)
    assert isinstance(back, DataManifest)
    assert back.version == 3 and back.symbol("000020").n_rows == 120


def test_load_manifest_missing_returns_none(tmp_path):
    assert load_manifest(tmp_path / "nope.json") is None


# ── M1.2 의존성 레지스트리 ────────────────────────────────────────────────────

from quant_core.blocks import Node, const, data  # noqa: E402
from quant_core.data import required_data  # noqa: E402
from quant_core.ir_engine import (  # noqa: E402
    Entry, Overlays, PositionSpec, Sizing, SimSpec, StrategyIR, Study, Universe,
)


def test_deps_single_rule_rsi():
    s = StrategyIR(
        signal=Node(op="compare", params={"op": "<"},
                    inputs={"left": data("__SELF__.rsi_14"), "right": const(30)}),
        universe=Universe(kind="single", symbols=["005930"]),
        position=PositionSpec(entry=Entry(mode="on_signal")))
    req = required_data(s)
    assert req == {"price.close", "price.open", "indicator.derived"}   # 단일·on_signal·next_open


def test_deps_factor_scheduled_all():
    s = StrategyIR(
        signal=Node(op="rank", inputs={"signal": data("momentum_12_1m")}),
        universe=Universe(kind="all"),
        position=PositionSpec(entry=Entry(mode="scheduled", rebalance="monthly", top_n=20)))
    req = required_data(s)
    assert "indicator.derived" in req            # momentum rank → 파생지표
    assert "price.open" not in req               # scheduled은 fill 무관
    # 게이트 비소비 토큰은 발행하지 않는다(calendar·symbol_master는 엔진·validate가 직접 강제).
    assert "calendar" not in req and "symbol_master" not in req


def test_deps_event_intraday_list():
    cross = Node(op="cross", params={"direction": "down"},
                 inputs={"left": data("__SELF__.Close"),
                         "right": Node(op="ts_mean", params={"window": 5},
                                       inputs={"signal": data("__SELF__.Close")})})
    s = StrategyIR(signal=cross, universe=Universe(kind="list", symbols=["A", "B"]),
                   position=PositionSpec(entry=Entry(mode="on_signal")), query="relate",
                   study=Study(event=cross, event_basis="intraday", windows=[1, 3]))
    req = required_data(s)
    assert {"price.close", "price.open"} <= req
    assert "symbol_master" not in req            # 게이트 비소비 토큰 미발행


def test_deps_group_fundamental_macro():
    sig = Node(op="group_neutralize", params={"group_type": "Sector"},
               inputs={"signal": data("__SELF__.trailing_pe")})
    s = StrategyIR(
        signal=sig, universe=Universe(kind="all"),
        position=PositionSpec(
            direction="long_short",
            entry=Entry(mode="scheduled", rebalance="monthly", top_n=20),
            overlays=Overlays(max_group_pct=30,
                              group_label=Node(op="bucket", params={"edges": [25.0]},
                                               inputs={"signal": data("VIX.Close")}))))
    req = required_data(s)
    assert "sector" in req and "fundamental" in req and "macro:VIX" in req


# ── Phase 2: 무결성 4액션 게이트 ──────────────────────────────────────────────

from quant_core.data import (  # noqa: E402
    DataManifest, FeedManifest, SymbolManifest, evaluate_data_soundness,
)


def _mani(symbols=None, feeds=None, **flags):
    sm = {s: SymbolManifest(symbol=s, **(m or {})) for s, m in (symbols or {}).items()}
    fm = {k: FeedManifest(key=k, **v) for k, v in (feeds or {}).items()}
    return DataManifest(symbols=sm, feeds=fm, **flags)


def _rule(ref="__SELF__.Close"):
    return StrategyIR(
        signal=Node(op="compare", params={"op": ">"},
                    inputs={"left": data(ref), "right": const(0)}),
        universe=Universe(kind="single", symbols=["005930"]),
        position=PositionSpec(entry=Entry(mode="on_signal")))


def _ie(iss):
    return any(i.is_error for i in iss)


def test_gate_macro_ref_missing_rejects():
    s = StrategyIR(
        signal=Node(op="compare", params={"op": ">"},
                    inputs={"left": data("__SELF__.Close"), "right": data("VIX.Close")}),
        universe=Universe(kind="single", symbols=["005930"]),
        position=PositionSpec(entry=Entry(mode="on_signal")))
    m = _mani(symbols={"005930": {"n_rows": 100, "feed": "ohlcv.kr"}})
    assert any(i.rule == "D-ref" and i.is_error for i in evaluate_data_soundness(s, m))


def test_gate_fundamental_absent_rejects():
    s = _rule("__SELF__.trailing_pe")
    m = _mani(symbols={"005930": {"n_rows": 100, "feed": "ohlcv.kr"}})   # 펀더 피드 없음
    assert any(i.rule == "D-avail" and i.is_error for i in evaluate_data_soundness(s, m))


def test_gate_fundamental_no_pit_warns_then_rejects_strict():
    s = _rule("__SELF__.trailing_pe")
    m = _mani(symbols={"005930": {"n_rows": 100, "feed": "ohlcv.kr"}},
              feeds={"fundamental.equity": {"status": "ok", "has_as_of": False}})
    iss = evaluate_data_soundness(s, m)
    assert any(i.rule == "D-pit" for i in iss) and not _ie(iss)        # 기본 WARN
    assert _ie(evaluate_data_soundness(s, m, strict=True))             # strict → 거부


def test_gate_survivorship_all_warns_then_rejects_strict():
    s = StrategyIR(signal=Node(op="rank", inputs={"signal": data("momentum_12_1m")}),
                   universe=Universe(kind="all"),
                   position=PositionSpec(entry=Entry(mode="scheduled", rebalance="monthly", top_n=20)))
    m = _mani(feeds={"ohlcv.kr": {"adjustment": "split_adjusted"}}, has_membership_history=False)
    iss = evaluate_data_soundness(s, m)
    assert any(i.rule == "D-surv" for i in iss) and not _ie(iss)
    assert any(i.rule == "D-surv" and i.is_error for i in evaluate_data_soundness(s, m, strict=True))


def test_gate_adjustment_raw_warns():
    s = _rule()
    m = _mani(symbols={"005930": {"n_rows": 100, "feed": "ohlcv.kr"}},
              feeds={"ohlcv.kr": {"adjustment": "raw"}})         # 요구 split_adjusted vs 실측 raw
    adj = [i for i in evaluate_data_soundness(s, m) if i.rule == "D-adj"]
    assert adj and not adj[0].is_error


def test_gate_list_missing_symbol_repair_info():
    s = StrategyIR(signal=Node(op="compare", params={"op": ">"},
                               inputs={"left": data("__SELF__.Close"), "right": const(0)}),
                   universe=Universe(kind="list", symbols=["A", "B", "ZZZ"]),
                   position=PositionSpec(entry=Entry(mode="on_signal")))
    m = _mani(symbols={"A": {"n_rows": 100, "feed": "ohlcv.kr", "adjustment": "split_adjusted"},
                       "B": {"n_rows": 100, "feed": "ohlcv.kr", "adjustment": "split_adjusted"}},
              feeds={"ohlcv.kr": {"adjustment": "split_adjusted"}})
    iss = evaluate_data_soundness(s, m)
    rep = [i for i in iss if i.rule == "D-repair"]
    assert rep and "ZZZ" in rep[0].message and not _ie(iss)     # 결손종목 제외 INFO, 거부 아님


def test_gate_warmup_late_listing_info():
    s = StrategyIR(signal=Node(op="compare", params={"op": ">"},
                               inputs={"left": data("__SELF__.Close"), "right": const(0)}),
                   universe=Universe(kind="list", symbols=["A", "B"]),
                   position=PositionSpec(entry=Entry(mode="on_signal")),
                   simulation=SimSpec(start="2020-01-01"))
    m = _mani(symbols={"A": {"n_rows": 100, "feed": "ohlcv.kr", "adjustment": "split_adjusted",
                             "listing_date": "2019-01-01"},
                       "B": {"n_rows": 100, "feed": "ohlcv.kr", "adjustment": "split_adjusted",
                             "listing_date": "2021-06-01"}},     # 백테스트 시작 이후 상장
              feeds={"ohlcv.kr": {"adjustment": "split_adjusted"}})
    iss = evaluate_data_soundness(s, m)
    warm = [i for i in iss if i.rule == "D-repair" and "워밍업" in i.message]
    assert warm and "B" in warm[0].message and not _ie(iss)   # 시작 후 상장 종목 INFO, 거부 아님


def test_gate_clean_manifest_no_errors():
    s = StrategyIR(signal=Node(op="rank", inputs={"signal": data("momentum_12_1m")}),
                   universe=Universe(kind="all"),
                   position=PositionSpec(entry=Entry(mode="scheduled", rebalance="monthly", top_n=20)))
    m = _mani(symbols={"A": {"n_rows": 100, "feed": "ohlcv.kr",
                             "adjustment": "split_adjusted", "calendar": "KRX"}},
              feeds={"ohlcv.kr": {"adjustment": "split_adjusted"}}, has_membership_history=True)
    assert not _ie(evaluate_data_soundness(s, m))               # 깨끗한 매니페스트 → 거부 0


def test_strategy_from_spec_applies_gate_strict():
    """strategy_from_spec에 manifest+strict 전달 → 생존편향이 거부로 승격되는지(배선 검증)."""
    import numpy as np
    from quant_core.ir_engine import strategy_from_spec
    idx = pd.date_range("2020-01-01", periods=200, freq="B")
    ds = {f"S{i}": pd.DataFrame(
        {"Open": np.linspace(100, 120, 200), "High": np.linspace(100, 120, 200) * 1.01,
         "Low": np.linspace(100, 120, 200) * 0.99, "Close": np.linspace(100, 120, 200),
         "Volume": 1e6, "momentum_12_1m": float(m)}, index=idx)
        for i, m in enumerate([9, 5, 1, -4])}
    spec = {"signal": {"op": "rank", "inputs": {"signal": {"op": "data", "params": {"ref": "momentum_12_1m"}}}},
            "universe": {"kind": "all"},
            "position": {"entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 2}}}
    m = _mani(feeds={"ohlcv.kr": {"adjustment": "split_adjusted"}}, has_membership_history=False)
    res = strategy_from_spec(spec, ds, manifest=m, strict=True)
    assert not res["success"]
    assert any(i["rule"] == "D-surv" for i in res["issues"])
    # 비-strict면 경고로 통과(실행 성공)
    ok = strategy_from_spec(spec, ds, manifest=m, strict=False)
    assert ok["success"] and any(w["rule"] == "D-surv" for w in ok.get("warnings", []))


def test_strategy_from_spec_surfaces_field_coverage():
    """다종목 쿼리에서 부분 커버리지 필드를 diagnostics.field_coverage로 노출(null≠0·편향가드)."""
    import numpy as np
    from quant_core.ir_engine import strategy_from_spec
    idx = pd.date_range("2020-01-01", periods=200, freq="B")

    def mk(pe, mom):
        d = {"Open": np.linspace(100, 120, 200), "High": np.linspace(100, 120, 200) * 1.01,
             "Low": np.linspace(100, 120, 200) * 0.99, "Close": np.linspace(100, 120, 200),
             "Volume": 1e6, "momentum_12_1m": float(mom)}
        if pe is not None:
            d["trailing_pe"] = float(pe)                # None이면 컬럼 자체 없음(미보유)
        return pd.DataFrame(d, index=idx)

    ds = {"A": mk(10, 9), "B": mk(12, 5), "C": mk(None, 1), "D": mk(None, -4)}  # trailing_pe 2/4만
    spec = {"signal": {"op": "rank", "inputs": {"signal": {"op": "data",
                                                           "params": {"ref": "momentum_12_1m"}}}},
            "universe": {"kind": "all"},
            "position": {"entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 2}}}
    m = build_manifest(ds, track_fields=["trailing_pe", "inst_net_buy"],
                       feeds={"ohlcv.kr": {"adjustment": "split_adjusted"}},
                       has_membership_history=True)
    res = strategy_from_spec(spec, ds, manifest=m)
    assert res["success"]
    assert res["diagnostics"]["field_coverage"] == {"trailing_pe": {"covered": 2, "total": 4}}
    # 부분만 노출 — 전무(inst_net_buy) 생략. manifest 없으면 미부착(기존 동작).
    assert "field_coverage" not in (strategy_from_spec(spec, ds).get("diagnostics") or {})


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

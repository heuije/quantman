"""P3 — consensus/flow indicators 배선.

reindex-ffill 병합(look-ahead 0)·target_upside 파생·compute_columns 프로젝션 동치·
사이드카 없을 때 골든 안전(신규 컬럼 0)을 고정한다.

    cd core && pytest tests/test_altdata_indicators.py -v
"""
import numpy as np
import pandas as pd
import pytest

from quant_core import indicators as ind


def _price(dates, close):
    idx = pd.to_datetime(dates)
    return pd.DataFrame({"Open": close, "High": close, "Low": close,
                         "Close": close, "Volume": [1000] * len(close)}, index=idx)


def test_add_flow_reindex_ffill_no_lookahead():
    df = _price(["2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08"], [100, 101, 102, 103])
    flow = pd.DataFrame({"inst_net_buy": [500, -200], "foreign_net_buy": [10, -5]},
                        index=pd.Index(pd.to_datetime(["2024-01-05", "2024-01-08"]), name="as_of"))
    out = ind.add_flow(df, flow)
    # 01-03·01-04: 이전 수급 없음 → NaN (미래 01-05 값을 당겨오지 않음=look-ahead 0)
    assert pd.isna(out.loc["2024-01-03", "inst_net_buy"])
    assert out.loc["2024-01-05", "inst_net_buy"] == 500
    assert out.loc["2024-01-08", "foreign_net_buy"] == -5


def test_add_consensus_target_upside():
    df = _price(["2024-01-03", "2024-01-05"], [100, 80])
    panel = pd.DataFrame({"consensus_target": [120.0], "consensus_target_median": [120.0],
                          "analyst_count": [1.0], "consensus_opinion": [1.0],
                          "target_dispersion": [np.nan], "target_revision_pct": [np.nan],
                          "days_since_report": [0.0]},
                         index=pd.Index(pd.to_datetime(["2024-01-03"]), name="as_of"))
    out = ind.add_consensus(df, panel)
    assert out.loc["2024-01-03", "consensus_target"] == 120.0
    # 괴리율 = 컨센목표가/종가 − 1. 종가 100 → 0.2, ffill된 목표가 120·종가 80 → 0.5
    assert out.loc["2024-01-03", "target_upside"] == pytest.approx(0.2)
    assert out.loc["2024-01-05", "target_upside"] == pytest.approx(0.5)


def test_compute_columns_projection_matches_compute_all():
    dates = pd.date_range("2024-01-01", periods=10).strftime("%Y-%m-%d")
    df = _price(dates, list(range(100, 110)))
    flow = pd.DataFrame({"inst_net_buy": [1, 2, 3], "foreign_net_buy": [4, 5, 6]},
                        index=pd.Index(pd.to_datetime(["2024-01-02", "2024-01-05", "2024-01-08"]),
                                       name="as_of"))
    pdata = {c: [0.0] for c in ind.CONSENSUS_FEED_COLS}
    pdata["consensus_target"] = [200.0]
    panel = pd.DataFrame(pdata, index=pd.Index(pd.to_datetime(["2024-01-03"]), name="as_of"))

    full = ind.compute_all(df, None, panel, flow)
    proj = ind.compute_columns(df, {"inst_net_buy", "target_upside", "consensus_target"},
                               None, panel, flow)
    for c in ("inst_net_buy", "target_upside", "consensus_target"):
        assert proj[c].equals(full[c]), f"프로젝션≠compute_all: {c}"


def test_compute_all_without_sidecars_adds_nothing():
    """사이드카 미전달 시 신규 컬럼 0 — 골든 byte-identical 안전망."""
    df = _price(["2024-01-03", "2024-01-04"], [100, 101])
    out = ind.compute_all(df)
    for c in ind.FLOW_INDICATOR_COLS + ind.CONSENSUS_INDICATOR_COLS:
        assert c not in out.columns


def test_groups_registered():
    assert ind.INDICATOR_GROUPS["수급"] == ["inst_net_buy", "foreign_net_buy"]
    assert "target_upside" in ind.INDICATOR_GROUPS["컨센서스"]
    # spec.provides(피드 7) ⊂ 컨센서스 그룹(8=7+target_upside)
    from quant_core.data import spec
    assert set(spec.get("estimate.consensus").provides) <= set(ind.INDICATOR_GROUPS["컨센서스"])
    assert ind.INDICATOR_GROUPS["수급"] == spec.get("flow.kr_investor").provides

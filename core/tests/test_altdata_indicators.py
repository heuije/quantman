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


def test_add_marketcap_overrides_and_trade_value():
    """KRX sto가 market_cap 정본으로 combine_first 덮음 + trade_value 신규(look-ahead 0)."""
    df = _price(["2024-01-03", "2024-01-04", "2024-01-05"], [100, 101, 102])
    df["market_cap"] = [1e12, 1e12, 1e12]        # 펀더 파생(add_fundamentals 산물 모사)
    mc = pd.DataFrame({"market_cap": [2e12, 3e12], "trade_value": [5e8, 7e8]},
                      index=pd.Index(pd.to_datetime(["2024-01-04", "2024-01-05"]), name="as_of"))
    out = ind.add_marketcap(df, mc)
    # 01-03: sto 이전값 없음 → 기존 펀더값 보존(combine_first·미래 안 당김)
    assert out.loc["2024-01-03", "market_cap"] == 1e12
    # 01-04~: KRX sto가 정본으로 덮음
    assert out.loc["2024-01-04", "market_cap"] == 2e12
    assert out.loc["2024-01-05", "market_cap"] == 3e12
    assert pd.isna(out.loc["2024-01-03", "trade_value"])   # 신규 컬럼도 look-ahead 0
    assert out.loc["2024-01-05", "trade_value"] == 7e8


def test_add_short_volume_ratio_pct():
    """공매도비중(%) = short_volume/total_volume×100 파생, reindex-ffill(look-ahead 0)."""
    df = _price(["2024-01-03", "2024-01-04", "2024-01-05"], [100, 101, 102])
    sv = pd.DataFrame({"short_volume": [300.0, 0.0], "short_exempt_volume": [1.0, 1.0],
                       "total_volume": [1000.0, 500.0]},
                      index=pd.Index(pd.to_datetime(["2024-01-04", "2024-01-05"]), name="as_of"))
    out = ind.add_short_volume(df, sv)
    assert pd.isna(out.loc["2024-01-03", "short_volume_ratio"])   # 이전 없음
    assert out.loc["2024-01-04", "short_volume_ratio"] == pytest.approx(30.0)  # 300/1000×100
    assert out.loc["2024-01-05", "short_volume_ratio"] == pytest.approx(0.0)
    # 원시 3컬럼은 df에 부착 안 함(파생 비중만 노출) — 매니페스트/인벤토리 규약
    assert "short_volume" not in out.columns


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
    for c in (ind.FLOW_INDICATOR_COLS + ind.CONSENSUS_INDICATOR_COLS
              + ind.INSTITUTIONAL_INDICATOR_COLS):
        assert c not in out.columns


def test_groups_registered():
    # 수급 그룹 = KR 순매수(flow.kr_investor) + US 공매도비중(파생). 둘 다 "수급" 성격.
    assert ind.INDICATOR_GROUPS["수급"] == ["inst_net_buy", "foreign_net_buy", "short_volume_ratio"]
    assert "target_upside" in ind.INDICATOR_GROUPS["컨센서스"]
    # 13F 기관보유는 별도 그룹(수급=KR 일별과 분리)
    assert ind.INDICATOR_GROUPS["기관보유(13F)"] == list(ind.INSTITUTIONAL_INDICATOR_COLS)


def test_add_institutional_holdings_qoq_and_lookahead():
    """13F 분기 패널 reindex-ffill + qoq(전분기 순증감%) 파생·look-ahead 0."""
    dates = pd.date_range("2024-01-01", "2024-07-01").strftime("%Y-%m-%d")
    df = _price(dates, [100.0] * len(dates))
    panel = pd.DataFrame(
        {"institutional_value": [1e9, 1.2e9], "institutional_shares": [1e6, 1.2e6],
         "institutional_holders": [50.0, 55.0]},
        index=pd.Index(pd.to_datetime(["2024-02-14", "2024-05-15"]), name="as_of"))
    out = ind.add_institutional_holdings(df, panel)
    assert pd.isna(out.loc["2024-01-15", "institutional_shares"])       # 첫 as_of 이전=look-ahead 0
    assert out.loc["2024-03-01", "institutional_holders"] == 50.0
    # qoq = (1.2M/1.0M−1)×100 = 20 (분기 패널에서 산출 — 비분기일 0 오도 방지)
    assert out.loc["2024-06-01", "institutional_qoq_change"] == pytest.approx(20.0)
    assert pd.isna(out.loc["2024-03-01", "institutional_qoq_change"])   # 첫 분기=전분기 없음
    # spec.provides(피드 7) ⊂ 컨센서스 그룹(8=7+target_upside)
    from quant_core.data import spec
    assert set(spec.get("estimate.consensus").provides) <= set(ind.INDICATOR_GROUPS["컨센서스"])
    # KR 순매수 provides ⊂ 수급 그룹(공매도비중은 별 피드라 그룹만 공유)
    assert set(spec.get("flow.kr_investor").provides) <= set(ind.INDICATOR_GROUPS["수급"])

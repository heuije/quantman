"""Phase 2b 회귀 — describe 360이 라이브 컨센서스·수급을 표면화하는지(엔진 + 요약).

배경: run_describe_report가 가격·위험·밸류 3종만 반환 → 라이브(main #149) 컨센서스·수급이
종목 360 리포트에서 누락(공급≫소비). 합성 df(컨센서스·수급 컬럼 포함)로 표면화를 잠그고,
미커버 종목(컬럼 부재)에선 가짜 채움 없이 생략되는지 확인.

    pytest tests/test_describe_surfacing.py -v
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.ir_engine import StrategyIR  # noqa: E402
from quant_core.ir_engine.run import run_describe_report  # noqa: E402
from quant_core.ir_engine.summarize import summarize_result  # noqa: E402


def _ir(sym: str = "AAA") -> StrategyIR:
    return StrategyIR.model_validate({
        "name": "desc", "query": "describe",
        "universe": {"kind": "single", "symbols": [sym]},
        "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 20},
                     "entry": {"mode": "on_signal"}, "exit": {"hold_days": 5}},
        "simulation": {"delay": 1, "fill": "next_open"}, "study": {"axis": "none"},
    })


def _df(n: int = 300, with_alt: bool = True) -> pd.DataFrame:
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    close = pd.Series(np.linspace(100.0, 130.0, n), index=idx)
    df = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Volume": 1_000_000.0}, index=idx)
    if with_alt:                                  # KR 피드 병합 모사 (라이브)
        df["consensus_target"] = 156.0
        df["target_upside"] = df["consensus_target"] / df["Close"] - 1.0
        df["consensus_opinion"] = 0.6
        df["analyst_count"] = 8.0
        df["target_revision_pct"] = 0.03
        df["days_since_report"] = 12.0
        df["inst_net_buy"] = 1e8                  # +1억/일
        df["foreign_net_buy"] = -5e7              # -0.5억/일
    return df


def test_describe_surfaces_consensus_and_flow():
    ds = {"AAA": _df()}
    res = run_describe_report(_ir(), ds)
    assert res["success"]
    c = res["consensus"]
    assert abs(c["consensus_target"] - 156.0) < 1e-9
    assert c["consensus_opinion"] == 0.6
    assert c["analyst_count"] == 8.0
    last_close = float(ds["AAA"]["Close"].iloc[-1])
    assert abs(c["target_upside"] - (156.0 / last_close - 1.0)) < 1e-9
    fl = res["flow"]
    assert abs(fl["inst_net_buy_20d"] - 1e8 * 20) < 1.0       # 최근 20거래일 누적
    assert abs(fl["foreign_net_buy_20d"] - (-5e7) * 20) < 1.0


def test_summarize_includes_consensus_flow():
    res = run_describe_report(_ir(), {"AAA": _df()})
    txt = summarize_result(res)
    assert "컨센서스 목표가" in txt and "상승여력" in txt
    assert "순매수" in txt


def test_describe_omits_alt_when_absent():
    """미커버 종목 — 컨센서스·수급 컬럼 부재 → 블록은 None, 요약엔 미표기(가짜 채움 금지)."""
    res = run_describe_report(_ir(), {"AAA": _df(with_alt=False)})
    assert res["consensus"]["consensus_target"] is None
    assert res["flow"]["inst_net_buy_20d"] is None
    txt = summarize_result(res)
    assert "컨센서스 목표가" not in txt and "순매수" not in txt


def test_describe_summary_returns_in_percent_not_fraction():
    """수익률·연변동성·MDD는 분수(0.05)가 아니라 %(5.0)로 표기 — 100× 축소 버그 회귀."""
    res = {"success": True, "query": "describe", "report": "single",
           "symbol": "AAA", "sector": "X", "as_of": "2024-01-01", "data_points": 300,
           "price": {"last": 100.0, "returns": {"1m": 0.05, "12m": 0.20},
                     "high_52w": 120.0, "low_52w": 80.0, "pct_from_52w_high": -0.1},
           "risk": {"vol_annualized": 0.30, "max_drawdown": -0.25},
           "fundamentals": {"pb_ratio": 1.2, "trailing_pe": 10.0, "ev_ebitda": 8.0},
           "consensus": {}, "flow": {}}
    txt = summarize_result(res)
    assert "1M 5.0%" in txt and "12M 20.0%" in txt        # 0.05→5%, not 0.05%
    assert "연변동성 30.0%" in txt and "MDD -25.0%" in txt


def test_portfolio_summary_vol_in_percent():
    """포트 진단 연변동성도 분수(0.25)가 아니라 %(25.0)로 — describe 계열 동일 100× 클래스."""
    res = {"success": True, "query": "describe", "report": "portfolio", "n_holdings": 3,
           "concentration": {"hhi": 0.4, "effective_n": 2.5, "top_weight": 0.5},
           "valuation": {"weighted_pb": 1.5},
           "risk": {"portfolio_vol_annualized": 0.25, "avg_pairwise_corr": 0.3},
           "sector_exposure": {}}
    txt = summarize_result(res)
    assert "연변동성 25.0%" in txt

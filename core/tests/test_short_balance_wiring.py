"""KR 공매도 잔고(flow.kr_short_balance) 엔진 배선 — 수급 배선 B.

prod 근거: G6 부류(수급 신호 실수요). 수집·웹 서빙만 되고 엔진 미병합이던 잔고를
시총(add_marketcap)과 동형 패턴으로 배선 — 노출은 short_balance_ratio(%) 1컬럼.
"""
import numpy as np
import pandas as pd

from quant_core.indicators import (SHORTBAL_INDICATOR_COLS, add_short_balance,
                                   compute_all, compute_columns,
                                   get_all_indicator_columns)
from quant_core.ir_engine.service import strategy_from_spec


def _price_df(periods=60):
    idx = pd.bdate_range("2024-01-01", periods=periods)
    close = np.linspace(100.0, 120.0, periods)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": np.full(periods, 1e6)}, index=idx)


def _shortbal_df(idx):
    # 피드 저장 스키마 그대로(bal_qty/bal_amt/bal_ratio) — 격일만 값 존재(ffill 검증)
    sparse = idx[::2]
    return pd.DataFrame({
        "bal_qty": np.full(len(sparse), 1e5),
        "bal_amt": np.full(len(sparse), 1e10),
        "bal_ratio": np.linspace(1.0, 6.0, len(sparse)),
    }, index=sparse)


def test_add_short_balance_attaches_ratio_only():
    """bal_ratio → short_balance_ratio 1컬럼만 부착(수량·금액 미노출)·희소일 ffill."""
    df = _price_df()
    out = add_short_balance(df, _shortbal_df(df.index))
    assert "short_balance_ratio" in out.columns
    assert "bal_qty" not in out.columns and "bal_amt" not in out.columns
    assert out["short_balance_ratio"].notna().all()          # 격일 원본 → ffill로 dense
    # 원본 없는 날은 직전 값 유지(look-ahead 0)
    assert out["short_balance_ratio"].iloc[1] == out["short_balance_ratio"].iloc[0]


def test_shortbal_in_indicator_ssot():
    """SSOT(get_all_indicator_columns) 포함 — 챗 reference_data·검증 valid-ref 자동 인지."""
    cols = get_all_indicator_columns()
    for c in SHORTBAL_INDICATOR_COLS:
        assert c in cols


def test_compute_paths_parity():
    """compute_all과 compute_columns(프로젝션)의 short_balance_ratio가 byte 동일."""
    df = _price_df()
    sb = _shortbal_df(df.index)
    full = compute_all(df, shortbal_df=sb)
    proj = compute_columns(df, ["short_balance_ratio"], shortbal_df=sb)
    pd.testing.assert_series_equal(full["short_balance_ratio"],
                                   proj["short_balance_ratio"])


def test_short_balance_signal_backtest():
    """__SELF__.short_balance_ratio 신호로 백테스트 e2e — 엔진 소비 계약."""
    df = _price_df()
    ds = {"005930": add_short_balance(df, _shortbal_df(df.index))}
    sig = {"op": "compare", "params": {"op": ">"},
           "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.short_balance_ratio"}},
                      "right": {"op": "const", "params": {"value": 3.0}}}}
    spec = {"name": "잔고", "universe": {"kind": "single", "symbols": ["005930"]},
            "signal": sig, "query": "simulate",
            "position": {"direction": "long", "sizing": {"mode": "pct_cash", "amount_pct": 100},
                         "entry": {"mode": "on_signal"}, "exit": {"hold_days": 5}},
            "simulation": {"initial_capital": 1e8}}
    res = strategy_from_spec(spec, ds)
    assert res.get("success"), res.get("error")
    assert (res.get("metrics") or {}).get("n_trades", 0) > 0   # 비중 3% 돌파 후 진입 발생

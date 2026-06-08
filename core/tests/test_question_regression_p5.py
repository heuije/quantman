"""P5 골든 — 다중팩터 횡단 회귀(Fama-MacBeth) + 신뢰구간/t값. statsmodels 대조.

    cd platform && pytest core/tests/test_question_regression_p5.py -v
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.blocks import data
from quant_core.ir_engine import (
    PositionSpec, SimSpec, StrategyIR, Study, Universe, run_query, validate_strategy,
)


def _errs(s):
    return [i.rule for i in validate_strategy(s) if i.is_error]


def _reg_ir(symbols, factors, windows=None):
    return StrategyIR(signal=data("__SELF__.Close"), universe=Universe(kind="list", symbols=symbols),
                      query="relate",
                      study=Study(relation_kind="regression", factors=factors,
                                  windows=windows or [5]))


def test_relation_kind_regression_parses():
    s = _reg_ir(["AAA", "BBB"], [data("__SELF__.fac1")])
    assert s.study.relation_kind == "regression" and len(s.study.factors) == 1


def test_validate_regression_requires_factors():
    s = _reg_ir(["AAA", "BBB"], [])
    assert "S-REG" in _errs(s)


def test_validate_regression_requires_multi_symbol():
    s = StrategyIR(signal=data("__SELF__.Close"), universe=Universe(kind="single", symbols=["AAA"]),
                   query="relate", study=Study(relation_kind="regression",
                                               factors=[data("__SELF__.fac1")], windows=[5]))
    assert "S-REG" in _errs(s)


from quant_core.ir_engine.run import _fama_macbeth


def test_fama_macbeth_math():
    # per-date 계수 3기간 × 2팩터 — 손계산 대조.
    betas = np.array([[2.0, 0.0], [2.2, 0.2], [1.8, -0.2]])
    mean, se, t, lo, hi = _fama_macbeth(betas)
    assert abs(mean[0] - 2.0) < 1e-9 and abs(mean[1] - 0.0) < 1e-9
    # 팩터1: std(ddof=1)=0.2 → se=0.2/√3, t=2.0/se 큰 값; 팩터2: mean 0 → t≈0
    assert t[0] > 10 and abs(t[1]) < 1e-9
    assert lo[0] < 2.0 < hi[0]


def _reg_fixture():
    """forward(5일) 수익 = 2.0*fac1 정확(종목별 상수 성장률). 회귀가 β=2.0 복원."""
    n = 80
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    f1 = {"AAA": -0.02, "BBB": -0.01, "CCC": 0.0, "DDD": 0.01, "EEE": 0.02, "FFF": 0.03}
    ds = {}
    for s, fv in f1.items():
        r = (1.0 + 2.0 * fv) ** (1.0 / 5.0) - 1.0      # (1+r)^5-1 = 2*fac1
        close = 100.0 * (1.0 + r) ** np.arange(n)
        ds[s] = pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                              "Close": close, "Volume": 1e6, "fac1": np.full(n, fv)}, index=idx)
    return ds, list(f1)


def test_regression_recovers_known_coef():
    ds, syms = _reg_fixture()
    s = _reg_ir(syms, [data("__SELF__.fac1")], windows=[5])
    res = run_query(s, ds)
    assert res["success"] and res["relation"] == "regression"
    fac = res["by_window"]["5"]["factors"][0]
    assert "fac1" in fac["name"]
    assert abs(fac["coef"] - 2.0) < 1e-6        # forward=2*fac1 복원
    # 정확 관계라 per-date 계수 분산 0 → t 무한대(완전 유의) 표기
    assert fac["t_inf"] is True or (fac["t_stat"] is not None and fac["t_stat"] > 100)


def test_regression_matches_statsmodels():
    """엔진 계수를 statsmodels OLS(단일 단면)와 대조 — lstsq=OLS 확인."""
    import statsmodels.api as sm
    ds, syms = _reg_fixture()
    s = _reg_ir(syms, [data("__SELF__.fac1")], windows=[5])
    res = run_query(s, ds)
    eng_coef = res["by_window"]["5"]["factors"][0]["coef"]
    # 한 날짜 단면으로 statsmodels OLS 직접
    fac = np.array([ds[c]["fac1"].iloc[0] for c in syms])
    close = pd.DataFrame({c: ds[c]["Close"] for c in syms})
    fwd = (close.shift(-5) / close - 1.0).iloc[0].to_numpy()
    X = sm.add_constant(fac)
    beta = sm.OLS(fwd, X).fit().params[1]
    assert abs(eng_coef - beta) < 1e-6

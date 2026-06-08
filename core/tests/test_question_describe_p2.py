"""P2 골든 — 단일종목 360 리포트 + 포트폴리오 진단 (run_describe_report·run_portfolio_diagnosis).

합성 픽스처로 결정적 단언(test_backtest_golden 스타일). describe를 universe.kind로 분기:
single→리포트, portfolio→진단, all/list→기존 팩터 분포(무변경).

    cd platform && pytest core/tests/test_question_describe_p2.py -v
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.ir_engine import run_query
from quant_core.ir_engine.spec import StrategyIR

_CLOSE = {"op": "data", "params": {"ref": "__SELF__.Close"}}   # 분석동사 명목 신호


def _single_df(prices, pb=None, n_pad=0):
    n = len(prices)
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    cols = {"Open": prices, "High": [p * 1.01 for p in prices],
            "Low": [p * 0.99 for p in prices], "Close": prices,
            "Volume": np.full(n, 1e6)}
    if pb is not None:
        cols["pb_ratio"] = np.full(n, pb)
    return pd.DataFrame(cols, index=idx)


# 단일종목 픽스처: 첫 130일 100, 다음 130일 200 (단조비감소). 260포인트.
_RAMP = [100.0] * 130 + [200.0] * 130
_DS_SINGLE = {"AAA": _single_df(_RAMP, pb=1.5)}


def _report_ir():
    return StrategyIR.model_validate({
        "universe": {"kind": "single", "symbols": ["AAA"]},
        "signal": _CLOSE, "query": "describe"})


def test_describe_single_routes_to_report():
    res = run_query(_report_ir(), _DS_SINGLE)
    assert res["success"] and res["query"] == "describe" and res["report"] == "single"
    assert res["symbol"] == "AAA"


def test_single_report_golden():
    res = run_query(_report_ir(), _DS_SINGLE)
    assert res["success"] and res["report"] == "single"
    assert res["sector"] == "Other"            # 합성 심볼 폴백
    assert res["data_points"] == 260
    p = res["price"]
    assert p["last"] == 200.0
    assert p["returns"]["12m"] == 1.0          # 200/100-1 (close[-253]=100)
    assert p["returns"]["6m"] == 0.0           # close[-127]=200
    assert p["high_52w"] == 200.0 and p["low_52w"] == 100.0
    assert res["risk"]["max_drawdown"] == 0.0  # 단조비감소
    assert res["risk"]["vol_annualized"] > 0   # 점프 1회로 변동성>0
    assert res["fundamentals"]["pb_ratio"] == 1.5
    assert res["fundamentals"]["trailing_pe"] is None   # 미수집=정직 None


def test_single_report_short_history():
    """짧은 이력 — 긴 윈도 수익은 None(크래시 금지)."""
    ds = {"BBB": _single_df([100.0, 101.0, 102.0])}
    ir = StrategyIR.model_validate({"universe": {"kind": "single", "symbols": ["BBB"]},
                                    "signal": _CLOSE, "query": "describe"})
    res = run_query(ir, ds)
    assert res["success"] and res["price"]["returns"]["12m"] is None
    assert res["price"]["last"] == 102.0


def test_single_report_missing_symbol():
    res = run_query(_report_ir(), {})          # 빈 데이터셋
    assert res["success"] is False


# 포트폴리오 픽스처: 변동성 있는 동일 경로(상관=1), pb 1/2/3.
_PATH = [100.0 + (i % 5) for i in range(80)]


def _pf_df(pb):
    return _single_df(_PATH, pb=pb)


_DS_PF = {"AAA": _pf_df(1.0), "BBB": _pf_df(2.0), "CCC": _pf_df(3.0)}


def _diag_ir(weights=None):
    u = {"kind": "portfolio", "symbols": ["AAA", "BBB", "CCC"]}
    if weights is not None:
        u["weights"] = weights
    return StrategyIR.model_validate({"universe": u, "signal": _CLOSE, "query": "describe"})


def test_portfolio_diagnosis_golden(monkeypatch):
    import quant_core.expression_parser as ep
    monkeypatch.setattr(ep, "get_symbol_group",
                        lambda s, g="Industry": {"AAA": "반도체", "BBB": "반도체",
                                                 "CCC": "자동차"}.get(s, "Other"))
    res = run_query(_diag_ir({"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}), _DS_PF)
    assert res["success"] and res["report"] == "portfolio" and res["n_holdings"] == 3
    c = res["concentration"]
    assert abs(c["hhi"] - 0.38) < 1e-9            # .25+.09+.04
    assert abs(c["effective_n"] - 1 / 0.38) < 1e-6
    assert c["top_weight"] == 0.5 and abs(c["top3_weight"] - 1.0) < 1e-9
    assert abs(res["sector_exposure"]["반도체"] - 0.8) < 1e-9
    assert abs(res["sector_exposure"]["자동차"] - 0.2) < 1e-9
    assert abs(res["valuation"]["weighted_pb"] - 1.7) < 1e-9   # .5*1+.3*2+.2*3
    assert res["valuation"]["weighted_pe"] is None
    assert abs(res["risk"]["avg_pairwise_corr"] - 1.0) < 1e-9  # 동일 경로
    assert res["risk"]["portfolio_vol_annualized"] > 0
    assert res["coverage"]["with_fundamentals"] == 3


def test_portfolio_equal_weight_default(monkeypatch):
    import quant_core.expression_parser as ep
    monkeypatch.setattr(ep, "get_symbol_group", lambda s, g="Industry": "Other")
    res = run_query(_diag_ir(), _DS_PF)            # weights 미지정 → 동일가중
    assert res["success"]
    for h in res["holdings"]:
        assert abs(h["weight"] - 1 / 3) < 1e-9
    assert abs(res["concentration"]["hhi"] - 1 / 3) < 1e-9


def test_portfolio_empty_rejected():
    ir = StrategyIR.model_validate({"universe": {"kind": "portfolio", "symbols": ["ZZZ"]},
                                    "signal": _CLOSE, "query": "describe"})
    res = run_query(ir, _DS_PF)                    # ZZZ 데이터 없음
    assert res["success"] is False


from quant_core.ir_engine.spec import validate_strategy

_FACTOR = {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}}


def _errs(d):
    return [i.rule for i in validate_strategy(StrategyIR.model_validate(d)) if i.is_error]


def test_validate_single_report_no_target_node_ok():
    # describe+single = 360 리포트 → target_node 불필요(분석 분포 아님).
    errs = _errs({"universe": {"kind": "single", "symbols": ["AAA"]},
                  "signal": _CLOSE, "query": "describe"})
    assert "S-target" not in errs


def test_validate_portfolio_ok():
    errs = _errs({"universe": {"kind": "portfolio", "symbols": ["AAA", "BBB"],
                               "weights": {"AAA": 0.6, "BBB": 0.4}},
                  "signal": _CLOSE, "query": "describe"})
    assert "S-PORT" not in errs and "S-target" not in errs


def test_validate_portfolio_requires_describe():
    errs = _errs({"universe": {"kind": "portfolio", "symbols": ["AAA"]},
                  "signal": _CLOSE, "query": "simulate"})
    assert "S-PORT" in errs        # portfolio는 진단(describe) 전용


def test_validate_portfolio_bad_weights():
    errs = _errs({"universe": {"kind": "portfolio", "symbols": ["AAA", "BBB"],
                               "weights": {"AAA": -0.5, "BBB": 0.4}},
                  "signal": _CLOSE, "query": "describe"})
    assert "S-PORT" in errs        # 음수 비중


def test_validate_universe_describe_still_requires_target_node():
    # describe+all = 팩터 분포 → target_node 여전히 필요(무변경).
    errs = _errs({"universe": {"kind": "all"}, "signal": _CLOSE, "query": "describe",
                  "position": {"entry": {"mode": "scheduled"}}})
    assert "S-target" in errs

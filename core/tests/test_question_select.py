import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.ir_engine.spec import StrategyIR

_SIG = {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}}
_BASE = {"universe": {"kind": "list", "symbols": ["AAA", "BBB"]}, "signal": _SIG}


def test_select_query_and_spec_parse():
    s = StrategyIR.model_validate({**_BASE, "query": "select",
                                   "select": {"top_n": 3, "descending": False,
                                              "display": ["pb_ratio"]}})
    assert s.query == "select"
    assert s.select is not None and s.select.top_n == 3
    assert s.select.descending is False and s.select.as_of == "latest"


def test_select_defaults():
    s = StrategyIR.model_validate({**_BASE, "query": "select", "select": {"top_n": 5}})
    assert s.select.descending is True and s.select.display == [] and s.select.top_pct is None


# ── 골든 스크린 (run_select — 합성 픽스처, 결정적) ────────────────────────────

import numpy as np
import pandas as pd
from quant_core.ir_engine import run_query


def _df(pb: float, mcap: float, n: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2021-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "Open": np.full(n, 100.0), "High": np.full(n, 101.0),
        "Low": np.full(n, 99.0), "Close": np.full(n, 100.0),
        "Volume": np.full(n, 1e6),
        "pb_ratio": np.full(n, pb),      # 종목별 상수(결정적 랭킹)
        "market_cap": np.full(n, mcap),
    }, index=idx)


# pb: A=0.8 B=3.0 C=1.2 D=0.5 E=2.0  (저평가=낮은 pb)  market_cap: D만 소형(5e10)
_DS = {"AAA": _df(0.8, 9e11), "BBB": _df(3.0, 9e11), "CCC": _df(1.2, 9e11),
       "DDD": _df(0.5, 5e10), "EEE": _df(2.0, 9e11)}
_PB = {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}}


def _select_ir(**select):
    return {"universe": {"kind": "list", "symbols": list(_DS)},
            "signal": _PB, "query": "select", "select": select}


def test_run_select_ranks_lowest_pb_first():
    res = run_query(__import__("quant_core.ir_engine.spec", fromlist=["StrategyIR"])
                    .StrategyIR.model_validate(_select_ir(top_n=3, descending=False,
                                                          display=["pb_ratio"])), _DS)
    assert res["success"] and res["query"] == "select"
    assert res["universe_size"] == 5 and res["eligible_size"] == 5
    syms = [r["symbol"] for r in res["results"]]
    assert syms == ["DDD", "AAA", "CCC"]                 # pb 0.5 < 0.8 < 1.2
    assert res["results"][0]["metrics"]["pb_ratio"] == 0.5
    assert res["results"][0]["score"] == 0.5


def test_run_select_screener_eligibility():
    # 대형주만(market_cap > 1e11) → DDD(소형) 제외 → 저pb 상위 2 = AAA, CCC
    ir = _select_ir(top_n=2, descending=False, display=["pb_ratio"])
    ir["universe"]["screener"] = {"condition": {
        "op": "compare", "params": {"op": ">"},
        "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.market_cap"}},
                   "right": {"op": "const", "params": {"value": 1e11}}}}}
    s = __import__("quant_core.ir_engine.spec", fromlist=["StrategyIR"]).StrategyIR.model_validate(ir)
    res = run_query(s, _DS)
    assert res["success"] and res["eligible_size"] == 4   # DDD 제외
    assert [r["symbol"] for r in res["results"]] == ["AAA", "CCC"]


def test_run_select_rejects_condition_signal():
    bad = {"universe": {"kind": "list", "symbols": ["AAA"]},
           "signal": {"op": "compare", "params": {"op": ">"},
                      "inputs": {"left": _PB, "right": {"op": "const", "params": {"value": 1.0}}}},
           "query": "select", "select": {"top_n": 1}}
    s = __import__("quant_core.ir_engine.spec", fromlist=["StrategyIR"]).StrategyIR.model_validate(bad)
    res = run_query(s, _DS)
    assert res["success"] is False     # score 아님 → 거부

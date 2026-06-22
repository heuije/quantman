"""뿌리② select.group_by(섹터별 top-N) + 뿌리③ name/code 부착 — run_select 계약 회귀."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import quant_core.expression_parser as ep  # noqa: E402
from quant_core.ir_engine import StrategyIR, run_query  # noqa: E402

_SEC = {"S1": "반도체", "S2": "반도체", "S3": "반도체",
        "S4": "배터리", "S5": "배터리", "S6": "배터리"}
_PB = {"S1": 0.5, "S2": 0.8, "S3": 1.2, "S4": 0.6, "S5": 0.9, "S6": 1.5}


def _ohlc(pb: float) -> pd.DataFrame:
    idx = pd.date_range("2022-01-03", periods=60, freq="B")
    close = np.full(60, 100.0)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": 1e6, "pb_ratio": pb}, index=idx)


def _ds() -> dict:
    return {s: _ohlc(pb) for s, pb in _PB.items()}


def _ir(**sel) -> dict:
    return {"universe": {"kind": "list", "symbols": list(_SEC)},
            "signal": {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}},
            "query": "select", "select": {"descending": False, **sel}}


def test_group_by_per_sector_top_n(monkeypatch):
    monkeypatch.setattr(ep, "get_symbol_group", lambda sym, gt="Industry": _SEC.get(sym, "기타"))
    res = run_query(StrategyIR.model_validate(_ir(top_n=2, group_by="Sector")), _ds())
    assert res.get("success"), res.get("error")
    assert res["group_by"] == "Sector"
    groups = {g["group"]: g["results"] for g in res["groups"]}
    assert set(groups) == {"반도체", "배터리"}                      # 멀티섹터 둘 다(반도체만 아님)
    assert len(groups["반도체"]) == 2 and len(groups["배터리"]) == 2  # 섹터별 top-2
    assert [r["code"] for r in groups["반도체"]] == ["S1", "S2"]     # 저평가(낮은 pb) 우선
    assert [r["code"] for r in groups["배터리"]] == ["S4", "S5"]


def test_results_have_name_and_code(monkeypatch):
    monkeypatch.setattr(ep, "get_symbol_group", lambda sym, gt="Industry": _SEC.get(sym, "기타"))
    res = run_query(StrategyIR.model_validate(_ir(top_n=3)), _ds())
    assert res.get("success"), res.get("error")
    r0 = res["results"][0]
    assert r0["code"] == r0["symbol"]              # 코드
    assert "name" in r0                            # 이름(자기서술)


def test_symbol_name_real_ticker():
    from quant_core.expression_parser import symbol_name
    assert symbol_name("005930") == "삼성전자"      # ticker_db.json(코어 metadata)
    assert symbol_name("999999") == "999999"       # 미수급 → 코드 폴백

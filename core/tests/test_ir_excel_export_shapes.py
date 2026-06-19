"""증빙 엑셀 — 모든 분석 형상(P2) MECE 검증.

엔진이 산출하는 12개 결과 형상(SIMULATE·SWEEP param/entity/label·PERIOD·EXTREMIZE·
SELECT·DESCRIBE single/portfolio/signal·RELATE ic/regression/event)을 합성 데이터로
실행 → build_strategy_excel 로 .xlsx 생성 → openpyxl 로 로드해 시트 구성·헤더·값·수식을
직접 검수한다. 엑셀이 챗봇 신뢰성의 증빙 수단이므로 형상별 빠짐없이 고정한다.

라이브 수식의 수치 평가는 openpyxl이 못 하므로, 수식 *문자열*과 엔진 값의 정합(예:
포트 HHI 수식 옆 엔진값)으로 검증한다.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from openpyxl import load_workbook

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.ir_engine import StrategyIR, build_strategy_excel, run_query


def _ohlc(close, extra=None):
    n = len(close)
    idx = pd.date_range("2020-01-02", periods=n, freq="B")
    close = np.asarray(close, dtype=float)
    op = np.concatenate([[close[0]], close[:-1]])
    d = {"Open": op, "High": np.maximum(op, close) * 1.001,
         "Low": np.minimum(op, close) * 0.999, "Close": close, "Volume": np.full(n, 1e6)}
    for k, v in (extra or {}).items():
        d[k] = np.full(n, v) if np.isscalar(v) else np.asarray(v, dtype=float)
    return pd.DataFrame(d, index=idx)


def _geom(drift, n):
    return 100.0 * (1 + drift) ** np.arange(n)


def ds_factor(n=300):
    return {"AAA": _ohlc(_geom(0.0010, n), {"momentum_12_1m": 3.0}),
            "BBB": _ohlc(_geom(-0.0005, n), {"momentum_12_1m": 1.0}),
            "CCC": _ohlc(_geom(0.0004, n), {"momentum_12_1m": 2.0})}


def ds_factor_kr(n=300):
    return {"005930": _ohlc(_geom(0.0010, n), {"momentum_12_1m": 3.0}),
            "000660": _ohlc(_geom(-0.0005, n), {"momentum_12_1m": 1.0}),
            "105560": _ohlc(_geom(0.0004, n), {"momentum_12_1m": 2.0})}


def ds_fund():
    pbs = {"AAA": 0.8, "BBB": 3.0, "CCC": 1.2, "DDD": 0.5, "EEE": 2.0}
    return {s: _ohlc(_geom(0.0, 120), {"pb_ratio": pb, "market_cap": 9e11}) for s, pb in pbs.items()}


def ds_single():
    return {"AAA": _ohlc(_geom(0.001, 260), {"pb_ratio": 1.5, "trailing_pe": 12.0})}


def ds_pf():
    return {"AAA": _ohlc(_geom(0.001, 120), {"pb_ratio": 1.0}),
            "BBB": _ohlc(_geom(-0.0005, 120), {"pb_ratio": 2.0}),
            "CCC": _ohlc(_geom(0.0008, 120), {"pb_ratio": 3.0})}


def ds_relate():
    out = {}
    for s, fv, gv in [("AAA", -0.02, 0.5), ("BBB", -0.01, -0.3), ("CCC", 0.0, 0.1),
                      ("DDD", 0.01, -0.2), ("EEE", 0.02, 0.4)]:
        r = (1 + 2 * fv) ** (1 / 5.0) - 1
        out[s] = _ohlc(_geom(r, 120), {"fac1": fv, "fac2": gv})
    return out


SIG_C = {"op": "data", "params": {"ref": "__SELF__.Close"}}
SIG_M = {"op": "data", "params": {"ref": "__SELF__.momentum_12_1m"}}
MA5 = {"op": "ts_mean", "params": {"window": 5}, "inputs": {"signal": SIG_C}}
EVENT = {"op": "compare", "params": {"op": ">"}, "inputs": {"left": SIG_C, "right": MA5}}
POS = {"direction": "long", "sizing": {"mode": "equal_weight"},
       "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 2}}
POS3 = {**POS, "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 3}}
SIM = {"initial_capital": 1e7}
LST3 = {"kind": "list", "symbols": ["AAA", "BBB", "CCC"]}
ALL = {"kind": "all"}

# (label, dataset, ir_dict, checks) — checks = {sheet: [반드시 포함된 문자열...]}
CASES = [
    ("simulate", ds_factor, {"universe": LST3, "signal": SIG_M, "position": POS, "simulation": SIM,
        "query": "simulate", "study": {"axis": "none"}},
     {"백테스트": ["일일 추가비용", "=(E", "STDEV.S"], "거래내역": ["종목"], "지표·설명": ["엔진 지표"]}),
    ("sweep_parameter", ds_factor, {"universe": ALL, "signal": SIG_M, "position": POS, "simulation": SIM,
        "query": "simulate", "study": {"axis": "parameter", "param_grid": [{"path": "simulation.commission", "values": [0.0, 0.001]}]}},
     {"스윕결과": ["commission=0.0", "commission=0.001", "CAGR(%)"], "설명": ["방법론"]}),
    ("sweep_entity", ds_factor, {"universe": ALL, "signal": SIG_M, "position": POS, "simulation": SIM,
        "query": "simulate", "study": {"axis": "entity", "assets": ["AAA", "BBB", "CCC"]}},
     {"스윕결과": ["AAA", "BBB", "CCC", "샤프"], "설명": ["방법론"]}),
    ("sweep_label", ds_factor_kr, {"universe": {"kind": "list", "symbols": ["005930", "000660", "105560"]},
        "signal": SIG_M, "position": POS3, "simulation": SIM, "query": "simulate",
        "study": {"axis": "label", "reduction": "contrast", "label": {"op": "attribute", "params": {"attr": "Industry"}}}},
     {"조건별성과": ["전체(포트폴리오)", "조건"], "설명": ["조건 대조"]}),
    ("period_split", ds_factor, {"universe": ALL, "signal": SIG_M, "position": POS, "simulation": SIM,
        "query": "simulate", "study": {"axis": "time_fold", "reduction": "consistency", "split_period": "year"}},
     {"연도별성과": ["연도", "엑셀 연수익", "2020"], "백테스트": ["라이브", "STDEV.S"], "원자료": ["원자료"]}),
    ("extremize", ds_factor, {"universe": ALL, "signal": SIG_M, "position": POS, "simulation": SIM,
        "query": "simulate", "study": {"axis": "entity", "reduction": "extremize", "assets": ["AAA", "BBB", "CCC"],
        "objective": {"metric": "cum_return", "direction": "max", "oos_guard": True}}},
     {"최적화결과": ["최적", "순위", "AAA"], "OOS검증": ["구간"], "설명": ["최적화"]}),
    ("select", ds_fund, {"universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC", "DDD", "EEE"]},
        "signal": {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}}, "query": "select",
        "select": {"top_n": 3, "descending": False, "display": ["pb_ratio"]}},
     {"스크리닝결과": ["순위", "DDD", "pb_ratio"], "설명": ["스크리닝"]}),
    ("describe_single", ds_single, {"universe": {"kind": "single", "symbols": ["AAA"]}, "signal": SIG_C, "query": "describe"},
     {"종목요약": ["현재가", "52주 고가", "=MAX(원자료", "STDEV.S(원자료"], "원자료": ["=B5/B4-1"], "설명": ["종목 분석"]}),
    ("describe_portfolio", ds_pf, {"universe": {"kind": "portfolio", "symbols": ["AAA", "BBB", "CCC"],
        "weights": {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}}, "signal": SIG_C, "query": "describe"},
     {"보유진단": ["=SUMPRODUCT(B4:B6,B4:B6)", "가중PBR"], "섹터노출": ["섹터"], "설명": ["포트폴리오"]}),
    ("describe_signal", ds_factor, {"universe": ALL, "signal": SIG_C, "query": "describe", "study": {"target_node": SIG_M}},
     {"신호분포": ["분위수", "q05"], "히스토그램": ["빈도"], "설명": ["신호 분포"]}),
    ("relate_ic", ds_relate, {"universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC", "DDD", "EEE"]},
        "signal": SIG_C, "query": "relate", "study": {"relation_kind": "ic",
        "target_node": {"op": "data", "params": {"ref": "__SELF__.fac1"}}, "windows": [5, 10]}},
     {"IC분석": ["윈도(일)", "평균IC(%)", "IR"], "설명": ["IC"]}),
    ("relate_regression", ds_relate, {"universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC", "DDD", "EEE"]},
        "signal": SIG_C, "query": "relate", "study": {"relation_kind": "regression",
        "factors": [{"op": "data", "params": {"ref": "__SELF__.fac1"}}, {"op": "data", "params": {"ref": "__SELF__.fac2"}}],
        "windows": [5, 10]}},
     {"회귀분석": ["fac1", "fac2", "계수(coef)"], "설명": ["Fama-MacBeth"]}),
    ("relate_event", ds_factor, {"universe": LST3, "signal": SIG_C, "query": "relate",
        "study": {"event": EVENT, "windows": [5, 10], "event_basis": "close"}},
     {"이벤트분석": ["윈도(일)", "평균MAE(%)", "페이오프"], "설명": ["이벤트 스터디"]}),
]


def _build(ds_fn, ir_dict):
    ds = ds_fn() if callable(ds_fn) else ds_fn
    ir = StrategyIR.model_validate(ir_dict)
    res = run_query(ir, ds)
    assert res.get("success"), f"엔진 실행 실패: {res.get('error')}"
    return ir, ds, res, build_strategy_excel(ir, ds, res)


def _sheet_text(ws) -> str:
    out = []
    for row in ws.iter_rows(values_only=True):
        for v in row:
            if v is not None:
                out.append(str(v))
    return "\n".join(out)


@pytest.mark.parametrize("label,ds_fn,ir_dict,checks", CASES, ids=[c[0] for c in CASES])
def test_shape_excel_structure(label, ds_fn, ir_dict, checks) -> None:
    """형상별: .xlsx 로드 + 기대 시트 존재 + 시트별 필수 문자열(헤더·값·수식) 포함."""
    _ir, _ds, _res, xlsx = _build(ds_fn, ir_dict)
    assert isinstance(xlsx, (bytes, bytearray)) and len(xlsx) > 4000
    wb = load_workbook(io.BytesIO(xlsx))
    for sheet, needles in checks.items():
        assert sheet in wb.sheetnames, f"[{label}] 시트 '{sheet}' 없음 — {wb.sheetnames}"
        text = _sheet_text(wb[sheet])
        for n in needles:
            assert n in text, f"[{label}] 시트 '{sheet}'에 '{n}' 없음"


def test_portfolio_hhi_formula_matches_engine() -> None:
    """포트 HHI 라이브 수식(=SUMPRODUCT)이 엔진 HHI 값과 일치(증빙 수식 정확성)."""
    _ir, _ds, res, xlsx = _build(ds_pf, {"universe": {"kind": "portfolio",
        "symbols": ["AAA", "BBB", "CCC"], "weights": {"AAA": 0.5, "BBB": 0.3, "CCC": 0.2}},
        "signal": SIG_C, "query": "describe"})
    ws = load_workbook(io.BytesIO(xlsx))["보유진단"]
    # B9=수식, C9=엔진값. 가중치 0.5/0.3/0.2 → HHI = 0.25+0.09+0.04 = 0.38.
    assert ws["B9"].value == "=SUMPRODUCT(B4:B6,B4:B6)"
    assert ws["C9"].value == pytest.approx(res["concentration"]["hhi"])
    assert ws["C9"].value == pytest.approx(0.38, abs=1e-9)


def test_all_shapes_have_methodology_sheet() -> None:
    """모든 P2 형상에 전략정의(IR)+방법론을 담은 '설명' 시트가 붙는다(증빙 꼬리표)."""
    for label, ds_fn, ir_dict, _checks in CASES:
        if label in ("simulate", "period_split"):
            continue   # 단일 자산곡선 형상은 '지표·설명'(simulate 시트 재사용)으로 동일 역할
        _ir, _ds, _res, xlsx = _build(ds_fn, ir_dict)
        wb = load_workbook(io.BytesIO(xlsx))
        assert "설명" in wb.sheetnames, f"[{label}] '설명' 시트 누락"
        assert "전략 정의 (IR)" in _sheet_text(wb["설명"])

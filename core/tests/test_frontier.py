"""Phase 5 프런티어 엔진 — 상관행렬·PRESCRIBE·breadth (엑셀 비경유 직접 검증).

새 형상은 시각화 전용(히트맵·트리맵·breadth)이라 excel 증빙 대상이 아니다 → BASE_CASES
(엑셀 결합)가 아닌 여기서 엔진+요약+result_shape를 직접 고정한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.ir_engine import (  # noqa: E402
    StrategyIR, build_strategy_excel, result_shape, run_query, strategy_from_spec, summarize_result,
)

# 분석 동사의 명목 신호(corpus와 동일 — 엔진이 가격/수익을 자동 조립)
_SIG = {"op": "data", "params": {"ref": "__SELF__.Close"}}


def _ohlc(close: np.ndarray, idx) -> pd.DataFrame:
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": 1e6}, index=idx)


# ── 5a 상관행렬 ───────────────────────────────────────────────────────────────

def _ds_corr() -> dict:
    """AAA·BBB 동행(상관↑) / CCC는 AAA 역행(상관↓) — 결정적."""
    idx = pd.date_range("2022-01-03", periods=120, freq="B")
    wave = np.cumsum(np.sin(np.linspace(0, 12, 120)) * 0.5)
    return {"AAA": _ohlc(100 + wave, idx),
            "BBB": _ohlc(100 + wave * 1.1 + 0.3, idx),     # AAA와 거의 동행
            "CCC": _ohlc(100 - wave * 0.9, idx)}           # AAA와 역행


def _corr_ir() -> dict:
    return {"universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC"]},
            "signal": _SIG, "query": "relate", "study": {"relation_kind": "correlation"}}


def test_correlation_shape_and_matrix():
    res = run_query(StrategyIR.model_validate(_corr_ir()), _ds_corr())
    assert res.get("success"), res.get("error")
    assert res["shape"] == "correlation_matrix" == result_shape(res)
    syms, m = res["symbols"], res["matrix"]
    assert set(syms) == {"AAA", "BBB", "CCC"}
    for i in range(len(syms)):
        assert abs(m[i][i] - 1.0) < 1e-6                   # 자기상관 1.0
    ia, ib, ic = syms.index("AAA"), syms.index("BBB"), syms.index("CCC")
    assert m[ia][ib] > 0.8                                 # 동행
    assert m[ia][ic] < -0.5                                # 역행
    assert res["least_correlated"][2] < 0                  # 분산 후보 = 음의 상관 쌍


def test_correlation_summary_surfaces_diversification():
    s = summarize_result(run_query(StrategyIR.model_validate(_corr_ir()), _ds_corr()))
    assert "상관행렬" in s and "분산" in s


def test_correlation_rejects_single_symbol():
    ir = {**_corr_ir(), "universe": {"kind": "single", "symbols": ["AAA"]}}
    res = strategy_from_spec(ir, _ds_corr())
    assert not res.get("success")                          # S-CORR: 2종목 이상 필요


# ── 5b PRESCRIBE (포트폴리오 비중 최적화) ─────────────────────────────────────

def _prescribe_ir(**ps) -> dict:
    ir = {"universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC"]},
          "signal": _SIG, "query": "prescribe"}
    if ps:
        ir["prescribe"] = ps
    return ir


def test_prescribe_objectives_long_only_sum_one():
    res = run_query(StrategyIR.model_validate(_prescribe_ir()), _ds_corr())
    assert res.get("success"), res.get("error")
    assert res["shape"] == "prescribe" == result_shape(res)
    objs = res["objectives"]
    assert set(objs) == {"min_variance", "max_sharpe", "risk_parity", "equal_weight"}
    for key, o in objs.items():
        w = list(o["weights"].values())
        assert abs(sum(w) - 1.0) < 2e-3, f"{key} 비중합≠1 (4자리 반올림 허용)"
        assert all(v >= -1e-9 for v in w), f"{key} 롱온리 위반"
        assert o["exp_vol"] is not None
    ew = objs["equal_weight"]["weights"]
    assert all(abs(v - 1 / 3) < 1e-3 for v in ew.values())            # 1/N (4자리 반올림)
    # 최소분산은 동일가중보다 변동성이 낮거나 같아야(최적화 작동 검증)
    assert objs["min_variance"]["exp_vol"] <= objs["equal_weight"]["exp_vol"] + 1e-6


def test_prescribe_max_weight_cap():
    res = run_query(StrategyIR.model_validate(_prescribe_ir(max_weight=0.4)), _ds_corr())
    assert res.get("success")
    for o in res["objectives"].values():
        assert all(v <= 0.4 + 1e-3 for v in o["weights"].values()), "max_weight 상한 위반"


def test_prescribe_summary_warns_mean_return_noise():
    s = summarize_result(run_query(StrategyIR.model_validate(_prescribe_ir()), _ds_corr()))
    assert "포트추천" in s and ("최소분산" in s or "최대샤프" in s)
    assert "⚠" in s and "과거평균" in s              # 기대수익 추정 노이즈 경고


def test_prescribe_rejects_single_symbol():
    ir = {**_prescribe_ir(), "universe": {"kind": "single", "symbols": ["AAA"]}}
    res = strategy_from_spec(ir, _ds_corr())
    assert not res.get("success")                     # S-PRESCRIBE: 2종목 이상


# ── 5c 시장 breadth ───────────────────────────────────────────────────────────

def _ds_breadth() -> dict:
    """5종목: 2개 상승(반도체)·3개 하락(자동차·화학) + 섹터 태깅 — 결정적 시장 폭."""
    idx = pd.date_range("2022-01-03", periods=80, freq="B")
    up, down = np.linspace(100, 130, 80), np.linspace(130, 100, 80)

    def _sec(close, sec):
        df = _ohlc(close, idx)
        df["sector"] = sec
        return df

    return {"AAA": _sec(up, "반도체"), "BBB": _sec(up * 0.99 + 1, "반도체"),
            "CCC": _sec(down, "자동차"), "DDD": _sec(down * 1.01, "자동차"),
            "EEE": _sec(down + 0.5, "화학")}


def _breadth_ir() -> dict:
    return {"universe": {"kind": "list", "symbols": ["AAA", "BBB", "CCC", "DDD", "EEE"]},
            "signal": _SIG, "query": "breadth"}


def test_breadth_counts_and_shape():
    res = run_query(StrategyIR.model_validate(_breadth_ir()), _ds_breadth())
    assert res.get("success"), res.get("error")
    assert res["shape"] == "breadth" == result_shape(res)
    assert res["n"] == 5
    assert res["n_up"] + res["n_down"] + res["n_flat"] == 5
    assert res["n_down"] >= 3                          # CCC·DDD·EEE 하락
    assert 0.0 <= res["pct_above_ma20"] <= 1.0
    secs = {k: v for k, v, _ in res["sector_breakdown"]}
    assert "반도체" in secs and "자동차" in secs        # 섹터 분해
    assert secs["자동차"] < 0 < secs["반도체"]          # 약세/강세 방향


def test_breadth_summary():
    s = summarize_result(run_query(StrategyIR.model_validate(_breadth_ir()), _ds_breadth()))
    assert "breadth" in s and "상승" in s and "20일선" in s


def test_breadth_rejects_single_symbol():
    ir = {**_breadth_ir(), "universe": {"kind": "single", "symbols": ["AAA"]}}
    res = strategy_from_spec(ir, _ds_breadth())
    assert not res.get("success")                     # S-BREADTH


# ── 회귀가드: prescribe·breadth가 정식 검증경로(strategy_from_spec)도 통과해야 한다 ──
# (run_query만 테스트하면 is_research 누락으로 S-entry 거부되는 버그를 놓친다 — 실제 챗 경로 보장)

def test_prescribe_passes_full_validation():
    res = strategy_from_spec(_prescribe_ir(), _ds_corr())
    assert res.get("success"), res.get("error")       # is_research 포함 — S-entry 거부 안 됨
    assert res["shape"] == "prescribe"


def test_breadth_passes_full_validation():
    res = strategy_from_spec(_breadth_ir(), _ds_breadth())
    assert res.get("success"), res.get("error")
    assert res["shape"] == "breadth"


def test_correlation_excel_rejected():
    """상관(correlation)은 엑셀 비대상 — _build_relation(ic/regression용)으로 흘리지 않고 거부."""
    import pytest
    res = run_query(StrategyIR.model_validate(_corr_ir()), _ds_corr())
    with pytest.raises(ValueError):
        build_strategy_excel(_corr_ir(), _ds_corr(), res)

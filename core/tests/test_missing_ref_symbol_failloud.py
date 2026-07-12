"""참조 심볼 부재 fail-loud 회귀 — "IR이 참조하는 심볼이 dataset에 없음" 부류.

이벤트스터디가 VIX 같은 외부 심볼을 참조하는데 그 심볼이 dataset dict에 없거나
(parquet 부재 → 로더가 키 생략) 키만 있고 df가 None/비면, 이벤트 패널이 NaN→False로
조용히 0건이 되어 '경향 없음'처럼 읽힌다(조용한 오답). 규칙0(R0)이 두 경우 모두
정직 거부("데이터 없음")하는지 strategy_from_spec 단일 검증 경로(챗 _run_engine·
/ir/strategy 공용) 기준으로 잠근다.
"""
import numpy as np
import pandas as pd

from quant_core.ir_engine.service import strategy_from_spec

_EVENT = {"op": "compare", "params": {"op": ">"},
          "inputs": {"left": {"op": "data", "params": {"ref": "VIX.pct_change_1d"}},
                     "right": {"op": "const", "params": {"value": 5.0}}}}


def _ir(param_grid=None):
    study = {"event": _EVENT, "windows": [5, 20], "event_basis": "close"}
    if param_grid is not None:
        study.update({"axis": "parameter", "param_grid": param_grid})
    return {"universe": {"kind": "single", "symbols": ["S&P500"]},
            "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
            "query": "relate", "study": study}


_GRID = [{"path": "study.event.inputs.right.params.value", "values": [3.0, 5.0]}]


def _spx(n=120):
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = np.linspace(100.0, 180.0, n)
    return pd.DataFrame({"Open": close, "High": close, "Low": close,
                         "Close": close, "Volume": np.full(n, 1e6)}, index=idx)


def _vix(n=120):
    """pct_change_1d가 주기적으로 임계(5.0)를 넘는 결정적 시계열."""
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = np.full(n, 20.0)
    df = pd.DataFrame({"Open": close, "High": close, "Low": close,
                       "Close": close, "Volume": np.full(n, 1e6)}, index=idx)
    pct = np.zeros(n)
    pct[10::10] = 10.0
    df["pct_change_1d"] = pct
    return df


def _assert_r0_refused(res):
    assert not res.get("success"), res
    assert "데이터 없음" in (res.get("error") or ""), res.get("error")
    assert any(i["rule"] == "R0" for i in res.get("issues") or []), res.get("issues")


def test_missing_key_refused_event_study():
    """키 자체 부재(parquet 부재 → 로더 생략) — 단일 이벤트 스터디 거부."""
    _assert_r0_refused(strategy_from_spec(_ir(), {"S&P500": _spx()}))


def test_missing_key_refused_event_sweep():
    """키 자체 부재 — 이벤트×param_grid 경로도 동일 거부(격자 셀 이전, 최상위 검증)."""
    _assert_r0_refused(strategy_from_spec(_ir(_GRID), {"S&P500": _spx()}))


def test_empty_df_key_refused_event_sweep():
    """키는 있으나 df가 빈 경우(빈 parquet 유입) — 키 존재만으로 R0를 통과해
    success=True·0건 버킷으로 조용히 끝나던 구멍. 거부로 마감."""
    _assert_r0_refused(strategy_from_spec(_ir(_GRID), {"S&P500": _spx(),
                                                       "VIX": pd.DataFrame()}))


def test_none_df_key_refused_event_study():
    """키는 있으나 df가 None인 경우 — 빈 df와 같은 부류."""
    _assert_r0_refused(strategy_from_spec(_ir(), {"S&P500": _spx(), "VIX": None}))


def test_present_ref_symbol_still_runs():
    """대조군 — 참조 심볼이 실데이터로 있으면 과잉 거부 없이 이벤트가 집계된다."""
    res = strategy_from_spec(_ir(), {"S&P500": _spx(), "VIX": _vix()})
    assert res.get("success"), res.get("error")
    assert res.get("n_events", 0) > 0

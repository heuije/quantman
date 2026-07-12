"""WS1 — 이벤트 이전(pre-event) 음수 윈도우 1급 지원 (IR 대수 캠페인).

prod 실수요(3대화: '급등 전 조짐'·'VIX 급등 전 하락')의 근본 지원. PR#359 P0가
'정직 거부'로 막은 음수 창을 계약 위에서 '지원'으로 승격한다.

의미론(forward와 대칭 — 창 시작점 anchor):
  w>0: anchor=이벤트일, endpoint=이벤트 후 w일 누적수익, MAE/MFE=그 경로 극값
  w<0: anchor=이벤트 |w|일 전, endpoint=이벤트일까지의 누적수익(전조 구간 수익),
       MAE/MFE=그 구간 경로 극값. wrap 금지 — 조기 이벤트(p+w<0)는 탈락(창별 n 회계).
  basis=intraday의 음수 창은 계약이 정직 거부(장중 전조는 분봉 필요 — 데이터 범위 밖).
"""
import numpy as np
import pandas as pd
import pytest

from quant_core.ir_engine.run import _event_paths
from quant_core.ir_engine.spec import StrategyIR, validate_strategy


# ── 엔진 경로 산술 (합성 배열 — 손계산 대조) ──────────────────────────────────

CA = np.array([100.0, 110.0, 121.0, 133.1, 146.41, 161.051, 177.156])  # +10%/일
OA = CA.copy()


def test_negative_window_endpoint_is_prewindow_cumreturn():
    """w=-3, p=5: anchor=ca[2], endpoint=ca[5]/ca[2]-1 (이벤트 이전 3일 누적수익)."""
    got = _event_paths(CA, OA, p=5, w=-3, basis="close", mvals=None)
    assert got is not None
    end, mae, mfe = got
    assert end == pytest.approx(CA[5] / CA[2] - 1.0)
    # 단조 상승 경로 — MAE=첫 스텝 수익(최소), MFE=endpoint(최대)
    assert mae == pytest.approx(CA[3] / CA[2] - 1.0)
    assert mfe == pytest.approx(end)


def test_negative_window_too_early_event_dropped_no_wrap():
    """조기 이벤트(p+w<0)는 None — 음수 인덱스 wrap으로 미래를 '전조'로 집계하던 부류의 반대 방어."""
    assert _event_paths(CA, OA, p=2, w=-4, basis="close", mvals=None) is None
    assert _event_paths(CA, OA, p=0, w=-1, basis="close", mvals=None) is None


def test_negative_window_excess_subtracts_market_anchored_same():
    """excess: 같은 창 시작점 anchor로 시장 누적을 차감(대칭)."""
    mvals = np.array([100.0, 105.0, 110.25, 115.7625, 121.550625, 127.628156, 134.009564])
    got = _event_paths(CA, OA, p=5, w=-3, basis="excess", mvals=mvals)
    assert got is not None
    end = got[0]
    expected = (CA[5] / CA[2] - 1.0) - (mvals[5] / mvals[2] - 1.0)
    assert end == pytest.approx(expected)


def test_negative_window_intraday_still_none():
    """intraday basis의 음수 창은 엔진도 None(계약이 막지만 저장 IR·직접 호출 방어)."""
    assert _event_paths(CA, OA, p=5, w=-3, basis="intraday", mvals=None) is None


def test_forward_semantics_unchanged():
    """기존 forward(w>0) 산술은 byte-identical 보존."""
    got = _event_paths(CA, OA, p=2, w=3, basis="close", mvals=None)
    assert got is not None
    assert got[0] == pytest.approx(CA[5] / CA[2] - 1.0)


# ── 계약(validator) — 음수 창 승격 + intraday 정직 거부 ───────────────────────

def _event_ir(windows, basis="close"):
    return StrategyIR.model_validate({
        "name": "전조", "universe": {"kind": "single", "symbols": ["005930"]},
        "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}, "query": "relate",
        "study": {"event": {"op": "compare", "params": {"op": ">"},
                            "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                                       "right": {"op": "const", "params": {"value": 100.0}}}},
                  "windows": windows, "event_basis": basis}})


def _errors(ir):
    return [i for i in validate_strategy(ir) if getattr(i, "severity", 0) >= 30
            or getattr(i, "is_error", False)]


def test_validator_accepts_negative_windows_close_basis():
    """음수 창 + basis=close = 지원(더 이상 S-event 거부 아님) — conv#50 부류 승격."""
    assert not _errors(_event_ir([-240, -120, 20]))


def test_validator_accepts_negative_windows_excess_basis():
    ir = StrategyIR.model_validate({
        "name": "전조", "universe": {"kind": "list", "symbols": ["005930", "000660"]},
        "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}, "query": "relate",
        "study": {"event": {"op": "compare", "params": {"op": ">"},
                            "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                                       "right": {"op": "const", "params": {"value": 100.0}}}},
                  "windows": [-60, 20], "event_basis": "excess"}})
    assert not _errors(ir)


def test_validator_rejects_negative_intraday_honestly():
    """intraday 기준 음수 창 = 정직 거부(장중 전조는 분봉 필요 — 데이터 범위 밖)."""
    errs = _errors(_event_ir([-5], basis="intraday"))
    assert errs and any("이전" in e.message or "전조" in e.message or "음수" in e.message
                        for e in errs)


def test_validator_still_rejects_zero_for_close_basis():
    """w=0(당일)은 intraday 전용 — close 기준에선 여전히 거부(기존 계약 보존)."""
    assert _errors(_event_ir([0, 20]))


def test_contract_advertises_prewindow_support():
    """계약 광고(does/not_supported)가 전조 지원을 반영 — 컴파일러 지원_한계 파생의 원천."""
    from quant_core.ir_engine.contracts import REGISTRY
    c = REGISTRY["relate.event_study"]
    text = (c.does or "") + (c.use_for or "")
    assert "이전" in text or "음수" in text or "전조" in text
    assert not any("전조 질문은 음수 윈도우를 만들지 말고" in ns for ns in (c.not_supported or ()))


# ── e2e — 합성 데이터셋으로 음수 창 요약(집계·회계) ───────────────────────────

def test_run_event_study_prewindow_e2e():
    from quant_core.ir_engine.service import strategy_from_spec
    idx = pd.bdate_range("2024-01-01", periods=40)
    close = pd.Series(np.linspace(100, 178, 40), index=idx)   # 단조 상승
    df = pd.DataFrame({"Open": close.values, "High": close.values,
                       "Low": close.values, "Close": close.values,
                       "Volume": np.full(40, 1e6)}, index=idx)
    # 이벤트 = Close > 170 (마지막 몇 일) — pre-창 -5는 산출 가능, -30은 대부분 가능
    spec = {"name": "전조 e2e", "universe": {"kind": "single", "symbols": ["TEST"]},
            "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}, "query": "relate",
            "study": {"event": {"op": "compare", "params": {"op": ">"},
                                "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                                           "right": {"op": "const", "params": {"value": 170.0}}}},
                      "windows": [-5, 2], "event_basis": "close"}}
    res = strategy_from_spec(spec, {"TEST": df})
    assert res.get("success"), res.get("error")
    assert res.get("shape", "event_study") in ("event_study",)
    assert set(res["windows"]) == {"-5", "2"}
    o = res["overall"]["-5"]
    assert o["n"] >= 1 and o["mean"] is not None and o["mean"] > 0   # 상승 전조(단조 상승)
    # 탈락 회계 — 창별 평가 수가 회계에 반영
    assert "-5" in res["accounting"]["evaluated_by_window"]

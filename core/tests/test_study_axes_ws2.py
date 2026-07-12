"""WS2 — Study 축 조합성: 이벤트×파라미터 격자(W2a) + 구조 대안 variant 축(W2b).

prod 실수요: conv#22/#27 'VIX 급등 임계값·윈도우별로 가장 두드러진 경향 찾기'(이벤트×sweep이
2-D 거부 → 봇이 수동 순차 sweep하다 사망) · conv#33 '진입조건 A/B/C/D 비교'(1콜 불가·조건 A만).
설계: 코호트 버킷 기계 재사용(신규 shape 0) — 이벤트 조합은 shape=cohort(행축만 파라미터/조건),
simulate variant는 기존 sweep 버킷. '격자/대안이 있는데 조용히 무시' 부류는 필드 존재 분기로 차단.
"""
import numpy as np
import pandas as pd
import pytest

from quant_core.ir_engine.contracts import REGISTRY, resolve_runner
from quant_core.ir_engine.service import strategy_from_spec
from quant_core.ir_engine.spec import StrategyIR, validate_strategy

_C = {"op": "data", "params": {"ref": "__SELF__.Close"}}


def _event_gt(value):
    return {"op": "compare", "params": {"op": ">"},
            "inputs": {"left": _C, "right": {"op": "const", "params": {"value": value}}}}


def _df(n=40):
    idx = pd.bdate_range("2024-01-01", periods=n)
    close = np.linspace(100, 178, n)
    return pd.DataFrame({"Open": close, "High": close, "Low": close,
                         "Close": close, "Volume": np.full(n, 1e6)}, index=idx)


def _errors(ir):
    return [i for i in validate_strategy(ir) if getattr(i, "is_error", False)]


# ── 디스패치(resolve_runner) — 조합 분기 + 조용한 무시 차단 ───────────────────

def _relate_ir(study_extra):
    return StrategyIR.model_validate({
        "universe": {"kind": "single", "symbols": ["TEST"]}, "signal": _C, "query": "relate",
        "study": {"event": _event_gt(170.0), "windows": [-5, 2], "event_basis": "close",
                  **study_extra}})


def test_resolver_event_param_grid_routes_to_event_sweep():
    ir = _relate_ir({"axis": "parameter",
                     "param_grid": [{"path": "study.event.inputs.right.params.value",
                                     "values": [165.0, 172.0]}]})
    assert resolve_runner(ir) == "relate.event_sweep"


def test_resolver_event_param_grid_without_axis_still_routes():
    """axis 누락 + param_grid 존재 = 조용한 무시 부류 → 필드 존재로 라우팅."""
    ir = _relate_ir({"param_grid": [{"path": "study.event.inputs.right.params.value",
                                     "values": [165.0, 172.0]}]})
    assert resolve_runner(ir) == "relate.event_sweep"


def test_resolver_event_variants_routes():
    ir = _relate_ir({"axis": "variant",
                     "variants": [{"name": "A", "node": _event_gt(165.0)},
                                  {"name": "B", "node": _event_gt(172.0)}]})
    assert resolve_runner(ir) == "relate.event_variants"


def test_resolver_simulate_variants_routes_even_without_axis():
    ir = StrategyIR.model_validate({
        "universe": {"kind": "single", "symbols": ["TEST"]},
        "signal": _event_gt(150.0), "query": "simulate",
        "position": {"entry": {"mode": "on_signal"}, "exit": {"hold_days": 2}},
        "study": {"variants": [{"name": "A안", "node": _event_gt(150.0)},
                               {"name": "B안", "node": _event_gt(170.0)}]}})
    assert resolve_runner(ir) == "simulate.sweep.variant"


def test_contracts_registered():
    for key in ("relate.event_sweep", "relate.event_variants", "simulate.sweep.variant"):
        c = REGISTRY[key]
        assert c.does and c.use_for and c.shape


# ── 검증기 — 조합 허용·모호 조합 거부 ─────────────────────────────────────────

def test_validator_accepts_event_param_grid():
    ir = _relate_ir({"axis": "parameter",
                     "param_grid": [{"path": "study.event.inputs.right.params.value",
                                     "values": [165.0, 172.0]}]})
    assert not _errors(ir)


def test_validator_rejects_variants_with_param_grid():
    ir = _relate_ir({"axis": "variant",
                     "variants": [{"name": "A", "node": _event_gt(165.0)},
                                  {"name": "B", "node": _event_gt(172.0)}],
                     "param_grid": [{"path": "study.windows", "values": [[5]]}]})
    assert any("동시에" in e.message for e in _errors(ir))


def test_validator_rejects_single_variant():
    ir = _relate_ir({"axis": "variant", "variants": [{"name": "A", "node": _event_gt(165.0)}]})
    assert _errors(ir)


def test_validator_rejects_duplicate_variant_names():
    ir = _relate_ir({"axis": "variant",
                     "variants": [{"name": "A", "node": _event_gt(165.0)},
                                  {"name": "A", "node": _event_gt(172.0)}]})
    assert any("name" in e.message or "서로 달라" in e.message for e in _errors(ir))


def test_validator_rejects_time_fold_with_variants():
    ir = StrategyIR.model_validate({
        "universe": {"kind": "single", "symbols": ["TEST"]},
        "signal": _event_gt(150.0), "query": "simulate",
        "study": {"axis": "time_fold", "split_period": "year",
                  "variants": [{"name": "A", "node": _event_gt(150.0)},
                               {"name": "B", "node": _event_gt(170.0)}]}})
    assert any(e.rule == "S-split" for e in _errors(ir))


# ── e2e — 합성 데이터 ─────────────────────────────────────────────────────────

def test_event_sweep_e2e_buckets_differ_by_threshold():
    """conv#22 부류: 임계값 격자별 이벤트 스터디 — 버킷별 n_events가 임계에 따라 달라진다."""
    spec = {"name": "이벤트 격자", "universe": {"kind": "single", "symbols": ["TEST"]},
            "signal": _C, "query": "relate",
            "study": {"axis": "parameter", "event": _event_gt(170.0),
                      "windows": [-5, 2], "event_basis": "close",
                      "param_grid": [{"path": "study.event.inputs.right.params.value",
                                      "values": [160.0, 172.0]}]}}
    res = strategy_from_spec(spec, {"TEST": _df()})
    assert res.get("success"), res.get("error")
    assert res["shape"] == "cohort" and res["row_axis"] == "파라미터"
    b = res["buckets"]
    assert set(b) == {"value=160.0", "value=172.0"}
    assert b["value=160.0"]["n_events"] > b["value=172.0"]["n_events"] > 0
    assert "-5" in (b["value=160.0"]["overall"] or {})          # 전조 창도 버킷에 산다(WS1 조합)


def test_event_variants_e2e_named_buckets():
    """conv#33 부류: 조건 A/B 대안 비교 1콜 — 이름 키 버킷."""
    spec = {"name": "조건 대안", "universe": {"kind": "single", "symbols": ["TEST"]},
            "signal": _C, "query": "relate",
            "study": {"axis": "variant", "windows": [2], "event_basis": "close",
                      "event": _event_gt(170.0),
                      "variants": [{"name": "느슨(160)", "node": _event_gt(160.0)},
                                   {"name": "엄격(172)", "node": _event_gt(172.0)}]}}
    res = strategy_from_spec(spec, {"TEST": _df()})
    assert res.get("success"), res.get("error")
    assert res["shape"] == "cohort" and res["row_axis"] == "조건"
    assert set(res["buckets"]) == {"느슨(160)", "엄격(172)"}
    assert res["buckets"]["느슨(160)"]["n_events"] > res["buckets"]["엄격(172)"]["n_events"]


def test_simulate_variants_e2e_sweep_buckets():
    """G12: 신호 구조 대안 백테스트 비교 — 기존 sweep 버킷 형상(성과 지표 키)."""
    spec = {"name": "신호 대안", "universe": {"kind": "single", "symbols": ["TEST"]},
            "signal": _event_gt(150.0), "query": "simulate",
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "on_signal"}, "exit": {"hold_days": 2}},
            "simulation": {"initial_capital": 1e8},
            "study": {"axis": "variant",
                      "variants": [{"name": "이른 진입", "node": _event_gt(120.0)},
                                   {"name": "늦은 진입", "node": _event_gt(170.0)}]}}
    res = strategy_from_spec(spec, {"TEST": _df()})
    assert res.get("success"), res.get("error")
    assert res.get("shape") == "sweep" and res["axis"] == "variant"
    assert set(res["buckets"]) == {"이른 진입", "늦은 진입"}
    assert "cum_return" in res["buckets"]["이른 진입"]          # 백테스트 지표 버킷

"""param_manifest — 실시간 변수조정용 '조정 가능 변수' 매니페스트 검증.

현재 IR에 의미 있는 수치/enum 노브만 노출하는지(없는 필드 나열 금지), path가 IR 재구성에
쓸 점경로인지, simulate/select/describe별로 적절한 집합인지 고정한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

from quant_core.ir_engine import StrategyIR, param_manifest

_SIG = {"op": "data", "params": {"ref": "__SELF__.momentum_12_1m"}}
_SIG_SCORE = {"op": "data", "params": {"ref": "__SELF__.pb_ratio"}}


def _paths(manifest):
    return {p["path"] for p in manifest}


def test_simulate_exposes_cost_capital_and_entry_knobs():
    ir = StrategyIR.model_validate({
        "universe": {"kind": "all"}, "signal": _SIG, "query": "simulate",
        "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                     "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 5},
                     "exit": {"hold_days": 20, "stop_loss": -8.0}},
        "simulation": {"initial_capital": 5e7}})
    m = param_manifest(ir)
    paths = _paths(m)
    # 항상 노출되는 비용·자본 노브(엑셀 노란셀 격)
    assert {"simulation.commission", "simulation.slippage", "simulation.initial_capital"} <= paths
    # 진입·청산 — 설정된 것
    assert {"position.entry.top_n", "position.entry.rebalance",
            "position.exit.hold_days", "position.exit.stop_loss"} <= paths
    by = {p["path"]: p for p in m}
    assert by["position.entry.top_n"]["value"] == 5 and by["position.entry.top_n"]["type"] == "number"
    assert by["position.entry.rebalance"]["type"] == "select"
    assert "monthly" in by["position.entry.rebalance"]["options"]
    assert by["simulation.initial_capital"]["value"] == 5e7
    # commission 미설정 → 엔진 기본값을 시작값으로(0 아님)
    assert by["simulation.commission"]["value"] is not None


def test_simulate_omits_unset_exit_rules():
    """설정 안 한 청산 규칙(익절·트레일링)은 노출 안 함 — 빈 필드 나열 금지."""
    ir = StrategyIR.model_validate({
        "universe": {"kind": "all"}, "signal": _SIG, "query": "simulate",
        "position": {"entry": {"mode": "scheduled", "rebalance": "weekly", "top_n": 3}},
        "simulation": {}})
    paths = _paths(param_manifest(ir))
    assert "position.exit.take_profit" not in paths
    assert "position.exit.trail_pct" not in paths
    assert "position.exit.hold_days" not in paths


def test_select_exposes_top_n_and_descending():
    ir = StrategyIR.model_validate({
        "universe": {"kind": "list", "symbols": ["A", "B", "C"]}, "signal": _SIG_SCORE,
        "query": "select", "select": {"top_n": 10, "descending": False, "display": ["pb_ratio"]}})
    by = {p["path"]: p for p in param_manifest(ir)}
    assert by["select.top_n"]["value"] == 10
    assert by["select.descending"]["type"] == "bool" and by["select.descending"]["value"] is False


def test_describe_has_no_adjustable_params():
    """describe(360 리포트)는 조정 노브 없음 → 빈 매니페스트(웹 패널 미노출)."""
    ir = StrategyIR.model_validate({
        "universe": {"kind": "single", "symbols": ["A"]},
        "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}, "query": "describe"})
    assert param_manifest(ir) == []

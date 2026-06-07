import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from quant_core.ir_engine.spec import StrategyIR

_BASE = {"universe": {"kind": "single", "symbols": ["AAA"]},
         "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
         "position": {"entry": {"mode": "always"}}}
def _m(**extra): return StrategyIR.model_validate({**_BASE, **extra})

def test_legacy_return_none_to_simulate():
    s = _m(sweep={"axis": "none", "target": "return"})
    assert s.query == "simulate" and s.study.axis == "none"
def test_legacy_parameter_axis():
    s = _m(sweep={"axis": "parameter", "param_grid": [{"path": "simulation.commission", "values": [0, 0.1]}]})
    assert s.query == "simulate" and s.study.axis == "parameter" and len(s.study.param_grid) == 1
def test_legacy_asset_to_entity():
    s = _m(sweep={"axis": "asset", "assets": ["AAA", "BBB"]})
    assert s.study.axis == "entity" and s.study.assets == ["AAA", "BBB"]
def test_legacy_condition_to_label_contrast():
    s = _m(sweep={"axis": "condition", "label": {"op": "calendar", "params": {"unit": "weekday"}}})
    assert s.study.axis == "label" and s.study.reduction == "contrast" and s.study.label is not None
def test_legacy_target_signal_to_describe():
    s = _m(sweep={"target": "signal", "target_node": {"op": "data", "params": {"ref": "__SELF__.rsi_14"}}})
    assert s.query == "describe" and s.study.target_node is not None
def test_legacy_target_relation_to_relate_ic():
    s = _m(sweep={"target": "relation", "target_node": {"op": "rank", "inputs": {"signal": {"op": "data", "params": {"ref": "momentum_12_1m"}}}}})
    assert s.query == "relate" and s.study.event is None
def test_legacy_axis_time_to_relate_event():
    s = _m(sweep={"axis": "time", "event": {"op": "compare", "params": {"op": "<"}, "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.rsi_14"}}, "right": {"op": "const", "params": {"value": 30}}}}})
    assert s.query == "relate" and s.study.event is not None
def test_legacy_period_split_oos_to_time_fold():
    s = StrategyIR.model_validate({**_BASE, "simulation": {"period_split": "oos"}})
    assert s.query == "simulate" and s.study.axis == "time_fold" and s.study.reduction == "consistency" and s.study.folds == 2
def test_new_form_passthrough():
    s = _m(query="simulate", study={"axis": "parameter", "reduction": "enumerate", "param_grid": [{"path": "simulation.leverage", "values": [1, 2]}]})
    assert s.query == "simulate" and s.study.axis == "parameter"

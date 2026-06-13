"""파싱 경계 마이그레이션 — 동결 레거시 IR(sweep/period_split) → 신 query/study 결정적 변환.

[결함 K — 라이브 실증] 로컬앱 ledger는 매수 시점 StrategyIR 전체를 JSON으로 동결
저장한다(자기완결 원장). 34ea912 클린 컷오버가 레거시 sweep/period_split 키를 loud
거부로 바꾸자, 구 스키마로 매수된 보유 포지션이 "IR strat 파싱 실패"로 관리 불능
(청산·손절·리밸런스 적용 불가)이 됐다. 스키마는 진화하고 동결 IR은 남는다 —
파싱 경계(StrategyIR before-validator)에 레거시→신 변환 단계를 둬 부류로 닫는다.

계약(이 파일이 잠그는 것):
  ① 레거시 키만 있으면 edc051d의 검증된 셰임과 동일 규칙으로 결정적 변환
     — 거래 의미(signal/universe/position/simulation)는 그대로, 리서치 평면만 매핑.
  ② 신형태 입력은 변환 no-op(멱등).
  ③ 입력 dict 비변경 — 원장(ledger) dict를 파싱이 부수효과로 고쳐쓰지 않는다.
  ④ 레거시+신 키 동시 존재는 동결 원장에서 불가능한 모순 → 종전대로 loud 거부
     (extra="ignore"의 silent-drop 방지 — NL repair loop가 교정).

    pytest core/tests/test_question_migration.py -v
"""
import copy
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

from quant_core.ir_engine.spec import StrategyIR  # noqa: E402

_BASE = {"universe": {"kind": "single", "symbols": ["AAA"]},
         "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
         "position": {"entry": {"mode": "always"}}}


def _m(**extra):
    return StrategyIR.model_validate({**_BASE, **extra})


# ── 신 형태 통과 (변경 없음) ───────────────────────────────────────────────────

def test_new_form_default():
    """query/study 미지정 → 기본 simulate·axis=none."""
    s = StrategyIR.model_validate(_BASE)
    assert s.query == "simulate" and s.study.axis == "none"


def test_new_form_parameter_passthrough():
    s = StrategyIR.model_validate({**_BASE, "query": "simulate", "study": {
        "axis": "parameter", "reduction": "enumerate",
        "param_grid": [{"path": "simulation.leverage", "values": [1, 2]}]}})
    assert s.query == "simulate" and s.study.axis == "parameter" and len(s.study.param_grid) == 1


def test_new_form_time_fold_passthrough():
    """기간분할 신형태 — study.axis=time_fold + folds/split_dates(study 내부)는 정상 통과."""
    s = StrategyIR.model_validate({**_BASE, "study": {
        "axis": "time_fold", "reduction": "consistency", "folds": 2,
        "split_dates": ["2022-01-01"]}})
    assert s.study.axis == "time_fold" and s.study.folds == 2
    assert s.study.split_dates == ["2022-01-01"]   # study 내부 split_dates는 신형태(거부 안 됨)


# ── 결함 K 재현 — 실물 ledger 동결 형태 (합성 픽스처) ──────────────────────────
# ~/.quant-platform/ledger.json 의 실패 포지션(0000J0/0000Z0/0126Z0) definition과
# 동일 구조: 컷오버 전 model_dump 전체 동결 — 레거시 SweepSpec 전 필드(all-default)
# + simulation.period_split/split_dates + extra "engine" 키. 계좌·잔고 데이터 없음.

def _ledger_frozen_definition() -> dict:
    return {
        "engine": "ir",                      # extra 키 — extra="ignore"로 무시돼야 함
        "name": "KOSPI 시총 상위 · 이평선 선별 (합성)",
        "universe": {"kind": "list", "symbols": ["000020", "005930", "000660"],
                     "screener": None, "exclude_macro": True},
        "signal": {"op": "compare",
                   "inputs": {"left": {"op": "data", "inputs": {},
                                       "params": {"ref": "__SELF__.Close"}},
                              "right": {"op": "ts_mean",
                                        "inputs": {"signal": {"op": "data", "inputs": {},
                                                              "params": {"ref": "__SELF__.Close"}}},
                                        "params": {"window": 60}}},
                   "params": {"op": "<="}},
        "position": {
            "direction": "long",
            "sizing": {"mode": "equal_weight", "amount_pct": 10.0, "amount_krw": None,
                       "target_vol_pct": None, "weights": None, "vol_window": 20,
                       "max_position_pct": 100.0},
            "entry": {"mode": "scheduled", "rebalance": "monthly", "every_n_days": None,
                      "top_n": 2, "top_pct": None, "threshold": None, "refill": "cash"},
            "exit": {"hold_days": None, "take_profit": None, "stop_loss": -7.0,
                     "trail_pct": None, "trail_atr_mult": None, "condition": None},
            "overlays": {"vol_target": None, "turnover_damp": None, "max_drawdown_stop": None,
                         "max_drawdown_soft": None, "max_group_pct": None, "group_label": None}},
        "simulation": {"initial_capital": 10_000_000.0, "delay": 1, "fill": "next_open",
                       "commission": None, "slippage": None, "sell_tax": None, "leverage": 1.0,
                       "short_borrow_pct": None, "funding_cost_pct": None, "rfr_pct": None,
                       "maintenance_margin_pct": None, "start": None, "end": None,
                       "period_split": "single", "split_dates": [],
                       "roll_method": None, "series_adjust": None, "roll_cost_pct": None,
                       "account_currency": "KRW"},
        "sweep": {"axis": "none", "target": "return", "target_node": None,
                  "relation_kind": "ic", "label": None, "param_grid": [], "assets": [],
                  "event": None, "windows": [5, 10, 20], "event_basis": "close"},
    }


def test_ledger_frozen_form_parses():
    """동결 원장 definition(레거시 전체 dump)이 파싱돼야 보유 포지션이 관리 가능하다."""
    s = StrategyIR.model_validate(_ledger_frozen_definition())
    assert s.query == "simulate" and s.study.axis == "none"


def test_ledger_frozen_form_preserves_trading_semantics():
    """거래 의미 보존 — 진입/청산/유니버스/사이징/시뮬 필드가 그대로 살아야 한다."""
    raw = _ledger_frozen_definition()
    s = StrategyIR.model_validate(raw)
    # universe·signal
    assert s.universe.kind == "list" and s.universe.symbols == ["000020", "005930", "000660"]
    assert s.signal.op == "compare" and s.signal.params["op"] == "<="
    assert s.signal.inputs["right"].params["window"] == 60
    # position — 청산 규칙(손절)·진입(월간 리밸런스 top2)·사이징
    assert s.position.direction == "long"
    assert s.position.entry.mode == "scheduled" and s.position.entry.rebalance == "monthly"
    assert s.position.entry.top_n == 2
    assert s.position.exit.stop_loss == -7.0 and s.position.exit.hold_days is None
    assert s.position.sizing.mode == "equal_weight" and s.position.sizing.amount_pct == 10.0
    # simulation — 레거시 키만 제거되고 나머지 보존
    assert s.simulation.initial_capital == 10_000_000.0
    assert s.simulation.delay == 1 and s.simulation.fill == "next_open"
    assert s.simulation.account_currency == "KRW"
    assert not hasattr(s.simulation, "period_split")


def test_migration_does_not_mutate_input():
    """파싱이 원장 dict를 고쳐쓰면 안 된다 — ledger 영속 상태에 대한 부수효과 금지."""
    raw = _ledger_frozen_definition()
    snapshot = copy.deepcopy(raw)
    StrategyIR.model_validate(raw)
    assert raw == snapshot


# ── 레거시 → 신 결정적 매핑 (edc051d 셰임과 동일 규칙) ─────────────────────────

def test_legacy_return_none_to_simulate():
    s = _m(sweep={"axis": "none", "target": "return"})
    assert s.query == "simulate" and s.study.axis == "none"


def test_legacy_parameter_axis():
    s = _m(sweep={"axis": "parameter",
                  "param_grid": [{"path": "simulation.commission", "values": [0, 0.1]}]})
    assert s.query == "simulate" and s.study.axis == "parameter"
    assert s.study.reduction == "enumerate" and len(s.study.param_grid) == 1
    assert s.study.param_grid[0].path == "simulation.commission"


def test_legacy_asset_to_entity():
    s = _m(sweep={"axis": "asset", "assets": ["AAA", "BBB"]})
    assert s.query == "simulate"
    assert s.study.axis == "entity" and s.study.reduction == "enumerate"
    assert s.study.assets == ["AAA", "BBB"]


def test_legacy_condition_to_label_contrast():
    s = _m(sweep={"axis": "condition", "label": {"op": "calendar", "params": {"unit": "weekday"}}})
    assert s.query == "simulate"
    assert s.study.axis == "label" and s.study.reduction == "contrast"
    assert s.study.label is not None


def test_legacy_target_signal_to_describe():
    s = _m(sweep={"target": "signal",
                  "target_node": {"op": "data", "params": {"ref": "__SELF__.rsi_14"}}})
    assert s.query == "describe" and s.study.target_node is not None


def test_legacy_target_relation_to_relate_ic():
    s = _m(sweep={"target": "relation", "target_node": {
        "op": "rank", "inputs": {"signal": {"op": "data", "params": {"ref": "momentum_12_1m"}}}}})
    assert s.query == "relate" and s.study.relation_kind == "ic"
    assert s.study.event is None and s.study.target_node is not None


def test_legacy_axis_time_to_relate_event():
    s = _m(sweep={"axis": "time", "event": {
        "op": "compare", "params": {"op": "<"},
        "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.rsi_14"}},
                   "right": {"op": "const", "params": {"value": 30}}}}})
    assert s.query == "relate" and s.study.event is not None


def test_legacy_period_split_oos_to_time_fold():
    s = StrategyIR.model_validate({**_BASE, "simulation": {"period_split": "oos"}})
    assert s.query == "simulate" and s.study.axis == "time_fold"
    assert s.study.reduction == "consistency" and s.study.folds == 2


def test_legacy_period_split_walk_forward_to_time_fold():
    s = StrategyIR.model_validate({**_BASE, "simulation": {"period_split": "walk_forward"}})
    assert s.study.axis == "time_fold" and s.study.folds == 4


def test_legacy_simulation_split_dates_to_time_fold():
    """simulation.split_dates만 있어도 기간분할 발동(레거시 의미) → study로 흡수."""
    s = StrategyIR.model_validate({**_BASE, "simulation": {"split_dates": ["2022-01-01"]}})
    assert s.study.axis == "time_fold" and s.study.split_dates == ["2022-01-01"]
    assert not hasattr(s.simulation, "split_dates")


# ── 멱등성 — 신형태 입력은 변환 no-op ─────────────────────────────────────────

def test_idempotent_modern_dump_revalidates_identically():
    """레거시→신 변환 결과를 다시 파싱해도 동일(마이그레이션 멱등)."""
    once = StrategyIR.model_validate(_ledger_frozen_definition())
    twice = StrategyIR.model_validate(once.model_dump())
    assert once.model_dump() == twice.model_dump()


def test_idempotent_modern_input_not_touched():
    """신형태 dict는 변환 단계가 손대지 않는다(byte-identical 통과)."""
    modern = {**_BASE, "query": "simulate",
              "study": {"axis": "parameter", "reduction": "enumerate",
                        "param_grid": [{"path": "simulation.leverage", "values": [1, 2]}]}}
    snapshot = copy.deepcopy(modern)
    s = StrategyIR.model_validate(modern)
    assert modern == snapshot
    assert s.query == "simulate" and s.study.axis == "parameter"


# ── 모순 입력은 종전대로 loud 거부 (silent-drop 방지) ──────────────────────────
# 동결 원장은 컷오버 전 스키마라 query/study가 있을 수 없다. 레거시+신 키 동시
# 존재는 손제작/LLM 오산출 — 어느 쪽 의도인지 결정 불가하므로 거부 유지
# (extra="ignore"가 sweep을 조용히 버리는 silent 오작동 차단, NL repair loop 교정).

@pytest.mark.parametrize("mixed", [
    {"sweep": {"axis": "parameter"}, "study": {"axis": "entity", "assets": ["AAA"]}},
    {"sweep": {"axis": "none"}, "query": "describe"},
    {"query": "simulate", "study": {"axis": "none"},
     "simulation": {"period_split": "oos"}},
])
def test_legacy_mixed_with_modern_rejected(mixed):
    with pytest.raises(ValidationError, match="레거시"):
        StrategyIR.model_validate({**_BASE, **mixed})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

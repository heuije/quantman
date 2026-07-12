"""WS3 — 전략 합성(components·수익률 수준) 1급 지원 (IR 대수 캠페인).

prod 실수요(conv#25·#28 "이와 별개로 병행합니다"): 전략 A+B 병행 포트폴리오가 IR로 표현되지
않아 봇이 분리 실행·수동 합산하던 부류. 수익률 수준 합성(구성별 독립 실행→일일수익 가중합
=고정비중 매일 리밸런스 혼합)으로 1콜 지원. 조인트 멀티레그(공동 증거금)는 명시 비목표.
"""
import numpy as np
import pandas as pd
import pytest

from quant_core.ir_engine.contracts import REGISTRY, resolve_runner
from quant_core.ir_engine.service import strategy_from_spec
from quant_core.ir_engine.spec import StrategyIR, needed_symbols, validate_strategy

_C = {"op": "data", "params": {"ref": "__SELF__.Close"}}
_ALWAYS = {"op": "compare", "params": {"op": ">"},
           "inputs": {"left": _C, "right": {"op": "const", "params": {"value": 0}}}}


def _hold_ir(sym, name):
    return {"name": name, "universe": {"kind": "single", "symbols": [sym]},
            "signal": _ALWAYS, "query": "simulate",
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "always"}, "exit": {}},
            "simulation": {"initial_capital": 1e8, "fill": "close"}}


def _composite(components, **kw):
    return {"name": "합성", "universe": {"kind": "single", "symbols": ["A"]},
            "signal": _C, "query": "simulate", "components": components, **kw}


def _df(mult):
    idx = pd.bdate_range("2024-01-01", periods=30)
    close = 100.0 * np.cumprod(np.full(30, mult))
    return pd.DataFrame({"Open": close, "High": close, "Low": close,
                         "Close": close, "Volume": np.full(30, 1e6)}, index=idx)


def _errors(spec):
    return [i for i in validate_strategy(StrategyIR.model_validate(spec))
            if getattr(i, "is_error", False)]


# ── 디스패치·계약 ─────────────────────────────────────────────────────────────

def test_resolver_routes_composite_before_verbs():
    ir = StrategyIR.model_validate(_composite(
        [{"weight": 1, "ir": _hold_ir("A", "가")}, {"weight": 1, "ir": _hold_ir("B", "나")}]))
    assert resolve_runner(ir) == "simulate.composite"


def test_contract_registered_with_honest_limits():
    c = REGISTRY["simulate.composite"]
    assert c.shape == "simulate" and c.not_supported
    assert any("증거금" in ns or "멀티레그" in ns for ns in c.not_supported)


# ── 검증기 ────────────────────────────────────────────────────────────────────

def test_validator_accepts_two_components():
    assert not _errors(_composite(
        [{"weight": 1, "ir": _hold_ir("A", "가")}, {"weight": 2, "ir": _hold_ir("B", "나")}]))


def test_validator_rejects_single_component():
    assert _errors(_composite([{"weight": 1, "ir": _hold_ir("A", "가")}]))


def test_validator_rejects_nested_composite():
    inner = _composite([{"weight": 1, "ir": _hold_ir("A", "가")},
                        {"weight": 1, "ir": _hold_ir("B", "나")}])
    errs = _errors(_composite([{"weight": 1, "ir": inner},
                               {"weight": 1, "ir": _hold_ir("B", "나")}]))
    assert any("중첩" in e.message for e in errs)


def test_validator_rejects_nonpositive_weight():
    assert _errors(_composite(
        [{"weight": 0, "ir": _hold_ir("A", "가")}, {"weight": 1, "ir": _hold_ir("B", "나")}]))


def test_validator_recurses_into_component_issues():
    bad = _hold_ir("A", "가")
    bad["position"]["exit"] = {"stop_loss": 7}          # 손절은 음수여야(M-exit) — 구성 내부 오류
    errs = _errors(_composite([{"weight": 1, "ir": bad},
                               {"weight": 1, "ir": _hold_ir("B", "나")}]))
    assert any(e.path.startswith("components.0.") for e in errs)


def test_validator_rejects_components_with_study():
    assert _errors(_composite(
        [{"weight": 1, "ir": _hold_ir("A", "가")}, {"weight": 1, "ir": _hold_ir("B", "나")}],
        study={"axis": "parameter",
               "param_grid": [{"path": "simulation.initial_capital", "values": [1, 2]}]}))


# ── 자원 순회 ─────────────────────────────────────────────────────────────────

def test_needed_symbols_unions_components():
    ir = StrategyIR.model_validate(_composite(
        [{"weight": 1, "ir": _hold_ir("AAA", "가")}, {"weight": 1, "ir": _hold_ir("BBB", "나")}]))
    assert needed_symbols(ir) == {"AAA", "BBB"}


# ── e2e — 합성 산술(가중 합산 손계산 대조) ────────────────────────────────────

def test_composite_e2e_weighted_mix():
    """A(+1%/일)·B(-0.5%/일)를 2:1 가중 — 합성 일일수익 = (2/3)(0.01)+(1/3)(-0.005)."""
    spec = _composite([{"weight": 2, "ir": _hold_ir("AAA", "상승")},
                       {"weight": 1, "ir": _hold_ir("BBB", "하락")}])
    res = strategy_from_spec(spec, {"AAA": _df(1.01), "BBB": _df(0.995)})
    assert res.get("success"), res.get("error")
    assert res["shape"] == "simulate"
    comps = res["components"]
    assert set(comps) == {"상승", "하락"}
    assert comps["상승"]["weight"] == pytest.approx(2 / 3)
    # 합성 CAGR 부호·크기 — 혼합 일일수익 ≈ +0.5%/일(양수)
    assert res["metrics"]["cum_return"] > 0
    assert comps["상승"]["cum_return"] > 0 > comps["하락"]["cum_return"]
    # equity 직렬화 계약(1회 백테스트 분기) — trades 빈 DataFrame 동봉
    assert isinstance(res["equity"], pd.Series) and isinstance(res["trades"], pd.DataFrame)


def test_composite_component_failure_is_loud():
    spec = _composite([{"weight": 1, "ir": _hold_ir("AAA", "가")},
                       {"weight": 1, "ir": _hold_ir("MISSING", "나")}])
    res = strategy_from_spec(spec, {"AAA": _df(1.01)})
    assert res.get("success") is False and "나" in (res.get("error") or "")

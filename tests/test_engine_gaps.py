"""A3·A4·A7·A8·A9 — 구조 갭 보완 회귀.

명세 §3.3·§3.4·§3.2·§4. 짝제약 검증·screener 거부·성과 지표·winsorize·달력.

    cd platform && pytest tests/test_engine_gaps.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

from quant_core.blocks import EvalContext, Node, catalog_spec, const, data, evaluate  # noqa: E402
from quant_core.ir_engine import (  # noqa: E402
    Entry, Exit, PositionSpec, Sizing, SimSpec, StrategyIR, Universe,
    run_strategy_ir, strategy_from_spec, validate_strategy,
)


def _multi():
    idx = pd.date_range("2020-01-01", periods=200, freq="B")

    def mk(seed):
        r = np.random.default_rng(seed)
        close = np.maximum(100 + np.cumsum(r.normal(0.05, 1.2, 200)), 5.0)
        return pd.DataFrame({
            "Open": close, "High": close * 1.01, "Low": close * 0.99,
            "Close": close, "Volume": 1e6, "momentum_12_1m": r.uniform(-10, 10, 200),
        }, index=idx)
    return {"AAA": mk(1), "BBB": mk(2), "CCC": mk(3)}


# ── A7: 성과 지표 보강 ────────────────────────────────────────────────────────

def test_extra_metrics_present():
    s = StrategyIR(
        signal=data("momentum_12_1m"), universe=Universe(kind="all"),
        position=PositionSpec(entry=Entry(mode="scheduled", rebalance="monthly", top_n=2)),
        simulation=SimSpec(initial_capital=1e7))
    m = run_strategy_ir(s, _multi())["metrics"]
    for k in ["sortino", "calmar", "var_95", "cvar_95", "beta"]:
        assert k in m, f"{k} 누락"


# ── A8: winsorize ─────────────────────────────────────────────────────────────

def test_winsorize_clips_outlier():
    idx = pd.date_range("2021-01-01", periods=3, freq="B")
    d = {s: pd.DataFrame({"x": [v] * 3}, index=idx)
         for s, v in [("A", 1.0), ("B", 2.0), ("C", 3.0), ("D", 1000.0)]}
    out = evaluate(Node(op="winsorize", params={"lower": 10, "upper": 90},
                        inputs={"signal": data("x")}), EvalContext.from_dataset(d))
    assert out.to_numpy().max() < 1000.0     # 이상치 절단
    assert "winsorize" in {b["op"] for b in catalog_spec()}


# ── A9: 달력 라벨 ─────────────────────────────────────────────────────────────

def test_calendar_weekday():
    idx = pd.date_range("2021-01-04", periods=10, freq="B")  # 월요일 시작
    d = {"A": pd.DataFrame({"Close": range(10)}, index=idx)}
    out = evaluate(Node(op="calendar", params={"unit": "weekday"}), EvalContext.from_dataset(d))
    assert set(np.unique(out["A"].dropna())) <= {0, 1, 2, 3, 4}   # 영업일 월~금
    assert "calendar" in {b["op"] for b in catalog_spec()}


# ── A3: 포지션 짝 제약 ────────────────────────────────────────────────────────

def test_budget_sizing_warns_in_scheduled():
    """종목당 예산 사이징(pct_cash)은 이벤트용 — 스케줄에선 S-pair 경고(에러 아님)."""
    s = StrategyIR(
        signal=data("momentum_12_1m"), universe=Universe(kind="all"),
        position=PositionSpec(sizing=Sizing(mode="pct_cash"), entry=Entry(mode="scheduled")))
    iss = validate_strategy(s)
    assert any(i.rule == "S-pair" and not i.is_error for i in iss)   # 경고지 에러 아님
    assert not any(i.rule == "S-pair" and i.is_error for i in iss)


# ── A5: 기간분할 (study.axis=time_fold) ───────────────────────────────────────

def _factor_spec(study=None):
    spec = {"signal": {"op": "data", "params": {"ref": "momentum_12_1m"}},
            "universe": {"kind": "all"},
            "position": {"entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 2}},
            "simulation": {"initial_capital": 1e7}}
    if study is not None:
        spec["study"] = study
    return spec


def test_period_split_walk_forward():
    # walk_forward = time_fold 4분할 일관성(folds=4).
    res = strategy_from_spec(
        _factor_spec({"axis": "time_fold", "reduction": "consistency", "folds": 4}), _multi())
    assert res["success"] and res["axis"] == "period_split"
    assert len(res["buckets"]) >= 2
    assert "consistency" in res


def test_period_split_oos_two_folds():
    # oos = time_fold 2분할(인샘플/아웃샘플, folds=2).
    res = strategy_from_spec(
        _factor_spec({"axis": "time_fold", "reduction": "consistency", "folds": 2}), _multi())
    assert res["success"]
    assert set(res["buckets"].keys()) == {"인샘플", "아웃샘플"}


def test_period_split_vs_sweep_conflict():
    # 기간분할(time_fold) × 펼침(param_grid) 동시 사용 금지 — study 평면에 둘 다 채우면 S-split 거부.
    spec = _factor_spec({"axis": "time_fold", "reduction": "consistency", "folds": 2,
                         "param_grid": [{"path": "position.entry.top_n", "values": [1, 2]}]})
    res = strategy_from_spec(spec, _multi())
    assert not res["success"]
    assert any(i["rule"] == "S-split" for i in res["issues"])


# ── D: 달력 라벨 단위 확장 (bday·turn_of_month) ───────────────────────────────

def test_calendar_turn_of_month():
    idx = pd.date_range("2022-01-03", periods=60, freq="B")
    d = {"A": pd.DataFrame({"Close": range(60)}, index=idx)}
    out = evaluate(Node(op="calendar",
                        params={"unit": "turn_of_month", "tom_start": 3, "tom_end": 1}),
                   EvalContext.from_dataset(d))
    lab = out["A"].dropna()
    assert set(np.unique(lab)) <= {0.0, 1.0}        # 라벨은 0/1
    assert 0.0 < lab.mean() < 1.0                    # 일부만 turn-of-month


def test_calendar_bday_ordinal():
    idx = pd.date_range("2022-01-03", periods=60, freq="B")
    d = {"A": pd.DataFrame({"Close": range(60)}, index=idx)}
    out = evaluate(Node(op="calendar", params={"unit": "bday"}), EvalContext.from_dataset(d))
    assert out["A"].min() == 1.0                      # 각 월 첫 영업일 서수=1
    assert "calendar" in {b["op"] for b in catalog_spec()}


# ── F: 체결가 typical = (고+저+종)/3 ──────────────────────────────────────────

def _always_buy():
    return Node(op="compare", params={"op": ">"},
                inputs={"left": data("__SELF__.Close"), "right": const(0.0)})


def _single_bt(df, fill):
    return run_strategy_ir(
        StrategyIR(signal=_always_buy(), universe=Universe(kind="single", symbols=["AAA"]),
                   position=PositionSpec(entry=Entry(mode="on_signal")),
                   simulation=SimSpec(initial_capital=1e7, fill=fill)),
        {"AAA": df})


def test_typical_fill_differs_from_close():
    idx = pd.date_range("2022-01-03", periods=12, freq="B")
    close = np.linspace(100.0, 110.0, 12)
    # 비대칭 바: typical=(H+L+C)/3 ≠ close
    df = pd.DataFrame({"Open": close, "High": close * 1.05, "Low": close * 0.99,
                       "Close": close}, index=idx)
    rc, rt = _single_bt(df, "close"), _single_bt(df, "typical")
    assert rc["success"] and rt["success"]
    assert rc["equity"].iloc[-1] != rt["equity"].iloc[-1]   # 체결가 달라 결과 분기


def test_typical_fill_requires_high_low():
    idx = pd.date_range("2022-01-03", periods=12, freq="B")
    close = np.linspace(100.0, 110.0, 12)
    df = pd.DataFrame({"Open": close, "Close": close}, index=idx)   # 고·저 없음
    res = _single_bt(df, "typical")
    assert not res["success"] and "typical" in res["error"]


# ── #3+#4: 섹터/정적속성 라벨(attribute) + 멤버십 조건(is_in) — A-1 섹터 제외 ────

def test_attribute_and_is_in_catalog():
    ops = {b["op"] for b in catalog_spec()}
    assert "attribute" in ops and "is_in" in ops


def test_attribute_emits_sector_label(monkeypatch):
    import quant_core.blocks.ops_advanced as oa
    sectors = {"A": "금융", "B": "지주", "C": "전기전자", "D": "화학"}
    monkeypatch.setattr(oa, "get_symbol_group",
                        lambda sym, attr="Industry": sectors.get(sym, "기타"))
    idx = pd.date_range("2021-01-01", periods=10, freq="B")
    ds = {s: pd.DataFrame({"Close": range(10)}, index=idx) for s in sectors}
    lab = evaluate(Node(op="attribute", params={"attr": "Industry"}), EvalContext.from_dataset(ds))
    assert lab["A"].iloc[0] == "금융" and lab["C"].iloc[-1] == "전기전자"


def test_is_in_membership_and_negate(monkeypatch):
    import quant_core.blocks.ops_advanced as oa
    sectors = {"A": "금융", "B": "지주", "C": "전기전자", "D": "화학"}
    monkeypatch.setattr(oa, "get_symbol_group",
                        lambda sym, attr="Industry": sectors.get(sym, "기타"))
    idx = pd.date_range("2021-01-01", periods=6, freq="B")
    ds = {s: pd.DataFrame({"Close": range(6)}, index=idx) for s in sectors}
    ctx = EvalContext.from_dataset(ds)
    attr = Node(op="attribute", params={"attr": "Industry"})
    keep = evaluate(Node(op="is_in", params={"values": ["금융", "지주"]}, inputs={"signal": attr}), ctx)
    assert keep["A"].all() and keep["B"].all() and not keep["C"].any()       # 포함
    drop = evaluate(Node(op="is_in", params={"values": ["금융", "지주"], "negate": True},
                         inputs={"signal": attr}), ctx)
    assert (~drop["A"]).all() and drop["C"].all() and drop["D"].all()        # 제외


def test_sector_exclusion_screener(monkeypatch):
    """A-1 핵심 — 금융·지주 섹터를 screener filter로 제외하고 모멘텀 top_n 선택."""
    import quant_core.blocks.ops_advanced as oa
    sectors = {"A": "금융", "B": "지주", "C": "전기전자", "D": "화학"}
    monkeypatch.setattr(oa, "get_symbol_group",
                        lambda sym, attr="Industry": sectors.get(sym, "기타"))
    idx = pd.date_range("2021-01-01", periods=80, freq="B")

    def mk(seed):
        r = np.random.default_rng(seed)
        close = np.maximum(100 + np.cumsum(r.normal(0.05, 1.0, 80)), 5.0)
        return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                             "Close": close, "Volume": 1e6,
                             "momentum_12_1m": r.uniform(-5, 5, 80)}, index=idx)
    ds = {s: mk(i) for i, s in enumerate(sectors)}
    excl = Node(op="is_in", params={"values": ["금융", "지주"], "negate": True},
                inputs={"signal": Node(op="attribute", params={"attr": "Industry"})})
    s = StrategyIR(
        signal=data("momentum_12_1m"),
        universe=Universe(kind="list", symbols=["A", "B", "C", "D"],
                          screener={"condition": excl.model_dump()}),
        position=PositionSpec(entry=Entry(mode="scheduled", rebalance="monthly", top_n=2)),
        simulation=SimSpec(initial_capital=1e7))
    assert not [i for i in validate_strategy(s) if i.is_error]
    res = run_strategy_ir(s, ds)
    assert res["success"], res.get("error")
    if not res["trades"].empty:                       # 금융(A)·지주(B)는 절대 매매 안 됨
        assert set(res["trades"]["종목"]) <= {"C", "D"}


# ── #2: calendar(label) → is_in → condition — turn-of-month 타이밍 매매(조합 표현) ──

def test_calendar_turn_of_month_tradable_via_is_in():
    """B-2 — calendar(LABEL)을 is_in으로 condition화 → turn-of-month 창에서만 시장 노출.

    calendar 타입을 바꾸지 않고 is_in(#4)이 label→condition 다리 역할. 비-ToM일엔 현금(rfr 적립).
    """
    idx = pd.date_range("2022-01-03", periods=140, freq="B")
    close = 100 + np.arange(140) * 0.1
    ds = {"SPY": pd.DataFrame({"Open": close, "High": close * 1.001, "Low": close * 0.999,
                               "Close": close, "Volume": 1e6}, index=idx)}
    sig = Node(op="is_in", params={"values": [1.0]},
               inputs={"signal": Node(op="calendar",
                                      params={"unit": "turn_of_month", "tom_start": 3, "tom_end": 1})})
    s = StrategyIR(signal=sig, universe=Universe(kind="list", symbols=["SPY"]),
                   position=PositionSpec(entry=Entry(mode="always")),   # 매일 점검: ToM면 보유, 아니면 현금
                   simulation=SimSpec(initial_capital=1e7, rfr_pct=2.0))
    assert not [i for i in validate_strategy(s) if i.is_error]
    res = run_strategy_ir(s, ds)
    assert res["success"], res.get("error")
    assert not res["trades"].empty                  # ToM 창 진입·이탈 라운드트립 발생


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))

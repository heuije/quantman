"""커버리지 게이트 시나리오 — 미커버 자산군 전략 skip(진입)·포지션 orphan(청산) (P1).

    cd platform/local && python -m pytest tests/scenarios/test_coverage_gate.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from sim import invariants  # noqa: E402

_DUMMY_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "right": {"op": "const", "params": {"value": 0}}},
}


def _ir_def(universe):
    return {
        "name": "전략", "engine": "ir", "universe": universe, "signal": _DUMMY_SIGNAL,
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 10},
                     "entry": {"mode": "on_signal"}, "exit": {}, "overlays": {}},
        "simulation": {},
    }


def _ds(closes):
    idx = pd.date_range("2026-05-01", periods=len(closes), freq="B")
    return {"005930": pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)}


_DS = _ds([70000, 70000, 70000, 70000, 70000])


def test_entry_gate_skips_uncovered_strategy(isolated_trader, monkeypatch):
    """선물 전략인데 선물 자격증명 미커버 → 전략 통째 skip, 발주 0 (C1·C3)."""
    from localapp import coverage
    t, broker = isolated_trader
    monkeypatch.setattr(coverage, "covered_categories", lambda: {"kr_equity", "us_equity"})
    sid = "futstrat"
    strategies = [{"id": sid, "name": "선물전략",
                   "definition": _ir_def({"kind": "single", "symbols": ["코스피200선물"]})}]
    by_strategy = [{"strategy_id": sid, "candidates": [{"symbol": "코스피200선물"}]}]
    decisions: list[dict] = []
    t._enter_from_preview(by_strategy, strategies, _DS, 10_000_000.0,
                          decisions, set(), market="KRX", catchup=False)
    assert broker.submitted == [], broker.submitted
    uncov = [d for d in decisions if d["action"] == "skip_uncovered"]
    assert len(uncov) == 1, decisions
    assert uncov[0]["strategy_id"] == sid


def test_entry_gate_allows_covered_strategy(isolated_trader, monkeypatch):
    """커버된 주식 전략은 게이트 통과 → 정상 발주 (회귀 가드)."""
    from localapp import coverage
    t, broker = isolated_trader
    monkeypatch.setattr(coverage, "covered_categories", lambda: {"kr_equity", "us_equity"})
    broker._prices["005930"] = 70000
    sid = "eq"
    strategies = [{"id": sid, "name": "주식전략",
                   "definition": _ir_def({"kind": "single", "symbols": ["005930"]})}]
    by_strategy = [{"strategy_id": sid, "candidates": [{"symbol": "005930"}]}]
    decisions: list[dict] = []
    t._enter_from_preview(by_strategy, strategies, _DS, 10_000_000.0,
                          decisions, set(), market="KRX", catchup=False)
    assert len(broker.submitted) == 1, decisions
    assert [d for d in decisions if d["action"] == "skip_uncovered"] == []
    invariants.check_all(t)


def test_cleanup_orphan_uncovered_position(isolated_trader, monkeypatch):
    """미커버 자산군 보유 포지션은 청산 skip(외부매도 오진) 대신 orphan_uncovered 표면화 (C5)."""
    from localapp import coverage
    t, broker = isolated_trader
    monkeypatch.setattr(coverage, "covered_categories", lambda: {"kr_equity", "us_equity"})
    # 선물 포지션(kr_futures, 미커버) 원장 주입 — 진입 경로 우회.
    t.ledger["heldfut"] = {
        "symbol": "코스피200선물", "qty": 1, "entry_date": "2026-05-20",
        "entry_price": 350.0, "peak_price": 350.0, "side": "long",
        "strategy_name": "선물전략", "definition": _ir_def(
            {"kind": "single", "symbols": ["코스피200선물"]})}
    payload = t.cycle(strategies=[], dataset=_DS, buy_candidates=[],
                      risk_limits={"kill_switch_daily_loss_pct": 3.0}, market="KRX")
    orphans = [d for d in payload["decisions"] if d["action"] == "orphan_uncovered"]
    assert len(orphans) == 1, payload["decisions"]
    assert orphans[0]["symbol"] == "코스피200선물"
    # 청산 발주 안 함 — 포지션 유지
    assert "heldfut" in t.ledger
    assert [s for s in broker.submitted if s.get("side") == "sell"] == []

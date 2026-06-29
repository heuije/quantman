"""P5-3 — 실행 계좌 가드(account_ref↔활성 핸들). C7 차단.

진입 루프에서 전략의 account_ref가 현재 활성 계좌 핸들이 아니면 그 전략을
통째 skip(skip_wrong_account) — 모의 검증 전략이 실전 계좌로 무경고 실거래되는
것(C7)을 식별자 차원에서 차단. account_ref=None(레거시·미바인딩)은 통과.

fixture/SimBroker/_enter_from_preview·cycle 호출 패턴은 P1 커버리지 게이트와
같은 결정 지점을 테스트하는 tests/scenarios/test_coverage_gate.py(+ scenarios/
conftest.py의 isolated_trader)에서 복사. SimBroker는 전 자산군 커버라 coverage
게이트는 통과 → account 가드만 분리 검증.

    cd local && python -m pytest tests/test_account_guard.py -v
"""
from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pandas as pd
import pytest

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from sim.broker import SimBroker  # noqa: E402

# ── test_coverage_gate.py에서 복사한 최소 유효 IR ─────────────────────────────
_DUMMY_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "right": {"op": "const", "params": {"value": 0}}},
}
_DEF = {
    "name": "전략", "engine": "ir",
    "universe": {"kind": "single", "symbols": ["005930"]}, "signal": _DUMMY_SIGNAL,
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


@pytest.fixture
def isolated_trader(tmp_path, monkeypatch):
    """scenarios/conftest.py의 isolated_trader 복사 — trader 영속 경로 tmp 격리 +
    KST 날짜 고정 + 서버 push 차단 + 전 자산군 coverage 스텁 → SimBroker 위 Trader."""
    from localapp import trader as tr
    from localapp import intraday_loop, killswitch, intents, order_log
    for name in ("LEDGER_PATH", "EQUITY_PATH", "PENDING_ORDERS_PATH", "TRADES_PATH"):
        monkeypatch.setattr(tr, name, tmp_path / f"{name}.json")
    monkeypatch.setattr(tr, "CLAIMED_FILLS_PATH",
                        tmp_path / "claimed_fills.json", raising=False)
    monkeypatch.setattr(killswitch, "KILLSWITCH_PATH", tmp_path / "ks.json")
    monkeypatch.setattr(intents, "INTENTS_PATH", tmp_path / "intents.jsonl")
    for name in ("ORDERS_PATH", "CYCLES_PATH", "SLIPPAGE_PATH"):
        monkeypatch.setattr(order_log, name, tmp_path / f"{name}.jsonl")
    monkeypatch.setattr(intraday_loop, "push_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(tr, "kst_today", lambda: datetime.date(2026, 6, 1))
    real_merged = tr.merged_execution
    monkeypatch.setattr(tr, "merged_execution", lambda o=None: {
        **real_merged(o), "post_submit_wait_sec": 0, "poll_interval_sec": 0.01})
    broker = SimBroker(prices={"005930": 70000})
    from localapp import coverage
    monkeypatch.setattr(coverage, "covered_categories",
                        lambda: {"kr_equity", "us_equity", "kr_futures", "us_futures"})
    return tr.Trader(broker), broker


def _run_entry(isolated_trader, strategies, by_strategy):
    """test_coverage_gate.py의 _enter_from_preview 호출 패턴 — decisions를 반환."""
    t, _broker = isolated_trader
    decisions: list[dict] = []
    t._enter_from_preview(by_strategy, strategies, _DS, 10_000_000.0,
                          decisions, set(), market="KRX", catchup=False)
    return decisions


def test_strategy_bound_to_other_account_is_skipped(isolated_trader, monkeypatch):
    """account_ref=다른 계좌 → skip_wrong_account, 발주 0 (C7 차단)."""
    monkeypatch.setattr("localapp.account_handle.active_account_ids",
                        lambda: ["acc_ACTIVE"])
    _t, broker = isolated_trader
    decisions = _run_entry(
        isolated_trader,
        strategies=[{"id": "1", "name": "s", "definition": _DEF,
                     "account_ref": "acc_OTHER"}],
        by_strategy=[{"strategy_id": "1", "candidates": [{"symbol": "005930"}]}])
    actions = [d["action"] for d in decisions]
    assert "skip_wrong_account" in actions, decisions
    assert "bought" not in actions, decisions
    assert broker.submitted == [], broker.submitted


def test_strategy_bound_to_active_account_runs(isolated_trader, monkeypatch):
    """account_ref=활성 계좌 → 가드 통과(skip_wrong_account 없음)."""
    monkeypatch.setattr("localapp.account_handle.active_account_ids",
                        lambda: ["acc_ACTIVE"])
    decisions = _run_entry(
        isolated_trader,
        strategies=[{"id": "1", "name": "s", "definition": _DEF,
                     "account_ref": "acc_ACTIVE"}],
        by_strategy=[{"strategy_id": "1", "candidates": [{"symbol": "005930"}]}])
    assert "skip_wrong_account" not in [d["action"] for d in decisions], decisions


def test_legacy_null_account_ref_runs(isolated_trader, monkeypatch):
    """레거시 — account_ref 키 자체가 없으면 가드 통과(기존 거동 무변경)."""
    monkeypatch.setattr("localapp.account_handle.active_account_ids",
                        lambda: ["acc_ACTIVE"])
    decisions = _run_entry(
        isolated_trader,
        strategies=[{"id": "1", "name": "s", "definition": _DEF}],  # account_ref 없음
        by_strategy=[{"strategy_id": "1", "candidates": [{"symbol": "005930"}]}])
    assert "skip_wrong_account" not in [d["action"] for d in decisions], decisions


def test_cycle_summary_counts_skip_wrong_account(isolated_trader, monkeypatch):
    """cycle()이 cycle_summary["n_skip_wrong_account"]로 카운트 표면화."""
    monkeypatch.setattr("localapp.account_handle.active_account_ids",
                        lambda: ["acc_ACTIVE"])
    t, _broker = isolated_trader
    strategies = [{"id": "1", "name": "s", "definition": _DEF,
                   "account_ref": "acc_OTHER"}]
    by_strategy = [{"strategy_id": "1", "candidates": [{"symbol": "005930"}]}]
    payload = t.cycle(strategies=strategies, dataset=_DS, buy_candidates=by_strategy,
                      risk_limits={"kill_switch_daily_loss_pct": 3.0}, market="KRX")
    cs = payload["cycle_summary"]
    assert cs["n_skip_wrong_account"] >= 1, payload["decisions"]

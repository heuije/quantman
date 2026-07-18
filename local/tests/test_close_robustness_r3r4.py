"""R3·R4 — 종가·정산 견고성 + 진입 미달 관측.

R4: 종가 재시도 하네스(하드컷)·정산 당일 재시도(no-op 판정)·catchup 종가 항목.
R3: entry_shortfall 관측(슬롯 미충족 + 실패 시도 존재 시에만).
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from localapp import catchup, runner
from localapp.trader import Trader

_KST = ZoneInfo("Asia/Seoul")


# ── R4-① 하드컷 ─────────────────────────────────────────────────────────────


def test_close_hard_cutoff_krx():
    c_stock = runner._close_hard_cutoff_kst("KRX", "stock")
    c_fut = runner._close_hard_cutoff_kst("KRX", "futures")
    assert (c_stock.hour, c_stock.minute) == (15, 30)
    assert (c_fut.hour, c_fut.minute) == (15, 45)


def test_close_hard_cutoff_us_uses_session_close(monkeypatch):
    from quant_core import market_calendar as mc
    close = datetime(2026, 7, 18, 5, 0, tzinfo=_KST)
    monkeypatch.setattr(mc, "session_kst", lambda m, d: (None, close))
    assert runner._close_hard_cutoff_kst("US", "stock") == close


# ── R4-① 재시도 하네스 ──────────────────────────────────────────────────────


@pytest.fixture
def _close_env(monkeypatch):
    from quant_core import market_calendar as mc
    monkeypatch.setattr(runner, "setup_logging", lambda: None)
    monkeypatch.setattr(runner, "_flush_pending", lambda: None)
    monkeypatch.setattr(mc, "is_session_day", lambda m, d: True)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    pushed = []
    monkeypatch.setattr(runner, "push_snapshot", lambda p: pushed.append(p))
    return pushed


def test_close_cycle_retries_then_succeeds(_close_env, monkeypatch):
    calls = {"n": 0}

    def once(market, cls):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("blip")
        return {"ok": True}

    monkeypatch.setattr(runner, "_close_cycle_once", once)
    monkeypatch.setattr(runner, "_close_hard_cutoff_kst",
                        lambda m, c: datetime.now(_KST) + timedelta(minutes=10))
    out = runner.run_close_cycle("KRX", "stock")
    assert out == {"ok": True} and calls["n"] == 3


def test_close_cycle_hard_cutoff_stops_and_surfaces(_close_env, monkeypatch):
    from localapp import order_log

    def once(market, cls):
        raise RuntimeError("down")

    monkeypatch.setattr(runner, "_close_cycle_once", once)
    # 컷이 이미 지남 — 1회 시도 후 즉시 종료(마감 뒤 발주 금지)
    monkeypatch.setattr(runner, "_close_hard_cutoff_kst",
                        lambda m, c: datetime.now(_KST) - timedelta(seconds=1))
    out = runner.run_close_cycle("KRX", "futures")
    assert out["status"] == "error"
    # 에러 스냅샷 push + cycles.jsonl 에러 기록(catchup·건강 C6 소비)
    pushed = _close_env
    assert pushed and "종가 사이클 전 시도 실패" in pushed[-1]["cycle_summary"]["error"]
    rows = [json.loads(x) for x in
            order_log.CYCLES_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    errs = [r for r in rows if (r.get("summary") or {}).get("kind") == "day_trade_close"
            and (r.get("summary") or {}).get("error")]
    assert errs


# ── R4-② 정산 재시도 ────────────────────────────────────────────────────────


def test_settlement_retry_noop_when_done(monkeypatch):
    from localapp import order_log
    today = datetime.now(_KST).date().isoformat()
    monkeypatch.setattr(order_log, "read_cycles", lambda n=20: [
        {"ts": f"{today}T15:50:01+09:00",
         "summary": {"kind": "post_close_settlement", "market": "KRX"}}])
    called = []
    monkeypatch.setattr(runner, "run_post_close_settlement",
                        lambda market="KRX": called.append(market))
    assert runner.run_settlement_retry("KRX") is None
    assert called == []


def test_settlement_retry_runs_when_missing_or_error(monkeypatch):
    from localapp import order_log
    today = datetime.now(_KST).date().isoformat()
    monkeypatch.setattr(order_log, "read_cycles", lambda n=20: [
        {"ts": f"{today}T15:50:01+09:00",
         "summary": {"kind": "post_close_settlement", "market": "KRX",
                      "error": "잔고 조회 실패"}}])
    called = []
    monkeypatch.setattr(runner, "run_post_close_settlement",
                        lambda market="KRX": called.append(market) or {"ok": 1})
    assert runner.run_settlement_retry("KRX") == {"ok": 1}
    assert called == ["KRX"]


# ── R4-③ catchup 종가 항목 ──────────────────────────────────────────────────


def _plan_at(monkeypatch, now, entries):
    from quant_core import market_calendar as mc
    monkeypatch.setattr(catchup, "_read_recent_cycles", lambda: entries)
    monkeypatch.setattr(mc, "is_session_day", lambda m, d: True)
    return catchup._decide_catchup_plan(now=now)


def test_catchup_close_window_stock_missing(monkeypatch):
    now = datetime(2026, 7, 17, 15, 26, tzinfo=_KST)   # 금요일 주식 종가창
    plan = _plan_at(monkeypatch, now, [])
    assert "stock" in plan.krx_close_classes
    assert "futures" not in plan.krx_close_classes      # 창 밖(15:35~)


def test_catchup_close_window_done_no_action(monkeypatch):
    now = datetime(2026, 7, 17, 15, 26, tzinfo=_KST)
    done_entry = {"ts": "2026-07-17T15:25:30+09:00",
                  "summary": {"kind": "day_trade_close", "market": "KRX",
                               "instrument_class": "stock"}}
    plan = _plan_at(monkeypatch, now, [done_entry])
    assert plan.krx_close_classes == []


def test_catchup_close_outside_window_no_action(monkeypatch):
    now = datetime(2026, 7, 17, 16, 10, tzinfo=_KST)    # 창 밖(마감 후)
    plan = _plan_at(monkeypatch, now, [])
    assert plan.krx_close_classes == []


# ── R3 — entry_shortfall 관측 ───────────────────────────────────────────────


def _mk_trader(monkeypatch, try_results):
    class _B:
        pass

    t = Trader(_B())
    seq = iter(try_results)
    monkeypatch.setattr(t, "_try_buy_one_symbol",
                        lambda *a, **k: next(seq))
    from localapp import account_handle, coverage
    monkeypatch.setattr(account_handle, "active_account_ids", lambda: set())
    monkeypatch.setattr(coverage, "missing_categories", lambda syms: set())
    return t


def _entry_args():
    strat = {"id": "7", "name": "티", "account_ref": None,
             "definition": {"universe": {"kind": "list"},
                             "simulation": {"fill": "next_open"}}}
    by_strategy = [{"strategy_id": "7",
                    "candidates": [{"symbol": "005930"}, {"symbol": "000660"}]}]
    return by_strategy, [strat]


def test_entry_shortfall_emitted_on_partial(monkeypatch):
    t = _mk_trader(monkeypatch, [True, False])          # 슬롯2 중 1 확정
    by, strats = _entry_args()
    decisions: list = []
    t._enter_from_preview(by, strats, {}, 1_000_000, decisions, set(),
                          market="KRX", entry_window="open")
    sf = [d for d in decisions if d["action"] == "entry_shortfall"]
    assert len(sf) == 1 and "슬롯 2 중 확정 1" in sf[0]["reason"]


def test_entry_shortfall_silent_when_slots_met(monkeypatch):
    t = _mk_trader(monkeypatch, [True, True])
    by, strats = _entry_args()
    decisions: list = []
    t._enter_from_preview(by, strats, {}, 1_000_000, decisions, set(),
                          market="KRX", entry_window="open")
    assert not [d for d in decisions if d["action"] == "entry_shortfall"]

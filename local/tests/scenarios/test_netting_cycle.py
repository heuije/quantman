"""Workbench 시나리오 — 넷팅 합성 체결 (Phase 2).

넷팅 핸드오프는 브로커 주문 없이 원장만 이관한다(합성 체결). 설계 §13.
  · exit leg: 닫는 전략 슬롯 축소 + realized 기록 (기존 _apply_fill 재사용).
  · entry leg: 여는 전략 슬롯 생성(entry_price=기준가).
  · 브로커 실주문 0 · order_no="NETTED-" · intent 저널 시드로 재발주 차단(N1).

    cd platform/local && python -m pytest tests/scenarios/test_netting_cycle.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

import pandas as pd

from localapp import intents
from localapp.netting import Intent

_DUMMY_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "right": {"op": "const", "params": {"value": 0}}},
}


def _daytrade_def(symbol="005930"):
    return {"name": "dt", "engine": "ir",
            "universe": {"kind": "single", "symbols": [symbol]},
            "signal": _DUMMY_SIGNAL,
            "position": {"direction": "long",
                         "sizing": {"mode": "pct_cash", "amount_pct": 10},
                         "entry": {"mode": "on_signal"}, "exit": {"hold_days": 0}},
            "simulation": {"fill": "next_open"}}


def _ds(symbol="005930", prev_close=100.0):
    idx = pd.date_range("2026-05-01", periods=8, freq="B")
    return {symbol: pd.DataFrame(
        {"Open": [prev_close] * 8, "High": [prev_close] * 8,
         "Low": [prev_close] * 8, "Close": [prev_close] * 8}, index=idx)}


def _seed_long(t, sid, symbol, qty, entry, name=None):
    t.ledger[sid] = {"symbol": symbol, "qty": qty, "entry_date": "2026-05-20",
                     "entry_price": float(entry), "peak_price": float(entry),
                     "side": "long", "strategy_name": name or sid, "definition": {}}


def _exit_leg(sid, symbol, qty, entry, ref, side="long"):
    return Intent(sid=sid, strategy_id=sid, strategy_name=sid, contract_key=symbol,
                  symbol=symbol, kind="exit", position_side=side,
                  order_side=("sell" if side == "long" else "buy"),
                  qty=qty, ref_price=float(ref), entry_price=float(entry),
                  mult=1.0, currency="KRW", definition={})


def _entry_leg(sid, symbol, qty, ref, side="long", name=None):
    return Intent(sid=sid, strategy_id=sid, strategy_name=sid, contract_key=symbol,
                  symbol=symbol, kind="entry", position_side=side,
                  order_side=("buy" if side == "long" else "sell"),
                  qty=qty, ref_price=float(ref), entry_price=None,
                  mult=1.0, currency="KRW", definition={"name": name or sid})


def test_handoff_zero_broker_orders_and_ledger_moves(isolated_trader):
    """하락일 핸드오프: A 롱청산5 + B 롱진입5 → 브로커 0·A 소멸·B 생성(기준가)."""
    t, broker = isolated_trader
    _seed_long(t, "A", "005930", 5, entry=90)

    decisions: list[dict] = []
    t._apply_netted_leg(_exit_leg("A", "005930", 5, entry=90, ref=100), decisions)
    t._apply_netted_leg(_entry_leg("B", "005930", 5, ref=100), decisions)

    assert broker.submitted == [], "넷팅은 브로커 실주문을 내면 안 됨"
    assert "A" not in t.ledger, "A 당일 롱은 청산(이관)돼야"
    assert t.ledger["B"]["qty"] == 5
    assert t.ledger["B"]["entry_price"] == 100.0, "B는 기준가로 개시"
    assert t.ledger["B"]["side"] == "long"


def test_handoff_realizes_closer_pnl_at_ref(isolated_trader):
    """닫는 전략 A의 실현손익 = (기준가−진입가)×수량 (넷팅 표식)."""
    t, broker = isolated_trader
    _seed_long(t, "A", "005930", 5, entry=90)
    decisions: list[dict] = []
    t._apply_netted_leg(_exit_leg("A", "005930", 5, entry=90, ref=100), decisions)
    sold = [d for d in decisions if d["action"] == "sold"]
    assert len(sold) == 1
    assert sold[0].get("netted") is True, "넷팅 청산 결정에 netted 표식"


def test_handoff_no_intent_seed_ledger_is_truth(isolated_trader):
    """N1: 넷팅 leg은 intent 시드를 쓰지 않는다(브로커 미접촉) — 원장이 멱등성 진실.

    시드를 쓰면 같은 (sid,symbol,side)의 잔여 실주문이 is_active에 막히므로 반드시 미기록.
    재실행 idempotency는 원장(A 소멸·B 생성)으로 held-check가 보장한다.
    """
    t, broker = isolated_trader
    _seed_long(t, "A", "005930", 5, entry=90)
    decisions: list[dict] = []
    t._apply_netted_leg(_exit_leg("A", "005930", 5, entry=90, ref=100), decisions)
    t._apply_netted_leg(_entry_leg("B", "005930", 5, ref=100), decisions)
    assert not intents.is_active("2026-06-01", "A", "005930", "sell"), \
        "넷팅 청산은 intent 시드 없어야(잔여 실주문 미차단)"
    assert not intents.is_active("2026-06-01", "B", "005930", "buy")
    assert "A" not in t.ledger and t.ledger["B"]["qty"] == 5   # 원장이 멱등성 진실


def test_partial_handoff_reduces_slot(isolated_trader):
    """부분 핸드오프: A 롱5 중 3만 넷팅 청산 → A 슬롯 2 잔존."""
    t, broker = isolated_trader
    _seed_long(t, "A", "005930", 5, entry=90)
    decisions: list[dict] = []
    t._apply_netted_leg(_exit_leg("A", "005930", 3, entry=90, ref=100), decisions)
    assert broker.submitted == []
    assert t.ledger["A"]["qty"] == 2, "부분 이관 후 잔여 2계약"


def test_short_handoff(isolated_trader):
    """숏 핸드오프: Z 숏청산(buy) + W 숏진입(sell) → 브로커 0·Z 소멸·W 숏 생성."""
    t, broker = isolated_trader
    t.ledger["Z"] = {"symbol": "코스피200선물", "qty": 4, "entry_date": "2026-05-20",
                     "entry_price": 110.0, "peak_price": 110.0, "side": "short",
                     "strategy_name": "Z", "definition": {}}
    decisions: list[dict] = []
    t._apply_netted_leg(_exit_leg("Z", "코스피200선물", 4, entry=110, ref=100, side="short"),
                        decisions)
    t._apply_netted_leg(_entry_leg("W", "코스피200선물", 4, ref=100, side="short"), decisions)
    assert broker.submitted == []
    assert "Z" not in t.ledger, "Z 숏 환매(이관)"
    assert t.ledger["W"]["qty"] == 4 and t.ledger["W"]["side"] == "short"


# ── Task 3.1 — plan_close_liquidations (의도 산출·발주 안 함) ────────────────────

def _seed_daytrade(t, sid, symbol, qty, entry):
    t.ledger[sid] = {"symbol": symbol, "qty": qty, "entry_date": "2026-06-01",
                     "entry_price": float(entry), "peak_price": float(entry),
                     "side": "long", "strategy_name": sid,
                     "definition": _daytrade_def(symbol)}


def test_plan_liquidations_daytrade(isolated_trader):
    t, broker = isolated_trader
    _seed_daytrade(t, "A", "005930", 5, entry=90)
    broker._prices["005930"] = 100.0
    broker.set_positions([{"symbol": "005930", "qty": 5, "side": "long"}])
    snap = broker.account_snapshot()
    legs = t.plan_close_liquidations(_ds(), "stock", "KRX", snap)
    assert broker.submitted == [], "PLAN은 발주하지 않는다"
    assert len(legs) == 1
    lg = legs[0]
    assert (lg.sid, lg.kind, lg.position_side, lg.order_side) == ("A", "exit", "long", "sell")
    assert lg.qty == 5 and lg.ref_price == 100.0 and lg.entry_price == 90.0
    assert lg.contract_key == "005930"


def test_plan_liquidations_excludes_overnight(isolated_trader):
    t, broker = isolated_trader
    # hold_days=1(오버나이트) → 종가 당일청산 대상 아님
    t.ledger["B"] = {"symbol": "005930", "qty": 3, "entry_date": "2026-06-01",
                     "entry_price": 90.0, "peak_price": 90.0, "side": "long",
                     "strategy_name": "B",
                     "definition": {**_daytrade_def(), "position": {
                         **_daytrade_def()["position"], "exit": {"hold_days": 1}}}}
    broker._prices["005930"] = 100.0
    broker.set_positions([{"symbol": "005930", "qty": 3, "side": "long"}])
    legs = t.plan_close_liquidations(_ds(), "stock", "KRX", broker.account_snapshot())
    assert legs == []


def test_plan_liquidations_clamps_external_sell(isolated_trader):
    t, broker = isolated_trader
    _seed_daytrade(t, "A", "005930", 5, entry=90)   # 원장 5
    broker._prices["005930"] = 100.0
    broker.set_positions([{"symbol": "005930", "qty": 3, "side": "long"}])  # 실보유 3(외부 2매도)
    legs = t.plan_close_liquidations(_ds(), "stock", "KRX", broker.account_snapshot())
    assert len(legs) == 1 and legs[0].qty == 3, "실보유로 클램프(외부매도 초과 방지)"


# ── Task 3.2 — plan_close_entries (여력원장 크레딧·발주 안 함) ────────────────────

def _overnight_def(symbol="005930", pct=100):
    return {"name": "ov", "engine": "ir",
            "universe": {"kind": "single", "symbols": [symbol]},
            "signal": _DUMMY_SIGNAL,
            "position": {"direction": "long",
                         "sizing": {"mode": "pct_cash", "amount_pct": pct},
                         "entry": {"mode": "on_signal"}, "exit": {"hold_days": 1}},
            "simulation": {"fill": "close"}}


def _liq(sid, symbol, qty, ref):
    return Intent(sid=sid, strategy_id=sid, strategy_name=sid, contract_key=symbol,
                  symbol=symbol, kind="exit", position_side="long", order_side="sell",
                  qty=qty, ref_price=float(ref), entry_price=90.0, mult=1.0,
                  currency="KRW", definition={})


def test_plan_entries_no_credit_sizes_zero_without_cash(isolated_trader):
    """크레딧 없으면(청산 없음) 현금 0 → 진입 사이징 0 (skip_funds)."""
    t, broker = isolated_trader
    broker._balance["cash"] = 0
    broker._prices["005930"] = 100.0
    by_strat = [{"strategy_id": "B", "candidates": [{"symbol": "005930"}]}]
    strats = [{"id": "B", "name": "B", "definition": _overnight_def(), "account_ref": None}]
    captured, decisions = t.plan_close_entries(
        by_strat, strats, _ds(prev_close=100.0), 10_000_000.0, [], "KRX", "stock")
    assert captured == [], "현금 0·크레딧 없음 → 진입 의도 없음"
    assert broker.submitted == [], "PLAN은 발주하지 않는다"


def test_plan_entries_credit_enables_sizing(isolated_trader):
    """E1: 같은 종목 청산 회수 현금을 크레딧하면 진입이 그 여력으로 사이징된다."""
    t, broker = isolated_trader
    broker._balance["cash"] = 0                        # 기저 현금 0(A가 점유했다고 가정)
    broker._prices["005930"] = 100.0
    liq = [_liq("A", "005930", 50, ref=100)]           # A 청산 50주 → 회수 현금 5000
    by_strat = [{"strategy_id": "B", "candidates": [{"symbol": "005930"}]}]
    strats = [{"id": "B", "name": "B", "definition": _overnight_def(), "account_ref": None}]
    captured, decisions = t.plan_close_entries(
        by_strat, strats, _ds(prev_close=100.0), 10_000_000.0, liq, "KRX", "stock")
    assert broker.submitted == []
    assert len(captured) == 1, decisions
    ce = captured[0]
    assert ce.kind == "entry" and ce.order_side == "buy" and ce.symbol == "005930"
    assert ce.qty == 50, "회수 현금 5000 / 100원 = 50주 (크레딧 없으면 0)"
    assert ce.ref_price == 100.0


def test_plan_entries_credit_depletes_across_multiple(isolated_trader):
    """E2/N9: 같은 종목 다중 진입은 회수 여력을 순차 소진(중복 사용 안 함)."""
    t, broker = isolated_trader
    broker._balance["cash"] = 0
    broker._prices["005930"] = 100.0
    liq = [_liq("A", "005930", 50, ref=100)]           # 회수 현금 5000 = 50주분
    # B1, B2 둘 다 005930 종가진입(각 pct_cash 100%) → 합쳐 50주까지만
    by_strat = [{"strategy_id": "B1", "candidates": [{"symbol": "005930"}]},
                {"strategy_id": "B2", "candidates": [{"symbol": "005930"}]}]
    strats = [{"id": "B1", "name": "B1", "definition": _overnight_def(), "account_ref": None},
              {"id": "B2", "name": "B2", "definition": _overnight_def(), "account_ref": None}]
    captured, _ = t.plan_close_entries(
        by_strat, strats, _ds(prev_close=100.0), 10_000_000.0, liq, "KRX", "stock")
    total = sum(c.qty for c in captured)
    assert total == 50, f"다중 진입 합계가 회수여력(50주)을 넘지 않아야 — 실제 {total}"


# ── Phase 4 — run_close_netting 전체 사이클 (PLAN-NET-APPLY) ─────────────────────

def test_close_netting_full_handoff_no_broker_orders(isolated_trader):
    """하락일 완전 넷팅: A 당일청산50 + B 오버나이트진입50 → 브로커 0·원장 이관·절약>0."""
    t, broker = isolated_trader
    broker._balance["cash"] = 0
    broker._prices["005930"] = 100.0
    broker.set_positions([{"symbol": "005930", "qty": 50, "side": "long"}])
    _seed_daytrade(t, "A", "005930", 50, entry=90)
    by_strat = [{"strategy_id": "B", "candidates": [{"symbol": "005930"}]}]
    strats = [{"id": "B", "name": "B", "definition": _overnight_def(), "account_ref": None}]
    payload = t.run_close_netting(by_strat, strats, _ds(prev_close=100.0),
                                  market="KRX", instrument_class="stock")
    assert broker.submitted == [], "완전 넷팅 → 브로커 주문 0"
    assert "A" not in t.ledger and t.ledger["B"]["qty"] == 50
    cs = payload["cycle_summary"]
    assert cs["n_netted"] == 50 and cs["commission_saved_krw"] > 0
    assert cs["n_sold"] == 0 and cs.get("n_bought", 0) == 0, "넷팅은 실거래로 세지 않음(N3)"


def test_close_netting_liquidation_only_submits_sell(isolated_trader):
    """넷팅 대상 없음(청산만): 잔여 실 매도 발주(현행 동작 보존)."""
    t, broker = isolated_trader
    broker._prices["005930"] = 100.0
    broker.set_positions([{"symbol": "005930", "qty": 50, "side": "long"}])
    _seed_daytrade(t, "A", "005930", 50, entry=90)
    payload = t.run_close_netting([], [], _ds(prev_close=100.0),
                                  market="KRX", instrument_class="stock")
    assert len(broker.submitted) == 1 and broker.submitted[0]["side"] == "sell"
    assert payload["cycle_summary"]["n_netted"] == 0


def test_close_netting_entry_only_submits_buy(isolated_trader):
    """넷팅 대상 없음(진입만): 잔여 실 매수 발주(현행 동작 보존)."""
    t, broker = isolated_trader
    broker._balance["cash"] = 1_000_000
    broker._prices["005930"] = 100.0
    by_strat = [{"strategy_id": "B", "candidates": [{"symbol": "005930"}]}]
    strats = [{"id": "B", "name": "B", "definition": _overnight_def(pct=10),
               "account_ref": None}]
    payload = t.run_close_netting(by_strat, strats, _ds(prev_close=100.0),
                                  market="KRX", instrument_class="stock")
    assert len(broker.submitted) == 1 and broker.submitted[0]["side"] == "buy"
    assert payload["cycle_summary"]["n_netted"] == 0


def test_close_netting_killswitch_blocks_entry_liquidates(isolated_trader):
    """킬스위치 활성: 진입 계획 skip(넷팅 없음)·청산은 진행(E5)."""
    from localapp import killswitch
    t, broker = isolated_trader
    killswitch.activate("test")
    broker._prices["005930"] = 100.0
    broker.set_positions([{"symbol": "005930", "qty": 50, "side": "long"}])
    _seed_daytrade(t, "A", "005930", 50, entry=90)
    by_strat = [{"strategy_id": "B", "candidates": [{"symbol": "005930"}]}]
    strats = [{"id": "B", "name": "B", "definition": _overnight_def(), "account_ref": None}]
    payload = t.run_close_netting(by_strat, strats, _ds(prev_close=100.0),
                                  market="KRX", instrument_class="stock", risk_limits={})
    assert any(d["action"] == "skip_killswitch" for d in payload["decisions"])
    assert len(broker.submitted) == 1 and broker.submitted[0]["side"] == "sell"
    assert payload["cycle_summary"]["n_netted"] == 0

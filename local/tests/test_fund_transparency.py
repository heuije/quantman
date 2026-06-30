"""P6-1 — 체결 invest(투입 투명성) 부착. scenarios 하니스 재사용.

체결 시점에 per-order 투입 정보를 trade 기록(ev)과 decision에 싣는지 검증:
  - 주식: invest.amount = 수량×체결가 (레버리지 없음)
  - 선물: invest.notional = 수량×체결가×승수, margin = notional×개시증거금률,
          leverage = 1/개시증거금률 (코스피200선물 → 250000·0.195·5.1x)

서버 preview는 선물을 사이징 못 함(보안경계) — 발주 시점 로컬이 진실원천.

isolated_trader는 scenarios/conftest.py에 있어 tests/ 루트에선 안 보이므로,
test_account_guard.py 선례대로 최소 fixture를 인라인 복사한다(_apply_fill만 구동).

    cd local && python -m pytest tests/test_fund_transparency.py -v
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest
from pytest import approx

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from sim.broker import SimBroker  # noqa: E402


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
    broker = SimBroker()
    from localapp import coverage
    monkeypatch.setattr(coverage, "covered_categories",
                        lambda: {"kr_equity", "us_equity", "kr_futures", "us_futures"})
    return tr.Trader(broker), broker


def _pending(sid, symbol, side, **extra):
    """test_futures_ledger.py의 _pending 패턴 — _apply_fill에 넘길 주문 dict."""
    p = {"strategy_id": sid, "symbol": symbol, "side": side,
         "strategy_name": "t", "definition": {}, "reason": extra.pop("reason", "")}
    p.update(extra)
    return p


def _last_trade():
    from localapp import trader as tr
    path = tr.TRADES_PATH
    lines = path.read_text(encoding="utf-8").splitlines()
    return json.loads([ln for ln in lines if ln.strip()][-1])


def test_stock_buy_records_amount(isolated_trader):
    """주식 매수 체결 → invest.amount == 수량×체결가, currency KRW, 레버리지 없음."""
    trader, _ = isolated_trader
    qty, fill = 10, 70000.0
    decisions: list[dict] = []
    trader._apply_fill("O1", _pending("e1", "005930", "buy"), qty, fill, decisions)

    # bought decision extra
    bought = [d for d in decisions if d["action"] == "bought"][-1]
    inv = bought["invest"]
    assert inv["amount"] == approx(qty * fill)        # 700,000
    assert inv["currency"] == "KRW"
    assert "leverage" not in inv                       # 주식엔 레버리지 없음
    assert "notional" not in inv

    # trade ev에도 동일 부착
    ev = _last_trade()
    assert ev["invest"]["amount"] == approx(qty * fill)


def test_futures_buy_records_notional_margin_leverage(isolated_trader):
    """코스피200선물 매수 체결 → invest.notional/margin/leverage.

    코스피200선물 spec: multiplier 250000·init_margin_rate 0.195(=레버리지 5.1x, 1자리 반올림).
    """
    trader, _ = isolated_trader
    qty, fill = 2, 350.0
    decisions: list[dict] = []
    trader._apply_fill("O1", _pending("f1", "코스피200선물", "buy"), qty, fill, decisions)

    bought = [d for d in decisions if d["action"] == "bought"][-1]
    inv = bought["invest"]
    assert inv["notional"] == approx(qty * fill * 250_000)       # 175,000,000
    assert inv["margin"] == approx(inv["notional"] * 0.195)      # 34,125,000
    assert inv["leverage"] == approx(5.1)                        # round(1/0.195, 1) = 5.1
    assert inv["currency"] == "KRW"
    assert "amount" not in inv

    # trade ev에도 동일 부착
    ev = _last_trade()
    assert ev["invest"]["notional"] == approx(qty * fill * 250_000)
    assert ev["invest"]["leverage"] == approx(5.1)

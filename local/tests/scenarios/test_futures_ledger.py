"""M4 — Trader ledger 선물 인지(롱/숏 정산회계) + 주식 무변경 검증.

_apply_fill을 직접 구동해 ledger side/qty/entry_price + 거래로그 realized_pnl을 확인.
isolated_trader(SimBroker + tmp 영속경로·KST 고정)로 격리. 정산손익 = (청산−진입)×계약×승수×부호.
"""
from __future__ import annotations

import json

from localapp import trader as tr


def _pending(sid, symbol, side, **extra):
    p = {"strategy_id": sid, "symbol": symbol, "side": side,
         "strategy_name": "t", "definition": {}, "reason": extra.pop("reason", "")}
    p.update(extra)
    return p


def _trades():
    path = tr.TRADES_PATH
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ── 선물 롱 ───────────────────────────────────────────────────────────────────
def test_futures_long_open_then_close_settles_pnl(isolated_trader):
    trader, _ = isolated_trader
    trader._apply_fill("O1", _pending("s1", "금선물", "buy"), 2, 1900.0, [])
    lg = trader.ledger["s1"]
    assert lg["side"] == "long" and lg["qty"] == 2 and lg["entry_price"] == 1900.0
    trader._apply_fill("O2", _pending("s1", "금선물", "sell", reason="익절"), 2, 1950.0, [])
    assert "s1" not in trader.ledger                                  # 전량 청산
    ev = _trades()[-1]
    assert ev["action"] == "sell" and ev["realized_pnl"] == (1950 - 1900) * 2 * 100  # 10000


def test_futures_long_add_averages(isolated_trader):
    trader, _ = isolated_trader
    trader._apply_fill("O1", _pending("s1", "금선물", "buy"), 2, 1900.0, [])
    trader._apply_fill("O2", _pending("s1", "금선물", "buy"), 2, 2000.0, [])
    lg = trader.ledger["s1"]
    assert lg["qty"] == 4 and lg["entry_price"] == 1950.0 and lg["side"] == "long"


# ── 선물 숏 ───────────────────────────────────────────────────────────────────
def test_futures_short_open_then_close_settles_pnl(isolated_trader):
    trader, _ = isolated_trader
    # 보유 없는 상태 매도 = 숏 진입(선물만)
    trader._apply_fill("O1", _pending("s2", "금선물", "sell"), 1, 1950.0, [])
    lg = trader.ledger["s2"]
    assert lg["side"] == "short" and lg["qty"] == 1 and lg["entry_price"] == 1950.0
    # 환매(buy) = 숏 청산. 하락에서 이익: (1950−1900)×1×100 = 5000
    trader._apply_fill("O2", _pending("s2", "금선물", "buy", reason="청산"), 1, 1900.0, [])
    assert "s2" not in trader.ledger
    ev = _trades()[-1]
    assert ev["action"] == "buy" and ev["realized_pnl"] == 5000


def test_futures_short_add_averages(isolated_trader):
    trader, _ = isolated_trader
    trader._apply_fill("O1", _pending("s2", "금선물", "sell"), 1, 1950.0, [])
    trader._apply_fill("O2", _pending("s2", "금선물", "sell"), 1, 2050.0, [])
    lg = trader.ledger["s2"]
    assert lg["side"] == "short" and lg["qty"] == 2 and lg["entry_price"] == 2000.0


# ── 주식 무변경 ────────────────────────────────────────────────────────────────
def test_equity_long_open_close_no_realized_field(isolated_trader):
    trader, _ = isolated_trader
    trader._apply_fill("O1", _pending("e1", "005930", "buy"), 10, 70000.0, [])
    lg = trader.ledger["e1"]
    assert lg["side"] == "long" and lg["qty"] == 10
    trader._apply_fill("O2", _pending("e1", "005930", "sell"), 10, 71000.0, [])
    assert "e1" not in trader.ledger
    ev = _trades()[-1]
    assert ev["action"] == "sell" and "realized_pnl" not in ev       # 주식 거래로그 무변경


def test_equity_sell_without_position_is_noop(isolated_trader):
    trader, _ = isolated_trader
    trader._apply_fill("O1", _pending("e9", "005930", "sell"), 5, 70000.0, [])
    assert "e9" not in trader.ledger        # 보유 없는 주식 매도 = 무동작(숏 진입 아님)


# ── M5d 부호방향 long_short — preview 후보 direction으로 진입 라우팅 ──────────────
import pandas as pd  # noqa: E402
from sim import scenario  # noqa: E402


def _fut_ds(symbol, px):
    idx = pd.date_range("2026-05-01", periods=5, freq="B")
    return {symbol: pd.DataFrame(
        {"Open": [px] * 5, "High": [px] * 5, "Low": [px] * 5, "Close": [px] * 5}, index=idx)}


def _directional_def(symbol):
    # futures_margin_pct=100 → 코스피200선물(승수 250k) 1계약 사이징 성립.
    return {
        "name": "양방향 선물", "engine": "ir",
        "universe": {"kind": "single", "symbols": [symbol]},
        "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
        "position": {"direction": "long_short",
                     "sizing": {"mode": "pct_cash", "amount_pct": 100,
                                "futures_margin_pct": 100},
                     "entry": {"mode": "on_signal", "threshold": 0.0},
                     "exit": {"hold_days": 0}, "overlays": {}},
        "execution": {"use_limit": False},
        "simulation": {},
    }


def test_directional_short_candidate_opens_short(isolated_trader):
    """long_short 전략 + 후보 direction='short' → 선물 sell-to-open → 원장 숏."""
    trader, broker = isolated_trader
    sym = "코스피200선물"
    broker._prices[sym] = 300.0
    n_before = len(broker.submitted)
    decisions: list = []
    ok = trader._try_buy_one_symbol(
        "ls1", "ls1", "양방향", _directional_def(sym), sym, _fut_ds(sym, 300.0),
        10_000_000.0, decisions, cand_direction="short")
    assert ok, decisions
    assert len(broker.submitted) == n_before + 1, broker.submitted
    order = broker.submitted[-1]
    assert order["side"] == "sell", order               # sell-to-open(숏 개시)
    scenario.inject_ws_fill(trader, broker, order["order_no"], order["qty"], 300.0)
    assert trader.ledger["ls1"]["side"] == "short", trader.ledger


def test_directional_long_candidate_opens_long(isolated_trader):
    """대칭 — 후보 direction='long' → 매수 진입(롱)."""
    trader, broker = isolated_trader
    sym = "코스피200선물"
    broker._prices[sym] = 300.0
    decisions: list = []
    ok = trader._try_buy_one_symbol(
        "ls2", "ls2", "양방향", _directional_def(sym), sym, _fut_ds(sym, 300.0),
        10_000_000.0, decisions, cand_direction="long")
    assert ok, decisions
    assert broker.submitted[-1]["side"] == "buy", broker.submitted


def test_directional_no_direction_skips(isolated_trader):
    """방향 정보 없는 long_short 후보(cand_direction=None) → 무음 롱전환 방지 skip."""
    trader, broker = isolated_trader
    sym = "코스피200선물"
    broker._prices[sym] = 300.0
    n_before = len(broker.submitted)
    decisions: list = []
    ok = trader._try_buy_one_symbol(
        "ls3", "ls3", "양방향", _directional_def(sym), sym, _fut_ds(sym, 300.0),
        10_000_000.0, decisions, cand_direction=None)
    assert not ok
    assert len(broker.submitted) == n_before                  # 발주 0
    assert any(d.get("action") == "skip_unsupported" for d in decisions), decisions

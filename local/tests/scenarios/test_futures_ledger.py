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

"""sim 선물 헬퍼 단위검증 — instrument_spec(승수·증거금) 재사용."""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent   # tests → local
for _p in (str(_LOCAL), str(_LOCAL.parent / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sim.futures import settlement_pnl, required_margin, make_futures_position


def test_settlement_pnl_long():
    # 코스피200선물: 승수 250_000. 롱 2계약, 375.0→377.0 = +2pt×2×250k = +1,000,000
    assert settlement_pnl("코스피200선물", "long", 2, 375.0, 377.0) == 1_000_000.0


def test_settlement_pnl_short_profits_on_drop():
    # 숏 1계약, 377.0→375.0(하락) = +2pt 이익 ×250k = +500,000
    assert settlement_pnl("코스피200선물", "short", 1, 377.0, 375.0) == 500_000.0


def test_required_margin():
    # notional = 375×2×250_000 = 187_500_000; 개시증거금률 0.10 → 18_750_000
    assert required_margin("코스피200선물", 2, 375.0) == 18_750_000.0


def test_make_futures_position_shape():
    p = make_futures_position("코스피200선물", "long", 2, 375.0, 377.0)
    assert p["symbol"] == "코스피200선물" and p["side"] == "long" and p["qty"] == 2
    assert p["avg_price"] == 375.0 and p["eval_price"] == 377.0
    assert p["multiplier"] == 250_000.0
    assert p["margin_requirement"] == 18_750_000.0
    assert p["eval_pnl"] == 1_000_000.0


from sim.broker import SimBroker


def test_simbroker_holds_futures_positions_and_margin():
    b = SimBroker()
    b.set_positions([make_futures_position("코스피200선물", "long", 2, 375.0, 377.0)])
    b.set_margin(total_margin=18_750_000.0, available_margin=100_000_000.0)
    snap = b.account_snapshot()
    assert snap["positions"][0]["side"] == "long"
    assert snap["positions"][0]["multiplier"] == 250_000.0
    assert snap["margin"] == {"total_margin": 18_750_000.0, "available_margin": 100_000_000.0}


def test_simbroker_stock_snapshot_has_no_margin_key():
    # 주식 회귀: margin 미설정이면 margin 키 없음(기존 동작 보존).
    b = SimBroker()
    assert "margin" not in b.account_snapshot()

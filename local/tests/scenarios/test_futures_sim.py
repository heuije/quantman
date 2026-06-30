"""선물 테스트베드 시나리오 — SimBroker 위 롱·숏 라운드트립 정산손익 + 증거금 불변식.

프로덕션 Trader ledger의 선물화는 P3(라우팅 배선과 함께). P0는 테스트베드 측 검증.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent.parent
for _p in (str(_LOCAL), str(_LOCAL.parent / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sim import invariants
from sim.broker import SimBroker
from sim.futures import make_futures_position, required_margin, settlement_pnl


def test_long_roundtrip_settlement_pnl():
    # 롱 2계약 375→377 보유 → 정산손익 +1,000,000. 불변식 통과.
    b = SimBroker()
    b.set_positions([make_futures_position("코스피200선물", "long", 2, 375.0, 377.0)])
    b.set_margin(required_margin("코스피200선물", 2, 375.0), 100_000_000.0)
    snap = b.account_snapshot()
    invariants.check_futures_sign(snap["positions"])
    invariants.check_futures_pnl(snap["positions"])
    invariants.check_futures_margin(snap)
    assert snap["positions"][0]["eval_pnl"] == 1_000_000.0


def test_short_roundtrip_profits_on_drop():
    # 숏 1계약 377→375(하락) → 정산손익 +500,000(숏 이익).
    b = SimBroker()
    b.set_positions([make_futures_position("코스피200선물", "short", 1, 377.0, 375.0)])
    b.set_margin(required_margin("코스피200선물", 1, 377.0), 100_000_000.0)
    snap = b.account_snapshot()
    invariants.check_futures_sign(snap["positions"])
    invariants.check_futures_pnl(snap["positions"])
    assert snap["positions"][0]["eval_pnl"] == 500_000.0


def test_overleverage_caught_by_invariant():
    import pytest
    b = SimBroker()
    b.set_margin(total_margin=120_000_000.0, available_margin=100_000_000.0)
    with pytest.raises(AssertionError, match="INV-FUT-3"):
        invariants.check_futures_margin(b.account_snapshot())


def test_event_buy_qty_futures_sizing_reused():
    # 사이징 재사용 회귀: 증거금예산/(px×승수×증거금률) = floor.
    # 코스피200선물 단일. px=375, 승수 250k, 증거금률 0.195 → 1계약 증거금 18,281,250.
    # 선물 예산 = cash × futures_margin_pct%(amount_pct 아님). cash 200M:
    #   기본 20%  → 40M / 18.28M = 2계약   (보수적 기본·유저 조절 가능 안전상한)
    #   100%(full-margin) → 200M / 18.28M = 10계약
    from quant_core.blocks import const
    from quant_core.ir_engine import StrategyIR, Universe, PositionSpec, Entry, Sizing
    from quant_core.ir_engine import live as ir_live

    def _ir(fmp: float) -> StrategyIR:
        return StrategyIR(
            universe=Universe(kind="single", symbols=["코스피200선물"]),
            signal=const(True),
            position=PositionSpec(
                entry=Entry(mode="always"),
                sizing=Sizing(mode="pct_cash", futures_margin_pct=fmp),
            ),
        )
    assert ir_live.event_buy_qty(_ir(20.0), cash=200_000_000.0, prev_close=375.0,
                                 capital=200_000_000.0) == 2     # 기본 20%
    assert ir_live.event_buy_qty(_ir(100.0), cash=200_000_000.0, prev_close=375.0,
                                 capital=200_000_000.0) == 10    # full-margin

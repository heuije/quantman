"""M5c — 숏 진입(direction→sell-to-open) + long_short 보류.

direction="short" 전략은 매도진입으로 숏 포지션 개시(definition 저장→나중 환매 청산).
long=매수진입(기존). long_short=라이브 v1 미지원(명시 skip).
"""
from __future__ import annotations

import pandas as pd
from quant_core.blocks import const
from quant_core.exec_defaults import merged_execution
from quant_core.ir_engine import Entry, PositionSpec, Sizing, StrategyIR, Universe


def _strat_def(direction: str) -> dict:
    ir = StrategyIR(
        universe=Universe(kind="single", symbols=["코스피200선물"]),
        signal=const(True),
        position=PositionSpec(direction=direction, entry=Entry(mode="always"),
                              sizing=Sizing(mode="pct_cash", amount_pct=100.0)),
    )
    return ir.model_dump()


def _dataset():
    return {"코스피200선물": pd.DataFrame({"Close": [350.0, 350.0]})}


def test_submit_open_short_creates_short_with_definition(isolated_trader):
    trader, broker = isolated_trader
    sd = _strat_def("short")
    trader._submit_open_short("k1", "t", sd, "코스피200선물", 1, 350.0, merged_execution(None), [])
    # sell-to-open = 매도 주문이 pending(side="sell")
    sells = [p for p in trader.pending.values() if p["side"] == "sell" and p["symbol"] == "코스피200선물"]
    assert len(sells) == 1
    order_no = sells[0]["order_no"]
    # 체결 → 보유없는 선물 매도 = 숏 진입(M4 _apply_fill). definition 저장(나중 청산 규칙).
    broker.mark_filled(order_no, 1, 350.0)
    trader._resolve_pending([])
    lg = trader.ledger["k1"]
    assert lg["side"] == "short" and lg["qty"] == 1 and lg["definition"]


def test_try_buy_routes_short_to_sell_to_open(isolated_trader):
    trader, broker = isolated_trader
    ok = trader._try_buy_one_symbol("k1", "k1", "t", _strat_def("short"),
                                    "코스피200선물", _dataset(), 0.0, [])
    assert ok
    assert any(o["side"] == "sell" for o in broker.submitted)      # 매도진입
    assert not any(o["side"] == "buy" for o in broker.submitted)


def test_try_buy_routes_long_to_buy(isolated_trader):
    trader, broker = isolated_trader
    ok = trader._try_buy_one_symbol("k1", "k1", "t", _strat_def("long"),
                                    "코스피200선물", _dataset(), 0.0, [])
    assert ok
    assert any(o["side"] == "buy" for o in broker.submitted)       # 매수진입
    assert not any(o["side"] == "sell" for o in broker.submitted)


def test_try_buy_long_short_skipped(isolated_trader):
    trader, broker = isolated_trader
    decisions: list[dict] = []
    ok = trader._try_buy_one_symbol("k1", "k1", "t", _strat_def("long_short"),
                                    "코스피200선물", _dataset(), 0.0, decisions)
    assert ok is False                                             # 미지원 skip
    assert not broker.submitted                                    # 발주 없음
    assert any(d.get("action") == "skip_unsupported" for d in decisions)

"""M6 — 만기 자동청산 백스톱 (tier-2) + 진입 시 계약코드·만기일 ledger 기록.

자동매매 선물은 만기 前 강제청산해야 한다(물리인도/현금정산으로 포지션이 사라지기 전에).
유저 청산규칙(보유기간·매도조건 등)이 먼저(tier-1), 미발동 시 만기 백스톱(tier-2)이 닫는다.
만기일은 진입 시 ledger에 기록(현 front-month가 보유 중 롤로 바뀌므로 진입 때 고정).

isolated_trader는 kst_today=2026-06-01 고정. KOSPI200 default_roll=days_before:5 → lead=5.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from quant_core.blocks import const
from quant_core.exec_defaults import merged_execution
from quant_core.ir_engine import Entry, PositionSpec, Sizing, StrategyIR, Universe
from sim.broker import SimBroker


def _strat_def_no_exit(direction: str = "long") -> dict:
    """청산 규칙 없는 전략 — _exit_reason_for가 None(유저 tier-1 미발동) → 백스톱만 트리거."""
    ir = StrategyIR(
        universe=Universe(kind="single", symbols=["코스피200선물"]),
        signal=const(True),
        position=PositionSpec(direction=direction, entry=Entry(mode="always"),
                              sizing=Sizing(mode="pct_cash", amount_pct=100.0)),
    )
    return ir.model_dump()


def _fut_pos(expiry_iso, *, side="long", symbol="코스피200선물") -> dict:
    pos = {"symbol": symbol, "qty": 1, "side": side, "entry_price": 350.0,
           "entry_date": "2026-05-20", "peak_price": 350.0,
           "strategy_name": "t", "definition": _strat_def_no_exit(side)}
    if expiry_iso is not None:
        pos["expiry_date"] = expiry_iso
    return pos


# ── _expiry_close_reason — 백스톱 판정(tier-2) ─────────────────────────────────
def test_backstop_fires_within_lead(isolated_trader):
    trader, _ = isolated_trader
    # lead=5 → 만기 6/4면 6/4-5=5/30, today 6/1 ≥ 5/30 → 발동
    r = trader._expiry_close_reason(_fut_pos("2026-06-04"), date(2026, 6, 1))
    assert r and "만기" in r


def test_backstop_silent_when_far(isolated_trader):
    trader, _ = isolated_trader
    # 만기 9/10 → 9/10-5=9/5, today 6/1 < 9/5 → 미발동
    assert trader._expiry_close_reason(_fut_pos("2026-09-10"), date(2026, 6, 1)) is None


def test_backstop_boundary_exact(isolated_trader):
    trader, _ = isolated_trader
    # 만기 6/6 → 6/6-5=6/1 == today → 경계 발동(>=)
    assert trader._expiry_close_reason(_fut_pos("2026-06-06"), date(2026, 6, 1))


def test_backstop_none_when_expiry_unrecorded(isolated_trader):
    trader, _ = isolated_trader
    # 만기 미기록(M6 이전 진입 등) → 백스톱 불가(None, 경고만)
    assert trader._expiry_close_reason(_fut_pos(None), date(2026, 6, 1)) is None


def test_backstop_none_for_equity(isolated_trader):
    trader, _ = isolated_trader
    pos = {"symbol": "005930", "qty": 10, "side": "long", "expiry_date": "2026-06-04"}
    assert trader._expiry_close_reason(pos, date(2026, 6, 1)) is None


# ── 진입 시 계약코드·만기일 ledger 기록 ───────────────────────────────────────
class _ExpiryBroker(SimBroker):
    """contract_expiry를 제공하는 SimBroker(라이브 BrokerRouter 대역)."""
    def contract_expiry(self, symbol):
        return ("A01606", date(2026, 6, 11))


def test_entry_records_contract_and_expiry(isolated_trader):
    trader, _ = isolated_trader
    trader.broker = _ExpiryBroker()
    trader.broker._prices["코스피200선물"] = 350.0   # 가격 평면 정책 — 발주는 broker 시세 필요
    trader._submit_buy("k1", "t", _strat_def_no_exit("long"),
                       "코스피200선물", 1, 350.0, merged_execution(None), [])
    on = trader.broker.submitted[-1]["order_no"]
    trader.broker.mark_filled(on, 1, 350.0)
    trader._resolve_pending([])
    lg = trader.ledger["k1"]
    assert lg["contract_code"] == "A01606"
    assert lg["expiry_date"] == "2026-06-11"


def test_entry_equity_has_no_contract_keys(isolated_trader):
    # 주식 진입은 계약/만기 키 무부착 → ledger byte-identical (SimBroker엔 contract_expiry도 없음)
    trader, broker = isolated_trader
    broker._prices["005930"] = 70_000.0           # 가격 평면 정책 — 발주는 broker 시세 필요
    trader._submit_buy("e1", "t", {}, "005930", 10, 70_000.0, merged_execution(None), [])
    on = broker.submitted[-1]["order_no"]
    broker.mark_filled(on, 10, 70_000.0)
    trader._resolve_pending([])
    lg = trader.ledger["e1"]
    assert "contract_code" not in lg and "expiry_date" not in lg


# ── 통합: cycle 청산 패스에서 백스톱이 실제 청산 발주 ──────────────────────────
def _dataset():
    return {"코스피200선물": pd.DataFrame({"Close": [350.0, 350.0]})}


def test_cycle_backstop_closes_near_expiry_long(isolated_trader):
    trader, broker = isolated_trader
    broker._prices["코스피200선물"] = 350.0       # 가격 평면 정책 — 청산 발주도 broker 시세 필요
    trader.ledger["k1"] = _fut_pos("2026-06-04")            # 만기 임박 롱
    broker.set_positions([{"symbol": "코스피200선물", "qty": 1, "side": "long"}])
    trader.cycle([], _dataset(), today=date(2026, 6, 1), buy_candidates=[], market="KRX")
    # 만기 백스톱 → 롱 청산(매도) 발주
    assert any(o["side"] == "sell" and o["symbol"] == "코스피200선물"
               for o in broker.submitted)


def test_cycle_no_close_when_far_expiry(isolated_trader):
    # 통제군: 같은 구성이나 만기가 멀면 청산 없음 → (a) 정의에 유저 청산규칙 없음 확인,
    # (b) 백스톱이 만기 멀 땐 침묵함을 통합 레벨에서 증명.
    trader, broker = isolated_trader
    trader.ledger["k1"] = _fut_pos("2026-09-10")            # 만기 먼 롱
    broker.set_positions([{"symbol": "코스피200선물", "qty": 1, "side": "long"}])
    trader.cycle([], _dataset(), today=date(2026, 6, 1), buy_candidates=[], market="KRX")
    assert not any(o["symbol"] == "코스피200선물" for o in broker.submitted)


def test_cycle_backstop_closes_near_expiry_short(isolated_trader):
    trader, broker = isolated_trader
    broker._prices["코스피200선물"] = 350.0       # 가격 평면 정책 — 청산 발주도 broker 시세 필요
    trader.ledger["s1"] = _fut_pos("2026-06-04", side="short")   # 만기 임박 숏
    broker.set_positions([{"symbol": "코스피200선물", "qty": 1, "side": "short"}])
    trader.cycle([], _dataset(), today=date(2026, 6, 1), buy_candidates=[], market="KRX")
    # 숏 만기청산 = 환매(buy-to-close)
    assert any(o["side"] == "buy" and o["symbol"] == "코스피200선물"
               for o in broker.submitted)

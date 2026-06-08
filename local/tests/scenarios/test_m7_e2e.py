"""M7 — 선물 자동매매 E2E 통합(M2~M6 배선이 BrokerRouter 위에서 맞물리는지).

per-milestone 단위테스트는 bare SimBroker(데이터셋 심볼로 set_positions)로 우회해, 라이브
경로의 통합 갭을 못 봤다: 라이브 잔고는 선물을 **계약코드(A01606·GCM26)**로 키잉하나 ledger/
reconcile/over-sell 클램프는 **데이터셋 심볼("코스피200선물")** 기준 → 병합·정규화가 없으면
선물 청산이 항상 skip되고 reconcile이 포지션을 고아 오삭제한다.

이 모듈은 **Trader를 BrokerRouter(stock=Sim, futures=Sim) 위에 올려** 그 통합 수정을 증명한다:
계약코드로 잔고를 든 선물 포지션이 청산되고(클램프 통과), reconcile에 보존된다.

isolated_trader는 kst_today=2026-06-01 고정. 코스피200선물 default_roll=days_before:5 → lead=5.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
from quant_core.blocks import const
from quant_core.exec_defaults import merged_execution
from quant_core.ir_engine import Entry, PositionSpec, Sizing, StrategyIR, Universe
from sim.broker import SimBroker

from localapp.broker_router import BrokerRouter

_CODES = {"코스피200선물": "A01606", "금선물": "GCM26"}
_EXPS = {"코스피200선물": ("A01606", date(2026, 6, 4)),   # 만기 임박(6/4)
         "금선물": ("GCM26", date(2026, 6, 26))}


def _strat_def_no_exit(direction="long") -> dict:
    ir = StrategyIR(
        universe=Universe(kind="single", symbols=["코스피200선물"]),
        signal=const(True),
        position=PositionSpec(direction=direction, entry=Entry(mode="always"),
                              sizing=Sizing(mode="pct_cash", amount_pct=100.0)),
    )
    return ir.model_dump()


def _router_trader(isolated_trader, monkeypatch):
    """Trader를 BrokerRouter(stock+futures Sim) 위에 — 라이브 토폴로지 모사."""
    trader, _ = isolated_trader
    stock, fut = SimBroker(), SimBroker()
    router = BrokerRouter(stock, fut, resolve=lambda s: _CODES.get(s),
                          resolve_expiry=lambda s: _EXPS.get(s, (None, None)))
    trader.broker = router
    # 발주 사실만 검증 — cycle의 미체결 60초 폴링 대기 제거(시간 절약, 동작 무관).
    monkeypatch.setattr(trader, "_wait_pending", lambda *a, **k: None)
    return trader, stock, fut


def _dataset():
    return {"코스피200선물": pd.DataFrame({"Close": [350.0, 350.0]})}


def _seed_long(trader, fut, expiry="2026-06-04"):
    """라이브 토폴로지: ledger=데이터셋심볼, broker 잔고=계약코드 키."""
    trader.ledger["k1"] = {
        "symbol": "코스피200선물", "qty": 1, "side": "long", "entry_price": 350.0,
        "entry_date": "2026-05-20", "peak_price": 350.0, "strategy_name": "t",
        "definition": _strat_def_no_exit("long"),
        "contract_code": "A01606", "expiry_date": expiry,
    }
    fut.set_positions([{"symbol": "A01606", "side": "long", "qty": 1, "avg_price": 350.0}])


# ── 진입: 라우터 통해 진입하면 계약코드/만기가 ledger에 기록 ──────────────────────
def test_e2e_entry_records_contract_and_expiry_via_router(isolated_trader, monkeypatch):
    trader, stock, fut = _router_trader(isolated_trader, monkeypatch)
    trader._submit_buy("k1", "t", _strat_def_no_exit(), "코스피200선물", 1, 350.0,
                       merged_execution(None), [])
    on = fut.submitted[-1]["order_no"]               # 국내선물 buy_limit → futures Sim
    fut.mark_filled(on, 1, 350.0)
    trader._resolve_pending([])
    lg = trader.ledger["k1"]
    assert lg["symbol"] == "코스피200선물"
    assert lg["contract_code"] == "A01606" and lg["expiry_date"] == "2026-06-04"


# ── 핵심 증명 ①: 계약코드로 잔고 든 선물 포지션이 청산된다(병합·정규화로 클램프 통과) ──
def test_e2e_expiry_close_works_through_router(isolated_trader, monkeypatch):
    trader, stock, fut = _router_trader(isolated_trader, monkeypatch)
    _seed_long(trader, fut, expiry="2026-06-04")     # 만기 임박
    trader.cycle([], _dataset(), today=date(2026, 6, 1), buy_candidates=[], market="KRX")
    # 만기 백스톱 청산 → 국내선물 sell이 계약코드로 발주(클램프가 병합 스냅샷서 보유 인식).
    assert any(o["side"] == "sell" and o["symbol"] == "A01606" for o in fut.submitted)


def test_e2e_no_close_when_clamp_sees_zero(isolated_trader, monkeypatch):
    # 대조군: broker가 그 포지션을 안 들고 있으면(외부 청산 추정) over-sell 클램프가 skip.
    trader, stock, fut = _router_trader(isolated_trader, monkeypatch)
    _seed_long(trader, fut, expiry="2026-06-04")
    fut.set_positions([])                             # broker 보유 0
    trader.cycle([], _dataset(), today=date(2026, 6, 1), buy_candidates=[], market="KRX")
    assert not any(o["symbol"] == "A01606" for o in fut.submitted)   # skip_oversell


# ── 핵심 증명 ②: reconcile이 선물 포지션을 고아 오삭제하지 않는다 ───────────────────
def test_e2e_reconcile_preserves_futures_position(isolated_trader, monkeypatch):
    trader, stock, fut = _router_trader(isolated_trader, monkeypatch)
    _seed_long(trader, fut, expiry="2026-09-10")     # 만기 멀어 백스톱 무관
    trader.reconcile_with_kis("2026-06-01")
    # 병합 스냅샷이 A01606→코스피200선물로 정규화 → ledger와 매칭 → 보존(고아 아님).
    assert "k1" in trader.ledger and trader.ledger["k1"]["qty"] == 1


def test_e2e_reconcile_removes_when_broker_flat(isolated_trader, monkeypatch):
    # 대조군: broker가 실제로 안 들고 있으면(외부 청산) reconcile이 차감 — 올바른 동작.
    trader, stock, fut = _router_trader(isolated_trader, monkeypatch)
    _seed_long(trader, fut, expiry="2026-09-10")
    fut.set_positions([])
    trader.reconcile_with_kis("2026-06-01")
    assert "k1" not in trader.ledger


# ── 숏 라이프사이클: 진입(sell-to-open)→broker 숏 보유→만기청산(buy-to-close) ───────
def test_e2e_short_expiry_close_through_router(isolated_trader, monkeypatch):
    trader, stock, fut = _router_trader(isolated_trader, monkeypatch)
    trader.ledger["s1"] = {
        "symbol": "코스피200선물", "qty": 1, "side": "short", "entry_price": 350.0,
        "entry_date": "2026-05-20", "peak_price": 350.0, "strategy_name": "t",
        "definition": _strat_def_no_exit("short"),
        "contract_code": "A01606", "expiry_date": "2026-06-04",
    }
    fut.set_positions([{"symbol": "A01606", "side": "short", "qty": 1, "avg_price": 350.0}])
    trader.cycle([], _dataset(), today=date(2026, 6, 1), buy_candidates=[], market="KRX")
    # 숏 만기청산 = 환매(buy-to-close)가 계약코드로 발주
    assert any(o["side"] == "buy" and o["symbol"] == "A01606" for o in fut.submitted)

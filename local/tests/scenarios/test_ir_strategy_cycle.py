"""Workbench 시나리오 — IR(전략 연구소) 전략 자동매매 사이클 end-to-end.

"자동매매 시뮬레이션 모듈"(SimBroker 하니스)을 IR에 맞게 검증: IR 전략이 결정경로
(매수 사이징 = event_buy_qty)로 발주 → 체결 → 원장 → 청산판정(cycle_exit_reason) →
매도 체결 → FLAT 까지 production trader 로직으로 결정론적으로 통과하는지 단언.

    cd platform/local && python -m pytest tests/scenarios/test_ir_strategy_cycle.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp.trader import _exit_reason_for
from sim import invariants, scenario
from sim.broker import SimBroker

_DUMMY_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "right": {"op": "const", "params": {"value": 0}}},
}
# 매도조건: Close < ts_mean(Close, 3)
_SELL_COND = {
    "op": "compare", "params": {"op": "<"},
    "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "right": {"op": "ts_mean", "params": {"window": 3},
                         "inputs": {"signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}}},
}


def _ir_def(sizing, universe, exit_spec=None):
    return {
        "name": "IR 전략", "engine": "ir",
        "universe": universe, "signal": _DUMMY_SIGNAL,
        "position": {"direction": "long", "sizing": sizing,
                     "entry": {"mode": "on_signal"}, "exit": exit_spec or {}, "overlays": {}},
        "simulation": {},
    }


def _dataset(closes):
    idx = pd.date_range("2026-05-01", periods=len(closes), freq="B")
    return {"005930": pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)}


# 마지막 종가 70000, 3일평균(80000,75000,70000)=75000 → Close<평균 → 매도조건 참.
_DS = _dataset([90000, 85000, 82000, 80000, 78000, 80000, 75000, 70000])


def test_ir_event_buy_sizing_fill_and_exit(isolated_trader):
    """IR 이벤트 진입(pct_cash 10%, 리스트 유니버스) → 사이징·체결·원장·청산 E2E."""
    t, broker = isolated_trader
    broker._prices["005930"] = 70000
    sid = "ir1"
    ir = _ir_def(sizing={"mode": "pct_cash", "amount_pct": 10},
                 universe={"kind": "list", "symbols": ["005930", "000660"]},
                 exit_spec={"condition": _SELL_COND})

    # 1) 매수 결정경로(_try_buy_one_symbol → event_buy_qty) → 체결
    order_no, decisions = scenario.strategy_buy_and_fill(
        t, broker, sid, ir, "005930", _DS, fill_price=70000.0, equity=10_000_000.0)
    assert order_no is not None, decisions
    # cash 1000만 × 10% = 100만 / 70000 = 14주 (event_buy_qty 파리티)
    assert broker.submitted[-1]["qty"] == 14, broker.submitted
    assert t.ledger[sid]["qty"] == 14
    invariants.check_all(t)

    # 2) EOD 청산 판정 — IR position.exit 매도조건(Close<MA3) 참 → "매도조건"
    held = 1
    reason, strat = _exit_reason_for(t.ledger[sid]["definition"], held, _DS, "005930")
    assert reason == "매도조건"
    assert strat is None              # IR → operand strat 없음

    # 3) 매도 발주 + 체결 → FLAT (주문 라이프사이클은 engine-무관)
    r = broker.sell_limit("005930", 14, 69000)
    t._after_submit(r, sid, "IR 전략", {}, "005930", "sell", 14, 70000, 69000,
                    {"use_limit": True, "sell_tolerance_pct": 1.0}, [], reason="매도조건")
    scenario.inject_ws_fill(t, broker, r["order_no"], 14, 69000.0)
    assert sid not in t.ledger        # FLAT
    invariants.check_all(t)


def test_ir_single_universe_full_invest(isolated_trader):
    """단일 종목 유니버스 → amount_pct 무시, 100% 전액 사이징(엔진 _budget 파리티)."""
    t, broker = isolated_trader
    broker._prices["005930"] = 70000
    ir = _ir_def(sizing={"mode": "pct_cash", "amount_pct": 10},
                 universe={"kind": "single", "symbols": ["005930"]})
    order_no, _ = scenario.strategy_buy_and_fill(
        t, broker, "ir2", ir, "005930", _DS, fill_price=70000.0, equity=10_000_000.0)
    assert order_no is not None
    # 단일 → 100% = 1000만 / 70000 = 142주
    assert t.ledger["ir2"]["qty"] == 142, broker.submitted


def test_ir_enter_from_preview_routes_and_sizes(isolated_trader):
    """실 진입 경로 `_enter_from_preview`가 IR 전략을 라우팅·사이징(operand trade_symbol 파싱 안 거침)."""
    t, broker = isolated_trader
    broker._prices["005930"] = 70000
    sid = "ir3"
    ir = _ir_def(sizing={"mode": "pct_cash", "amount_pct": 10},
                 universe={"kind": "list", "symbols": ["005930", "000660"]})
    strategies = [{"id": sid, "name": "IR 전략", "definition": ir}]
    by_strategy = [{"strategy_id": sid, "candidates": [{"symbol": "005930"}]}]
    decisions: list[dict] = []
    t._enter_from_preview(by_strategy, strategies, _DS, 10_000_000.0,
                          decisions, set(), market="KRX", catchup=False)
    assert len(broker.submitted) == 1, decisions      # IR 파싱 실패로 skip되지 않음
    assert broker.submitted[-1]["qty"] == 14          # event_buy_qty (list pct_cash 10%)
    last = broker.submitted[-1]
    scenario.inject_ws_fill(t, broker, last["order_no"], last["qty"], 70000.0)
    assert t.ledger[f"{sid}:005930"]["qty"] == 14     # 다중키 원장
    invariants.check_all(t)


def _futures_dataset(closes, symbol="코스피200선물"):
    idx = pd.date_range("2026-05-01", periods=len(closes), freq="B")
    return {symbol: pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)}


# 코스피200선물 종가 375 (승수 250k·증거금률 카탈로그 0.195 → 1계약 ≈18.28M).
_FUT_DS = _futures_dataset([370, 372, 374, 376, 375])


def test_ir_domestic_futures_model_a_clamps_to_orderable(isolated_trader):
    """모델 A: 국내선물 % 진입에서 broker.orderable_qty가 주입되면 발주 qty=model_a_qty.

    라이브 계약수 = floor(사용률% × 주문가능수량). orderable=7·사용률 20% →
    model_a_qty(event_qty, 7, 20)=min(7, floor(7×0.20))=1. event_buy_qty(카탈로그
    추정 증거금률) 산정값과 무관하게 브로커 주문가능수량이 1차 기준임을 검증.
    """
    from quant_core.ir_engine import live as ir_live

    t, broker = isolated_trader
    broker._prices["코스피200선물"] = 375
    # 선물 사이징 현금 — 충분히 커서 event_buy_qty>0 (모델A 미적용이면 10계약 발주됐을 값).
    broker._balance["futures_order_cash_kr"] = 1_000_000_000
    # 인스턴스 레벨 주입(픽스처마다 새 SimBroker — 전역 오염 없음). getattr 가드가 이를 발견.
    broker.orderable_qty = lambda symbol, side, price: 7

    sid = "irfut"
    ir = _ir_def(sizing={"mode": "pct_cash", "futures_margin_pct": 20},
                 universe={"kind": "single", "symbols": ["코스피200선물"]})
    order_no, decisions = scenario.strategy_buy_and_fill(
        t, broker, sid, ir, "코스피200선물", _FUT_DS, fill_price=375.0,
        equity=1_000_000_000.0)
    assert order_no is not None, decisions
    expected = ir_live.model_a_qty(99, 7, 20.0)        # = 1 (event_qty 무관, min 캡)
    assert expected == 1
    assert broker.submitted[-1]["qty"] == expected, broker.submitted


def test_ir_domestic_futures_no_orderable_qty_degrades(isolated_trader):
    """degrade 가드: SimBroker(orderable_qty 없음)는 모델A 건너뜀 → event_buy_qty 유지.

    이것이 671 회귀 byte-identical의 근거 — 기존 sim 시나리오는 orderable_qty가 없어
    모델A 분기를 타지 않는다. cash 1e9×20%/(375×250000×0.195) = 2e8/18.28M = 10계약.
    """
    from quant_core.ir_engine import StrategyIR
    from quant_core.ir_engine import live as ir_live

    t, broker = isolated_trader
    broker._prices["코스피200선물"] = 375
    broker._balance["futures_order_cash_kr"] = 1_000_000_000
    assert not hasattr(broker, "orderable_qty")        # 가드 전제 — Sim엔 없음

    sid = "irfut2"
    ir = _ir_def(sizing={"mode": "pct_cash", "futures_margin_pct": 20},
                 universe={"kind": "single", "symbols": ["코스피200선물"]})
    order_no, decisions = scenario.strategy_buy_and_fill(
        t, broker, sid, ir, "코스피200선물", _FUT_DS, fill_price=375.0,
        equity=1_000_000_000.0)
    assert order_no is not None, decisions
    expected = ir_live.event_buy_qty(StrategyIR.model_validate(ir),
                                     cash=1_000_000_000.0, prev_close=375.0,
                                     capital=1_000_000_000.0, symbol="코스피200선물")
    assert expected == 10
    assert broker.submitted[-1]["qty"] == expected, broker.submitted


def test_ir_domestic_futures_orderable_fail_holds_order(isolated_trader):
    """degrade→skip: 실 브로커(orderable_qty 보유)가 None(조회 실패) 반환 → **발주 보류**.

    카탈로그 증거금률(0.195)로 event_buy_qty(10계약)를 산정하더라도, 실 브로커 주문가능수량
    조회가 실패하면 그 카탈로그 추정만으로 발주하지 않는다(브로커 실시간 증거금률이 진실원천).
    실전 자금안전 위해 조회 실패 시 qty=0으로 발주 보류(skip_funds) — 카탈로그 10계약 발주 안 함.
    (SimBroker는 orderable_qty 메서드 자체가 없어 이 경로에 도달 안 함 — 위 _degrades 테스트가 보장.)
    """
    t, broker = isolated_trader
    broker._prices["코스피200선물"] = 375
    broker._balance["futures_order_cash_kr"] = 1_000_000_000
    broker.orderable_qty = lambda symbol, side, price: None     # 실 브로커 조회 실패 시뮬

    sid = "irfut3"
    ir = _ir_def(sizing={"mode": "pct_cash", "futures_margin_pct": 20},
                 universe={"kind": "single", "symbols": ["코스피200선물"]})
    order_no, decisions = scenario.strategy_buy_and_fill(
        t, broker, sid, ir, "코스피200선물", _FUT_DS, fill_price=375.0,
        equity=1_000_000_000.0)
    assert order_no is None, broker.submitted                  # 발주 보류
    assert broker.submitted == [], broker.submitted            # 카탈로그 10계약 발주 안 함(★ 자금안전)
    assert any(d.get("action") == "skip_funds" for d in decisions), decisions


def test_ir_intraday_stop_loss_fill_and_flat(isolated_trader):
    """IR 장중 손절(-5%) tick → IntradayStopManager 트리거 → 실 매도→체결→FLAT.

    실 머니패스: 매수→체결(원장 definition 자기완결 저장) → on_tick이 pos["definition"]
    (engine=ir) 디스패치 → intraday_exit_reason 손절 → trader._submit_sell → WS 체결 → FLAT.
    """
    from localapp.intraday_stop import IntradayStopManager

    t, broker = isolated_trader
    broker._prices["005930"] = 70000
    sid = "ir_int"
    ir = _ir_def(sizing={"mode": "pct_cash", "amount_pct": 10},
                 universe={"kind": "list", "symbols": ["005930", "000660"]},
                 exit_spec={"stop_loss": -5.0})

    # 1) 매수 → 체결 (event_buy_qty: cash 1000만×10%/70000 = 14주)
    order_no, _ = scenario.strategy_buy_and_fill(
        t, broker, sid, ir, "005930", _DS, fill_price=70000.0, equity=10_000_000.0)
    assert order_no is not None
    qty = t.ledger[sid]["qty"]
    assert qty == 14
    # on_tick의 L-04 클램프가 실 보유를 확인 → SimBroker 잔고에도 반영
    broker.set_positions([{"symbol": "005930", "qty": qty}])

    # 2) 장중 손절 tick — (66000-70000)/70000 = -5.71% ≤ -5% → 손절(intraday)
    mgr = IntradayStopManager(broker=broker, get_ledger=lambda: t.ledger,
                              submit_sell_fn=t._submit_sell, dataset=_DS)
    mgr.reset_daily()
    mgr.on_tick("005930", 66000.0)

    # 3) 매도 발주됨(production 경로) → WS 체결 → FLAT
    sell = broker.submitted[-1]
    assert sell["side"] == "sell" and sell["qty"] == 14, broker.submitted
    scenario.inject_ws_fill(t, broker, sell["order_no"], 14, 66000.0)
    assert sid not in t.ledger        # FLAT
    invariants.check_all(t)


def test_ir_intraday_no_trigger_when_above_stop(isolated_trader):
    """손절선 위(−2%)면 트리거 안 됨 — 매도 발주 없음(가격기반 정밀도)."""
    from localapp.intraday_stop import IntradayStopManager

    t, broker = isolated_trader
    broker._prices["005930"] = 70000
    sid = "ir_int2"
    ir = _ir_def(sizing={"mode": "pct_cash", "amount_pct": 10},
                 universe={"kind": "list", "symbols": ["005930", "000660"]},
                 exit_spec={"stop_loss": -5.0})
    scenario.strategy_buy_and_fill(
        t, broker, sid, ir, "005930", _DS, fill_price=70000.0, equity=10_000_000.0)
    broker.set_positions([{"symbol": "005930", "qty": t.ledger[sid]["qty"]}])
    n_before = len(broker.submitted)

    mgr = IntradayStopManager(broker=broker, get_ledger=lambda: t.ledger,
                              submit_sell_fn=t._submit_sell, dataset=_DS)
    mgr.reset_daily()
    mgr.on_tick("005930", 68600.0)   # -2% > -5% → 미발동

    assert len(broker.submitted) == n_before   # 매도 없음
    assert sid in t.ledger                      # 보유 유지
    invariants.check_all(t)

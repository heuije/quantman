"""패리티 오라클(F-01) — 백테스트 vs 라이브(Trader+SimBroker) 경제결과 대조 골든.

배경(2026-06-12 라이브 실증 결함 S): "구글 일일 100만원 시가매수"가 GOOG($353)에
2,832주(≈$93만) 사이징을 시도 — `event_buy_qty`의 fixed_amount가 amount_krw(₩)를
통화 환산 없이 USD 가격으로 나눴다. 백테스트 엔진 `_budget`도 같은 식이라 양쪽이
같은 배수로 깨졌다(**패리티 통과 ≠ 정확**).

이 파일은 "백테스트=라이브 코드 공유" 가정을 *경제결과*로 강제한다: 같은 시나리오
(같은 dataset·같은 체결가)를 ① `run_strategy_ir`(엔진 표준 진입)과 ② 실제 Trader의
결정경로(`_enter_from_preview` → `_try_buy_one_symbol` → `event_buy_qty`)로 돌려
수량·체결가·실현손익이 일치하는지 단언한다. 코드가 갈라지는 미래 회귀를 잡는 안전망.

시나리오:
  a. USD 정액(실증 사고 재현) — GOOG $353, 일일 ₩1,000,000 시가매수·종가매도,
     USDKRW=1370 → 기대 수량 = floor(1,000,000/(353×1370)) = **2주**.
     · 백테스트: 수량 2 + 진입/청산가 고정.
     · 라이브: 2주 초과 발주가 절대 없어야 함(과대 사이징 차단 가드).
     · 라이브 완전 패리티(수량 2 발주·왕복 손익 일치) — trader가 event_buy_qty에
       symbol·dataset을 전달(F-01 배선)하므로 본 단언으로 강제.
  b. KRW 회귀 가드 — 같은 시나리오의 005930판(₩70,000): 수정 전후 결과 무변경.
     백테스트·라이브 둘 다 14주, 왕복 실현손익 −14,000 일치.

    cd platform/local && python -m pytest tests/scenarios/test_parity_oracle_fx.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import market_index
from localapp import trader as tr_mod
from localapp.trader import _exit_reason_for, _policy
from sim import scenario
from sim.broker import SimBroker

from quant_core.ir_engine import StrategyIR, run_strategy_ir

_FX = "원달러환율"
_IDX = pd.date_range("2026-05-27", periods=3, freq="B")
_SID = "po1"

_ALWAYS_TRUE = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "right": {"op": "const", "params": {"value": 0}}},
}


def _bars(open_: float, close: float, idx=_IDX) -> pd.DataFrame:
    o = np.full(len(idx), float(open_))
    c = np.full(len(idx), float(close))
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c), "Low": np.minimum(o, c),
                         "Close": c, "Volume": 1e6}, index=idx)


def _fx_df(rate: float, idx=_IDX) -> pd.DataFrame:
    return pd.DataFrame({"Close": [float(rate)] * len(idx)}, index=idx)


def _ir_def(symbol: str, amount_krw: float) -> dict:
    """실증 사고 전략 형태 — 단일 종목·일일 정액(₩)·시가매수(next_open)·종가매도(hold 0).

    비용 0으로 고정해 백테스트 트레이드 가격·손익이 라이브 체결가 산식과 1:1 비교되게 한다.
    """
    return {
        "name": "패리티 오라클", "engine": "ir",
        "universe": {"kind": "single", "symbols": [symbol]},
        "signal": _ALWAYS_TRUE,
        "position": {"direction": "long",
                     "sizing": {"mode": "fixed_amount", "amount_krw": amount_krw},
                     "entry": {"mode": "on_signal"}, "exit": {"hold_days": 0},
                     "overlays": {}},
        "simulation": {"initial_capital": 10_000_000.0, "commission": 0.0,
                       "slippage": 0.0, "sell_tax": 0.0},
    }


def _bt_first_trade(ir_def: dict, ds: dict, per_share_loss: float):
    """백테스트 절반 — 엔진 표준 진입(run_strategy_ir)으로 첫 왕복의 (수량, 진입가, 청산가).

    trades에 수량 컬럼이 없어 비용 0 당일매매의 equity 변화로 역산:
    equity[1] = initial − qty×(진입가−청산가).
    """
    res = run_strategy_ir(StrategyIR.model_validate(ir_def), ds)
    assert res["success"], res.get("error")
    assert len(res["trades"]) >= 1, "백테스트 트레이드 없음"
    tr0 = res["trades"].iloc[0]
    qty = round((10_000_000.0 - float(res["equity"].iloc[1])) / per_share_loss)
    return int(qty), float(tr0["진입가"]), float(tr0["청산가"])


def _inject_us_index(monkeypatch):
    """시장 라우팅을 결정론·오프라인으로 — GOOG=US(NAS), 그 외(005930 등)=국내 형태 판정."""
    monkeypatch.setattr(market_index, "_state", {
        "by_ticker": {"GOOG": {"exchange": "NAS", "currency": "USD", "kind": "stock"}},
        "fetched_at": "2026-06-01T00:00:00+00:00"})


def _live_enter(trader, ir_def: dict, symbol: str, ds_live: dict, market: str) -> list[dict]:
    """라이브 절반 진입 — 실제 production 결정경로(_enter_from_preview)를 구동."""
    strategies = [{"id": _SID, "name": ir_def["name"], "definition": ir_def}]
    by_strategy = [{"strategy_id": _SID, "candidates": [{"symbol": symbol}]}]
    decisions: list[dict] = []
    trader._enter_from_preview(by_strategy, strategies, ds_live, 10_000_000.0,
                               decisions, set(), market=market, catchup=False)
    return decisions


def _live_roundtrip(trader, broker, ir_def: dict, symbol: str, ds_live: dict,
                    buy_fill: float, sell_fill: float):
    """매수 체결 → 당일청산 판정(is_close) → 매도 발주·체결 → FLAT. (수량, 실현손익) 반환."""
    buy = broker.submitted[-1]
    assert buy["side"] == "buy", broker.submitted
    qty = int(buy["qty"])
    scenario.inject_ws_fill(trader, broker, buy["order_no"], qty, buy_fill)
    assert trader.ledger[_SID]["qty"] == qty

    reason, _ = _exit_reason_for(trader.ledger[_SID]["definition"], 0, ds_live,
                                 symbol, is_close=True)
    assert reason == "당일청산"
    broker._prices[symbol] = sell_fill          # 미국 매도 지정가는 신선 현재가 기준
    decisions: list[dict] = []
    trader._submit_sell(_SID, ir_def["name"], symbol, qty, buy_fill,
                        _policy(ir_def), reason, decisions)
    sell = broker.submitted[-1]
    assert sell["side"] == "sell" and int(sell["qty"]) == qty, broker.submitted
    scenario.inject_ws_fill(trader, broker, sell["order_no"], qty, sell_fill)
    assert _SID not in trader.ledger            # FLAT
    return qty, qty * (sell_fill - buy_fill)


# ── 시나리오 a — USD 정액 (실증 사고 재현) ─────────────────────────────────────

_GOOG_BT = {"GOOG": _bars(353.0, 350.0), _FX: _fx_df(1370.0)}
# 라이브 번들 = 의사결정 시점(아침)까지의 데이터 — 전일까지 1바(전일종가 353).
_GOOG_LIVE = {"GOOG": _bars(353.0, 353.0, _IDX[:1]), _FX: _fx_df(1370.0, _IDX[:1])}
_USD_EXPECTED_QTY = 2          # floor(1,000,000 / (353 × 1370)) — 결함 코드는 2,832


def _usd_trader(isolated_trader):
    """USD 매수여력 경로(usd_cash 모드)를 SimBroker로 — cash_usd 충분(클램프 비구속)."""
    t, _ = isolated_trader      # 픽스처 = trader 모듈 영속경로 tmp 격리(부수효과만 사용)
    broker = SimBroker(balance={"cash": 0, "total_eval": 10_000_000,
                                "foreign_eval_krw": 0,
                                "cash_usd": 2_000_000.0, "fx_usdkrw": 1370.0})
    broker._prices["GOOG"] = 353.0
    trader = tr_mod.Trader(broker)
    trader._us_bp_mode = "usd_cash"   # SimBroker엔 buying_power_usd(통합증거금) API 없음
    return trader, broker


def test_oracle_usd_backtest_qty(isolated_trader, monkeypatch):
    """백테스트: GOOG 일일 ₩100만 시가매수 = 2주/일. 결함 코드는 2,832주(₩→$ 무환산)."""
    _inject_us_index(monkeypatch)
    qty_bt, entry_px, exit_px = _bt_first_trade(_ir_def("GOOG", 1_000_000),
                                                _GOOG_BT, per_share_loss=3.0)
    assert (entry_px, exit_px) == (353.0, 350.0)
    assert qty_bt == _USD_EXPECTED_QTY, \
        f"백테스트 USD 정액 수량 {qty_bt} ≠ {_USD_EXPECTED_QTY} (₩1,000,000/(353×1370))"


def test_oracle_usd_live_never_oversizes(isolated_trader, monkeypatch):
    """라이브(실 Trader 결정경로): 기대 수량(2주)을 초과하는 발주가 절대 없어야 한다.

    결함 코드는 2,832주(≈$93만)를 발주한다(실증 사고). 수정+배선 후엔 정확 수량(2)만.
    """
    _inject_us_index(monkeypatch)
    trader, broker = _usd_trader(isolated_trader)
    decisions = _live_enter(trader, _ir_def("GOOG", 1_000_000), "GOOG",
                            _GOOG_LIVE, market="US")
    oversized = [s for s in broker.submitted if int(s["qty"]) > _USD_EXPECTED_QTY]
    assert not oversized, (
        f"USD 정액 과대 사이징 발주: {[(s['symbol'], s['qty']) for s in oversized]} "
        f"(기대 ≤ {_USD_EXPECTED_QTY}주) — decisions={decisions}")


def test_oracle_usd_live_parity_roundtrip(isolated_trader, monkeypatch):
    """라이브 완전 패리티(F-01 배선): 수량 2 발주 → 353 체결 → 당일청산 350 → 손익 −6 = 백테스트."""
    _inject_us_index(monkeypatch)
    ir = _ir_def("GOOG", 1_000_000)
    qty_bt, entry_px, exit_px = _bt_first_trade(ir, _GOOG_BT, per_share_loss=3.0)

    trader, broker = _usd_trader(isolated_trader)
    _live_enter(trader, ir, "GOOG", _GOOG_LIVE, market="US")
    assert broker.submitted and int(broker.submitted[-1]["qty"]) == qty_bt == _USD_EXPECTED_QTY
    qty_live, pnl_live = _live_roundtrip(trader, broker, ir, "GOOG", _GOOG_LIVE,
                                         buy_fill=entry_px, sell_fill=exit_px)
    assert qty_live == qty_bt
    assert pnl_live == pytest.approx(qty_bt * (exit_px - entry_px))   # −6.0 USD


# ── 시나리오 b — KRW 회귀 가드 (수정 전후 무변경) ──────────────────────────────

def test_oracle_krw_parity_roundtrip(isolated_trader, monkeypatch):
    """KRW 같은 시나리오(005930 ₩70,000): 백테스트=라이브 수량 14·왕복 손익 −14,000 일치.

    이 테스트는 FX 수정 *전에도 후에도* 그대로 통과해야 한다(₩ 경로 byte-identical 가드).
    dataset에 원달러환율이 있어도 KRW 사이징은 환산하지 않는다.
    """
    _inject_us_index(monkeypatch)
    ir = _ir_def("005930", 1_000_000)
    ds_bt = {"005930": _bars(70000.0, 69000.0), _FX: _fx_df(1370.0)}
    qty_bt, entry_px, exit_px = _bt_first_trade(ir, ds_bt, per_share_loss=1000.0)
    assert (qty_bt, entry_px, exit_px) == (14, 70000.0, 69000.0)

    trader, broker = isolated_trader            # 기본 잔고: 국내 cash 1,000만
    broker._prices["005930"] = 70000.0
    ds_live = {"005930": _bars(70000.0, 70000.0, _IDX[:1]), _FX: _fx_df(1370.0, _IDX[:1])}
    _live_enter(trader, ir, "005930", ds_live, market="KRX")
    assert broker.submitted, "KRW 정액 진입이 발주되지 않음 — ₩ 경로 회귀"
    assert int(broker.submitted[-1]["qty"]) == qty_bt == 14

    qty_live, pnl_live = _live_roundtrip(trader, broker, ir, "005930", ds_live,
                                         buy_fill=entry_px, sell_fill=exit_px)
    assert qty_live == qty_bt
    pnl_bt = qty_bt * (exit_px - entry_px)
    assert pnl_live == pytest.approx(pnl_bt) == pytest.approx(-14000.0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

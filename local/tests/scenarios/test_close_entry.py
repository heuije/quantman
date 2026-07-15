"""Workbench 시나리오 — 종가매수(fill=close) 진입 사이클 E2E (Stage C).

종가 진입은 아침 시가창(fill=next_open)과 대칭인 종가창 전용 진입 경로다:
  · 아침 진입 패스(entry_window="open")는 fill=close 전략을 진입하지 않는다(종가창 전담).
  · 종가 진입 패스(entry_window="close")는 fill=close 전략만, 참조가=종가 무렵 현재가로 발주.
  · instrument_class로 주식/선물 종가창을 분리(15:25 주식·15:40 선물).
  · enter_close_candidates가 킬스위치·drawdown 게이트를 적용(아침 진입 §3과 동일).
익일 시가매도(hold_days≥1)는 다음날 아침 청산 패스가 처리하므로 여기선 진입만 검증한다.

    cd platform/local && python -m pytest tests/scenarios/test_close_entry.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from sim import invariants, scenario

_DUMMY_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "right": {"op": "const", "params": {"value": 0}}},
}


def _ir_def(fill: str, symbol: str = "005930", sizing: dict | None = None):
    """단일/리스트 유니버스 이벤트 IR — simulation.fill로 진입창을 가른다."""
    return {
        "name": f"fill={fill}", "engine": "ir",
        "universe": {"kind": "list", "symbols": [symbol, "000660"]},
        "signal": _DUMMY_SIGNAL,
        "position": {"direction": "long",
                     "sizing": sizing or {"mode": "pct_cash", "amount_pct": 10},
                     "entry": {"mode": "on_signal"},
                     "exit": {"hold_days": 1}, "overlays": {}},   # 오버나이트(익일 시가매도)
        "simulation": {"fill": fill},
    }


def _ds(symbol: str = "005930", prev_close: float = 50000.0):
    # 마지막 봉을 fixture kst_today(2026-06-01)의 직전 거래일(05-29)에 맞춘다 — 신선도 게이트가
    # stale로 오판하지 않도록(정상 진입 경로 검증). 값은 날짜 무관(전부 prev_close).
    idx = pd.date_range(end="2026-05-29", periods=8, freq="B")
    return {symbol: pd.DataFrame(
        {"Open": [prev_close] * 8, "High": [prev_close] * 8,
         "Low": [prev_close] * 8, "Close": [prev_close] * 8}, index=idx)}


def _cands(sid: str, symbol: str = "005930"):
    return [{"strategy_id": sid, "candidates": [{"symbol": symbol}]}]


def _strats(sid: str, ir: dict):
    return [{"id": sid, "name": ir["name"], "definition": ir, "account_ref": None}]


# ── 진입창 라우팅(fill) ────────────────────────────────────────────────────

def test_open_window_skips_close_fill(isolated_trader):
    """아침 시가창(entry_window='open')은 fill=close 전략을 진입하지 않는다 — 종가창 전담."""
    t, broker = isolated_trader
    broker._prices["005930"] = 100000.0
    sid = "closefill"
    ir = _ir_def("close")
    decisions: list[dict] = []
    t._enter_from_preview(_cands(sid), _strats(sid, ir), _ds(), 10_000_000.0,
                          decisions, set(), market="KRX", entry_window="open")
    assert broker.submitted == [], "fill=close는 아침 시가창서 진입되면 안 됨(종가창 전담)"


def test_open_window_enters_next_open(isolated_trader):
    """회귀: 아침 시가창은 fill=next_open 전략을 전일종가 기준으로 정상 진입."""
    t, broker = isolated_trader
    broker._prices["005930"] = 100000.0            # 현재가(무시돼야 — 시가창은 전일종가 기준)
    sid = "nextopen"
    ir = _ir_def("next_open")
    decisions: list[dict] = []
    t._enter_from_preview(_cands(sid), _strats(sid, ir), _ds(prev_close=50000.0),
                          10_000_000.0, decisions, set(), market="KRX",
                          entry_window="open")
    assert len(broker.submitted) == 1, decisions
    # 사이징 참조가 = 전일종가 50000 → 1000만×10%/50000 = 20주(현재가 100000이면 10주).
    assert broker.submitted[-1]["qty"] == 20, "시가창은 전일종가 기준이어야(현재가 아님)"


def test_close_window_enters_close_fill_at_current_price(isolated_trader):
    """종가창(entry_window='close')은 fill=close 전략을 *종가 무렵 현재가* 기준으로 진입."""
    t, broker = isolated_trader
    broker._prices["005930"] = 100000.0            # 종가 무렵 현재가
    sid = "closefill"
    ir = _ir_def("close")
    decisions: list[dict] = []
    t._enter_from_preview(_cands(sid), _strats(sid, ir), _ds(prev_close=50000.0),
                          10_000_000.0, decisions, set(), market="KRX",
                          entry_window="close", instrument_class="stock")
    assert len(broker.submitted) == 1, decisions
    # is_close_entry → 참조가=현재가 100000 → 1000만×10%/100000 = 10주(전일종가면 20주).
    assert broker.submitted[-1]["qty"] == 10, \
        "종가 진입은 전일종가가 아니라 종가 무렵 현재가 기준이어야(미체결 회피)"


def test_close_window_skips_next_open_fill(isolated_trader):
    """종가창은 fill=next_open 전략을 진입하지 않는다 — 시가창 전담."""
    t, broker = isolated_trader
    broker._prices["005930"] = 100000.0
    sid = "nextopen"
    ir = _ir_def("next_open")
    decisions: list[dict] = []
    t._enter_from_preview(_cands(sid), _strats(sid, ir), _ds(), 10_000_000.0,
                          decisions, set(), market="KRX",
                          entry_window="close", instrument_class="stock")
    assert broker.submitted == [], "fill=next_open은 종가창서 진입되면 안 됨(시가창 전담)"


def test_close_window_instrument_class_routing(isolated_trader):
    """종가창 클래스 필터: 주식 종가창은 선물 후보를 건드리지 않고, 선물 종가창이 진입한다."""
    t, broker = isolated_trader
    fut = "코스피200선물"
    broker._prices[fut] = 375.0
    broker._balance["cash"] = 1_000_000_000            # 선물 1계약 증거금(≈1828만) 여유
    sid = "futclose"
    ir = _ir_def("close", symbol=fut,
                 sizing={"mode": "pct_cash", "futures_margin_pct": 20})

    # 주식 종가창(instrument_class='stock') — 선물 후보는 클래스 필터로 skip(무발주).
    decisions: list[dict] = []
    t._enter_from_preview(_cands(sid, fut), _strats(sid, ir), _ds(fut, 375.0),
                          1_000_000_000.0, decisions, set(), market="KRX",
                          entry_window="close", instrument_class="stock")
    assert broker.submitted == [], "주식 종가창이 선물 종가매수를 진입하면 안 됨(클래스 필터)"

    # 선물 종가창(instrument_class='futures') — 진입.
    t._enter_from_preview(_cands(sid, fut), _strats(sid, ir), _ds(fut, 375.0),
                          1_000_000_000.0, decisions, set(), market="KRX",
                          entry_window="close", instrument_class="futures")
    assert len(broker.submitted) == 1, decisions
    assert broker.submitted[-1]["side"] == "buy"


# ── enter_close_candidates 래퍼(게이트 + 병합) ─────────────────────────────

def test_enter_close_candidates_e2e(isolated_trader):
    """enter_close_candidates: fill=close 진입 발주 → WS 체결 → 원장 적재. kind=day_trade_close."""
    t, broker = isolated_trader
    broker._prices["005930"] = 100000.0
    sid = "closefill"
    ir = _ir_def("close")
    payload = t.enter_close_candidates(
        _cands(sid), _strats(sid, ir), _ds(prev_close=50000.0), {},
        market="KRX", instrument_class="stock")
    assert len(broker.submitted) == 1, payload["decisions"]
    order = broker.submitted[-1]
    assert order["side"] == "buy" and order["qty"] == 10
    # 종가 슬롯 매칭용 kind(서버 trading.py 종가 슬롯 = day_trade_close) — 별도 슬롯/서버변경 없음.
    assert payload["cycle_summary"]["kind"] == "day_trade_close"

    # 체결 → 원장 적재(익일 시가매도는 다음날 아침 청산 패스가 처리).
    scenario.inject_ws_fill(t, broker, order["order_no"], 10, 100000.0)
    assert t.ledger[f"{sid}:005930"]["qty"] == 10
    invariants.check_all(t)


def test_close_window_skips_degenerate_day_trade_close(isolated_trader):
    """degenerate: fill=close + hold_days=0(당일청산)은 종가창에 진입 안 함(오버나이트 방치 방지)."""
    t, broker = isolated_trader
    broker._prices["005930"] = 100000.0
    sid = "closeday"
    ir = _ir_def("close")
    ir["position"]["exit"]["hold_days"] = 0          # 종가매수인데 당일청산 — 청산 창 없음
    decisions: list[dict] = []
    t._enter_from_preview(_cands(sid), _strats(sid, ir), _ds(), 10_000_000.0,
                          decisions, set(), market="KRX",
                          entry_window="close", instrument_class="stock")
    assert broker.submitted == [], "fill=close+hold_days=0은 종가창 진입되면 안 됨(degenerate)"
    assert any(d["action"] == "skip_unsupported" for d in decisions), decisions


def test_close_window_daytrade_exit_before_overnight_entry(isolated_trader):
    """유저 시나리오: 같은 종목(코스피200선물)에 당일매매 청산(S&P500 역추종)과 오버나이트 롱
    진입이 한 종가창에 겹칠 때 — ①청산(환매) 발주 → ②진입(매수) 발주 순서 + 무충돌 검증.
    run_close_cycle의 ①liquidate_day_trades → ②enter_close_candidates 순서를 재현한다."""
    t, broker = isolated_trader
    fut = "코스피200선물"
    broker._prices[fut] = 375.0
    broker._balance["cash"] = 1_000_000_000
    # A: S&P500 역추종 당일매매(hold_days=0) 숏 3 — 종가에 청산될 대상(원장+브로커 실보유).
    defA = _ir_def("next_open", symbol=fut,
                   sizing={"mode": "pct_cash", "futures_margin_pct": 20})
    defA["position"]["exit"]["hold_days"] = 0
    t.ledger["A_daytrade"] = {
        "symbol": fut, "qty": 3, "entry_date": "2026-06-01", "entry_price": 375.0,
        "peak_price": 375.0, "side": "short", "strategy_name": "S&P500 역추종",
        "definition": defA}
    broker.set_positions([{"symbol": fut, "qty": 3, "side": "sell"}])   # 숏 3 실보유

    # ① 당일매매 청산 — A 숏 3 환매(buy-to-close).
    n0 = len(broker.submitted)
    t.liquidate_day_trades(_ds(fut, 375.0), "futures", market="KRX")
    assert len(broker.submitted) == n0 + 1, broker.submitted
    a_close = broker.submitted[-1]
    assert a_close["side"] == "buy" and a_close["symbol"] == fut, "A 숏 청산=환매(buy)"

    # ② 오버나이트 롱 진입 — B 롱 매수.
    sid = "B_overnight"
    irB = _ir_def("close", symbol=fut,
                  sizing={"mode": "pct_cash", "futures_margin_pct": 20})
    t.enter_close_candidates(_cands(sid, fut), _strats(sid, irB), _ds(fut, 375.0), {},
                             market="KRX", instrument_class="futures")
    assert len(broker.submitted) == n0 + 2, broker.submitted
    b_open = broker.submitted[-1]
    assert b_open["side"] == "buy" and b_open["symbol"] == fut, "B 오버나이트 롱=매수"
    # 순서: 청산(환매) 발주가 진입(매수) 발주보다 먼저.
    assert broker.submitted.index(a_close) < broker.submitted.index(b_open)


def test_enter_close_candidates_blocked_by_killswitch(isolated_trader):
    """킬스위치 활성 시 종가 진입 차단(신규 진입만 — 아침 진입 §3과 동일)."""
    t, broker = isolated_trader
    from localapp import killswitch
    killswitch.activate("일일 손실 한도")
    broker._prices["005930"] = 100000.0
    sid = "closefill"
    ir = _ir_def("close")
    payload = t.enter_close_candidates(
        _cands(sid), _strats(sid, ir), _ds(), {},
        market="KRX", instrument_class="stock")
    assert broker.submitted == [], "킬스위치 활성 시 종가 진입 금지"
    assert any(d["action"] == "skip_killswitch" for d in payload["decisions"])

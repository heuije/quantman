"""θ — 종가청산 사이클 체결-기록 신뢰성 (2026-06-12 라이브 결함 재현).

라이브 증거: 코스피200 선물 종가청산 0000004525가 15:40 발주 → 종가 단일가(~15:45)
체결됐는데, liquidate_day_trades가 발주 직후 단일 _resolve_pending만 수행해 지연
체결을 놓쳤고(θ), 정산 cron(15:35)이 청산(15:40)보다 먼저 돌아 그날 안에 다시
확인할 기회도 없었다 → 18:54 수동 "지금 실행"으로야 복구(+24.9M 미기록 위험).

이 파일은 그 부류를 닫는 3개 축을 검증한다:
  θ  — 종가청산도 일반 cycle과 동일한 _wait_pending으로 지연 체결을 흡수.
  N1 — ledger 블라인드 방어: 사이클 시작에 _resolve_pending을 먼저 돌려 미기록
       진입 체결(δ류)을 ledger에 복원한 뒤 순회(US GOOG 261주 방치 사고 부류).
       체결확인이 끝내 불능이면 추측 발주 없이 명시 표면화(외부 보유 오인 매도
       금지 — 병1 불변식).
  N2 — cycle_summary가 market/instrument_class/n_pending_unresolved를 노출해
       서버 타임라인이 "발주-but-미확인"을 녹색 성공으로 표시하지 않게 한다.

    cd platform/local && python -m pytest tests/scenarios/test_theta_close_reliability.py -q
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from sim import invariants, scenario

KST = ZoneInfo("Asia/Seoul")

_DUMMY_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
               "right": {"op": "const", "params": {"value": 0}}},
}


def _ir_def(symbol, hold_days=0):
    return {
        "name": "당일매매", "engine": "ir",
        "universe": {"kind": "single", "symbols": [symbol]},
        "signal": _DUMMY_SIGNAL,
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 10},
                     "entry": {"mode": "on_signal"},
                     "exit": {"hold_days": hold_days}, "overlays": {}},
        "simulation": {},
    }


def _dataset(symbol, px=70000.0, n=8):
    idx = pd.date_range("2026-05-01", periods=n, freq="B")
    closes = [px] * n
    return {symbol: pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)}


def _enter_day_trade(t, broker, symbol, sid, fill=70000.0):
    """test_day_trade_close와 동일 — 진입 → 원장 + SimBroker 실보유 반영."""
    broker._prices[symbol] = fill
    ds = _dataset(symbol, fill)
    order_no, decisions = scenario.strategy_buy_and_fill(
        t, broker, sid, _ir_def(symbol), symbol, ds, fill_price=fill,
        equity=10_000_000.0)
    assert order_no is not None, decisions
    qty = t.ledger[sid]["qty"]
    broker.set_positions([{"symbol": symbol, "qty": qty}])
    return ds, qty


def _make_entry_pending(t, order_no, sid, symbol, qty, *, side="buy",
                        submitted_ts=None):
    """_after_submit이 등록하는 형태의 당일매매 진입 pending을 직접 구성.

    submitted_ts 기본값은 conftest의 고정 KST 오늘(2026-06-01) 아침 — N1의
    '오늘 제출분' 판정이 실제 시계가 아닌 거래일 기준으로 동작해야 한다.
    """
    if submitted_ts is None:
        submitted_ts = datetime(2026, 6, 1, 8, 56, tzinfo=KST).timestamp()
    t.pending[order_no] = {
        "order_no": order_no, "strategy_id": sid, "strategy_name": "당일매매",
        "symbol": symbol, "side": side, "qty": qty, "limit_price": 0,
        "intended_price": 70000.0,
        "submitted_ts": submitted_ts,
        "definition": _ir_def(symbol), "reason": "매수신호", "filled_so_far": 0,
    }


# ── θ: 지연 체결 흡수 ───────────────────────────────────────────────────────

def test_close_cycle_records_delayed_fill(isolated_trader, monkeypatch):
    """종가청산 발주 → 첫 상태조회는 미체결 → 잠시 후 체결.

    재현(라이브): 모의 체결 ~27초 지연·실전 단일가 15:45 체결을 발주 직후 단일
    조회가 놓쳐 체결이 미기록됐다. 종가청산도 _wait_pending으로 폴링해야 한다.
    """
    t, broker = isolated_trader
    from localapp import trader as tr
    sid = "dt_theta"
    ds, qty = _enter_day_trade(t, broker, "005930", sid)

    # 매도 주문 상태: 1번째 조회=unknown(지연), 2번째부터=filled — 단일 조회는 놓침
    calls: dict[str, int] = {}
    real_status = broker.order_status

    def delayed_status(order_no, symbol=None, hint=None):
        n = calls.get(order_no, 0) + 1
        calls[order_no] = n
        sub = next((s for s in broker.submitted if s["order_no"] == order_no), None)
        if sub is not None and sub["side"] == "sell" and n >= 2:
            return {"order_no": order_no, "status": "filled",
                    "filled_qty": sub["qty"], "remain_qty": 0, "fill_price": 70000.0}
        return real_status(order_no, symbol)

    monkeypatch.setattr(broker, "order_status", delayed_status)
    # 짧은 wait로 폴링 동작만 검증 (운영 기본 60s/20s)
    real_merged = tr.merged_execution
    monkeypatch.setattr(tr, "merged_execution", lambda o=None: {
        **real_merged(o), "post_submit_wait_sec": 2, "poll_interval_sec": 0.01})

    payload = t.liquidate_day_trades(ds, "stock", market="KRX")

    assert sid not in t.ledger, "지연 체결이 기록되지 않음 — 종가청산이 폴링 없이 단일 조회(θ)"
    assert any(d["action"] == "sold" for d in payload["decisions"]), payload["decisions"]
    assert payload["cycle_summary"]["n_pending_unresolved"] == 0
    invariants.check_all(t)


# ── N1: ledger 블라인드 방어 ────────────────────────────────────────────────

def test_close_cycle_restores_unrecorded_entry_then_closes(isolated_trader):
    """진입 체결이 ledger에 미기록(δ류)이지만 지금은 상태조회 가능 — 종가청산이
    시작 시점에 resolve로 ledger를 복원한 뒤 그 포지션을 청산해야 한다.

    재현(라이브): GOOG 261주 — 예약매수 체결이 미기록 → ledger 블라인드 →
    종가매도 사이클이 아무것도 안 팔고 주말 방치(N1 cascade).
    """
    t, broker = isolated_trader
    sid, symbol, qty = "dt_unrecorded", "005930", 10
    _make_entry_pending(t, "B001", sid, symbol, qty)
    # 진입 주문은 사실 체결돼 있었다(δ 해소 후 조회 가능) + 계좌 실보유 존재
    broker._statuses["B001"] = {"order_no": "B001", "status": "filled",
                                "filled_qty": qty, "remain_qty": 0,
                                "fill_price": 70000.0}
    broker._prices[symbol] = 70000.0
    broker.set_positions([{"symbol": symbol, "qty": qty}])

    t.liquidate_day_trades(_dataset(symbol), "stock", market="KRX")

    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["qty"] == qty, \
        f"미기록 진입이 복원·청산되지 않음(N1): submitted={broker.submitted}"


def test_close_cycle_surfaces_blind_unrecorded_entry(isolated_trader, monkeypatch):
    """체결확인이 끝내 불능(δ 잔존)인데 계좌 보유 > 원장 — 추측 발주는 금지하되
    '당일청산 불능'을 명시 표면화해야 한다(침묵 방치 금지·외부 보유 오인 매도 금지).

    이 테스트만 실제 오늘 날짜 사용: WS-1의 pending GC(7일)가 실제 시계(time.time)
    기준이라, conftest 고정일(2026-06-01)로는 '오늘 제출' pending을 만들 수 없다.
    """
    import time as _time

    from localapp import trader as tr
    t, broker = isolated_trader
    real_today = datetime.now(KST).date()
    monkeypatch.setattr(tr, "kst_today", lambda: real_today)
    sid, symbol, qty = "dt_blind", "005930", 10
    _make_entry_pending(t, "B002", sid, symbol, qty, submitted_ts=_time.time())
    # 상태조회는 계속 unknown(broker._statuses 미설정), 계좌엔 보유 존재
    broker._prices[symbol] = 70000.0
    broker.set_positions([{"symbol": symbol, "qty": qty}])

    payload = t.liquidate_day_trades(_dataset(symbol), "stock", market="KRX")

    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert sells == [], "체결확인 불능 상태에서 추측 매도 발주 금지(병1 불변식)"
    errs = [d for d in payload["decisions"] if d["action"] == "error"]
    assert any("당일청산 불능" in (d.get("reason") or d.get("detail") or "")
               for d in errs), f"blind 방치가 표면화되지 않음: {payload['decisions']}"
    assert payload["cycle_summary"]["n_pending_unresolved"] == 1


# ── N2: cycle_summary 관측 필드 ────────────────────────────────────────────

def test_close_summary_identifies_market_and_class(isolated_trader):
    """day_trade_close 요약은 market 'ALL' 하드코딩이 아니라 실제 시장·클래스를
    식별해야 서버 타임라인이 슬롯별(주식 15:25/선물 15:40)로 매칭할 수 있다."""
    t, broker = isolated_trader
    payload = t.liquidate_day_trades(_dataset("005930"), "stock", market="KRX")
    cs = payload["cycle_summary"]
    assert cs["kind"] == "day_trade_close"
    assert cs["market"] == "KRX", f"market이 'ALL' 하드코딩: {cs}"
    assert cs["instrument_class"] == "stock"
    assert cs["n_pending_unresolved"] == 0

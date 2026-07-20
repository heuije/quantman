"""R3·R4 — 종가·정산 견고성 + 진입 미달 관측.

R4: 종가 재시도 하네스(하드컷)·정산 당일 재시도(no-op 판정)·catchup 종가 항목.
R3: entry_shortfall 관측(슬롯 미충족 + 실패 시도 존재 시에만).
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from localapp import catchup, runner
from localapp.trader import Trader

_KST = ZoneInfo("Asia/Seoul")


# ── R4-① 하드컷 ─────────────────────────────────────────────────────────────


def test_close_hard_cutoff_krx():
    c_stock = runner._close_hard_cutoff_kst("KRX", "stock")
    c_fut = runner._close_hard_cutoff_kst("KRX", "futures")
    assert (c_stock.hour, c_stock.minute) == (15, 30)
    assert (c_fut.hour, c_fut.minute) == (15, 45)


def test_close_hard_cutoff_us_uses_session_close(monkeypatch):
    from quant_core import market_calendar as mc
    close = datetime(2026, 7, 18, 5, 0, tzinfo=_KST)
    monkeypatch.setattr(mc, "session_kst", lambda m, d: (None, close))
    assert runner._close_hard_cutoff_kst("US", "stock") == close


# ── R4-① 재시도 하네스 ──────────────────────────────────────────────────────


@pytest.fixture
def _close_env(monkeypatch):
    from quant_core import market_calendar as mc
    monkeypatch.setattr(runner, "setup_logging", lambda: None)
    monkeypatch.setattr(runner, "_flush_pending", lambda: None)
    monkeypatch.setattr(mc, "is_session_day", lambda m, d: True)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    pushed = []
    monkeypatch.setattr(runner, "push_snapshot", lambda p: pushed.append(p))
    return pushed


def test_close_cycle_retries_then_succeeds(_close_env, monkeypatch):
    calls = {"n": 0}

    def once(market, cls):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("blip")
        return {"ok": True}

    monkeypatch.setattr(runner, "_close_cycle_once", once)
    monkeypatch.setattr(runner, "_close_hard_cutoff_kst",
                        lambda m, c: datetime.now(_KST) + timedelta(minutes=10))
    out = runner.run_close_cycle("KRX", "stock")
    assert out == {"ok": True} and calls["n"] == 3


def test_close_cycle_hard_cutoff_stops_and_surfaces(_close_env, monkeypatch):
    from localapp import order_log

    def once(market, cls):
        raise RuntimeError("down")

    monkeypatch.setattr(runner, "_close_cycle_once", once)
    # 컷이 이미 지남 — 1회 시도 후 즉시 종료(마감 뒤 발주 금지)
    monkeypatch.setattr(runner, "_close_hard_cutoff_kst",
                        lambda m, c: datetime.now(_KST) - timedelta(seconds=1))
    out = runner.run_close_cycle("KRX", "futures")
    assert out["status"] == "error"
    # 에러 스냅샷 push + cycles.jsonl 에러 기록(catchup·건강 C6 소비)
    pushed = _close_env
    assert pushed and "종가 사이클 전 시도 실패" in pushed[-1]["cycle_summary"]["error"]
    rows = [json.loads(x) for x in
            order_log.CYCLES_PATH.read_text(encoding="utf-8").splitlines() if x.strip()]
    errs = [r for r in rows if (r.get("summary") or {}).get("kind") == "day_trade_close"
            and (r.get("summary") or {}).get("error")]
    assert errs


# ── R4-② 정산 재시도 ────────────────────────────────────────────────────────


def test_settlement_retry_noop_when_done(monkeypatch):
    from localapp import order_log
    today = datetime.now(_KST).date().isoformat()
    monkeypatch.setattr(order_log, "read_cycles", lambda n=20: [
        {"ts": f"{today}T15:50:01+09:00",
         "summary": {"kind": "post_close_settlement", "market": "KRX"}}])
    called = []
    monkeypatch.setattr(runner, "run_post_close_settlement",
                        lambda market="KRX": called.append(market))
    assert runner.run_settlement_retry("KRX") is None
    assert called == []


def test_settlement_retry_runs_when_missing_or_error(monkeypatch):
    from localapp import order_log
    today = datetime.now(_KST).date().isoformat()
    monkeypatch.setattr(order_log, "read_cycles", lambda n=20: [
        {"ts": f"{today}T15:50:01+09:00",
         "summary": {"kind": "post_close_settlement", "market": "KRX",
                      "error": "잔고 조회 실패"}}])
    called = []
    monkeypatch.setattr(runner, "run_post_close_settlement",
                        lambda market="KRX": called.append(market) or {"ok": 1})
    assert runner.run_settlement_retry("KRX") == {"ok": 1}
    assert called == ["KRX"]


# ── R4-③ catchup 종가 항목 ──────────────────────────────────────────────────


def _plan_at(monkeypatch, now, entries):
    from quant_core import market_calendar as mc
    monkeypatch.setattr(catchup, "_read_recent_cycles", lambda: entries)
    monkeypatch.setattr(mc, "is_session_day", lambda m, d: True)
    return catchup._decide_catchup_plan(now=now)


def test_catchup_close_window_stock_missing(monkeypatch):
    now = datetime(2026, 7, 17, 15, 26, tzinfo=_KST)   # 금요일 주식 종가창
    plan = _plan_at(monkeypatch, now, [])
    assert "stock" in plan.krx_close_classes
    assert "futures" not in plan.krx_close_classes      # 창 밖(15:35~)


def test_catchup_close_window_done_no_action(monkeypatch):
    now = datetime(2026, 7, 17, 15, 26, tzinfo=_KST)
    done_entry = {"ts": "2026-07-17T15:25:30+09:00",
                  "summary": {"kind": "day_trade_close", "market": "KRX",
                               "instrument_class": "stock"}}
    plan = _plan_at(monkeypatch, now, [done_entry])
    assert plan.krx_close_classes == []


def test_catchup_close_outside_window_no_action(monkeypatch):
    now = datetime(2026, 7, 17, 16, 10, tzinfo=_KST)    # 창 밖(마감 후)
    plan = _plan_at(monkeypatch, now, [])
    assert plan.krx_close_classes == []


# ── R3 — entry_shortfall 관측 ───────────────────────────────────────────────


def _mk_trader(monkeypatch, try_results):
    class _B:
        pass

    t = Trader(_B())
    seq = iter(try_results)
    monkeypatch.setattr(t, "_try_buy_one_symbol",
                        lambda *a, **k: next(seq))
    from localapp import account_handle, coverage
    monkeypatch.setattr(account_handle, "active_account_ids", lambda: set())
    monkeypatch.setattr(coverage, "missing_categories", lambda syms: set())
    return t


def _entry_args():
    strat = {"id": "7", "name": "티", "account_ref": None,
             "definition": {"universe": {"kind": "list"},
                             "simulation": {"fill": "next_open"}}}
    by_strategy = [{"strategy_id": "7",
                    "candidates": [{"symbol": "005930"}, {"symbol": "000660"}]}]
    return by_strategy, [strat]


def test_entry_shortfall_emitted_on_partial(monkeypatch):
    t = _mk_trader(monkeypatch, [True, False])          # 슬롯2 중 1 확정
    by, strats = _entry_args()
    decisions: list = []
    t._enter_from_preview(by, strats, {}, 1_000_000, decisions, set(),
                          market="KRX", entry_window="open")
    sf = [d for d in decisions if d["action"] == "entry_shortfall"]
    assert len(sf) == 1 and "슬롯 2 중 확정 1" in sf[0]["reason"]


def test_entry_shortfall_silent_when_slots_met(monkeypatch):
    t = _mk_trader(monkeypatch, [True, True])
    by, strats = _entry_args()
    decisions: list = []
    t._enter_from_preview(by, strats, {}, 1_000_000, decisions, set(),
                          market="KRX", entry_window="open")
    assert not [d for d in decisions if d["action"] == "entry_shortfall"]


# ── N4 전략 pull 재시도 · N5 재시도의 하드컷 인지 ────────────────────────────
# N4: 종가 경로만 `pull_strategies`를 **1회 시도 후 except로 흡수**해, 일시 502 하나로
#     그날 종가 진입이 통째 소실되고 그 사실이 어디에도 남지 않았다(아침 경로는 backoff
#     루프 안에서 재시도하고 cycle_summary에 표면화한다 — 종가만 누락).
# N5: 재시도 sleep이 하드컷(선물 15:45 마감 단일가)을 보지 않는다. 동시호가 가드가
#     창 끝(15:44:30)에 재실행하면 45초 대기가 단일가를 넘길 수 있다.

def _cutoff_in(seconds: float):
    return lambda m, c: datetime.now(_KST) + timedelta(seconds=seconds)


def test_strategies_pull_retried_then_succeeds(monkeypatch):
    """일시 실패 후 재시도로 회복 — 종가 진입이 살아난다."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("502")
        return [{"id": "s1"}]

    monkeypatch.setattr(runner, "pull_strategies", flaky)
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    strategies, failed = runner._pull_strategies_with_retry(
        "[종가] ", deadline=datetime.now(_KST) + timedelta(minutes=10))
    assert strategies == [{"id": "s1"}] and failed is False and calls["n"] == 3


def test_strategies_pull_exhausted_is_failsoft_and_surfaced(monkeypatch):
    """전부 실패해도 **청산은 진행**(빈 목록 반환)하되 실패 사실을 반환한다.

    fail-soft를 깨면 안 된다 — 진입 재료가 없다고 청산까지 막으면 오버나이트다.
    """
    monkeypatch.setattr(runner, "pull_strategies",
                        lambda: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(runner.time, "sleep", lambda s: None)
    strategies, failed = runner._pull_strategies_with_retry(
        "[종가] ", deadline=datetime.now(_KST) + timedelta(minutes=10))
    assert strategies == [] and failed is True


def test_strategies_retry_stops_before_hard_cutoff(monkeypatch):
    """N5 — 컷오프를 넘길 대기는 하지 않는다(마감 단일가 이후 발주 금지)."""
    slept: list = []
    monkeypatch.setattr(runner, "pull_strategies",
                        lambda: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))
    # 컷까지 5초 — 첫 시도(즉시)만 가능하고 +15s 대기는 컷을 넘는다.
    strategies, failed = runner._pull_strategies_with_retry(
        "[종가] ", deadline=datetime.now(_KST) + timedelta(seconds=5))
    assert slept == [], f"컷오프를 넘기는 대기를 했다: {slept}"
    assert failed is True


def test_preview_retry_stops_before_hard_cutoff(monkeypatch):
    """N5 — preview 재시도도 동일. 종전엔 sleep 앞에 컷 검사가 없어 45초를 다 썼다."""
    from localapp.sync_client import PreviewUnavailable
    slept: list = []
    monkeypatch.setattr(runner, "pull_preview",
                        lambda: (_ for _ in ()).throw(PreviewUnavailable("503")))
    monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))
    out = runner._pull_preview_with_retry(
        "[종가] ", deadline=datetime.now(_KST) + timedelta(seconds=5))
    assert out is None and slept == [], f"컷오프를 넘기는 대기를 했다: {slept}"


def test_preview_retry_without_deadline_keeps_legacy_behavior(monkeypatch):
    """deadline 미지정(아침 경로)은 종전 그대로 3회 시도 — 거동 변경 없음."""
    from localapp.sync_client import PreviewUnavailable
    slept: list = []
    monkeypatch.setattr(runner, "pull_preview",
                        lambda: (_ for _ in ()).throw(PreviewUnavailable("503")))
    monkeypatch.setattr(runner.time, "sleep", lambda s: slept.append(s))
    assert runner._pull_preview_with_retry("") is None
    assert slept == [15, 30]


def test_close_cycle_surfaces_strategies_pull_failure(_close_env, monkeypatch):
    """N4 — 소진 시 cycle_summary에 표면화(아침 경로와 동일 계약).

    종전엔 WARNING 로그가 유일한 흔적이라 "왜 종가 진입이 0건인가"를 원격에서
    알 수 없었고, 사이클은 정상 종료로 기록됐다.
    """
    monkeypatch.setattr(runner, "make_broker", lambda: object())
    monkeypatch.setattr(runner, "pull_strategies",
                        lambda: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(runner, "_close_hard_cutoff_kst", _cutoff_in(600))

    captured: dict = {}

    class _FakeTrader:
        ledger: dict = {}

        def __init__(self, b):
            pass

        def run_close_netting(self, *a, **kw):
            return {"cycle_summary": {"kind": "day_trade_close"}}

    monkeypatch.setattr(runner, "Trader", _FakeTrader)
    monkeypatch.setattr(runner.qc, "load_dataset_for", lambda *a, **k: {})
    out = runner._close_cycle_once("KRX", "futures")
    captured.update(out)
    assert captured["cycle_summary"].get("strategies_pull_failed") is True

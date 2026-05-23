"""Q5 회귀 — 장중 kill switch 평가 (Tier 1 체결 후, Tier 2 60초 monitor).

설계 합의(AL-2): monitor 주기 60초, 단위 계정 단위(통합 자본), 발동 시 미체결
cancel + 빈 cycle 재호출. AL-4: trader._CYCLE_LOCK으로 cycle ↔ settlement ↔
ks trigger 직렬화.

테스트 시나리오:
1. evaluate_killswitch_now: 자본 -3% 도달 시 activate, 미도달 시 무동작
2. cancel_all_pending: pending 전체 cancel 시도
3. monitor loop: 60초 주기 평가 → 발동 시 on_trigger 호출
4. _apply_fill Tier 1: 체결 후 즉시 평가 + ks_trigger_hook 호출
5. _CYCLE_LOCK 직렬화: 동시 진입 시 직렬화 (단순 acquire/release 확인)
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))


# ── 격리된 killswitch 파일 (tmp_path 사용) ────────────────────────────────────


@pytest.fixture
def isolated_ks(tmp_path, monkeypatch):
    """killswitch.KILLSWITCH_PATH를 tmp로 격리. day_start_equity 미리 설정."""
    from localapp import killswitch
    p = tmp_path / "ks.json"
    monkeypatch.setattr(killswitch, "KILLSWITCH_PATH", p)
    # day_start_equity = 10,000,000원
    killswitch.update_day_start(10_000_000.0, "2026-05-23")
    yield killswitch


def _mock_broker(balance: dict) -> MagicMock:
    b = MagicMock()
    b.account_snapshot.return_value = {"balance": balance, "positions": []}
    return b


# ── Tier 1: evaluate_killswitch_now ──────────────────────────────────────────


def test_evaluate_below_threshold_no_trigger(isolated_ks, monkeypatch, tmp_path):
    """자본 -2% (한도 -3% 미도달) → 발동 안 함."""
    from localapp.trader import Trader
    # ledger·equity·pending 격리
    from localapp import config as cfg
    monkeypatch.setattr(cfg, "LEDGER_PATH", tmp_path / "l.json")
    monkeypatch.setattr(cfg, "EQUITY_PATH", tmp_path / "e.json")
    monkeypatch.setattr(cfg, "PENDING_ORDERS_PATH", tmp_path / "p.json")
    monkeypatch.setattr(cfg, "REBALANCE_PATH", tmp_path / "r.json")

    broker = _mock_broker({"total_eval": 9_800_000, "foreign_eval_krw": 0,
                            "cash_usd": 0, "fx_usdkrw": 0})
    trader = Trader(broker)
    fired = trader.evaluate_killswitch_now(3.0, decisions=[])
    assert fired is False
    assert isolated_ks.is_active() is False


def test_evaluate_above_threshold_triggers(isolated_ks, monkeypatch, tmp_path):
    """자본 -4% 도달 → 발동."""
    from localapp.trader import Trader
    from localapp import config as cfg
    monkeypatch.setattr(cfg, "LEDGER_PATH", tmp_path / "l.json")
    monkeypatch.setattr(cfg, "EQUITY_PATH", tmp_path / "e.json")
    monkeypatch.setattr(cfg, "PENDING_ORDERS_PATH", tmp_path / "p.json")
    monkeypatch.setattr(cfg, "REBALANCE_PATH", tmp_path / "r.json")

    broker = _mock_broker({"total_eval": 9_600_000, "foreign_eval_krw": 0,
                            "cash_usd": 0, "fx_usdkrw": 0})
    trader = Trader(broker)
    decisions = []
    fired = trader.evaluate_killswitch_now(3.0, decisions=decisions)
    assert fired is True
    assert isolated_ks.is_active() is True
    # decision 로그에 기록
    assert any(d.get("action") == "kill_switch" for d in decisions)


def test_evaluate_already_active_no_double_trigger(isolated_ks, monkeypatch, tmp_path):
    """이미 active면 재발동 안 함 (중복 trigger 방지)."""
    from localapp.trader import Trader
    from localapp import config as cfg
    monkeypatch.setattr(cfg, "LEDGER_PATH", tmp_path / "l.json")
    monkeypatch.setattr(cfg, "EQUITY_PATH", tmp_path / "e.json")
    monkeypatch.setattr(cfg, "PENDING_ORDERS_PATH", tmp_path / "p.json")
    monkeypatch.setattr(cfg, "REBALANCE_PATH", tmp_path / "r.json")

    isolated_ks.activate("기존 발동")
    broker = _mock_broker({"total_eval": 9_500_000, "foreign_eval_krw": 0,
                            "cash_usd": 0, "fx_usdkrw": 0})
    trader = Trader(broker)
    fired = trader.evaluate_killswitch_now(3.0, decisions=[])
    assert fired is False  # 이미 active라 새로 발동 X


# ── cancel_all_pending ─────────────────────────────────────────────────────


def test_cancel_all_pending_calls_broker_cancel(monkeypatch, tmp_path):
    """pending 2건 → broker.cancel 2회 호출 + decisions에 기록."""
    from localapp.trader import Trader
    from localapp import config as cfg
    monkeypatch.setattr(cfg, "LEDGER_PATH", tmp_path / "l.json")
    monkeypatch.setattr(cfg, "EQUITY_PATH", tmp_path / "e.json")
    monkeypatch.setattr(cfg, "PENDING_ORDERS_PATH", tmp_path / "p.json")
    monkeypatch.setattr(cfg, "REBALANCE_PATH", tmp_path / "r.json")

    broker = MagicMock()
    trader = Trader(broker)
    trader.pending = {
        "ORD1": {"symbol": "005930", "qty": 10, "strategy_id": "s1",
                 "strategy_name": "T1"},
        "ORD2": {"symbol": "000660", "qty": 5, "strategy_id": "s2",
                 "strategy_name": "T2"},
    }
    decisions = []
    n = trader.cancel_all_pending(decisions=decisions)
    assert n == 2
    assert broker.cancel.call_count == 2
    assert sum(1 for d in decisions if d.get("action") == "cancelled") == 2


def test_cancel_all_pending_continues_on_failure(monkeypatch, tmp_path):
    """일부 cancel 실패해도 나머지 시도 — 자금 노출 최소화."""
    from localapp.trader import Trader
    from localapp import config as cfg
    monkeypatch.setattr(cfg, "LEDGER_PATH", tmp_path / "l.json")
    monkeypatch.setattr(cfg, "EQUITY_PATH", tmp_path / "e.json")
    monkeypatch.setattr(cfg, "PENDING_ORDERS_PATH", tmp_path / "p.json")
    monkeypatch.setattr(cfg, "REBALANCE_PATH", tmp_path / "r.json")

    broker = MagicMock()
    broker.cancel.side_effect = [Exception("KIS down"), None]
    trader = Trader(broker)
    trader.pending = {
        "ORD1": {"symbol": "005930", "qty": 10, "strategy_id": "s1",
                 "strategy_name": "T1"},
        "ORD2": {"symbol": "000660", "qty": 5, "strategy_id": "s2",
                 "strategy_name": "T2"},
    }
    # 예외가 두 번째 호출까지 전파되지 않아야 함
    n = trader.cancel_all_pending(decisions=[])
    assert n == 1  # 1건만 성공
    assert broker.cancel.call_count == 2


# ── Tier 2: monitor loop ──────────────────────────────────────────────────────


def test_monitor_evaluate_once_fires_on_trigger(isolated_ks):
    """monitor 1회 평가 — 자본 -4% → on_trigger 콜백 호출."""
    from localapp.intraday_stop import IntradayStopManager

    broker = _mock_broker({"total_eval": 9_600_000, "foreign_eval_krw": 0,
                            "cash_usd": 0, "fx_usdkrw": 0})
    mgr = IntradayStopManager(
        broker=broker, get_ledger=lambda: {},
        get_strat_def=lambda sid: None,
        submit_sell_fn=MagicMock())
    triggered = []
    fired = mgr._ks_evaluate_once(3.0, lambda: triggered.append("fired"))
    assert fired is True
    assert triggered == ["fired"]
    assert isolated_ks.is_active() is True


def test_monitor_skips_when_already_active(isolated_ks):
    """이미 active면 평가 skip — on_trigger 미호출."""
    from localapp.intraday_stop import IntradayStopManager

    isolated_ks.activate("이전 발동")
    broker = _mock_broker({"total_eval": 9_000_000, "foreign_eval_krw": 0,
                            "cash_usd": 0, "fx_usdkrw": 0})
    mgr = IntradayStopManager(
        broker=broker, get_ledger=lambda: {},
        get_strat_def=lambda sid: None,
        submit_sell_fn=MagicMock())
    triggered = []
    fired = mgr._ks_evaluate_once(3.0, lambda: triggered.append("fired"))
    assert fired is False
    assert triggered == []


def test_monitor_start_stop_lifecycle(isolated_ks):
    """start_monitor → 짧은 주기로 1회 평가 → stop_monitor → thread 종료."""
    from localapp.intraday_stop import IntradayStopManager

    broker = _mock_broker({"total_eval": 9_600_000, "foreign_eval_krw": 0,
                            "cash_usd": 0, "fx_usdkrw": 0})
    mgr = IntradayStopManager(
        broker=broker, get_ledger=lambda: {},
        get_strat_def=lambda sid: None,
        submit_sell_fn=MagicMock())
    triggered = []
    mgr.start_monitor(3.0, lambda: triggered.append("fired"), period_sec=0.2)
    # 첫 wait(period_sec)는 0.2초 후 평가 → 발동 → on_trigger 호출
    time.sleep(0.5)
    mgr.stop_monitor()
    assert len(triggered) >= 1
    assert mgr._ks_monitor_thread is None


# ── Tier 1: _apply_fill 후 trigger hook ──────────────────────────────────────


def test_apply_fill_triggers_ks_hook(isolated_ks, monkeypatch, tmp_path):
    """체결 후 자본 -4% 도달 → _ks_trigger_hook 호출."""
    from localapp.trader import Trader
    from localapp import config as cfg
    monkeypatch.setattr(cfg, "LEDGER_PATH", tmp_path / "l.json")
    monkeypatch.setattr(cfg, "EQUITY_PATH", tmp_path / "e.json")
    monkeypatch.setattr(cfg, "PENDING_ORDERS_PATH", tmp_path / "p.json")
    monkeypatch.setattr(cfg, "REBALANCE_PATH", tmp_path / "r.json")
    monkeypatch.setattr(cfg, "TRADES_PATH", tmp_path / "t.jsonl")

    broker = _mock_broker({"total_eval": 9_600_000, "foreign_eval_krw": 0,
                            "cash_usd": 0, "fx_usdkrw": 0})
    trader = Trader(broker)
    trader._daily_loss_limit_pct = 3.0
    triggered = []
    trader._ks_trigger_hook = lambda src: triggered.append(src)

    # 매수 체결 시뮬레이션
    p = {"strategy_id": "s1", "symbol": "005930", "side": "buy",
         "qty": 10, "strategy_name": "T1", "definition": {}}
    decisions = []
    trader._apply_fill("ORD1", p, 10, 60000.0, decisions, partial=False)

    assert "apply_fill" in triggered
    assert isolated_ks.is_active() is True


def test_apply_fill_skips_trigger_when_in_cycle(isolated_ks, monkeypatch, tmp_path):
    """데드락 방지 회귀 — cycle 내부 _apply_fill은 hook 호출 skip.

    같은 thread가 cycle lock을 쥔 채 _apply_fill에서 hook → cycle 재호출하면
    _CYCLE_LOCK 데드락 + 무한 재귀. _in_cycle=True면 ks 평가/hook 자체를 skip
    하고 cycle 본체가 책임진다.
    """
    from localapp.trader import Trader
    from localapp import config as cfg
    monkeypatch.setattr(cfg, "LEDGER_PATH", tmp_path / "l.json")
    monkeypatch.setattr(cfg, "EQUITY_PATH", tmp_path / "e.json")
    monkeypatch.setattr(cfg, "PENDING_ORDERS_PATH", tmp_path / "p.json")
    monkeypatch.setattr(cfg, "REBALANCE_PATH", tmp_path / "r.json")
    monkeypatch.setattr(cfg, "TRADES_PATH", tmp_path / "t.jsonl")

    broker = _mock_broker({"total_eval": 9_600_000, "foreign_eval_krw": 0,
                            "cash_usd": 0, "fx_usdkrw": 0})
    trader = Trader(broker)
    trader._daily_loss_limit_pct = 3.0
    triggered = []
    trader._ks_trigger_hook = lambda src: triggered.append(src)
    # cycle 내부를 시뮬레이션
    trader._in_cycle = True

    p = {"strategy_id": "s1", "symbol": "005930", "side": "buy",
         "qty": 10, "strategy_name": "T1", "definition": {}}
    trader._apply_fill("ORD1", p, 10, 60000.0, [], partial=False)

    # in_cycle 상태 → 평가/hook skip. account_snapshot도 호출 안 됨.
    assert triggered == []
    assert broker.account_snapshot.call_count == 0
    # ks도 활성화 안 됨 — cycle 본체가 진입부에서 평가하므로
    assert isolated_ks.is_active() is False


def test_in_cycle_flag_reset_on_exception():
    """예외 발생 시에도 _in_cycle이 finally로 reset 보장."""
    from localapp.trader import Trader, _CYCLE_LOCK

    broker = MagicMock()
    broker.account_snapshot.side_effect = Exception("KIS down")
    trader = Trader.__new__(Trader)
    trader.broker = broker
    trader._in_cycle = False
    trader._daily_loss_limit_pct = None
    trader._ks_trigger_hook = None

    # _cycle_locked가 예외 던지면 _in_cycle이 reset돼야 함
    def boom(*a, **kw):
        raise RuntimeError("forced")
    trader._cycle_body = boom
    with pytest.raises(RuntimeError):
        trader._cycle_locked([], {}, None, None, None, "KRX")
    assert trader._in_cycle is False


def test_apply_fill_no_trigger_when_limit_unset(monkeypatch, tmp_path):
    """_daily_loss_limit_pct 미설정 시 평가 skip — 임의 trader 사용 시 안전."""
    from localapp.trader import Trader
    from localapp import config as cfg
    monkeypatch.setattr(cfg, "LEDGER_PATH", tmp_path / "l.json")
    monkeypatch.setattr(cfg, "EQUITY_PATH", tmp_path / "e.json")
    monkeypatch.setattr(cfg, "PENDING_ORDERS_PATH", tmp_path / "p.json")
    monkeypatch.setattr(cfg, "REBALANCE_PATH", tmp_path / "r.json")
    monkeypatch.setattr(cfg, "TRADES_PATH", tmp_path / "t.jsonl")

    broker = _mock_broker({"total_eval": 9_000_000, "foreign_eval_krw": 0,
                            "cash_usd": 0, "fx_usdkrw": 0})
    trader = Trader(broker)
    # _daily_loss_limit_pct는 None — 평가 skip
    triggered = []
    trader._ks_trigger_hook = lambda src: triggered.append(src)
    p = {"strategy_id": "s1", "symbol": "005930", "side": "buy",
         "qty": 10, "strategy_name": "T1", "definition": {}}
    trader._apply_fill("ORD1", p, 10, 60000.0, [], partial=False)
    # broker.account_snapshot도 호출 안 됨 (limit None → skip)
    assert triggered == []


# ── AL-4: _CYCLE_LOCK 직렬화 ──────────────────────────────────────────────────


def test_cycle_lock_is_module_level():
    """_CYCLE_LOCK이 module-level이고 threading.Lock 인스턴스."""
    from localapp import trader as trader_mod
    assert isinstance(trader_mod._CYCLE_LOCK, type(threading.Lock()))


def test_cycle_lock_serializes_concurrent_acquires():
    """동시 acquire 시도 — 한 번에 1개만 acquire."""
    from localapp.trader import _CYCLE_LOCK
    assert _CYCLE_LOCK.acquire(blocking=False) is True
    try:
        # 첫 acquire 점유 중 → 두 번째는 즉시 실패
        assert _CYCLE_LOCK.acquire(blocking=False) is False
    finally:
        _CYCLE_LOCK.release()
    # 해제 후 다시 acquire 가능
    assert _CYCLE_LOCK.acquire(blocking=False) is True
    _CYCLE_LOCK.release()

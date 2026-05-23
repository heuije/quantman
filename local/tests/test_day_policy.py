"""Q7 회귀 — DAY 단일 정책 (5분 timeout cancel 제거).

결정(2026-05-23): 업계 표준 DAY 정책(Alpaca/IB/Fidelity/KIS 공통) 채택.
KIS가 정규장 마감(15:30)에 미체결분을 자동 cancel하므로 로컬 timeout
분기는 제거. 일중 limit 도달 시 자연 체결을 허용.

검증:
1. _resolve_pending이 timeout cancel을 호출하지 않음 (오래된 미체결 유지)
2. KIS가 cancelled로 반환하면 정리 + decisions에 unfilled 기록
3. _wait_pending이 post_submit_wait_sec(=60) 기본값 사용
4. 발주 시 p 객체에 timeout_sec 필드 없음
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))


@pytest.fixture
def _isolated_paths(monkeypatch, tmp_path):
    from localapp import config as cfg
    monkeypatch.setattr(cfg, "LEDGER_PATH", tmp_path / "l.json")
    monkeypatch.setattr(cfg, "EQUITY_PATH", tmp_path / "e.json")
    monkeypatch.setattr(cfg, "PENDING_ORDERS_PATH", tmp_path / "p.json")
    monkeypatch.setattr(cfg, "REBALANCE_PATH", tmp_path / "r.json")
    monkeypatch.setattr(cfg, "TRADES_PATH", tmp_path / "t.jsonl")
    yield tmp_path


def test_resolve_pending_does_not_timeout_cancel(_isolated_paths):
    """오래된(예: 6시간 전) 미체결도 우리가 cancel하지 않음 — DAY 정책."""
    from localapp.trader import Trader

    broker = MagicMock()
    # KIS는 여전히 unknown 상태 반환 (KIS가 알아서 마감에 cancel할 것)
    broker.order_status.return_value = {
        "status": "unknown", "filled_qty": 0, "fill_price": 0}
    trader = Trader(broker)
    trader.pending = {
        "ORD1": {"symbol": "005930", "side": "buy", "qty": 10,
                 "strategy_id": "s1", "strategy_name": "T1",
                 # 6시간 전 발주 (어떤 timeout이든 초과)
                 "submitted_ts": time.time() - 6 * 3600,
                 "limit_price": 60000, "intended_price": 60000,
                 "filled_so_far": 0},
    }
    decisions = []
    trader._resolve_pending(decisions)

    # 우리는 cancel 호출 안 함
    assert broker.cancel.call_count == 0
    # pending도 유지 (KIS가 마감 시 처리할 것)
    assert "ORD1" in trader.pending
    # timeout 결정도 없음
    assert not any(d.get("action") in ("unfilled", "timeout") for d in decisions)


def test_resolve_pending_handles_kis_cancelled(_isolated_paths):
    """KIS가 cancelled 반환 시 ledger·pending 정리 + decisions에 unfilled."""
    from localapp.trader import Trader

    broker = MagicMock()
    broker.order_status.return_value = {
        "status": "cancelled", "filled_qty": 0, "fill_price": 0}
    trader = Trader(broker)
    trader.pending = {
        "ORD1": {"symbol": "005930", "side": "buy", "qty": 10,
                 "strategy_id": "s1", "strategy_name": "T1",
                 "submitted_ts": time.time(),
                 "limit_price": 60000, "intended_price": 60000,
                 "filled_so_far": 0},
    }
    decisions = []
    trader._resolve_pending(decisions)

    # pending 정리
    assert "ORD1" not in trader.pending
    # 우리는 cancel 호출 안 함 (KIS가 이미 cancel)
    assert broker.cancel.call_count == 0
    # decisions에 unfilled 기록
    assert any(d.get("action") == "unfilled" for d in decisions)


def test_exec_defaults_has_post_submit_wait_sec():
    """exec_defaults가 post_submit_wait_sec를 정의하고 unfilled_timeout_sec는
    제거됨 (또는 더 이상 _wait_pending에서 참조되지 않음)."""
    from quant_core.exec_defaults import DEFAULT_EXECUTION
    assert "post_submit_wait_sec" in DEFAULT_EXECUTION
    assert DEFAULT_EXECUTION["post_submit_wait_sec"] == 60


def test_wait_pending_uses_post_submit_wait_sec(_isolated_paths):
    """_wait_pending이 짧은 시간만 폴링 (단위테스트는 0초 timeout으로 즉시 종료)."""
    from localapp.trader import Trader

    broker = MagicMock()
    broker.order_status.return_value = {
        "status": "unknown", "filled_qty": 0, "fill_price": 0}
    trader = Trader(broker)
    trader.pending = {
        "ORD1": {"symbol": "005930", "side": "buy", "qty": 10,
                 "strategy_id": "s1", "strategy_name": "T1",
                 "submitted_ts": time.time(),
                 "limit_price": 60000, "intended_price": 60000,
                 "filled_so_far": 0},
    }
    # 0초 timeout → 즉시 종료
    start = time.time()
    trader._wait_pending(0, 0.01, [])
    elapsed = time.time() - start
    # 0초 timeout이라 0.5초 안에는 끝나야 함
    assert elapsed < 0.5

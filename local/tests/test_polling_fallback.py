"""Q3 회귀 — 시세 WebSocket 끊김 시 REST 폴링 fallback.

결정(2026-05-23):
- 방안 A 채택 (REST 폴링 fallback)
- 사용자 알림 생략 (서비스상 별도 대응 존재)
- 폴링 주기 동적 (보유 종목 수별, KIS rate limit 보호)
- WebSocket 복구 안 되면 장 마감까지 폴링 계속

검증:
1. _polling_round_interval — 보유 종목 수별 주기 매핑
2. ws.is_connected=True면 폴링 skip
3. ws.is_connected=False면 broker.price 호출 → manager.on_tick 전달
4. 라운드 중 ws 복구 시 즉시 중단
5. stop_flag로 종료
6. broker.price 예외 시 다음 종목 진행
7. 보유 0종목이면 폴링 skip + health check 주기 대기
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))


# ── _polling_round_interval ──────────────────────────────────────────────────


def test_polling_interval_by_held_count():
    from localapp.intraday_loop import _polling_round_interval
    assert _polling_round_interval(1) == 3.0
    assert _polling_round_interval(5) == 3.0
    assert _polling_round_interval(6) == 5.0
    assert _polling_round_interval(15) == 5.0
    assert _polling_round_interval(16) == 10.0
    assert _polling_round_interval(30) == 10.0
    assert _polling_round_interval(31) == 15.0
    assert _polling_round_interval(50) == 15.0
    assert _polling_round_interval(51) == 20.0
    assert _polling_round_interval(100) == 20.0


# ── 폴링 동작 ────────────────────────────────────────────────────────────────


def _make_mock_manager(held: list[str]):
    """held 종목을 가진 manager mock. on_tick 호출 기록."""
    mgr = MagicMock()
    mgr.held_symbols.return_value = set(held)
    mgr.ticks = []
    def _on_tick(sym, price):
        mgr.ticks.append((sym, price))
    mgr.on_tick.side_effect = _on_tick
    return mgr


def test_polling_skipped_when_ws_connected(monkeypatch):
    """ws.is_connected=True면 broker.price 미호출."""
    from localapp import intraday_loop

    # health check 주기를 짧게
    monkeypatch.setattr(intraday_loop, "_POLLING_HEALTH_CHECK_SEC", 0.05)

    ws = MagicMock()
    ws.is_connected = True
    broker = MagicMock()
    broker.price.return_value = 60000.0
    mgr = _make_mock_manager(["005930"])
    stop_flag = threading.Event()

    t = threading.Thread(target=intraday_loop._rest_polling_loop,
                          kwargs={"ws": ws, "broker": broker, "manager": mgr,
                                   "in_market_fn": lambda s: True,
                                   "stop_flag": stop_flag, "market": "KRX"},
                          daemon=True)
    t.start()
    time.sleep(0.15)
    stop_flag.set()
    t.join(timeout=2)

    # WebSocket 정상이라 폴링 skip
    assert broker.price.call_count == 0
    assert mgr.ticks == []


def test_polling_invokes_broker_when_ws_disconnected(monkeypatch):
    """ws.is_connected=False면 broker.price 호출 → manager.on_tick 전달."""
    from localapp import intraday_loop

    monkeypatch.setattr(intraday_loop, "_POLLING_HEALTH_CHECK_SEC", 0.05)

    ws = MagicMock()
    ws.is_connected = False
    broker = MagicMock()
    broker.price.return_value = 60000.0
    mgr = _make_mock_manager(["005930", "000660"])
    stop_flag = threading.Event()

    t = threading.Thread(target=intraday_loop._rest_polling_loop,
                          kwargs={"ws": ws, "broker": broker, "manager": mgr,
                                   "in_market_fn": lambda s: True,
                                   "stop_flag": stop_flag, "market": "KRX"},
                          daemon=True)
    t.start()
    # 2종목 → round 3초 → 종목당 1.5초 → 첫 종목은 즉시, 다음은 1.5초 후
    # 4초 정도면 한 라운드는 충분히 완료
    time.sleep(4.0)
    stop_flag.set()
    t.join(timeout=3)

    # 보유 2종목 모두 1회 이상 폴링
    assert broker.price.call_count >= 2
    assert len(mgr.ticks) >= 2
    syms_polled = {sym for sym, _ in mgr.ticks}
    assert "005930" in syms_polled
    assert "000660" in syms_polled
    # on_tick에 price 그대로 전달
    assert all(price == 60000.0 for _, price in mgr.ticks)


def test_polling_round_aborts_on_ws_recovery(monkeypatch):
    """라운드 중 ws.is_connected=True 되면 즉시 중단."""
    from localapp import intraday_loop

    monkeypatch.setattr(intraday_loop, "_POLLING_HEALTH_CHECK_SEC", 0.05)

    # 첫 종목 호출 후 ws 복구 시뮬레이션
    ws = MagicMock()
    ws.is_connected = False

    call_log = []

    def _price_side_effect(sym):
        call_log.append(sym)
        # 첫 호출 후 ws 복구
        if len(call_log) == 1:
            ws.is_connected = True
        return 60000.0

    broker = MagicMock()
    broker.price.side_effect = _price_side_effect
    mgr = _make_mock_manager(["005930", "000660", "035720", "035420"])
    stop_flag = threading.Event()

    t = threading.Thread(target=intraday_loop._rest_polling_loop,
                          kwargs={"ws": ws, "broker": broker, "manager": mgr,
                                   "in_market_fn": lambda s: True,
                                   "stop_flag": stop_flag, "market": "KRX"},
                          daemon=True)
    t.start()
    time.sleep(2.0)
    stop_flag.set()
    t.join(timeout=3)

    # 4종목 보유였지만 첫 호출 후 복구 감지 → 라운드 중단
    # 결과: 첫 호출만 발생 (또는 1~2개 — 타이밍 의존). 4개 전부는 호출 안 됨.
    assert broker.price.call_count < 4


def test_polling_continues_on_price_exception(monkeypatch):
    """broker.price 실패 시 다음 종목 진행."""
    from localapp import intraday_loop

    monkeypatch.setattr(intraday_loop, "_POLLING_HEALTH_CHECK_SEC", 0.05)

    ws = MagicMock()
    ws.is_connected = False
    broker = MagicMock()
    # 첫 호출은 실패, 두 번째는 성공
    broker.price.side_effect = [RuntimeError("KIS rate limit"), 60000.0,
                                  RuntimeError("KIS rate limit"), 60000.0]
    mgr = _make_mock_manager(["005930", "000660"])
    stop_flag = threading.Event()

    t = threading.Thread(target=intraday_loop._rest_polling_loop,
                          kwargs={"ws": ws, "broker": broker, "manager": mgr,
                                   "in_market_fn": lambda s: True,
                                   "stop_flag": stop_flag, "market": "KRX"},
                          daemon=True)
    t.start()
    time.sleep(3.5)
    stop_flag.set()
    t.join(timeout=3)

    # 실패한 종목은 on_tick 호출 안 되고, 성공한 종목만 전달됨
    assert broker.price.call_count >= 2
    # 성공한 호출만 ticks에 들어감
    assert len(mgr.ticks) >= 1
    assert all(price == 60000.0 for _, price in mgr.ticks)


def test_polling_no_holdings_waits_health_check(monkeypatch):
    """보유 0종목이면 폴링 skip + health check 주기 대기."""
    from localapp import intraday_loop

    monkeypatch.setattr(intraday_loop, "_POLLING_HEALTH_CHECK_SEC", 0.05)

    ws = MagicMock()
    ws.is_connected = False
    broker = MagicMock()
    broker.price.return_value = 60000.0
    mgr = _make_mock_manager([])     # 보유 없음
    stop_flag = threading.Event()

    t = threading.Thread(target=intraday_loop._rest_polling_loop,
                          kwargs={"ws": ws, "broker": broker, "manager": mgr,
                                   "in_market_fn": lambda s: True,
                                   "stop_flag": stop_flag, "market": "KRX"},
                          daemon=True)
    t.start()
    time.sleep(0.2)
    stop_flag.set()
    t.join(timeout=2)

    # 보유 0이라 broker.price 미호출
    assert broker.price.call_count == 0
    assert mgr.ticks == []


def test_polling_in_market_filter(monkeypatch):
    """in_market_fn으로 시장별 필터링 — 다른 시장 종목은 skip."""
    from localapp import intraday_loop

    monkeypatch.setattr(intraday_loop, "_POLLING_HEALTH_CHECK_SEC", 0.05)

    ws = MagicMock()
    ws.is_connected = False
    broker = MagicMock()
    broker.price.return_value = 100.0
    mgr = _make_mock_manager(["005930", "AAPL", "000660", "TSLA"])
    stop_flag = threading.Event()

    # US만 필터 (시뮬레이션)
    def in_us(sym):
        return sym in ("AAPL", "TSLA")

    t = threading.Thread(target=intraday_loop._rest_polling_loop,
                          kwargs={"ws": ws, "broker": broker, "manager": mgr,
                                   "in_market_fn": in_us,
                                   "stop_flag": stop_flag, "market": "US"},
                          daemon=True)
    t.start()
    time.sleep(4.0)
    stop_flag.set()
    t.join(timeout=3)

    polled_syms = [c.args[0] for c in broker.price.call_args_list]
    # KRX 종목은 안 불림
    assert "005930" not in polled_syms
    assert "000660" not in polled_syms
    # US 종목만 호출
    assert all(s in ("AAPL", "TSLA") for s in polled_syms)


def test_polling_stops_on_stop_flag():
    """stop_flag.set()으로 즉시 종료."""
    from localapp import intraday_loop

    ws = MagicMock()
    ws.is_connected = False
    broker = MagicMock()
    broker.price.return_value = 100.0
    mgr = _make_mock_manager(["005930"])
    stop_flag = threading.Event()

    t = threading.Thread(target=intraday_loop._rest_polling_loop,
                          kwargs={"ws": ws, "broker": broker, "manager": mgr,
                                   "in_market_fn": lambda s: True,
                                   "stop_flag": stop_flag, "market": "KRX"},
                          daemon=True)
    t.start()
    time.sleep(0.5)
    stop_flag.set()
    # join은 5초 안에 끝나야 함 (per_symbol_sleep=3.0이지만 wait이 stop_flag 인지)
    t.join(timeout=5)
    assert not t.is_alive()

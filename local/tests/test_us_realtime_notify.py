"""P8 — 미국 해외 실시간 시세 미신청 고지(_check_us_realtime) 단위테스트 (네트워크 없음).

KIS 해외 실시간 시세 신청(HTS [7781]) 미신청 시 WebSocket 구독은 되지만 tick이
흐르지 않는다. intraday_loop은 grace(120s) 내내 US 보유분에 해외 tick이 0이면
'실시간 손절 미제공'을 사용자에게 1회 고지(서버 push)한다. 라이브 체결·시세
entitlement에 의존하지 않고 그 판정 로직을 결정론적으로 검증한다.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

# localapp 패키지 import 가능하도록 local 디렉터리를 path에 추가
_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))

from localapp import intraday_loop, market_index


class _StubManager:
    def __init__(self, held: list[str]):
        self._held = held

    def held_symbols(self):
        return list(self._held)


class _StubBroker:
    def account_snapshot(self, overseas: bool = True):
        return {"balance": {}, "positions": []}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """각 테스트마다 모듈 전역 상태를 격리하고 네트워크/시장인덱스를 stub.

    - push_snapshot: 호출 payload를 캡처(네트워크 차단).
    - market_index.is_us: AAPL/MSFT만 US로 — 마스터 로드 없이 결정론.
    - _us_realtime_warned / _state: 매 테스트 초기화.
    """
    captured: list[dict] = []
    monkeypatch.setattr(intraday_loop, "push_snapshot",
                        lambda payload: captured.append(payload))
    monkeypatch.setattr(market_index, "is_us",
                        lambda s: s in {"AAPL", "MSFT"})
    intraday_loop._us_realtime_warned = False
    intraday_loop._state.update({
        "market": "US", "started_at": 0.0, "last_overseas_tick": 0.0,
    })
    return captured


def _set_state(started_ago_sec: float, last_tick: float) -> None:
    intraday_loop._state["started_at"] = time.time() - started_ago_sec
    intraday_loop._state["last_overseas_tick"] = last_tick


# ── 양성: 보유 있음 + grace 경과 + tick 0 → 고지 발화 ──────────────────────────

def test_fires_when_us_held_and_no_tick_after_grace(_isolate):
    captured = _isolate
    _set_state(started_ago_sec=intraday_loop._US_REALTIME_GRACE_SEC + 10,
               last_tick=0.0)
    mgr = _StubManager(["AAPL"])
    intraday_loop._check_us_realtime(_StubBroker(), mgr)

    assert intraday_loop._us_realtime_warned is True
    assert len(captured) == 1
    cs = captured[0]["cycle_summary"]
    assert cs["kind"] == "us_realtime_unavailable"
    assert cs["us_realtime_unavailable"] is True
    assert "시세" in cs["message"]


# ── 음성 케이스들 ─────────────────────────────────────────────────────────────

def test_no_fire_within_grace(_isolate):
    captured = _isolate
    _set_state(started_ago_sec=10, last_tick=0.0)   # grace(120s) 이내
    intraday_loop._check_us_realtime(_StubBroker(), _StubManager(["AAPL"]))
    assert intraday_loop._us_realtime_warned is False
    assert captured == []


def test_no_fire_when_tick_received(_isolate):
    captured = _isolate
    _set_state(started_ago_sec=intraday_loop._US_REALTIME_GRACE_SEC + 10,
               last_tick=time.time())              # tick 수신됨 → 정상
    intraday_loop._check_us_realtime(_StubBroker(), _StubManager(["AAPL"]))
    assert intraday_loop._us_realtime_warned is False
    assert captured == []


def test_no_fire_when_no_us_holding(_isolate):
    captured = _isolate
    _set_state(started_ago_sec=intraday_loop._US_REALTIME_GRACE_SEC + 10,
               last_tick=0.0)
    # KR 종목만 보유 — is_us stub이 False → 미국 보유 없음
    intraday_loop._check_us_realtime(_StubBroker(), _StubManager(["005930"]))
    assert intraday_loop._us_realtime_warned is False
    assert captured == []


def test_fires_only_once_per_session(_isolate):
    captured = _isolate
    _set_state(started_ago_sec=intraday_loop._US_REALTIME_GRACE_SEC + 10,
               last_tick=0.0)
    mgr = _StubManager(["AAPL", "MSFT"])
    intraday_loop._check_us_realtime(_StubBroker(), mgr)
    intraday_loop._check_us_realtime(_StubBroker(), mgr)   # 두 번째 호출
    assert len(captured) == 1                              # 세션당 1회만

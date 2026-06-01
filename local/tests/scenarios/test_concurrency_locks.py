"""REV-A 회귀 — pending 변경 진입점이 _CYCLE_LOCK으로 직렬화되는가(INV-CONC-1).

M3는 _apply_fill을 락으로 닫았으나, 그 _apply_fill *밖*의 작업 — (1) _resolve_pending의
pending 순회·order_status·del, (2) _on_exec_event의 매칭·dedup append·del — 이 락 밖이라
스케줄러/settlement 스레드와 WS 스레드가 같은 pending을 비직렬 변경하면 over-position/
KeyError-스레드死.

핵심: "_apply_fill에서 블록되니 차단됨"은 가짜 신호다. _apply_fill *이전*의 부수효과
(order_status 호출 / dedup append)가 락 점유 중에도 일어나면 임계구역이 락 밖이라는 증거.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from sim import scenario


def _submit(t, broker, sid="s1", qty=5, px=70000):
    r = broker.buy_limit("005930", qty, px)
    t._after_submit(r, sid, "T", {}, "005930", "buy", qty, px, px,
                    {"use_limit": True, "buy_tolerance_pct": 1.0}, [], reason="x")
    return r["order_no"]


def _hold_lock_then(call, observe_side_effect_absent):
    """다른 스레드가 _CYCLE_LOCK 점유 중일 때 call()의 _apply_fill 이전 부수효과가
    일어나지 않아야(=임계구역이 락 안) 한다. observe_side_effect_absent()는 점유 중
    부수효과가 '아직 없음'이면 True."""
    from localapp.trader import _CYCLE_LOCK
    lock_held = threading.Event()
    release = threading.Event()
    done = threading.Event()

    def holder():
        with _CYCLE_LOCK:
            lock_held.set()
            release.wait(timeout=3)

    def worker():
        call()
        done.set()

    h = threading.Thread(target=holder, daemon=True)
    h.start()
    assert lock_held.wait(timeout=3)
    w = threading.Thread(target=worker, daemon=True)
    w.start()
    time.sleep(0.3)                       # 락 점유 중 진행 기회를 충분히 줌
    ok = observe_side_effect_absent()
    assert not done.is_set(), "락 점유 중 진입점이 완료됨"
    release.set()
    assert done.wait(timeout=3), "락 해제 후에도 미완료"
    h.join(timeout=3)
    w.join(timeout=3)
    assert ok, "임계구역(_apply_fill 이전 부수효과)이 _CYCLE_LOCK 밖에서 실행됨(직렬화 실패)"


def test_resolve_pending_serializes_on_lock(isolated_trader):
    """_resolve_pending이 락을 잡기 전까진 order_status조차 호출하면 안 된다."""
    t, broker = isolated_trader
    on = _submit(t, broker)
    broker.mark_filled(on, 5, 70000.0)
    calls = {"n": 0}
    orig = broker.order_status

    def spy(order_no, symbol=None):
        calls["n"] += 1
        return orig(order_no, symbol)
    broker.order_status = spy

    _hold_lock_then(lambda: t._resolve_pending([]),
                    observe_side_effect_absent=lambda: calls["n"] == 0)
    assert t.ledger["s1"]["qty"] == 5


def test_on_exec_event_serializes_on_lock(isolated_trader):
    """_on_exec_event의 dedup append(매칭 직후)가 락 점유 중엔 일어나면 안 된다."""
    t, broker = isolated_trader
    on = _submit(t, broker)
    entry = next(iter(t.pending.values()))    # pending 엔트리(공유 dict)

    _hold_lock_then(lambda: scenario.inject_ws_fill(t, broker, on, 5, 70000.0),
                    observe_side_effect_absent=lambda: not entry.get("_dedup_keys"))
    assert t.ledger["s1"]["qty"] == 5

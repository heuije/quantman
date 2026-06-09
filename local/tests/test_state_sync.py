"""회귀 — 웹↔로컬 상태 실시간 동기화.

근본 결함: 상태 변경(kill-switch 해제·일시정지·재개·주문취소)이 스냅샷 push를 안 해
웹이 stale("해제했는데 웹은 활성")로 남았다. 또 자동매매 running/paused 상태는 스냅샷에
실리지조차 않아 웹에 표시 불가.

계약: ① auto_state(running/paused/stopped) 영속 ② 모든 push가 auto_status를 단일
출구(push_snapshot)에서 일괄 주입 ③ Trader.state_snapshot()이 거래 없이 현 상태를 빌드.
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import auto_state, sync_client


def test_auto_state_default_is_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(auto_state, "AUTO_STATE_PATH", tmp_path / "a.json")
    assert auto_state.load() == "stopped"


def test_auto_state_roundtrip_and_invalid_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(auto_state, "AUTO_STATE_PATH", tmp_path / "a.json")
    auto_state.set_status("running")
    assert auto_state.load() == "running"
    auto_state.set_status("paused")
    assert auto_state.load() == "paused"
    auto_state.set_status("bogus")                  # 유효하지 않은 값은 무시
    assert auto_state.load() == "paused"


class _Resp:
    def raise_for_status(self):
        pass


def _capture_post(monkeypatch):
    captured = {}

    def _post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        return _Resp()

    monkeypatch.setattr(sync_client.requests, "post", _post)
    monkeypatch.setattr(sync_client, "_headers", lambda: {})
    return captured


def test_push_snapshot_injects_auto_status(monkeypatch, tmp_path):
    """어떤 payload로 push되든 현재 auto_status가 단일 출구에서 주입된다."""
    monkeypatch.setattr(auto_state, "AUTO_STATE_PATH", tmp_path / "a.json")
    auto_state.set_status("paused")
    captured = _capture_post(monkeypatch)
    sync_client.push_snapshot({"balance": {}})
    assert captured["json"]["payload"]["auto_status"] == "paused"


def test_push_snapshot_preserves_explicit_auto_status(monkeypatch, tmp_path):
    """builder가 명시한 auto_status는 보존(setdefault)."""
    monkeypatch.setattr(auto_state, "AUTO_STATE_PATH", tmp_path / "a.json")
    auto_state.set_status("running")
    captured = _capture_post(monkeypatch)
    sync_client.push_snapshot({"balance": {}, "auto_status": "stopped"})
    assert captured["json"]["payload"]["auto_status"] == "stopped"

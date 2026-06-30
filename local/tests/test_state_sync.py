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


# ── 동기화 성공 로그 평가금액 — 선물 전용 balance KeyError 둔갑 회귀 (2026-06-30 LS 선물 모의) ──
# 종전: push_snapshot 성공 후 성공로그가 balance['total_eval']를 하드 subscript → 선물 전용
# balance엔 키 부재 → KeyError가 같은 try의 except에서 '동기화 실패'로 둔갑 + 불필요 재전송.
# 근본수정: 로그를 try/except/else의 else로 분리 + _synced_eval_krw(.get + futures_eval_krw 폴백).

def test_synced_eval_krw_futures_only_balance_falls_back():
    from localapp.runner import _synced_eval_krw
    # 선물 전용 balance(주식 미보유) — total_eval 키 자체가 없음 → 통합평가 폴백(KeyError 아님).
    assert _synced_eval_krw(
        {"futures_eval_krw": 236254700.0, "futures_order_cash_kr": 3.4e8}) == 236254700


def test_synced_eval_krw_stock_uses_total_eval():
    from localapp.runner import _synced_eval_krw
    assert _synced_eval_krw({"total_eval": 500000000, "futures_eval_krw": 1}) == 500000000


def test_synced_eval_krw_empty_balance_zero():
    from localapp.runner import _synced_eval_krw
    assert _synced_eval_krw({}) == 0                      # 키 둘 다 없음 → 0 (KeyError 아님)


def test_push_snapshot_preserves_explicit_auto_status(monkeypatch, tmp_path):
    """builder가 명시한 auto_status는 보존(setdefault)."""
    monkeypatch.setattr(auto_state, "AUTO_STATE_PATH", tmp_path / "a.json")
    auto_state.set_status("running")
    captured = _capture_post(monkeypatch)
    sync_client.push_snapshot({"balance": {}, "auto_status": "stopped"})
    assert captured["json"]["payload"]["auto_status"] == "stopped"

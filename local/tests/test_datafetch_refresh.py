"""회귀 — 2026-06-10 무발주 인시던트 RC-1: 발주 사이클의 dataset 갱신 정지점 제거.

확정된 사고 메커니즘(py-spy 실측): 기동 스레드의 갱신이 _REFRESH_LOCK을 시간
단위로 점유(410 → manifest 종목별 폴백 grind) → 모든 발주 사이클이 timeout 없는
락 acquire에서 무한·무로그 대기(락 컨보이) → 발주 0.
docs/incidents/2026-06-10-autotrading-week-retrospective.md 참조.

구조 수정 계약:
  ① 갱신 진행 중이면 후발 호출은 기다리지 않고 즉시 False(캐시 진행).
  ② bundle 410이면 manifest 폴백 없이 False(캐시 진행) — 폴백 자체 제거.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import datafetch, sync_client


def test_refresh_returns_immediately_when_already_running(monkeypatch):
    """① 락 보유자가 있으면 대기 없이 즉시 캐시 진행 — 락 컨보이 금지."""
    called = {"bundle": False}
    monkeypatch.setattr(
        sync_client, "fetch_dataset_bundle",
        lambda *a, **k: called.__setitem__("bundle", True) or {"ok": True})

    assert datafetch._REFRESH_LOCK.acquire(blocking=False), "테스트 전제: 락 획득"
    try:
        t0 = time.monotonic()
        result = datafetch.refresh_market_data()
        elapsed = time.monotonic() - t0
    finally:
        datafetch._REFRESH_LOCK.release()

    assert result is False, "갱신 진행 중엔 캐시 진행(False)이어야 한다"
    assert elapsed < 1.0, f"즉시 반환해야 한다 (실측 {elapsed:.2f}s — 대기 발생)"
    assert called["bundle"] is False, "후발 호출이 중복 다운로드하면 안 된다"


def test_bundle_410_proceeds_with_cache_without_manifest_fallback(monkeypatch):
    """② 410(server bundle 미준비)은 캐시 진행 — manifest 종목별 폴백 제거 확인."""
    def _raise_410(*a, **k):
        raise ValueError("server bundle 미준비")

    monkeypatch.setattr(sync_client, "fetch_dataset_bundle", _raise_410)
    result = datafetch.refresh_market_data()
    assert result is False, "410은 예외 없이 캐시 진행(False)"
    # 폴백 함수 자체가 제거됐어야 한다 — 재도입(부활) 회귀 방지.
    assert not hasattr(sync_client, "sync_dataset")
    assert not hasattr(sync_client, "fetch_dataset_symbol")

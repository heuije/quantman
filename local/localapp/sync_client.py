"""플랫폼 동기화 — 로컬앱에서 아웃바운드 연결만 사용.

올리는 것: 잔고·포지션·자산곡선·체결로그 (안전정보).
받는 것: 모의/실전으로 배정된 전략 정의.
API키·계좌번호·원시주문은 절대 전송하지 않는다.
"""

from __future__ import annotations

import requests

from .config import PLATFORM_URL
from .secrets_store import load_device_token


def _headers() -> dict:
    token = load_device_token()
    if not token:
        raise RuntimeError("기기 페어링이 필요합니다.")
    return {"Authorization": f"Bearer {token}"}


def push_snapshot(payload: dict) -> None:
    """안전정보 스냅샷을 플랫폼에 푸시."""
    r = requests.post(f"{PLATFORM_URL}/sync/push", headers=_headers(),
                      json={"payload": payload}, timeout=15)
    r.raise_for_status()


def pull_strategies() -> list[dict]:
    """모의/실전으로 배정된 전략 목록을 가져온다."""
    r = requests.get(f"{PLATFORM_URL}/sync/strategies", headers=_headers(),
                     timeout=15)
    r.raise_for_status()
    return r.json()

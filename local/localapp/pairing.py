"""플랫폼 기기 페어링 — OAuth 기기 인증 그랜트의 로컬앱 측.

브라우저에서 사용자가 로그인·승인하고, 로컬앱은 발급된 기기 토큰만 받는다.
비밀번호는 로컬앱을 거치지 않는다.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

import requests

from .config import PLATFORM_URL
from .secrets_store import save_device_token


class PairingCancelled(Exception):
    """사용자가 페어링을 취소함 — 폴링 중단(승인 실패·만료와 구분되는 정상 흐름)."""


def start_pairing(device_name: str = "내 PC") -> dict:
    """페어링 시작 — 사용자에게 보여줄 user_code와 인증 URL을 받는다."""
    r = requests.post(f"{PLATFORM_URL}/auth/device/start",
                      json={"device_name": device_name}, timeout=10)
    r.raise_for_status()
    return r.json()   # {device_code, user_code, verification_uri, expires_in}


def poll_for_token(device_code: str, interval: float = 2.0,
                   timeout: float = 600.0,
                   stop_event: Optional[threading.Event] = None) -> str:
    """사용자가 웹에서 승인할 때까지 폴링한다. 승인되면 기기 토큰을 저장·반환.

    stop_event가 set되면 즉시 PairingCancelled를 던져 폴링을 중단한다(취소 지원 —
    대기를 Event.wait로 처리해 최대 interval만큼 기다리지 않고 바로 빠져나온다).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if stop_event is not None and stop_event.is_set():
            raise PairingCancelled()
        r = requests.post(f"{PLATFORM_URL}/auth/device/token",
                          json={"device_code": device_code}, timeout=10)
        if r.status_code == 410:
            raise TimeoutError("페어링 코드가 만료되었습니다. 다시 시도하세요.")
        r.raise_for_status()
        d = r.json()
        if d["status"] == "approved":
            save_device_token(d["device_token"])
            return d["device_token"]
        # 취소 가능한 대기 — stop_event가 interval 내 set되면 즉시 중단.
        if stop_event is not None:
            if stop_event.wait(interval):
                raise PairingCancelled()
        else:
            time.sleep(interval)
    raise TimeoutError("페어링 승인 대기 시간이 초과되었습니다.")

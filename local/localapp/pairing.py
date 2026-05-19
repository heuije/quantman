"""플랫폼 기기 페어링 — OAuth 기기 인증 그랜트의 로컬앱 측.

브라우저에서 사용자가 로그인·승인하고, 로컬앱은 발급된 기기 토큰만 받는다.
비밀번호는 로컬앱을 거치지 않는다.
"""

from __future__ import annotations

import time

import requests

from .config import PLATFORM_URL
from .secrets_store import save_device_token


def start_pairing(device_name: str = "내 PC") -> dict:
    """페어링 시작 — 사용자에게 보여줄 user_code와 인증 URL을 받는다."""
    r = requests.post(f"{PLATFORM_URL}/auth/device/start",
                      json={"device_name": device_name}, timeout=10)
    r.raise_for_status()
    return r.json()   # {device_code, user_code, verification_uri, expires_in}


def poll_for_token(device_code: str, interval: float = 2.0,
                   timeout: float = 600.0) -> str:
    """사용자가 웹에서 승인할 때까지 폴링한다. 승인되면 기기 토큰을 저장·반환."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.post(f"{PLATFORM_URL}/auth/device/token",
                          json={"device_code": device_code}, timeout=10)
        if r.status_code == 410:
            raise TimeoutError("페어링 코드가 만료되었습니다. 다시 시도하세요.")
        r.raise_for_status()
        d = r.json()
        if d["status"] == "approved":
            save_device_token(d["device_token"])
            return d["device_token"]
        time.sleep(interval)
    raise TimeoutError("페어링 승인 대기 시간이 초과되었습니다.")

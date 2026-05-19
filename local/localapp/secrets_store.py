"""민감정보 보관 — OS 자격증명 저장소(keyring) 전용.

KIS API키·계좌번호·플랫폼 기기토큰은 평문 파일에 절대 저장하지 않는다.
이 정보는 사용자 PC를 떠나지 않는다.
"""

from __future__ import annotations

import json

import keyring

from .config import KEYRING_SERVICE

_KIS = "kis_credentials"
_DEVICE = "device_token"


def save_kis(app_key: str, app_secret: str, account_no: str,
             virtual: bool = True) -> None:
    keyring.set_password(KEYRING_SERVICE, _KIS, json.dumps({
        "app_key": app_key,
        "app_secret": app_secret,
        "account_no": account_no,
        "virtual": virtual,
    }))


def load_kis() -> dict | None:
    raw = keyring.get_password(KEYRING_SERVICE, _KIS)
    return json.loads(raw) if raw else None


def save_device_token(token: str) -> None:
    keyring.set_password(KEYRING_SERVICE, _DEVICE, token)


def load_device_token() -> str | None:
    return keyring.get_password(KEYRING_SERVICE, _DEVICE)


def clear() -> None:
    for key in (_KIS, _DEVICE):
        try:
            keyring.delete_password(KEYRING_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass

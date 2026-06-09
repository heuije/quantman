"""민감정보 보관 — OS 자격증명 저장소(keyring) 전용.

KIS API키·계좌번호·플랫폼 기기토큰은 평문 파일에 절대 저장하지 않는다.
이 정보는 사용자 PC를 떠나지 않는다.
"""

from __future__ import annotations

import json

import keyring

from .config import KEYRING_SERVICE

_KIS = "kis_credentials"
_KIS_FUT = "kis_futures_credentials"   # 선물옵션 계좌(상품코드 03) — 주식 계좌와 별개
_KIS_OVF = "kis_overseas_futures_credentials"   # 해외선물옵션 계좌(상품코드 08) — 국내선물·주식과 별개
_DEVICE = "device_token"


def save_kis(app_key: str, app_secret: str, account_no: str,
             virtual: bool = True, hts_id: str = "",
             quote_app_key: str = "", quote_app_secret: str = "") -> None:
    """KIS 자격증명 저장. hts_id는 체결통보 WebSocket(H0STCNI0)의 tr_key용 — 선택.

    quote_app_key/secret: 시세 조회용 **실전** 앱키. KIS는 시세를 실전 도메인 전용으로
    제공하고 모의 앱키를 거부(EGW02004)하므로, 모의(virtual=True) 사용 시 시세 조회에는
    별도 실전 앱키가 필수다. 주문·잔고는 app_key(모의)로, 시세만 quote_app_key(실전)로
    호출한다. 실전(virtual=False)이면 app_key가 이미 실전이라 비워둬도 된다.
    """
    keyring.set_password(KEYRING_SERVICE, _KIS, json.dumps({
        "app_key": app_key,
        "app_secret": app_secret,
        "account_no": account_no,
        "virtual": virtual,
        "hts_id": hts_id,
        "quote_app_key": quote_app_key,
        "quote_app_secret": quote_app_secret,
    }))


def load_kis() -> dict | None:
    raw = keyring.get_password(KEYRING_SERVICE, _KIS)
    return json.loads(raw) if raw else None


def save_kis_futures(app_key: str, app_secret: str, account_no: str,
                     virtual: bool = True) -> None:
    """선물옵션 *거래* 자격증명 저장(주식과 별개 — 선물옵션 계좌·상품코드 03).

    국내선물 모의투자 지원 → virtual=True로 먼저 검증 후 실전 전환 권장. 이 정보는
    사용자 PC를 떠나지 않는다(서버·리포 전송 금지).
    """
    keyring.set_password(KEYRING_SERVICE, _KIS_FUT, json.dumps({
        "app_key": app_key,
        "app_secret": app_secret,
        "account_no": account_no,
        "virtual": virtual,
    }))


def load_kis_futures() -> dict | None:
    raw = keyring.get_password(KEYRING_SERVICE, _KIS_FUT)
    return json.loads(raw) if raw else None


def save_kis_overseas_futures(app_key: str, app_secret: str, account_no: str,
                              virtual: bool = False) -> None:
    """해외선물옵션 *거래* 자격증명 저장(국내선물·주식과 별개 — 상품코드 08).

    ⚠ KIS 해외선물은 모의투자 미지원 → 실전 전용(virtual=False 고정 권장). 로컬 PC 전용(서버·리포 전송 금지).
    """
    keyring.set_password(KEYRING_SERVICE, _KIS_OVF, json.dumps({
        "app_key": app_key, "app_secret": app_secret,
        "account_no": account_no, "virtual": virtual,
    }))


def load_kis_overseas_futures() -> dict | None:
    raw = keyring.get_password(KEYRING_SERVICE, _KIS_OVF)
    return json.loads(raw) if raw else None


_cached_device_token = None


def save_device_token(token: str) -> None:
    global _cached_device_token
    keyring.set_password(KEYRING_SERVICE, _DEVICE, token)
    _cached_device_token = token


def load_device_token() -> str | None:
    global _cached_device_token
    if _cached_device_token is None:
        _cached_device_token = keyring.get_password(KEYRING_SERVICE, _DEVICE)
    return _cached_device_token


def clear() -> None:
    global _cached_device_token
    _cached_device_token = None
    for key in (_KIS, _KIS_FUT, _KIS_OVF, _DEVICE):
        try:
            keyring.delete_password(KEYRING_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass

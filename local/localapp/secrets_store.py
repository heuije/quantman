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
_LS = "ls_credentials"
_LS_FUT = "ls_futures_credentials"   # LS 선물계좌 (국내선물; 별도 계좌)
_LS_OVF = "ls_overseas_futures_credentials"   # LS 해외선물계좌 (CME 등; 별도 인증 컨텍스트)
_BROKER_CHOICE = "active_broker"   # "kis" | "ls" — 단일 브로커 모델 SSOT


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


def save_ls(app_key: str, app_secret: str, account_no: str,
            virtual: bool = True) -> None:
    """LS증권 자격증명 저장. 국내주식 기준 — 시세도 동일 키로 조회(KIS와 달리 별도
    실전 앱키 불필요, 미검증 가정). 모의(virtual)는 별도 키로 LS 서버가 자동 라우팅.
    이 정보는 사용자 PC를 떠나지 않는다(서버·리포 전송 금지)."""
    keyring.set_password(KEYRING_SERVICE, _LS, json.dumps({
        "app_key": app_key, "app_secret": app_secret,
        "account_no": account_no, "virtual": virtual,
    }))


def load_ls() -> dict | None:
    raw = keyring.get_password(KEYRING_SERVICE, _LS)
    return json.loads(raw) if raw else None


def save_ls_futures(app_key: str, app_secret: str, account_no: str,
                    virtual: bool = True) -> None:
    """LS 선물(국내·해외) 자격증명. 국내주식과 별도 계좌(LS OPEN API 계좌단위).
    이 정보는 사용자 PC를 떠나지 않는다(서버·리포 전송 금지)."""
    keyring.set_password(KEYRING_SERVICE, _LS_FUT, json.dumps({
        "app_key": app_key, "app_secret": app_secret,
        "account_no": account_no, "virtual": virtual,
    }))


def load_ls_futures() -> dict | None:
    raw = keyring.get_password(KEYRING_SERVICE, _LS_FUT)
    return json.loads(raw) if raw else None


def save_ls_overseas_futures(app_key: str, app_secret: str, account_no: str,
                              virtual: bool = False) -> None:
    """LS 해외선물(CME 등) 자격증명. 국내선물과 별도 계좌·인증 컨텍스트.
    이 정보는 사용자 PC를 떠나지 않는다(서버·리포 전송 금지)."""
    keyring.set_password(KEYRING_SERVICE, _LS_OVF, json.dumps({
        "app_key": app_key, "app_secret": app_secret,
        "account_no": account_no, "virtual": virtual,
    }))


def load_ls_overseas_futures() -> dict | None:
    raw = keyring.get_password(KEYRING_SERVICE, _LS_OVF)
    return json.loads(raw) if raw else None


def set_active_broker(name: str) -> None:
    if name not in ("kis", "ls"):
        raise ValueError(f"지원하지 않는 브로커: {name}")
    keyring.set_password(KEYRING_SERVICE, _BROKER_CHOICE, name)


def get_active_broker() -> str:
    """현재 활성 브로커. 미설정이면 'kis'(기존 사용자 무변경)."""
    return keyring.get_password(KEYRING_SERVICE, _BROKER_CHOICE) or "kis"


def broker_ready(broker: str) -> bool:
    """주어진 브로커의 자격증명이 하나라도 등록됐는지(자산군 슬롯 ≥1) — 온보딩 ready SSOT.

    기존 active_cred_ok(주식 슬롯만)을 자산군 슬롯 집합으로 일반화한다. 주식 없이 선물만
    등록해도 ready=True(선물 단독 온보딩). 주식 보유 사용자는 종전과 동일(주식 있으면 True)."""
    if broker == "ls":
        return bool(load_ls() or load_ls_futures() or load_ls_overseas_futures())
    return bool(load_kis() or load_kis_futures() or load_kis_overseas_futures())


def active_cred_ok() -> bool:
    """현재 활성 브로커의 자격증명이 하나라도 등록됐는지 — 단일 브로커 'broker ready' SSOT.

    기존 KIS 사용자(주식 보유)는 종전과 동일(불변식). 선물 단독 등 신규 조합만 새로 ready."""
    return broker_ready(get_active_broker())


def active_cred_label() -> str:
    """활성 브로커 자격증명의 UI 표시 라벨 — KIS/LS 문구 단일 출처(SSOT)."""
    return "LS증권 자격증명" if get_active_broker() == "ls" else "KIS 자격증명"


def clear() -> None:
    global _cached_device_token
    _cached_device_token = None
    for key in (_KIS, _KIS_FUT, _KIS_OVF, _LS, _LS_FUT, _LS_OVF, _BROKER_CHOICE, _DEVICE):
        try:
            keyring.delete_password(KEYRING_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass

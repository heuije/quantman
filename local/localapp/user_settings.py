"""사용자 환경설정 (평문 JSON) — 민감정보 X.

KIS API 키·계좌번호·플랫폼 토큰은 keyring 전용.
현재 동작 옵션 없음 — v0.8.3에서 us_realtime_enabled를 추가했으나 v0.8.4에서
KIS spec 재확인 후 D-prefix가 무료 자동 실시간임을 확인, toggle 불필요해 제거.
향후 다른 사용자 환경설정 추가용으로 module 골격은 유지.
"""

from __future__ import annotations

import json
import logging

from .config import USER_SETTINGS_PATH

log = logging.getLogger("localapp.user_settings")

_DEFAULTS: dict = {}


def load() -> dict:
    """현재 설정 dict. 파일 없거나 파싱 실패 시 _DEFAULTS."""
    if not USER_SETTINGS_PATH.exists():
        return dict(_DEFAULTS)
    try:
        data = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("user_settings 파싱 실패 (default 사용): %s", e)
        return dict(_DEFAULTS)
    # 모르는 key는 무시, 알려진 key 누락은 default로 채움
    merged = dict(_DEFAULTS)
    for k in _DEFAULTS:
        if k in data:
            merged[k] = data[k]
    return merged


def save(patch: dict) -> dict:
    """기존 설정에 patch 병합 후 저장. 반환 = 저장된 최종 dict."""
    cur = load()
    cur.update({k: v for k, v in patch.items() if k in _DEFAULTS})
    USER_SETTINGS_PATH.write_text(
        json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    return cur


def get(key: str) -> object:
    return load().get(key, _DEFAULTS.get(key))

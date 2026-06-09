"""자동매매 스케줄러 상태 — running / paused / stopped.

웹↔로컬 실시간 동기화용. GUI가 시작·일시정지·재개·정지 시 기록하고, 모든
snapshot push(`sync_client.push_snapshot`)가 이 값을 실어 보내 웹이 현재 자동매매
상태를 즉시 표시한다. kill-switch와 같은 파일 기반 단순 영속(단일 인스턴스 가정·
서버 재시작에도 유지). 파싱 실패/부재 시 "stopped"로 보수적 fallback.
"""

from __future__ import annotations

import json
import logging

from .config import AUTO_STATE_PATH
from .state_store import save_json

log = logging.getLogger("localapp.auto_state")

VALID = ("running", "paused", "stopped")


def load() -> str:
    """현재 자동매매 상태 문자열 반환. 없으면 'stopped'."""
    if not AUTO_STATE_PATH.exists():
        return "stopped"
    try:
        status = json.loads(AUTO_STATE_PATH.read_text(encoding="utf-8")).get("status")
        return status if status in VALID else "stopped"
    except Exception as e:
        log.warning("auto_state 파싱 실패, stopped로 간주: %s", e)
        return "stopped"


def set_status(status: str) -> None:
    """상태 기록. 유효하지 않은 값은 무시(방어). 원자적 저장 + owner-only ACL."""
    if status not in VALID:
        log.warning("알 수 없는 auto_state: %s (무시)", status)
        return
    save_json(AUTO_STATE_PATH, {"status": status})

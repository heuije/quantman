"""기업개요 저장소 — 전 종목 프로필(설립일·대표이사·홈페이지·종업원수)을 JSON에 저장.

실시간으로 매번 부르지 않고 저장본을 서빙(빠름). 출처: FnGuide 기업정보(krdata.profile,
전자공시 기반 핵심정보). 분기(90일) 1회 갱신 — refresh_if_stale를 cron/기동 시 호출한다.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime

from . import krdata

_log = logging.getLogger("app.company_profiles")
_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "company_profiles.json")
_REFRESH_DAYS = 90   # 분기 1회

_EMPTY = {"established": "", "homepage": "", "ceo": "", "employees": ""}


def _load() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"updated": "", "profiles": {}}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _PATH)


def get(code: str) -> dict:
    """저장된 기업개요(없으면 빈 dict)."""
    return _load()["profiles"].get(str(code).strip()) or dict(_EMPTY)


def refresh_all(codes: list[str]) -> dict:
    """모든 종목 프로필을 FnGuide에서 수집해 저장(병렬). 반환: 저장 데이터."""
    from concurrent.futures import ThreadPoolExecutor
    profiles: dict = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for code, p in zip(codes, ex.map(krdata.profile, codes)):
            profiles[code] = p
    data = {"updated": date.today().isoformat(), "profiles": profiles}
    _save(data)
    _log.info("기업개요 %d개 수집·저장 완료", len(profiles))
    return data


def is_stale() -> bool:
    u = _load().get("updated", "")
    if not u:
        return True
    try:
        return (date.today() - datetime.strptime(u, "%Y-%m-%d").date()).days >= _REFRESH_DAYS
    except Exception:
        return True


def refresh_if_stale(codes: list[str]) -> None:
    """저장본이 분기(90일) 이상 오래됐으면 갱신. cron/기동 시 호출."""
    if is_stale():
        try:
            refresh_all(codes)
        except Exception as e:
            _log.warning("분기 기업개요 갱신 실패: %s", e)

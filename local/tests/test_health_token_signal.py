"""KIS 토큰 만료 건강신호 — 다중엔트리 캐시 정확 읽기 (2026-07-13 결함 수정 회귀 가드).

결함: 옛 analytics.local_health는 top-level `expires_at`을 읽어 **항상 None**이었다(kis_broker는
캐시를 {fp: {access_token, expires_at}} 다중엔트리로 write). 비교도 naive-local 캐시를 UTC로
오해석했다. 수정 후: 가장 먼저 만료되는 토큰 기준 + kis_broker와 동일한 naive-local 비교.
"""
import json
from datetime import datetime, timedelta

from localapp import analytics


def _write_cache(entries: dict):
    analytics._KIS_TOKEN_CACHE.write_text(json.dumps(entries), encoding="utf-8")


def _clear_cache():
    if analytics._KIS_TOKEN_CACHE.exists():
        analytics._KIS_TOKEN_CACHE.unlink()


def test_reads_multi_entry_and_uses_soonest():
    now = datetime.now()
    exp_soon = (now + timedelta(hours=1)).isoformat()   # 가장 먼저 만료
    exp_late = (now + timedelta(hours=10)).isoformat()
    _write_cache({
        "fp_order": {"access_token": "a", "expires_at": exp_soon},
        "fp_quote": {"access_token": "b", "expires_at": exp_late},
    })
    try:
        h = analytics.local_health()
    finally:
        _clear_cache()
    assert h["kis_token_expires_at"] == exp_soon         # 옛 버전은 None이었음
    assert any("2시간 이내" in w for w in h["warnings"])   # 1h < 2h → 경고 발화


def test_expired_token_warns():
    now = datetime.now()
    _write_cache({"fp": {"access_token": "a",
                         "expires_at": (now - timedelta(minutes=5)).isoformat()}})
    try:
        h = analytics.local_health()
    finally:
        _clear_cache()
    assert any("만료 — 재발급" in w for w in h["warnings"])


def test_healthy_far_expiry_no_token_warning():
    now = datetime.now()
    exp = (now + timedelta(hours=10)).isoformat()
    _write_cache({"fp": {"access_token": "a", "expires_at": exp}})
    try:
        h = analytics.local_health()
    finally:
        _clear_cache()
    assert h["kis_token_expires_at"] == exp
    assert not any("만료" in w for w in h["warnings"])


def test_old_single_entry_format_is_graceful_none():
    """구버전 단일 엔트리(top-level access_token) — 크래시 없이 None(kis_broker도 무시·재발급)."""
    now = datetime.now()
    _write_cache({"access_token": "a",
                  "expires_at": (now + timedelta(hours=1)).isoformat(), "fp": "x"})
    try:
        h = analytics.local_health()
    finally:
        _clear_cache()
    # top-level 값은 문자열(dict 아님) → 스킵 → None (옛 형식 안전 무시)
    assert h["kis_token_expires_at"] is None


def test_local_health_emits_broker_ready_flag():
    """P2: local_health가 broker_ready(bool)·active_broker를 emit — 서버 C5가 소비.

    값은 머신의 실제 keyring 자격증명 상태를 반영(keyring은 QP_LOCAL_DIR로 격리 안 됨) —
    값 자체가 아니라 **emit 여부·타입**을 검증한다. 계좌번호·자격증명 실값은 미노출(bool만).
    """
    _clear_cache()
    h = analytics.local_health()
    assert isinstance(h["broker_ready"], bool)     # 값 아닌 bool 플래그만 노출(보안경계)
    assert h["active_broker"] in ("kis", "ls")

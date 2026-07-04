"""Q2+Q8 회귀 — market_calendar의 사용자 캐시 우선 + check_fresh.

설계(2026-05-23):
- 사용자 캐시(~/.quantman/calendars/) 우선 로드, 번들(quant_core/calendars/)
  fallback. 서버에서 일일 pull한 최신 캘린더가 사용자 캐시에 저장됨.
- check_fresh(market, today, lookahead_days=7): 캘린더 만료 임박 시 (False, msg)
  반환. raise X. 호출자(runner)가 로그만 남기고 사이클은 계속 진행 (AL-3).

검증:
1. USER_CACHE_DIR를 monkeypatch.setattr로 tmp 격리 (isolated_cache fixture)
2. 사용자 캐시 있으면 그것이 로드됨 (번들과 다른 내용으로 검증)
3. 사용자 캐시 없으면 번들 fallback
4. 사용자 캐시 손상 시 번들 fallback (조용히 무시 아님, 명시적 폴백)
5. check_fresh: 충분한 lookahead면 True, 부족하면 False+msg
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_CORE_DIR = Path(__file__).resolve().parent.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))


def _write_sessions_json(path: Path, market: str,
                          first: str, last: str,
                          extra_sessions: dict | None = None) -> None:
    """테스트용 sessions JSON 생성. first/last 사이 평일을 임의로 추가."""
    sessions = {first: ["09:00", "15:30"], last: ["09:00", "15:30"]}
    if extra_sessions:
        sessions.update(extra_sessions)
    tz = "Asia/Seoul" if market == "KR" else "America/New_York"
    data = {
        "market": market,
        "calendar": "XKRX" if market == "KR" else "XNYS",
        "tz_local": tz,
        "range": [first, last],
        "sessions": sessions,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def isolated_cache(monkeypatch, tmp_path):
    """USER_CACHE_DIR를 tmp로 격리. _load lru_cache도 무효화.

    모듈을 reload하지 않고 USER_CACHE_DIR 속성만 monkeypatch한다. reload는 공유
    모듈 객체를 전역 변형하는데, USER_CACHE_DIR는 import 시점에 한 번 계산되므로
    monkeypatch가 거둬가는 env 변수와 달리 teardown 후에도 삭제된 tmp 경로를
    그대로 가리킨다 — 뒤이어 같은 모듈을 쓰는 다른 테스트 파일을 오염시키는
    순서 의존 플레이키의 근원. setattr는 teardown 시 원래 값으로 자동 복원된다.
    """
    from quant_core import market_calendar
    user_dir = tmp_path / "user_cache"
    monkeypatch.setattr(market_calendar, "USER_CACHE_DIR", user_dir)
    market_calendar._load.cache_clear()
    yield user_dir, market_calendar
    market_calendar._load.cache_clear()


def test_user_cache_takes_priority(isolated_cache):
    """사용자 캐시 커버 구간은 사용자 캐시가 권위(번들 덮어씀), 그 밖은 번들 보존.

    _load는 번들(넓은 과거~2030)과 사용자 캐시를 병합한다: [u_lo,u_hi] 구간은
    사용자 캐시가 이기고, 그 밖(여기선 미래 2026~2030)은 번들이 채운다.
    """
    user_dir, mc = isolated_cache
    # 사용자 캐시: [2024-01-02, 2025-12-31] 구간. 번들에 실재하는 2024-01-02
    # (반일장 ['10:00','15:30'])을 다른 값으로 덮어쓴다.
    _write_sessions_json(user_dir / "kr_sessions.json", "KR",
                          "2024-01-02", "2025-12-31",
                          extra_sessions={"2024-01-02": ["11:11", "12:22"]})
    cal = mc._load("KR")
    # 겹치는 날은 사용자 캐시가 우선 — 번들의 ['10:00','15:30']이 아니라 사용자 값
    assert cal["sessions"]["2024-01-02"] == ["11:11", "12:22"]
    # 사용자 캐시 구간 밖(미래)의 번들 세션은 병합으로 보존됨
    assert "2030-12-30" in cal["sessions"]


def test_bundle_fallback_when_user_cache_missing(isolated_cache):
    """사용자 캐시 없으면 번들 fallback (quant_core/calendars/krx_sessions.json)."""
    user_dir, mc = isolated_cache
    # 사용자 캐시 미생성. 번들의 실제 데이터를 로드.
    cal = mc._load("KR")
    # 번들엔 2024년 데이터가 있어야 함
    assert any(d.startswith("2024-") for d in cal["sorted_days"])


def test_corrupted_user_cache_falls_back_to_bundle(isolated_cache, caplog):
    """사용자 캐시 손상(JSON 깨짐) → 번들 fallback + 명시적 경고 로그."""
    user_dir, mc = isolated_cache
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "kr_sessions.json").write_text("not json{", encoding="utf-8")

    import logging
    with caplog.at_level(logging.WARNING, logger="quant_core.market_calendar"):
        cal = mc._load("KR")
    # 번들 fallback 성공
    assert "sessions" in cal
    assert len(cal["sessions"]) > 0
    # 경고 로그 발생 — 조용히 무시하지 않음
    assert any("손상" in rec.message for rec in caplog.records)


def test_check_fresh_returns_true_with_horizon(isolated_cache):
    """오늘 + 7일 안에 마지막 세션이 있으면 True."""
    user_dir, mc = isolated_cache
    today = date(2026, 5, 23)
    # 오늘 + 30일 후까지 세션 있음 → fresh
    far = (today + timedelta(days=30)).isoformat()
    _write_sessions_json(user_dir / "kr_sessions.json", "KR",
                          today.isoformat(), far)
    fresh, msg = mc.check_fresh("KR", today, lookahead_days=7)
    assert fresh is True
    assert msg == ""


def test_check_fresh_returns_false_when_expiring(isolated_cache, monkeypatch):
    """오늘 + 7일 안에 마지막 세션이 없으면 False + 설명 msg.

    번들엔 2030년까지 세션이 있어 병합하면 캘린더가 절대 만료되지 않는다. 만료
    경로를 검증하려면 번들을 비활성화해 짧은 사용자 캐시가 전체 캘린더가 되게 한다.
    """
    user_dir, mc = isolated_cache
    monkeypatch.setattr(mc, "_BUNDLE_FILES",
                        {**mc._BUNDLE_FILES, "KR": mc._BUNDLE_DIR / "__nonexistent__.json"})
    today = date(2026, 5, 23)
    # 마지막 세션이 오늘 + 3일 (lookahead 7일 미충족)
    near = (today + timedelta(days=3)).isoformat()
    _write_sessions_json(user_dir / "kr_sessions.json", "KR",
                          today.isoformat(), near)
    fresh, msg = mc.check_fresh("KR", today, lookahead_days=7)
    assert fresh is False
    assert "만료 임박" in msg
    assert near in msg


def test_check_fresh_handles_missing_market(isolated_cache, monkeypatch):
    """없는 시장 코드 → (False, 사유)."""
    user_dir, mc = isolated_cache
    today = date(2026, 5, 23)
    fresh, msg = mc.check_fresh("BOGUS", today, lookahead_days=7)
    assert fresh is False
    assert "BOGUS" in msg or "지원" in msg or "로드 실패" in msg

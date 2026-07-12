"""cron refresh 함수 회귀 — PR #325 함수 삽입 위치 실수의 재발 방지.

실측 발견(2026-07-12, Railway 로그): PR #325(d46815c)가 _refresh_bonds를
_refresh_financials의 refresh_all()과 clear_cache() **사이**에 삽입해,
① financials.clear_cache()가 _refresh_bonds 꼬리로 편입 → financials 미정의
   NameError(F821)로 bonds_daily가 수집 완료 직후 매번 실패, _run_with_retry가
   backoff[5,15,30,60,120]분으로 FRED/MOF/ECB 전체 수집을 최대 5회 중복 실행.
② _refresh_financials는 의도한 당일 메모리 캐시 무효화를 조용히 상실 — 저녁
   재무 갱신 후에도 낮에 캐시된 옛 재무제표가 자정(_day 키 롤오버)까지 서빙.
이 테스트는 두 결함을 각각 직접 회귀-검증한다(외부 fetch는 전부 monkeypatch).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import main as appmain  # noqa: E402
from app import financials as fin  # noqa: E402


def test_refresh_bonds_completes_without_nameerror(monkeypatch):
    """bonds_daily 본체 — 수집 성공 후 예외 없이 완주해야 한다(NameError 회귀 가드)."""
    monkeypatch.setattr("quant_core.data.feeds.bonds.refresh_all",
                        lambda: {"US": 1, "JP": 1, "EU": 1, "KR": 1, "CN": 1})
    appmain._refresh_bonds()   # 결함 시 NameError: name 'financials' is not defined


def test_refresh_financials_invalidates_memory_cache(monkeypatch):
    """재무 일괄 갱신 후 lru 메모리 캐시가 비워져 새 저장본이 즉시 서빙돼야 한다."""
    monkeypatch.setattr(fin, "_load_or_fetch",
                        lambda code: {"fetched": "x", "annual": {}, "quarterly": {}})
    monkeypatch.setattr(fin, "refresh_all", lambda tickers: {"ok": True})
    monkeypatch.setattr(appmain, "_industry_tickers", lambda: ["000001"])
    fin._cached.cache_clear()
    try:
        fin._cached("000001", "2026-01-01")            # 낮 동안 캐시된 옛 재무제표 재현
        assert fin._cached.cache_info().currsize == 1
        appmain._refresh_financials()
        assert fin._cached.cache_info().currsize == 0  # 갱신 직후 무효화돼야 함
    finally:
        fin._cached.cache_clear()

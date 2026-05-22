"""스크리너 API — 종목 자동 선택.

엔드포인트:
  GET  /screener/presets             — 사용 가능한 프리셋 카탈로그
  POST /screener/preset/{key}/run    — 프리셋 실행 → 매칭 종목 리스트
  POST /screener/run                 — 사용자 정의 ScreenerSpec 실행

스크리너 입력은 공개 시세성 데이터로만 동작하므로 인증 불필요 (V1).
악용 가능성 낮음 (메모리 캐시 read-only).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import krx_cache, screener

router = APIRouter(prefix="/screener", tags=["screener"])


def _as_of() -> str | None:
    """현재 스크리너 데이터의 기준일(YYYY-MM-DD). UI가 '5/22(금) 기준' 표기에 사용."""
    return krx_cache.get_status().get("snapshot_date")


@router.get("/presets")
def get_presets():
    """프리셋 카탈로그 + 편집용 spec + 데이터 기준일."""
    return {"presets": screener.list_presets(), "as_of": _as_of()}


@router.get("/fields")
def get_fields():
    """커스터마이징 UI용 필드 카탈로그."""
    return {"fields": screener.field_catalog()}


@router.post("/preset/{key}/run")
def run_preset(key: str):
    """프리셋 실행 → 매칭 종목 리스트."""
    try:
        matches = screener.run_preset(key)
    except screener.ScreenerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"preset": key, "count": len(matches), "matches": matches,
            "as_of": _as_of()}


@router.post("/run")
def run_custom(spec: dict):
    """사용자 정의 ScreenerSpec 실행 → 매칭 종목 + 기준일."""
    try:
        parsed = screener.parse_spec(spec)
        matches = screener.run(parsed)
    except screener.ScreenerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"count": len(matches), "matches": matches, "as_of": _as_of()}

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

from .. import screener

router = APIRouter(prefix="/screener", tags=["screener"])


@router.get("/presets")
def get_presets():
    """프리셋 카탈로그."""
    return {"presets": screener.list_presets()}


@router.post("/preset/{key}/run")
def run_preset(key: str):
    """프리셋 실행 → 매칭 종목 리스트."""
    try:
        matches = screener.run_preset(key)
    except screener.ScreenerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"preset": key, "count": len(matches), "matches": matches}


@router.post("/run")
def run_custom(spec: dict):
    """사용자 정의 ScreenerSpec 실행."""
    try:
        parsed = screener.parse_spec(spec)
        matches = screener.run(parsed)
    except screener.ScreenerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"count": len(matches), "matches": matches}

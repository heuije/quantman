"""서버 dataset 배포 라우터 — 로컬앱이 단일 진실 공급원으로 pull.

Phase 29: 로컬앱이 더 이상 yfinance/FDR을 직접 호출하지 않고 서버에서 pull.
서버가 cron으로 갱신한 parquet을 manifest + symbol bytes 두 endpoint로 제공.
기기 토큰 인증 — 사용자 자격증명·KIS 키와 무관한 안전 정보.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from ..deps import get_current_device
from ..models import Device

_log = logging.getLogger("app.dataset")

router = APIRouter(prefix="/dataset", tags=["dataset"])


def _data_dir() -> Path:
    """data_fetcher의 DATA_DIR를 동적으로 조회 (서버·로컬앱 환경변수 다를 수 있음)."""
    from quant_core import data_fetcher
    return data_fetcher.DATA_DIR


@router.get("/manifest")
def manifest(device: Device = Depends(get_current_device)) -> dict:
    """가용 종목 목록과 각 종목의 마지막 데이터 일자·행수를 반환.

    로컬앱이 자기 캐시와 비교해 변경된 종목만 선택적으로 다운로드한다.
    """
    from quant_core import data_fetcher

    base_dir = _data_dir()
    items: list[dict] = []

    def _add(key: str) -> None:
        p = base_dir / f"{key.replace('/', '_')}.parquet"
        if not p.exists():
            return
        try:
            df = pd.read_parquet(p)
        except Exception as e:
            _log.warning("manifest: parquet 읽기 실패 %s: %s", p, e)
            return
        if df.empty:
            return
        items.append({
            "key": key,
            "n_rows": len(df),
            "last_date": str(df.index[-1])[:10],
        })

    # 매크로/자산 (ALL_SYMBOLS) + 사용자 종목 + 자동 관리 한국·해외
    for sym in data_fetcher.ALL_SYMBOLS:
        _add(sym)
    for stock in data_fetcher.load_user_stocks():
        _add(stock["name"])
    for code in data_fetcher.load_managed_kr_codes():
        _add(code)
    for stock in data_fetcher.load_managed_overseas():
        _add(stock["code"])

    return {"symbols": items, "count": len(items)}


@router.get("/{key:path}")
def get_symbol(key: str, device: Device = Depends(get_current_device)) -> Response:
    """단일 종목의 parquet bytes 반환. 로컬앱이 manifest 비교 후 변경분만 다운로드.

    경로 파라미터에 슬래시 가능 종목명도 있을 수 있어 :path 컨버터 사용.
    """
    safe_key = key.replace("/", "_")
    p = _data_dir() / f"{safe_key}.parquet"
    if not p.exists():
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"'{key}' dataset에 없습니다.")
    try:
        body = p.read_bytes()
    except Exception as e:
        _log.error("parquet 읽기 실패 %s: %s", p, e)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "parquet 읽기 실패")
    # 일봉이라 캐시 가능 — ETag로 byte 단위 변경 시만 재다운로드
    etag = f'"{p.stat().st_mtime_ns}-{len(body)}"'
    return Response(
        content=body, media_type="application/octet-stream",
        headers={"ETag": etag, "Cache-Control": "private, max-age=300"},
    )

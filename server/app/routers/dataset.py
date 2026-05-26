"""서버 dataset 배포 라우터 — 로컬앱이 단일 진실 공급원으로 pull.

Phase 29: 로컬앱이 더 이상 yfinance/FDR을 직접 호출하지 않고 서버에서 pull.
서버가 cron으로 갱신한 parquet을 manifest + symbol bytes 두 endpoint로 제공.
기기 토큰 인증 — 사용자 자격증명·KIS 키와 무관한 안전 정보.
"""

from __future__ import annotations

import hashlib
import logging
import tarfile
import threading
import time
from pathlib import Path

import pandas as pd
import zstandard
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, Response

from ..deps import get_current_device
from ..models import Device

_log = logging.getLogger("app.dataset")

router = APIRouter(prefix="/dataset", tags=["dataset"])

# 메모리 캐시 딕셔너리: key -> {"mtime": float, "size": int, "n_rows": int, "last_date": str}
_MANIFEST_CACHE: dict[str, dict] = {}

# Phase 58-C — Dataset bundle (tar.zst).
# Railway disk ephemeral 가정. main.py가 dataset update cron 끝에서 호출한다.
# /srv/core/data 옆에 임시 bundle 파일 + 메타(ETag).
_BUNDLE_LOCK = threading.Lock()


def _bundle_path() -> Path:
    """bundle 파일 경로 — DATA_DIR과 같은 폴더에 dataset-bundle.tar.zst."""
    return _data_dir().parent / "dataset-bundle.tar.zst"


def _bundle_meta_path() -> Path:
    return _data_dir().parent / "dataset-bundle.meta"


def build_bundle() -> dict:
    """모든 parquet을 tar.zst로 묶고 ETag(md5) 저장. dataset update cron 끝에서 호출.

    동시 호출 lock — 패키징 중 다른 호출이 같은 작업 안 하도록.
    """
    with _BUNDLE_LOCK:
        base = _data_dir()
        if not base.exists():
            _log.warning("bundle: DATA_DIR 없음 (%s) — skip", base)
            return {"ok": False, "reason": "DATA_DIR 없음"}
        bp = _bundle_path()
        tmp = bp.with_suffix(".tmp")
        t0 = time.time()
        n_files = 0
        # tar.zst — 메모리에서 tar stream을 zstd로 압축. level 3은 빠르고 적당한 압축률.
        cctx = zstandard.ZstdCompressor(level=3, threads=-1)
        with open(tmp, "wb") as f, cctx.stream_writer(f) as zw, \
                tarfile.open(fileobj=zw, mode="w|") as tar:
            for p in sorted(base.glob("*.parquet")):
                tar.add(p, arcname=p.name)
                n_files += 1
        size_mb = tmp.stat().st_size / 1024 / 1024
        # ETag = md5 of bundle (강력함, 안전한 cache invalidation)
        h = hashlib.md5()
        with open(tmp, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        etag = h.hexdigest()
        tmp.replace(bp)   # atomic rename
        _bundle_meta_path().write_text(etag, encoding="utf-8")
        elapsed = time.time() - t0
        _log.info("dataset bundle 갱신: %d files, %.1f MB, etag=%s, %.1fs",
                  n_files, size_mb, etag[:12], elapsed)
        return {"ok": True, "n_files": n_files, "size_mb": size_mb,
                "etag": etag, "elapsed_sec": elapsed}


def _current_bundle_etag() -> str | None:
    p = _bundle_meta_path()
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return None


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
    global _MANIFEST_CACHE

    base_dir = _data_dir()
    items: list[dict] = []

    def _add(key: str) -> None:
        p = base_dir / f"{key.replace('/', '_')}.parquet"
        if not p.exists():
            return
        try:
            stat = p.stat()
            mtime = stat.st_mtime
            size = stat.st_size
        except Exception:
            return

        # 캐시된 정보가 있고 파일이 수정되지 않았다면 디스크 읽기를 생략
        cached = _MANIFEST_CACHE.get(key)
        if cached and cached["mtime"] == mtime and cached["size"] == size:
            items.append({
                "key": key,
                "n_rows": cached["n_rows"],
                "last_date": cached["last_date"],
            })
            return

        try:
            df = pd.read_parquet(p)
            if df.empty:
                return
            n_rows = len(df)
            last_date = str(df.index[-1])[:10]

            # 캐시 업데이트
            _MANIFEST_CACHE[key] = {
                "mtime": mtime,
                "size": size,
                "n_rows": n_rows,
                "last_date": last_date,
            }

            items.append({
                "key": key,
                "n_rows": n_rows,
                "last_date": last_date,
            })
        except Exception as e:
            _log.warning("manifest: parquet 읽기 실패 %s: %s", p, e)
            return

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


@router.get("/bundle")
def get_bundle(request: Request, device: Device = Depends(get_current_device)):
    """Phase 58-C — dataset 전체 tar.zst 단일 파일 다운로드.

    매일 dataset update cron이 build_bundle()으로 packaging. 로컬앱이 ETag 비교로
    변경 시만 다운로드 → 종목별 4445 req 직렬 다운로드(~114분) → 단일 ~1분으로 단축.

    fallback: bundle 없으면 410 Gone — 로컬앱은 기존 manifest 경로로 폴백.
    """
    bp = _bundle_path()
    etag_val = _current_bundle_etag()
    if not bp.exists() or not etag_val:
        raise HTTPException(
            status.HTTP_410_GONE,
            "dataset bundle 미준비 — 다음 cron 갱신 후 가능")
    etag = f'"{etag_val}"'
    # If-None-Match 헤더로 클라가 같은 ETag 들고 있으면 304
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return FileResponse(
        bp, media_type="application/zstd",
        filename="dataset-bundle.tar.zst",
        headers={"ETag": etag, "Cache-Control": "private, max-age=0"},
    )


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

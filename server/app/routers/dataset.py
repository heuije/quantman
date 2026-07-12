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

# Phase 58-C — Dataset bundle (tar.zst). main.py가 dataset refresh 완료 시 호출한다.
_BUNDLE_LOCK = threading.Lock()

# 번들 스코프 → 포함 서브디렉터리. top-level *.parquet + _classification/_listing은 공통.
#   trading — 자동매매 로컬앱이 실제 소비하는 것만(price + 펀더멘털). 배포된 로컬앱의
#             다운로드 대상 = 이 스코프. 파일명·내용 불변(하위호환).
#   full    — 위 + 서버 챗봇 전용 피드(flow·시총·공매도·13F). dev 테스트환경(localhost
#             챗봇)이 프로덕션 볼륨과 동일 데이터로 pull하는 스코프. 로컬앱은 이 피드를
#             소비하지 않으므로 trading에 넣지 않는다(불필요 다운로드·번들 비대 방지).
_BUNDLE_SUBDIRS: dict[str, tuple[str, ...]] = {
    "trading": ("fundamentals",),
    "full": ("fundamentals", "flow", "marketcap", "short_volume", "shortbal", "institutional"),
}


def _bundle_stem(scope: str) -> str:
    # trading은 기존 파일명(dataset-bundle) 유지 — 배포된 로컬앱 하위호환.
    return "dataset-bundle" if scope == "trading" else f"dataset-bundle-{scope}"


def _bundle_path(scope: str = "trading") -> Path:
    """bundle 파일 경로 — DATA_DIR **안**(영속 볼륨 위)에 dataset-bundle[-<scope>].tar.zst.

    Railway 영속 볼륨은 DATA_DIR(/srv/data)에 마운트돼 있는데, 이전 코드는
    bundle을 `DATA_DIR.parent`(=/srv, 컨테이너 휘발 영역)에 둬 **재배포마다
    bundle만 증발** → 410 창 → 구버전 로컬앱이 manifest 폴백 grind로 추락해
    발주 사이클이 통째로 블록됐다(2026-06-10 무발주 인시던트 RC-1/D4-1,
    docs/incidents/2026-06-10-autotrading-week-retrospective.md).
    build_bundle은 *.parquet만 tar에 담으므로(.tar.zst·.meta는 비포함) DATA_DIR
    안에 둬도 자기 자신을 포함하지 않는다.
    """
    return _data_dir() / f"{_bundle_stem(scope)}.tar.zst"


def _bundle_meta_path(scope: str = "trading") -> Path:
    return _data_dir() / f"{_bundle_stem(scope)}.meta"


def build_bundle(scope: str = "trading") -> dict:
    """스코프별 parquet을 tar.zst로 묶고 ETag(md5) 저장. dataset update cron 끝에서 호출.

    scope="trading"(기본): top-level *.parquet + _classification/_listing + fundamentals/.
        자동매매 로컬앱 소비분. 파일명·내용 불변(배포된 로컬앱 하위호환).
    scope="full": 위 + flow/·marketcap/·short_volume/·institutional/(서버 챗봇 전용 피드).
        dev 테스트환경이 프로덕션 볼륨과 동일 데이터로 pull하는 스코프.

    동시 호출 lock — 패키징 중 다른 호출이 같은 작업 안 하도록.
    """
    subdirs = _BUNDLE_SUBDIRS.get(scope)
    if subdirs is None:
        raise ValueError(f"알 수 없는 bundle scope: {scope!r}")
    with _BUNDLE_LOCK:
        base = _data_dir()
        if not base.exists():
            _log.warning("bundle: DATA_DIR 없음 (%s) — skip", base)
            return {"ok": False, "reason": "DATA_DIR 없음"}
        bp = _bundle_path(scope)
        tmp = bp.with_suffix(".tmp")
        t0 = time.time()
        n_files = 0
        _log.info("dataset bundle(%s) 압축 시작", scope)   # 웹 지연 상관분석용(끝에 elapsed 로깅)
        # tar.zst — 메모리에서 tar stream을 zstd로 압축. level 3은 빠르고 적당한 압축률.
        # threads=0(단일코어) — 옛 threads=-1(전 CPU 코어)은 압축 수십초 동안 전 코어를 점유해,
        # 같은 프로세스에서 도는 웹 요청이 CPU를 못 얻어 100초+ 행이 났다(대표적 warmup 렉의
        # 근본). in-process 백그라운드 패키징이라 압축 wall-clock이 좀 늘어도 무방 — 그 대가로
        # 웹 요청에 코어를 양보해 "어떤 시간에 접속해도 빠른 응답"을 지킨다. (2026-07-07)
        cctx = zstandard.ZstdCompressor(level=3, threads=0)

        def _safe_add(tar, p, arcname) -> int:
            """압축 중 데이터엔진 cron·병렬세션이 parquet을 **원자적 교체(write_parquet_atomic의
            temp→rename, 짧은 unlink 창)**하면 glob 시점엔 있던 파일이 tar.add 시점엔 없을 수 있다
            → 그 파일만 건너뛴다(다음 빌드가 담음). 무가드면 파일 1개 사라짐이 전체 빌드를
            FileNotFoundError로 실패시킨다(테스트가 재현·2026-07-07)."""
            try:
                tar.add(p, arcname=arcname)
                return 1
            except FileNotFoundError:
                return 0

        try:
            with open(tmp, "wb") as f, cctx.stream_writer(f) as zw, \
                    tarfile.open(fileobj=zw, mode="w|") as tar:
                for p in sorted(base.glob("*.parquet")):
                    n_files += _safe_add(tar, p, p.name)
                # 정적 메타 사이드카(섹터·상장폐지일) — 로컬 get_symbol_group·매니페스트가 소비.
                for name in ("_classification.json", "_listing.json"):
                    sp = base / name
                    if sp.exists():
                        n_files += _safe_add(tar, sp, sp.name)
                # 스코프별 parquet 서브디렉터리. trading=fundamentals(SEC US·OpenDART KR,
                # 로컬 조건평가), full=+서버 챗봇 피드(flow·시총·공매도·13F). 없으면 skip(멱등).
                for sub in subdirs:
                    sdir = base / sub
                    if not sdir.exists():
                        continue
                    for p in sorted(sdir.glob("*.parquet")):
                        n_files += _safe_add(tar, p, f"{sub}/{p.name}")
            size_mb = tmp.stat().st_size / 1024 / 1024
            # ETag = md5 of bundle (강력함, 안전한 cache invalidation)
            h = hashlib.md5()
            with open(tmp, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            etag = h.hexdigest()
            tmp.replace(bp)   # atomic rename
            _bundle_meta_path(scope).write_text(etag, encoding="utf-8")
            elapsed = time.time() - t0
            _log.info("dataset bundle(%s) 갱신: %d files, %.1f MB, etag=%s, %.1fs",
                      scope, n_files, size_mb, etag[:12], elapsed)
            return {"ok": True, "scope": scope, "n_files": n_files, "size_mb": size_mb,
                    "etag": etag, "elapsed_sec": elapsed}
        except Exception:
            # 빌드 실패(디스크풀·프로세스 중단 등) 시 부분 .tmp를 남기지 않는다 — orphaned 대형
            # .tmp(full 스코프 ~1GB)가 볼륨을 잠식하는 것 방지(2026-07-07 디스크풀 인시던트 재발방지).
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def _current_bundle_etag(scope: str = "trading") -> str | None:
    p = _bundle_meta_path(scope)
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
def get_bundle(request: Request, scope: str = "trading",
               device: Device = Depends(get_current_device)):
    """Phase 58-C — dataset tar.zst 단일 파일 다운로드.

    scope="trading"(기본): 자동매매 로컬앱 소비분(price+펀더멘털). 배포된 로컬앱은
        scope 쿼리를 보내지 않으므로 항상 이 기본을 받는다(하위호환).
    scope="full": + 서버 챗봇 전용 피드(flow·시총·공매도·13F). dev 테스트환경 pull용.

    매일 dataset update cron이 build_bundle()으로 packaging. 로컬앱이 ETag 비교로
    변경 시만 다운로드 → 종목별 4445 req 직렬 다운로드(~114분) → 단일 ~1분으로 단축.

    fallback: bundle 없으면 410 Gone — 로컬앱은 기존 manifest 경로로 폴백.
    """
    if scope not in _BUNDLE_SUBDIRS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"알 수 없는 scope: {scope}")
    bp = _bundle_path(scope)
    etag_val = _current_bundle_etag(scope)
    if not bp.exists() or not etag_val:
        raise HTTPException(
            status.HTTP_410_GONE,
            f"dataset bundle({scope}) 미준비 — 다음 cron 갱신 후 가능")
    etag = f'"{etag_val}"'
    # If-None-Match 헤더로 클라가 같은 ETag 들고 있으면 304.
    # v0.9.14 fix — client(sync_client.fetch_dataset_bundle)는 응답 ETag를 strip('"')
    # 후 저장 + If-None-Match로 따옴표 없이 송신. RFC 7232상 ETag는 따옴표 포함이지만
    # 우리 client 옛 버전들이 이미 그렇게 동작하므로 서버가 양쪽 모두 수용. 미적용 시
    # client가 같은 etag 보내도 매번 mismatch → 매번 풀 다운로드(150MB, ~60~150s) →
    # 매 cycle 시작이 2~3분 지연. 실측: 5/28 22:25 미장 cycle에서 dataset 재다운로드
    # 147s로 발주 의도가에서 시초가가 ~4분 벗어났음.
    incoming = request.headers.get("if-none-match", "")
    if incoming == etag or incoming == etag_val:
        return Response(status_code=304, headers={"ETag": etag})
    return FileResponse(
        bp, media_type="application/zstd",
        filename=f"{_bundle_stem(scope)}.tar.zst",
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

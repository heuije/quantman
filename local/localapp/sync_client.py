"""플랫폼 동기화 — 로컬앱에서 아웃바운드 연결만 사용.

올리는 것: 잔고·포지션·자산곡선·체결로그 (안전정보).
받는 것: 모의/실전으로 배정된 전략 정의, dataset parquet (Phase 29 — 단일 진실 공급원).
API키·계좌번호·원시주문은 절대 전송하지 않는다.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

from .config import PLATFORM_URL, PREVIEW_CACHE_PATH, PREVIEW_CACHE_TTL_SEC
from .file_security import restrict_to_owner
from .secrets_store import load_device_token

log = logging.getLogger("localapp.sync")


def _headers() -> dict:
    token = load_device_token()
    if not token:
        raise RuntimeError("기기 페어링이 필요합니다.")
    return {"Authorization": f"Bearer {token}"}


def push_snapshot(payload: dict) -> None:
    """안전정보 스냅샷을 플랫폼에 푸시."""
    r = requests.post(f"{PLATFORM_URL}/sync/push", headers=_headers(),
                      json={"payload": payload}, timeout=15)
    r.raise_for_status()


def fetch_dataset_bundle(local_data_dir: Path) -> dict:
    """Phase 58-C — server tar.zst bundle 단일 다운로드 + 압축 해제.

    종목별 4445 req 직렬 다운로드(~114분) → 단일 파일(~140MB, 1분)으로 단축.
    ETag로 변경 시만 다운로드, 동일 ETag면 server 304 → skip.

    실패 시 ValueError raise → 호출자가 manifest fallback으로 폴백.
    """
    import io
    import tarfile

    import zstandard

    etag_cache = local_data_dir.parent / "dataset-bundle.etag"
    cached_etag = ""
    if etag_cache.exists():
        try:
            cached_etag = etag_cache.read_text(encoding="utf-8").strip()
        except Exception:
            cached_etag = ""

    headers = _headers()
    if cached_etag:
        headers["If-None-Match"] = cached_etag

    t0 = time.time()
    log.info("dataset bundle 다운로드 시도 (etag=%s)...",
             cached_etag[:12] if cached_etag else "(없음)")
    r = requests.get(f"{PLATFORM_URL}/dataset/bundle",
                     headers=headers, timeout=300, stream=True)
    if r.status_code == 304:
        log.info("dataset bundle: 변경 없음 (ETag 일치) — skip")
        return {"ok": True, "skipped": True}
    if r.status_code == 410:
        raise ValueError("server bundle 미준비")
    r.raise_for_status()
    new_etag = (r.headers.get("ETag") or "").strip('"')

    # tar.zst stream을 메모리에서 해제 → DATA_DIR 추출
    local_data_dir.mkdir(parents=True, exist_ok=True)
    dctx = zstandard.ZstdDecompressor()
    n_extracted = 0
    with dctx.stream_reader(r.raw) as zr, \
            tarfile.open(fileobj=zr, mode="r|") as tar:
        for member in tar:
            if not member.isfile() or not member.name.endswith(".parquet"):
                continue
            tar.extract(member, path=local_data_dir,
                         set_attrs=False, filter="data")
            n_extracted += 1
    if new_etag:
        etag_cache.write_text(new_etag, encoding="utf-8")
    elapsed = time.time() - t0
    log.info("dataset bundle 적용: %d parquet, %.1fs, etag=%s",
              n_extracted, elapsed, new_etag[:12])
    return {"ok": True, "skipped": False, "n_files": n_extracted,
            "elapsed_sec": elapsed}


def push_heartbeat() -> None:
    """Phase 58 — 5분 주기 alive 신호. KIS API 호출 없음(잔고 query X).

    cycle 외 시간(새벽 등)에도 server에 살아있음 통지 → 웹앱 "끊김" 표시 회피.
    페어링 안 됐거나 네트워크 실패 시 silent fail — alive 신호일 뿐 fatal 아님.
    """
    try:
        token = load_device_token()
        if not token:
            return
        r = requests.post(f"{PLATFORM_URL}/sync/heartbeat",
                          headers={"Authorization": f"Bearer {token}"},
                          timeout=10)
        if not r.ok:
            log.debug("heartbeat 실패: %s %s", r.status_code, r.text[:100])
    except Exception as e:
        log.debug("heartbeat 예외: %s", e)


def pull_strategies() -> list[dict]:
    """모의/실전으로 배정된 전략 목록을 가져온다."""
    r = requests.get(f"{PLATFORM_URL}/sync/strategies", headers=_headers(),
                     timeout=15)
    r.raise_for_status()
    return r.json()


def pull_krx_status() -> dict[str, dict]:
    """Phase 48 — KRX 종목별 거래 상태 (거래정지·관리종목 등) flag.

    매수 발주 직전 trader가 차단 판단에 사용. 실패 시 빈 dict —
    안전 fallback: status를 알 수 없으면 일반 종목으로 취급해 매수 통과.
    (KIS broker가 거래정지 종목 거부로 2차 안전망 제공)

    반환: {symbol: {"is_halt": bool, "is_managed": bool}}
    """
    try:
        r = requests.get(f"{PLATFORM_URL}/krx/status",
                         headers=_headers(), timeout=15)
        if not r.ok:
            return {}
        return (r.json() or {}).get("status", {}) or {}
    except Exception as e:
        log.warning("krx_status pull 실패 (status 차단 skip): %s", e)
        return {}


def pull_risk_limits() -> dict:
    """Phase 38.7/38.10 — 사용자별 kill switch·drawdown 한도.

    응답 필드(둘 다 nullable):
      kill_switch_daily_loss_pct, max_drawdown_pct
    실패 시 빈 dict — 호출자가 default로 fallback.
    """
    try:
        r = requests.get(f"{PLATFORM_URL}/sync/risk_limits",
                         headers=_headers(), timeout=15)
        if not r.ok:
            return {}
        return r.json() or {}
    except Exception as e:
        log.warning("risk_limits pull 실패 (default 사용): %s", e)
        return {}


def _load_preview_cache() -> dict | None:
    """Phase 41 — 마지막 성공 preview 디스크 캐시 로드 (TTL 검사).

    파일 없음·파싱 오류·TTL 초과 → None.
    """
    if not PREVIEW_CACHE_PATH.exists():
        return None
    try:
        raw = json.loads(PREVIEW_CACHE_PATH.read_text(encoding="utf-8"))
        age = time.time() - float(raw.get("cached_at", 0))
        if age > PREVIEW_CACHE_TTL_SEC:
            log.info("preview 캐시 만료 (%.1fh > %dh) — 사용 안 함",
                      age / 3600, PREVIEW_CACHE_TTL_SEC // 3600)
            return None
        return raw.get("data")
    except Exception as e:
        log.warning("preview 캐시 로드 실패: %s", e)
        return None


def _save_preview_cache(data: dict) -> None:
    """Phase 41 — 성공한 preview를 디스크에 저장 (다음 fallback 용)."""
    try:
        PREVIEW_CACHE_PATH.write_text(json.dumps({
            "cached_at": time.time(),
            "data": data,
        }, ensure_ascii=False), encoding="utf-8")
        # 잔고·후보 종목 정보가 포함되어 있어 같은 PC의 다른 사용자가 읽으면 안 됨.
        restrict_to_owner(PREVIEW_CACHE_PATH)
    except Exception as e:
        log.warning("preview 캐시 저장 실패: %s", e)


def pull_preview() -> dict | None:
    """서버 next-day preview를 가져온다 — 매수 후보 확정 정보.

    Phase 37 — 옵션 B: 08:55 메인 사이클이 매수 신호를 재평가하지 않고 서버의
    18:15 preview 결과(candidates)를 그대로 발주 대상으로 사용. 잔고·사이징은
    발주 직전 KIS 재조회로 재계산. 사용자가 미리보기에서 본 종목 = 실제 발주
    종목 일관성 보장.

    Phase 41 — 서버 일시 장애가 "preview 없음 → 신규 진입 0 → 청산만 발동"으로
    이어지지 않도록 24h 디스크 캐시 fallback. 성공 시 캐시 갱신, 실패 시 TTL
    이내 캐시 사용 + 경고 로그. 404·available=False는 캐시 fallback 없이
    None (서버가 명시적으로 preview 없음을 응답한 정상 상태).

    Returns:
      preview dict ({available, summary, by_strategy, exit_candidates, ...}) 또는
      네트워크/응답 오류 + 캐시도 만료 시 None — 호출자가 기존 청산-only 경로로.
    """
    try:
        # 디바이스 인증 엔드포인트 — 웹용 /preview/next-day(유저 JWT)가 아니라
        # /sync/preview(디바이스 토큰). 이전엔 유저 전용 엔드포인트를 호출해 항상
        # 401 → "preview 없음 → 신규 진입 0"이 되던 버그를 수정.
        r = requests.get(f"{PLATFORM_URL}/sync/preview", headers=_headers(),
                         timeout=15)
    except Exception as e:
        log.warning("preview pull 네트워크 실패: %s — 캐시 fallback 시도", e)
        cached = _load_preview_cache()
        if cached is not None:
            log.warning("preview 캐시 사용 (네트워크 장애 fallback)")
        return cached
    if r.status_code == 404:
        # 서버가 명시적으로 "preview 없음" 응답 — 캐시 fallback 안 함 (정상 상태).
        return None
    if not r.ok:
        log.warning("preview pull 응답 오류: %s — 캐시 fallback 시도", r.status_code)
        cached = _load_preview_cache()
        if cached is not None:
            log.warning("preview 캐시 사용 (서버 %s fallback)", r.status_code)
        return cached
    try:
        data = r.json() or {}
    except Exception as e:
        log.warning("preview pull JSON 파싱 실패: %s — 캐시 fallback 시도", e)
        cached = _load_preview_cache()
        if cached is not None:
            log.warning("preview 캐시 사용 (JSON 파싱 실패 fallback)")
        return cached
    if not data.get("available"):
        return None
    # 성공 — 다음 장애 fallback을 위해 캐시 저장.
    _save_preview_cache(data)
    return data


# ── Phase 29: 서버 dataset 단일 진실 공급원 pull ─────────────────────────────────

def fetch_dataset_manifest() -> list[dict]:
    """서버 dataset의 종목 manifest. [{key, n_rows, last_date}, ...]

    서버 캐싱 초기 부하 등으로 타임아웃이 발생할 수 있으므로, 제한을 120초로 상향하고 최대 3회 재시도를 구현합니다.
    """
    url = f"{PLATFORM_URL}/dataset/manifest"
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            log.info("서버 manifest 로드 시도 (%d/%d)...", attempt, max_retries)
            r = requests.get(url, headers=_headers(), timeout=120)
            r.raise_for_status()
            return r.json().get("symbols", [])
        except Exception as e:
            if attempt == max_retries:
                raise e
            sleep_time = 5 * attempt
            log.warning("서버 manifest 로드 실패 (%s). %d초 후 재시도합니다.", e, sleep_time)
            time.sleep(sleep_time)
    return []


def fetch_dataset_symbol(key: str, dest_path: Path) -> bool:
    """단일 종목 parquet을 다운로드해 dest_path에 저장. 성공 시 True.

    404는 False 반환 — 서버에 해당 종목이 아직 없는 정상 상태.
    그 외 네트워크 오류는 예외 그대로 전파.
    """
    r = requests.get(
        f"{PLATFORM_URL}/dataset/{key}", headers=_headers(), timeout=60)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(r.content)
    return True


def sync_dataset(local_data_dir: Path) -> dict:
    """서버 manifest와 로컬 parquet을 비교해 변경분만 다운로드.

    "변경분" 판정: 로컬에 parquet이 없거나, 로컬 마지막 일자가 서버보다 옛날이면 다운로드.
    날짜 비교는 문자열(YYYY-MM-DD) 직접 비교로 충분.

    실패한 종목은 skip + 로그 — 한 종목 실패가 전체 sync를 막지 않는다.
    서버 도달 자체가 실패하면 예외 그대로 던짐 → 호출자가 로컬 캐시로 fallback.
    """
    import pandas as pd

    manifest = fetch_dataset_manifest()
    n_total = len(manifest)
    n_skipped = n_pulled = n_failed = 0

    for entry in manifest:
        key = entry["key"]
        server_last = entry.get("last_date", "")
        safe_key = key.replace("/", "_")
        local_path = local_data_dir / f"{safe_key}.parquet"

        if local_path.exists():
            try:
                df_local = pd.read_parquet(local_path)
                local_last = str(df_local.index[-1])[:10] if len(df_local) else ""
            except Exception:
                local_last = ""
            if local_last and local_last >= server_last:
                n_skipped += 1
                continue

        try:
            if fetch_dataset_symbol(key, local_path):
                n_pulled += 1
            else:
                n_failed += 1
        except Exception as e:
            log.warning("dataset sync 실패 [%s]: %s", key, e)
            n_failed += 1

    log.info("dataset sync: 총 %d → 다운로드 %d · 최신 유지 %d · 실패 %d",
              n_total, n_pulled, n_skipped, n_failed)
    return {"total": n_total, "pulled": n_pulled,
            "skipped": n_skipped, "failed": n_failed}


# ── 로컬앱 → 서버 Parquet 데이터 동기화 업로드 ─────────────────────────────────────

def upload_single_parquet(file_path: Path, category: str = "price", http_session: requests.Session | None = None, headers: dict | None = None) -> bool:
    """단일 parquet 파일을 서버에 업로드. 성공 시 True.

    서버 일시적 부하 또는 프록시 502 에러에 대비하여 지수 백오프 기반 최대 3회 재시도를 수행합니다.
    """
    if headers is None:
        headers = _headers()
    url = f"{PLATFORM_URL}/sync/upload_parquet"
    params = {"category": category}
    
    client = http_session if http_session is not None else requests
    
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            with open(file_path, "rb") as f:
                files = {"file": (file_path.name, f, "application/octet-stream")}
                r = client.post(
                    url,
                    headers=headers,
                    params=params,
                    files=files,
                    timeout=60
                )
            r.raise_for_status()
            return r.json().get("ok", False)
        except Exception as e:
            if attempt == max_retries:
                raise e
            sleep_time = 2 * attempt
            log.warning("Parquet 업로드 지연/실패 [%s] (%d/%d): %s. %d초 후 재시도합니다.", 
                        file_path.name, attempt, max_retries, e, sleep_time)
            time.sleep(sleep_time)
    return False


def push_local_dataset(local_data_dir: Path, max_workers: int = 8) -> dict:
    """로컬에 축적된 parquet 파일들을 서버의 영구 저장소로 업로드 (네이버 차단 완벽 우회).

    로컬의 가격 데이터 및 펀더멘털 데이터를 비교하여 서버에 없거나 로컬 데이터가 더 최신인 경우 업로드합니다.
    """
    import pandas as pd
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 1. 서버 manifest 가져오기
    try:
        server_manifest = fetch_dataset_manifest()
        server_map = {entry["key"]: entry for entry in server_manifest}
    except Exception as e:
        log.warning("서버 manifest 로드 실패. 전체 무조건 업로드 모드로 전환합니다: %s", e)
        server_map = {}

    n_total = 0
    n_uploaded = 0
    n_skipped = 0
    n_failed = 0

    # 업로드 대상 선별
    price_files = list(local_data_dir.glob("*.parquet"))
    tasks = [] # (file_path, category)
    
    for fp in price_files:
        symbol = fp.stem
        # manifest key와 비교
        server_entry = server_map.get(symbol)
        
        need_upload = True
        if server_entry:
            try:
                # 로컬 Parquet 파일의 마지막 날짜 확인
                df_local = pd.read_parquet(fp)
                local_last = str(df_local.index[-1])[:10] if len(df_local) else ""
                server_last = server_entry.get("last_date", "")
                if local_last and server_last and local_last <= server_last:
                    need_upload = False
            except Exception:
                pass

        n_total += 1
        if not need_upload:
            n_skipped += 1
            continue
        tasks.append((fp, "price"))

    # 3. 펀더멘털 데이터 추가 (local_data_dir/fundamentals/*.parquet)
    fund_dir = local_data_dir / "fundamentals"
    if fund_dir.exists():
        fund_files = list(fund_dir.glob("*.parquet"))
        for fp in fund_files:
            n_total += 1
            tasks.append((fp, "fundamentals"))

    # 4. 멀티스레드 업로드 실행
    total_to_upload = len(tasks)
    if total_to_upload > 0:
        log.info("🚀 총 %d개의 파일을 %d개 스레드로 초고속 병렬 업로드 시작합니다...", total_to_upload, max_workers)
        
        # requests.Session 도입으로 SSL/TLS 핸드쉐이크 재사용 및 Keep-Alive 적용 (최소 10배 속도업)
        from requests.adapters import HTTPAdapter
        http_session = requests.Session()
        adapter = HTTPAdapter(pool_connections=max_workers, pool_maxsize=max_workers)
        http_session.mount("https://", adapter)
        http_session.mount("http://", adapter)
        
        # OS 자격 증명 조회는 최초 1회만 수행하여 재사용 (Windows Credential Manager 병목 차단)
        headers = _headers()
        
        def worker(item):
            fp, cat = item
            try:
                success = upload_single_parquet(fp, category=cat, http_session=http_session, headers=headers)
                return fp.name, success, None
            except Exception as e:
                return fp.name, False, str(e)

        completed_count = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task = {executor.submit(worker, task): task for task in tasks}
            for future in as_completed(future_to_task):
                fname, success, err = future.result()
                completed_count += 1
                if success:
                    n_uploaded += 1
                else:
                    n_failed += 1
                    log.warning("Parquet 업로드 실패 [%s]: %s", fname, err)
                
                # 50개 단위 또는 마지막에 진행 상황 브리핑
                if completed_count % 50 == 0 or completed_count == total_to_upload:
                    log.info(" 진행 상황: %d/%d 완료 (성공: %d, 실패: %d)", 
                             completed_count, total_to_upload, n_uploaded, n_failed)
    else:
        log.info("업로드할 신규 데이터가 없습니다. 모든 데이터가 최신 상태입니다.")

    # ── Phase 2.5: 모든 업로드 및 스킵 판정이 종료된 시점에 서버 메모리 캐시를 단 1회만 무효화하도록 신호 전송 ──
    try:
        r = requests.post(f"{PLATFORM_URL}/sync/complete", headers=_headers(), timeout=15)
        r.raise_for_status()
        log.info("✅ 서버에 동기화 완료 신호를 성공적으로 전송하여 메모리 캐시를 갱신했습니다.")
    except Exception as e:
        log.warning("⚠️ 서버 동기화 완료 알림 전송 실패 (최신 백테스트 데이터 갱신이 다소 지연될 수 있음): %s", e)

    log.info("로컬 데이터 업로드 완료: 총 %d -> 업로드 %d · 최신 유지 %d · 실패 %d",
             n_total, n_uploaded, n_skipped, n_failed)
    return {"total": n_total, "uploaded": n_uploaded, "skipped": n_skipped, "failed": n_failed}




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

# dataset 번들 다운로드 timeout — (connect, read) 튜플로 stalled stream을 fast-fail.
# read는 "청크 간 무수신 허용 시간"이므로 정상 스트리밍(연속 수신)엔 영향 없고, 소켓이
# 멈추면 ~30초 내 ReadTimeout → refresh_market_data가 캐시로 폴백. 옛 단일 timeout=300은
# stall 시 최대 5분 hang(정규 cycle 멈춤·과거 비상정지 hang 원인)이라 제거.
_BUNDLE_CONNECT_TIMEOUT_SEC = 10
_BUNDLE_READ_TIMEOUT_SEC = 30
# read timeout은 "청크 간 간격"만 제한해 저속 trickle 스트림의 총 시간은 못 막는다 —
# 서버 degraded 시 다운로드가 이론상 무한정 늘어지며 발주 사이클을 점유할 수 있어
# (2026-06-10 무발주 인시던트 D1-1 부류) 총 시간 상한을 별도로 강제한다.
# 정상 bundle은 ~1-5분(실측 282s) — 10분이면 충분한 여유.
_BUNDLE_TOTAL_TIMEOUT_SEC = 600


def _headers() -> dict:
    token = load_device_token()
    if not token:
        raise RuntimeError("기기 페어링이 필요합니다.")
    return {"Authorization": f"Bearer {token}"}


def push_snapshot(payload: dict) -> None:
    """안전정보 스냅샷을 플랫폼에 푸시.

    모든 snapshot egress의 단일 출구 — 자동매매 상태(auto_status)를 여기서 일괄 주입해
    cycle·비상청산·reconcile·상태변경 등 어느 경로로 push되든 웹이 현재 running/paused/
    stopped를 항상 받게 한다(builder마다 중복 배선 없이 한 곳에서 보장). builder가 이미
    넣었으면 보존(setdefault).
    """
    from . import __version__, auto_state
    payload.setdefault("auto_status", auto_state.load())
    # 앱 버전 — 서버 템플릿 승격 게이트(장중 템플릿 설계 §2.6)가 최신 스냅샷에서 읽어
    # "스캔 기능 없는 구앱에 템플릿 전략이 내려가는" divergence를 차단한다. 미보고(구앱)는
    # 서버가 버전 미달로 간주(fail-safe).
    payload.setdefault("app_version", __version__)
    r = requests.post(f"{PLATFORM_URL}/sync/push", headers=_headers(),
                      json={"payload": payload}, timeout=15)
    r.raise_for_status()


def fetch_dataset_bundle(local_data_dir: Path, scope: str = "trading") -> dict:
    """Phase 58-C — server tar.zst bundle 단일 다운로드 + 압축 해제.

    단일 파일(~150MB, 1분) 다운로드. ETag로 변경 시만 다운로드, 동일 ETag면
    server 304 → skip.

    scope="trading"(기본): 자동매매 로컬앱 소비분(price+펀더멘털). 배포된 로컬앱은
        이 기본으로 호출 → scope 쿼리 미전송 → 서버 wire 요청·파일명 불변(하위호환).
    scope="full": +서버 챗봇 전용 피드(flow·시총·공매도·13F). dev 테스트환경
        (pull_prod_data)이 프로덕션 볼륨과 동일 데이터로 pull할 때만 사용.

    실패 시(410 포함) 예외 raise → 호출자(datafetch)가 기존 로컬 캐시로 진행.
    종목별 manifest 폴백은 제거됨 — 2026-06-10 무발주 인시던트(D1-2) 참조.

    v0.9.5-beta — `r.raw` stream + Transfer-Encoding chunked 충돌 fix.
    이전(v0.9.0~v0.9.4)은 `dctx.stream_reader(r.raw)`로 디코드 시도. Railway
    서버가 chunked transfer encoding으로 보내면 `r.raw`에 chunk header (hex
    digit + CRLF)가 zstd magic 앞에 끼어 "Unknown frame descriptor" 영구
    실패 → 모든 사용자가 dataset 미적용 상태 (stale 로컬 캐시) → trader가
    prev_close 못 찾아 매수 0건. 임시파일 경유로 안전 디코드.
    """
    import os
    import tarfile
    import tempfile

    import zstandard

    # scope별 etag 캐시 분리 — trading은 기존 파일명 유지(하위호환), full은 별도.
    _stem = "dataset-bundle" if scope == "trading" else f"dataset-bundle-{scope}"
    etag_cache = local_data_dir.parent / f"{_stem}.etag"
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
    # trading(기본)은 params 미전송 → 배포된 로컬앱 wire 요청 불변. full만 ?scope=full.
    params = None if scope == "trading" else {"scope": scope}
    r = requests.get(
        f"{PLATFORM_URL}/dataset/bundle", headers=headers, params=params, stream=True,
        timeout=(_BUNDLE_CONNECT_TIMEOUT_SEC, _BUNDLE_READ_TIMEOUT_SEC))
    if r.status_code == 304:
        log.info("dataset bundle: 변경 없음 (ETag 일치) — skip")
        return {"ok": True, "skipped": True}
    if r.status_code == 410:
        raise ValueError("server bundle 미준비")
    r.raise_for_status()
    new_etag = (r.headers.get("ETag") or "").strip('"')

    # Step 1 — chunk stream을 임시 파일로 받음. requests의 iter_content가
    # Transfer-Encoding chunked를 transparent 해제 — payload만 .zst로 저장.
    local_data_dir.mkdir(parents=True, exist_ok=True)
    tmp_path: str | None = None
    n_extracted = 0
    failed_members: list[str] = []
    try:
        _deadline = time.monotonic() + _BUNDLE_TOTAL_TIMEOUT_SEC
        with tempfile.NamedTemporaryFile(delete=False, suffix=".zst") as tmp:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if time.monotonic() > _deadline:
                    raise TimeoutError(
                        f"bundle 다운로드 총 시간 초과({_BUNDLE_TOTAL_TIMEOUT_SEC}s)"
                        " — 저속 스트림(read timeout 미발동) 보호")
                if chunk:
                    tmp.write(chunk)
            tmp_path = tmp.name

        # Step 2 — 디스크 파일에서 안전하게 zstd 디코드 + tar 추출.
        # v0.9.6-beta — bundle 적용한 종목들을 universe(managed_kr_stocks·
        # managed_overseas_stocks)에 자동 등록. 이전엔 client load_all이 universe
        # 화이트리스트 기반인데 server bundle download 흐름과 단절 → 디스크엔
        # 4459 parquet 있어도 dataset dict에 macro 51개만 로드 → trader가
        # 매수 후보 종목(AAPL 등) prev_close 못 찾아 skip_no_data로 매수 0건.
        extracted_symbols: list[str] = []
        from quant_core.parquet_io import sanitize_fs_name
        dctx = zstandard.ZstdDecompressor()
        with open(tmp_path, "rb") as f, \
                dctx.stream_reader(f) as zr, \
                tarfile.open(fileobj=zr, mode="r|") as tar:
            for member in tar:
                if not member.isfile() or not member.name.endswith(".parquet"):
                    continue
                # per-member 격리 — 한 멤버 실패가 나머지 추출을 중단시키지 못하게 한다
                # (2026-07-13 CON.parquet 인시던트: 예약명 1개 추출 실패가 스트림 루프 전체를
                # 죽여 dataset이 거의 빈 채로 남아 발주 0). 스트리밍 tar(r|)은 seek 불가라
                # tar.extract가 target open에서 실패하면 멤버 바이트가 미소비돼 이후 멤버가
                # 깨진다 → extractfile로 **바이트를 먼저 소비**한 뒤 OS-안전 이름으로 직접 write:
                # 스트림 정렬 보존 + 예약명(CON 등) 회피를 동시에(서버 미배포 시 클라 단독 방어).
                name = member.name.replace("\\", "/")
                if name.startswith("/") or ".." in name.split("/"):
                    failed_members.append(f"{member.name}(unsafe_path)")
                    continue
                try:
                    fobj = tar.extractfile(member)
                    data = fobj.read() if fobj is not None else b""
                except Exception as e:
                    failed_members.append(f"{member.name}(read:{type(e).__name__})")
                    log.error("dataset bundle 멤버 읽기 실패(격리·계속): %s — %s", member.name, e)
                    continue
                head, _, base_name = name.rpartition("/")
                stem = Path(base_name).stem
                safe_rel = (f"{head}/" if head else "") + f"{sanitize_fs_name(stem)}.parquet"
                target = local_data_dir / safe_rel
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "wb") as out:
                        out.write(data)
                except Exception as e:
                    failed_members.append(f"{member.name}(write:{type(e).__name__})")
                    log.error("dataset bundle 멤버 저장 실패(격리·계속): %s — %s", member.name, e)
                    continue
                n_extracted += 1
                # symbol 추출 — "subdir/AAPL.parquet" → "AAPL"(원본 stem으로 universe 등록)
                if stem:
                    extracted_symbols.append(stem)
        if failed_members:
            log.error("dataset bundle: %d개 멤버 추출 실패(나머지 %d개 적용) — %s",
                      len(failed_members), n_extracted, failed_members[:10])

        # bundle 종목들을 KRX(6자리 숫자) / overseas(영문 ticker)로 분류해 universe 등록.
        # macro symbol(S&P500·달러원 등)은 ALL_SYMBOLS에 이미 있으니 overseas 등록에서 제외.
        from quant_core import data_fetcher as _df
        macro_set = set(_df.ALL_SYMBOLS)
        kr_codes, overseas = [], []
        for sym in extracted_symbols:
            if sym in macro_set:
                continue
            if sym.isdigit() and len(sym) == 6:
                kr_codes.append(sym)
            elif sym and sym[0].isalpha():
                overseas.append({"code": sym, "name": sym})
        if kr_codes:
            _df.save_managed_kr_codes(sorted(set(kr_codes)))
        if overseas:
            # code 기준 dedupe (save_managed_overseas가 내부 dedupe도 함).
            _df.save_managed_overseas(overseas)
        log.info("universe 자동 등록 — KRX %d종목 · Overseas %d종목",
                  len(kr_codes), len(overseas))
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if new_etag:
        etag_cache.write_text(new_etag, encoding="utf-8")
    elapsed = time.time() - t0
    log.info("dataset bundle 적용: %d parquet, %.1fs, etag=%s",
              n_extracted, elapsed, new_etag[:12])
    return {"ok": True, "skipped": False, "n_files": n_extracted,
            "n_failed": len(failed_members), "failed_sample": failed_members[:10],
            "elapsed_sec": elapsed}


def fetch_user_info() -> dict | None:
    """페어링된 user 정보(email)를 server에서 1회 조회.

    GUI hero에 "v0.x · email@example.com" 형태로 표시하기 위해서만 사용.
    페어링 안 됐거나 네트워크 실패 시 None — 표시만 건너뛰면 됨.
    """
    try:
        token = load_device_token()
        if not token:
            return None
        r = requests.get(f"{PLATFORM_URL}/auth/device/me",
                         headers={"Authorization": f"Bearer {token}"},
                         timeout=10)
        if not r.ok:
            return None
        return r.json()
    except Exception as e:
        log.debug("user info 조회 실패: %s", e)
        return None


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


# timeline ETag 캐시 — 변경 없으면 서버가 304(body 0) → 마지막 데이터 재사용(Neon egress 절감).
# GUI가 60s 폴링하지만 새 cycle·heartbeat가 없으면 If-None-Match로 304만 받는다.
_timeline_cache: dict = {"etag": "", "data": None}


def pull_timeline() -> dict | None:
    """서버 자동매매 timeline 조회 — GUI 풀 timeline 패널용.

    /sync/timeline (device-authed) 호출 — 어제·오늘·내일 6 종류 event +
    heartbeat 상태. 응답 형식은 /trading/timeline과 동일 (서버에서 같은
    헬퍼 재사용). 실패 시 None (caller가 마지막 표시 유지).

    egress: 서버 tag-first ETag를 캐시하고 다음 호출에 If-None-Match로 보낸다.
    변경 없으면 304(body 0) → 캐시 데이터 재사용(docs/incidents/2026-06-10-…).
    """
    headers = _headers()
    if _timeline_cache["etag"]:
        headers["If-None-Match"] = _timeline_cache["etag"]
    try:
        r = requests.get(f"{PLATFORM_URL}/sync/timeline", headers=headers,
                          timeout=10)
    except Exception as e:
        log.debug("timeline pull 네트워크 실패: %s", e)
        return None
    if r.status_code == 304:
        return _timeline_cache["data"]          # 변경 없음 — 캐시 재사용(egress 0)
    if not r.ok:
        log.debug("timeline pull 응답 오류: %s", r.status_code)
        return None
    try:
        data = r.json()
    except Exception as e:
        log.debug("timeline pull JSON 파싱 실패: %s", e)
        return None
    etag = r.headers.get("ETag")
    if etag:
        _timeline_cache["etag"] = etag
        _timeline_cache["data"] = data
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


# (제거됨) fetch_dataset_symbol·sync_dataset — bundle 410 시 종목별 manifest 폴백.
# 수만 parquet 직렬 신선도 검사(파일당 read_parquet)로 시간 단위를 소모하며
# _REFRESH_LOCK을 점유, 발주 사이클을 통째로 막았다(2026-06-10 무발주 인시던트
# D1-2 — docs/incidents/2026-06-10-autotrading-week-retrospective.md). bundle
# 실패는 이제 "기존 캐시로 진행"이 유일한 동작(datafetch 참조).
# fetch_dataset_manifest는 업로드 diff(push_local_dataset)가 계속 사용한다.


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




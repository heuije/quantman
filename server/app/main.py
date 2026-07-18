"""MyStock API 서버."""

from __future__ import annotations

import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
import time
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from quant_core.data.backfill import DateCursorBackfill
from quant_core.data.policy import CORE_FLOOR_COMPACT

from . import (calendar_cache, data_cache, kis_master_cache, krx_cache,
                naver_fundamentals, technical_cache)
from .config import settings
from .db import call_with_disconnect_retry, create_db_and_tables, prune_old_rows
from .routers import (activity as activity_router, admin as admin_router, auth, backtest,
                       calendars as calendars_router, chat as chat_router, commands,
                       dataset, ir as ir_router, ir_compile as ir_compile_router,
                       market, futures, opinions as opinions_router, portfolio,
                       preview as preview_router,
                       screener as screener_router,
                       settings as settings_router, strategies, sync,
                       trading as trading_router)

_log = logging.getLogger("app.main")

# ── 로깅 — app.* INFO를 stdout으로(Railway 캡처) ─────────────────────────────
# 미설정 시 Python 기본(lastResort)이 WARNING+만 stderr로 내보내 INFO 진단이 통째로 안
# 보인다 — get_projected 단계타이밍·xlsx export 단계(load/run/build)·chat usage 등
# #203/#159 계측이 프로덕션에서 장님이 됨(뿌리④ 3분 export의 병목을 못 짚는 직접 원인).
# app 로거에 stdout 핸들러 1개 + INFO 레벨을 명시(import당 1회 idempotent). propagate는
# 기본(True) 유지 — 프로덕션 root엔 핸들러가 없어(app WARNING이 lastResort로 나왔던 게 증거)
# 중복이 안 생기고, pytest caplog(root 전파 캡처)도 보존된다.
_app_log = logging.getLogger("app")
if not _app_log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
    _app_log.addHandler(_h)
    _app_log.setLevel(logging.INFO)

# ── Fetch 재시도 헬퍼 ────────────────────────────────────────────────────────
#
# 외부 소스(KIS/KRX/NAVER/yfinance/FRED 등)는 일시 장애가 잦다. 정시 cron이
# 한 번 실패하면 다음날까지 stale인 게 큰 문제이므로, 실패 시 자동 재시도.
#
# 정책: 시도 N회, backoff [3, 6, 10, 15, 30, 60]분(누적 ~124분 후 포기).
# 정시 cron이 다시 트리거되면 기존 retry 큐는 모두 cancel하고 다시 시작.
#
# 로드맵 B — 종전 [5,15,30,60,120]은 초반이 느슨해 07:30 글로벌 수집 실패가
# 2회 재시도(+20분) 안에 못 살아나면 아침 발주창(08:35/08:55)을 넘겼다.
# 초반 [3,6,10,15]는 선물 트랙(08:02)이 이미 운용·실측해 온 cadence와 동일 —
# 같은 포장 파이프라인이라 소요시간 근거를 공유한다. 꼬리(30·60)는 상류
# 장애 장기화 대비(정시 cron 전 회복 기회 유지).

_RETRY_BACKOFFS_MIN = [3, 6, 10, 15, 30, 60]
_RETRY_MAX_ATTEMPTS = 6

# ── 스케줄 tz 앵커 ───────────────────────────────────────────────────────────
# 모든 cron 라벨 시각은 KST. ⚠ apscheduler 3.x에서 CronTrigger "인스턴스"는
# 스케줄러 timezone을 상속하지 않는다(상속은 add_job 문자열 형식에만 적용) —
# timezone 미지정 인스턴스는 tzlocal(Railway 컨테이너=UTC)에 앵커돼 전 cron이
# +9h 시프트로 돌았다(2026-07-12 실측, docs/incidents/2026-07-12-cron-utc-anchor-9h-shift.md).
# 모든 CronTrigger에 이 값을 명시한다(회귀 가드: tests/test_scheduler_timezone.py).
_TZ_SEOUL = "Asia/Seoul"


def _run_with_retry(name: str, fn: Callable[[], object],
                     scheduler: BackgroundScheduler,
                     backoffs_min: list[int] | None = None) -> None:
    """fn을 즉시 실행, 실패 시 backoff 후 재시도 job을 scheduler에 등록.

    호출될 때마다 같은 name의 기존 retry job을 cancel — 정시 cron이 트리거되면
    이전 실패의 재시도 큐를 깨끗이 비우고 다시 시작한다.
    backoffs_min: 커스텀 백오프(분) — 발주창처럼 좁은 시간 예산의 job이 기본
    [5,15,30,60,120]보다 촘촘히 재시도해야 할 때(예: 아침 선물 [3,6,10,15]).
    """
    # 기존 retry job cancel (정시 cron이 새로 시작될 때마다 큐 비움).
    # retry job id는 고정(retry_{name}) — 시도마다 다른 id를 쓰면 max_instances=1
    # (job id별 카운트)이 중복 스폰을 못 막는다.
    try:
        scheduler.remove_job(f"retry_{name}")
    except JobLookupError:
        pass  # 대기 중 retry 없음 — 정상 (대부분의 정시 트리거)

    _backoffs = backoffs_min or _RETRY_BACKOFFS_MIN
    state = {"attempt": 0}

    def _attempt() -> None:
        state["attempt"] += 1
        try:
            fn()
            _log.info("[%s] 성공 (시도 %d)", name, state["attempt"])
        except Exception as e:
            _log.exception("[%s] 시도 %d 실패: %s", name, state["attempt"], e)
            if state["attempt"] >= _RETRY_MAX_ATTEMPTS:
                _log.error("[%s] 최대 재시도(%d) 도달 — 다음 정시 cron까지 포기",
                           name, _RETRY_MAX_ATTEMPTS)
                return
            backoff_min = _backoffs[
                min(state["attempt"] - 1, len(_backoffs) - 1)]
            # tz-aware(KST) 시각으로 생성 — scheduler가 Asia/Seoul이므로 naive를
            # 쓰면 UTC 배포(Railway)에서 과거 시각으로 해석돼 misfire drop된다.
            run_at = datetime.now(ZoneInfo(_TZ_SEOUL)) + timedelta(minutes=backoff_min)
            _log.warning("[%s] %d분 후 재시도 (#%d) — %s",
                         name, backoff_min, state["attempt"] + 1,
                         run_at.strftime("%H:%M:%S"))
            scheduler.add_job(
                _attempt, trigger="date", run_date=run_at,
                id=f"retry_{name}", replace_existing=True)

    _attempt()


def _initial_master_refresh():
    """시작 시 KIS 마스터 1회 다운로드 — 예외를 명시적으로 로그.

    daemon thread의 unhandled exception은 로거를 안 거치고 stderr로 가서 묻힐 수 있어,
    try-except로 감싸서 어떤 이유로 실패했는지 명확히 남긴다.
    """
    try:
        _log.info("KIS 마스터 초기 다운로드 시작")
        _refresh_kis_master()
    except Exception:
        _log.exception("KIS 마스터 초기 다운로드 중 예외 — 정시 cron 재시도")


# ── Raw refresh 함수 ─────────────────────────────────────────────────────────
#
# 각 외부 소스를 fetch하는 raw 함수 — 예외를 잡지 않고 그대로 전파.
# 호출자(_run_with_retry 또는 _initial_*)가 예외 처리·재시도 담당.
# 성공 시 _trigger_preview(data_source)로 모든 사용자 next-day preview 갱신.


def _trigger_preview(data_source: str) -> None:
    """데이터 갱신 직후 preview 자동 갱신 — 실패는 **예외 전파**(파이프라인 문제 8 근본수정).

    구 버전은 예외를 삼켰다: 데이터 fetch는 성공했는데 preview 갱신만 실패하면
    _run_with_retry가 재시도를 안 걸고 저장본 preview가 stale로 잔존, 로컬 pull은
    저장본이 있으면 재빌드하지 않아(sync.py stale-present) stale 그대로 발주에 쓰였다.
    이제 예외가 호출자(_run_with_retry 래핑 refresh)로 전파돼 fetch→번들→preview
    전체가 하나의 원자적 공급 단위로 재시도된다(fetch는 최근분 재수집이라 멱등).

    Phase 60 — Neon 연결 끊김(`server conn crashed?`) 시 fresh 연결로 1회 재시도는 유지.
    """
    from . import preview_engine
    from .db import call_with_disconnect_retry
    call_with_disconnect_retry(preview_engine.refresh_all_users_preview, data_source)


def _prune_db() -> None:
    """무한 성장 테이블(HeartbeatEvent·SyncSnapshot) 일일 pruning — db.prune_old_rows.

    04:00 KST는 유휴 시간이라 Neon compute가 suspend된 상태일 확률이 높다 — 첫
    쿼리의 stale 연결(`server conn crashed?`)을 call_with_disconnect_retry로 흡수
    (_trigger_preview와 동일 패턴). 삭제는 멱등이라 재시도에 안전.
    """
    result = call_with_disconnect_retry(prune_old_rows)
    _log.info("DB pruning 결과: %s", result)


def _refresh_kis_master() -> None:
    result = kis_master_cache.refresh()
    _log.info("KIS 마스터 갱신 결과: %s", result)
    # preview trigger 없음 — 종목명 lookup만 영향, 평가 결과 무변화


def _refresh_krx() -> None:
    """KRX 일별 스냅샷 갱신 — 정규장 OHLCV. 시간외 단일가는 수집하지 않음.

    15:45 정규장 마감 직후 1회만 실행. krx_cache.refresh()가 _state["metrics"]를
    통째 교체하므로 17:00 NAVER + 17:15 technical merge 후엔 절대 재호출 금지
    (in-place merge된 PER/PBR/RSI 필드가 파괴됨).
    """
    result = krx_cache.refresh()
    _log.info("KRX 스냅샷 갱신 결과: %s", result)
    # preview trigger 없음 — screener 입력 부분 갱신. 17:15 technical 완료 시 일괄 트리거


def _refresh_naver() -> None:
    result = naver_fundamentals.refresh()
    _log.info("NAVER 펀더멘털 갱신 결과: %s", result)
    # 밸류(PBR/PER)는 NAVER가 아닌 canonical OpenDART에서 — NAVER 고유필드(EPS·BPS·DPS·배당·
    # 외국인) merge 직후 같은 자리에 적재해야, 스냅샷 통째 재구축(15:45·부팅) 후에도 NAVER와 동일
    # cadence로 항상 채워진다(17:30 앵커에만 두면 부팅~17:30 사이 스크리너 밸류 공백 = 회귀).
    _materialize_kr_valuation()
    # preview trigger 없음 — 위와 동일


def _refresh_technical() -> None:
    result = technical_cache.refresh()
    _log.info("기술적 지표 갱신 결과: %s", result)
    # screener 입력(KRX+NAVER+technical)이 모두 완성된 시점 — 자동 선택 preview 트리거
    _trigger_preview("technical")


def _refresh_static_meta() -> None:
    """static.classification(섹터·업종)+static.listing(상장·폐지일) 사이드카 갱신 (FDR KRX-DESC/DELISTING).

    KR dataset 갱신(18:15) 직전 실행 → 그 invalidate가 새 사이드카로 매니페스트를 재빌드하고,
    bundle(18:30)이 사이드카를 로컬로 전파한다. 변동이 느려 일 1회로 충분.
    """
    from quant_core.data.feeds import classification, listing
    c = classification.fetch()
    ls = listing.fetch()
    _log.info("정적 메타 갱신: classification %d종목 · listing %d종목", len(c), len(ls))


def _refresh_us_fundamentals() -> None:
    """US 펀더멘털(SEC Company Facts) — S&P500 + 등록 해외종목. filing-date PIT. 주1회로 충분(분기 변동)."""
    from quant_core import data_fetcher
    from quant_core.data.feeds import fundamental_us
    tickers = sorted({t for t in (data_fetcher.sp500_yf_codes()
                                  + [s.get("code", "") for s in data_fetcher.load_managed_overseas()]) if t})
    res = fundamental_us.fetch(tickers)
    _log.info("US 펀더멘털(SEC) %d종목: %s", len(tickers), res)
    data_cache.invalidate()                      # 다음 로드 시 펀더멘털 attach


def _materialize_kr_valuation() -> None:
    """canonical OpenDART 밸류(pb_ratio·trailing_pe) 최신 단면을 스크리너 스냅샷에 적재.

    스크리너 PBR/PER을 360·백테스트와 동일 출처(OpenDART)로 일원화 — 같은 get_projected 계산을
    쓰므로 값이 구조적으로 일치(NAVER 별도 소스 제거). recent_days 소량이어도 펀더멘털은 full로
    attach되고 pb는 룩백이 없어 최신 행 값이 full 계산과 동일(get_projected 불변식). 스냅샷에 있는
    종목만 갱신, OpenDART 미적재 종목은 None(가짜 채움 0 — 정직)."""
    proj = data_cache.get_projected(["pb_ratio", "trailing_pe"], symbols=None, recent_days=10)
    updates: dict[str, dict] = {}
    for sym, df in proj.items():
        row: dict = {}
        for col, key in (("pb_ratio", "pbr"), ("trailing_pe", "per")):
            if col in df.columns:
                s = df[col].dropna()
                if len(s):
                    row[key] = float(s.iloc[-1])
        if row:
            updates[sym] = row
    n = krx_cache.merge_fields(updates)
    _log.info("스크리너 밸류 일원화(OpenDART) — %d종목 pbr/per 적재", n)


def _backfill_kr_fundamentals_chunk() -> None:
    """KR 펀더멘털(OpenDART) 증분 백필 청크 — **10분마다 짧게**(재배포 견딤).

    한 번에 크게(부팅+420초 의존) 받던 방식은 재배포 폭주에 fetch가 시작도 못 해 정체했다
    (2026-06-10 실측: 24h 0건). 짧은 청크를 자주 돌려 재배포 사이에 완료·재개되게 한다. fetch는
    멱등·미수집 우선·종목별 원자기록이라 죽어도 진행분 보존+자동 재개. 다 채우면(전부 fresh)
    콜 0으로 자연 무비용. invalidate는 비싼 재빌드라 여기서 안 하고(144회/일 폭주 방지)
    일일 attach(_refresh_kr_fundamentals 17:30·dataset_kr 18:15)가 담당."""
    from datetime import datetime
    from quant_core import data_fetcher
    from quant_core.data.feeds import fundamental_kr
    codes = data_fetcher.load_managed_kr_codes()
    yr = datetime.now().year
    # 과거 PER/밸류 이력 백필 — OpenDART 바닥 2015까지(yr-11·한 경기순환 포함). fetch_one이 종목
    # parquet을 덮어쓰므로 전체 범위를 한 번에 받아 이력을 보존한다(과거엔 [yr-1,yr]만 받아 2년치로
    # 잘려 과거 PER이 ~2년 전부터만 = 데이터 분열 뿌리①의 한 증상). 2010~14는 무료 OpenDART에 없어
    # 2015가 바닥. fresh_days=80·종목별 원자기록·OpenDART 한도(020) 자동스로틀로 한도(~20k콜/일)
    # 내에서 ~2주에 걸쳐 점진 채움.
    res = fundamental_kr.fetch(codes, list(range(yr - 11, yr + 1)), budget_calls=1500)
    if res.get("ok") or res.get("empty") or res.get("rate_limited"):   # 진행/한도 있을 때만 로그(무작업 폴 침묵)
        _log.info("KR 펀더멘털 백필 청크(OpenDART): %s", res)


def _refresh_kr_fundamentals() -> None:
    """KR 펀더멘털 일일 attach 앵커 — 실제 수집은 10분 백필 청크가 한다.

    누적된 펀더멘털 parquet를 캐시에 attach(invalidate)만 한다(다음 로드 시 반영, US와 동일)."""
    data_cache.invalidate()


# ── 시작 시 1회 초기 fetch (실패해도 다음 정시 cron이 재시도) ─────────────────

def _initial_krx_refresh():
    import time
    try:
        time.sleep(45)            # KIS 마스터 우선
        _log.info("KRX 스냅샷 초기 fetch 시작")
        _refresh_krx()
    except Exception:
        _log.exception("KRX 스냅샷 초기 fetch 중 예외 — 정시 cron 재시도")


def _initial_naver_refresh():
    import time
    try:
        time.sleep(120)
        _log.info("NAVER 펀더멘털 초기 fetch 시작")
        _refresh_naver()
    except Exception:
        _log.exception("NAVER 펀더멘털 초기 fetch 중 예외 — 정시 cron 재시도")


def _initial_kr_fundamentals_refresh():
    """부팅 직후 1회 백필 청크 — 신규 볼륨·재배포 후 빠른 첫 진행(10분 cron을 기다리지 않게).

    짧은 지연 후 한 청크를 받는다. 실패(키 미설정 등)는 부팅을 막지 않고 10분 cron이 재시도한다."""
    import time
    try:
        time.sleep(60)             # dataset 초기 갱신·마스터 이후 — 외부 호출 분산(짧게)
        _log.info("KR 펀더멘털(OpenDART) 초기 백필 청크 시작")
        _backfill_kr_fundamentals_chunk()
    except Exception:
        _log.exception("KR 펀더멘털 초기 청크 중 예외 — 10분 cron 재시도")


# ── 애널 컨센서스 — reports_kr(네이버)가 담당(옛 한경 consensus_kr 은퇴 2026-07) ──────────────
# 컨센서스 지표 패널(consensus/{code}.parquet)은 reports_kr.ingest가 원시에서 재산출한다 → **별도 cron 불필요**.
# reports_chunk/reports_daily가 목록·목표가·투자의견을 수집할 때 패널까지 함께 갱신. load_stock_consensus 소비.
# (은퇴 이유: 네이버가 커버리지 2배·대형 증권사 포함·이력 2007까지로 한경 대비 전 축 우위 — reports_kr docstring.)


# ── 애널 리포트(네이버, 무로그인) — 목록 AND 컨센서스 지표의 유일 소스(한경 consensus_kr 은퇴) ──────
# reports_kr 피드가 목록·목표가·투자의견을 수집하고 컨센서스 패널까지 재산출(ingest). 상세:
# core/quant_core/data/feeds/reports_kr.py docstring.
# 네이버는 날짜윈도우 파라미터를 무시하고 페이지 newest-first만 지원 → 날짜 cursor 대신 page cutoff-stop.
_REPORTS_FLOOR = "2010-01-01"         # 백필 바닥(CORE_FLOOR과 동일 — 컨센서스 이력 깊이)
_REPORTS_CHUNK_PAGES = 10             # 백필 1청크 페이지 수(상세 fetch 있어 작게 — 여러 날 분산·봇차단 완화)


def _reports_cache_clear() -> None:
    """서버 리포트 서빙 lru(day 키)를 비워 방금 ingest한 신규 리포트를 같은 날 즉시 반영."""
    try:
        from . import krdata
        krdata._reports_from_feed.cache_clear()
    except Exception:                 # noqa: BLE001 — 캐시 클리어 실패는 무해(day 키가 익일 자연 갱신)
        pass


def _refresh_reports() -> None:
    """일일 리포트 증분 — 최근 3일 네이버 신규 리포트 수집·상세 목표가/투자의견 부착·ingest(19:30 KST)."""
    from datetime import date, timedelta
    from quant_core.data.feeds import reports_kr
    floor = (date.today() - timedelta(days=3)).isoformat()
    reports, ok, _ = reports_kr.collect_pages(start_page=1, max_pages=60, floor=floor)
    if ok and reports:
        reports_kr.enrich(reports)                    # 소량(최근분) — 상세 목표가 부착
        res = reports_kr.ingest(reports)
        _reports_cache_clear()
        _log.info("[altdata] 리포트 증분(네이버) floor=%s: %s", floor, res)


def _backfill_reports_chunk() -> None:
    """네이버 리포트 과거 백필 — 목록 페이지를 chunked로 전진하며 상세(목표가·투자의견) 부착·적재.

    페이지 커서(reports/_backfill.json)로 재개 → 수천 페이지를 **여러 날 분산**(상세 fetch가 있어 청크는 작게).
    2010 floor 또는 빈 페이지 도달 시 done(무비용). 수집 transient 실패 시 커서 유지·재시도(dedup로 안전)."""
    import json
    from quant_core import data_fetcher
    from quant_core.data.feeds import reports_kr
    cur_path = data_fetcher.REPORTS_DIR / "_backfill.json"
    try:
        state = json.loads(cur_path.read_text(encoding="utf-8")) if cur_path.exists() else {}
    except Exception:                             # noqa: BLE001 — 손상 커서는 처음부터(dedup로 안전)
        state = {}
    if state.get("done"):
        return                                    # 백필 완료 — 무비용
    start = int(state.get("next_page", 1))
    reports, ok, done = reports_kr.collect_pages(start_page=start,
                                                 max_pages=_REPORTS_CHUNK_PAGES,
                                                 floor=_REPORTS_FLOOR)
    if reports:
        reports_kr.enrich(reports)
        reports_kr.ingest(reports)
        _reports_cache_clear()
    if not ok:                                    # transient — 커서 미전진(다음 cron 재시도)
        _log.warning("[altdata] 리포트 백필 청크 p%s~ transient — 커서 유지", start)
        return
    new_state = {"done": True} if done else {"next_page": start + _REPORTS_CHUNK_PAGES}
    cur_path.parent.mkdir(parents=True, exist_ok=True)
    cur_path.write_text(json.dumps(new_state), encoding="utf-8")
    _log.info("[altdata] 리포트 백필 청크(네이버) p%s-%s: reports=%s done=%s",
              start, start + _REPORTS_CHUNK_PAGES - 1, len(reports), done)


def _initial_reports_refresh():
    import time
    try:
        time.sleep(120)
        # 한경→네이버 컨센서스 컷오버(1회): 백필로 이미 수집된 원시에서 컨센서스 패널 재산출(재fetch 없음).
        # 없으면 백필이 지나간 종목의 패널이 옛 한경 값으로 남는다 → 플래그로 1회만.
        from quant_core import data_fetcher
        from quant_core.data.feeds import reports_kr
        flag = data_fetcher.REPORTS_DIR / "_panels_rebuilt.flag"
        if not flag.exists():
            n = reports_kr.rebuild_all_panels()
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text("1", encoding="utf-8")
            _reports_cache_clear()
            _log.info("[altdata] 컨센서스 패널 초기 재산출(네이버 원시→패널): %s종목", n)
        _log.info("리포트(네이버) 초기 증분")
        _refresh_reports()                        # 최근분 즉시 채움(과거 백필은 10분 chunk cron이 분산 수행)
    except Exception:
        _log.exception("리포트 초기 증분/재산출 예외 — cron 재시도")


# ── 기관·외국인 수급(KRX pykrx 로그인, 일별 전종목) ───────────────────────────
# 종목별 반복(get_market_trading_value_by_date·3,579콜)이 KRX MDC 봇차단을 유발 → 일별 전종목
# (get_market_net_purchases_of_equities_by_ticker·1콜/시장/투자자)로 근본 전환(로컬 blocked=0 실증).
# 수집은 marketcap_krx와 동일하게 날짜축 cursor 백필(DateCursorBackfill — 컨센서스·KRX와 동일 문법).
_FLOW_WINDOW_DAYS = 60      # 일별 4콜/평일 × ~43평일 ≈ 172콜·throttle 0.2s → ~40s/청크
_SHORTBAL_WINDOW_DAYS = 60  # 공매도 잔고: 일별 2콜/평일(시장) × ~43평일 ≈ 86콜/청크(flow의 절반)
_FLOW_DERIV_WINDOW_DAYS = 240  # 선물·ETF 수급: 소스당 기간 1콜(3콜/청크·~55s — 13102 소요 거래일당
                               # ~0.1s 선형 실측·600일은 timeout) — 전체 이력 ~26청크 완주


def _backfill_flow_chunk() -> None:
    """KR 기관·외국인 수급 백필 청크 — 일별 전종목(pykrx) 60일 창 today→2010 역순 1청크.

    KRX_ID/PW 미설정 시 no-op(비활성·로그 침묵). 성공 시만 cursor 전진(부분 전진 금지 — 실패면
    커서 유지·같은 윈도우 재시도). floor(2010) 도달 시 자연 종료(무비용)."""
    from quant_core import data_fetcher
    from quant_core.data.feeds import flow_kr
    if not flow_kr._has_login():
        return
    bf = DateCursorBackfill(cursor_path=data_fetcher.DATA_DIR / "_flow.cursor",
                            floor=CORE_FLOOR_COMPACT, window_days=_FLOW_WINDOW_DAYS)
    win = bf.next_window()
    if win is None:
        return                                       # 백필 완료 — 무비용
    start, end = win
    res = flow_kr.fetch_range(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if not res.get("ok"):
        _log.warning("[altdata] KR 수급 백필 실패(%s~%s) — cursor 유지·재시도", start, end)
        return
    bf.advance(start)
    _log.info("[altdata] KR 수급 백필(pykrx 일별전종목) %s~%s: %s", start, end, res)


def _refresh_flow() -> None:
    """일일 수급 증분 — 최근 7일 재수집·merge(16:30 KST, KRX 마감 후). 일별 전종목 경로."""
    from datetime import date, timedelta
    from quant_core.data.feeds import flow_kr
    if not flow_kr._has_login():
        return
    end = date.today()
    res = flow_kr.fetch_range((end - timedelta(days=7)).strftime("%Y%m%d"),
                              end.strftime("%Y%m%d"))
    if res.get("ok"):
        data_cache.invalidate()
        _log.info("[altdata] KR 수급 증분(pykrx 일별전종목) ~%s: %s", end, res)


def _initial_flow_refresh():
    import time
    try:
        time.sleep(75)
        _log.info("KR 수급(pykrx) 초기 백필 청크 시작")
        _backfill_flow_chunk()
    except Exception:
        _log.exception("KR 수급 초기 청크 예외 — 10분 cron 재시도")


# ── 공매도 잔고(KRX pykrx 로그인, 일별 전종목) ────────────────────────────────
# 옛 웹 경로(krdata._shorting: KRX MDC OTP→CSV·종목당 1콜·재배포마다 콜드 재크롤)를 대체.
# flow_kr와 동형(일별 전종목·날짜 cursor 백필·부분 전진 금지). 웹 kr_extras가 피드 우선 서빙.
def _backfill_shortbal_chunk() -> None:
    """KR 공매도 잔고 백필 청크 — 일별 전종목(pykrx) 60일 창 today→2010 역순 1청크.
    KRX_ID/PW 미설정 시 no-op. 성공 시만 cursor 전진(실패면 커서 유지·같은 윈도우 재시도)."""
    from quant_core import data_fetcher
    from quant_core.data.feeds import short_balance_kr
    if not short_balance_kr._has_login():
        return
    bf = DateCursorBackfill(cursor_path=data_fetcher.DATA_DIR / "_shortbal.cursor",
                            floor=CORE_FLOOR_COMPACT, window_days=_SHORTBAL_WINDOW_DAYS)
    win = bf.next_window()
    if win is None:
        return                                       # 백필 완료 — 무비용
    start, end = win
    res = short_balance_kr.fetch_range(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if not res.get("ok"):
        _log.warning("[altdata] KR 공매도 잔고 백필 실패(%s~%s) — cursor 유지·재시도", start, end)
        return
    bf.advance(start)
    _log.info("[altdata] KR 공매도 잔고 백필(pykrx 일별전종목) %s~%s: %s", start, end, res)


def _refresh_shortbal() -> None:
    """일일 공매도 잔고 증분 — 최근 7일 재수집·merge(16:30 KST, KRX 마감 후·T+2 잔고 반영)."""
    from datetime import date, timedelta
    from quant_core.data.feeds import short_balance_kr
    if not short_balance_kr._has_login():
        return
    end = date.today()
    res = short_balance_kr.fetch_range((end - timedelta(days=7)).strftime("%Y%m%d"),
                                       end.strftime("%Y%m%d"))
    if res.get("ok"):
        _log.info("[altdata] KR 공매도 잔고 증분(pykrx 일별전종목) ~%s: %s", end, res)


def _initial_shortbal_refresh():
    import time
    try:
        time.sleep(90)
        _log.info("KR 공매도 잔고(pykrx) 초기 백필 청크 시작")
        _backfill_shortbal_chunk()
    except Exception:
        _log.exception("KR 공매도 잔고 초기 청크 예외 — 10분 cron 재시도")


# ── KR 선물·ETF 투자자별 수급(KRX MDC 로그인, 소스당 기간 1콜) ─────────────────
# flow_kr(종목별 수급)와 별개 — 상품/시장 단위 매크로 심볼 6종(flow_deriv_kr.SYMBOLS).
def _backfill_flow_deriv_chunk() -> None:
    """KR 선물·ETF 수급 백필 청크 — 240일 창 today→2010 역순 1청크(3콜·~55s).
    KRX_ID/PW 미설정 시 no-op. 성공 시만 cursor 전진(실패면 커서 유지·같은 윈도우 재시도)."""
    from quant_core import data_fetcher
    from quant_core.data.feeds import flow_deriv_kr
    if not flow_deriv_kr._has_login():
        return
    bf = DateCursorBackfill(cursor_path=data_fetcher.DATA_DIR / "_flow_deriv.cursor",
                            floor=CORE_FLOOR_COMPACT, window_days=_FLOW_DERIV_WINDOW_DAYS)
    win = bf.next_window()
    if win is None:
        return                                       # 백필 완료 — 무비용
    start, end = win
    res = flow_deriv_kr.fetch_range(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if not res.get("ok"):
        _log.warning("[altdata] KR 선물·ETF 수급 백필 실패(%s~%s, fail=%s) — cursor 유지·재시도",
                     start, end, res.get("fail"))
        return
    bf.advance(start)
    _log.info("[altdata] KR 선물·ETF 수급 백필(KRX MDC) %s~%s: %s", start, end, res)


def _refresh_flow_deriv() -> None:
    """일일 선물·ETF 수급 증분 — 최근 7일 재수집·merge(18:40 KST — 당일치 18시 이후 제공)."""
    from datetime import date, timedelta
    from quant_core.data.feeds import flow_deriv_kr
    if not flow_deriv_kr._has_login():
        return
    end = date.today()
    res = flow_deriv_kr.fetch_range((end - timedelta(days=7)).strftime("%Y%m%d"),
                                    end.strftime("%Y%m%d"))
    if res.get("ok"):
        data_cache.invalidate()
        _log.info("[altdata] KR 선물·ETF 수급 증분(KRX MDC) ~%s: %s", end, res)


# ── KR OHLCV 깊이 백필(FDR, 무키) ─────────────────────────────────────────────

def _backfill_kr_ohlcv_chunk() -> None:
    """KR OHLCV 깊이 백필 청크(10분) — 기존 종목을 2010까지 소급 prepend.

    일일 dataset_kr fetch는 기존 parquet을 *앞으로만* 증분하므로 2010~2014 과거가
    안 채워진다. 이 백필이 그 갭만 메운다(종목별 1회 + depth-done 마커로 완료 시 0비용)."""
    from quant_core import data_fetcher
    codes = data_fetcher.load_managed_kr_codes()
    res = data_fetcher.backfill_korean_stocks_depth(codes, budget_symbols=150)
    if res.get("deepened") or res.get("fail"):
        _log.info("KR OHLCV 깊이 백필 청크(FDR): %s", res)


def _initial_kr_ohlcv_backfill():
    try:
        time.sleep(100)            # dataset 초기 갱신 이후 — 외부 호출 분산 (모듈 time 사용)
        _log.info("KR OHLCV 깊이 백필 초기 청크 시작")
        _backfill_kr_ohlcv_chunk()
    except Exception:
        _log.exception("KR OHLCV 깊이 백필 초기 청크 예외 — 10분 cron 재시도")


# ── US OHLCV 깊이 백필(yfinance, 무키) — KR 깊이 백필의 US 미러 ────────────────

def _backfill_us_ohlcv_chunk() -> None:
    """US OHLCV 깊이 백필 청크(10분) — 기존 종목을 CORE_FLOOR(2010)까지 소급 prepend.

    과거 backfill_start=2015 코호트(실측 US 2010 도달 3%)의 floor 이전 갭을 메운다.
    같은 min_date 그룹 배치 1콜 + depth-done 마커(완주 시 0비용) — KR과 동일 규약."""
    from quant_core import data_fetcher
    res = data_fetcher.backfill_overseas_depth(budget_symbols=200)
    if res.get("deepened") or res.get("fail"):
        _log.info("US OHLCV 깊이 백필 청크(yfinance): %s", res)


def _initial_us_ohlcv_backfill():
    try:
        time.sleep(130)            # KR 깊이(100s)와 스태거 — 외부 호출 분산
        _log.info("US OHLCV 깊이 백필 초기 청크 시작")
        _backfill_us_ohlcv_chunk()
    except Exception:
        _log.exception("US OHLCV 깊이 백필 초기 청크 예외 — 10분 cron 재시도")


# ── US 선물 COT 포지셔닝 (CFTC Socrata, 무키) ─────────────────────────────────

def _refresh_cot() -> None:
    """COT 전 시장 수집(주간) — 시장당 1콜=전체 이력(멱등 merge)이라 백필·증분이 같은 호출.

    발표: 화요일 마감분을 금요일 15:30 ET(토 새벽 KST) 공개 → 토 09:00 cron. 정정도
    전체 재수집이 자연 흡수. 실패 시장은 skip·재시도(_run_with_retry)."""
    from quant_core.data.feeds import cot_cftc
    res = cot_cftc.fetch_all()
    if res.get("saved"):
        data_cache.invalidate()
    _log.info("[altdata] COT(CFTC) 주간 수집: 시장 %s·실패 %s", res.get("markets"), res.get("fail"))


def _initial_cot_refresh():
    try:
        time.sleep(140)            # US 깊이(130s)와 스태거
        _log.info("COT(CFTC) 초기 수집 시작(전체 이력)")
        _refresh_cot()
    except Exception:
        _log.exception("COT 초기 수집 예외 — 주간 cron 재시도")


def _initial_shortvol_backfill():
    try:
        time.sleep(170)            # COT(140s)와 스태거
        _log.info("US 공매도량(FINRA) 초기 백필 청크 시작")
        _backfill_shortvol_chunk()
    except Exception:
        _log.exception("US 공매도량 초기 청크 예외 — 30분 cron 재시도")


# ── US 13F 기관보유 (SEC 구조화 Form 13F Data Sets, 무키) ──────────────────────

def _backfill_13f_chunk() -> None:
    """US 13F 기관보유 백필 청크(30분) — 분기 ZIP 1개씩 최신→과거(다운로드→CUSIP 집계→종목별 저장).

    CUSIP→ticker 맵 없으면 최초 1회 FTD로 구축. 실패 ZIP은 마커 금지·재시도. 전 분기 소진 후
    무비용 no-op(신규 분기 자동 픽업). ZIP은 RAM(BytesIO) 처리 후 해제 — run당 1개(메모리 상한)."""
    from quant_core.data.feeds import institutional_13f
    res = institutional_13f.backfill_13f(limit=1)
    if res.get("processed"):
        data_cache.invalidate()
    _log.info("[altdata] US 13F 백필 청크(SEC): %s", res)


def _initial_13f_backfill():
    try:
        time.sleep(200)            # 공매도(170s)와 스태거
        _log.info("US 13F(SEC) 초기 백필 청크 시작")
        _backfill_13f_chunk()
    except Exception:
        _log.exception("US 13F 초기 청크 예외 — 30분 cron 재시도")


# ── KR 시장지표 (공식 KRX Open API, KRX_API_KEY) ──────────────────────────────
# V-KOSPI·옵션풋콜비율·KRX채권지수·국고채3/10년 = 매크로형 명명 시계열. 날짜축 cursor 백필
# (DateCursorBackfill — 컨센서스와 동일 문법). 가벼운 지표(3서비스)는 넓은 윈도우,
# 옵션 풋콜(하루 ~19k행)은 좁은 윈도우.
_KRX_LIGHT_WINDOW_DAYS = 90
_KRX_PUTCALL_WINDOW_DAYS = 4        # 옵션 무거움(timeout 90s) — 좁게
_KRX_MARKETCAP_WINDOW_DAYS = 60     # sto 2서비스×평일(~86콜/청크) — 30분 케이던스로 quota 관리


def _krx_backfill(name: str, window_days: int, fetch_fn, floor: str = CORE_FLOOR_COMPACT) -> None:
    """공식 KRX API 날짜축 cursor 백필 — today→floor 역순 1청크. 성공 시만 cursor 전진.

    floor 기본=CORE_FLOOR. 상장이 floor보다 늦은 상품(코스닥150선물 2015-11-23)은 상장일
    floor로 — 그 이전 구간은 소스에 행이 없어 헛도는 콜만 생긴다."""
    from quant_core import data_fetcher
    from quant_core.data.feeds import krx_openapi
    if not krx_openapi.is_active():
        return
    bf = DateCursorBackfill(cursor_path=data_fetcher.DATA_DIR / f"_krx_{name}.cursor",
                            floor=floor, window_days=window_days)
    win = bf.next_window()
    if win is None:
        return                                       # 백필 완료 — 무비용
    start, end = win
    res = fetch_fn(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if not res.get("ok"):
        _log.warning("[altdata] KRX %s 백필 실패(%s~%s) — cursor 유지·재시도", name, start, end)
        return
    bf.advance(start)
    _log.info("[altdata] KRX %s 백필 청크 %s~%s: %s", name, start, end, res.get("saved"))


def _backfill_krx_market_chunk() -> None:
    from quant_core.data.feeds import krx_openapi
    _krx_backfill("market", _KRX_LIGHT_WINDOW_DAYS, krx_openapi.fetch_market_indicators)


def _backfill_krx_putcall_chunk() -> None:
    from quant_core.data.feeds import krx_openapi
    _krx_backfill("putcall", _KRX_PUTCALL_WINDOW_DAYS, krx_openapi.fetch_putcall)


def _backfill_krx_etf_chunk() -> None:
    from quant_core.data.feeds import krx_openapi
    _krx_backfill("etf", _KRX_LIGHT_WINDOW_DAYS, krx_openapi.fetch_etf_flow)


def _backfill_krx_futures_panel_chunk() -> None:
    from quant_core.data.feeds import krx_openapi
    _krx_backfill("futures_panel", _KRX_LIGHT_WINDOW_DAYS, krx_openapi.fetch_futures_panel)


def _backfill_kq150_panel_chunk() -> None:
    """코스닥150선물 만기물 패널 소급 백필 — 상장일(2015-11-23) floor 전용 커서.

    기존 futures_panel 커서는 K200 단독 시절 완주분이라 KQ150 과거를 못 채운다 — 뒤늦게
    합류한 상품은 자기 커서로 소급(같은 fut_bydd_trd 일별 응답에서 KQ150만 추출·저장)."""
    from functools import partial
    from quant_core.data.feeds import krx_openapi
    _krx_backfill("kq150_panel", _KRX_LIGHT_WINDOW_DAYS,
                  partial(krx_openapi.fetch_futures_panel, symbols=["코스닥150선물"]),
                  floor="20151123")


def _backfill_marketcap_chunk() -> None:
    """KR 시총·거래대금(sto) 백필 청크(30분) — 60일 창 역순, 종목별 parquet 축적.

    ⚠ `sto` 서비스 KRX 포털 미신청이면 에러 페이로드→실패(커서 유지)로 정직히 드러난다
    (전용 fetcher가 휴장 []와 구분 — 침묵 전진 방지). 신청 완료 즉시 자동 가동."""
    from quant_core.data.feeds import marketcap_krx
    _krx_backfill("marketcap", _KRX_MARKETCAP_WINDOW_DAYS, marketcap_krx.fetch_range)


def _refresh_marketcap() -> None:
    """시총·거래대금 일일 증분 — 최근 7일 재수집(09:35 — sto는 T+1 08시 확정, 라이브 실증)."""
    from datetime import date, timedelta
    from quant_core.data.feeds import marketcap_krx
    if not marketcap_krx.is_active():
        return
    end = date.today()
    res = marketcap_krx.fetch_range((end - timedelta(days=7)).strftime("%Y%m%d"),
                                    end.strftime("%Y%m%d"))
    if res.get("ok"):
        data_cache.invalidate()
    _log.info("[altdata] KR 시총·거래대금 증분(sto): %s", res)


# ── US 공매도 거래량 (FINRA Reg SHO, 무키) ────────────────────────────────────

def _backfill_shortvol_chunk() -> None:
    """US 공매도량 백필 청크(30분) — 90일 창 역순, floor=2018-08-01(consolidated 포맷 시작).

    FINRA 무키 공개 CDN. 커서는 DateCursorBackfill(단일 백필 문법)."""
    from quant_core import data_fetcher
    from quant_core.data.feeds import short_volume_us
    bf = DateCursorBackfill(cursor_path=data_fetcher.DATA_DIR / "_shortvol.cursor",
                            floor=short_volume_us.SOURCE_FLOOR.replace("-", ""),
                            window_days=90)
    win = bf.next_window()
    if win is None:
        return                                       # 백필 완료 — 무비용
    start, end = win
    res = short_volume_us.fetch_range(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
    if not res.get("ok"):
        _log.warning("[altdata] US 공매도량 백필 실패(%s~%s) — cursor 유지·재시도", start, end)
        return
    bf.advance(start)
    _log.info("[altdata] US 공매도량 백필 청크(FINRA) %s~%s: %s일·%s종목",
              start, end, res.get("days"), res.get("stocks"))


def _refresh_shortvol() -> None:
    """US 공매도량 일일 증분 — 최근 5일 재수집(09:40 KST — 당일 18:00 ET 게시 후)."""
    from datetime import date, timedelta
    from quant_core.data.feeds import short_volume_us
    end = date.today()
    res = short_volume_us.fetch_range((end - timedelta(days=5)).strftime("%Y%m%d"),
                                      end.strftime("%Y%m%d"))
    if res.get("ok") and res.get("stocks"):
        data_cache.invalidate()
    _log.info("[altdata] US 공매도량 증분(FINRA): %s", res)


def _refresh_krx_market() -> None:
    """일일 증분 — 최근 7일 재수집(공식 KRX API는 T+1 08시 갱신)·캐시 attach."""
    from datetime import date, timedelta
    from quant_core.data.feeds import krx_openapi
    if not krx_openapi.is_active():
        return
    end = date.today()
    s = (end - timedelta(days=7)).strftime("%Y%m%d")
    e = end.strftime("%Y%m%d")
    r1 = krx_openapi.fetch_market_indicators(s, e)
    r2 = krx_openapi.fetch_putcall(s, e)
    r3 = krx_openapi.fetch_etf_flow(s, e)
    data_cache.invalidate()
    _log.info("[altdata] KRX 시장지표 증분 %s~%s: light=%s pc=%s etf=%s",
              s, e, r1.get("saved"), r2.get("saved"), r3.get("saved"))


def _initial_krx_market_refresh():
    try:
        time.sleep(110)
        _log.info("KRX 시장지표(공식API) 초기 백필 청크 시작")
        _backfill_krx_market_chunk()
        _backfill_krx_etf_chunk()
        _backfill_krx_putcall_chunk()
        _backfill_krx_futures_panel_chunk()
        _backfill_marketcap_chunk()
    except Exception:
        _log.exception("KRX 시장지표 초기 청크 예외 — 10분 cron 재시도")


def _initial_technical_refresh():
    import time
    try:
        time.sleep(180)
        _log.info("기술적 지표 초기 fetch 시작")
        _refresh_technical()
    except Exception:
        _log.exception("기술적 지표 초기 fetch 중 예외 — 정시 cron 재시도")


def _initial_static_meta_refresh():
    import time
    try:
        time.sleep(150)
        _log.info("정적 메타 초기 fetch 시작 (섹터·상장폐지일)")
        _refresh_static_meta()
    except Exception:
        _log.exception("정적 메타 초기 fetch 예외 — 18:10 cron 재시도")


def _refresh_dataset_all() -> None:
    """글로벌 + 한국 dataset 동시 갱신 — 시작 시 초기 fetch에만 사용.
    정시 cron은 글로벌(07:30)·한국(18:15)이 각자 호출."""
    _refresh_global_dataset()
    _refresh_kr_dataset()


def _package_bundle(include_full: bool = True) -> None:
    """dataset bundle 재패키징 — 반드시 dataset refresh '완료' 뒤에서만 호출한다.

    이전 구조(고정 시각 cron 07:45/18:30 + boot+300s 고정 sleep)는 refresh '진행 중'
    디스크를 묶은 부분 bundle을 만들 수 있었고(재배포 직후엔 빈 디스크), bundle
    부재(410) 창과 결합해 로컬앱이 manifest 폴백 grind(시간 단위)로 추락 →
    `_REFRESH_LOCK` 컨보이로 발주 사이클이 통째로 블록되는 사고를 유발했다
    (2026-06-10 무발주 인시던트 RC-1/D4-1·D4-6,
    docs/incidents/2026-06-10-autotrading-week-retrospective.md).

    두 스코프를 빌드한다:
      trading — 자동매매 로컬앱 다운로드 대상(하위호환·필수 경로). 먼저·항상 빌드.
      full    — dev 테스트환경(localhost 챗봇)이 프로덕션 볼륨과 동일 데이터로 pull하는
                스코프(+flow·시총·공매도·13F). best-effort: full 빌드가 실패해도 로컬앱
                경로(trading bundle)엔 영향이 없도록 격리한다.

    include_full=False — 아침 선물 경로(08:02) 전용: full은 대용량·단일코어 zstd라
    아침 발주창(로컬 08:35 선물/08:55 주식 pull 전) 예산을 위협한다. 로컬앱이 받는
    trading만 재포장하고 full은 다음 정기 재포장(18:15)이 담당(dev 환경만 영향).
    """
    from .routers import dataset as dataset_router
    dataset_router.build_bundle("trading")
    if not include_full:
        return
    try:
        dataset_router.build_bundle("full")
    except Exception:
        _log.exception("full bundle 패키징 실패 (dev 테스트환경 pull만 영향 — 로컬앱 무영향)")


def _refresh_krx_futures() -> None:
    """KRX 선물(등록 상품 전체 — K200·KQ150) 만기물 패널 최근분 증분 + 연속물 재구성.

    진실원천 = 만기물별 일봉 패널(fut_bydd_trd — 하루 1콜에 전 상품·날짜축 cursor 백필).
    백테스트용 단일 연속물은 카탈로그 기본 롤(at_expiry)로 파생(rebuild_futures_continuous),
    다른 롤/조정은 엔진이 백테스트 시 패널서 재-stitch(E2). KRX는 T+1(익일 08시) 확정
    데이터라 미확정 당일 봉 문제가 없다(KIS 실시간과 달리 — 옛 F1 인시던트 무관). 라이브
    체결가는 별도(Naver/브로커) — 이 시리즈는 백테스트/신호 전용. 선물분석 탭(futures_config
    정적 CSV)은 이 경로와 무관(무변경).

    2026-07-15 파이프라인 근본수정(kr-target-reconciliation.md §15 Phase 3 — 07-14 사고 B2):
    ① **휴장 게이트**(문제 5): KR 휴장일엔 skip — 봉이 원래 없는 날을 지연으로 오판해
       재시도 낭비하지 않는다(거래 안전은 로컬 휴장 게이트 별도).
    ② **신선도 판정 = "직전 거래일 봉을 받았는가"**(문제 3·4 근본): fetch 반환 saved는
       병합 후 총 행수라 "새 봉 없음" 신호가 아니다(실측). 저장된 패널의 마지막 봉
       날짜를 직접 검사해, KRX 공개 지연일엔 예외를 던져 _run_with_retry 백오프가
       발주창 안에서 재시도하게 한다.
    ③ **번들 재포장**(문제 1·2 — 07-14 근본원인): fresh 확인 후 trading 번들을 즉시
       재포장해 로컬이 받는 번들과 preview가 같은 아침 데이터 상태를 가리키게 한다.
       구 버전은 preview만 트리거하고 번들(07:30분)을 안 바꿔 선물이 구조적 T-2였다.
    """
    from datetime import datetime as _dt, timedelta

    from quant_core import market_calendar as _mc
    from quant_core import data_fetcher as _df
    from quant_core.data.feeds import krx_openapi
    if not krx_openapi.is_active():
        _log.info("KRX 선물 refresh skip(KRX_API_KEY 미설정)")
        return
    # ⚠ 컨테이너(UTC)에서 date.today()는 아침 KST에 어제 — 반드시 KST 벽시계 기준.
    today_kst = _dt.now(ZoneInfo(_TZ_SEOUL)).date()
    if not _mc.is_session_day("KR", today_kst):
        _log.info("KRX 선물 refresh skip — KR 휴장일(%s)", today_kst)
        return
    end = today_kst
    s = (end - timedelta(days=7)).strftime("%Y%m%d")     # 최근 7일 재수집(T+1 확정 반영)
    res = krx_openapi.fetch_futures_panel(s, end.strftime("%Y%m%d"))
    data_cache.invalidate()
    _log.info("[altdata] KRX 선물 패널 증분 %s~%s: %s", s, end, res.get("saved"))
    # ② 신선도 판정 — 수집된 각 상품의 저장 패널 마지막 봉 ≥ 직전 KR 거래일이어야 한다.
    prev = _mc.prev_session_day("KR", today_kst)
    if prev is not None:
        stale: list[str] = []
        for sym in (res.get("saved") or {}):
            try:
                panel = _df.load_futures_panel(sym)
            except Exception:
                continue                     # 신규 상장/미백필 — 판정 불가는 차단하지 않음
            if panel is None or len(panel) == 0:
                continue
            last = panel.index[-1]
            last_d = last.date() if hasattr(last, "date") else last
            if last_d < prev:
                stale.append(f"{sym}(마지막 봉 {last_d})")
        if stale:
            # 예외 전파 → _run_with_retry 백오프(아침 커스텀 [3,6,10,15]분)가 재시도.
            raise RuntimeError(
                f"KRX 선물 stale — 직전 거래일({prev}) 봉 미수신: {', '.join(stale)}")
    # ③ fresh 확정 → 로컬이 받는 trading 번들 즉시 재포장(문제 1 근본 — full은 18:15 소관).
    _package_bundle(include_full=False)
    # preview 재계산 — 이게 없으면 07:30(dataset_global발)에 계산된 "선물 stale → 후보 0"
    # 스냅샷이 08:55 로컬 pull까지 잔존(2026-07-10 후속리뷰). 실패는 전파(문제 8)되어
    # fetch→번들→preview 전체가 원자적으로 재시도된다.
    _trigger_preview("kospi_futures")


def _refresh_global_dataset() -> None:
    """글로벌 데이터셋 — yfinance/FDR ETF/FRED/Binance/공포탐욕 + 해외 on-demand 종목.

    외부 publish: 미국 마감(06:00 KST)·FRED(06:15)·Binance/공포탐욕(09:00 자정 UTC).
    cron 07:30이 모든 글로벌 소스 publish 후 안전 마진. 자동매매 사이클(08:55) 전.
    """
    from quant_core import data_fetcher

    # 매크로/자산/사용자 종목 (yfinance, FDR ETF, FRED, Binance, 공포탐욕)
    data_fetcher.fetch_all(verbose=False)

    # 해외 유니버스 rebuild를 fetch 전에 — 콜드스타트 레이스 방지(빈 universe→US OHLCV 못받음).
    # rebuild는 KIS 마스터를 읽으므로 마스터 로드가 선행돼야 한다(가드·멱등).
    if not kis_master_cache.get_master_set():
        _log.info("해외 rebuild 전 KIS 마스터 로드 (콜드스타트 가드)")
        kis_master_cache.refresh()
    _rebuild_overseas_universe()   # NASDAQ Trader(권위·정제) ∪ KIS US master(거래가능) overwrite

    # 해외 종목 — yfinance 배치 수집 (글로벌 cron에 묶음)
    n = data_fetcher.fetch_managed_overseas()
    if n:
        _log.info("해외 종목 fetch: %d 종목", n)

    data_cache.invalidate()
    # 미국 스크리너 metrics 재빌드 (방금 갱신된 S&P500 OHLCV + 기존 시총 캐시)
    try:
        from . import us_metrics_cache
        # 콜드스타트 레이스 가드: 마스터가 아직 안 떴으면 먼저 로드. build_metrics는
        # 거래소 메타(NAS/NYS/AMS) 없는 종목을 전부 skip하므로, 마스터 미로드 시
        # us_metrics가 0이 된다. 멱등(이미 로드됐으면 무동작).
        if not kis_master_cache.get_master_set():
            _log.info("us_metrics 빌드 전 KIS 마스터 로드 (콜드스타트 가드)")
            kis_master_cache.refresh()
        us_metrics_cache.refresh()
    except Exception:
        _log.exception("us_metrics 갱신 실패 (미국 스크리너 영향)")
    # 번들 먼저 → preview — preview 예외(전파·문제 8)가 번들 재포장을 건너뛰지 않게.
    _package_bundle()
    _trigger_preview("dataset_global")


def _refresh_us_market_caps() -> None:
    """미국 S&P500 시가총액(fast_info) 주1회 수집 후 metrics 재빌드.

    분기 변동성이 낮은 시총은 매일 받을 필요가 없어 주말 1회만 갱신.
    """
    from . import us_metrics_cache
    us_metrics_cache.refresh_market_caps(timeout_each=0.2)
    us_metrics_cache.refresh()


def _refresh_kr_dataset() -> None:
    """한국 데이터셋 — KIS 마스터 KOSPI/KOSDAQ 거래 가능 종목 OHLC + 등록 전략 해외 코드 union.

    외부 publish: KRX 시간외 포함 18:10. cron 18:15가 KRX 직후 안전 마진.
    **dataset의 Close = 정규장(15:30) 종가**, 시간외 단일가(16:00~18:00) 미반영.
    백테스트와 라이브 매수 신호 평가가 동일 정규장 종가 위에서 일관되게 동작.
    등록 전략에서 새 해외 코드 발견 시 managed_overseas에 추가 (다음 글로벌 cron에서 fetch).

    Phase 41 — KR 전 종목 OHLCV를 매일 fetch함으로써 자동 선택 종목별 조건 평가
    (`[이 종목]` placeholder)가 dataset 안에서 그대로 가능해진다. load_dataset()이
    종목별 RSI/MA/ATR 등을 compute_all로 계산해 dict로 반환.
    """
    from quant_core import data_fetcher

    # 1. KIS 마스터 KOSPI/KOSDAQ → 한국 종목 FDR fetch
    master_list = kis_master_cache.get_master_list()
    kr_codes = sorted({m["symbol"] for m in master_list
                        if m.get("market") in ("KOSPI", "KOSDAQ")})
    data_fetcher.save_managed_kr_codes(kr_codes)
    if kr_codes:
        _log.info("한국 종목 fetch: %d 종목", len(kr_codes))
        data_fetcher.fetch_korean_stocks(kr_codes, verbose=False)

    # 2. 해외 유니버스 rebuild — 등록 전략의 해외 심볼은 by_code(KIS master) 조회로만 추가됐어
    #    KIS US master ⊆ 라, rebuild(NASDAQ Trader ∪ KIS US master)가 자동 포함한다.
    _rebuild_overseas_universe()

    data_cache.invalidate()
    # 번들 먼저 → preview — preview 예외(전파·문제 8)가 번들 재포장을 건너뛰지 않게.
    _package_bundle()
    _trigger_preview("dataset_kr")


def _seed_sp500_overseas() -> int:
    """S&P500 큐레이션 유니버스를 managed_overseas에 union (yf 대시 코드).

    글로벌 cron이 다음 사이클에 OHLCV를 fetch. 수동 갱신(manage)도 공유.
    """
    from quant_core import data_fetcher
    sp = [{"code": c["symbol"].replace(".", "-"), "name": c.get("name", "")}
          for c in data_fetcher.load_sp500() if c.get("symbol")]
    data_fetcher.save_managed_overseas(data_fetcher.load_managed_overseas() + sp)
    return len(sp)


def _seed_us_master_overseas() -> int:
    """KIS 미국 마스터(NAS/NYS/AMS, 주식+ETF) 전체를 managed_overseas에 union.

    국내(managed_kr = KIS 마스터 KOSPI/KOSDAQ 전체)와 대칭. 데이터 없는 종목은
    yfinance fetch가 빈 결과 → parquet 미생성 → /symbols 자동 제외(§4.8)되므로
    별도 유동성 필터 없이 "전부 시드 → 데이터 있는 것만 노출"로 자동 큐레이션된다.
    클래스주(BRK.B 등) 정확한 yf 코드는 _seed_sp500_overseas가 보장(code 기준 dedupe).
    """
    from quant_core import data_fetcher
    master = kis_master_cache.get_master_list()
    us = [{"code": (m.get("ticker") or m["symbol"]), "name": m.get("name", "")}
          for m in master if m.get("market") in ("NAS", "NYS", "AMS")]
    data_fetcher.save_managed_overseas(data_fetcher.load_managed_overseas() + us)
    return len(us)


_OVERSEAS_MIN_SANE = 8000        # NASDAQ Trader rebuild 가드 — 이보다 적으면 반쪽/실패로 보고 skip


def _rebuild_overseas_universe() -> int:
    """US 데이터 유니버스를 **재생성(overwrite)** — NASDAQ Trader(권위 정의) ∪ KIS US master(거래가능).

    기존 append(누적)는 상장폐지·정크가 영영 안 빠졌다. rebuild로 매 cron이 깨끗한 명단을 다시 쓴다
    (데이터 parquet은 안 지움 — 명단만). NASDAQ Trader=보통주+ETF 정제(워런트/우선주 제외), KIS US
    master=거래가능 보장(데이터⊇거래 — 등록전략 해외심볼도 KIS master ⊆ 라 자동 포함). '/'코드 제외·
    dedupe는 save_managed_overseas가 담당.

    **가드:** NASDAQ Trader fetch 실패 또는 종목수 비정상(<_OVERSEAS_MIN_SANE)이면 overwrite 하지
    않고 기존 유니버스를 보존 — 네트워크 일시오류로 유니버스가 비는 것 방지.
    """
    from quant_core import data_fetcher
    from quant_core.data.feeds import nasdaq_trader

    try:
        nt = nasdaq_trader.fetch()                  # 보통주+ETF 정제·yf코드 정규화
    except Exception:
        _log.exception("NASDAQ Trader fetch 실패 — 해외 유니버스 보존(rebuild skip)")
        return 0
    if len(nt) < _OVERSEAS_MIN_SANE:
        _log.warning("NASDAQ Trader %d종목(<%d 비정상) — 해외 유니버스 보존(rebuild skip)",
                     len(nt), _OVERSEAS_MIN_SANE)
        return 0

    master = kis_master_cache.get_master_list()     # 거래가능(KIS) — 데이터⊇거래
    kis = [{"code": (m.get("ticker") or m["symbol"]), "name": m.get("name", "")}
           for m in master if m.get("market") in ("NAS", "NYS", "AMS")]
    rows = [{"code": r["code"], "name": r["name"]} for r in nt] + kis   # NT 우선 union → overwrite
    data_fetcher.save_managed_overseas(rows)        # '/'제외·dedupe·overwrite(append 아님)
    _log.info("해외 유니버스 rebuild: NASDAQ Trader %d + KIS %d", len(nt), len(kis))
    return len(nt)


# orphan parquet 1회성 정리 — PR#400(유니버스 malformed 차단) 후속. 옛 오종목('/'→'_' 파일
# AACT_U.parquet 등)이 볼륨에 영구 잔존해 매 번들에 실리는 대역폭 낭비를 제거. 정합성 아닌
# hygiene이라 1회면 충분(marker 게이트). ⚠자동 삭제라 mass-deletion 가드 필수: 유니버스가
# 비정상적으로 작으면(미로드 의심) 삭제 skip·marker 미기록으로 다음 부팅 재시도.
_ORPHAN_PRUNE_MARKER = "_orphan_prune_done.marker"
_ORPHAN_MIN_SANE_UNIVERSE = 1000        # prod 유니버스 25k+ — 이보다 적으면 미로드로 보고 삭제 금지


def _prune_orphan_parquets_once():
    """유니버스 밖 orphan parquet 1회 정리(marker 게이트·유니버스 sanity 가드).

    keep-set은 write 경로(_parquet_path)와 동일 규칙 + 해외는 predicate 필터라 매크로·실티커·
    별칭·서브디렉터리·정당 종목 전부 보존(find_orphan_parquets). 유니버스 키가 floor 미만이면
    (managed 목록 미로드 등) 전 파일이 orphan으로 오판될 수 있어 **삭제하지 않고 재시도**한다."""
    import time
    from quant_core import data_fetcher
    try:
        time.sleep(540)                 # dataset_refresh(360)+rebuild 이후 — 명단 self-clean 뒤 스태거
        marker = data_fetcher.DATA_DIR / _ORPHAN_PRUNE_MARKER
        if marker.exists():
            return
        n_keys = len(data_fetcher.iter_universe_keys())
        if n_keys < _ORPHAN_MIN_SANE_UNIVERSE:
            _log.warning("orphan 정리 skip — 유니버스 키 %d개(<%d 미로드 의심) — 삭제 금지·다음 부팅 재시도",
                         n_keys, _ORPHAN_MIN_SANE_UNIVERSE)
            return                      # marker 미기록 → 재시도
        orphans = data_fetcher.find_orphan_parquets()
        total = len(list(data_fetcher.DATA_DIR.glob("*.parquet")))
        _log.info("orphan 정리(1회성): 유니버스 %d키 · top-level %d개 중 orphan %d개 삭제",
                  n_keys, total, len(orphans))
        for p in orphans[:20]:
            _log.info("  orphan 표본: %s", p.name)
        deleted = 0
        for p in orphans:
            try:
                p.unlink()
                deleted += 1
            except OSError as e:
                _log.warning("orphan 삭제 실패(건너뜀): %s — %s", p.name, e)
        marker.write_text(f"pruned {deleted}/{len(orphans)}", encoding="utf-8")
        _log.info("orphan 정리 완료: %d/%d 삭제 · marker 기록", deleted, len(orphans))
        if deleted:
            data_fetcher.mark_data_dirty()
    except Exception:
        _log.exception("orphan 정리 1회성 잡 예외 — 다음 부팅 재시도(marker 미기록)")


def _initial_dataset_refresh():
    """시작 시 1회 dataset 갱신 — 기술적 지표 후 240초 지연 (외부 소스 동시 호출 분산)."""
    import time
    try:
        time.sleep(240)
        _log.info("dataset 초기 갱신 시작 (글로벌 + 한국)")
        # 선물은 단건이라 무거운 전체 갱신 앞에서 먼저 — 빠른 데이터·검증. 자체 try로 격리(선물
        # 실패가 메인 데이터 갱신을 막지 않게). KIS 데이터키 미설정이면 fail-safe no-op.
        try:
            _refresh_krx_futures()
        except Exception:
            _log.exception("KRX 선물 초기 수집 예외 — 아침 cron 재시도")
        _refresh_dataset_all()
        _log.info("dataset 초기 갱신 완료")
    except Exception:
        _log.exception("dataset 초기 갱신 중 예외 — 정시 cron 재시도")


def _initial_us_market_caps():
    """시작 시 1회 미국 시가총액 부트스트랩 (캐시 비어있을 때 첫 주 대기 방지).
    dataset 초기 갱신 이후 충분히 지연."""
    import time
    try:
        time.sleep(360)
        from . import us_metrics_cache
        if not us_metrics_cache._load_caps():
            _log.info("미국 시가총액 초기 fetch 시작")
            _refresh_us_market_caps()
    except Exception:
        _log.exception("미국 시가총액 초기 fetch 예외 — 주간 cron 재시도")


def _initial_calendar_refresh():
    """Q2+Q8: 기동 시 1회 KR/US 캘린더 빌드. 외부 fetch와 달리 라이브러리 호출만이라
    실패 가능성 매우 낮음 — 그러나 디스크 권한·import 실패는 가능하므로 try-except.
    """
    try:
        _log.info("캘린더 초기 빌드 시작 (KR/US)")
        result = calendar_cache.refresh()
        _log.info("캘린더 초기 빌드 결과: %s", result)
    except Exception:
        _log.exception("캘린더 초기 빌드 예외 — 다음 03:00 cron 재시도")


def _industry_tickers() -> list[str]:
    """우리가 다루는 종목 = data/industry_*.csv의 전 종목 코드."""
    import csv
    import os as _os
    base = _os.path.join(_os.path.dirname(__file__), "data")
    codes: list[str] = []
    for fn in _os.listdir(base):
        if fn.startswith("industry_") and fn.endswith(".csv"):
            with open(_os.path.join(base, fn), encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    tk = str(r.get("티커", "")).strip()
                    if tk.isdigit():
                        codes.append(tk.zfill(6))
    return sorted(set(codes))


def _refresh_company_profiles() -> None:
    """기업개요(설립일·대표 등) 분기 갱신 — 매일 호출하되 90일 경과 시에만 실제 수집.
    대상 = data/industry_*.csv의 전 종목(우리가 다루는 기업들). 출처 FnGuide(키 불필요)."""
    from . import company_profiles
    company_profiles.refresh_if_stale(_industry_tickers())


def _refresh_financials() -> None:
    """재무제표(PL·BS·CF, 연간 YoY·분기 QoQ) 일괄 갱신 — 분기/사업보고서 제출 마감일 저녁 cron.
    Financials 탭은 이 저장본을 즉시 서빙(로딩 없음). 출처 FnGuide(전자공시 집계·키 불필요)."""
    from . import financials
    financials.refresh_all(_industry_tickers())
    financials.clear_cache()                   # 새 저장본이 즉시 반영되도록 메모리 캐시 무효화


def _refresh_bonds() -> None:
    """국가별 국채 수익률곡선(미·일·유·한·중, FRED·MOF·ECB·무키) 일일 갱신 → 볼륨(bonds/{cc}.parquet).
    GlobalMarket 국채 탭은 이 저장본을 즉시 서빙(재배포 warmup·요청당 크롤 제거·미스 시 서버 self-heal)."""
    from quant_core.data.feeds import bonds
    _log.info("[altdata] 국채금리 수집(FRED/MOF/ECB): %s", bonds.refresh_all())


def _initial_financials_prewarm() -> None:
    """기동 시 산업 종목 재무제표를 메모리·디스크에 미리 데움 — Financials 첫 진입을 즉시화."""
    try:
        from . import financials
        n = financials.prewarm(_industry_tickers())
        _log.info("재무제표 프리워밍 완료 — %d종목", n)
    except Exception:
        _log.exception("재무제표 프리워밍 예외")


def _initial_industry_prewarm() -> None:
    """기동 시 산업분석 트리맵 가격·시총을 미리 데움 — 재시작 후 첫 진입 콜드 로딩(라이브 FDR) 제거.

    트리맵 시총/등락은 _industry(=_returns DataReader + _shares)로 라이브 계산돼 _day 키로
    메모리 캐시된다. 재시작하면 캐시가 비어 첫 진입마다 수십 종목 시세를 라이브 조회(느림).
    기동 시 전 산업을 백그라운드로 데워 둬 첫 진입부터 즉시 뜨게 한다(가격 cron과 별개·기동용)."""
    try:
        from . import industry
        for name in industry.INDUSTRIES:
            try:
                industry.industry(name)
            except Exception:
                _log.exception("산업 트리맵 프리워밍 실패: %s", name)
        _log.info("산업 트리맵 프리워밍 완료 — %d개 산업", len(industry.INDUSTRIES))
    except Exception:
        _log.exception("산업 트리맵 프리워밍 예외")


def _initial_dataset_cache_prewarm() -> None:
    """기동 시 원시 OHLCV 메모리 캐시(data_cache._raw)를 미리 데운다 — 첫 챗 백테스트/스크린의
    콜드 로드(실측 ~28s, Railway 로그 'get_projected: raw=28.07s')를 요청 경로 밖으로 옮긴다.

    raw 캐시는 프로세스 수명 1회 로드(이후 raw=0.00s)라, 재시작·배포 직후 첫 무거운 챗 턴이
    홀로 28s를 떠안던 것을 제거한다. full compute(get_dataset)는 데우지 않는다 — get_projected가
    전략이 참조하는 지표만 즉석 계산하므로 raw만 상주하면 충분(메모리도 raw ~1.3GB만)."""
    try:
        from . import data_cache
        n = len(data_cache.get_raw_dataset())
        _log.info("원시 데이터셋 캐시 프리워밍 완료 — %d종목", n)
    except Exception:
        _log.exception("원시 데이터셋 캐시 프리워밍 예외")


def _initial_summary_prewarm() -> None:
    """기동 시 개별종목분석 summary(종목상세) 경로를 데운다 — 첫 유저가 warmup 비용을 물지 않도록.

    summary는 symbol_detail이 담당하는데 그 첫 호출은 _all_listings_cached가 US StockListing
    (네트워크)를 콜드로 물고(전 종목 공유 비용·1회), 이어 종목별 원시 시세를 콜드 로드한다. 대표
    종목 몇 개를 미리 호출해 **공유 목록 캐시 + 그 종목 시세 캐시**를 데워 첫 진입부터 즉시 뜨게
    한다(트리맵·재무제표 프리워밍과 대칭). 커버 종목의 볼륨 read는 콜드여도 빠르므로 전 종목을
    데울 필요는 없다 — 공유 목록 캐시 워밍이 핵심(그래야 임의 종목 첫 조회도 목록 콜드를 안 문다)."""
    try:
        from .routers import market
        seeds = ["005930", "000660", "247540", "005380", "035420"]  # 대형·최빈 조회 대표
        for code in seeds:
            try:
                market.symbol_detail(code, range="1y", light=1, user=None)
            except Exception:
                _log.exception("summary 프리워밍 실패: %s", code)
        _log.info("summary(종목상세) 프리워밍 완료 — 공유 목록캐시 + %d종목", len(seeds))
    except Exception:
        _log.exception("summary 프리워밍 예외")


def _health_scan() -> None:
    """자동매매 건강 dead-man's-switch — 전 유저 건강을 주기 평가해 신규 RED(무발주·데이터결손·
    앱다운)를 운영자 webhook으로 능동 통지한다. on-ingest로는 못 잡는 '침묵 유저'(push 없음)까지
    cron이 잡아, mwmw CON.parquet 사고(설치 이래 발주 0이 며칠 미탐지)의 사각을 봉합한다.
    실패 비크리티컬(자체 예외 로깅)."""
    try:
        from sqlmodel import Session

        from .db import engine
        from .health_monitor import scan_and_alert
        from .routers.sync import _post_webhook
        url = settings.OPERATOR_ALERT_WEBHOOK
        send = (lambda msg: _post_webhook(url, msg)) if url else None
        with Session(engine) as s:
            r = scan_and_alert(s, send=send)
        level = _log.warning if r["alerts_sent"] else _log.info
        level("[health] %d users evaluated, %d operator alerts sent",
              r["evaluated"], r["alerts_sent"])
    except Exception as e:
        _log.warning("[health] scan 실패: %s", e)


def _probe_summary_latency() -> None:
    """summary 응답시간 상시 계측 — 대표 종목상세를 주기적으로 내부 호출해 소요 ms를 로깅한다.

    유저 트래픽이 뜸해도 '어떤 시간에 접속해도 summary가 빠른가'를 연속 신호로 확인한다(번들
    압축 등 백그라운드 작업이 웹을 굶기면 이 값이 튄다). Railway 로그 `[probe] summary` grep으로
    추이 관측 — 배포 후 렉이 실제로 사라졌는지 추측 아닌 데이터로 판정하기 위한 측정 세팅."""
    try:
        import shutil
        import time as _t
        from quant_core import data_fetcher as _df
        from .routers import market
        t0 = _t.perf_counter()
        market.symbol_detail("005930", range="1y", light=1, user=None)
        dt = (_t.perf_counter() - t0) * 1000
        # 볼륨 디스크 사용률도 함께 로깅 — 재充 조기 감지(2026-07-07 디스크풀 인시던트 재발방지·
        # `[probe] summary … disk` grep). 디스크풀 시 쓰기 실패로 HOME 조회가 500/행이 됐었다.
        du = shutil.disk_usage(_df.DATA_DIR)
        _log.info("[probe] summary(005930) %.0fms · disk %.0f%% (%dMB free/%dMB)",
                  dt, du.used / du.total * 100, du.free >> 20, du.total >> 20)
    except Exception:
        _log.exception("summary 레이턴시 프로브 예외")


def _refresh_industry_prices() -> None:
    """산업분석 트리맵 가격 캐시를 장 마감·정산 후 비우고 공식 종가로 미리 데운다.

    트리맵 가격(_closes/_returns/_industry)은 _day(날짜) 키로 lru_cache돼 **장중 첫 로딩 값이
    종일 고정**된다. symbol_detail(금일 종가)은 매 요청 신선 조회라, 마감 후 트리맵 종가가
    금일 종가와 불일치하던 문제를 이 cron이 해소한다. refresh_prices는 가격 캐시만 비우고
    느린 D&A·상장주식수 캐시는 보존하므로(재조회 X) 가볍다."""
    from . import industry
    from .routers import market
    industry.refresh_prices()
    market.clear_price_cache()                # symbol_detail 원시 시세 캐시도 함께 무효화(금일 종가 정합)
    for name in industry.INDUSTRIES:          # 공식 종가로 즉시 재구성 → 다음 조회부터 정확
        try:
            industry.industry(name)
        except Exception:
            _log.exception("산업 가격 프리워밍 실패: %s", name)


def _build_scheduler() -> BackgroundScheduler:
    """매일 정기 갱신 cron 구성 (Phase 31 — 외부 publish 시각에 맞춰 재배치).

    구성만 — start()는 lifespan에서. 각 cron은 _run_with_retry로 감싸 실패 시
    backoff[5,15,30,60,120]분 재시도.

    job_defaults — 스케줄러 폭주 가드:
    - misfire_grace_time=3600: APScheduler 기본 grace(≈1s)는 GIL/IO로 1초+ 밀린
      무거운 job(naver 15분·dataset 수십분)을 조용히 misfire-drop시킨다(preview
      누락 잠재) → 1시간 유예.
    - coalesce=True: 밀린 실행이 여러 번 쌓여도 1회로 합쳐 실행.
    - max_instances=1: 같은 job(id 기준)의 동시 실행 금지 — retry job 고정 id
      (retry_{name})와 함께 같은 외부 소스 중복 fetch를 차단.
    """
    scheduler = BackgroundScheduler(
        timezone=_TZ_SEOUL,
        job_defaults={"misfire_grace_time": 3600, "coalesce": True,
                      "max_instances": 1})

    # 06:05 — KIS 마스터 1차 (06:00 first publish 직후)
    scheduler.add_job(
        lambda: _run_with_retry("kis_master_1st", _refresh_kis_master, scheduler),
        CronTrigger(hour=6, minute=5, timezone=_TZ_SEOUL),
        id="kis_master_1st", replace_existing=True)
    # 18:58 — KIS 마스터 2차 (18:55 last publish 직후, 당일 변경 모두 반영)
    scheduler.add_job(
        lambda: _run_with_retry("kis_master_2nd", _refresh_kis_master, scheduler),
        CronTrigger(hour=18, minute=58, timezone=_TZ_SEOUL),
        id="kis_master_2nd", replace_existing=True)

    # 07:30 — dataset 글로벌 (yfinance/FRED 06:15 publish + Binance/공포탐욕 09:00 publish 이후)
    scheduler.add_job(
        lambda: _run_with_retry("dataset_global", _refresh_global_dataset, scheduler),
        CronTrigger(hour=7, minute=30, timezone=_TZ_SEOUL),
        id="dataset_global", replace_existing=True)

    # 07:40 — 국가별 국채 수익률곡선(FRED/MOF/ECB, 무키·5개국) 일일 갱신. dataset_global 직후 스태거.
    scheduler.add_job(
        lambda: _run_with_retry("bonds_daily", _refresh_bonds, scheduler),
        CronTrigger(hour=7, minute=40, timezone=_TZ_SEOUL),
        id="bonds_daily", replace_existing=True)

    # 상시 summary 레이턴시 프로브 — 5분마다 대표 종목상세를 내부 호출해 소요 ms를 로깅.
    # 유저 트래픽과 무관하게 '어떤 시간에도 summary가 빠른가'를 연속 측정(번들 압축 등 배경작업이
    # 웹을 굶기면 이 값이 튄다). 실패 비크리티컬이라 _run_with_retry 미사용(자체 예외 로깅).
    scheduler.add_job(
        _probe_summary_latency,
        CronTrigger(minute="*/5", timezone=_TZ_SEOUL),
        id="summary_probe", replace_existing=True)

    # 자동매매 건강 dead-man's-switch — 15분마다 전 유저 건강 평가·신규 RED 운영자 통지.
    # on-ingest로 못 잡는 '침묵 유저'(push 없음)까지 cron이 잡는다(mwmw 사고 사각 봉합).
    scheduler.add_job(
        _health_scan,
        CronTrigger(minute="*/15", timezone=_TZ_SEOUL),
        id="health_scan", replace_existing=True)

    # 15:45 — KRX 정규장 1차 (15:40 publish 직후)
    scheduler.add_job(
        lambda: _run_with_retry("krx_1st", _refresh_krx, scheduler),
        CronTrigger(hour=15, minute=45, timezone=_TZ_SEOUL),
        id="krx_1st", replace_existing=True)

    # 16:05 + 16:35 — 산업분석 트리맵 가격 캐시 무효화 + 공식 종가 프리워밍.
    # 트리맵 가격은 _day(날짜) 키로 캐시돼 장중 첫 로딩 값이 종일 고정 → 마감 후 금일종가와
    # 불일치하던 문제 해소. 16:05(정산 직후) + 16:35(지연 종목 보정) 2회.
    for _hh, _mm in [(16, 5), (16, 35)]:
        scheduler.add_job(
            lambda: _run_with_retry("industry_prices", _refresh_industry_prices, scheduler),
            CronTrigger(hour=_hh, minute=_mm, timezone=_TZ_SEOUL),
            id=f"industry_prices_{_hh}_{_mm}", replace_existing=True)

    # 08:02 — KRX 선물(K200·KQ150) 만기물 패널 최근분 증분 + 연속물 재구성 (공식 KRX
    # API, T+1 08시 갱신 직후). KRX_API_KEY 미설정이면 fail-safe no-op. 백필은
    # krx_futures_panel_chunk(10분·K200 완주분) + kq150_panel_chunk(10분·상장일 소급)가 담당.
    #
    # 2026-07-15 재설계(문제 1·3·4 — kr-target-reconciliation.md §15 Phase 3):
    # · 08:02 시작 + 커스텀 백오프 [3,6,10,15]분 — 지연일 재시도가 08:05/08:11/08:21/08:36
    #   으로 발주창(로컬 선물 08:35·주식 08:55 pull) 안에 든다. 재시도 발동 조건은
    #   _refresh_krx_futures의 신선도 판정("직전 거래일 봉 수신") 예외.
    # · 구 09:35 재시도 cron 삭제 — 08:55 아침 사이클보다 늦어 당일 진입에 무의미했고
    #   (그날 진입은 이미 차단된 후), retry name 공유로 08:02의 재시도 큐를 비우는
    #   충돌도 있었다. 미수신분은 다음 아침 7일 재수집이 자동 백필.
    # job id·retry name은 도입 당시 이름(kospi_futures*) 유지 — 로그 연속성.
    scheduler.add_job(
        lambda: _run_with_retry("kospi_futures", _refresh_krx_futures, scheduler,
                                backoffs_min=[3, 6, 10, 15]),
        CronTrigger(hour=8, minute=2, timezone=_TZ_SEOUL),
        id="kospi_futures_am", replace_existing=True)

    # 17:00 — NAVER 펀더멘털 (publish 비공개, 보수적 추정)
    scheduler.add_job(
        lambda: _run_with_retry("naver", _refresh_naver, scheduler),
        CronTrigger(hour=17, minute=0, timezone=_TZ_SEOUL),
        id="naver", replace_existing=True)

    # 17:15 — 기술지표 (NAVER 직후, daily_metrics 내부 계산)
    scheduler.add_job(
        lambda: _run_with_retry("technical", _refresh_technical, scheduler),
        CronTrigger(hour=17, minute=15, timezone=_TZ_SEOUL),
        id="technical", replace_existing=True)

    # 03:30 — 기업개요 분기 갱신(매일 staleness 체크 → 90일 경과 시에만 FnGuide 재수집)
    scheduler.add_job(
        lambda: _run_with_retry("company_profiles", _refresh_company_profiles, scheduler),
        CronTrigger(hour=3, minute=30, timezone=_TZ_SEOUL),
        id="company_profiles", replace_existing=True)

    # 재무제표(Financials) — 분기/사업보고서 제출 마감일 저녁 20:10에 일괄 갱신.
    # 사업보고서 3/31 · 1Q 5/15 · 반기 8/14 · 3Q 11/14 (12월 결산 기준 법정 마감).
    # CronTrigger는 month·day를 교차곱으로 처리하므로 마감일마다 개별 job으로 등록.
    # 공시가 마감 무렵 FnGuide에 반영되므로 저녁에 받아 저장 → 탭은 저장본 즉시 서빙.
    for _mo, _dy in [(3, 31), (5, 15), (8, 14), (11, 14)]:
        scheduler.add_job(
            lambda: _run_with_retry("financials", _refresh_financials, scheduler),
            CronTrigger(month=_mo, day=_dy, hour=20, minute=10, timezone=_TZ_SEOUL),
            id=f"financials_{_mo}_{_dy}", replace_existing=True)

    # 18:10 — 정적 메타(섹터·상장폐지일) 사이드카. dataset_kr(18:15) invalidate 직전 →
    # 매니페스트가 새 사이드카로 재빌드, bundle(18:30)이 로컬 전파. FDR KRX-DESC/DELISTING.
    scheduler.add_job(
        lambda: _run_with_retry("static_meta", _refresh_static_meta, scheduler),
        CronTrigger(hour=18, minute=10, timezone=_TZ_SEOUL),
        id="static_meta", replace_existing=True)

    # 18:15 — 한국 dataset 갱신 (정규장 종가 + KRX 정정 반영, parquet 영구 저장)
    # 주: KRX 2차 cron은 제거됨. krx_cache.refresh()가 in-memory metrics를 통째
    # 교체해서 17:00 NAVER + 17:15 technical로 채워진 PER/PBR/RSI/MA 필드를
    # 모두 파괴했음. 정정 보정 가치 < 자동 선택 데이터 손실. 15:45 KRX 1차로 충분.
    scheduler.add_job(
        lambda: _run_with_retry("dataset_kr", _refresh_kr_dataset, scheduler),
        CronTrigger(hour=18, minute=15, timezone=_TZ_SEOUL),
        id="dataset_kr", replace_existing=True)

    # Dataset bundle packaging은 고정 시각 cron이 아니라 각 dataset refresh의
    # 끝(_package_bundle)에서 이벤트로 수행 — 부분/부재 bundle 창 제거 (D4-1·D4-6).

    # 10분마다 — KR 펀더멘털(OpenDART) 증분 백필 청크. 짧게 자주 → 재배포 폭주에도 정체 없이
    # 전진(한 방 17:30 의존이 폭주에 죽어 24h 0건이던 근본 수정, 2026-06-10). budget 1500=~150종목.
    scheduler.add_job(
        lambda: _run_with_retry("kr_fund_chunk", _backfill_kr_fundamentals_chunk, scheduler),
        CronTrigger(minute="*/10", timezone=_TZ_SEOUL),
        id="kr_fund_chunk", replace_existing=True)
    # 17:30 — 누적 펀더멘털 일일 attach(invalidate). dataset_kr(18:15) 직전 매니페스트 반영.
    scheduler.add_job(
        lambda: _run_with_retry("kr_fundamentals", _refresh_kr_fundamentals, scheduler),
        CronTrigger(hour=17, minute=30, timezone=_TZ_SEOUL),
        id="kr_fundamentals", replace_existing=True)

    # 애널 컨센서스 cron은 별도로 없다 — reports_kr(네이버)가 목록·목표가·투자의견 수집 시 컨센서스 패널까지
    # 재산출(한경 consensus_kr 은퇴). 아래 reports_chunk/reports_daily가 그 역할을 겸한다.
    # 애널 리포트(네이버, 무로그인) — 10분 백필 청크(페이지 커서→2010·상세 목표가/투자의견 부착) + 19:30 증분.
    scheduler.add_job(
        lambda: _run_with_retry("reports_chunk", _backfill_reports_chunk, scheduler),
        CronTrigger(minute="9-59/10", timezone=_TZ_SEOUL),             # :09,:19… — 다른 10분 cron과 스태거
        id="reports_chunk", replace_existing=True)
    scheduler.add_job(
        lambda: _run_with_retry("reports_daily", _refresh_reports, scheduler),
        CronTrigger(hour=19, minute=30, timezone=_TZ_SEOUL),           # 컨센서스(19:00) 후 스태거
        id="reports_daily", replace_existing=True)
    # KR 기관·외국인 수급(pykrx, KRX_ID/PW 필요) — 10분 백필 청크 + 16:30 일일 증분(KRX 마감 후).
    scheduler.add_job(
        lambda: _run_with_retry("flow_chunk", _backfill_flow_chunk, scheduler),
        CronTrigger(minute="5-59/10", timezone=_TZ_SEOUL),           # :05,:15… — 다른 10분 cron과 스태거
        id="flow_chunk", replace_existing=True)
    scheduler.add_job(
        lambda: _run_with_retry("flow_daily", _refresh_flow, scheduler),
        CronTrigger(hour=16, minute=30, timezone=_TZ_SEOUL),
        id="flow_daily", replace_existing=True)
    # KR 공매도 잔고(pykrx, KRX_ID/PW 필요) — 10분 백필 청크 + 16:35 일일 증분(KRX 마감 후).
    scheduler.add_job(
        lambda: _run_with_retry("shortbal_chunk", _backfill_shortbal_chunk, scheduler),
        CronTrigger(minute="7-59/10", timezone=_TZ_SEOUL),           # :07,:17… — 다른 10분 cron과 스태거
        id="shortbal_chunk", replace_existing=True)
    scheduler.add_job(
        lambda: _run_with_retry("shortbal_daily", _refresh_shortbal, scheduler),
        CronTrigger(hour=16, minute=35, timezone=_TZ_SEOUL),
        id="shortbal_daily", replace_existing=True)
    # KR 선물·ETF 투자자별 수급(KRX MDC, KRX_ID/PW 필요) — 10분 백필 청크 + 18:40 일일 증분
    # (당일 최종치는 18시 이후 제공 — ETF 04802 화면 명시·파생 동일 규약).
    scheduler.add_job(
        lambda: _run_with_retry("flowderiv_chunk", _backfill_flow_deriv_chunk, scheduler),
        CronTrigger(minute="2-59/10", timezone=_TZ_SEOUL),           # :02,:12… — 도입 시 :04가 putcall과 겹쳐 재스태거
        id="flowderiv_chunk", replace_existing=True)
    scheduler.add_job(
        lambda: _run_with_retry("flowderiv_daily", _refresh_flow_deriv, scheduler),
        CronTrigger(hour=18, minute=40, timezone=_TZ_SEOUL),
        id="flowderiv_daily", replace_existing=True)

    # KR OHLCV 깊이 백필(FDR, 무키) — 10분 청크로 기존 종목을 2010까지 소급 prepend.
    # 일일 dataset_kr 증분은 앞으로만 가서 과거를 못 채움 → 별도 백필이 그 갭만 메움(완료 시 0비용).
    scheduler.add_job(
        lambda: _run_with_retry("kr_ohlcv_depth", _backfill_kr_ohlcv_chunk, scheduler),
        CronTrigger(minute="7-59/10", timezone=_TZ_SEOUL),           # :07,:17… — fund(*/10)·flow(5)·reports(9)와 스태거
        id="kr_ohlcv_depth", replace_existing=True)

    # US OHLCV 깊이 백필(yfinance, 무키) — 10분 청크로 기존 종목을 2010까지 소급 prepend.
    # 과거 backfill_start=2015 코호트의 floor 이전 갭(KR과 동일 이유·동일 마커 규약).
    scheduler.add_job(
        lambda: _run_with_retry("us_ohlcv_depth", _backfill_us_ohlcv_chunk, scheduler),
        CronTrigger(minute="3-59/10", timezone=_TZ_SEOUL),           # :03,:13… — 다른 10분 청크들과 스태거
        id="us_ohlcv_depth", replace_existing=True)

    # KR 시장지표(공식 KRX API, KRX_API_KEY) — 10분 청크 백필(가벼운 지표 넓게·옵션 풋콜 좁게)
    # + 09:30 일일 증분. KRX_API_KEY 미설정이면 feed가 no-op(무영향).
    scheduler.add_job(
        lambda: _run_with_retry("krx_market_chunk", _backfill_krx_market_chunk, scheduler),
        CronTrigger(minute="8-59/10", timezone=_TZ_SEOUL),           # :08,:18… 스태거
        id="krx_market_chunk", replace_existing=True)
    scheduler.add_job(
        lambda: _run_with_retry("krx_putcall_chunk", _backfill_krx_putcall_chunk, scheduler),
        CronTrigger(minute="4-59/10", timezone=_TZ_SEOUL),           # :04,:14… 스태거 (옵션 무거움·좁은 윈도우)
        id="krx_putcall_chunk", replace_existing=True)
    scheduler.add_job(
        lambda: _run_with_retry("krx_etf_chunk", _backfill_krx_etf_chunk, scheduler),
        CronTrigger(minute="6-59/10", timezone=_TZ_SEOUL),           # :06,:16… 스태거 (ETF AUM/flow)
        id="krx_etf_chunk", replace_existing=True)
    scheduler.add_job(
        lambda: _run_with_retry("krx_futures_panel_chunk", _backfill_krx_futures_panel_chunk, scheduler),
        CronTrigger(minute="9-59/10", timezone=_TZ_SEOUL),           # :09,:19… 스태거 (선물 만기물 패널 2010 백필)
        id="krx_futures_panel_chunk", replace_existing=True)
    scheduler.add_job(
        lambda: _run_with_retry("kq150_panel_chunk", _backfill_kq150_panel_chunk, scheduler),
        CronTrigger(minute="1-59/10", timezone=_TZ_SEOUL),           # :01,:11… 스태거 (KQ150 상장일 소급 — 완주 후 무비용)
        id="kq150_panel_chunk", replace_existing=True)
    scheduler.add_job(
        lambda: _run_with_retry("krx_market_daily", _refresh_krx_market, scheduler),
        CronTrigger(hour=9, minute=30, timezone=_TZ_SEOUL),          # 공식 KRX API T+1 08시 갱신 후
        id="krx_market_daily", replace_existing=True)

    # KR 시총·거래대금(공식 KRX API `sto` — 포털 별도 신청) — 30분 청크 백필(60일 창,
    # 2서비스×평일 ~86콜/청크 → quota 관리) + 16:40 일일 증분. 미신청이면 실패로 정직히
    # 드러남(전용 fetcher가 에러 페이로드와 휴장을 구분 — 커서 침묵 전진 방지).
    scheduler.add_job(
        lambda: _run_with_retry("marketcap_chunk", _backfill_marketcap_chunk, scheduler),
        CronTrigger(minute="1-59/30", timezone=_TZ_SEOUL),           # :01,:31 — 10분 청크들과 스태거
        id="marketcap_chunk", replace_existing=True)
    scheduler.add_job(
        lambda: _run_with_retry("marketcap_daily", _refresh_marketcap, scheduler),
        CronTrigger(hour=9, minute=35, timezone=_TZ_SEOUL),          # sto는 T+1 08시 확정(라이브 실증) — 09:30 krx_market 뒤
        id="marketcap_daily", replace_existing=True)

    # US 공매도 거래량(FINRA Reg SHO, 무키) — 30분 청크 백필(90일 창·floor 2018-08)
    # + 09:40 일일 증분(당일 18:00 ET 게시 후).
    scheduler.add_job(
        lambda: _run_with_retry("shortvol_chunk", _backfill_shortvol_chunk, scheduler),
        CronTrigger(minute="16-59/30", timezone=_TZ_SEOUL),          # :16,:46 — marketcap(:01,:31)과 스태거
        id="shortvol_chunk", replace_existing=True)
    scheduler.add_job(
        lambda: _run_with_retry("shortvol_daily", _refresh_shortvol, scheduler),
        CronTrigger(hour=9, minute=40, timezone=_TZ_SEOUL),
        id="shortvol_daily", replace_existing=True)

    # 일요일 08:00 — 미국 S&P500 시가총액 (fast_info). 분기 변동 낮아 주1회.
    scheduler.add_job(
        lambda: _run_with_retry("us_market_caps", _refresh_us_market_caps, scheduler),
        CronTrigger(day_of_week="sun", hour=8, minute=0, timezone=_TZ_SEOUL),
        id="us_market_caps", replace_existing=True)

    # 일요일 09:00 — US 펀더멘털(SEC Company Facts). 분기 변동 낮아 주1회. 자체 invalidate.
    scheduler.add_job(
        lambda: _run_with_retry("us_fundamentals", _refresh_us_fundamentals, scheduler),
        CronTrigger(day_of_week="sun", hour=9, minute=0, timezone=_TZ_SEOUL),
        id="us_fundamentals", replace_existing=True)

    # 토요일 09:00 — US 선물 COT(CFTC). 금요일 15:30 ET(토 새벽 KST) 공개 직후 안전 마진.
    # 시장당 1콜=전체 이력 멱등 merge라 백필·증분·정정 흡수가 같은 호출(주 8콜).
    scheduler.add_job(
        lambda: _run_with_retry("cot_weekly", _refresh_cot, scheduler),
        CronTrigger(day_of_week="sat", hour=9, minute=0, timezone=_TZ_SEOUL),
        id="cot_weekly", replace_existing=True)

    # US 13F 기관보유(SEC 구조화·무키) — 30분 청크 백필(분기 ZIP 1개/run·최신→과거·메모리 상한).
    # 전 분기 소진 후 무비용 no-op(신규 분기 자동 픽업). CUSIP→ticker 맵은 최초 1회 FTD로 구축.
    scheduler.add_job(
        lambda: _run_with_retry("institutional_13f_chunk", _backfill_13f_chunk, scheduler),
        CronTrigger(minute="23-59/30", timezone=_TZ_SEOUL),          # :23,:53 — marketcap(:01)·shortvol(:16)과 스태거
        id="institutional_13f_chunk", replace_existing=True)

    # 03:00 — KR/US 시장 캘린더 일일 재빌드 (Q2+Q8).
    # exchange_calendars 패치(임시공휴일 추가)를 매일 받아서 stale 캘린더 방지.
    # 시각: 한국·미국 모두 새벽 — 사이클·시장 시간과 무관.
    scheduler.add_job(
        lambda: _run_with_retry("calendars", calendar_cache.refresh, scheduler),
        CronTrigger(hour=3, minute=0, timezone=_TZ_SEOUL),
        id="calendars", replace_existing=True)

    # 04:00 — DB pruning: HeartbeatEvent 30일·SyncSnapshot 30일(유저별 최신 1건
    # 보존). 무거래 새벽 시각 — 첫 실행 누적 대량분도 batch 삭제라 락이 짧다.
    # 정책·인덱스 의존은 db.prune_old_rows docstring 참조.
    scheduler.add_job(
        lambda: _run_with_retry("db_prune", _prune_db, scheduler),
        CronTrigger(hour=4, minute=0, timezone=_TZ_SEOUL),
        id="db_prune", replace_existing=True)

    return scheduler


def _schedule_startup_jobs() -> None:
    """시작 시 1회 초기 fetch·백필·프리워밍 (백그라운드 thread — 부팅 차단 없음).

    전 startup 잡의 단일 스케줄 진입점 — QP_SKIP_STARTUP_JOBS=1이면 lifespan이
    이 호출을 통째로 생략한다(개별 잡 분기 없음).

    ⚠ 폭주 가드 — 20개 job을 동시에 돌리면 배포 직후 CPU가 포화돼 첫 로그인 요청이
    2~3분씩 밀렸다(실측: 폭풍 중 /symbols 47~101s·1y 상세 13s → 폭풍 후 0.02~0.5s).
    세마포어(동시 2)로 직렬화 + 사용자 경험 비크리티컬 백필은 계층 지연 뒤 시작한다.
    데이터 신선도는 그대로(순서만 뒤로) — 어차피 전부 백그라운드 갱신이다.
    """
    _bg_sem = threading.BoundedSemaphore(2)

    def _bg(name: str, target, delay: float = 0.0) -> None:
        def _run():
            if delay:
                time.sleep(delay)
            with _bg_sem:
                _log.info("startup job 시작: %s", name)
                target()
        threading.Thread(target=_run, daemon=True, name=f"init-{name}").start()

    # 계층 0 — 첫 로그인 크리티컬(종목 목록·달력·시세 기반). 즉시.
    # /symbols 프리워밍은 세마포어 **밖**·즉시 — 마스터 파싱(GIL-heavy)과 직렬화하면
    # 캐시 준비가 수십 초 밀려 첫 유저가 그대로 블로킹된다(실측 96s). 순수 빌드는 ~4s이고
    # 마스터 도착으로 키가 바뀌면 요청 경로의 stale-while-revalidate가 알아서 재빌드한다.
    threading.Thread(target=backtest.prewarm_symbols, daemon=True, name="init-symbols").start()
    _bg("kis_master", _initial_master_refresh)
    _bg("calendar", _initial_calendar_refresh)                 # <1s
    # 계층 1 — 화면 데이터 소스(홈·산업). 1분 뒤.
    _bg("krx", _initial_krx_refresh, delay=60)
    _bg("naver_fund", _initial_naver_refresh, delay=60)
    _bg("technical", _initial_technical_refresh, delay=90)
    _bg("industry_prewarm", _initial_industry_prewarm, delay=90)
    _bg("summary_prewarm", _initial_summary_prewarm, delay=90)   # 종목상세 첫 진입 콜드 제거(목록캐시+대표종목)
    _bg("static_meta", _initial_static_meta_refresh, delay=120)
    # 계층 2 — 백테스트/스크리너 백필(무거움). 5분 뒤.
    _bg("kr_fundamentals", _initial_kr_fundamentals_refresh, delay=300)
    _bg("reports", _initial_reports_refresh, delay=690)        # 네이버 리포트 초기 증분(백필은 10분 chunk cron)
    _bg("flow", _initial_flow_refresh, delay=330)
    _bg("shortbal", _initial_shortbal_refresh, delay=420)      # 공매도 잔고 초기 백필(flow 뒤 스태거)
    _bg("krx_market", _initial_krx_market_refresh, delay=330)
    _bg("dataset_refresh", _initial_dataset_refresh, delay=360)
    # bundle packaging은 _refresh_global_dataset/_refresh_kr_dataset 끝에서
    # refresh '완료' 이벤트로 수행 — _package_bundle docstring 참조.
    _bg("us_market_caps", _initial_us_market_caps, delay=360)
    _bg("dataset_cache_prewarm", _initial_dataset_cache_prewarm, delay=420)
    _bg("orphan_prune", _prune_orphan_parquets_once, delay=0)   # 자체 sleep(540)·marker 게이트·1회성
    # 계층 3 — 대용량·저빈도 백필(OHLCV 깊이·COT·공매도·13F·재무 XBRL). 10분 뒤.
    _bg("kr_ohlcv_backfill", _initial_kr_ohlcv_backfill, delay=600)
    _bg("us_ohlcv_backfill", _initial_us_ohlcv_backfill, delay=600)
    _bg("cot", _initial_cot_refresh, delay=660)
    _bg("shortvol", _initial_shortvol_backfill, delay=660)
    _bg("13f", _initial_13f_backfill, delay=720)
    _bg("financials_prewarm", _initial_financials_prewarm, delay=720)
    _log.info("선물 grid 워머 thread 시작")
    futures.start_grid_warmer()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info("lifespan 시작 — DB 초기화")
    create_db_and_tables()

    # startup 1회성 잡 — QP_SKIP_STARTUP_JOBS=1(dev 전용·scripts/run_dev_server.py가 설정)
    # 이면 통째로 생략해 dev 머신 CPU/디스크를 아낀다. 미설정(프로덕션 Railway) 기본 =
    # 전부 실행. cron 정기 스케줄(아래)은 게이트와 무관하게 항상 구동.
    if os.environ.get("QP_SKIP_STARTUP_JOBS") == "1":
        _log.info("QP_SKIP_STARTUP_JOBS=1 — startup 1회성 잡 생략(dev fast-start)")
    else:
        _schedule_startup_jobs()

    # ── 매일 정기 갱신 — cron 구성·폭주 가드는 _build_scheduler 참조 ──────────
    scheduler = _build_scheduler()
    scheduler.start()
    _log.info("cron 시작: "
              "03:00 캘린더 · 04:00 DB pruning · 06:05 KIS-1 · 07:30 dataset글로벌 · "
              "15:45 KRX · 17:00 NAVER · 17:15 기술 · 18:15 dataset한국 · 18:58 KIS-2 KST "
              "(실패 시 backoff[5,15,30,60,120]분 재시도)")
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="MyStock API", version="0.2.0", lifespan=lifespan)

# 응답 gzip 압축 — /symbols 같은 대용량 JSON이 ~1/10로 줄어 전송 시간이 급감.
# Accept-Encoding: gzip을 보내는 클라이언트(브라우저)에만 적용.
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    # 로컬 개발에서 앱이 localhost / 127.0.0.1 / [::1] 어느 쪽으로 열려도 CORS 통과(포트 무관).
    # Windows에서 vite가 IPv6(::1)에 떠 origin이 http://[::1]:5173일 수 있어 정적 목록만으론 막힘.
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1|\[::1\]):\d+",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # ETag는 CORS-safelisted 응답 헤더가 아니라 expose 없이는 교차출처 브라우저
    # JS가 못 읽는다 → api.ts etagCache가 If-None-Match를 한 번도 못 보내
    # ETag/304 최적화 전체(P0-1·PR#75)가 웹에서 불발이었다(2026-06-10 라이브 실측).
    expose_headers=["ETag"],
)

app.include_router(auth.router)
app.include_router(chat_router.router)
app.include_router(strategies.router)
app.include_router(backtest.router)
app.include_router(sync.router)
app.include_router(commands.router)
app.include_router(market.router)
app.include_router(opinions_router.router)
app.include_router(portfolio.router)
app.include_router(screener_router.router)
app.include_router(settings_router.router)
app.include_router(dataset.router)
app.include_router(preview_router.router)
app.include_router(calendars_router.router)
app.include_router(trading_router.router)
app.include_router(ir_router.router)
app.include_router(ir_compile_router.router)   # NL 컴파일러(베타) — 미배포(로컬만), 배포 시 별도 결정
app.include_router(admin_router.router)
app.include_router(activity_router.router)
app.include_router(futures.router)


@app.get("/health")
def health():
    return {"status": "ok", "service": "quant-platform-api"}


def _require_health_token(x_health_token: str | None = Header(default=None)) -> None:
    """production에서 /health/*/refresh를 토큰으로 보호.

    무인증이면 누구나 호출해 상류(KIS/KRX/NAVER) rate limit·비용을 소모시킬 수 있다.
    development에서는 토큰 검증을 건너뛰어 로컬 진단을 그대로 허용.
    """
    if settings.ENV != "production":
        return
    if not x_health_token or x_health_token != settings.HEALTH_TOKEN:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "X-Health-Token 헤더가 필요합니다.")


@app.get("/health/master")
def master_health():
    """KIS 종목마스터 캐시 상태 — 인증 없이 진단용."""
    return kis_master_cache.get_status()


@app.post("/health/master/refresh")
def master_refresh(_: None = Depends(_require_health_token)):
    """KIS 마스터 즉시 갱신 — 진단/배포 직후 수동 트리거. production은 토큰 필요."""
    return kis_master_cache.refresh()


@app.get("/health/krx")
def krx_health():
    """KRX 스냅샷 캐시 상태 — 진단용."""
    return krx_cache.get_status()


@app.post("/health/krx/refresh")
def krx_refresh(_: None = Depends(_require_health_token)):
    """KRX 스냅샷 즉시 갱신 — 진단/검증용 수동 트리거. production은 토큰 필요."""
    return krx_cache.refresh()


@app.get("/krx/status")
def krx_status():
    """종목별 거래 상태(거래정지·관리·투자위험·투자경고) flag.

    Phase 48 — local app trader가 매수 직전 status 확인용. dataset 컬럼이
    아닌 별도 메타이므로 별도 endpoint. is_halt/is_managed는 KRX 마감 후
    NAVER 기준으로 일 1회 갱신되며 장중 새 거래정지는 다음 영업일에 반영된다.
    KIS broker가 발주 거부로 2차 안전망을 제공.
    """
    all_metrics = krx_cache.get_all_metrics()
    return {
        "status": {
            sym: {"is_halt": bool(m.get("is_halt")),
                   "is_managed": bool(m.get("is_managed"))}
            for sym, m in all_metrics.items()
        },
        "snapshot_date": krx_cache.get_status().get("snapshot_date"),
    }


@app.get("/health/naver")
def naver_health():
    """NAVER 펀더멘털 캐시 상태."""
    return naver_fundamentals.get_status()


@app.post("/health/naver/refresh")
def naver_refresh(_: None = Depends(_require_health_token)):
    """NAVER 펀더멘털 즉시 갱신 — 진단/검증용. production은 토큰 필요."""
    return naver_fundamentals.refresh()


@app.get("/health/technical")
def technical_health():
    """기술적 지표 캐시 상태."""
    return technical_cache.get_status()


@app.post("/health/technical/refresh")
def technical_refresh(_: None = Depends(_require_health_token)):
    """기술적 지표 즉시 갱신 — 진단/검증용. production은 토큰 필요."""
    return technical_cache.refresh()


@app.get("/health/calendars")
def calendars_health():
    """Q2+Q8 — 캘린더 캐시 상태 (built_at, KR/US 로드 여부)."""
    return calendar_cache.get_status()


@app.post("/health/calendars/refresh")
def calendars_refresh(_: None = Depends(_require_health_token)):
    """캘린더 즉시 재빌드 — 임시공휴일 발견 시 수동 트리거. production은 토큰 필요."""
    return calendar_cache.refresh()

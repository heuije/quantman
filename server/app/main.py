"""MyStock API 서버."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from . import (calendar_cache, data_cache, kis_master_cache, krx_cache,
                naver_fundamentals, technical_cache)
from .config import settings
from .db import create_db_and_tables
from .routers import (admin as admin_router, auth, backtest,
                       calendars as calendars_router, commands,
                       dataset, ir as ir_router, ir_compile as ir_compile_router,
                       market, futures, portfolio,
                       preview as preview_router,
                       screener as screener_router,
                       settings as settings_router, strategies, sync,
                       trading as trading_router)

_log = logging.getLogger("app.main")

# ── Fetch 재시도 헬퍼 ────────────────────────────────────────────────────────
#
# 외부 소스(KIS/KRX/NAVER/yfinance/FRED 등)는 일시 장애가 잦다. 정시 cron이
# 한 번 실패하면 다음날까지 stale인 게 큰 문제이므로, 실패 시 자동 재시도.
#
# 정책: 시도 N회, backoff [5, 15, 30, 60, 120]분. 최대 누적 ~230분 후 포기.
# 정시 cron이 다시 트리거되면 기존 retry 큐는 모두 cancel하고 다시 시작.

_RETRY_BACKOFFS_MIN = [5, 15, 30, 60, 120]
_RETRY_MAX_ATTEMPTS = 5


def _run_with_retry(name: str, fn: Callable[[], object],
                     scheduler: BackgroundScheduler) -> None:
    """fn을 즉시 실행, 실패 시 backoff 후 재시도 job을 scheduler에 등록.

    호출될 때마다 같은 name의 기존 retry job을 cancel — 정시 cron이 트리거되면
    이전 실패의 재시도 큐를 깨끗이 비우고 다시 시작한다.
    """
    # 기존 retry job 모두 cancel (정시 cron이 새로 시작될 때마다 큐 비움)
    for job in scheduler.get_jobs():
        if job.id.startswith(f"retry_{name}_"):
            try:
                scheduler.remove_job(job.id)
            except Exception:
                pass

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
            backoff_min = _RETRY_BACKOFFS_MIN[
                min(state["attempt"] - 1, len(_RETRY_BACKOFFS_MIN) - 1)]
            # tz-aware(KST) 시각으로 생성 — scheduler가 Asia/Seoul이므로 naive를
            # 쓰면 UTC 배포(Railway)에서 과거 시각으로 해석돼 misfire drop된다.
            run_at = datetime.now(ZoneInfo("Asia/Seoul")) + timedelta(minutes=backoff_min)
            _log.warning("[%s] %d분 후 재시도 (#%d) — %s",
                         name, backoff_min, state["attempt"] + 1,
                         run_at.strftime("%H:%M:%S"))
            scheduler.add_job(
                _attempt, trigger="date", run_date=run_at,
                id=f"retry_{name}_{state['attempt']}", replace_existing=True)

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
    """데이터 갱신 직후 preview 자동 갱신. 실패해도 cron 본 작업엔 영향 X.

    Phase 60 — Neon 연결 끊김(`server conn crashed?`) 시 fresh 연결로 1회 재시도.
    이게 없으면 데이터 fetch는 성공했는데 preview 갱신만 끊김으로 실패→예외가 여기서
    삼켜져 _run_with_retry가 재시도를 안 걸고, preview가 stale해진다(국장 후보결정 누락 근본원인).
    """
    try:
        from . import preview_engine
        from .db import call_with_disconnect_retry
        call_with_disconnect_retry(preview_engine.refresh_all_users_preview, data_source)
    except Exception:
        _log.exception("preview 자동 갱신 실패 [%s]", data_source)


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
    res = fundamental_kr.fetch(codes, [yr - 1, yr], budget_calls=1500)
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


def _package_bundle() -> None:
    """dataset bundle 재패키징 — 반드시 dataset refresh '완료' 뒤에서만 호출한다.

    이전 구조(고정 시각 cron 07:45/18:30 + boot+300s 고정 sleep)는 refresh '진행 중'
    디스크를 묶은 부분 bundle을 만들 수 있었고(재배포 직후엔 빈 디스크), bundle
    부재(410) 창과 결합해 로컬앱이 manifest 폴백 grind(시간 단위)로 추락 →
    `_REFRESH_LOCK` 컨보이로 발주 사이클이 통째로 블록되는 사고를 유발했다
    (2026-06-10 무발주 인시던트 RC-1/D4-1·D4-6,
    docs/incidents/2026-06-10-autotrading-week-retrospective.md).
    """
    from .routers import dataset as dataset_router
    dataset_router.build_bundle()


def _refresh_kospi_futures() -> None:
    """KOSPI200 선물 일봉 — 번들 CSV로 깊은 과거(2010+) base 시드 + KIS 최근분 증분 append.

    데이터포인트당 소스 1개(과거=CSV·신규=KIS): 번들 정적 CSV
    (data/static/kospi200_futures.csv = 투자닷컴 연속선물 2010+ 스냅샷)를 깊은 base로 쓰고, 그
    마지막 일자 이후만 KIS(FHKIF03020100, 모의 OK)로 채운다. parquet이 이미 깊으면(2010까지) CSV
    재시드를 건너뛰고 KIS 증분만 — 매 갱신 4천행 재기록 회피. KIS 미설정이어도 CSV 깊은 데이터로
    백테스트는 동작(fail-safe).

    ⚠ 미검증: 실제 KIS 연결(키·토큰·현행 최근월물 종목코드)은 자격증명 설정 시점에 검증 필요
    (런북: docs/futures-ir-design.md F2).
    ⚠ 한계: KIS 증분은 현행 최근월물 *원시가*라, 분기 롤 시 연속물(CSV) 대비 베이시스 점프 가능
    (최근 꼬리 한정·소폭). 정밀 역조정(back-adjust)은 후속 과제.
    """
    import os

    from quant_core import data_fetcher as dfm

    from .kis_data_client import get_kis_data_client
    from .kis_futures_master import resolve_kospi200_front_month

    sym = "코스피200선물"
    csv_path = Path(__file__).resolve().parent / "data" / "static" / "kospi200_futures.csv"

    # 1) 깊은 base — parquet이 없거나 2010까지 안 내려가면(얕음/오염) CSV로 재구축(덮어쓰기).
    existing = dfm._load_existing(sym)
    needs_deep = (existing is None or existing.empty
                  or existing.index.min() > datetime(2010, 12, 31))
    if needs_deep:
        if not csv_path.exists():
            _log.error("KOSPI200 선물 깊은 시드 실패 — 번들 CSV 없음: %s", csv_path)
            return
        existing = dfm.seed_from_clean_csv(sym, csv_path)
        _log.info("KOSPI200 선물 깊은 시드(CSV): 총 %d행 (%s~%s)",
                  len(existing), existing.index[0].date(), existing.index[-1].date())

    # 2) KIS 최근분 증분 — 마지막 보유 일자 이후 → 오늘. 키/코드 없으면 CSV base로 운영(fail-safe).
    client = get_kis_data_client()
    if client is None:
        _log.info("KOSPI200 선물 KIS 증분 skip(데이터키 미설정) — CSV 깊은 데이터로 운영")
        data_cache.invalidate()
        return
    # 종목코드: env 명시 override가 있으면 그것, 없으면 공개 마스터로 최근월물 자동 해석(분기 롤 자동).
    # 클라이언트가 있을 때만 해석(마스터 네트워크 다운로드를 키 없을 땐 생략).
    iscd = os.getenv("QP_KIS_KOSPI_FUT_ISCD", "").strip() or (resolve_kospi200_front_month() or "")
    if not iscd:
        _log.info("KOSPI200 선물 KIS 증분 skip(최근월물 코드 해석 실패) — CSV 깊은 데이터로 운영")
        data_cache.invalidate()
        return
    # F1(2026-06-11 인시던트): 미확정 당일 봉 차단 — KR선물 정규장 마감(15:45 KST) 전엔
    # 전일까지만 수집한다. 형성 중 봉이 canonical에 들어가면 '전일 종가' 의미가 깨지고
    # 장중에 재생성된 preview·백테스트가 미확정 값을 본다. 마감 후(16시~)엔 당일 확정 봉 수집.
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    end_day = now_kst if now_kst.hour >= 16 else now_kst - timedelta(days=1)
    today = end_day.strftime("%Y%m%d")
    since = existing.index.max().strftime("%Y%m%d")
    if since > today:
        # 과도기(이전 코드가 형성 중 당일 봉을 이미 저장) — 마감 후 cron이 확정 봉으로 덮어쓴다
        _log.info("KOSPI200 선물 KIS 증분 보류(장 마감 전, 당일 봉 미확정)")
        data_cache.invalidate()
        return
    merged = dfm.fetch_kis_futures_daily(sym, client.request, iscd=iscd, start=since, end=today)
    rng = f"{merged.index[0].date()}~{merged.index[-1].date()}" if len(merged) else "-"
    _log.info("KOSPI200 선물 KIS 증분 append: 총 %d행 (%s, 코드 %s, since %s)",
              len(merged), rng, iscd, since)
    data_cache.invalidate()


def _refresh_global_dataset() -> None:
    """글로벌 데이터셋 — yfinance/FDR ETF/FRED/Binance/공포탐욕 + 해외 on-demand 종목.

    외부 publish: 미국 마감(06:00 KST)·FRED(06:15)·Binance/공포탐욕(09:00 자정 UTC).
    cron 07:30이 모든 글로벌 소스 publish 후 안전 마진. 자동매매 사이클(08:55) 전.
    """
    from quant_core import data_fetcher

    # 매크로/자산/사용자 종목 (yfinance, FDR ETF, FRED, Binance, 공포탐욕)
    data_fetcher.fetch_all(verbose=False)

    # 해외 종목 시드를 fetch 전에 — 콜드스타트 레이스 방지. 시드는 원래
    # _refresh_kr_dataset(18:15)에만 있어, 첫 부팅 때 글로벌 초기 갱신이 kr보다
    # 먼저 돌면 managed_overseas가 비어 US OHLCV를 못 받았다. save_managed_overseas는
    # union이라 멱등. US 마스터 시드는 마스터 로드가 선행돼야 하므로 가드(멱등).
    if not kis_master_cache.get_master_set():
        _log.info("해외 시드 전 KIS 마스터 로드 (콜드스타트 가드)")
        kis_master_cache.refresh()
    _seed_sp500_overseas()         # 클래스주 yf 코드 보존
    _seed_us_master_overseas()     # KIS 미국 마스터 전체 (주식+ETF) — 국내와 대칭

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
    _trigger_preview("dataset_global")
    _package_bundle()


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
    from sqlmodel import Session, select
    from .db import engine
    from .models import Strategy

    # 1. KIS 마스터 KOSPI/KOSDAQ → 한국 종목 FDR fetch
    master_list = kis_master_cache.get_master_list()
    by_code = {m["symbol"]: m for m in master_list}
    kr_codes = sorted({m["symbol"] for m in master_list
                        if m.get("market") in ("KOSPI", "KOSDAQ")})
    data_fetcher.save_managed_kr_codes(kr_codes)
    if kr_codes:
        _log.info("한국 종목 fetch: %d 종목", len(kr_codes))
        data_fetcher.fetch_korean_stocks(kr_codes, verbose=False)

    # 2. 등록 전략의 해외 trade_symbol을 managed_overseas에 등록 (fetch는 글로벌 cron에서)
    overseas_new: list[dict] = []
    with Session(engine) as session:
        rows = session.exec(select(Strategy)).all()
        for s in rows:
            # IR 단일 체제 — 매매 타겟은 universe.symbols. 레거시 operand 행은 skip.
            if s.engine != "ir":
                continue
            syms = ((s.definition or {}).get("universe") or {}).get("symbols") or []
            for code in syms:
                meta = by_code.get(code)
                if meta is None or meta.get("market") in ("KOSPI", "KOSDAQ"):
                    continue
                overseas_new.append({"code": code, "name": meta.get("name", "")})

    existing_overseas = data_fetcher.load_managed_overseas()
    data_fetcher.save_managed_overseas(existing_overseas + overseas_new)
    _seed_sp500_overseas()        # S&P500 큐레이션 (클래스주 yf 코드 보존)
    _seed_us_master_overseas()    # KIS 미국 마스터 전체 (주식+ETF) — 국내와 대칭

    data_cache.invalidate()
    _trigger_preview("dataset_kr")
    _package_bundle()


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


def _initial_dataset_refresh():
    """시작 시 1회 dataset 갱신 — 기술적 지표 후 240초 지연 (외부 소스 동시 호출 분산)."""
    import time
    try:
        time.sleep(240)
        _log.info("dataset 초기 갱신 시작 (글로벌 + 한국)")
        # 선물은 단건이라 무거운 전체 갱신 앞에서 먼저 — 빠른 데이터·검증. 자체 try로 격리(선물
        # 실패가 메인 데이터 갱신을 막지 않게). KIS 데이터키 미설정이면 fail-safe no-op.
        try:
            _refresh_kospi_futures()
        except Exception:
            _log.exception("KOSPI200 선물 초기 수집 예외 — 16:00 cron 재시도")
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info("lifespan 시작 — DB 초기화")
    create_db_and_tables()

    # ── 시작 시 1회 초기 fetch (백그라운드 thread, 부팅 차단 방지) ─────────────
    _log.info("KIS 마스터 초기 다운로드 thread 시작")
    threading.Thread(target=_initial_master_refresh, daemon=True).start()
    # Q2+Q8: 캘린더 빌드는 매우 빠르고(<1s) 다른 fetch와 의존성 없어 별도 지연 없이 시작
    _log.info("캘린더 초기 빌드 thread 시작 (KR/US)")
    threading.Thread(target=_initial_calendar_refresh, daemon=True).start()
    _log.info("KRX 스냅샷 초기 fetch thread 시작")
    threading.Thread(target=_initial_krx_refresh, daemon=True).start()
    _log.info("NAVER 펀더멘털 초기 fetch thread 시작")
    threading.Thread(target=_initial_naver_refresh, daemon=True).start()
    _log.info("KR 펀더멘털(OpenDART) 초기 fetch thread 시작")
    threading.Thread(target=_initial_kr_fundamentals_refresh, daemon=True).start()
    _log.info("기술적 지표 초기 fetch thread 시작")
    threading.Thread(target=_initial_technical_refresh, daemon=True).start()
    _log.info("정적 메타 초기 fetch thread 시작 (섹터·상장폐지일)")
    threading.Thread(target=_initial_static_meta_refresh, daemon=True).start()
    _log.info("dataset 초기 갱신 thread 시작")
    threading.Thread(target=_initial_dataset_refresh, daemon=True).start()
    # bundle packaging은 _refresh_global_dataset/_refresh_kr_dataset 끝에서
    # refresh '완료' 이벤트로 수행 — _package_bundle docstring 참조.
    _log.info("미국 시가총액 초기 fetch thread 시작")
    threading.Thread(target=_initial_us_market_caps, daemon=True).start()
    _log.info("선물 grid 워머 thread 시작")
    futures.start_grid_warmer()

    # ── 매일 정기 갱신 (Phase 31 — 외부 publish 시각에 맞춰 재배치) ──────────
    # 각 cron은 _run_with_retry로 감싸 실패 시 backoff[5,15,30,60,120]분 재시도.
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    # 06:05 — KIS 마스터 1차 (06:00 first publish 직후)
    scheduler.add_job(
        lambda: _run_with_retry("kis_master_1st", _refresh_kis_master, scheduler),
        CronTrigger(hour=6, minute=5),
        id="kis_master_1st", replace_existing=True)
    # 18:58 — KIS 마스터 2차 (18:55 last publish 직후, 당일 변경 모두 반영)
    scheduler.add_job(
        lambda: _run_with_retry("kis_master_2nd", _refresh_kis_master, scheduler),
        CronTrigger(hour=18, minute=58),
        id="kis_master_2nd", replace_existing=True)

    # 07:30 — dataset 글로벌 (yfinance/FRED 06:15 publish + Binance/공포탐욕 09:00 publish 이후)
    scheduler.add_job(
        lambda: _run_with_retry("dataset_global", _refresh_global_dataset, scheduler),
        CronTrigger(hour=7, minute=30),
        id="dataset_global", replace_existing=True)

    # 15:45 — KRX 정규장 1차 (15:40 publish 직후)
    scheduler.add_job(
        lambda: _run_with_retry("krx_1st", _refresh_krx, scheduler),
        CronTrigger(hour=15, minute=45),
        id="krx_1st", replace_existing=True)

    # 16:00 — KOSPI200 선물 일봉 증분 (KIS, 선물 정규장 마감 직후). env(QP_KIS_DATA_APPKEY·
    # QP_KIS_KOSPI_FUT_ISCD) 미설정이면 fail-safe no-op이라 기존 갱신에 무영향.
    scheduler.add_job(
        lambda: _run_with_retry("kospi_futures", _refresh_kospi_futures, scheduler),
        CronTrigger(hour=16, minute=0),
        id="kospi_futures", replace_existing=True)

    # 17:00 — NAVER 펀더멘털 (publish 비공개, 보수적 추정)
    scheduler.add_job(
        lambda: _run_with_retry("naver", _refresh_naver, scheduler),
        CronTrigger(hour=17, minute=0),
        id="naver", replace_existing=True)

    # 17:15 — 기술지표 (NAVER 직후, daily_metrics 내부 계산)
    scheduler.add_job(
        lambda: _run_with_retry("technical", _refresh_technical, scheduler),
        CronTrigger(hour=17, minute=15),
        id="technical", replace_existing=True)

    # 18:10 — 정적 메타(섹터·상장폐지일) 사이드카. dataset_kr(18:15) invalidate 직전 →
    # 매니페스트가 새 사이드카로 재빌드, bundle(18:30)이 로컬 전파. FDR KRX-DESC/DELISTING.
    scheduler.add_job(
        lambda: _run_with_retry("static_meta", _refresh_static_meta, scheduler),
        CronTrigger(hour=18, minute=10),
        id="static_meta", replace_existing=True)

    # 18:15 — 한국 dataset 갱신 (정규장 종가 + KRX 정정 반영, parquet 영구 저장)
    # 주: KRX 2차 cron은 제거됨. krx_cache.refresh()가 in-memory metrics를 통째
    # 교체해서 17:00 NAVER + 17:15 technical로 채워진 PER/PBR/RSI/MA 필드를
    # 모두 파괴했음. 정정 보정 가치 < 자동 선택 데이터 손실. 15:45 KRX 1차로 충분.
    scheduler.add_job(
        lambda: _run_with_retry("dataset_kr", _refresh_kr_dataset, scheduler),
        CronTrigger(hour=18, minute=15),
        id="dataset_kr", replace_existing=True)

    # Dataset bundle packaging은 고정 시각 cron이 아니라 각 dataset refresh의
    # 끝(_package_bundle)에서 이벤트로 수행 — 부분/부재 bundle 창 제거 (D4-1·D4-6).

    # 10분마다 — KR 펀더멘털(OpenDART) 증분 백필 청크. 짧게 자주 → 재배포 폭주에도 정체 없이
    # 전진(한 방 17:30 의존이 폭주에 죽어 24h 0건이던 근본 수정, 2026-06-10). budget 1500=~150종목.
    scheduler.add_job(
        lambda: _run_with_retry("kr_fund_chunk", _backfill_kr_fundamentals_chunk, scheduler),
        CronTrigger(minute="*/10"),
        id="kr_fund_chunk", replace_existing=True)
    # 17:30 — 누적 펀더멘털 일일 attach(invalidate). dataset_kr(18:15) 직전 매니페스트 반영.
    scheduler.add_job(
        lambda: _run_with_retry("kr_fundamentals", _refresh_kr_fundamentals, scheduler),
        CronTrigger(hour=17, minute=30),
        id="kr_fundamentals", replace_existing=True)

    # 일요일 08:00 — 미국 S&P500 시가총액 (fast_info). 분기 변동 낮아 주1회.
    scheduler.add_job(
        lambda: _run_with_retry("us_market_caps", _refresh_us_market_caps, scheduler),
        CronTrigger(day_of_week="sun", hour=8, minute=0),
        id="us_market_caps", replace_existing=True)

    # 일요일 09:00 — US 펀더멘털(SEC Company Facts). 분기 변동 낮아 주1회. 자체 invalidate.
    scheduler.add_job(
        lambda: _run_with_retry("us_fundamentals", _refresh_us_fundamentals, scheduler),
        CronTrigger(day_of_week="sun", hour=9, minute=0),
        id="us_fundamentals", replace_existing=True)

    # 03:00 — KR/US 시장 캘린더 일일 재빌드 (Q2+Q8).
    # exchange_calendars 패치(임시공휴일 추가)를 매일 받아서 stale 캘린더 방지.
    # 시각: 한국·미국 모두 새벽 — 사이클·시장 시간과 무관.
    scheduler.add_job(
        lambda: _run_with_retry("calendars", calendar_cache.refresh, scheduler),
        CronTrigger(hour=3, minute=0),
        id="calendars", replace_existing=True)

    scheduler.start()
    _log.info("cron 시작: "
              "03:00 캘린더 · 06:05 KIS-1 · 07:30 dataset글로벌 · 15:45 KRX · "
              "17:00 NAVER · 17:15 기술 · 18:15 dataset한국 · 18:58 KIS-2 KST "
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
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # ETag는 CORS-safelisted 응답 헤더가 아니라 expose 없이는 교차출처 브라우저
    # JS가 못 읽는다 → api.ts etagCache가 If-None-Match를 한 번도 못 보내
    # ETag/304 최적화 전체(P0-1·PR#75)가 웹에서 불발이었다(2026-06-10 라이브 실측).
    expose_headers=["ETag"],
)

app.include_router(auth.router)
app.include_router(strategies.router)
app.include_router(backtest.router)
app.include_router(sync.router)
app.include_router(commands.router)
app.include_router(market.router)
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

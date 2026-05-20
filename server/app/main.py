"""퀀트 플랫폼 API 서버."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import kis_master_cache, krx_cache, naver_fundamentals, technical_cache
from .config import settings
from .db import create_db_and_tables
from .routers import (auth, backtest, commands, market, portfolio,
                       screener as screener_router,
                       settings as settings_router, strategies, sync)

_log = logging.getLogger("app.main")


def _initial_master_refresh():
    """시작 시 KIS 마스터 1회 다운로드 — 예외를 명시적으로 로그.

    daemon thread의 unhandled exception은 로거를 안 거치고 stderr로 가서 묻힐 수 있어,
    try-except로 감싸서 어떤 이유로 실패했는지 명확히 남긴다.
    """
    try:
        _log.info("KIS 마스터 초기 다운로드 시작")
        result = kis_master_cache.refresh()
        _log.info("KIS 마스터 초기 다운로드 결과: %s", result)
    except Exception:
        _log.exception("KIS 마스터 초기 다운로드 중 예외")


def _scheduled_master_refresh():
    """cron 호출용 wrapper — 같은 이유로 예외 로깅."""
    try:
        _log.info("KIS 마스터 정기 갱신 (06:00 KST) 시작")
        result = kis_master_cache.refresh()
        _log.info("KIS 마스터 정기 갱신 결과: %s", result)
    except Exception:
        _log.exception("KIS 마스터 정기 갱신 중 예외")


def _initial_krx_refresh():
    """KRX 스냅샷 초기 fetch — KIS 마스터 어느 정도 받을 시간 확보 후 시작."""
    import time
    try:
        time.sleep(45)            # KIS 마스터 우선 (종목명 매칭용)
        _log.info("KRX 스냅샷 초기 fetch 시작")
        result = krx_cache.refresh()
        _log.info("KRX 스냅샷 초기 fetch 결과: %s", result)
    except Exception:
        _log.exception("KRX 스냅샷 초기 fetch 중 예외")


def _scheduled_krx_refresh():
    """매일 16:30 KST — KRX 한국 마감 후 일별 데이터 갱신."""
    try:
        _log.info("KRX 스냅샷 정기 갱신 (16:30 KST) 시작")
        result = krx_cache.refresh()
        _log.info("KRX 스냅샷 정기 갱신 결과: %s", result)
    except Exception:
        _log.exception("KRX 스냅샷 정기 갱신 중 예외")


def _initial_naver_refresh():
    """NAVER 펀더멘털 초기 fetch — KRX 캐시 채워진 후 시작."""
    import time
    try:
        time.sleep(120)           # KRX 캐시 채울 시간 확보 (~90초)
        _log.info("NAVER 펀더멘털 초기 fetch 시작")
        result = naver_fundamentals.refresh()
        _log.info("NAVER 펀더멘털 초기 fetch 결과: %s", result)
    except Exception:
        _log.exception("NAVER 펀더멘털 초기 fetch 중 예외")


def _scheduled_naver_refresh():
    """매일 17:30 KST — KRX 직후 NAVER 펀더멘털 갱신."""
    try:
        _log.info("NAVER 펀더멘털 정기 갱신 (17:30 KST) 시작")
        result = naver_fundamentals.refresh()
        _log.info("NAVER 펀더멘털 정기 갱신 결과: %s", result)
    except Exception:
        _log.exception("NAVER 펀더멘털 정기 갱신 중 예외")


def _initial_technical_refresh():
    """기술적 지표 초기 fetch — NAVER 후 60초 지연."""
    import time
    try:
        time.sleep(180)
        _log.info("기술적 지표 초기 fetch 시작")
        result = technical_cache.refresh()
        _log.info("기술적 지표 초기 fetch 결과: %s", result)
    except Exception:
        _log.exception("기술적 지표 초기 fetch 중 예외")


def _scheduled_technical_refresh():
    """매일 17:45 KST — NAVER 직후 기술적 지표 갱신."""
    try:
        _log.info("기술적 지표 정기 갱신 (17:45 KST) 시작")
        result = technical_cache.refresh()
        _log.info("기술적 지표 정기 갱신 결과: %s", result)
    except Exception:
        _log.exception("기술적 지표 정기 갱신 중 예외")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _log.info("lifespan 시작 — DB 초기화")
    create_db_and_tables()

    # KIS 종목마스터 — 시작 시 1회 다운로드 (백그라운드, 부팅 차단 방지)
    _log.info("KIS 마스터 초기 다운로드 thread 시작")
    threading.Thread(target=_initial_master_refresh, daemon=True).start()

    # KRX 스냅샷 — 시작 시 1회 (KIS 마스터 후 45초 지연)
    _log.info("KRX 스냅샷 초기 fetch thread 시작")
    threading.Thread(target=_initial_krx_refresh, daemon=True).start()

    # NAVER 펀더멘털 — 시작 시 1회 (KRX 후 120초 지연)
    _log.info("NAVER 펀더멘털 초기 fetch thread 시작")
    threading.Thread(target=_initial_naver_refresh, daemon=True).start()

    # 기술적 지표 — 시작 시 1회 (NAVER 후 180초 지연)
    _log.info("기술적 지표 초기 fetch thread 시작")
    threading.Thread(target=_initial_technical_refresh, daemon=True).start()

    # 매일 06:00 KST 자동 갱신
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")
    scheduler.add_job(_scheduled_master_refresh,
                       CronTrigger(hour=6, minute=0),
                       id="kis_master_refresh", replace_existing=True)
    scheduler.add_job(_scheduled_krx_refresh,
                       CronTrigger(hour=16, minute=30),
                       id="krx_cache_refresh", replace_existing=True)
    scheduler.add_job(_scheduled_naver_refresh,
                       CronTrigger(hour=17, minute=30),
                       id="naver_fundamentals_refresh", replace_existing=True)
    scheduler.add_job(_scheduled_technical_refresh,
                       CronTrigger(hour=17, minute=45),
                       id="technical_cache_refresh", replace_existing=True)
    scheduler.start()
    _log.info("cron 시작: KIS 06:00 · KRX 16:30 · NAVER 17:30 · 기술적 17:45 KST")
    app.state.scheduler = scheduler
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="퀀트 플랫폼 API", version="0.2.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


@app.get("/health")
def health():
    return {"status": "ok", "service": "quant-platform-api"}


@app.get("/health/master")
def master_health():
    """KIS 종목마스터 캐시 상태 — 인증 없이 진단용."""
    return kis_master_cache.get_status()


@app.post("/health/master/refresh")
def master_refresh():
    """KIS 마스터 즉시 갱신 — 진단/배포 직후 수동 트리거.

    공개 엔드포인트지만 부작용은 동일 데이터 다운로드뿐 (악용 무관).
    """
    return kis_master_cache.refresh()


@app.get("/health/krx")
def krx_health():
    """KRX 스냅샷 캐시 상태 — 진단용."""
    return krx_cache.get_status()


@app.post("/health/krx/refresh")
def krx_refresh():
    """KRX 스냅샷 즉시 갱신 — 진단/검증용 수동 트리거.

    공용 시세성 데이터라 부작용 없음 (KRX 공식 데이터 1회 다운로드).
    """
    return krx_cache.refresh()


@app.get("/health/naver")
def naver_health():
    """NAVER 펀더멘털 캐시 상태."""
    return naver_fundamentals.get_status()


@app.post("/health/naver/refresh")
def naver_refresh():
    """NAVER 펀더멘털 즉시 갱신 — 진단/검증용."""
    return naver_fundamentals.refresh()


@app.get("/health/technical")
def technical_health():
    """기술적 지표 캐시 상태."""
    return technical_cache.get_status()


@app.post("/health/technical/refresh")
def technical_refresh():
    """기술적 지표 즉시 갱신 — 진단/검증용."""
    return technical_cache.refresh()

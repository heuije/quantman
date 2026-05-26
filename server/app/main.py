"""퀀트 플랫폼 API 서버."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
import time
from datetime import datetime, timedelta
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
from .routers import (auth, backtest, calendars as calendars_router, commands,
                       dataset, market, portfolio,
                       preview as preview_router,
                       screener as screener_router,
                       settings as settings_router, strategies, sync)

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
    """데이터 갱신 직후 preview 자동 갱신. 실패해도 cron 본 작업엔 영향 X."""
    try:
        from . import preview_engine
        preview_engine.refresh_all_users_preview(data_source)
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
    # preview trigger 없음 — 위와 동일


def _refresh_technical() -> None:
    result = technical_cache.refresh()
    _log.info("기술적 지표 갱신 결과: %s", result)
    # screener 입력(KRX+NAVER+technical)이 모두 완성된 시점 — 자동 선택 preview 트리거
    _trigger_preview("technical")


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


def _initial_technical_refresh():
    import time
    try:
        time.sleep(180)
        _log.info("기술적 지표 초기 fetch 시작")
        _refresh_technical()
    except Exception:
        _log.exception("기술적 지표 초기 fetch 중 예외 — 정시 cron 재시도")


def _refresh_dataset_all() -> None:
    """글로벌 + 한국 dataset 동시 갱신 — 시작 시 초기 fetch에만 사용.
    정시 cron은 글로벌(07:30)·한국(18:15)이 각자 호출."""
    _refresh_global_dataset()
    _refresh_kr_dataset()


def _refresh_global_dataset() -> None:
    """글로벌 데이터셋 — yfinance/FDR ETF/FRED/Binance/공포탐욕 + 해외 on-demand 종목.

    외부 publish: 미국 마감(06:00 KST)·FRED(06:15)·Binance/공포탐욕(09:00 자정 UTC).
    cron 07:30이 모든 글로벌 소스 publish 후 안전 마진. 자동매매 사이클(08:55) 전.
    """
    from quant_core import data_fetcher

    # 매크로/자산/사용자 종목 (yfinance, FDR ETF, FRED, Binance, 공포탐욕)
    data_fetcher.fetch_all(verbose=False)

    # S&P500 큐레이션 유니버스를 fetch 전에 시드 — 콜드스타트 레이스 방지.
    # 시드는 원래 _refresh_kr_dataset(18:15)에만 있어, 첫 부팅 때 글로벌 초기 갱신이
    # kr보다 먼저 돌면 managed_overseas가 비어 US OHLCV를 못 받고 us_metrics가
    # 0이 됐다(다음 07:30 cron까지). 글로벌에서도 시드해 항상 S&P500을 포함한다.
    # save_managed_overseas가 union이라 멱등 — 중복 호출 안전.
    _seed_sp500_overseas()

    # 해외 종목(S&P500 + on-demand) — yfinance 의존이라 글로벌 cron에 묶음
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
    from quant_core import data_fetcher, parse_trade_symbols
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
            tsym = (s.definition or {}).get("trade_symbol", "")
            mode, syms = parse_trade_symbols(tsym)
            if mode == "screener":
                continue
            for code in syms:
                meta = by_code.get(code)
                if meta is None or meta.get("market") in ("KOSPI", "KOSDAQ"):
                    continue
                overseas_new.append({"code": code, "name": meta.get("name", "")})

    existing_overseas = data_fetcher.load_managed_overseas()
    data_fetcher.save_managed_overseas(existing_overseas + overseas_new)
    _seed_sp500_overseas()      # S&P500 큐레이션 유니버스 추가 (미국 자동선택)

    data_cache.invalidate()
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


def _initial_dataset_refresh():
    """시작 시 1회 dataset 갱신 — 기술적 지표 후 240초 지연 (외부 소스 동시 호출 분산)."""
    import time
    try:
        time.sleep(240)
        _log.info("dataset 초기 갱신 시작 (글로벌 + 한국)")
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
    _log.info("기술적 지표 초기 fetch thread 시작")
    threading.Thread(target=_initial_technical_refresh, daemon=True).start()
    _log.info("dataset 초기 갱신 thread 시작")
    threading.Thread(target=_initial_dataset_refresh, daemon=True).start()
    # Phase 58-C — dataset 초기 갱신 후 bundle 한 번 packaging (사용자가 다음
    # cron 도래 전에도 bundle 받을 수 있게). dataset thread가 끝난 후 호출.
    def _initial_bundle_after_dataset():
        # dataset 초기 fetch가 끝날 때까지 충분히 기다림 (보수적 5분).
        # 길게 잡아도 사용자 영향 없음 — daemon thread.
        time.sleep(300)
        try:
            from .routers import dataset as dataset_router
            dataset_router.build_bundle()
        except Exception as e:
            _log.warning("초기 bundle packaging 실패: %s", e)
    threading.Thread(target=_initial_bundle_after_dataset,
                     daemon=True, name="bundle-initial").start()
    _log.info("미국 시가총액 초기 fetch thread 시작")
    threading.Thread(target=_initial_us_market_caps, daemon=True).start()

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

    # 18:15 — 한국 dataset 갱신 (정규장 종가 + KRX 정정 반영, parquet 영구 저장)
    # 주: KRX 2차 cron은 제거됨. krx_cache.refresh()가 in-memory metrics를 통째
    # 교체해서 17:00 NAVER + 17:15 technical로 채워진 PER/PBR/RSI/MA 필드를
    # 모두 파괴했음. 정정 보정 가치 < 자동 선택 데이터 손실. 15:45 KRX 1차로 충분.
    scheduler.add_job(
        lambda: _run_with_retry("dataset_kr", _refresh_kr_dataset, scheduler),
        CronTrigger(hour=18, minute=15),
        id="dataset_kr", replace_existing=True)

    # Phase 58-C — Dataset bundle packaging.
    # 글로벌 dataset(07:30) + 한국 dataset(18:15) 갱신 직후 packaging.
    # 사용자 로컬앱은 08:00 KST sync로 글로벌 + 어제 한국 close 묶음을 받음.
    # 한국 close(18:15) 후 packaging은 다음 날 사용자 sync에 반영.
    def _do_package_bundle():
        from .routers import dataset as dataset_router
        return dataset_router.build_bundle()
    scheduler.add_job(
        lambda: _run_with_retry("bundle_morning", _do_package_bundle, scheduler),
        CronTrigger(hour=7, minute=45),
        id="bundle_morning", replace_existing=True)
    scheduler.add_job(
        lambda: _run_with_retry("bundle_evening", _do_package_bundle, scheduler),
        CronTrigger(hour=18, minute=30),
        id="bundle_evening", replace_existing=True)

    # 일요일 08:00 — 미국 S&P500 시가총액 (fast_info). 분기 변동 낮아 주1회.
    scheduler.add_job(
        lambda: _run_with_retry("us_market_caps", _refresh_us_market_caps, scheduler),
        CronTrigger(day_of_week="sun", hour=8, minute=0),
        id="us_market_caps", replace_existing=True)

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


app = FastAPI(title="퀀트 플랫폼 API", version="0.2.0", lifespan=lifespan)

# 응답 gzip 압축 — /symbols 같은 대용량 JSON이 ~1/10로 줄어 전송 시간이 급감.
# Accept-Encoding: gzip을 보내는 클라이언트(브라우저)에만 적용.
app.add_middleware(GZipMiddleware, minimum_size=1000)

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
app.include_router(dataset.router)
app.include_router(preview_router.router)
app.include_router(calendars_router.router)


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

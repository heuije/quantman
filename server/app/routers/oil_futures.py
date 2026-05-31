"""WTI 원유선물 분석 API (Phase 2 — 대시보드 백엔드).

quant_core.oil_futures 모듈을 HTTP로 노출. 가격 통계 데이터라 인증 불요로
공개. 대시보드(web/src/pages/OilFutures.tsx)가 호출.

엔드포인트:
- GET  /oil-futures/data-info        데이터 메타 (기간/행수/가격범위)
- GET  /oil-futures/prices           일봉 시계열 (차트용, 기간 필터 옵션)
- GET  /oil-futures/grid             전체 grid (히트맵·표용)
- GET  /oil-futures/signals          특정 (type, threshold) 신호 목록
- POST /oil-futures/backtest         단일 조합 백테스트 (trades + equity)
- POST /oil-futures/walkforward      train/test 분할 검증
"""

from __future__ import annotations

import gc
import math
import re
import time
from functools import lru_cache
from typing import Literal, Optional

import pandas as pd
import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quant_core.oil_futures import (
    CostModel,
    ExitRules,
    Side,
    generate_signals,
    grid_search,
    load_wti,
    run_backtest,
    summarize,
    walk_forward,
)

router = APIRouter(prefix="/oil-futures", tags=["oil-futures"])

# 임계값 grid — Railway 무료 티어 메모리 한계로 $10 단위 사용 ($1 단위는
# OOM 발생: 488 셀 × 5759행 백테스트 = 메모리 압박). 사용자가 ?shorts=...
# 명시 시 임의 범위 가능. 추후 결과 캐시 layer 추가 후 $1 재시도 예정.
DEFAULT_SHORTS = [80, 90, 100, 110, 120, 130, 140, 150]
DEFAULT_LONGS = [10, 20, 30, 40, 50, 60]
# Horizon (영업일 기준 보유 기간): 단기~장기 비교
# 365일 = 약 1.5 캘린더 년 (영업일 ≈ 252일/년)
DEFAULT_HORIZONS = [20, 40, 60, 120, 180, 240, 365]


# DataFrame 로드는 첫 호출에만 (~5MB CSV, 메모리 상주) — 매 요청 디스크 IO 회피.
@lru_cache(maxsize=1)
def _df() -> pd.DataFrame:
    return load_wti()


# ───── 실시간 현재가 (investing.com 우선, yfinance 폴백) ─────────────────
#
# investing.com은 anti-bot(Cloudflare)을 쓴다. 가정용 IP에선 HTML 응답을 주지만
# 데이터센터 IP(Railway)에선 차단될 수 있다 — 이건 외부 시스템의 진짜 한계이므로
# yfinance(CL=F, ~15분 지연) 폴백이 정당하다 (CLAUDE.md 근본원인 원칙: 외부 한계
# 시에만 fallback 허용). 응답엔 어느 소스를 썼는지 source로 명시한다.
#
# 60초 캐시 — 매 요청마다 1.1MB HTML 받지 않도록 (메모리·rate-limit 보호).

_PRICE_CACHE: dict = {"data": None, "ts": 0.0}
_PRICE_TTL = 60.0
_INVESTING_URL = "https://www.investing.com/commodities/crude-oil"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36")


class LatestPrice(BaseModel):
    price: float
    change: Optional[float]          # 전일 대비 절대값
    change_pct: Optional[float]      # 전일 대비 % (소수, 예: -0.0173)
    source: str                      # "investing.com" | "yfinance" | "csv"
    delayed: bool                    # 실시간(False) vs 지연(True)
    fetched_at: str                  # ISO 시각 (UTC)


def _fetch_investing() -> Optional[LatestPrice]:
    """investing.com WTI 페이지에서 현재가 파싱. 차단·실패 시 None."""
    try:
        r = requests.get(_INVESTING_URL, headers={"User-Agent": _UA}, timeout=8)
        if r.status_code != 200:
            return None
        html = r.text
        m_price = re.search(r'data-test="instrument-price-last">([0-9.,]+)', html)
        if not m_price:
            return None
        price = float(m_price.group(1).replace(",", ""))
        m_chg = re.search(r'data-test="instrument-price-change">([+-]?[0-9.,]+)', html)
        m_pct = re.search(
            r'data-test="instrument-price-change-percent">\(([+-]?[0-9.,]+)%\)', html)
        change = float(m_chg.group(1).replace(",", "")) if m_chg else None
        change_pct = (float(m_pct.group(1).replace(",", "")) / 100.0) if m_pct else None
        return LatestPrice(
            price=price, change=change, change_pct=change_pct,
            source="investing.com", delayed=False,
            fetched_at=pd.Timestamp.utcnow().isoformat(),
        )
    except Exception:
        return None


def _fetch_yfinance() -> Optional[LatestPrice]:
    """yfinance CL=F 최근 2일 종가로 현재가 + 전일대비 산출 (~15분 지연)."""
    try:
        import yfinance as yf
        hist = yf.download("CL=F", period="5d", progress=False, auto_adjust=False)
        if hist.empty:
            return None
        closes = hist["Close"].dropna()
        if hasattr(closes, "iloc") and closes.ndim > 1:
            closes = closes.iloc[:, 0]
        price = float(closes.iloc[-1])
        prev = float(closes.iloc[-2]) if len(closes) >= 2 else price
        change = price - prev
        change_pct = (price / prev - 1) if prev else None
        return LatestPrice(
            price=price, change=change, change_pct=change_pct,
            source="yfinance", delayed=True,
            fetched_at=pd.Timestamp.utcnow().isoformat(),
        )
    except Exception:
        return None


@router.get("/latest-price", response_model=LatestPrice)
def latest_price():
    """WTI 실시간 현재가. investing.com 우선 → 실패 시 yfinance → CSV 마지막 종가."""
    now = time.time()
    if _PRICE_CACHE["data"] is not None and (now - _PRICE_CACHE["ts"]) < _PRICE_TTL:
        return _PRICE_CACHE["data"]

    result = _fetch_investing() or _fetch_yfinance()
    if result is None:
        # 최후 폴백 — CSV 마지막 종가 (지연 명시)
        df = _df()
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) >= 2 else last
        result = LatestPrice(
            price=float(last["close"]),
            change=float(last["close"] - prev["close"]),
            change_pct=float(last["close"] / prev["close"] - 1) if prev["close"] else None,
            source="csv", delayed=True,
            fetched_at=pd.Timestamp.utcnow().isoformat(),
        )

    _PRICE_CACHE["data"] = result
    _PRICE_CACHE["ts"] = now
    return result


# ─── Response models ────────────────────────────────────────────────────

class DataInfo(BaseModel):
    n_rows: int
    start_date: str
    end_date: str
    price_min: float
    price_max: float


class PricePoint(BaseModel):
    date: str
    close: float
    high: float
    low: float


class GridCellOut(BaseModel):
    side: str
    threshold: float
    horizon: int
    n_trades: int
    win_rate: float
    avg_return: float
    sharpe: float
    mdd_usd: float
    gross_profit_usd: float     # 이긴 거래만 net PnL 합 (양수)
    gross_loss_usd: float       # 진 거래만 net PnL 합 (음수)
    net_pnl_usd: float
    profit_factor: Optional[float]   # None = 손실 없음(∞ 대체)
    low_sample: bool


class SignalEvent(BaseModel):
    date: str
    side: str
    threshold: float
    entry_ref_close: float


class EquityPoint(BaseModel):
    date: str
    cumulative_usd: float


class SummaryOut(BaseModel):
    n_trades: int
    win_rate: float
    avg_return: float
    avg_win: float
    avg_loss: float
    profit_factor: Optional[float]
    sharpe: float
    mdd_usd: float
    gross_profit_usd: float
    gross_loss_usd: float
    net_pnl_usd: float
    # 🅐 MAE/MFE (장중 평가손익)
    worst_mae_usd: float
    avg_mae_usd: float
    avg_mfe_usd: float
    # 🅑 streak
    max_win_streak: int
    max_loss_streak: int
    # 선물 만기 강제 롤오버
    total_rollovers: int
    total_roll_cost_usd: float
    low_sample: bool


class TradeOut(BaseModel):
    signal_date: str
    side: str
    threshold: float
    entry_date: str
    entry_price: float
    exit_date: str
    exit_price: float
    horizon_days: int
    return_pct: float
    net_pnl_usd: float
    mae_usd: float    # 🅐 보유 중 최악 평가손실 (음수, 1계약)
    mfe_usd: float    # 🅐 보유 중 최고 평가이익 (양수, 1계약)
    exit_reason: str  # 'horizon' | 'stop_loss' | 'take_profit'
    num_rollovers: int       # 보유 중 만기 통과(강제 롤오버) 횟수
    roll_cost_usd: float     # 롤 비용 (음수 또는 0)


class BacktestResponse(BaseModel):
    summary: SummaryOut
    trades: list[TradeOut]
    equity_curve: list[EquityPoint]                # realized (기존)
    portfolio_equity_curve: list[EquityPoint]      # 🅓 mark-to-market 시가평가
    portfolio_mdd_usd: float                       # 🅓 시가평가 MDD


class BestCellOut(BaseModel):
    side: str
    threshold: float
    horizon: int
    summary: SummaryOut


class WalkForwardResponse(BaseModel):
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    best_in_sample: BestCellOut
    out_of_sample: SummaryOut


# ─── Helpers ────────────────────────────────────────────────────────────

def _pf(x: float) -> Optional[float]:
    """profit_factor: inf → None (JSON에 표현 불가, 프론트에서 '손실 없음' 처리)."""
    if x == float("inf") or x != x:  # inf or nan
        return None
    return float(x)


def _parse_csv_floats(s: str | None) -> list[float]:
    if not s:
        return []
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def _parse_csv_ints(s: str | None) -> list[int]:
    if not s:
        return []
    return [int(x.strip()) for x in s.split(",") if x.strip()]


# ─── Endpoints ──────────────────────────────────────────────────────────

@router.get("/data-info", response_model=DataInfo)
def data_info():
    df = _df()
    return DataInfo(
        n_rows=len(df),
        start_date=str(df["date"].iloc[0].date()),
        end_date=str(df["date"].iloc[-1].date()),
        price_min=float(df["close"].min()),
        price_max=float(df["close"].max()),
    )


@router.get("/prices", response_model=list[PricePoint])
def prices(start: Optional[str] = None, end: Optional[str] = None):
    df = _df()
    if start:
        df = df[df["date"] >= pd.Timestamp(start)]
    if end:
        df = df[df["date"] <= pd.Timestamp(end)]
    return [
        PricePoint(
            date=str(d.date()),
            close=float(c),
            high=float(h),
            low=float(l),
        )
        for d, c, h, l in zip(df["date"], df["close"], df["high"], df["low"])
    ]


# 결과 캐시 — 같은 파라미터 재계산 방지로 메모리 안정화 (Railway 무료 티어 보호).
# 키: (shorts, longs, horizons, commission, slippage) 튜플.
_GRID_CACHE: dict[tuple, list[dict]] = {}


@router.get("/grid", response_model=list[GridCellOut])
def grid(
    shorts: str = "",
    longs: str = "",
    horizons: str = "",
    commission: float = 2.5,
    slippage_ticks: int = 1,
):
    """모든 (side, threshold, horizon) 조합 백테스트.

    파라미터 미지정 시 엑셀 원본과 동일한 기본 범위.
    결과는 메모리에 캐시 — 같은 파라미터 재호출 시 즉시 응답.
    """
    df = _df()
    s = tuple(_parse_csv_floats(shorts) or DEFAULT_SHORTS)
    l = tuple(_parse_csv_floats(longs) or DEFAULT_LONGS)
    h = tuple(_parse_csv_ints(horizons) or DEFAULT_HORIZONS)
    key = (s, l, h, commission, slippage_ticks)
    if key in _GRID_CACHE:
        return _GRID_CACHE[key]

    cells = grid_search(df, s, l, h, CostModel(commission, slippage_ticks))
    out = [
        GridCellOut(
            side=c.side.value,
            threshold=c.threshold,
            horizon=c.horizon_days,
            n_trades=c.summary.n_trades,
            win_rate=c.summary.win_rate,
            avg_return=c.summary.avg_return,
            sharpe=c.summary.sharpe_annualized,
            mdd_usd=c.summary.max_drawdown_usd,
            gross_profit_usd=c.summary.gross_profit_usd,
            gross_loss_usd=c.summary.gross_loss_usd,
            net_pnl_usd=c.summary.total_net_pnl_usd,
            profit_factor=_pf(c.summary.profit_factor),
            low_sample=c.summary.low_sample,
        )
        for c in cells
    ]
    # 중간 cells (BacktestResult 다수 포함) 즉시 해제 → 메모리 회수
    del cells
    gc.collect()

    # 캐시 크기 제한 — 4가지 변형까지만 유지
    if len(_GRID_CACHE) >= 4:
        _GRID_CACHE.pop(next(iter(_GRID_CACHE)))
    _GRID_CACHE[key] = out
    return out


@router.get("/signals", response_model=list[SignalEvent])
def signals(
    type: Literal["short", "long"],
    threshold: float,
    since: Optional[str] = None,
):
    df = _df()
    short_th = [threshold] if type == "short" else []
    long_th = [threshold] if type == "long" else []
    sigs = generate_signals(df, short_thresholds=short_th, long_thresholds=long_th)
    if since:
        cut = pd.Timestamp(since)
        sigs = [s for s in sigs if s.date >= cut]
    return [
        SignalEvent(
            date=str(s.date.date()),
            side=s.side.value,
            threshold=s.threshold,
            entry_ref_close=s.entry_ref_close,
        )
        for s in sigs
    ]


class BacktestRequest(BaseModel):
    side: Literal["short", "long"]
    threshold: float
    horizon_days: int = Field(..., ge=1, le=500)
    commission: float = 2.5
    slippage_ticks: int = 1
    # 🅒 SL/TP 시뮬레이터 — None이면 기존 horizon 고정 보유
    stop_loss_pct: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    take_profit_pct: Optional[float] = Field(default=None, ge=0.0, le=2.0)
    # 선물 만기 롤오버 비용 (%/회, 추정 가정) — 0이면 미적용.
    # 양수=contango 비용, 음수=backwardation 이익 (현재 WTI는 backwardation).
    roll_cost_pct: float = Field(default=0.0, ge=-0.1, le=0.1)


@router.post("/backtest", response_model=BacktestResponse)
def backtest(req: BacktestRequest):
    from quant_core.oil_futures import RollModel
    df = _df()
    short_th = [req.threshold] if req.side == "short" else []
    long_th = [req.threshold] if req.side == "long" else []
    sigs = generate_signals(df, short_thresholds=short_th, long_thresholds=long_th)
    if not sigs:
        raise HTTPException(404, "신호가 발생하지 않음 — 임계값/타입 확인")
    res = run_backtest(
        df, sigs, req.horizon_days,
        CostModel(req.commission, req.slippage_ticks),
        ExitRules(req.stop_loss_pct, req.take_profit_pct),
        RollModel(roll_cost_pct=req.roll_cost_pct),
    )
    s = summarize(res)
    return BacktestResponse(
        summary=SummaryOut(
            n_trades=s.n_trades,
            win_rate=s.win_rate,
            avg_return=s.avg_return,
            avg_win=s.avg_win,
            avg_loss=s.avg_loss,
            profit_factor=_pf(s.profit_factor),
            sharpe=s.sharpe_annualized,
            mdd_usd=s.max_drawdown_usd,
            gross_profit_usd=s.gross_profit_usd,
            gross_loss_usd=s.gross_loss_usd,
            net_pnl_usd=s.total_net_pnl_usd,
            worst_mae_usd=s.worst_mae_usd,
            avg_mae_usd=s.avg_mae_usd,
            avg_mfe_usd=s.avg_mfe_usd,
            max_win_streak=s.max_win_streak,
            max_loss_streak=s.max_loss_streak,
            total_rollovers=s.total_rollovers,
            total_roll_cost_usd=s.total_roll_cost_usd,
            low_sample=s.low_sample,
        ),
        trades=[
            TradeOut(
                signal_date=str(t.signal.date.date()),
                side=t.signal.side.value,
                threshold=t.signal.threshold,
                entry_date=str(t.entry_date.date()),
                entry_price=t.entry_price,
                exit_date=str(t.exit_date.date()),
                exit_price=t.exit_price,
                horizon_days=t.horizon_days,
                return_pct=t.return_pct,
                net_pnl_usd=t.net_pnl_usd,
                mae_usd=t.mae_usd,
                mfe_usd=t.mfe_usd,
                exit_reason=t.exit_reason,
                num_rollovers=t.num_rollovers,
                roll_cost_usd=t.roll_cost_usd,
            )
            for t in res.trades
        ],
        equity_curve=[
            EquityPoint(date=str(idx.date()), cumulative_usd=float(val))
            for idx, val in res.equity_curve.items()
        ],
        portfolio_equity_curve=[
            EquityPoint(date=str(idx.date()), cumulative_usd=float(val))
            for idx, val in res.portfolio_equity_curve.items()
        ],
        portfolio_mdd_usd=res.portfolio_mdd_usd,
    )


class WalkForwardRequest(BaseModel):
    shorts: list[float] = DEFAULT_SHORTS
    longs: list[float] = DEFAULT_LONGS
    horizons: list[int] = DEFAULT_HORIZONS
    split_date: str
    commission: float = 2.5
    slippage_ticks: int = 1


@router.post("/walkforward", response_model=WalkForwardResponse)
def walkforward_endpoint(req: WalkForwardRequest):
    df = _df()
    try:
        res = walk_forward(
            df,
            req.shorts,
            req.longs,
            req.horizons,
            pd.Timestamp(req.split_date),
            CostModel(req.commission, req.slippage_ticks),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    b = res.best_in_sample
    bs = b.summary
    oos = res.best_out_of_sample
    return WalkForwardResponse(
        train_start=str(res.train_period[0].date()),
        train_end=str(res.train_period[1].date()),
        test_start=str(res.test_period[0].date()),
        test_end=str(res.test_period[1].date()),
        best_in_sample=BestCellOut(
            side=b.side.value,
            threshold=b.threshold,
            horizon=b.horizon_days,
            summary=SummaryOut(
                n_trades=bs.n_trades,
                win_rate=bs.win_rate,
                avg_return=bs.avg_return,
                avg_win=bs.avg_win,
                avg_loss=bs.avg_loss,
                profit_factor=_pf(bs.profit_factor),
                sharpe=bs.sharpe_annualized,
                mdd_usd=bs.max_drawdown_usd,
                gross_profit_usd=bs.gross_profit_usd,
                gross_loss_usd=bs.gross_loss_usd,
                net_pnl_usd=bs.total_net_pnl_usd,
                worst_mae_usd=bs.worst_mae_usd,
                avg_mae_usd=bs.avg_mae_usd,
                avg_mfe_usd=bs.avg_mfe_usd,
                max_win_streak=bs.max_win_streak,
                max_loss_streak=bs.max_loss_streak,
                total_rollovers=bs.total_rollovers,
                total_roll_cost_usd=bs.total_roll_cost_usd,
                low_sample=bs.low_sample,
            ),
        ),
        out_of_sample=SummaryOut(
            n_trades=oos.n_trades,
            win_rate=oos.win_rate,
            avg_return=oos.avg_return,
            avg_win=oos.avg_win,
            avg_loss=oos.avg_loss,
            profit_factor=_pf(oos.profit_factor),
            sharpe=oos.sharpe_annualized,
            mdd_usd=oos.max_drawdown_usd,
            gross_profit_usd=oos.gross_profit_usd,
            gross_loss_usd=oos.gross_loss_usd,
            net_pnl_usd=oos.total_net_pnl_usd,
            worst_mae_usd=oos.worst_mae_usd,
            avg_mae_usd=oos.avg_mae_usd,
            avg_mfe_usd=oos.avg_mfe_usd,
            max_win_streak=oos.max_win_streak,
            max_loss_streak=oos.max_loss_streak,
            total_rollovers=oos.total_rollovers,
            total_roll_cost_usd=oos.total_roll_cost_usd,
            low_sample=oos.low_sample,
        ),
    )


# ───── 🅒 Seasonality 엔드포인트 ─────────────────────────────────────

class SeasonalityCell(BaseModel):
    key: int           # month(1~12) or weekday(0~4)
    name: str          # "1월", "월" 같이 사람이 보는 라벨
    n_days: int
    avg_return: float  # 일별 수익률 평균 (close-to-close 일간)
    win_rate: float    # 양수 수익 비율


class SeasonalityResponse(BaseModel):
    monthly: list[SeasonalityCell]
    weekday: list[SeasonalityCell]


@router.get("/seasonality", response_model=SeasonalityResponse)
def seasonality():
    """일간 close-to-close 수익률을 월별 / 요일별로 집계 (신호 무관 구조 패턴).

    "10월에 평균 음수 수익" / "월요일이 약함" 같은 시장 구조 발견용.
    """
    df = _df().copy()
    df["ret"] = df["close"].pct_change()
    df = df.dropna(subset=["ret"])
    df["month"] = df["date"].dt.month
    df["weekday"] = df["date"].dt.weekday   # Mon=0, Sun=6

    KO_MONTHS = [f"{m}월" for m in range(1, 13)]
    KO_WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

    monthly = []
    for m in range(1, 13):
        s = df[df["month"] == m]["ret"]
        if len(s) == 0:
            continue
        monthly.append(SeasonalityCell(
            key=m, name=KO_MONTHS[m - 1],
            n_days=len(s),
            avg_return=float(s.mean()),
            win_rate=float((s > 0).mean()),
        ))

    weekday = []
    for w in range(0, 5):  # 평일만 (영업일이라 토/일 거의 없음)
        s = df[df["weekday"] == w]["ret"]
        if len(s) == 0:
            continue
        weekday.append(SeasonalityCell(
            key=w, name=KO_WEEKDAYS[w],
            n_days=len(s),
            avg_return=float(s.mean()),
            win_rate=float((s > 0).mean()),
        ))

    return SeasonalityResponse(monthly=monthly, weekday=weekday)


# ───── 🅔 Macro context (VIX·DXY 외생 변수) ─────────────────────────

from pathlib import Path  # noqa: E402

_MACRO_CSV = Path(__file__).resolve().parents[3] / "core" / "data" / "macro_daily.csv"


@lru_cache(maxsize=1)
def _macro_df() -> Optional[pd.DataFrame]:
    if not _MACRO_CSV.exists():
        return None
    m = pd.read_csv(_MACRO_CSV, parse_dates=["date"])
    return m


class MacroRegimeCell(BaseModel):
    bucket: str           # 예: "VIX < 15", "VIX >= 25"
    n_days: int
    wti_avg_return: float
    wti_win_rate: float


class MacroCorrelation(BaseModel):
    pair: str             # 예: "WTI vs VIX"
    pearson: float        # -1 ~ +1


class MacroResponse(BaseModel):
    available: bool
    coverage_days: int
    correlations: list[MacroCorrelation]
    vix_regime: list[MacroRegimeCell]    # 3 buckets
    dxy_regime: list[MacroRegimeCell]    # 3 buckets


@router.get("/macro-context", response_model=MacroResponse)
def macro_context():
    """WTI 일간 수익률과 VIX·DXY 관계 — 외생 변수 신호 가치 측정.

    - 상관관계: WTI vs VIX (음의 상관 예상 — risk-off에 유가 약세),
              WTI vs DXY (음의 상관 — 강달러에 commodity 약세)
    - 체제 분할: VIX 저/중/고 / DXY 저/중/고 구간별 WTI 평균수익·승률
    """
    wti = _df().copy()
    macro = _macro_df()
    if macro is None or macro.empty:
        return MacroResponse(
            available=False, coverage_days=0,
            correlations=[], vix_regime=[], dxy_regime=[],
        )

    wti["ret"] = wti["close"].pct_change()
    merged = wti.merge(macro, on="date", how="inner").dropna(
        subset=["ret", "vix_close", "dxy_close"]
    )
    if len(merged) == 0:
        return MacroResponse(
            available=False, coverage_days=0,
            correlations=[], vix_regime=[], dxy_regime=[],
        )

    # 1) 상관관계
    correlations = [
        MacroCorrelation(pair="WTI vs VIX", pearson=float(merged["ret"].corr(merged["vix_close"].pct_change()))),
        MacroCorrelation(pair="WTI vs DXY", pearson=float(merged["ret"].corr(merged["dxy_close"].pct_change()))),
        MacroCorrelation(pair="WTI vs VIX(level)", pearson=float(merged["ret"].corr(merged["vix_close"]))),
        MacroCorrelation(pair="WTI vs DXY(level)", pearson=float(merged["ret"].corr(merged["dxy_close"]))),
    ]

    # 2) VIX 체제 (3분위: 저/중/고)
    vix_q33 = merged["vix_close"].quantile(0.33)
    vix_q66 = merged["vix_close"].quantile(0.66)
    vix_buckets = [
        (f"VIX < {vix_q33:.1f} (저변동)", merged[merged["vix_close"] < vix_q33]),
        (f"{vix_q33:.1f} ≤ VIX < {vix_q66:.1f} (중변동)",
         merged[(merged["vix_close"] >= vix_q33) & (merged["vix_close"] < vix_q66)]),
        (f"VIX ≥ {vix_q66:.1f} (고변동)", merged[merged["vix_close"] >= vix_q66]),
    ]
    vix_regime = [
        MacroRegimeCell(
            bucket=name, n_days=len(df_b),
            wti_avg_return=float(df_b["ret"].mean()) if len(df_b) else 0.0,
            wti_win_rate=float((df_b["ret"] > 0).mean()) if len(df_b) else 0.0,
        )
        for name, df_b in vix_buckets
    ]

    # 3) DXY 체제 (강·중·약달러 — 같은 3분위)
    dxy_q33 = merged["dxy_close"].quantile(0.33)
    dxy_q66 = merged["dxy_close"].quantile(0.66)
    dxy_buckets = [
        (f"DXY < {dxy_q33:.1f} (약달러)", merged[merged["dxy_close"] < dxy_q33]),
        (f"{dxy_q33:.1f} ≤ DXY < {dxy_q66:.1f} (중간)",
         merged[(merged["dxy_close"] >= dxy_q33) & (merged["dxy_close"] < dxy_q66)]),
        (f"DXY ≥ {dxy_q66:.1f} (강달러)", merged[merged["dxy_close"] >= dxy_q66]),
    ]
    dxy_regime = [
        MacroRegimeCell(
            bucket=name, n_days=len(df_b),
            wti_avg_return=float(df_b["ret"].mean()) if len(df_b) else 0.0,
            wti_win_rate=float((df_b["ret"] > 0).mean()) if len(df_b) else 0.0,
        )
        for name, df_b in dxy_buckets
    ]

    return MacroResponse(
        available=True,
        coverage_days=len(merged),
        correlations=correlations,
        vix_regime=vix_regime,
        dxy_regime=dxy_regime,
    )

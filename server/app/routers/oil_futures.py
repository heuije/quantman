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

import math
from functools import lru_cache
from typing import Literal, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quant_core.oil_futures import (
    CostModel,
    Side,
    generate_signals,
    grid_search,
    load_wti,
    run_backtest,
    summarize,
    walk_forward,
)

router = APIRouter(prefix="/oil-futures", tags=["oil-futures"])

# 엑셀과 동일한 기본 임계값/horizon (UI 디폴트와도 일치)
DEFAULT_SHORTS = [80, 90, 100, 110, 120, 130, 140, 150]
DEFAULT_LONGS = [10, 20, 30, 40, 50, 60]
DEFAULT_HORIZONS = [20, 40, 60, 120]


# DataFrame 로드는 첫 호출에만 (~5MB CSV, 메모리 상주) — 매 요청 디스크 IO 회피.
@lru_cache(maxsize=1)
def _df() -> pd.DataFrame:
    return load_wti()


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
    net_pnl_usd: float
    profit_factor: Optional[float]   # None = 손실 없음(∞ 대체)
    low_sample: bool


class SignalEvent(BaseModel):
    date: str
    side: str
    threshold: float
    entry_ref_close: float


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
    net_pnl_usd: float
    low_sample: bool


class BacktestResponse(BaseModel):
    summary: SummaryOut
    trades: list[TradeOut]
    equity_curve: list[EquityPoint]


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
    """
    df = _df()
    s = _parse_csv_floats(shorts) or DEFAULT_SHORTS
    l = _parse_csv_floats(longs) or DEFAULT_LONGS
    h = _parse_csv_ints(horizons) or DEFAULT_HORIZONS
    cells = grid_search(df, s, l, h, CostModel(commission, slippage_ticks))
    return [
        GridCellOut(
            side=c.side.value,
            threshold=c.threshold,
            horizon=c.horizon_days,
            n_trades=c.summary.n_trades,
            win_rate=c.summary.win_rate,
            avg_return=c.summary.avg_return,
            sharpe=c.summary.sharpe_annualized,
            mdd_usd=c.summary.max_drawdown_usd,
            net_pnl_usd=c.summary.total_net_pnl_usd,
            profit_factor=_pf(c.summary.profit_factor),
            low_sample=c.summary.low_sample,
        )
        for c in cells
    ]


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


@router.post("/backtest", response_model=BacktestResponse)
def backtest(req: BacktestRequest):
    df = _df()
    short_th = [req.threshold] if req.side == "short" else []
    long_th = [req.threshold] if req.side == "long" else []
    sigs = generate_signals(df, short_thresholds=short_th, long_thresholds=long_th)
    if not sigs:
        raise HTTPException(404, "신호가 발생하지 않음 — 임계값/타입 확인")
    res = run_backtest(
        df, sigs, req.horizon_days, CostModel(req.commission, req.slippage_ticks)
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
            net_pnl_usd=s.total_net_pnl_usd,
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
            )
            for t in res.trades
        ],
        equity_curve=[
            EquityPoint(date=str(idx.date()), cumulative_usd=float(val))
            for idx, val in res.equity_curve.items()
        ],
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
                net_pnl_usd=bs.net_pnl_usd if hasattr(bs, "net_pnl_usd") else bs.total_net_pnl_usd,
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
            net_pnl_usd=oos.total_net_pnl_usd,
            low_sample=oos.low_sample,
        ),
    )

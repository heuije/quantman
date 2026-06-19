"""선물 분석 API (Phase 2 — 대시보드 백엔드).

quant_core.oil_futures 엔진을 종목별(symbol)로 HTTP에 노출. 인증 필요 —
로그인 게이트(라우터 전역 Depends(get_current_user)). 종목 차이는
futures_config.INSTRUMENTS에 모은다(데이터키·계약사양·임계범위·라벨).

엔드포인트:
- GET  /futures/instruments              선택기용 종목 목록
- GET  /futures/{symbol}/data-info       데이터 메타 (기간/행수/가격범위/라벨)
- GET  /futures/{symbol}/latest-price    최신가 (캐시 일봉 마지막 종가)
- GET  /futures/{symbol}/prices          일봉 시계열 (차트용, 기간 필터 옵션)
- GET  /futures/{symbol}/grid            전체 grid (히트맵·표용)
- GET  /futures/{symbol}/signals         특정 (type, threshold) 신호 목록
- GET  /futures/{symbol}/seasonality     월·요일 계절성
- GET  /futures/{symbol}/macro-context   VIX·DXY 외생 변수 레짐·상관
- POST /futures/{symbol}/backtest        단일 조합 백테스트 (trades + equity)
- POST /futures/{symbol}/walkforward     train/test 분할 검증
"""

from __future__ import annotations

import gc
import logging
import math
import threading
import time
from pathlib import Path
from typing import Literal, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from quant_core.oil_futures import (
    LOW_SAMPLE_THRESHOLD,
    CostModel,
    ExitRules,
    RollModel,
    Side,
    build_oil_excel,
    build_oil_trend_excel,
    generate_signals,
    grid_search,
    prepare_wti,
    run_backtest,
    summarize,
    trend_events,
    trend_explanatory_scan,
    trend_matched,
    trend_regression,
    walk_forward,
)

from ..data_cache import get_raw_dataset, get_version
from ..deps import get_current_user
from ..futures_config import INSTRUMENTS, InstrumentConfig

_log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/futures",
    tags=["futures"],
    dependencies=[Depends(get_current_user)],
)


def _get_cfg(symbol: str) -> InstrumentConfig:
    cfg = INSTRUMENTS.get(symbol)
    if cfg is None:
        raise HTTPException(404, f"지원하지 않는 종목: {symbol}")
    return cfg


DEFAULT_HORIZONS = [20, 40, 60, 120, 180, 240, 365]


# 종목별 정제 df 캐시 — (symbol → {version, df}). 데이터 버전 변경 시 자동 갱신.
_DF_CACHE: dict[str, dict] = {}

# server/app 디렉터리 — 정적 스냅샷 CSV 경로 해석 기준(배포에 확실히 포함되는 위치).
_APP_DIR = Path(__file__).resolve().parents[1]


def _load_static_csv(rel_path: str) -> pd.DataFrame:
    """번들된 정적 스냅샷 CSV(date,open,high,low,close[,volume])를 읽어 prepare_wti 입력으로 반환.
    파일이 없으면 503 (빈 프레임을 캐시로 굳히지 않음)."""
    path = _APP_DIR / rel_path
    if not path.exists():
        raise HTTPException(status_code=503, detail=f"정적 데이터 파일 없음: {rel_path}")
    return pd.read_csv(path)


def _df(symbol: str) -> pd.DataFrame:
    """종목의 캐시 시리즈를 정제해 반환. 데이터 버전 변경 시 자동 갱신.
    미수집/전구간 탈락이면 503 (version 미갱신 — 빈 프레임을 캐시로 굳히지 않음)."""
    cfg = _get_cfg(symbol)
    v = get_version()
    slot = _DF_CACHE.get(symbol)
    if slot is None or slot.get("version") != v:
        if cfg.static_path:        # 정적 스냅샷 종목(KOSPI) — 라이브 캐시 대신 번들 CSV
            raw = _load_static_csv(cfg.static_path)
        else:
            ds = get_raw_dataset()
            raw = ds.get(cfg.data_key)
        df = prepare_wti(raw) if raw is not None and not raw.empty else None
        if df is None or df.empty:
            raise HTTPException(status_code=503,
                                detail=f"{cfg.name} 데이터 미수집 — 데이터 수집 후 이용 가능")
        _DF_CACHE[symbol] = {"version": v, "df": df}
    return _DF_CACHE[symbol]["df"]


# ───── 최신가 (캐시 일봉 마지막 종가) ───────────────────────────────────

class LatestPrice(BaseModel):
    price: float
    change: Optional[float]          # 전일 대비 절대값
    change_pct: Optional[float]      # 전일 대비 % (소수, 예: -0.0173)
    source: str                      # "yahoo-cl=f"
    delayed: bool                    # 실시간(False) vs 지연(True)
    fetched_at: str                  # ISO 시각 (UTC)


class InstrumentInfo(BaseModel):
    symbol: str
    name: str


@router.get("/instruments", response_model=list[InstrumentInfo])
def instruments():
    """선택기용 종목 목록."""
    return [InstrumentInfo(symbol=c.symbol, name=c.name) for c in INSTRUMENTS.values()]


@router.get("/{symbol}/latest-price", response_model=LatestPrice)
def latest_price(symbol: str):
    cfg = _get_cfg(symbol)
    df = _df(symbol)
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    return LatestPrice(
        price=float(last["close"]),
        change=float(last["close"] - prev["close"]),
        change_pct=float(last["close"] / prev["close"] - 1) if prev["close"] else None,
        source=cfg.source, delayed=True,
        fetched_at=pd.Timestamp.utcnow().isoformat(),
    )


# ─── Response models ────────────────────────────────────────────────────

class DataInfo(BaseModel):
    n_rows: int
    start_date: str
    end_date: str
    price_min: float
    price_max: float
    name: str
    eyebrow: str
    unit: str
    roll_note: str
    currency: str        # 손익 표시 통화 ("USD"|"KRW") — 웹이 금액 라벨에 사용


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

@router.get("/{symbol}/data-info", response_model=DataInfo)
def data_info(symbol: str):
    cfg = _get_cfg(symbol)
    df = _df(symbol)
    return DataInfo(
        n_rows=len(df),
        start_date=str(df["date"].iloc[0].date()),
        end_date=str(df["date"].iloc[-1].date()),
        price_min=float(df["close"].min()),
        price_max=float(df["close"].max()),
        name=cfg.name, eyebrow=cfg.eyebrow, unit=cfg.unit, roll_note=cfg.roll_note,
        currency=cfg.currency,
    )


@router.get("/{symbol}/prices", response_model=list[PricePoint])
def prices(symbol: str, start: Optional[str] = None, end: Optional[str] = None):
    df = _df(symbol)
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


_GRID_CACHE: dict[tuple, list[GridCellOut]] = {}   # 키: (symbol, version, s, l, h, comm, slip)
_grid_lock = threading.Lock()


def _ensure_grid_cached(symbol: str, s: tuple, l: tuple, h: tuple,
                        commission: float, slippage_ticks: int,
                        smooth_window: int = 1, min_gap_days: int = 0,
                        roll_cost_pct: float = 0.0) -> list[GridCellOut]:
    """(symbol, 데이터버전, 파라미터) 결과 캐시. 워머·요청 공용. 미수집이면 _df가 503."""
    cfg = _get_cfg(symbol)
    df = _df(symbol)
    version = get_version()
    key = (symbol, version, s, l, h, commission, slippage_ticks,
           smooth_window, min_gap_days, roll_cost_pct)
    cached = _GRID_CACHE.get(key)
    if cached is not None:
        return cached
    with _grid_lock:
        cached = _GRID_CACHE.get(key)
        if cached is not None:
            return cached
        cells = grid_search(df, s, l, h, CostModel(commission, slippage_ticks),
                            light=True, spec=cfg.spec,
                            smooth_window=smooth_window, min_gap_days=min_gap_days,
                            roll_cost_pct=roll_cost_pct)
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
        del cells
        gc.collect()
        if len(_GRID_CACHE) >= len(INSTRUMENTS) * 2:   # 종목당 2변형(기본+커스텀) 여유
            _GRID_CACHE.pop(next(iter(_GRID_CACHE)))
        _GRID_CACHE[key] = out
        return out


@router.get("/{symbol}/grid", response_model=list[GridCellOut])
def grid(symbol: str, shorts: str = "", longs: str = "", horizons: str = "",
         commission: float = 2.5, slippage_ticks: int = 1,
         smooth_window: int = 1, min_gap_days: int = 0, roll_cost_pct: float = 0.0):
    """(side, threshold, horizon) 조합 백테스트. 미지정 시 종목 기본 grid(캐시·워머).

    smooth_window·min_gap_days: 신호 품질 옵션. roll_cost_pct: 만기 롤오버 비용(%/롤). 기본값=현행.
    """
    cfg = _get_cfg(symbol)
    s = tuple(_parse_csv_floats(shorts) or cfg.shorts.values())
    l = tuple(_parse_csv_floats(longs) or cfg.longs.values())
    h = tuple(_parse_csv_ints(horizons) or DEFAULT_HORIZONS)
    return _ensure_grid_cached(symbol, s, l, h, commission, slippage_ticks,
                               smooth_window, min_gap_days, roll_cost_pct)


def _warmer_loop() -> None:
    """전 종목 기본 grid를 백그라운드로 미리 계산·캐시. 데이터 버전 변경 시 재워밍.
    한 종목이라도 미준비(503)면 자주 재시도(20s), 전 종목 워밍되면 느슨히 재확인(300s)."""
    while True:
        pending = False
        for cfg in INSTRUMENTS.values():
            try:
                _ensure_grid_cached(cfg.symbol, tuple(cfg.shorts.values()),
                                    tuple(cfg.longs.values()), tuple(DEFAULT_HORIZONS), 2.5, 1, 1, 0)
            except HTTPException:
                pending = True   # 이 종목 데이터 미수집 — 다음 틱 재시도
            except Exception:
                _log.warning("선물 grid 워밍 실패 [%s] — 온디맨드로 계산됨",
                             cfg.symbol, exc_info=True)
        time.sleep(20 if pending else 300)


def start_grid_warmer() -> None:
    threading.Thread(target=_warmer_loop, daemon=True, name="futures-grid-warm").start()


@router.get("/{symbol}/signals", response_model=list[SignalEvent])
def signals(
    symbol: str,
    type: Literal["short", "long"],
    threshold: float,
    since: Optional[str] = None,
    smooth_window: int = 1,
    min_gap_days: int = 0,
):
    df = _df(symbol)
    short_th = [threshold] if type == "short" else []
    long_th = [threshold] if type == "long" else []
    sigs = generate_signals(df, short_thresholds=short_th, long_thresholds=long_th,
                            smooth_window=smooth_window, min_gap_days=min_gap_days)
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
    # 신호 품질 옵션 (N일 평균 임계·최소 신호 간격). 기본값=현행.
    smooth_window: int = Field(default=1, ge=1, le=120)
    min_gap_days: int = Field(default=0, ge=0, le=250)


@router.post("/{symbol}/backtest", response_model=BacktestResponse)
def backtest(symbol: str, req: BacktestRequest):
    cfg = _get_cfg(symbol)
    df = _df(symbol)
    short_th = [req.threshold] if req.side == "short" else []
    long_th = [req.threshold] if req.side == "long" else []
    sigs = generate_signals(df, short_thresholds=short_th, long_thresholds=long_th,
                            smooth_window=req.smooth_window, min_gap_days=req.min_gap_days)
    if not sigs:
        raise HTTPException(404, "신호가 발생하지 않음 — 임계값/타입 확인")
    res = run_backtest(
        df, sigs, req.horizon_days,
        CostModel(req.commission, req.slippage_ticks),
        ExitRules(req.stop_loss_pct, req.take_profit_pct),
        RollModel(roll_cost_pct=req.roll_cost_pct),
        spec=cfg.spec,
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


_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.post("/{symbol}/export.xlsx")
def export_excel(symbol: str, req: BacktestRequest):
    """현재 종목·파라미터의 백테스트를 라이브 수식 엑셀(.xlsx)로 반환.

    /backtest 와 동일 입력(BacktestRequest). 빌더가 종목 ContractSpec(cfg.spec)으로
    손익을 계산해 앱 백테스트 숫자와 일치한다. SL/TP는 엑셀 단순화상 미반영(보유기간
    고정) — 빌더 로직설명 시트에 명시.
    """
    cfg = _get_cfg(symbol)
    df = _df(symbol)
    data = build_oil_excel(
        df,
        side=req.side,
        threshold=req.threshold,
        horizon_days=req.horizon_days,
        spec=cfg.spec,
        commission_per_contract=req.commission,
        slippage_ticks=req.slippage_ticks,
        roll_cost_pct=req.roll_cost_pct,
        min_gap_days=req.min_gap_days,
        name=cfg.name,
        currency=cfg.currency,
        unit=cfg.unit,
    )
    return Response(
        content=data,
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="futures_{cfg.symbol}.xlsx"'},
    )


class WalkForwardRequest(BaseModel):
    shorts: Optional[list[float]] = None
    longs: Optional[list[float]] = None
    horizons: list[int] = DEFAULT_HORIZONS
    split_date: str
    commission: float = 2.5
    slippage_ticks: int = 1
    smooth_window: int = Field(default=1, ge=1, le=120)
    min_gap_days: int = Field(default=0, ge=0, le=250)


@router.post("/{symbol}/walkforward", response_model=WalkForwardResponse)
def walkforward_endpoint(symbol: str, req: WalkForwardRequest):
    cfg = _get_cfg(symbol)
    df = _df(symbol)
    shorts = req.shorts if req.shorts is not None else cfg.shorts.values()
    longs = req.longs if req.longs is not None else cfg.longs.values()
    try:
        res = walk_forward(
            df, shorts, longs, req.horizons,
            pd.Timestamp(req.split_date),
            CostModel(req.commission, req.slippage_ticks),
            spec=cfg.spec,
            smooth_window=req.smooth_window, min_gap_days=req.min_gap_days,
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


@router.get("/{symbol}/seasonality", response_model=SeasonalityResponse)
def seasonality(symbol: str):
    """일간 close-to-close 수익률을 월별 / 요일별로 집계 (신호 무관 구조 패턴).

    "10월에 평균 음수 수익" / "월요일이 약함" 같은 시장 구조 발견용.
    """
    df = _df(symbol).copy()
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

def _macro_df() -> Optional[pd.DataFrame]:
    """캐시된 VIX·달러지수 종가로 date·vix_close·dxy_close 프레임 생성."""
    ds = get_raw_dataset()
    vix = ds.get("VIX")
    dxy = ds.get("달러지수")
    if vix is None or vix.empty or dxy is None or dxy.empty:
        return None
    v = vix["Close"].rename("vix_close")
    d = dxy["Close"].rename("dxy_close")
    m = pd.concat([v, d], axis=1).reset_index()
    m = m.rename(columns={m.columns[0]: "date"})
    m["date"] = pd.to_datetime(m["date"]).dt.tz_localize(None).dt.normalize()
    return m.dropna(subset=["vix_close", "dxy_close"])


class MacroRegimeCell(BaseModel):
    bucket: str           # 예: "VIX < 15", "VIX >= 25"
    n_days: int
    avg_return: float
    win_rate: float


class MacroCorrelation(BaseModel):
    pair: str             # 예: "원유 (WTI) vs VIX"
    pearson: float        # -1 ~ +1


class MacroResponse(BaseModel):
    available: bool
    coverage_days: int
    correlations: list[MacroCorrelation]
    vix_regime: list[MacroRegimeCell]    # 3 buckets
    dxy_regime: list[MacroRegimeCell]    # 3 buckets


@router.get("/{symbol}/macro-context", response_model=MacroResponse)
def macro_context(symbol: str):
    """종목 일간 수익률과 VIX·DXY 관계 — 외생 변수 신호 가치 측정.

    - 상관관계: 자산 vs VIX (음의 상관 예상 — risk-off에 위험자산 약세),
              자산 vs DXY (음의 상관 — 강달러에 commodity 약세)
    - 체제 분할: VIX 저/중/고 / DXY 저/중/고 구간별 자산 평균수익·승률
    """
    cfg = _get_cfg(symbol)
    asset = _df(symbol).copy()
    macro = _macro_df()
    if macro is None or macro.empty:
        return MacroResponse(
            available=False, coverage_days=0,
            correlations=[], vix_regime=[], dxy_regime=[],
        )

    asset["ret"] = asset["close"].pct_change()
    merged = asset.merge(macro, on="date", how="inner").dropna(
        subset=["ret", "vix_close", "dxy_close"]
    )
    if len(merged) == 0:
        return MacroResponse(
            available=False, coverage_days=0,
            correlations=[], vix_regime=[], dxy_regime=[],
        )

    # 1) 상관관계
    correlations = [
        MacroCorrelation(pair=f"{cfg.name} vs VIX", pearson=float(merged["ret"].corr(merged["vix_close"].pct_change()))),
        MacroCorrelation(pair=f"{cfg.name} vs DXY", pearson=float(merged["ret"].corr(merged["dxy_close"].pct_change()))),
        MacroCorrelation(pair=f"{cfg.name} vs VIX(level)", pearson=float(merged["ret"].corr(merged["vix_close"]))),
        MacroCorrelation(pair=f"{cfg.name} vs DXY(level)", pearson=float(merged["ret"].corr(merged["dxy_close"]))),
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
            avg_return=float(df_b["ret"].mean()) if len(df_b) else 0.0,
            win_rate=float((df_b["ret"] > 0).mean()) if len(df_b) else 0.0,
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
            avg_return=float(df_b["ret"].mean()) if len(df_b) else 0.0,
            win_rate=float((df_b["ret"] > 0).mean()) if len(df_b) else 0.0,
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


# ───── 🅕 Trend → Forward (진입 추세 → 미래 수익률) ──────────────────

class TrendEventOut(BaseModel):
    date: str
    close: float
    past_return: float       # close[t] / close[t-lookback] - 1
    forward_return: float    # close[t+horizon] / close[t] - 1


class TrendRegressionOut(BaseModel):
    slope: float             # forward~past β
    intercept: float
    r_squared: float
    n: int
    hac_se: float            # Newey-West(HAC) slope 표준오차 (겹침 보정)
    hac_t_stat: float
    hac_p_value: float       # 정규근사 양측 p


class TrendEventsResponse(BaseModel):
    lookback: int
    horizon: int
    mode: str                       # "all"(전체 영업일) | "signal"(신호 앵커)
    side: Optional[str]
    threshold: Optional[float]
    events: list[TrendEventOut]
    regression: Optional[TrendRegressionOut]
    low_sample: bool                # n < 30


@router.get("/{symbol}/trend-events", response_model=TrendEventsResponse)
def trend_events_endpoint(
    symbol: str,
    lookback: int = 20,
    horizon: int = 60,
    side: Optional[Literal["short", "long"]] = None,
    threshold: Optional[float] = None,
    smooth_window: int = 1,
    min_gap_days: int = 0,
):
    """진입 직전 추세(과거 lookback일 증감율) ↔ 이후 horizon일 수익률 이벤트·회귀.

    side·threshold 동시 지정 = 신호 크로스일만(임계별), 둘 다 생략 = 전체 영업일(베이스라인).
    무거운 계산 없음(단일 패스) — 웹이 증감율 밴드를 실시간 필터/집계한다.
    """
    if lookback < 1 or not (1 <= horizon <= 500):
        raise HTTPException(422, "lookback≥1, 1≤horizon≤500 이어야 함")
    if (side is None) != (threshold is None):
        raise HTTPException(422, "side와 threshold는 함께 지정하거나 함께 생략")

    df = _df(symbol)
    side_enum = Side(side) if side else None
    evs = trend_events(df, lookback, horizon, side=side_enum, threshold=threshold,
                       smooth_window=smooth_window, min_gap_days=min_gap_days)
    reg = trend_regression(evs, horizon)
    return TrendEventsResponse(
        lookback=lookback, horizon=horizon,
        mode="signal" if side else "all",
        side=side, threshold=threshold,
        events=[
            TrendEventOut(
                date=str(e.date.date()), close=e.close,
                past_return=e.past_return, forward_return=e.forward_return,
            )
            for e in evs
        ],
        regression=(
            TrendRegressionOut(
                slope=reg.slope, intercept=reg.intercept, r_squared=reg.r_squared,
                n=reg.n, hac_se=reg.hac_se, hac_t_stat=reg.hac_t_stat,
                hac_p_value=reg.hac_p_value,
            ) if reg else None
        ),
        low_sample=len(evs) < LOW_SAMPLE_THRESHOLD,
    )


# ───── 🅖 Trend explanatory scan — (L,H) 설명력 격자 ──────────────────

_SCAN_LOOKBACKS = [5, 10, 20, 40, 60, 90, 120]
_SCAN_HORIZONS = [5, 10, 20, 40, 60, 90, 120]


class ScanCellOut(BaseModel):
    lookback: int
    horizon: int
    n: int
    r_squared: Optional[float]    # 회귀 불가(n<3) 시 None. min_n 미만이어도 그려짐(저신뢰)
    slope: Optional[float]
    intercept: Optional[float]
    hac_p_value: Optional[float]
    oos_r_squared: Optional[float]
    oos_slope: Optional[float]
    sign_stable: bool
    mean_forward: Optional[float]  # 평균 향후 H일 증감율 (%). n≥1이면 값(R²와 달리 n<3도)
    up_ratio: Optional[float]      # 향후 증감율>0 비율 (%)


class TrendScanResponse(BaseModel):
    price_lo: float
    price_hi: float
    lookbacks: list[int]
    horizons: list[int]
    cells: list[ScanCellOut]
    n_cells: int                  # 격자 칸 수 = 다중비교 규모(데이터 스누핑 경고용)
    oos_split: float
    min_n: int


def _parse_int_list(raw: Optional[str], default: list[int]) -> list[int]:
    if not raw:
        return list(default)
    out: list[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            v = int(tok)
        except ValueError:
            raise HTTPException(422, f"정수 목록 파싱 실패: {raw!r}")
        if v >= 1:
            out.append(v)
    if not out:
        raise HTTPException(422, "lookbacks/horizons 는 1 이상 정수 1개 이상")
    return sorted(set(out))


def _nn(v: Optional[float]) -> Optional[float]:
    """nan/None → None (JSON 호환)."""
    return None if (v is None or math.isnan(v)) else float(v)


@router.get("/{symbol}/trend-scan", response_model=TrendScanResponse)
def trend_scan_endpoint(
    symbol: str,
    price_lo: float,
    price_hi: float,
    lookbacks: Optional[str] = None,
    horizons: Optional[str] = None,
    oos_split: float = 0.5,
    min_n: int = 30,
    gap: int = 0,
):
    """종가범위 조건 하 (L,H) 격자별 forward~past 설명력(R²·HAC p·기울기) + OOS 안정성.

    설명력은 종가범위만 조건(증감율 밴드는 회귀에 미포함 — x축 절단 회피).
    ⚠ 여러 칸 중 R² 최댓값을 고르면 다중비교 과최적화 — n_cells 가 검정 규모이고,
    sign_stable·OOS·연속영역으로 견고성을 판단해야 한다(웹이 경고 노출).
    """
    lo, hi = (price_lo, price_hi) if price_lo <= price_hi else (price_hi, price_lo)
    ls = _parse_int_list(lookbacks, _SCAN_LOOKBACKS)
    hs = _parse_int_list(horizons, _SCAN_HORIZONS)
    if len(ls) * len(hs) > 144:
        raise HTTPException(422, "격자 칸 수는 144 이하 (lookbacks×horizons)")
    if not (0.2 <= oos_split <= 0.8):
        raise HTTPException(422, "oos_split 은 0.2~0.8")
    if min_n < 3:
        raise HTTPException(422, "min_n 은 3 이상")

    df = _df(symbol)
    # min_n은 더 이상 컴퓨트 게이트가 아니다 — 스캔은 회귀 가능한 모든 칸(n≥3)을 그리고,
    # min_n은 응답에 실어 클라이언트가 '저신뢰'(n<min_n) 음영·경고에만 쓴다(표시 임계).
    cells = trend_explanatory_scan(df, ls, hs, lo, hi, oos_split=oos_split, gap=max(0, gap))
    return TrendScanResponse(
        price_lo=lo, price_hi=hi, lookbacks=ls, horizons=hs,
        cells=[
            ScanCellOut(
                lookback=c.lookback, horizon=c.horizon, n=c.n,
                r_squared=_nn(c.r_squared), slope=_nn(c.slope),
                intercept=_nn(c.intercept), hac_p_value=_nn(c.hac_p_value),
                oos_r_squared=_nn(c.oos_r_squared), oos_slope=_nn(c.oos_slope),
                sign_stable=c.sign_stable,
                mean_forward=_nn(c.mean_forward), up_ratio=_nn(c.up_ratio),
            )
            for c in cells
        ],
        n_cells=len(cells),
        oos_split=oos_split, min_n=min_n,
    )


@router.get("/{symbol}/trend-export.xlsx")
def trend_export_endpoint(
    symbol: str,
    lookback: int,
    horizon: int,
    price_lo: float,
    price_hi: float,
    change_lo: float = -1e9,
    change_hi: float = 1e9,
    gap: int = 0,
):
    """추세 탐색기 '향후 종가 증감율'을 **라이브 수식 .xlsx** 로 반환.

    raw OHLCV + Excel 수식(종가/증감율 밴드 매칭·gap 디클러스터·요약·회귀)을 담아,
    노란 칸을 바꾸면 엑셀에서 재계산된다(화면과 동일 로직, trend_matched=정적 스냅샷).
    """
    if lookback < 1 or not (1 <= horizon <= 500):
        raise HTTPException(422, "lookback≥1, 1≤horizon≤500 이어야 함")
    cfg = _get_cfg(symbol)
    df = _df(symbol)
    lo, hi = (price_lo, price_hi) if price_lo <= price_hi else (price_hi, price_lo)
    clo, chi = (change_lo, change_hi) if change_lo <= change_hi else (change_hi, change_lo)
    events, raw_n = trend_matched(df, lookback, horizon, lo, hi, clo, chi, max(0, gap))
    data = build_oil_trend_excel(
        df, events, raw_n,
        lookback=lookback, horizon=horizon,
        price_lo=lo, price_hi=hi, change_lo=clo, change_hi=chi, gap=max(0, gap),
        name=cfg.name, price_sym=("" if cfg.currency == "KRW" else "$"),
    )
    return Response(
        content=data,
        media_type=_XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="trend_{cfg.symbol}.xlsx"'},
    )

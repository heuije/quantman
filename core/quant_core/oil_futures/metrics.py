"""백테스트 결과 → 요약 지표.

엑셀 원본 한계 #4, #9 보완:
- 단순 평균수익률·승률만 보는 게 아니라 위험지표(Sharpe, MDD, profit factor) 동시 산출.
- 샘플 수가 통계적 유의성 임계(LOW_SAMPLE_THRESHOLD) 미만이면 low_sample 플래그 → 의사결정자가
  "평균은 좋아 보이지만 거래 횟수가 너무 적음"을 즉시 인지하도록.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from .backtest import BacktestResult

# 통계적 유의성을 위한 최소 거래 수 (관행: ~30).
# 30 미만이면 평균/표준편차 추정이 매우 불안정 — 결과에 low_sample=True 표시.
LOW_SAMPLE_THRESHOLD = 30

# 연중 영업일 (백테스트 Sharpe 연환산 가정)
BUSINESS_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class Summary:
    """단일 (side, threshold, horizon) 조합의 백테스트 요약."""

    n_trades: int
    win_rate: float                  # 0~1
    avg_return: float                # 거래당 평균 수익률 (소수)
    avg_win: float                   # 이긴 거래 평균 수익률
    avg_loss: float                  # 진 거래 평균 수익률 (음수)
    profit_factor: float             # 총이익 / |총손실|, 손실 없으면 inf
    total_net_pnl_usd: float         # 1계약 기준 누적 net PnL
    gross_profit_usd: float          # 이긴 거래만 net PnL 합 (USD)
    gross_loss_usd: float            # 진 거래만 net PnL 합 (USD, 음수)
    sharpe_annualized: float         # 거래 수익률 기반 연환산 Sharpe (rf=0)
    max_drawdown_usd: float          # equity curve 최대낙폭 (음수)
    # 🅐 MAE/MFE 집계 (장중 평가손익 — horizon 청산 PnL과 별개)
    worst_mae_usd: float             # 모든 trade 중 최악 평가손실(가장 음수)
    avg_mae_usd: float               # 평균 MAE (음수 또는 0)
    avg_mfe_usd: float               # 평균 MFE (양수 또는 0)
    # 🅑 연속 streak
    max_win_streak: int              # 최장 연속 승
    max_loss_streak: int             # 최장 연속 패
    low_sample: bool                 # n_trades < LOW_SAMPLE_THRESHOLD


def summarize(result: BacktestResult) -> Summary:
    """BacktestResult → Summary."""
    trades = result.trades
    n = len(trades)
    if n == 0:
        return Summary(
            n_trades=0,
            win_rate=0.0,
            avg_return=0.0,
            avg_win=0.0,
            avg_loss=0.0,
            profit_factor=0.0,
            total_net_pnl_usd=0.0,
            gross_profit_usd=0.0,
            gross_loss_usd=0.0,
            sharpe_annualized=0.0,
            max_drawdown_usd=0.0,
            worst_mae_usd=0.0,
            avg_mae_usd=0.0,
            avg_mfe_usd=0.0,
            max_win_streak=0,
            max_loss_streak=0,
            low_sample=True,
        )

    rets = pd.Series([t.return_pct for t in trades])
    wins = rets[rets > 0]
    losses = rets[rets < 0]

    avg_win = float(wins.mean()) if len(wins) else 0.0
    avg_loss = float(losses.mean()) if len(losses) else 0.0

    sum_wins = float(wins.sum())
    sum_losses_abs = float(-losses.sum())
    if sum_losses_abs > 0:
        profit_factor = sum_wins / sum_losses_abs
    elif sum_wins > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    # 절대 USD 기준 gross profit / loss (PnL 컬럼 분해용)
    pnls = pd.Series([t.net_pnl_usd for t in trades])
    gross_profit_usd = float(pnls[pnls > 0].sum())
    gross_loss_usd = float(pnls[pnls < 0].sum())   # 음수

    # 🅐 MAE/MFE 집계 — 장중 평가손익 통계
    maes = [t.mae_usd for t in trades]
    mfes = [t.mfe_usd for t in trades]
    worst_mae_usd = float(min(maes)) if maes else 0.0
    avg_mae_usd = float(sum(maes) / len(maes)) if maes else 0.0
    avg_mfe_usd = float(sum(mfes) / len(mfes)) if mfes else 0.0

    # 🅑 연속 win/loss streak (시간 순서 — trades 가 entry/exit 순서대로 들어옴)
    max_win_streak = 0
    max_loss_streak = 0
    cur_win = 0
    cur_loss = 0
    for t in trades:
        if t.net_pnl_usd > 0:
            cur_win += 1
            cur_loss = 0
            max_win_streak = max(max_win_streak, cur_win)
        elif t.net_pnl_usd < 0:
            cur_loss += 1
            cur_win = 0
            max_loss_streak = max(max_loss_streak, cur_loss)
        else:  # 정확히 0 (드물지만 정의)
            cur_win = 0
            cur_loss = 0

    # Sharpe (annualized) — trade returns 기반.
    # 가정: 보유기간(horizon) 동안 자본 노출. 연중 거래 가능 회수 ≈ 252 / horizon.
    horizon = trades[0].horizon_days
    if rets.std(ddof=1) > 0 and horizon > 0:
        trades_per_year = BUSINESS_DAYS_PER_YEAR / horizon
        sharpe = float(rets.mean() / rets.std(ddof=1) * math.sqrt(trades_per_year))
    else:
        sharpe = 0.0

    # MDD on equity curve (절대 USD 기준)
    ec = result.equity_curve
    if len(ec) > 0:
        running_max = ec.cummax()
        mdd = float((ec - running_max).min())
    else:
        mdd = 0.0

    return Summary(
        n_trades=n,
        win_rate=float(len(wins) / n),
        avg_return=float(rets.mean()),
        avg_win=avg_win,
        avg_loss=avg_loss,
        profit_factor=profit_factor,
        total_net_pnl_usd=float(sum(t.net_pnl_usd for t in trades)),
        gross_profit_usd=gross_profit_usd,
        gross_loss_usd=gross_loss_usd,
        sharpe_annualized=sharpe,
        max_drawdown_usd=mdd,
        worst_mae_usd=worst_mae_usd,
        avg_mae_usd=avg_mae_usd,
        avg_mfe_usd=avg_mfe_usd,
        max_win_streak=max_win_streak,
        max_loss_streak=max_loss_streak,
        low_sample=(n < LOW_SAMPLE_THRESHOLD),
    )

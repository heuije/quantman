"""(side × threshold × horizon) 그리드 탐색 + walk-forward 검증.

엑셀 원본 한계 #10 보완:
- in-sample/out-of-sample 분할 walk-forward로 overfit 위험을 노출.
- 그리드 결과는 DataFrame으로 변환 가능 → CSV/대시보드 입력 직결.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from .backtest import CostModel, run_backtest
from .metrics import Summary, summarize
from .signals import Side, generate_signals


@dataclass(frozen=True)
class GridCell:
    """그리드 한 칸의 결과 (side, threshold, horizon × Summary)."""

    side: Side
    threshold: float
    horizon_days: int
    summary: Summary


def grid_search(
    df: pd.DataFrame,
    short_thresholds: Iterable[float],
    long_thresholds: Iterable[float],
    horizons: Iterable[int],
    cost: CostModel = CostModel(),
) -> list[GridCell]:
    """모든 (side, threshold, horizon) 조합 백테스트.

    같은 threshold 의 신호 리스트는 horizon별로 재사용 (성능).
    """
    cells: list[GridCell] = []
    horizons = list(horizons)

    for th in short_thresholds:
        sigs = generate_signals(df, short_thresholds=[th])
        for h in horizons:
            bt = run_backtest(df, sigs, horizon_days=int(h), cost=cost)
            cells.append(GridCell(Side.SHORT, float(th), int(h), summarize(bt)))

    for th in long_thresholds:
        sigs = generate_signals(df, long_thresholds=[th])
        for h in horizons:
            bt = run_backtest(df, sigs, horizon_days=int(h), cost=cost)
            cells.append(GridCell(Side.LONG, float(th), int(h), summarize(bt)))

    return cells


def grid_to_dataframe(cells: list[GridCell]) -> pd.DataFrame:
    """GridCell 리스트 → DataFrame (대시보드/CSV 출력용)."""
    rows = []
    for c in cells:
        s = c.summary
        rows.append({
            "side": c.side.value,
            "threshold": c.threshold,
            "horizon": c.horizon_days,
            "n_trades": s.n_trades,
            "win_rate": s.win_rate,
            "avg_return": s.avg_return,
            "avg_win": s.avg_win,
            "avg_loss": s.avg_loss,
            "profit_factor": s.profit_factor,
            "total_net_pnl_usd": s.total_net_pnl_usd,
            "sharpe_annualized": s.sharpe_annualized,
            "max_drawdown_usd": s.max_drawdown_usd,
            "low_sample": s.low_sample,
        })
    return pd.DataFrame(rows)


@dataclass(frozen=True)
class WalkForwardResult:
    """walk-forward 검증 결과.

    best_in_sample: train 구간에서 net PnL 최대 조합 (저샘플 제외).
    best_out_of_sample: 같은 파라미터를 test 구간에 적용한 결과.
    in-sample vs out-of-sample 격차가 크면 overfit 가능성 시사.
    """

    train_period: tuple[pd.Timestamp, pd.Timestamp]
    test_period: tuple[pd.Timestamp, pd.Timestamp]
    best_in_sample: GridCell
    best_out_of_sample: Summary


def walk_forward(
    df: pd.DataFrame,
    short_thresholds: Iterable[float],
    long_thresholds: Iterable[float],
    horizons: Iterable[int],
    split_date: pd.Timestamp,
    cost: CostModel = CostModel(),
    require_min_trades: int = 5,
) -> WalkForwardResult:
    """split_date 기준 train/test 분할 walk-forward.

    train(split_date 이전)에서 grid_search → 거래 수 require_min_trades 이상 조합 중
    total_net_pnl_usd 최대 셀 선택 → 동일 파라미터로 test(split_date 이후) 평가.
    """
    df_train = df[df["date"] < split_date].reset_index(drop=True)
    df_test = df[df["date"] >= split_date].reset_index(drop=True)
    if df_train.empty or df_test.empty:
        raise ValueError(
            f"split_date={split_date.date()}로 train/test 분할 불가 "
            f"(train={len(df_train)}, test={len(df_test)})"
        )

    cells = grid_search(df_train, short_thresholds, long_thresholds, horizons, cost)
    eligible = [c for c in cells if c.summary.n_trades >= require_min_trades]
    if not eligible:
        raise ValueError(
            f"train에 거래 수 {require_min_trades} 이상인 조합이 없음 — "
            "임계값/horizon 범위를 넓혀야 함"
        )

    best = max(eligible, key=lambda c: c.summary.total_net_pnl_usd)

    # OOS 평가: 같은 (side, threshold, horizon) 적용
    short_th = [best.threshold] if best.side == Side.SHORT else []
    long_th = [best.threshold] if best.side == Side.LONG else []
    sigs_test = generate_signals(
        df_test, short_thresholds=short_th, long_thresholds=long_th
    )
    bt_test = run_backtest(df_test, sigs_test, best.horizon_days, cost)
    oos = summarize(bt_test)

    return WalkForwardResult(
        train_period=(
            pd.Timestamp(df_train["date"].iloc[0]),
            pd.Timestamp(df_train["date"].iloc[-1]),
        ),
        test_period=(
            pd.Timestamp(df_test["date"].iloc[0]),
            pd.Timestamp(df_test["date"].iloc[-1]),
        ),
        best_in_sample=best,
        best_out_of_sample=oos,
    )

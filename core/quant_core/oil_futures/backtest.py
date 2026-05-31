"""신호 → 백테스트 거래 시뮬레이션.

엑셀 원본 한계 #3, #5 보완:
- 진입가 = 신호일 다음 영업일 시가 (look-ahead bias 제거).
  엑셀은 entry = 신호일 종가로 가정 → 종가는 마감 후에 확정되므로 비현실.
- 청산가 = 진입 후 horizon_days 영업일 후 종가.
- 수수료·슬리피지 모델 외부 주입 (CostModel) — 비용 0 모드도 지원.

WTI 선물 계약 단위:
- 1 contract = 1000 배럴, 틱 사이즈 = $0.01/배럴
- 1틱 가치 = $10 (1 contract 기준)
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .signals import Side, Signal

# WTI 선물 (CL) 계약 사양
WTI_TICK = 0.01       # USD/배럴
WTI_MULTIPLIER = 1000  # 1계약 = 1000배럴


@dataclass(frozen=True)
class CostModel:
    """거래 비용 모델.

    commission_per_contract: 한 방향(진입 또는 청산)당 계약당 수수료(USD).
    slippage_ticks: 진입·청산 각각 N틱씩 불리한 가격으로 체결 가정.
    """

    commission_per_contract: float = 2.5
    slippage_ticks: int = 1


@dataclass(frozen=True)
class Trade:
    """완결된 단일 거래.

    MAE/MFE: 보유 기간 중의 *장중* 최악·최고 가격 기준 평가손익(미실현).
    horizon 만기 청산 PnL만 보는 한계 해소 — 시가평가 위험 가시화.
    1계약 기준 USD. MAE는 음수(불리), MFE는 양수(유리).
    """

    signal: Signal
    horizon_days: int
    entry_date: pd.Timestamp
    entry_price: float       # 실제 진입가 (다음 영업일 시가)
    exit_date: pd.Timestamp
    exit_price: float        # 청산가 (horizon_days 후 종가)
    gross_pnl_usd: float     # 1계약 기준, 비용 차감 전
    net_pnl_usd: float       # 1계약 기준, 비용 차감 후
    return_pct: float        # gross 수익률 (sign 적용)
    mae_usd: float           # 보유 중 최악 평가손실 (음수 또는 0, 1계약)
    mfe_usd: float           # 보유 중 최고 평가이익 (양수 또는 0, 1계약)


@dataclass(frozen=True)
class BacktestResult:
    """백테스트 산출물."""

    trades: list[Trade]
    equity_curve: pd.Series  # exit_date 기준 누적 net_pnl_usd


def run_backtest(
    df: pd.DataFrame,
    signals: list[Signal],
    horizon_days: int,
    cost: CostModel = CostModel(),
) -> BacktestResult:
    """신호 리스트에 horizon_days 보유 정책 적용 → BacktestResult.

    df는 load_wti 출력 호환 (date ASC, OHLCV).
    horizon_days 후 데이터가 부족한 신호는 거래로 잡지 않는다 (미실현 포지션).
    """
    if horizon_days < 1:
        raise ValueError(f"horizon_days >= 1 이어야 함 (입력: {horizon_days})")

    df = df.sort_values("date").reset_index(drop=True)
    # 날짜 → 인덱스 매핑 (pd.Timestamp 키)
    idx_by_date = {pd.Timestamp(d): i for i, d in enumerate(df["date"])}
    n = len(df)

    slip = cost.slippage_ticks * WTI_TICK

    trades: list[Trade] = []
    for sig in signals:
        sig_i = idx_by_date.get(pd.Timestamp(sig.date))
        if sig_i is None:
            continue
        entry_i = sig_i + 1
        exit_i = entry_i + horizon_days
        if exit_i >= n:
            # horizon이 미래로 벗어남 → 거래 미성립
            continue

        entry_row = df.iloc[entry_i]
        exit_row = df.iloc[exit_i]
        entry_price = float(entry_row["open"])
        exit_price = float(exit_row["close"])

        # MAE/MFE: 진입~청산 사이 모든 영업일의 장중 high/low 추적
        # (진입일 시가는 이미 잡았으니 다음날부터 청산일까지)
        held = df.iloc[entry_i : exit_i + 1]
        held_high = float(held["high"].max())
        held_low = float(held["low"].min())
        if sig.side == Side.LONG:
            mfe_price = held_high - entry_price       # 최고가 - 진입가 (이익 가능 최대)
            mae_price = held_low - entry_price        # 최저가 - 진입가 (손실 가능 최대, 음수)
        else:  # SHORT
            mfe_price = entry_price - held_low        # 진입가 - 최저가 (숏 이익)
            mae_price = entry_price - held_high       # 진입가 - 최고가 (숏 손실, 음수)
        mfe_usd = max(0.0, mfe_price) * WTI_MULTIPLIER
        mae_usd = min(0.0, mae_price) * WTI_MULTIPLIER

        sign = -1.0 if sig.side == Side.SHORT else 1.0

        # 슬리피지: 진입/청산 모두 불리하게.
        # Long: 진입가↑, 청산가↓. Short: 진입가↓, 청산가↑.
        if sig.side == Side.LONG:
            eff_entry = entry_price + slip
            eff_exit = exit_price - slip
        else:
            eff_entry = entry_price - slip
            eff_exit = exit_price + slip

        gross = sign * (exit_price - entry_price) * WTI_MULTIPLIER
        net_price_diff = sign * (eff_exit - eff_entry)
        net = net_price_diff * WTI_MULTIPLIER - 2 * cost.commission_per_contract
        ret = sign * (exit_price / entry_price - 1)

        trades.append(
            Trade(
                signal=sig,
                horizon_days=horizon_days,
                entry_date=pd.Timestamp(entry_row["date"]),
                entry_price=entry_price,
                exit_date=pd.Timestamp(exit_row["date"]),
                exit_price=exit_price,
                gross_pnl_usd=gross,
                net_pnl_usd=net,
                return_pct=ret,
                mae_usd=mae_usd,
                mfe_usd=mfe_usd,
            )
        )

    if trades:
        # 동일 exit_date에 다건이면 합산 후 누적
        s = pd.Series(
            [t.net_pnl_usd for t in trades],
            index=[t.exit_date for t in trades],
        )
        equity = s.groupby(level=0).sum().sort_index().cumsum()
    else:
        equity = pd.Series(dtype=float)

    return BacktestResult(trades=trades, equity_curve=equity)

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
from typing import Optional

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
class ExitRules:
    """horizon 만기 외 조기 청산 룰.

    stop_loss_pct=0.10 → 진입가 대비 10% 손실 도달 시 그날 즉시 청산 (장중 high/low 기준).
    take_profit_pct=0.20 → 진입가 대비 20% 이익 도달 시 그날 즉시 청산.
    None = 그 룰 비활성. 둘 다 None이면 기존 horizon 고정 보유와 동일.

    체결 가정: 그날 SL/TP price에 정확히 체결 (보수적이려면 slippage 추가).
    같은 날 둘 다 hit한 경우 → SL 우선 (worst-case).
    """

    stop_loss_pct: Optional[float] = None      # 0~1, 예: 0.10 = -10%
    take_profit_pct: Optional[float] = None    # 0~1, 예: 0.20 = +20%


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
    exit_reason: str         # 'horizon' | 'stop_loss' | 'take_profit'


@dataclass(frozen=True)
class BacktestResult:
    """백테스트 산출물."""

    trades: list[Trade]
    equity_curve: pd.Series          # exit_date 기준 누적 net_pnl_usd (realized)
    portfolio_equity_curve: pd.Series  # 매 영업일 mark-to-market 포트폴리오 가치 (시가평가)
    portfolio_mdd_usd: float         # mark-to-market 곡선의 MDD (음수)


def run_backtest(
    df: pd.DataFrame,
    signals: list[Signal],
    horizon_days: int,
    cost: CostModel = CostModel(),
    exits: ExitRules = ExitRules(),
) -> BacktestResult:
    """신호 리스트에 horizon_days 보유 정책 적용 → BacktestResult.

    df는 load_wti 출력 호환 (date ASC, OHLCV).
    horizon_days 후 데이터가 부족한 신호는 거래로 잡지 않는다 (미실현 포지션).

    exits.stop_loss_pct / take_profit_pct 지정 시 horizon 전에 조기 청산.
    """
    if horizon_days < 1:
        raise ValueError(f"horizon_days >= 1 이어야 함 (입력: {horizon_days})")

    df = df.sort_values("date").reset_index(drop=True)
    idx_by_date = {pd.Timestamp(d): i for i, d in enumerate(df["date"])}
    n = len(df)

    slip = cost.slippage_ticks * WTI_TICK
    sl_pct = exits.stop_loss_pct
    tp_pct = exits.take_profit_pct

    trades: list[Trade] = []
    for sig in signals:
        sig_i = idx_by_date.get(pd.Timestamp(sig.date))
        if sig_i is None:
            continue
        entry_i = sig_i + 1
        horizon_exit_i = entry_i + horizon_days
        if horizon_exit_i >= n:
            continue

        entry_row = df.iloc[entry_i]
        entry_price = float(entry_row["open"])
        sign = -1.0 if sig.side == Side.SHORT else 1.0

        # SL/TP price (Long 기준 계산 후 Short는 반대)
        if sig.side == Side.LONG:
            sl_price = entry_price * (1 - sl_pct) if sl_pct is not None else None
            tp_price = entry_price * (1 + tp_pct) if tp_pct is not None else None
        else:
            sl_price = entry_price * (1 + sl_pct) if sl_pct is not None else None
            tp_price = entry_price * (1 - tp_pct) if tp_pct is not None else None

        # 보유 구간 day-by-day 스캔 (SL/TP hit 검사)
        exit_i = horizon_exit_i
        exit_price = float(df.iloc[horizon_exit_i]["close"])
        exit_reason = "horizon"

        if sl_pct is not None or tp_pct is not None:
            for k in range(entry_i, horizon_exit_i + 1):
                day = df.iloc[k]
                day_high = float(day["high"])
                day_low = float(day["low"])

                if sig.side == Side.LONG:
                    sl_hit = sl_price is not None and day_low <= sl_price
                    tp_hit = tp_price is not None and day_high >= tp_price
                else:  # SHORT
                    sl_hit = sl_price is not None and day_high >= sl_price
                    tp_hit = tp_price is not None and day_low <= tp_price

                if sl_hit and tp_hit:
                    # 같은 날 둘 다 — 보수적으로 SL 우선
                    exit_i = k
                    exit_price = sl_price  # type: ignore
                    exit_reason = "stop_loss"
                    break
                if sl_hit:
                    exit_i = k
                    exit_price = sl_price  # type: ignore
                    exit_reason = "stop_loss"
                    break
                if tp_hit:
                    exit_i = k
                    exit_price = tp_price  # type: ignore
                    exit_reason = "take_profit"
                    break

        exit_row = df.iloc[exit_i]

        # MAE/MFE: 실제 보유 구간(entry~exit) 장중 high/low 기준
        held = df.iloc[entry_i : exit_i + 1]
        held_high = float(held["high"].max())
        held_low = float(held["low"].min())
        if sig.side == Side.LONG:
            mfe_price = held_high - entry_price
            mae_price = held_low - entry_price
        else:
            mfe_price = entry_price - held_low
            mae_price = entry_price - held_high
        mfe_usd = max(0.0, mfe_price) * WTI_MULTIPLIER
        mae_usd = min(0.0, mae_price) * WTI_MULTIPLIER

        # 슬리피지
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
                exit_reason=exit_reason,
            )
        )

    # Realized equity (기존)
    if trades:
        s = pd.Series(
            [t.net_pnl_usd for t in trades],
            index=[t.exit_date for t in trades],
        )
        equity = s.groupby(level=0).sum().sort_index().cumsum()
    else:
        equity = pd.Series(dtype=float)

    # Portfolio mark-to-market equity (시가평가)
    # 매 영업일: 종료된 trade의 realized PnL 누적 + 열려있는 trade의 미실현 P&L 합
    portfolio_curve, portfolio_mdd = _compute_portfolio_mtm(df, trades, cost)

    return BacktestResult(
        trades=trades,
        equity_curve=equity,
        portfolio_equity_curve=portfolio_curve,
        portfolio_mdd_usd=portfolio_mdd,
    )


def _compute_portfolio_mtm(
    df: pd.DataFrame,
    trades: list[Trade],
    cost: CostModel,
) -> tuple[pd.Series, float]:
    """매 영업일 mark-to-market 포트폴리오 가치 곡선 + MDD.

    realized: 그 날까지 청산 완료된 trade의 net_pnl 합.
    unrealized: 그 날 열려있는 모든 trade의 (today_close - entry_price) * sign * mult.
    portfolio_value = realized + unrealized.
    MDD = peak-to-trough on this curve (음수 USD).
    """
    if not trades:
        return pd.Series(dtype=float), 0.0

    # 인덱스 빠른 lookup
    df_idx = df.set_index("date")["close"]
    dates = df_idx.index
    # 첫 trade entry부터 마지막 trade exit까지만 그림
    start = min(t.entry_date for t in trades)
    end = max(t.exit_date for t in trades)
    mask = (dates >= start) & (dates <= end)
    relevant_dates = dates[mask]

    closes = df_idx.loc[relevant_dates]

    values = []
    for d in relevant_dates:
        close = float(closes.loc[d])
        realized = 0.0
        unrealized = 0.0
        for t in trades:
            if t.exit_date <= d:
                realized += t.net_pnl_usd
            elif t.entry_date <= d < t.exit_date:
                sign = -1.0 if t.signal.side == Side.SHORT else 1.0
                # 미실현 = (현재가 - 진입가) * sign * 멀티플, 수수료는 청산시 차감 (미반영)
                unrealized += sign * (close - t.entry_price) * WTI_MULTIPLIER
        values.append(realized + unrealized)

    curve = pd.Series(values, index=relevant_dates)
    running_max = curve.cummax()
    mdd = float((curve - running_max).min())

    return curve, mdd

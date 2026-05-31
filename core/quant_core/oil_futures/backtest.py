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
class RollModel:
    """선물 만기 강제 롤오버 모델 (A-2: 만기 청산 → 재진입 → horizon까지 유지).

    WTI는 실물 인수도라 만기 전 강제 청산/롤오버 필수. 보유기간에 만기가 끼면
    그 횟수만큼 롤오버가 발생하고, 매 롤마다 contango/backwardation 롤 비용 +
    거래 마찰이 든다.

    roll_cost_pct: 롤오버 1회당 추정 비용 (notional 대비, 양수=비용).
      ⚠️ 추정 가정 — 우리 데이터는 연속물 단일 시계열이라 실제 근월/원월
      가격차(term structure)가 없다. 정확한 롤 yield 계산엔 만기물별 데이터 필요.
      예: 0.005 = 롤 1회당 0.5% 비용 (contango 평균적 드래그 가정).
    apply_txn_per_roll: 롤마다 진입/청산 왕복 거래비용도 부과할지.
    """

    roll_cost_pct: float = 0.0
    apply_txn_per_roll: bool = True


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
    net_pnl_usd: float       # 1계약 기준, 비용 차감 후 (롤 비용 포함)
    return_pct: float        # gross 수익률 (sign 적용, 롤 비용 차감 후)
    mae_usd: float           # 보유 중 최악 평가손실 (음수 또는 0, 1계약)
    mfe_usd: float           # 보유 중 최고 평가이익 (양수 또는 0, 1계약)
    exit_reason: str         # 'horizon' | 'stop_loss' | 'take_profit'
    num_rollovers: int = 0   # 보유 중 통과한 만기(=강제 롤오버) 횟수
    roll_cost_usd: float = 0.0  # 롤 비용 총합 (음수 또는 0, 1계약)


def wti_expiry_dates(start: pd.Timestamp, end: pd.Timestamp) -> list[pd.Timestamp]:
    """WTI(CL) 월물 만기일 추정 리스트.

    CME 규칙: 인도월 전월 25일의 3영업일 전 거래 종료.
    → 각 캘린더 월마다 1개 만기 (대략 20~22일경).
    start~end 범위를 덮는 모든 월의 만기일을 반환.
    """
    out: list[pd.Timestamp] = []
    # 범위보다 한 달 여유 두고 순회
    cur = pd.Timestamp(start.year, start.month, 1) - pd.DateOffset(months=1)
    last = pd.Timestamp(end.year, end.month, 1) + pd.DateOffset(months=2)
    while cur <= last:
        # 이 달(cur) 25일에서 3영업일 전 = 인도월(cur+1)물의 만기
        twenty_fifth = pd.Timestamp(cur.year, cur.month, 25)
        # 25일 포함 직전 영업일들 카운트 (주말 제외, 공휴일은 근사 무시)
        bd = pd.bdate_range(end=twenty_fifth, periods=4)
        expiry = bd[0]  # 3영업일 전 (4개 중 첫째 = 25일 포함 시 -3)
        if start - pd.Timedelta(days=40) <= expiry <= end + pd.Timedelta(days=40):
            out.append(expiry)
        cur = cur + pd.DateOffset(months=1)
    return sorted(set(out))


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
    roll: RollModel = RollModel(),
) -> BacktestResult:
    """신호 리스트에 horizon_days 보유 정책 적용 → BacktestResult.

    df는 load_wti 출력 호환 (date ASC, OHLCV).
    horizon_days 후 데이터가 부족한 신호는 거래로 잡지 않는다 (미실현 포지션).

    exits.stop_loss_pct / take_profit_pct 지정 시 horizon 전에 조기 청산.
    roll.roll_cost_pct > 0 시: 보유 중 통과한 만기 횟수만큼 롤 비용을 차감 (A-2).
    """
    if horizon_days < 1:
        raise ValueError(f"horizon_days >= 1 이어야 함 (입력: {horizon_days})")

    df = df.sort_values("date").reset_index(drop=True)
    idx_by_date = {pd.Timestamp(d): i for i, d in enumerate(df["date"])}
    n = len(df)

    slip = cost.slippage_ticks * WTI_TICK
    sl_pct = exits.stop_loss_pct
    tp_pct = exits.take_profit_pct

    # 만기 스케줄 — 데이터 전체 범위 1회 생성 후 재사용
    expiries = wti_expiry_dates(
        pd.Timestamp(df["date"].iloc[0]), pd.Timestamp(df["date"].iloc[-1])
    ) if len(df) else []

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

        # ── 만기 강제 롤오버 (A-2) ─────────────────────────────────────
        entry_d = pd.Timestamp(entry_row["date"])
        exit_d = pd.Timestamp(exit_row["date"])
        num_rolls = sum(1 for e in expiries if entry_d < e <= exit_d)
        roll_cost = 0.0
        if num_rolls > 0 and roll.roll_cost_pct > 0:
            # 롤 1회당: notional(진입가×멀티플) 대비 roll_cost_pct + (옵션) 왕복 거래비용
            notional = entry_price * WTI_MULTIPLIER
            roll_cost = -(num_rolls * roll.roll_cost_pct * notional)
            if roll.apply_txn_per_roll:
                # 각 롤 = 청산+재진입 = 왕복 (수수료 2회 + 슬리피지 2회분)
                txn = 2 * cost.commission_per_contract + 2 * slip * WTI_MULTIPLIER
                roll_cost -= num_rolls * txn
            net += roll_cost
            # 수익률에도 반영 (notional 대비)
            ret += roll_cost / notional if notional else 0.0

        trades.append(
            Trade(
                signal=sig,
                horizon_days=horizon_days,
                entry_date=entry_d,
                entry_price=entry_price,
                exit_date=exit_d,
                exit_price=exit_price,
                gross_pnl_usd=gross,
                net_pnl_usd=net,
                return_pct=ret,
                mae_usd=mae_usd,
                mfe_usd=mfe_usd,
                exit_reason=exit_reason,
                num_rollovers=num_rolls,
                roll_cost_usd=roll_cost,
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
    # 데이터 전체 기간 (2004-01-05 ~ 데이터 끝) 으로 곡선을 그림.
    # 첫 trade 이전 = 평평한 0, 마지막 trade 청산 이후 = 마지막 누적값 평평하게 유지.
    # 차트 x축이 항상 전체 기간을 보여줌 (예: 2025·2026도 보임 — 활동 없으면 평평).
    relevant_dates = dates

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

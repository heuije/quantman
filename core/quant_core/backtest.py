"""
백테스트 엔진.

매수 조건이 충족되면 진입하고, 청산 규칙(보유기간 · 익절 · 손절 · 매도조건)
중 먼저 트리거되는 것으로 청산한다. 일별 자산곡선과 성과지표를 산출한다.

체결 모델:
  - "next_open" : 신호 다음 거래일 시가 체결 (look-ahead bias 방지, 기본값)
  - "close"     : 신호 당일 종가 체결
포지션은 단일 종목·단일 포지션, 가용 현금 전액 투입으로 가정한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .analysis import build_signal_mask

TRADING_DAYS = 252


def _empty(error: str) -> dict:
    return {"success": False, "error": error}


def _metrics(equity: pd.Series, benchmark: pd.Series, trades_df: pd.DataFrame) -> dict:
    def _stats(curve: pd.Series):
        first, last = float(curve.iloc[0]), float(curve.iloc[-1])
        total = (last - first) / first * 100
        years = len(curve) / TRADING_DAYS
        cagr = ((last / first) ** (1 / years) - 1) * 100 if years > 0 and last > 0 else np.nan
        peak = curve.cummax()
        mdd = ((curve - peak) / peak).min() * 100
        return total, cagr, mdd

    s_total, s_cagr, s_mdd = _stats(equity)
    b_total, b_cagr, b_mdd = _stats(benchmark)

    daily = equity.pct_change().dropna()
    sharpe = (daily.mean() / daily.std() * np.sqrt(TRADING_DAYS)
              if daily.std() and daily.std() > 0 else np.nan)

    n_trades = len(trades_df)
    if n_trades:
        win_rate = (trades_df["수익률(%)"] > 0).mean() * 100
        avg_hold = trades_df["보유일"].mean()
        avg_ret = trades_df["수익률(%)"].mean()
    else:
        win_rate = avg_hold = avg_ret = np.nan

    return {
        "total_return": s_total, "cagr": s_cagr, "mdd": s_mdd,
        "sharpe": sharpe, "n_trades": n_trades, "win_rate": win_rate,
        "avg_hold": avg_hold, "avg_trade_return": avg_ret,
        "bench_total": b_total, "bench_cagr": b_cagr, "bench_mdd": b_mdd,
        "excess_return": s_total - b_total,
    }


def run_backtest(
    data: dict[str, pd.DataFrame],
    trade_symbol: str,
    buy_conditions: list[dict],
    buy_logic: str = "AND",
    hold_days: int | None = None,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    trail_atr_mult: float | None = None,
    trail_pct: float | None = None,
    sell_conditions: list[dict] | None = None,
    sell_logic: str = "AND",
    fill: str = "next_open",
    commission: float = 0.00015,
    slippage: float = 0.0005,
    initial_capital: float = 10_000_000.0,
    start=None,
    end=None,
) -> dict:
    """매매 전략을 과거 데이터로 시뮬레이션한다. 결과 dict를 반환한다."""
    if trade_symbol not in data or data[trade_symbol].empty:
        return _empty(f"'{trade_symbol}' 데이터 없음")
    if not buy_conditions:
        return _empty("매수 조건을 1개 이상 설정하세요.")

    trade_df = data[trade_symbol]
    if not {"Open", "Close"}.issubset(trade_df.columns):
        return _empty(f"'{trade_symbol}'에 시가·종가 데이터가 없습니다.")

    trade_df = trade_df.sort_index()
    if start is not None:
        trade_df = trade_df[trade_df.index >= pd.Timestamp(start)]
    if end is not None:
        trade_df = trade_df[trade_df.index <= pd.Timestamp(end)]
    trade_df = trade_df.dropna(subset=["Open", "Close"])
    if len(trade_df) < 2:
        return _empty("백테스트 기간의 가격 데이터가 부족합니다.")

    buy_mask = build_signal_mask(data, buy_conditions, buy_logic)
    if buy_mask.empty:
        return _empty("매수 조건의 종목·지표를 확인하세요.")
    buy_arr = buy_mask.reindex(trade_df.index, fill_value=False).to_numpy(dtype=bool)

    if sell_conditions:
        sell_mask = build_signal_mask(data, sell_conditions, sell_logic)
        if sell_mask.empty:
            return _empty("매도 조건의 종목·지표를 확인하세요.")
        sell_arr = sell_mask.reindex(trade_df.index, fill_value=False).to_numpy(dtype=bool)
    else:
        sell_arr = None

    dates = trade_df.index
    opens = trade_df["Open"].to_numpy(dtype=float)
    closes = trade_df["Close"].to_numpy(dtype=float)
    highs = (trade_df["High"].to_numpy(dtype=float)
             if "High" in trade_df.columns else closes)
    atr_arr = (trade_df["atr_14"].to_numpy(dtype=float)
               if "atr_14" in trade_df.columns else None)
    if trail_atr_mult is not None and atr_arr is None:
        return _empty(f"'{trade_symbol}'에 ATR 지표가 없어 ATR 트레일링 스톱을 쓸 수 없습니다.")
    n = len(trade_df)
    next_open = (fill == "next_open")

    cash = float(initial_capital)
    shares = 0.0
    position = False
    entry_price = 0.0
    entry_i = -1
    peak_high = 0.0      # 진입 후 최고가 (ATR 트레일링용)
    peak_close = 0.0     # 진입 후 최고 종가 (비율 트레일링용)
    pending_buy = False
    pending_sell = False
    pending_reason = ""

    trades: list[dict] = []
    equity = np.empty(n, dtype=float)

    def _open(i: int, raw_price: float):
        nonlocal cash, shares, position, entry_price, entry_i, peak_high, peak_close
        price = raw_price * (1 + slippage)
        shares = cash / (price * (1 + commission))
        cash -= shares * price * (1 + commission)
        entry_price = price
        entry_i = i
        peak_high = highs[i]
        peak_close = closes[i]
        position = True

    def _close(i: int, raw_price: float, reason: str):
        nonlocal cash, shares, position
        price = raw_price * (1 - slippage)
        proceeds = shares * price * (1 - commission)
        cost = shares * entry_price * (1 + commission)
        trades.append({
            "진입일": dates[entry_i],
            "청산일": dates[i],
            "보유일": i - entry_i,
            "진입가": entry_price,
            "청산가": price,
            "수익률(%)": (proceeds - cost) / cost * 100,
            "청산사유": reason,
        })
        cash += proceeds
        shares = 0.0
        position = False

    for i in range(n):
        # 1) 익일 시가 체결: 전일 신호를 오늘 시가에 실행
        if next_open:
            if pending_buy and not position:
                _open(i, opens[i])
                pending_buy = False
            if pending_sell and position:
                _close(i, opens[i], pending_reason)
                pending_sell = False

        # 2) 오늘 종가 기준 신호 평가
        if not position and not pending_buy:
            if buy_arr[i]:
                if next_open:
                    pending_buy = True
                else:
                    _open(i, closes[i])
        elif position and not pending_sell:
            if highs[i] > peak_high:
                peak_high = highs[i]
            if closes[i] > peak_close:
                peak_close = closes[i]
            cur_ret = (closes[i] - entry_price) / entry_price * 100
            held = i - entry_i
            reason = ""
            if take_profit is not None and cur_ret >= take_profit:
                reason = "익절"
            elif stop_loss is not None and cur_ret <= stop_loss:
                reason = "손절"
            elif (trail_atr_mult is not None and atr_arr is not None
                  and not np.isnan(atr_arr[i])
                  and closes[i] <= peak_high - trail_atr_mult * atr_arr[i]):
                reason = "ATR트레일링"
            elif (trail_pct is not None
                  and closes[i] <= peak_close * (1 - trail_pct / 100)):
                reason = "트레일링스톱"
            elif hold_days is not None and held >= hold_days:
                reason = "보유기간"
            elif sell_arr is not None and sell_arr[i]:
                reason = "매도신호"
            if reason:
                if next_open:
                    pending_sell = True
                    pending_reason = reason
                else:
                    _close(i, closes[i], reason)

        equity[i] = cash + shares * closes[i]

    # 기간 종료 시 미청산 포지션 강제 청산
    if position:
        _close(n - 1, closes[n - 1], "기간종료")
        equity[n - 1] = cash

    equity_s = pd.Series(equity, index=dates, name="전략")
    benchmark_s = pd.Series(closes / closes[0] * initial_capital,
                            index=dates, name="Buy&Hold")
    trades_df = pd.DataFrame(trades)

    return {
        "success": True,
        "error": None,
        "equity": equity_s,
        "benchmark": benchmark_s,
        "trades": trades_df,
        "metrics": _metrics(equity_s, benchmark_s, trades_df),
    }

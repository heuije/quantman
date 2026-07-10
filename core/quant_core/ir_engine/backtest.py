"""1회 백테스트 — 블록 IR 신호로 구동 (단일 종목).

명세 §7. 매수/매도 신호를 새 평가기(blocks.evaluate)로 만들고, 검증된 시뮬레이션
부품(_metrics·round_to_tick)을 재사용한다. 기존 run_backtest의 단일종목 path와
비트 동일한 결과를 내도록 루프를 이식했다 — test_engine_backtest_ir로 고정.

포트폴리오/스크리너/팩터 path의 IR 통합은 후속 증분(P1-3b~).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..backtest import (
    _DEFAULT_COMMISSION, _DEFAULT_SELL_TAX, _DEFAULT_SLIPPAGE, _empty, _metrics,
)
from ..blocks import EvalContext, Node, evaluate, select_symbol
from ..exec_defaults import round_to_tick
from .metrics import finalize_metrics

_REASON_KEYS = (
    ("익절", "tp"), ("손절", "sl"), ("ATR트레일링", "atr"),
    ("트레일링", "trail"), ("보유기간", "hold"),
)


def _ir_mask(node: Node | None, ctx: EvalContext, sym: str, index) -> np.ndarray | None:
    """조건 Node를 평가해 sym 종목의 boolean 배열로. node None이면 None."""
    if node is None:
        return None
    panel = evaluate(node, ctx)
    series = select_symbol(panel, sym)
    return series.reindex(index, fill_value=False).fillna(False).to_numpy(dtype=bool)


def run_backtest_ir(
    dataset: dict[str, pd.DataFrame],
    trade_symbol: str,
    buy_node: Node,
    *,
    sell_node: Node | None = None,
    hold_days: int | None = None,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    trail_atr_mult: float | None = None,
    trail_pct: float | None = None,
    sell_amount_pct: float = 100.0,
    rule_sell_pcts: dict | None = None,
    fill: str = "next_open",
    commission: float = _DEFAULT_COMMISSION,
    slippage: float = _DEFAULT_SLIPPAGE,
    sell_tax: float = _DEFAULT_SELL_TAX,
    initial_capital: float = 10_000_000.0,
    start=None,
    end=None,
) -> dict:
    """단일 종목 백테스트 — 매수/매도 신호를 블록 IR로 평가.

    buy_node/sell_node는 type:condition Node. 단일 종목 path의 체결·청산·성과 로직.
    """
    # 통화는 종목에서 추론(국내 6자리 숫자=KRW, 그 외=USD) — 다종목 경로와 동일 규칙.
    # 호가단위 라운딩(round_to_tick)용. 시뮬 설정으로 덮어쓰지 않음(미국주식 KRW 정수절삭 방지).
    currency = "KRW" if trade_symbol.isdigit() else "USD"
    if trade_symbol not in dataset or dataset[trade_symbol].empty:
        return _empty(f"'{trade_symbol}' 데이터 없음")
    trade_df = dataset[trade_symbol]
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

    ctx = EvalContext.from_dataset(dataset, universe=[trade_symbol])
    buy_arr = _ir_mask(buy_node, ctx, trade_symbol, trade_df.index)
    if buy_arr is None:
        return _empty("매수 신호가 비었습니다.")
    sell_arr = _ir_mask(sell_node, ctx, trade_symbol, trade_df.index)

    dates = trade_df.index
    opens = trade_df["Open"].to_numpy(dtype=float)
    closes = trade_df["Close"].to_numpy(dtype=float)
    highs = (trade_df["High"].to_numpy(dtype=float)
             if "High" in trade_df.columns else closes)
    lows = (trade_df["Low"].to_numpy(dtype=float)
            if "Low" in trade_df.columns else closes)
    atr_arr = (trade_df["atr_14"].to_numpy(dtype=float)
               if "atr_14" in trade_df.columns else None)
    if trail_atr_mult is not None and atr_arr is None:
        return _empty(f"'{trade_symbol}'에 ATR 지표가 없어 ATR 트레일링을 쓸 수 없습니다.")
    n = len(trade_df)
    defer = (fill == "next_open")
    if fill == "typical" and not {"High", "Low"}.issubset(trade_df.columns):
        return _empty(f"'{trade_symbol}'에 고가·저가가 없어 typical 체결을 쓸 수 없습니다.")
    # 당일 체결가: close=종가, typical=(고+저+종)/3(일봉 VWAP 근사). next_open은 익일 시가.
    same_day_price = ((highs + lows + closes) / 3.0) if fill == "typical" else closes

    cash = float(initial_capital)
    shares = 0.0
    position = False
    entry_price = 0.0
    entry_i = -1
    peak_high = 0.0
    peak_close = 0.0
    pending_buy = False
    pending_sell = False
    pending_reason = ""
    executed_rules: set[str] = set()
    rule_pcts = rule_sell_pcts or {}

    def _sell_pct_of(reason: str) -> float:
        if not reason:
            return float(sell_amount_pct)
        for needle, key in _REASON_KEYS:
            if needle in reason:
                v = rule_pcts.get(key)
                return float(v) if v is not None else float(sell_amount_pct)
        return float(sell_amount_pct)

    def _reason_key(reason: str) -> str | None:
        for needle, key in _REASON_KEYS:
            if needle in reason:
                return key
        return None

    trades: list[dict] = []
    equity = np.empty(n, dtype=float)

    def _open(i: int, raw_price: float):
        nonlocal cash, shares, position, entry_price, entry_i, peak_high, peak_close
        slipped = raw_price * (1 + slippage)
        price = round_to_tick(slipped, direction="up", currency=currency)
        if price <= 0:
            return
        per_share_cost = price * (1 + commission)
        new_shares = int(cash // per_share_cost)
        if new_shares <= 0:
            return
        shares = float(new_shares)
        cash -= shares * per_share_cost
        entry_price = price
        entry_i = i
        peak_high = highs[i]
        peak_close = closes[i]
        position = True
        executed_rules.clear()

    def _close(i: int, raw_price: float, reason: str, sell_pct: float = 100.0):
        nonlocal cash, shares, position
        if shares <= 0:
            return
        sell_pct = max(0.0, min(100.0, float(sell_pct)))
        if sell_pct <= 0:
            return
        slipped = raw_price * (1 - slippage)
        price = round_to_tick(slipped, direction="down", currency=currency)
        if price <= 0:
            shares = 0.0
            position = False
            return
        sell_shares = float(int(shares * sell_pct / 100.0))
        if sell_shares <= 0:
            return
        sell_shares = min(sell_shares, shares)
        proceeds = sell_shares * price * (1 - commission - sell_tax)
        cost = sell_shares * entry_price * (1 + commission)
        is_partial = sell_shares < shares - 1e-9
        trades.append({
            "진입일": dates[entry_i], "청산일": dates[i], "보유일": i - entry_i,
            "진입가": entry_price, "청산가": price,
            "수익률(%)": (proceeds - cost) / cost * 100,
            "청산사유": reason + (f"({sell_pct:.0f}%)" if is_partial else ""),
        })
        cash += proceeds
        shares -= sell_shares
        if shares <= 1e-9:
            shares = 0.0
            position = False
            executed_rules.clear()

    for i in range(n):
        if defer:
            if pending_buy and not position:
                _open(i, opens[i])
                pending_buy = False
            if pending_sell and position:
                sell_pct = _sell_pct_of(pending_reason)
                _close(i, opens[i], pending_reason, sell_pct)
                key = _reason_key(pending_reason)
                if key is not None and position:
                    executed_rules.add(key)
                pending_sell = False

        if not position and not pending_buy:
            if buy_arr[i]:
                if defer:
                    pending_buy = True
                else:
                    _open(i, same_day_price[i])
        elif position and not pending_sell:
            if highs[i] > peak_high:
                peak_high = highs[i]
            if closes[i] > peak_close:
                peak_close = closes[i]
            cur_ret = (closes[i] - entry_price) / entry_price * 100
            held = i - entry_i
            reason = ""
            if take_profit is not None and cur_ret >= take_profit and "tp" not in executed_rules:
                reason = "익절"
            elif stop_loss is not None and cur_ret <= stop_loss and "sl" not in executed_rules:
                reason = "손절"
            elif (trail_atr_mult is not None and atr_arr is not None
                  and not np.isnan(atr_arr[i])
                  and closes[i] <= peak_high - trail_atr_mult * atr_arr[i]
                  and "atr" not in executed_rules):
                reason = "ATR트레일링"
            elif (trail_pct is not None
                  and closes[i] <= peak_close * (1 - trail_pct / 100)
                  and "trail" not in executed_rules):
                reason = "트레일링스톱"
            elif hold_days is not None and held >= hold_days and "hold" not in executed_rules:
                reason = "보유기간"
            elif sell_arr is not None and sell_arr[i]:
                reason = "매도신호"
            if reason:
                if defer:
                    pending_sell = True
                    pending_reason = reason
                else:
                    sell_pct = _sell_pct_of(reason)
                    _close(i, same_day_price[i], reason, sell_pct)
                    key = _reason_key(reason)
                    if key is not None and position:
                        executed_rules.add(key)

        equity[i] = cash + shares * closes[i]

    if position:
        _close(n - 1, closes[n - 1], "기간종료")
        equity[n - 1] = cash

    equity_s = pd.Series(equity, index=dates, name="전략")
    benchmark_s = pd.Series(closes / closes[0] * initial_capital, index=dates, name="Buy&Hold")
    trades_df = pd.DataFrame(trades)

    return {
        "success": True, "error": None,
        "equity": equity_s, "benchmark": benchmark_s, "trades": trades_df,
        "metrics": finalize_metrics(_metrics(equity_s, benchmark_s, trades_df),
                                    equity_s, benchmark_s, trades_df),
    }


# ── 다종목 포트폴리오 (이벤트 드리븐 룰, on_signal + 리스트 유니버스) ──────────

def run_portfolio_ir(
    dataset: dict[str, pd.DataFrame],
    symbols: list[str],
    buy_node: Node,
    *,
    sell_node: Node | None = None,
    hold_days: int | None = None,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    trail_atr_mult: float | None = None,
    trail_pct: float | None = None,
    sell_amount_pct: float = 100.0,
    rule_sell_pcts: dict | None = None,
    fill: str = "next_open",
    commission: float = _DEFAULT_COMMISSION,
    slippage: float = _DEFAULT_SLIPPAGE,
    sell_tax: float = _DEFAULT_SELL_TAX,
    initial_capital: float = 10_000_000.0,
    amount_pct: float = 10.0,
    amount_krw: float | None = None,
    start=None,
    end=None,
) -> dict:
    """다종목 이벤트 드리븐 백테스트 — 매수/매도 신호를 IR로 평가.

    검증된 레거시 포트폴리오 루프(공유 cash·입력순서 우선·partial 청산)를 그대로
    쓰되, 신호 마스크만 새 평가기(panel 컬럼)에서 가져온다. 종목당 예산 =
    amount_krw(고정) 또는 cash×amount_pct%. test_engine_portfolio_ir로 동치 고정.
    """
    from ..backtest import _MAX_POSITIONS_GLOBAL, _PortPosition

    syms = [s for s in symbols if s in dataset and not dataset[s].empty]
    if not syms:
        return _empty("포트폴리오 종목이 없습니다.")

    symbol_dfs: dict[str, pd.DataFrame] = {}
    for sym in syms:
        df = dataset[sym]
        if not {"Open", "Close"}.issubset(df.columns):
            return _empty(f"'{sym}'에 시가·종가 데이터가 없습니다.")
        df = df.sort_index()
        if start is not None:
            df = df[df.index >= pd.Timestamp(start)]
        if end is not None:
            df = df[df.index <= pd.Timestamp(end)]
        df = df.dropna(subset=["Open", "Close"])
        if len(df) < 2:
            return _empty(f"'{sym}' 백테스트 기간의 가격 데이터가 부족합니다.")
        symbol_dfs[sym] = df
    if trail_atr_mult is not None:
        for sym, df in symbol_dfs.items():
            if "atr_14" not in df.columns:
                return _empty(f"'{sym}'에 ATR 지표가 없어 ATR 트레일링을 쓸 수 없습니다.")
    if fill == "typical":
        for sym, df in symbol_dfs.items():
            if not {"High", "Low"}.issubset(df.columns):
                return _empty(f"'{sym}'에 고가·저가가 없어 typical 체결을 쓸 수 없습니다.")

    master_idx = pd.DatetimeIndex(
        sorted(set().union(*(df.index for df in symbol_dfs.values()))))
    n = len(master_idx)
    if n < 2:
        return _empty("백테스트 기간의 가격 데이터가 부족합니다.")

    ctx = EvalContext.from_dataset(dataset, universe=syms)
    buy_panel = evaluate(buy_node, ctx)
    if not isinstance(buy_panel, pd.DataFrame):
        return _empty("매수 신호가 패널을 산출하지 않습니다.")
    sell_panel = evaluate(sell_node, ctx) if sell_node is not None else None

    aligned: dict[str, dict] = {}
    buy_arrs: dict[str, np.ndarray] = {}
    sell_arrs: dict[str, np.ndarray | None] = {}
    for sym in syms:
        df = symbol_dfs[sym].reindex(master_idx)
        cl = df["Close"].to_numpy(dtype=float)
        hi = (df["High"].to_numpy(dtype=float) if "High" in df.columns else cl)
        lo = (df["Low"].to_numpy(dtype=float) if "Low" in df.columns else cl)
        aligned[sym] = {
            "open": df["Open"].to_numpy(dtype=float),
            "close": cl, "high": hi, "low": lo,
            # 당일 체결가: typical=(고+저+종)/3, 그 외=종가. next_open은 익일 시가 사용.
            "exec": ((hi + lo + cl) / 3.0) if fill == "typical" else cl,
            "atr": (df["atr_14"].to_numpy(dtype=float)
                    if "atr_14" in df.columns else None),
            "currency": "KRW" if sym.isdigit() else "USD",
        }
        bm = (buy_panel[sym] if sym in buy_panel.columns
              else pd.Series(False, index=master_idx))
        buy_arrs[sym] = bm.reindex(master_idx, fill_value=False).fillna(False).to_numpy(dtype=bool)
        if sell_panel is not None and sym in sell_panel.columns:
            sell_arrs[sym] = (sell_panel[sym].reindex(master_idx, fill_value=False)
                              .fillna(False).to_numpy(dtype=bool))
        else:
            sell_arrs[sym] = None

    defer = (fill == "next_open")
    cash = float(initial_capital)
    rule_pcts = rule_sell_pcts or {}
    positions: dict[str, _PortPosition] = {}
    pending_buys: list[str] = []
    pending_sells: dict[str, str] = {}
    trades: list[dict] = []
    equity = np.empty(n, dtype=float)
    last_valid_close: dict[str, float] = {sym: 0.0 for sym in syms}

    def _sell_pct_of(reason: str) -> float:
        if not reason:
            return float(sell_amount_pct)
        for needle, key in _REASON_KEYS:
            if needle in reason:
                v = rule_pcts.get(key)
                return float(v) if v is not None else float(sell_amount_pct)
        return float(sell_amount_pct)

    def _reason_key(reason: str) -> str | None:
        for needle, key in _REASON_KEYS:
            if needle in reason:
                return key
        return None

    def _budget(cash_snapshot: float) -> float:
        return float(amount_krw) if amount_krw else cash_snapshot * (amount_pct / 100.0)

    def _open(sym: str, i: int, raw_price: float, budget: float):
        nonlocal cash
        if np.isnan(raw_price) or raw_price <= 0 or budget <= 0:
            return
        slipped = raw_price * (1 + slippage)
        price = round_to_tick(slipped, direction="up", currency=aligned[sym]["currency"])
        if price <= 0:
            return
        per_share_cost = price * (1 + commission)
        new_shares = min(int(budget // per_share_cost), int(cash // per_share_cost))
        if new_shares <= 0:
            return
        cash -= new_shares * per_share_cost
        ph, pc = aligned[sym]["high"][i], aligned[sym]["close"][i]
        positions[sym] = _PortPosition(
            shares=float(new_shares), entry_price=price, entry_i=i,
            peak_high=price if np.isnan(ph) else float(ph),
            peak_close=price if np.isnan(pc) else float(pc), executed_rules=set())

    def _close(sym: str, i: int, raw_price: float, reason: str, sell_pct: float = 100.0):
        nonlocal cash
        pos = positions.get(sym)
        if pos is None or pos.shares <= 0:
            return
        sell_pct = max(0.0, min(100.0, float(sell_pct)))
        if sell_pct <= 0 or np.isnan(raw_price) or raw_price <= 0:
            return
        slipped = raw_price * (1 - slippage)
        price = round_to_tick(slipped, direction="down", currency=aligned[sym]["currency"])
        if price <= 0:
            return
        sell_shares = min(float(int(pos.shares * sell_pct / 100.0)), pos.shares)
        if sell_shares <= 0:
            return
        proceeds = sell_shares * price * (1 - commission - sell_tax)
        cost = sell_shares * pos.entry_price * (1 + commission)
        is_partial = sell_shares < pos.shares - 1e-9
        trades.append({
            "종목": sym, "진입일": master_idx[pos.entry_i], "청산일": master_idx[i],
            "보유일": i - pos.entry_i, "진입가": pos.entry_price, "청산가": price,
            "수익률(%)": (proceeds - cost) / cost * 100,
            "청산사유": reason + (f"({sell_pct:.0f}%)" if is_partial else "")})
        cash += proceeds
        pos.shares -= sell_shares
        if pos.shares <= 1e-9:
            del positions[sym]

    for i in range(n):
        closed_this_step: set[str] = set()
        if defer:
            for sym, reason in list(pending_sells.items()):
                _close(sym, i, aligned[sym]["open"][i], reason, _sell_pct_of(reason))
                key = _reason_key(reason)
                if key is not None and sym in positions:
                    positions[sym].executed_rules.add(key)
            pending_sells.clear()
            cash_snapshot = cash
            for sym in pending_buys:
                if len(positions) >= _MAX_POSITIONS_GLOBAL or sym in positions:
                    continue
                _open(sym, i, aligned[sym]["open"][i], min(_budget(cash_snapshot), cash))
            pending_buys.clear()

        for sym in list(positions.keys()):
            pos = positions[sym]
            close = aligned[sym]["close"][i]
            if np.isnan(close):
                continue
            high = aligned[sym]["high"][i]
            atr_arr = aligned[sym]["atr"]
            atr_v = atr_arr[i] if atr_arr is not None else np.nan
            if not np.isnan(high) and high > pos.peak_high:
                pos.peak_high = float(high)
            if close > pos.peak_close:
                pos.peak_close = float(close)
            cur_ret = (close - pos.entry_price) / pos.entry_price * 100
            held = i - pos.entry_i
            sym_sell = sell_arrs[sym]
            reason = ""
            if take_profit is not None and cur_ret >= take_profit and "tp" not in pos.executed_rules:
                reason = "익절"
            elif stop_loss is not None and cur_ret <= stop_loss and "sl" not in pos.executed_rules:
                reason = "손절"
            elif (trail_atr_mult is not None and atr_arr is not None and not np.isnan(atr_v)
                  and close <= pos.peak_high - trail_atr_mult * atr_v
                  and "atr" not in pos.executed_rules):
                reason = "ATR트레일링"
            elif (trail_pct is not None and close <= pos.peak_close * (1 - trail_pct / 100)
                  and "trail" not in pos.executed_rules):
                reason = "트레일링스톱"
            elif hold_days is not None and held >= hold_days and "hold" not in pos.executed_rules:
                reason = "보유기간"
            elif sym_sell is not None and sym_sell[i]:
                reason = "매도신호"
            if reason:
                if defer:
                    pending_sells[sym] = reason
                else:
                    _close(sym, i, aligned[sym]["exec"][i], reason, _sell_pct_of(reason))
                    if sym not in positions:
                        closed_this_step.add(sym)
                    else:
                        key = _reason_key(reason)
                        if key is not None:
                            positions[sym].executed_rules.add(key)

        if defer:
            for sym in syms:
                if sym in positions or sym in pending_buys:
                    continue
                if buy_arrs[sym][i]:
                    pending_buys.append(sym)
        else:
            cash_snapshot = cash
            for sym in syms:
                if sym in positions or sym in closed_this_step:
                    continue
                if len(positions) >= _MAX_POSITIONS_GLOBAL:
                    break
                if not buy_arrs[sym][i]:
                    continue
                if np.isnan(aligned[sym]["close"][i]):
                    continue
                _open(sym, i, aligned[sym]["exec"][i], min(_budget(cash_snapshot), cash))

        nav = cash
        for sym, pos in positions.items():
            cl = aligned[sym]["close"][i]
            if not np.isnan(cl):
                last_valid_close[sym] = float(cl)
                nav += pos.shares * cl
            elif last_valid_close[sym] > 0:
                nav += pos.shares * last_valid_close[sym]
            else:
                nav += pos.shares * pos.entry_price
        equity[i] = nav

    for sym in list(positions.keys()):
        cl_arr = aligned[sym]["close"]
        final_close = np.nan
        for j in range(n - 1, -1, -1):
            if not np.isnan(cl_arr[j]):
                final_close = float(cl_arr[j])
                break
        if np.isnan(final_close):
            cash += positions[sym].shares * positions[sym].entry_price
            del positions[sym]
        else:
            _close(sym, n - 1, final_close, "기간종료", 100.0)
    equity[n - 1] = cash

    bench = np.zeros(n, dtype=float)
    weight = initial_capital / len(syms)
    for sym in syms:
        cl_series = pd.Series(aligned[sym]["close"]).ffill().bfill()
        first = cl_series.iloc[0] if not cl_series.empty else 0
        if first and first > 0:
            bench += (cl_series / first * weight).to_numpy()

    equity_s = pd.Series(equity, index=master_idx, name="전략")
    benchmark_s = pd.Series(bench, index=master_idx, name="Buy&Hold")
    trades_df = pd.DataFrame(trades)
    return {"success": True, "error": None, "equity": equity_s,
            "benchmark": benchmark_s, "trades": trades_df,
            "metrics": finalize_metrics(_metrics(equity_s, benchmark_s, trades_df),
                                        equity_s, benchmark_s, trades_df)}

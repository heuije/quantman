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

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .analysis import build_signal_mask
from .exec_defaults import DEFAULT_EXECUTION, round_to_tick

TRADING_DAYS = 252

# CM-01 — 백테스트 비용 default는 ExecutionPolicy 단일 출처에서 끌어온다.
# 이전엔 backtest.py가 자체 default(commission 0.00015 / slippage 0.0005)를 들고
# exec_defaults는 25 bps / 10 bps로 별개여서 호출 경로마다 다른 값이 적용됐다.
_DEFAULT_COMMISSION = DEFAULT_EXECUTION["bt_commission_bps"] / 10_000.0
_DEFAULT_SLIPPAGE = DEFAULT_EXECUTION["bt_slippage_bps"] / 10_000.0
_DEFAULT_SELL_TAX = DEFAULT_EXECUTION["bt_sell_tax_bps"] / 10_000.0


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
    # Phase 56 — 매도 룰별 sell_pct (TP partial 등). rule_sell_pcts={"tp":50.0,"sl":100.0,...}
    # sell_amount_pct: 매도신호(자유 매도조건) 및 미지정 룰의 fallback %.
    sell_amount_pct: float = 100.0,
    rule_sell_pcts: dict | None = None,
    fill: str = "next_open",
    commission: float = _DEFAULT_COMMISSION,
    slippage: float = _DEFAULT_SLIPPAGE,
    sell_tax: float = _DEFAULT_SELL_TAX,            # C-01: 매도 단방향(거래세)
    currency: str = "KRW",                            # C-03: 틱 라운딩 통화
    initial_capital: float = 10_000_000.0,
    # Phase 57 — 다종목 portfolio 모드에서만 사용. 1종목 single path는 cash 전액 투입(기존 동작) 유지.
    amount_pct: float = 100.0,
    # Phase 57-B — 자동선택(screener) 백테스트 인자. trade_symbol이 "screener:..."일 때만 사용.
    screener_spec: dict | None = None,
    screener_limit: int = 20,
    rebalance_mode: str = "hold",
    rebalance_period: str = "weekly",
    rebalance_every_n_days: int | None = None,
    start=None,
    end=None,
    gap_extra_cost: bool = False,
    gap_threshold_pct: float = 1.0,
) -> dict:
    """매매 전략을 과거 데이터로 시뮬레이션한다. 결과 dict를 반환한다.

    Phase 57 — trade_symbol이 콤마로 여러 종목이면 portfolio 백테스트로 라우팅한다.
    Phase 57-B — trade_symbol이 'screener:...'면 screener 동적 후보 백테스트로 라우팅.
    단일 종목 케이스는 기존 single-symbol path 그대로 (호환성 게이트: 기존 golden 보존).
    """
    if not buy_conditions:
        return _empty("매수 조건을 1개 이상 설정하세요.")
    # Phase 57-B — screener 모드 라우팅 (수동/콤마 분기 전에 평가).
    if (trade_symbol or "").startswith("screener:"):
        return _run_screener_backtest(
            data=data,
            screener_spec=screener_spec or {},
            screener_limit=int(screener_limit),
            rb_mode=rebalance_mode, rb_period=rebalance_period,
            rb_every_n_days=rebalance_every_n_days,
            buy_conditions=buy_conditions, buy_logic=buy_logic,
            hold_days=hold_days, take_profit=take_profit, stop_loss=stop_loss,
            trail_atr_mult=trail_atr_mult, trail_pct=trail_pct,
            sell_conditions=sell_conditions, sell_logic=sell_logic,
            sell_amount_pct=sell_amount_pct, rule_sell_pcts=rule_sell_pcts,
            fill=fill, commission=commission, slippage=slippage,
            sell_tax=sell_tax, currency=currency,
            initial_capital=initial_capital, amount_pct=amount_pct,
            start=start, end=end,
            gap_extra_cost=gap_extra_cost, gap_threshold_pct=gap_threshold_pct,
        )
    # Phase 57 — 다종목 라우팅. 1종목은 그대로 single path.
    symbols_list = [s.strip() for s in (trade_symbol or "").split(",") if s.strip()]
    if not symbols_list:
        return _empty("매수 후보 종목이 없습니다.")
    if len(symbols_list) >= 2:
        return _run_portfolio_backtest(
            data=data, symbols=symbols_list,
            buy_conditions=buy_conditions, buy_logic=buy_logic,
            hold_days=hold_days, take_profit=take_profit, stop_loss=stop_loss,
            trail_atr_mult=trail_atr_mult, trail_pct=trail_pct,
            sell_conditions=sell_conditions, sell_logic=sell_logic,
            sell_amount_pct=sell_amount_pct, rule_sell_pcts=rule_sell_pcts,
            fill=fill, commission=commission, slippage=slippage,
            sell_tax=sell_tax, currency=currency,
            initial_capital=initial_capital, amount_pct=amount_pct,
            start=start, end=end,
            gap_extra_cost=gap_extra_cost, gap_threshold_pct=gap_threshold_pct,
        )
    trade_symbol = symbols_list[0]   # whitespace strip 후 단일 종목
    if trade_symbol not in data or data[trade_symbol].empty:
        return _empty(f"'{trade_symbol}' 데이터 없음")

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

    # Phase 41 — 백테스트는 단일 종목 시뮬레이션이므로 [이 종목] placeholder는
    # 모두 trade_symbol로 치환되어 평가된다.
    buy_mask = build_signal_mask(data, buy_conditions, buy_logic,
                                  current_symbol=trade_symbol)
    if buy_mask.empty:
        return _empty("매수 조건의 종목·지표를 확인하세요.")
    buy_arr = buy_mask.reindex(trade_df.index, fill_value=False).to_numpy(dtype=bool)

    if sell_conditions:
        sell_mask = build_signal_mask(data, sell_conditions, sell_logic,
                                       current_symbol=trade_symbol)
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
    # Phase 56 — partial close once-per-trade: 같은 룰 재 trigger 방지.
    # entry 시 reset, partial 매도된 룰은 추후 trigger 평가에서 skip.
    executed_rules: set[str] = set()
    rule_pcts = rule_sell_pcts or {}

    # reason → rule key 매핑 (strategy.py와 동일 패턴, 모듈 결합도 회피 위해 inline).
    _REASON_KEYS = (
        ("익절", "tp"), ("손절", "sl"), ("ATR트레일링", "atr"),
        ("트레일링", "trail"), ("보유기간", "hold"),
    )

    def _sell_pct_of(reason: str) -> float:
        if not reason:
            return float(sell_amount_pct)
        for needle, key in _REASON_KEYS:
            if needle in reason:
                v = rule_pcts.get(key)
                return float(v) if v is not None else float(sell_amount_pct)
        return float(sell_amount_pct)   # 매도신호·기간종료 등

    def _reason_key(reason: str) -> str | None:
        for needle, key in _REASON_KEYS:
            if needle in reason:
                return key
        return None

    trades: list[dict] = []
    equity = np.empty(n, dtype=float)

    def _gap_extra(i: int) -> float:
        """전일 종가 → 당일 시가 갭이 임계값 초과 시 추가 슬리피지 (편도)."""
        if not gap_extra_cost or i == 0:
            return 0.0
        prev_close = closes[i - 1]
        if prev_close <= 0:
            return 0.0
        gap_pct = abs(opens[i] - prev_close) / prev_close * 100
        if gap_pct <= gap_threshold_pct:
            return 0.0
        # 임계값 초과분의 절반을 추가 비용으로 계상
        return (gap_pct - gap_threshold_pct) / 100 * 0.5

    def _open(i: int, raw_price: float):
        nonlocal cash, shares, position, entry_price, entry_i
        nonlocal peak_high, peak_close
        extra = _gap_extra(i) if next_open else 0.0
        # C-03 — 슬리피지 적용 후 호가단위로 라운딩(매수는 up = 매수자 불리).
        slipped = raw_price * (1 + slippage + extra)
        price = round_to_tick(slipped, direction="up", currency=currency)
        if price <= 0:
            return
        # C-04 — 정수주 강제. 잔여 현금은 cash로 유지.
        per_share_cost = price * (1 + commission)
        new_shares = int(cash // per_share_cost)
        if new_shares <= 0:
            return
        shares = float(new_shares)
        entry_shares = shares
        cash -= shares * per_share_cost
        entry_price = price
        entry_i = i
        peak_high = highs[i]
        peak_close = closes[i]
        position = True
        executed_rules.clear()   # 새 trade 시작 — 룰 trigger 이력 초기화

    def _close(i: int, raw_price: float, reason: str, sell_pct: float = 100.0):
        """Phase 56 — sell_pct < 100이면 partial close. shares 일부만 처분, position 유지.
        잔여 ~0이면 position 종료. trades 행에는 partial 여부 기록."""
        nonlocal cash, shares, position
        if shares <= 0:
            return
        sell_pct = max(0.0, min(100.0, float(sell_pct)))
        if sell_pct <= 0:
            return
        extra = _gap_extra(i) if next_open else 0.0
        # C-03 — 슬리피지 적용 후 호가단위로 라운딩(매도는 down = 매도자 불리).
        slipped = raw_price * (1 - slippage - extra)
        price = round_to_tick(slipped, direction="down", currency=currency)
        if price <= 0:
            # 가격 산정 불가 — 전량 강제 종료 (safety, 기존 동작 유지)
            shares = 0.0
            position = False
            return
        # 매도할 주수 — partial이면 floor (1주 미만 매수 불가 = 정수 단위)
        sell_shares = float(int(shares * sell_pct / 100.0))
        if sell_shares <= 0:
            return                       # 너무 작은 % → 매도 1주도 안 됨, skip
        sell_shares = min(sell_shares, shares)
        # C-01 — 매도세는 매도 단방향. 매수에 세금이 붙던 이전 모델은 비대칭 현실
        # (한국 시장)을 비대칭 모델로 표현한다.
        proceeds = sell_shares * price * (1 - commission - sell_tax)
        cost = sell_shares * entry_price * (1 + commission)
        is_partial = sell_shares < shares - 1e-9
        trades.append({
            "진입일": dates[entry_i],
            "청산일": dates[i],
            "보유일": i - entry_i,
            "진입가": entry_price,
            "청산가": price,
            "수익률(%)": (proceeds - cost) / cost * 100,
            "청산사유": reason + (f"({sell_pct:.0f}%)" if is_partial else ""),
        })
        cash += proceeds
        shares -= sell_shares
        if shares <= 1e-9:
            shares = 0.0
            position = False
            executed_rules.clear()       # trade 종료 — 다음 진입을 위해 reset

    for i in range(n):
        # 1) 익일 시가 체결: 전일 신호를 오늘 시가에 실행
        if next_open:
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
            # Phase 56 — once-per-trade: 이미 partial 매도된 룰은 같은 trade에서 skip.
            # 잔여분은 다른 룰(SL·trail·hold·신호) 또는 기간 종료로 청산.
            if take_profit is not None and cur_ret >= take_profit \
                    and "tp" not in executed_rules:
                reason = "익절"
            elif stop_loss is not None and cur_ret <= stop_loss \
                    and "sl" not in executed_rules:
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
            elif hold_days is not None and held >= hold_days \
                    and "hold" not in executed_rules:
                reason = "보유기간"
            elif sell_arr is not None and sell_arr[i]:
                reason = "매도신호"   # 매도신호는 cumulative (한 trade에 여러 번 가능)
            if reason:
                if next_open:
                    pending_sell = True
                    pending_reason = reason
                else:
                    sell_pct = _sell_pct_of(reason)
                    _close(i, closes[i], reason, sell_pct)
                    key = _reason_key(reason)
                    if key is not None and position:   # partial 후 position 유지된 경우만
                        executed_rules.add(key)

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


# ---------------------------------------------------------------------------
# Phase 57 — Portfolio (다종목) 백테스트
# ---------------------------------------------------------------------------

# 동시 보유 전역 한도 (frontend `screener_limit` cap·`max_concurrent` 제거 정책과 정합).
_MAX_POSITIONS_GLOBAL = 30


@dataclass
class _PortPosition:
    shares: float
    entry_price: float
    entry_i: int
    peak_high: float
    peak_close: float
    executed_rules: set = field(default_factory=set)


def _run_portfolio_backtest(
    data: dict[str, pd.DataFrame],
    symbols: list[str],
    buy_conditions: list[dict],
    buy_logic: str = "AND",
    hold_days: int | None = None,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    trail_atr_mult: float | None = None,
    trail_pct: float | None = None,
    sell_conditions: list[dict] | None = None,
    sell_logic: str = "AND",
    sell_amount_pct: float = 100.0,
    rule_sell_pcts: dict | None = None,
    fill: str = "next_open",
    commission: float = _DEFAULT_COMMISSION,
    slippage: float = _DEFAULT_SLIPPAGE,
    sell_tax: float = _DEFAULT_SELL_TAX,
    currency: str = "KRW",
    initial_capital: float = 10_000_000.0,
    amount_pct: float = 10.0,
    start=None,
    end=None,
    gap_extra_cost: bool = False,
    gap_threshold_pct: float = 1.0,
) -> dict:
    """다종목 portfolio 백테스트.

    설계(Phase 57 합의):
      - 단일 cash 풀 + 종목별 Position dict
      - 매수 신호 동시 발생 시 i일 시작 cash 기준 amount_pct% × trade_symbol 입력 순서
      - 자본·동시보유한도 부족 시 우선순위대로 가능한 것까지 (skip)
      - 매도 룰별 sell_pct (partial close) — single path와 동일 정책
      - 매도 → 매수 순으로 일별 평가 (매도 cash가 같은 날 매수에 사용 가능)
      - benchmark = equal-weight buy-and-hold
    """
    # 1) 종목별 가격 데이터 검증·필터
    symbol_dfs: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        if sym not in data or data[sym].empty:
            return _empty(f"'{sym}' 데이터 없음")
        df = data[sym]
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

    # 2) ATR trailing 사용 시 모든 종목에 atr_14 컬럼 필요
    if trail_atr_mult is not None:
        for sym, df in symbol_dfs.items():
            if "atr_14" not in df.columns:
                return _empty(
                    f"'{sym}'에 ATR 지표가 없어 ATR 트레일링 스톱을 쓸 수 없습니다.")

    # 3) 마스터 타임라인 = 종목별 index union (정렬). 종목 거래일이 다르면 NaN 로 채워짐.
    master_idx = pd.DatetimeIndex(
        sorted(set().union(*(df.index for df in symbol_dfs.values()))))
    n = len(master_idx)
    if n < 2:
        return _empty("백테스트 기간의 가격 데이터가 부족합니다.")

    # 4) 종목별 시계열 reindex (NaN 허용). 종목별 currency는 코드 isdigit으로 추정.
    aligned: dict[str, dict] = {}
    for sym in symbols:
        df = symbol_dfs[sym].reindex(master_idx)
        aligned[sym] = {
            "open": df["Open"].to_numpy(dtype=float),
            "close": df["Close"].to_numpy(dtype=float),
            "high": (df["High"].to_numpy(dtype=float)
                     if "High" in df.columns
                     else df["Close"].to_numpy(dtype=float)),
            "atr": (df["atr_14"].to_numpy(dtype=float)
                    if "atr_14" in df.columns else None),
            "currency": "KRW" if sym.isdigit() else "USD",
        }

    # 5) 종목별 매수/매도 mask
    buy_arrs: dict[str, np.ndarray] = {}
    sell_arrs: dict[str, np.ndarray | None] = {}
    for sym in symbols:
        bm = build_signal_mask(data, buy_conditions, buy_logic,
                                current_symbol=sym)
        if bm.empty:
            return _empty("매수 조건의 종목·지표를 확인하세요.")
        buy_arrs[sym] = bm.reindex(master_idx, fill_value=False).to_numpy(dtype=bool)
        if sell_conditions:
            sm = build_signal_mask(data, sell_conditions, sell_logic,
                                    current_symbol=sym)
            if sm.empty:
                return _empty("매도 조건의 종목·지표를 확인하세요.")
            sell_arrs[sym] = sm.reindex(master_idx, fill_value=False).to_numpy(dtype=bool)
        else:
            sell_arrs[sym] = None

    # 6) State
    next_open = (fill == "next_open")
    cash = float(initial_capital)
    rule_pcts = rule_sell_pcts or {}
    positions: dict[str, _PortPosition] = {}
    pending_buys: list[str] = []          # 입력 순서 보존
    pending_sells: dict[str, str] = {}    # sym → reason
    trades: list[dict] = []
    equity = np.empty(n, dtype=float)
    last_valid_close: dict[str, float] = {sym: 0.0 for sym in symbols}

    _REASON_KEYS = (
        ("익절", "tp"), ("손절", "sl"), ("ATR트레일링", "atr"),
        ("트레일링", "trail"), ("보유기간", "hold"),
    )

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

    def _gap_extra(sym: str, i: int) -> float:
        if not gap_extra_cost or i == 0:
            return 0.0
        prev_close = aligned[sym]["close"][i - 1]
        cur_open = aligned[sym]["open"][i]
        if np.isnan(prev_close) or np.isnan(cur_open) or prev_close <= 0:
            return 0.0
        gap_pct = abs(cur_open - prev_close) / prev_close * 100
        if gap_pct <= gap_threshold_pct:
            return 0.0
        return (gap_pct - gap_threshold_pct) / 100 * 0.5

    def _open(sym: str, i: int, raw_price: float, budget: float):
        """portfolio 매수. `budget`=이번 매수에 쓸 수 있는 최대 금액."""
        nonlocal cash
        if np.isnan(raw_price) or raw_price <= 0 or budget <= 0:
            return
        extra = _gap_extra(sym, i) if next_open else 0.0
        slipped = raw_price * (1 + slippage + extra)
        price = round_to_tick(slipped, direction="up",
                              currency=aligned[sym]["currency"])
        if price <= 0:
            return
        per_share_cost = price * (1 + commission)
        max_by_budget = int(budget // per_share_cost)
        max_by_cash = int(cash // per_share_cost)
        new_shares = min(max_by_budget, max_by_cash)
        if new_shares <= 0:
            return
        cost = new_shares * per_share_cost
        cash -= cost
        peak_h = aligned[sym]["high"][i]
        peak_c = aligned[sym]["close"][i]
        positions[sym] = _PortPosition(
            shares=float(new_shares),
            entry_price=price,
            entry_i=i,
            peak_high=price if np.isnan(peak_h) else float(peak_h),
            peak_close=price if np.isnan(peak_c) else float(peak_c),
            executed_rules=set(),
        )

    def _close(sym: str, i: int, raw_price: float, reason: str,
                sell_pct: float = 100.0):
        nonlocal cash
        pos = positions.get(sym)
        if pos is None or pos.shares <= 0:
            return
        sell_pct = max(0.0, min(100.0, float(sell_pct)))
        if sell_pct <= 0:
            return
        if np.isnan(raw_price) or raw_price <= 0:
            return   # 가격 산정 불가 — skip (기간종료는 별도 final_close 사용)
        extra = _gap_extra(sym, i) if next_open else 0.0
        slipped = raw_price * (1 - slippage - extra)
        price = round_to_tick(slipped, direction="down",
                              currency=aligned[sym]["currency"])
        if price <= 0:
            return
        sell_shares = float(int(pos.shares * sell_pct / 100.0))
        if sell_shares <= 0:
            return
        sell_shares = min(sell_shares, pos.shares)
        proceeds = sell_shares * price * (1 - commission - sell_tax)
        cost = sell_shares * pos.entry_price * (1 + commission)
        is_partial = sell_shares < pos.shares - 1e-9
        trades.append({
            "종목": sym,
            "진입일": master_idx[pos.entry_i],
            "청산일": master_idx[i],
            "보유일": i - pos.entry_i,
            "진입가": pos.entry_price,
            "청산가": price,
            "수익률(%)": (proceeds - cost) / cost * 100,
            "청산사유": reason + (f"({sell_pct:.0f}%)" if is_partial else ""),
        })
        cash += proceeds
        pos.shares -= sell_shares
        if pos.shares <= 1e-9:
            del positions[sym]

    # 7) 시뮬레이션 루프
    for i in range(n):
        # close 체결 모델에서 same-day 매도→재매수 방지 (single path 정책과 정합).
        closed_this_step: set[str] = set()
        # 7-1) next_open pending 처리 (매도 → 매수: cash 확보 후 신규)
        if next_open:
            for sym, reason in list(pending_sells.items()):
                op = aligned[sym]["open"][i]
                _close(sym, i, op, reason, _sell_pct_of(reason))
                key = _reason_key(reason)
                if key is not None and sym in positions:
                    positions[sym].executed_rules.add(key)
            pending_sells.clear()
            cash_snapshot = cash    # 동시 매수 fairness — 같은 cash 기준 amount_pct%
            for sym in pending_buys:
                if len(positions) >= _MAX_POSITIONS_GLOBAL:
                    break
                if sym in positions:
                    continue
                op = aligned[sym]["open"][i]
                desired = cash_snapshot * (amount_pct / 100.0)
                _open(sym, i, op, min(desired, cash))
            pending_buys.clear()

        # 7-2) 종가 기준 매도 신호 평가 (보유 종목)
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
            if (take_profit is not None and cur_ret >= take_profit
                    and "tp" not in pos.executed_rules):
                reason = "익절"
            elif (stop_loss is not None and cur_ret <= stop_loss
                    and "sl" not in pos.executed_rules):
                reason = "손절"
            elif (trail_atr_mult is not None and atr_arr is not None
                    and not np.isnan(atr_v)
                    and close <= pos.peak_high - trail_atr_mult * atr_v
                    and "atr" not in pos.executed_rules):
                reason = "ATR트레일링"
            elif (trail_pct is not None
                    and close <= pos.peak_close * (1 - trail_pct / 100)
                    and "trail" not in pos.executed_rules):
                reason = "트레일링스톱"
            elif (hold_days is not None and held >= hold_days
                    and "hold" not in pos.executed_rules):
                reason = "보유기간"
            elif sym_sell is not None and sym_sell[i]:
                reason = "매도신호"
            if reason:
                if next_open:
                    pending_sells[sym] = reason
                else:
                    _close(sym, i, close, reason, _sell_pct_of(reason))
                    if sym not in positions:
                        closed_this_step.add(sym)   # full close → 같은 날 재매수 차단
                    else:
                        key = _reason_key(reason)
                        if key is not None:
                            positions[sym].executed_rules.add(key)

        # 7-3) 매수 신호 평가 (미보유 종목, 입력 순서 우선순위)
        if next_open:
            for sym in symbols:
                if sym in positions or sym in pending_buys:
                    continue
                if buy_arrs[sym][i]:
                    pending_buys.append(sym)
        else:
            # close 체결 모델: i일 종가에 즉시 매수. 동시 신호 시 입력 순서·cash_snapshot fairness.
            cash_snapshot = cash
            for sym in symbols:
                if sym in positions or sym in closed_this_step:
                    continue
                if len(positions) >= _MAX_POSITIONS_GLOBAL:
                    break
                if not buy_arrs[sym][i]:
                    continue
                cl = aligned[sym]["close"][i]
                if np.isnan(cl):
                    continue
                desired = cash_snapshot * (amount_pct / 100.0)
                _open(sym, i, cl, min(desired, cash))

        # 7-4) NAV 기록 — NaN close는 마지막 valid close로 평가
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

    # 8) 기간 종료 — 미청산 포지션 강제 청산 (마지막 valid close 사용)
    for sym in list(positions.keys()):
        cl_arr = aligned[sym]["close"]
        final_close = np.nan
        for j in range(n - 1, -1, -1):
            if not np.isnan(cl_arr[j]):
                final_close = float(cl_arr[j])
                break
        if np.isnan(final_close):
            # 종목 가격 데이터가 모두 NaN — 발생 거의 없지만 안전: entry_price로 환원.
            pos = positions[sym]
            cash += pos.shares * pos.entry_price
            del positions[sym]
        else:
            _close(sym, n - 1, final_close, "기간종료", 100.0)
    equity[n - 1] = cash

    # 9) Benchmark — equal-weight buy-and-hold portfolio.
    #    종목별 cl/첫valid_cl × 비중자본을 합산. 거래일 다른 종목은 ffill로 메움.
    bench = np.zeros(n, dtype=float)
    weight = initial_capital / len(symbols)
    for sym in symbols:
        cl_series = pd.Series(aligned[sym]["close"]).ffill().bfill()
        if cl_series.empty:
            continue
        first = cl_series.iloc[0]
        if not first or first <= 0:
            continue
        bench += (cl_series / first * weight).to_numpy()

    equity_s = pd.Series(equity, index=master_idx, name="전략")
    benchmark_s = pd.Series(bench, index=master_idx, name="Buy&Hold")
    trades_df = pd.DataFrame(trades)

    return {
        "success": True,
        "error": None,
        "equity": equity_s,
        "benchmark": benchmark_s,
        "trades": trades_df,
        "metrics": _metrics(equity_s, benchmark_s, trades_df),
    }


# ---------------------------------------------------------------------------
# Phase 57-B — Screener(자동선택) 백테스트
# ---------------------------------------------------------------------------

# Screener V1 field → dataset 컬럼 매핑. 백테스트에서 historical로 평가 가능한 항목만.
_SCREENER_FIELD_TO_DATASET = {
    "close": "Close", "open": "Open", "high": "High", "low": "Low", "volume": "Volume",
    "pct_change_1d": "pct_change_1d", "pct_change_5d": "pct_change_5d",
    "pct_change_20d": "pct_change_20d", "pct_change_252d": "pct_change_252d",
    "log_return_1d": "log_return_1d",
    "rsi_14": "rsi_14", "atr_14": "atr_14", "atr_14_pct": "atr_14_pct",
    "bb_width": "bb_width", "bb_pct": "bb_pct",
    "ma_dev_20d": "ma_dev_20d", "ma_dev_60d": "ma_dev_60d",
    "ma_dev_200d": "ma_dev_200d", "ma_gap_20_60": "ma_gap_20_60",
    "momentum_12_1m": "momentum_12_1m",
    # screener V1 명세는 volume_ratio_20d, dataset은 volume_ratio (20일 기준). 같은 의미.
    "volume_ratio_20d": "volume_ratio",
    "high_dev_20d": "high_dev_20d", "streak": "streak",
}

# 펀더멘털·시총·52주 — historical 데이터 부재로 백테스트 불가. 사용 시 명시 에러.
_SCREENER_UNSUPPORTED_FIELDS = frozenset({
    "market_cap", "shares_listed", "change_won",
    "per", "pbr", "eps", "bps", "dps", "dividend_yield", "foreign_rate",
    "high_52w", "low_52w",
})


def _screener_value_at_date(df: pd.DataFrame, date: pd.Timestamp, field: str) -> float | None:
    """종목 raw df에서 특정 date의 screener field 값. date가 df.index에 없으면 None.

    Phase 57-B 최적화: 모든 종목을 reindex 안 하고 raw df의 .loc[date] 사용 → 메모리·시간 절약.
    """
    if date not in df.index:
        return None
    if field == "trade_value":
        cl = df.at[date, "Close"] if "Close" in df.columns else None
        vol = df.at[date, "Volume"] if "Volume" in df.columns else None
        if cl is None or vol is None or pd.isna(cl) or pd.isna(vol):
            return None
        return float(cl) * float(vol)
    col = _SCREENER_FIELD_TO_DATASET.get(field)
    if col is None or col not in df.columns:
        return None
    v = df.at[date, col]
    if pd.isna(v):
        return None
    return float(v)


def _evaluate_screener_at(
    spec: dict, pool_dfs: dict[str, pd.DataFrame], date: pd.Timestamp, pool: list[str],
) -> list[str]:
    """date 시점에 screener spec을 평가, 후보 종목을 sort+limit 후 반환.

    `pool_dfs[sym]`은 raw(reindex 전) DataFrame. .at[date, col]로 직접 조회.
    펀더멘털·52주 등 미지원 field 사용 시 ValueError.
    """
    rules = spec.get("rules") or []
    sort_obj = spec.get("sort") if isinstance(spec.get("sort"), dict) else None
    sort_field = (sort_obj or {}).get("field") or spec.get("sort_field")
    sort_order = (sort_obj or {}).get("order") or spec.get("sort_order") or "desc"
    limit = int(spec.get("limit") or 20)

    used = {r.get("field") for r in rules if isinstance(r, dict) and r.get("field")}
    if sort_field:
        used.add(sort_field)
    bad = used & _SCREENER_UNSUPPORTED_FIELDS
    if bad:
        raise ValueError(
            f"백테스트 미지원 screener 필드: {sorted(bad)}. "
            "기술지표·OHLCV·trade_value 룰만 사용 가능 "
            "(펀더멘털·시총·52주 데이터는 historical 부재).")

    matched: list[tuple[str, float]] = []
    for sym in pool:
        df = pool_dfs[sym]
        ok = True
        for r in rules:
            if not isinstance(r, dict):
                ok = False
                break
            field = r.get("field")
            op = r.get("op")
            target = r.get("value")
            v = _screener_value_at_date(df, date, field)
            if v is None:
                ok = False
                break
            if op == ">":
                ok = v > target
            elif op == ">=":
                ok = v >= target
            elif op == "<":
                ok = v < target
            elif op == "<=":
                ok = v <= target
            elif op == "between":
                if isinstance(target, list) and len(target) == 2:
                    ok = target[0] <= v <= target[1]
                else:
                    ok = False
            else:
                ok = False
            if not ok:
                break
        if not ok:
            continue
        sk = _screener_value_at_date(df, date, sort_field) if sort_field else 0.0
        matched.append((sym, sk if sk is not None else 0.0))

    matched.sort(key=lambda x: x[1], reverse=(sort_order == "desc"))
    return [s for s, _ in matched[:limit]]


def _is_rebalance_day(
    i: int, last_rb_i: int | None, master_idx: pd.DatetimeIndex,
    period: str, every_n_days: int | None,
) -> bool:
    """i가 rebalance 평가 시점인지. 첫날(last_rb_i=None)이면 항상 True."""
    if last_rb_i is None:
        return True
    if period == "daily":
        return True
    d_cur = master_idx[i]
    d_prev = master_idx[last_rb_i]
    if period == "weekly":
        iso_cur = d_cur.isocalendar()
        iso_prev = d_prev.isocalendar()
        return (iso_cur.year, iso_cur.week) != (iso_prev.year, iso_prev.week)
    if period == "monthly":
        return (d_cur.year, d_cur.month) != (d_prev.year, d_prev.month)
    if period == "every_n_days":
        if not every_n_days or every_n_days < 1:
            return False
        return (i - last_rb_i) >= int(every_n_days)
    return False


def _run_screener_backtest(
    data: dict[str, pd.DataFrame],
    screener_spec: dict,
    screener_limit: int,
    rb_mode: str,
    rb_period: str,
    rb_every_n_days: int | None,
    buy_conditions: list[dict],
    buy_logic: str = "AND",
    hold_days: int | None = None,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    trail_atr_mult: float | None = None,
    trail_pct: float | None = None,
    sell_conditions: list[dict] | None = None,
    sell_logic: str = "AND",
    sell_amount_pct: float = 100.0,
    rule_sell_pcts: dict | None = None,
    fill: str = "next_open",
    commission: float = _DEFAULT_COMMISSION,
    slippage: float = _DEFAULT_SLIPPAGE,
    sell_tax: float = _DEFAULT_SELL_TAX,
    currency: str = "KRW",
    initial_capital: float = 10_000_000.0,
    amount_pct: float = 10.0,
    start=None,
    end=None,
    gap_extra_cost: bool = False,
    gap_threshold_pct: float = 1.0,
) -> dict:
    """자동선택(screener) 백테스트.

    rebalance 주기마다 screener_spec을 historical로 평가해 candidates 풀을 갱신.
    mode:
      - "off"      : 첫 rebalance만 평가 → 후보 lock-in. 이후 buy_conditions·매도 룰만 동작.
      - "hold"     : 주기마다 평가. 보유 매도 X. 빈 슬롯만 새 후보로 채움.
      - "replace"  : 주기마다 평가. 후보에서 탈락한 보유 종목 매도 + 신규 후보 매수.
    """
    if not screener_spec:
        return _empty("자동선택 스펙이 비었습니다 (screener_spec).")
    if not buy_conditions:
        return _empty("매수 조건을 1개 이상 설정하세요.")

    # 0) Pre-flight: 펀더멘털·52주 등 미지원 field 사용 시 reindex 전에 즉시 에러 반환.
    _rules = screener_spec.get("rules") or []
    _sort_obj = screener_spec.get("sort") if isinstance(screener_spec.get("sort"), dict) else None
    _used_fields = {r.get("field") for r in _rules if isinstance(r, dict) and r.get("field")}
    _used_fields.add((_sort_obj or {}).get("field") or screener_spec.get("sort_field"))
    _bad = _used_fields & _SCREENER_UNSUPPORTED_FIELDS
    if _bad:
        return _empty(
            f"백테스트 미지원 screener 필드: {sorted(_bad)}. "
            "기술지표·OHLCV·trade_value 룰만 사용 가능 "
            "(펀더멘털·시총·52주 데이터는 historical 부재).")

    # 1) 후보 풀 = data 안의 종목 중 OHLCV·기간 유효한 것 (markets/exclude는 V1.B 무시)
    pool_dfs: dict[str, pd.DataFrame] = {}
    for sym, df in data.items():
        if df is None or df.empty:
            continue
        if not {"Open", "Close"}.issubset(df.columns):
            continue
        d = df.sort_index()
        if start is not None:
            d = d[d.index >= pd.Timestamp(start)]
        if end is not None:
            d = d[d.index <= pd.Timestamp(end)]
        d = d.dropna(subset=["Open", "Close"])
        if len(d) < 2:
            continue
        pool_dfs[sym] = d
    if not pool_dfs:
        return _empty("백테스트 기간의 가격 데이터가 부족합니다.")

    # 2) 마스터 타임라인
    master_idx = pd.DatetimeIndex(
        sorted(set().union(*(df.index for df in pool_dfs.values()))))
    n = len(master_idx)
    if n < 2:
        return _empty("백테스트 기간의 가격 데이터가 부족합니다.")

    pool: list[str] = sorted(pool_dfs.keys())

    # 3) aligned·buy/sell mask는 lazy(필요한 종목만 build + 캐시). 매수 후보가 된 적
    # 없는 종목은 reindex/mask 비용 발생 안 함 — 4445 종목 universe에서 핵심 최적화.
    aligned_cache: dict[str, dict] = {}
    buy_arr_cache: dict[str, np.ndarray] = {}
    sell_arr_cache: dict[str, np.ndarray | None] = {}

    def _aligned(sym: str) -> dict:
        if sym in aligned_cache:
            return aligned_cache[sym]
        rdf = pool_dfs[sym].reindex(master_idx)
        aligned_cache[sym] = {
            "open": rdf["Open"].to_numpy(dtype=float),
            "close": rdf["Close"].to_numpy(dtype=float),
            "high": (rdf["High"].to_numpy(dtype=float)
                     if "High" in rdf.columns else rdf["Close"].to_numpy(dtype=float)),
            "atr": (rdf["atr_14"].to_numpy(dtype=float)
                    if "atr_14" in rdf.columns else None),
            "currency": "KRW" if sym.isdigit() else "USD",
        }
        return aligned_cache[sym]

    _BUY_BAD: list[bool] = []   # 첫 빌드 실패 marker (raise 대신 _empty 반환에 쓰기 어려움 → 내부 ValueError)

    def _buy_arr(sym: str) -> np.ndarray | None:
        """매수 mask. 빌드 실패 시 None 반환. 종목별 [이 종목] placeholder 치환."""
        if sym in buy_arr_cache:
            return buy_arr_cache[sym]
        bm = build_signal_mask(data, buy_conditions, buy_logic, current_symbol=sym)
        if bm.empty:
            buy_arr_cache[sym] = None   # 다음 호출에도 None
            return None
        arr = bm.reindex(master_idx, fill_value=False).to_numpy(dtype=bool)
        buy_arr_cache[sym] = arr
        return arr

    def _sell_arr(sym: str) -> np.ndarray | None:
        if not sell_conditions:
            return None
        if sym in sell_arr_cache:
            return sell_arr_cache[sym]
        sm = build_signal_mask(data, sell_conditions, sell_logic, current_symbol=sym)
        if sm.empty:
            sell_arr_cache[sym] = None
            return None
        arr = sm.reindex(master_idx, fill_value=False).to_numpy(dtype=bool)
        sell_arr_cache[sym] = arr
        return arr

    # 5) State
    next_open = (fill == "next_open")
    cash = float(initial_capital)
    rule_pcts = rule_sell_pcts or {}
    positions: dict[str, _PortPosition] = {}
    pending_buys: list[str] = []
    pending_sells: dict[str, str] = {}
    trades: list[dict] = []
    equity = np.empty(n, dtype=float)
    last_valid_close: dict[str, float] = {}   # 종목 진입 시 lazy 초기화
    candidates: list[str] = []      # 정렬된 후보 (rebalance 결과)
    candidates_set: set[str] = set()
    last_rb_i: int | None = None
    effective_limit = max(1, min(
        int(screener_spec.get("limit") or screener_limit),
        int(screener_limit),
        _MAX_POSITIONS_GLOBAL,
    ))

    _REASON_KEYS = (
        ("익절", "tp"), ("손절", "sl"), ("ATR트레일링", "atr"),
        ("트레일링", "trail"), ("보유기간", "hold"),
    )

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

    def _gap_extra(sym: str, i: int) -> float:
        if not gap_extra_cost or i == 0:
            return 0.0
        a = _aligned(sym)
        prev_close = a["close"][i - 1]
        cur_open = a["open"][i]
        if np.isnan(prev_close) or np.isnan(cur_open) or prev_close <= 0:
            return 0.0
        gap_pct = abs(cur_open - prev_close) / prev_close * 100
        if gap_pct <= gap_threshold_pct:
            return 0.0
        return (gap_pct - gap_threshold_pct) / 100 * 0.5

    def _open(sym: str, i: int, raw_price: float, budget: float):
        nonlocal cash
        if np.isnan(raw_price) or raw_price <= 0 or budget <= 0:
            return
        a = _aligned(sym)
        extra = _gap_extra(sym, i) if next_open else 0.0
        slipped = raw_price * (1 + slippage + extra)
        price = round_to_tick(slipped, direction="up", currency=a["currency"])
        if price <= 0:
            return
        per_share_cost = price * (1 + commission)
        max_by_budget = int(budget // per_share_cost)
        max_by_cash = int(cash // per_share_cost)
        new_shares = min(max_by_budget, max_by_cash)
        if new_shares <= 0:
            return
        cost = new_shares * per_share_cost
        cash -= cost
        peak_h = a["high"][i]
        peak_c = a["close"][i]
        positions[sym] = _PortPosition(
            shares=float(new_shares),
            entry_price=price,
            entry_i=i,
            peak_high=price if np.isnan(peak_h) else float(peak_h),
            peak_close=price if np.isnan(peak_c) else float(peak_c),
            executed_rules=set(),
        )

    def _close(sym: str, i: int, raw_price: float, reason: str,
                sell_pct: float = 100.0):
        nonlocal cash
        pos = positions.get(sym)
        if pos is None or pos.shares <= 0:
            return
        sell_pct = max(0.0, min(100.0, float(sell_pct)))
        if sell_pct <= 0:
            return
        if np.isnan(raw_price) or raw_price <= 0:
            return
        a = _aligned(sym)
        extra = _gap_extra(sym, i) if next_open else 0.0
        slipped = raw_price * (1 - slippage - extra)
        price = round_to_tick(slipped, direction="down", currency=a["currency"])
        if price <= 0:
            return
        sell_shares = float(int(pos.shares * sell_pct / 100.0))
        if sell_shares <= 0:
            return
        sell_shares = min(sell_shares, pos.shares)
        proceeds = sell_shares * price * (1 - commission - sell_tax)
        cost = sell_shares * pos.entry_price * (1 + commission)
        is_partial = sell_shares < pos.shares - 1e-9
        trades.append({
            "종목": sym,
            "진입일": master_idx[pos.entry_i],
            "청산일": master_idx[i],
            "보유일": i - pos.entry_i,
            "진입가": pos.entry_price,
            "청산가": price,
            "수익률(%)": (proceeds - cost) / cost * 100,
            "청산사유": reason + (f"({sell_pct:.0f}%)" if is_partial else ""),
        })
        cash += proceeds
        pos.shares -= sell_shares
        if pos.shares <= 1e-9:
            del positions[sym]

    # 6) 시뮬레이션 loop
    for i in range(n):
        closed_this_step: set[str] = set()

        # 6-0) Rebalance 시점 평가
        if _is_rebalance_day(i, last_rb_i, master_idx, rb_period, rb_every_n_days):
            try:
                new_cands = _evaluate_screener_at(screener_spec, pool_dfs, master_idx[i], pool)
            except ValueError as e:
                return _empty(str(e))
            new_cands = new_cands[:effective_limit]
            if rb_mode == "off":
                if not candidates:
                    candidates = list(new_cands)
                    candidates_set = set(candidates)
            elif rb_mode == "hold":
                slots = max(0, effective_limit - len(positions))
                added = [s for s in new_cands if s not in positions][:slots]
                candidates = list(positions.keys()) + added
                candidates_set = set(candidates)
            elif rb_mode == "replace":
                new_set = set(new_cands)
                for sym in list(positions.keys()):
                    if sym not in new_set:
                        if next_open:
                            pending_sells.setdefault(sym, "리밸런싱")
                        else:
                            _close(sym, i, _aligned(sym)["close"][i], "리밸런싱", 100.0)
                            if sym not in positions:
                                closed_this_step.add(sym)
                candidates = list(new_cands)
                candidates_set = new_set
            last_rb_i = i

        # 6-1) next_open pending 처리 (매도 → 매수)
        if next_open:
            for sym, reason in list(pending_sells.items()):
                op = _aligned(sym)["open"][i]
                _close(sym, i, op, reason, _sell_pct_of(reason))
                key = _reason_key(reason)
                if key is not None and sym in positions:
                    positions[sym].executed_rules.add(key)
            pending_sells.clear()
            cash_snapshot = cash
            for sym in pending_buys:
                if len(positions) >= effective_limit:
                    break
                if sym in positions:
                    continue
                op = _aligned(sym)["open"][i]
                desired = cash_snapshot * (amount_pct / 100.0)
                _open(sym, i, op, min(desired, cash))
            pending_buys.clear()

        # 6-2) 매도 평가 (보유 종목)
        for sym in list(positions.keys()):
            pos = positions[sym]
            a = _aligned(sym)
            close = a["close"][i]
            if np.isnan(close):
                continue
            high = a["high"][i]
            atr_arr = a["atr"]
            atr_v = atr_arr[i] if atr_arr is not None else np.nan
            if not np.isnan(high) and high > pos.peak_high:
                pos.peak_high = float(high)
            if close > pos.peak_close:
                pos.peak_close = float(close)
            cur_ret = (close - pos.entry_price) / pos.entry_price * 100
            held = i - pos.entry_i
            sym_sell = _sell_arr(sym)
            reason = ""
            if (take_profit is not None and cur_ret >= take_profit
                    and "tp" not in pos.executed_rules):
                reason = "익절"
            elif (stop_loss is not None and cur_ret <= stop_loss
                    and "sl" not in pos.executed_rules):
                reason = "손절"
            elif (trail_atr_mult is not None and atr_arr is not None
                    and not np.isnan(atr_v)
                    and close <= pos.peak_high - trail_atr_mult * atr_v
                    and "atr" not in pos.executed_rules):
                reason = "ATR트레일링"
            elif (trail_pct is not None
                    and close <= pos.peak_close * (1 - trail_pct / 100)
                    and "trail" not in pos.executed_rules):
                reason = "트레일링스톱"
            elif (hold_days is not None and held >= hold_days
                    and "hold" not in pos.executed_rules):
                reason = "보유기간"
            elif sym_sell is not None and sym_sell[i]:
                reason = "매도신호"
            if reason:
                if next_open:
                    pending_sells[sym] = reason
                else:
                    _close(sym, i, close, reason, _sell_pct_of(reason))
                    if sym not in positions:
                        closed_this_step.add(sym)
                    else:
                        key = _reason_key(reason)
                        if key is not None:
                            positions[sym].executed_rules.add(key)

        # 6-3) 매수 평가 — candidates 순서 안에서 미보유 + buy_arr trigger
        if next_open:
            for sym in candidates:
                if sym in positions or sym in pending_buys:
                    continue
                ba = _buy_arr(sym)
                if ba is not None and ba[i]:
                    pending_buys.append(sym)
        else:
            cash_snapshot = cash
            for sym in candidates:
                if sym in positions or sym in closed_this_step:
                    continue
                if len(positions) >= effective_limit:
                    break
                ba = _buy_arr(sym)
                if ba is None or not ba[i]:
                    continue
                cl = _aligned(sym)["close"][i]
                if np.isnan(cl):
                    continue
                desired = cash_snapshot * (amount_pct / 100.0)
                _open(sym, i, cl, min(desired, cash))

        # 6-4) NAV
        nav = cash
        for sym, pos in positions.items():
            cl = _aligned(sym)["close"][i]
            if not np.isnan(cl):
                last_valid_close[sym] = float(cl)
                nav += pos.shares * cl
            elif last_valid_close.get(sym, 0.0) > 0:
                nav += pos.shares * last_valid_close[sym]
            else:
                nav += pos.shares * pos.entry_price
        equity[i] = nav

    # 7) 기간 종료 — 미청산 강제 청산
    for sym in list(positions.keys()):
        cl_arr = _aligned(sym)["close"]
        final_close = np.nan
        for j in range(n - 1, -1, -1):
            if not np.isnan(cl_arr[j]):
                final_close = float(cl_arr[j])
                break
        if np.isnan(final_close):
            pos = positions[sym]
            cash += pos.shares * pos.entry_price
            del positions[sym]
        else:
            _close(sym, n - 1, final_close, "기간종료", 100.0)
    equity[n - 1] = cash

    # 8) Benchmark — 첫날 후보의 equal-weight buy-and-hold (없으면 pool 평균)
    bench_pool = candidates if candidates else pool[:effective_limit]
    bench = np.zeros(n, dtype=float)
    if bench_pool:
        weight = initial_capital / len(bench_pool)
        for sym in bench_pool:
            cl_series = pd.Series(_aligned(sym)["close"]).ffill().bfill()
            if cl_series.empty:
                continue
            first = cl_series.iloc[0]
            if not first or first <= 0:
                continue
            bench += (cl_series / first * weight).to_numpy()
    else:
        bench[:] = initial_capital

    equity_s = pd.Series(equity, index=master_idx, name="전략")
    benchmark_s = pd.Series(bench, index=master_idx, name="Buy&Hold")
    trades_df = pd.DataFrame(trades)

    return {
        "success": True,
        "error": None,
        "equity": equity_s,
        "benchmark": benchmark_s,
        "trades": trades_df,
        "metrics": _metrics(equity_s, benchmark_s, trades_df),
    }

"""통합 실행 엔진 (Unified Execution Engine) — 명세 §7.6.

포지션 기반 일별 루프. 진입(트리거)·선택·사이징·청산·실행을 직교 원자 단계로
조립한다. 정수주 회계. 기존 backtest.py(이벤트)·run.py:_run_rebalance(리밸런스)를
단계 설정으로 흡수하는 것이 목표 — 두 경로의 능력이 임의 조합 가능해진다.

설계 계약(§7.6):
  - 거래단위 = 레그 리스트(단일 종목 = 1레그; 페어/헤지 = N레그). Stage 1은 1레그.
  - 단계 pluggable: Selector·Sizer는 registry, 진입은 (주기 ⊕ 이벤트+지연).
  - 회계 정수주. 숏은 음수주 + borrow_cost(차입가능성 미모델 — 데이터 부재 한계).
  - 비용·슬리피지는 스칼라 또는 (날짜/종목) 시계열. 현금은 무위험금리 가산(기본 0).

구현 단계(STAGE 주석으로 표시):
  - Stage 1 ✅: 코어 루프 + 이벤트 트리거 + per-name 사이징 + 전체 청산 + 정수 실행
               + 벤치마크 파라미터. run_portfolio_ir/run_backtest_ir 패리티.
  - Stage 2~5: 횡단 스케줄 선택·사이저 registry·청산 overlay(refill)·전역 overlay·전환.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..backtest import (
    _DEFAULT_COMMISSION, _DEFAULT_SELL_TAX, _DEFAULT_SLIPPAGE,
    _MAX_POSITIONS_GLOBAL, _empty, _metrics,
)
from ..blocks import EvalContext, Node, evaluate, referenced_symbols, select_symbol
from ..blocks.catalog import get, has
from ..exec_defaults import margin_rate, round_to_tick
from .metrics import finalize_metrics
from .spec import StrategyIR

TRADING_DAYS = 252

# 청산 사유 → rule_sell_pcts 키 (부분청산 비율 조회용). backtest.py와 동일 규약.
_REASON_KEYS = (
    ("익절", "tp"), ("손절", "sl"), ("ATR트레일링", "atr"),
    ("트레일링", "trail"), ("보유기간", "hold"),
)


# ── 포지션 상태 (바스켓 가능 — Stage 1은 1레그) ──────────────────────────────

@dataclass
class Position:
    """보유 단위. legs = [(symbol, leg_weight)]; 단일 종목 = [(sym, 1.0)].

    Stage 1은 단일 레그만 — sym 프로퍼티로 접근. 멀티레그 체결은 후속 Stage.
    """
    legs: list                      # [(symbol, leg_weight), ...]
    shares: float
    entry_price: float
    entry_i: int
    peak_high: float
    peak_close: float
    executed_rules: set = field(default_factory=set)

    @property
    def sym(self) -> str:
        return self.legs[0][0]


# ── 평가 스코핑 (run.py와 동일 — 유니버스 공통 달력) ──────────────────────────

def _scoped(dataset: dict, syms, *nodes) -> dict:
    # 유니버스 순서(syms)를 보존하고 외부 참조는 정렬해 덧붙인다 — ds(=패널 컬럼) 순서를
    # 결정적으로 고정. 과거엔 set(syms)을 그대로 순회해 컬럼 순서가 PYTHONHASHSEED에 따라
    # 프로세스마다 달라졌고, 정수주 리밸런스(_execute 매수 배분)·rank(method="first") 동률
    # 분해가 그 순서에 의존해 같은 전략·데이터의 백테스트 결과가 서버 재시작마다 흔들렸다.
    keep = list(dict.fromkeys(syms))
    extra: set = set()
    for nd in nodes:
        if nd is not None:
            extra |= referenced_symbols(nd)
    keep += sorted(extra.difference(keep))
    sub = {s: dataset[s] for s in keep
           if s in dataset and dataset[s] is not None and not dataset[s].empty}
    return sub or dataset


def _ir_panel(node: Node | None, ctx: EvalContext):
    """조건/점수 Node를 평가해 패널(DataFrame) 반환. None이면 None."""
    if node is None:
        return None
    return evaluate(node, ctx)


def _sym_bool(panel, sym: str, index) -> np.ndarray | None:
    """패널에서 sym 컬럼을 bool 배열로 (없으면 전부 False)."""
    if panel is None:
        return None
    series = select_symbol(panel, sym) if not isinstance(panel, pd.DataFrame) else (
        panel[sym] if sym in panel.columns else pd.Series(False, index=index))
    return series.reindex(index, fill_value=False).fillna(False).to_numpy(dtype=bool)


# ── 청산 사유 판정 (backtest.py 캐스케이드와 동일 우선순위) ────────────────────

def price_exit_reason(*, cur_ret: float, close: float, peak_high: float,
                      peak_close: float, atr_v, exits, sign: float = 1.0,
                      executed=()) -> str:
    """가격기반 청산 사유(익절·손절·ATR트레일·트레일) — 백테스트·라이브 단일 소스.

    backtest 일별 루프(`_exit_reason`)와 로컬앱 장중 tick(`ir_engine.live.intraday_exit_reason`)
    이 같은 임계 공식을 쓰도록 추출 — backtest=live 일치 보장.

    cur_ret: 부호 반영 수익률(%) = sign*(close-entry)/entry*100. peak_high/peak_close: ATR/
    퍼센트 트레일 기준(라이브 tick은 단일 고가 워터마크를 둘 다에 전달). executed: 이미 발동한
    룰 키 셋(백테스트 부분청산 가드 — 라이브는 빈 셋). 미발동이면 "".
    sign: +1 롱(기본)·-1 숏(익절/손절 부호 반영). 트레일링은 롱 지향(peak 기반) — 숏 트레일 후속.
    """
    if exits.take_profit is not None and cur_ret >= exits.take_profit and "tp" not in executed:
        return "익절"
    if exits.stop_loss is not None and cur_ret <= exits.stop_loss and "sl" not in executed:
        return "손절"
    if (sign > 0 and exits.trail_atr_mult is not None and atr_v is not None and not np.isnan(atr_v)
            and close <= peak_high - exits.trail_atr_mult * atr_v and "atr" not in executed):
        return "ATR트레일링"
    if (sign > 0 and exits.trail_pct is not None
            and close <= peak_close * (1 - exits.trail_pct / 100) and "trail" not in executed):
        return "트레일링스톱"
    return ""


def _exit_reason(pos: Position, i: int, close: float, high: float, atr_v: float,
                 sell_arr, exits, sign: float = 1.0) -> str:
    """보유 포지션 청산 사유 — 가격기반(공유 scalar) → 보유일 → 매도신호 OR(첫 발동).

    가격기반(익절·손절·트레일)은 `price_exit_reason`으로 분리(라이브 장중청산과 단일 소스).
    보유일(i 기반)·매도신호(sell_arr[i])는 백테스트 일별 루프 전용이라 여기서 캐스케이드 마무리.
    """
    cur_ret = sign * (close - pos.entry_price) / pos.entry_price * 100
    r = price_exit_reason(cur_ret=cur_ret, close=close, peak_high=pos.peak_high,
                          peak_close=pos.peak_close, atr_v=atr_v, exits=exits,
                          sign=sign, executed=pos.executed_rules)
    if r:
        return r
    if exits.hold_days is not None and (i - pos.entry_i) >= exits.hold_days and "hold" not in pos.executed_rules:
        return "보유기간"
    if sell_arr is not None and sell_arr[i]:
        return "매도신호"
    return ""


def _reason_key(reason: str) -> str | None:
    for needle, key in _REASON_KEYS:
        if needle in reason:
            return key
    return None


# ── 통합 엔진 (Stage 1: 이벤트 트리거) ────────────────────────────────────────

def run_unified(strategy: StrategyIR, dataset: dict[str, pd.DataFrame]) -> dict:
    """StrategyIR을 통합 일별 루프로 백테스트.

    STAGE 1: entry.mode=="on_signal" (이벤트 트리거)만 처리. scheduled/always는 후속.
    이벤트 경로를 run_portfolio_ir 역학에 맞춰 패리티 보존 — 단일 종목은 전액 투입.
    """
    pos_spec = strategy.position
    ent = pos_spec.entry
    sim = strategy.simulation
    exits = pos_spec.exit
    u = strategy.universe

    if ent.mode != "on_signal":
        return _run_scheduled(strategy, dataset)

    # 신호 타입 계약 (엔진 불변식 — §5.4)
    st = get(strategy.signal.op).out_type.value if has(strategy.signal.op) else None
    if st != "condition":
        return _empty("이벤트(on_signal) 진입은 신호가 condition(참/거짓)이어야 합니다 "
                      f"(현재: {st or '알 수 없는 블록'}).")
    if exits.condition is not None:
        et = get(exits.condition.op).out_type.value if has(exits.condition.op) else None
        if et != "condition":
            return _empty(f"매도 조건은 condition이어야 합니다 (현재: {et or '알 수 없는 블록'}).")

    syms = [s for s in u.symbols if s in dataset and not dataset[s].empty]
    if not syms:
        return _empty("유니버스에 종목이 없습니다.")
    single = (u.kind == "single" and len(syms) == 1)

    ds = _scoped(dataset, syms, strategy.signal, exits.condition)

    # 종목별 정렬 데이터 + 신호 마스크
    symbol_dfs: dict[str, pd.DataFrame] = {}
    for sym in syms:
        df = ds[sym]
        if not {"Open", "Close"}.issubset(df.columns):
            return _empty(f"'{sym}'에 시가·종가 데이터가 없습니다.")
        df = df.sort_index()
        if sim.start is not None:
            df = df[df.index >= pd.Timestamp(sim.start)]
        if sim.end is not None:
            df = df[df.index <= pd.Timestamp(sim.end)]
        df = df.dropna(subset=["Open", "Close"])
        if len(df) < 2:
            return _empty(f"'{sym}' 백테스트 기간의 가격 데이터가 부족합니다.")
        symbol_dfs[sym] = df
    if exits.trail_atr_mult is not None:
        for sym, df in symbol_dfs.items():
            if "atr_14" not in df.columns:
                return _empty(f"'{sym}'에 ATR 지표가 없어 ATR 트레일링을 쓸 수 없습니다.")

    master_idx = pd.DatetimeIndex(sorted(set().union(*(df.index for df in symbol_dfs.values()))))
    n = len(master_idx)
    if n < 2:
        return _empty("백테스트 기간의 가격 데이터가 부족합니다.")

    ctx = EvalContext.from_dataset(ds)
    buy_panel = _ir_panel(strategy.signal, ctx)
    if not isinstance(buy_panel, pd.DataFrame):
        return _empty("매수 신호가 패널을 산출하지 않습니다.")
    sell_panel = _ir_panel(exits.condition, ctx)

    fill = sim.fill
    if fill == "typical":
        for sym, df in symbol_dfs.items():
            if not {"High", "Low"}.issubset(df.columns):
                return _empty(f"'{sym}'에 고가·저가가 없어 typical 체결을 쓸 수 없습니다.")

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
            "exec": ((hi + lo + cl) / 3.0) if fill == "typical" else cl,
            "atr": (df["atr_14"].to_numpy(dtype=float) if "atr_14" in df.columns else None),
            "currency": "KRW" if sym.isdigit() else "USD",
        }
        buy_arrs[sym] = _sym_bool(buy_panel, sym, master_idx)
        sell_arrs[sym] = _sym_bool(sell_panel, sym, master_idx) if sell_panel is not None else None

    # 비용 (스칼라; 비용·슬리피지 시계열은 backlog — §7.6)
    commission = sim.commission if sim.commission is not None else _DEFAULT_COMMISSION
    slippage = sim.slippage if sim.slippage is not None else _DEFAULT_SLIPPAGE
    sell_tax = sim.sell_tax if sim.sell_tax is not None else _DEFAULT_SELL_TAX

    sz = pos_spec.sizing
    amount_pct = 100.0 if single else sz.amount_pct
    amount_krw = sz.amount_krw if sz.mode == "fixed_amount" else None
    rfr_daily = 0.0  # 현금 무위험수익(연율) hook — Stage 1 기본 0(패리티). 후속: sim 필드.

    defer = (fill == "next_open")
    cash = float(sim.initial_capital)
    positions: dict[str, Position] = {}
    pending_buys: list[str] = []
    pending_sells: dict[str, str] = {}
    trades: list[dict] = []
    equity = np.empty(n, dtype=float)
    last_valid_close: dict[str, float] = {sym: 0.0 for sym in syms}

    def _budget(cash_snapshot: float) -> float:
        return float(amount_krw) if amount_krw else cash_snapshot * (amount_pct / 100.0)

    def _open(sym: str, i: int, raw_price: float, budget: float):
        nonlocal cash
        if np.isnan(raw_price) or raw_price <= 0 or budget <= 0:
            return
        price = round_to_tick(raw_price * (1 + slippage), "up", aligned[sym]["currency"])
        if price <= 0:
            return
        per_share_cost = price * (1 + commission)
        new_shares = min(int(budget // per_share_cost), int(cash // per_share_cost))
        if new_shares <= 0:
            return
        cash -= new_shares * per_share_cost
        ph, pc = aligned[sym]["high"][i], aligned[sym]["close"][i]
        positions[sym] = Position(
            legs=[(sym, 1.0)], shares=float(new_shares), entry_price=price, entry_i=i,
            peak_high=price if np.isnan(ph) else float(ph),
            peak_close=price if np.isnan(pc) else float(pc))

    def _close(sym: str, i: int, raw_price: float, reason: str, sell_pct: float = 100.0):
        nonlocal cash
        pos = positions.get(sym)
        if pos is None or pos.shares <= 0:
            return
        sell_pct = max(0.0, min(100.0, float(sell_pct)))
        if sell_pct <= 0 or np.isnan(raw_price) or raw_price <= 0:
            return
        price = round_to_tick(raw_price * (1 - slippage), "down", aligned[sym]["currency"])
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
        closed_this_step: set[str] = set()   # 비-defer: 당일 청산 종목 동일일 재진입 차단
        if rfr_daily:
            cash *= (1 + rfr_daily)
        if defer:
            for sym, reason in list(pending_sells.items()):
                _close(sym, i, aligned[sym]["open"][i], reason, _sell_pct(reason, sz))
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

        # 청산 검사
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
            reason = _exit_reason(pos, i, close, high, atr_v, sell_arrs[sym], exits)
            if reason:
                if defer:
                    pending_sells[sym] = reason
                else:
                    _close(sym, i, aligned[sym]["exec"][i], reason, _sell_pct(reason, sz))
                    if sym not in positions:
                        closed_this_step.add(sym)
                    else:
                        key = _reason_key(reason)
                        if key is not None:
                            positions[sym].executed_rules.add(key)

        # 진입 검사 (이벤트 트리거)
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
                if not buy_arrs[sym][i] or np.isnan(aligned[sym]["close"][i]):
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

    # 기간말 강제청산
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

    benchmark_s = _benchmark(strategy, dataset, aligned, syms, master_idx, sim.initial_capital)
    equity_s = pd.Series(equity, index=master_idx, name="전략")
    trades_df = pd.DataFrame(trades)
    return {"success": True, "error": None, "equity": equity_s,
            "benchmark": benchmark_s, "trades": trades_df,
            "metrics": finalize_metrics(_metrics(equity_s, benchmark_s, trades_df),
                                        equity_s, benchmark_s, trades_df)}


def _sell_pct(reason: str, sizing) -> float:
    """부분청산 비율 — Stage 1은 전량(100%). rule_sell_pcts 배선은 후속."""
    return 100.0


# ── 벤치마크 (지정 가능 — §7.6) ───────────────────────────────────────────────

def _benchmark(strategy: StrategyIR, dataset: dict, aligned: dict, syms: list,
               master_idx, initial_capital: float) -> pd.Series:
    """비교 기준 시계열. Stage 1 기본 = 유니버스 동일가중 buy&hold (기존 동작).

    §7.6: 심볼/지수/무위험금리/사용자전략 지정은 후속 Stage(simulation.benchmark 필드).
    """
    bench = np.zeros(len(master_idx), dtype=float)
    weight = initial_capital / len(syms)
    for sym in syms:
        cl_series = pd.Series(aligned[sym]["close"]).ffill().bfill()
        first = cl_series.iloc[0] if not cl_series.empty else 0
        if first and first > 0:
            bench += (cl_series / first * weight).to_numpy()
    return pd.Series(bench, index=master_idx, name="Buy&Hold")


# ══ Stage 2: 횡단 스케줄 경로 (정수주 리밸런스) ════════════════════════════════

def _is_rebalance(i: int, last: int | None, idx, period: str, every_n) -> bool:
    """캘린더 주기 규칙 — daily·weekly·monthly·quarterly·annual·every_n_days (§7.6)."""
    if last is None:
        return True
    if period == "daily":
        return True
    d, p = idx[i], idx[last]
    if period == "weekly":
        return d.isocalendar()[:2] != p.isocalendar()[:2]
    if period == "monthly":
        return (d.year, d.month) != (p.year, p.month)
    if period == "quarterly":
        return (d.year, (d.month - 1) // 3) != (p.year, (p.month - 1) // 3)
    if period == "annual":
        return d.year != p.year
    if period == "every_n_days":
        return bool(every_n) and (i - last) >= int(every_n)
    return False


def _universe_symbols(strategy: StrategyIR, dataset: dict) -> list:
    u = strategy.universe
    if u.kind in ("single", "list"):
        return [s for s in u.symbols if s in dataset and not dataset[s].empty]
    macro: set = set()
    if u.exclude_macro:
        try:
            from ..data_fetcher import MACRO_SYMBOLS
            macro = set(MACRO_SYMBOLS)
        except Exception:
            macro = set()
    return [s for s, df in dataset.items()
            if s not in macro and df is not None and not df.empty
            and {"Open", "Close"}.issubset(df.columns)]


def _screener_mask(screener: dict, ctx, cols: list) -> pd.DataFrame:
    """스크리너 자격 마스크(dates×cols, bool) — 단일 선별 조건 평가, 시점별 PIT.

    조건은 필터·횡단순위(rank 블록)를 AND/OR로 조합한 condition 트리. 횡단 연산은
    엔진이 panel(dates×cols) 컨텍스트에서 그대로 평가한다(별도 rank 처리 없음)."""
    midx = ctx.master_idx
    cond = screener.get("condition")
    if cond is None:
        return pd.DataFrame(True, index=midx, columns=cols)
    m = evaluate(Node.model_validate(cond), ctx)
    if isinstance(m, pd.DataFrame):
        return m.reindex(index=midx, columns=cols).fillna(False).astype(bool)
    return pd.DataFrame(True, index=midx, columns=cols)


def _apply_refresh(mask: pd.DataFrame, refresh: str, start) -> pd.DataFrame:
    """자격 마스크에 재선별 시점 적용.

    each_rebalance(동적): 마스크 그대로 — 매 시점 PIT 자격.
    once_at_start(정적): 백테스트 시작(sim.start 이후 첫 행)의 자격 행을 전 기간으로
    broadcast → 후보 바스켓 고정. 시작일까지의 데이터만 쓰므로 lookahead 없음. 시작일
    충족 0개면 빈 바스켓(거래 없음).
    """
    if refresh != "once_at_start":
        return mask
    rows = mask if start is None else mask[mask.index >= pd.Timestamp(start)]
    if rows.empty:
        # start가 데이터 윈도우 밖 — 형성할 바스켓 없음. 빈(전부 False) 마스크(거래 없음).
        return pd.DataFrame(False, index=mask.index, columns=mask.columns)
    basket = rows.iloc[0]                      # 형성일 자격 (cols, bool)
    return pd.DataFrame(
        np.tile(basket.to_numpy(dtype=bool), (len(mask.index), 1)),
        index=mask.index, columns=mask.columns)


def _cap_groups(w: pd.Series, labels: pd.Series, cap_pct: float) -> pd.Series:
    """그룹별 |비중| 합이 cap 초과 시 그 그룹만 비례 축소(현금 버퍼 — 재정규화 안 함)."""
    cap = cap_pct / 100.0
    lab = labels.reindex(w.index)
    out = w.copy()
    for g in pd.unique(lab.dropna()):
        mask = (lab == g).reindex(out.index, fill_value=False)
        gsum = float(out[mask].abs().sum())
        if gsum > cap and gsum > 0:
            out.loc[mask] = out[mask] * (cap / gsum)
    return out


# ── 선택 Selector — score → (longs, shorts) ───────────────────────────────────

def _select(alpha_row: pd.Series, pos, is_condition: bool):
    """부호있는 목표집합. condition=참인 전부; score=임계(threshold) 또는 top_n/top_pct 횡단 랭킹."""
    a = alpha_row.dropna()
    if a.empty:
        return [], []
    direction = pos.direction
    if is_condition:
        sel = list(a[a > 0].index)
        return ([], sel) if direction == "short" else (sel, [])
    thr = pos.entry.threshold
    if thr is not None:                  # 임계 선택 — 부호·수준 기준(횡단 랭킹과 직교; TSMOM 등)
        longs, shorts = list(a[a > thr].index), list(a[a < thr].index)
        if direction == "long":
            return longs, []
        if direction == "short":
            return [], shorts
        return longs, shorts             # long_short
    top_n, top_pct = pos.entry.top_n, pos.entry.top_pct
    n = len(a)
    k = None
    if top_pct is not None:
        k = max(1, int(round(n * float(top_pct) / 100.0)))
    elif top_n is not None:
        k = min(int(top_n), n)
    if direction == "long_short":
        kk = k or max(1, n // 5)
        return list(a.nlargest(min(kk, n)).index), list(a.nsmallest(min(kk, n)).index)
    sel = a.nlargest(k) if k is not None else (a[a > 0] if (a > 0).any() else a.nlargest(1))
    return ([], list(sel.index)) if direction == "short" else (list(sel.index), [])


# ── 사이징 Sizer registry — 집합 → 비중(정규화 전) ────────────────────────────

def _sizer_equal(syms, alpha_row, vol_row, sz):
    return pd.Series(1.0, index=syms)


def _sizer_signal(syms, alpha_row, vol_row, sz):
    base = alpha_row.reindex(syms).clip(lower=0)
    return base if float(base.sum()) > 0 else pd.Series(1.0, index=syms)


def _sizer_inverse_vol(syms, alpha_row, vol_row, sz):
    if vol_row is None:
        return pd.Series(1.0, index=syms)
    inv = (1.0 / vol_row.reindex(syms)).replace([np.inf, -np.inf], np.nan)
    inv = inv.fillna(inv[inv.notna()].mean() if inv.notna().any() else 1.0)
    return inv if float(inv.sum()) > 0 else pd.Series(1.0, index=syms)


def _sizer_target_vol(syms, alpha_row, vol_row, sz):
    """per-name 목표 연변동성 — raw_i = target_vol / 연환산_vol_i (레버리지 팩터, 비정규화)."""
    tv = (sz.target_vol_pct or 10.0) / 100.0
    if vol_row is None:
        return pd.Series(1.0, index=syms)
    ann = vol_row.reindex(syms) * np.sqrt(TRADING_DAYS)
    raw = (tv / ann).replace([np.inf, -np.inf], np.nan)
    return raw.fillna(raw[raw.notna()].mean() if raw.notna().any() else 1.0)


SIZERS = {                              # registry — 새 사이저는 등록만(코어 불변)
    "equal_weight": _sizer_equal,
    "signal_proportional": _sizer_signal,
    "vol_inverse": _sizer_inverse_vol,
    "target_vol": _sizer_target_vol,
}


def _target_weights(longs, shorts, alpha_row, vol_row, sz):
    """부호있는 목표비중. 기본 sum|w|=1; target_vol은 절대 레버리지 보존(비정규화)."""
    if sz.mode == "fixed_weight" and sz.weights:
        members = longs + shorts
        w = pd.Series({s: float(sz.weights.get(s, 0.0)) for s in members})
        tot = float(w.abs().sum())
        return w / tot if tot > 0 else w
    sizer = SIZERS.get(sz.mode, _sizer_equal)
    norm = (sz.mode != "target_vol")     # target_vol은 per-name 절대 레버리지를 보존
    parts = []
    if longs:
        wl = sizer(longs, alpha_row, vol_row, sz)
        wl = wl / wl.sum() if norm else wl
        parts.append(wl * (0.5 if (shorts and norm) else 1.0))
    if shorts:
        ws = sizer(shorts, alpha_row, vol_row, sz)
        ws = ws / ws.sum() if norm else ws
        parts.append(-ws * (0.5 if (longs and norm) else 1.0))
    return pd.concat(parts) if parts else pd.Series(dtype=float)


def _apply_dd_control(net: pd.Series, hard_pct: float, soft_pct=None) -> pd.Series:
    """낙폭 반응형 노출 스케일러 — soft~hard 선형 디리스킹, hard에서 0. causal(전일까지 낙폭)."""
    hard = abs(float(hard_pct))
    soft = abs(float(soft_pct)) if soft_pct is not None else hard
    if soft >= hard:
        soft = hard
    span = hard - soft
    arr = net.to_numpy(dtype=float)
    out = np.empty_like(arr)
    eq = peak = 1.0
    for i, r in enumerate(arr):
        depth = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        scale = 1.0 if depth <= soft else (0.0 if depth >= hard else (hard - depth) / span)
        rr = r * scale
        out[i] = rr
        eq *= (1.0 + rr)
        if eq > peak:
            peak = eq
    return pd.Series(out, index=net.index)


def _run_scheduled(strategy: StrategyIR, dataset: dict) -> dict:
    """스케줄 횡단 리밸런스 — 정수주. STAGE 2: long-only(롱숏 정수 체결은 Stage 4).

    청산 overlay(중간 청산·refill)는 Stage 3. 여기선 리밸런스 교체만.
    """
    pos = strategy.position
    ent, sim, sz = pos.entry, strategy.simulation, pos.sizing
    signal = strategy.signal
    st = get(signal.op).out_type.value if has(signal.op) else None
    if st not in ("condition", "score"):
        return _empty("정기 리밸런스·상시 진입은 신호가 condition 또는 score여야 합니다 "
                      f"(현재: {st or '알 수 없는 블록'}).")
    is_condition = (st == "condition")
    if pos.direction in ("short", "long_short") and is_condition:
        return _empty("숏·롱숏은 신호가 score(점수)여야 순위로 방향을 가립니다.")

    syms = _universe_symbols(strategy, dataset)
    if not syms:
        return _empty("유니버스에 종목이 없습니다.")
    screener = strategy.universe.screener or {}
    filt_node = (Node.model_validate(screener["condition"])
                 if screener.get("condition") else None)
    gl = pos.overlays.group_label
    ds = _scoped(dataset, syms, signal, filt_node, gl)
    ctx = EvalContext.from_dataset(ds)
    try:
        alpha = evaluate(signal, ctx)
    except Exception as e:  # noqa: BLE001
        return _empty(f"신호 평가 오류: {e}")
    if not isinstance(alpha, pd.DataFrame):
        return _empty("신호가 패널(종목×날짜)을 산출하지 않습니다.")
    if is_condition:
        b = alpha.astype(bool)
        alpha = b.astype(float).where(b, np.nan)
    cols = [s for s in syms if s in alpha.columns]
    if not cols:
        return _empty("신호가 유니버스 종목을 포함하지 않습니다.")
    alpha = alpha[cols]
    if screener.get("condition"):
        elig = _apply_refresh(_screener_mask(screener, ctx, cols),
                              screener.get("refresh", "each_rebalance"), sim.start)
        alpha = alpha.where(elig.reindex(index=alpha.index, columns=cols).fillna(False))

    group_panel = None
    if gl is not None:
        gp = evaluate(gl, ctx)
        group_panel = gp.reindex(columns=cols) if isinstance(gp, pd.DataFrame) else None

    if sim.start is not None:
        alpha = alpha[alpha.index >= pd.Timestamp(sim.start)]
    if sim.end is not None:
        alpha = alpha[alpha.index <= pd.Timestamp(sim.end)]
    master = alpha.index
    if len(master) < 2:
        return _empty("백테스트 기간의 데이터가 부족합니다.")

    close = {s: dataset[s]["Close"].reindex(master).ffill().to_numpy(dtype=float) for s in cols}
    openp = {s: dataset[s]["Open"].reindex(master).ffill().to_numpy(dtype=float) for s in cols}
    high = {s: (dataset[s]["High"].reindex(master).ffill().to_numpy(dtype=float)
                if "High" in dataset[s].columns else close[s]) for s in cols}
    atr = {s: (dataset[s]["atr_14"].reindex(master).ffill().to_numpy(dtype=float)
               if "atr_14" in dataset[s].columns else None) for s in cols}
    rets = pd.DataFrame({s: pd.Series(close[s], index=master).pct_change().fillna(0.0)
                         for s in cols}, index=master)
    vol = rets.rolling(sz.vol_window).std()

    # 청산 overlay (Stage 3) — 매도조건 패널 + tp/sl/trail/hold 상태 기반
    exits = pos.exit
    if exits.condition is not None:
        ct = get(exits.condition.op).out_type.value if has(exits.condition.op) else None
        if ct != "condition":
            return _empty(f"매도 조건은 condition이어야 합니다 (현재: {ct or '알 수 없는 블록'}).")
    if exits.trail_atr_mult is not None and any(atr[s] is None for s in cols):
        return _empty("ATR 트레일링은 atr_14 지표가 필요합니다.")
    sell_panel = evaluate(exits.condition, ctx) if exits.condition is not None else None
    sell_arr: dict = {}
    for s in cols:
        if isinstance(sell_panel, pd.DataFrame) and s in sell_panel.columns:
            sell_arr[s] = (sell_panel[s].reindex(master, fill_value=False)
                           .fillna(False).to_numpy(dtype=bool))
        else:
            sell_arr[s] = None
    refill_mode = ent.refill
    cur_of = {s: ("KRW" if s.isdigit() else "USD") for s in cols}
    commission = sim.commission if sim.commission is not None else _DEFAULT_COMMISSION
    slippage = sim.slippage if sim.slippage is not None else _DEFAULT_SLIPPAGE
    sell_tax = sim.sell_tax if sim.sell_tax is not None else _DEFAULT_SELL_TAX
    cap = sz.max_position_pct / 100.0
    lev = float(sim.leverage)
    rfr_daily = (sim.rfr_pct / 100.0 / TRADING_DAYS) if sim.rfr_pct else 0.0
    borrow_daily = (sim.short_borrow_pct / 100.0 / TRADING_DAYS) if sim.short_borrow_pct else 0.0
    funding_daily = (sim.funding_cost_pct / 100.0 / TRADING_DAYS) if sim.funding_cost_pct else 0.0
    maint = (sim.maintenance_margin_pct / 100.0) if sim.maintenance_margin_pct else None
    defer = (sim.fill == "next_open")
    period = "daily" if ent.mode == "always" else ent.rebalance

    cash = float(sim.initial_capital)
    positions: dict[str, Position] = {}
    pending: dict | None = None
    last = None
    n = len(master)
    equity = np.empty(n, dtype=float)
    trades: list[dict] = []
    turnover_notional = 0.0

    def _exec_price(s, i, side):
        raw = openp[s][i] if defer else close[s][i]
        if np.isnan(raw) or raw <= 0:
            return None
        return round_to_tick(raw * (1 + slippage) if side == "buy" else raw * (1 - slippage),
                             "up" if side == "buy" else "down", cur_of[s])

    def _record(s, p, qty, px, i, reason):
        """qty주 청산 기록 — 부호 인식. 롱: 순매도-순매수 대비; 숏: 진입-청산."""
        if p.shares > 0:
            cost = qty * p.entry_price * (1 + commission)
            proceeds = qty * px * (1 - commission - sell_tax)
            ret = (proceeds - cost) / cost * 100 if cost else 0.0
        else:                                          # 숏: 가격 하락이 이익
            ret = (p.entry_price - px) / p.entry_price * 100 if p.entry_price else 0.0
        trades.append({"종목": s, "진입일": master[p.entry_i], "청산일": master[i],
                       "보유일": i - p.entry_i, "진입가": p.entry_price, "청산가": px,
                       "수익률(%)": ret, "청산사유": reason})

    def _execute(target: dict, i: int, reasons: dict | None = None):
        """정수주 리밸런스 — 롱숏 signed. 1단계 매도(현금↑) → 2단계 매수(현금↓). 부호교차=청산+재개시.

        거래세는 매도(롱청산·숏개시) 단방향. 트레이드는 전량 청산 시에만 기록(라운드트립)."""
        nonlocal cash, turnover_notional
        reasons = reasons or {}
        # 매수여력 = NAV×L. 현금이 floor_cash까지 내려가도록(=차입) 허용해 Σ|w|=lev 타깃을 채운다.
        # lev=1이면 floor_cash=0 → 캡이 정확히 기존 현금캡(cash//per)과 동일(무레버리지 무영향).
        nav_exec = cash + sum(p.shares * close[s][i] for s, p in positions.items()
                              if not np.isnan(close[s][i]))
        floor_cash = nav_exec * (1.0 - lev)
        # 결정적 순서 — 매수 루프가 종목별로 남은 현금에서 정수주를 순차 배분하므로
        # 처리 순서가 반올림 잔여(=결과)를 바꾼다. set 순회는 PYTHONHASHSEED 의존이라
        # 같은 전략·데이터의 결과가 프로세스마다 흔들렸다. 심볼명 정렬로 재현성 고정.
        allsyms = sorted(set(positions) | set(target))
        for s in allsyms:                              # 거래 의도 노티오널(턴오버 지표)
            cur = positions[s].shares if s in positions else 0.0
            px0 = close[s][i]
            if not np.isnan(px0):
                turnover_notional += abs(float(int(target.get(s, 0))) - cur) * px0
        for s in allsyms:                              # 1) 매도(현금 생성)
            cur = positions[s].shares if s in positions else 0.0
            tgt = float(int(target.get(s, 0)))
            if tgt >= cur:
                continue
            px = _exec_price(s, i, "sell")
            if px is None:
                continue
            if cur > 0:                                # 롱 축소/청산 (+ 부호교차 시 숏 개시)
                p = positions[s]
                close_qty = cur - max(tgt, 0)
                cash += close_qty * px * (1 - commission - sell_tax)
                p.shares -= close_qty
                if p.shares <= 1e-9:
                    _record(s, p, close_qty, px, i, reasons.get(s, "리밸런스"))
                    del positions[s]
                if tgt < 0:                            # 부호교차 → 숏 |tgt| 개시
                    qty = int(-tgt)
                    cash += qty * px * (1 - commission - sell_tax)
                    positions[s] = Position(legs=[(s, 1.0)], shares=-qty, entry_price=px,
                                            entry_i=i, peak_high=px, peak_close=px)
            elif s in positions:                       # 숏 증가 (cur<0, tgt<cur)
                p = positions[s]
                delta = cur - tgt
                cash += delta * px * (1 - commission - sell_tax)
                p.entry_price = (abs(cur) * p.entry_price + delta * px) / (abs(cur) + delta)
                p.shares = tgt
            else:                                      # 신규 숏 (cur=0, tgt<0)
                qty = int(-tgt)
                cash += qty * px * (1 - commission - sell_tax)
                positions[s] = Position(legs=[(s, 1.0)], shares=-qty, entry_price=px,
                                        entry_i=i, peak_high=px, peak_close=px)
        for s in allsyms:                              # 2) 매수(현금 사용)
            cur = positions[s].shares if s in positions else 0.0
            tgt = float(int(target.get(s, 0)))
            if tgt <= cur:
                continue
            px = _exec_price(s, i, "buy")
            if px is None:
                continue
            per = px * (1 + commission)
            if cur < 0:                                # 숏 커버 (+ 부호교차 시 롱 개시)
                p = positions[s]
                cover = min(int(-cur), int(tgt - cur))
                cash -= cover * per
                p.shares += cover
                if abs(p.shares) <= 1e-9:
                    _record(s, p, cover, px, i, reasons.get(s, "리밸런스"))
                    del positions[s]
                if tgt > 0:                            # 부호교차 → 롱 개시
                    buy = min(int(tgt), int((cash - floor_cash) // per)) if per > 0 else 0
                    if buy > 0:
                        cash -= buy * per
                        positions[s] = Position(legs=[(s, 1.0)], shares=buy, entry_price=px,
                                                entry_i=i, peak_high=px, peak_close=px)
            else:                                      # 롱 증가/신규 — 매수여력(NAV×L) 한도
                buy = min(int(tgt - cur), int((cash - floor_cash) // per)) if per > 0 else 0
                if buy <= 0:
                    continue
                cash -= buy * per
                if s in positions:
                    p = positions[s]
                    p.entry_price = (p.shares * p.entry_price + buy * px) / (p.shares + buy)
                    p.shares += buy
                else:
                    positions[s] = Position(legs=[(s, 1.0)], shares=buy, entry_price=px,
                                            entry_i=i, peak_high=px, peak_close=px)

    def _compute_target(i, d) -> dict:
        longs, shorts = _select(alpha.loc[d], pos, is_condition)
        if not longs and not shorts:
            return {}
        vol_row = vol.loc[d] if d in vol.index else None
        w = _target_weights(longs, shorts, alpha.loc[d], vol_row, sz)
        w = w[w != 0]
        if w.empty:
            return {}
        w = w.clip(lower=-cap, upper=cap)
        if sz.mode == "target_vol":
            gross = float(w.abs().sum())
            if gross > lev and gross > 0:
                w = w * (lev / gross)                 # 그로스 레버리지 상한
        else:
            tot = float(w.abs().sum())
            w = w / tot * lev if tot > 0 else w        # sum|w| = leverage
        if pos.overlays.max_group_pct is not None and group_panel is not None and d in group_panel.index:
            w = _cap_groups(w, group_panel.loc[d], float(pos.overlays.max_group_pct))
        nav = cash + sum(positions[x].shares * close[x][i] for x in positions
                         if not np.isnan(close[x][i]))
        if nav <= 0:
            return {}
        if pos.overlays.turnover_damp:                 # 작은 비중 변화 억제(턴오버↓)
            thr = float(pos.overlays.turnover_damp)
            cur_w = {x: positions[x].shares * close[x][i] / nav
                     for x in positions if not np.isnan(close[x][i])}
            for s_ in list(w.index):
                if abs(float(w[s_]) - cur_w.get(s_, 0.0)) < thr:
                    w[s_] = cur_w.get(s_, 0.0)
        target = {}
        for s_, wt in w.items():
            px = close[s_][i]
            if px and px > 0 and not np.isnan(px):
                target[s_] = int(np.sign(wt) * np.floor(abs(wt) * nav / px))
        return target

    def _refill(target: dict, reasons: dict, i: int, d, excluded: set):
        """replace 모드 — 청산으로 빈 슬롯을 현재 score 차순위 자격 후보로 충원."""
        a = alpha.loc[d].dropna().sort_values(ascending=False)
        held = set(positions.keys())
        cands = [s for s in a.index if s not in held and s not in excluded]
        ci = 0
        for s_exit in reasons:
            freed = positions[s_exit].shares * close[s_exit][i]
            if np.isnan(freed) or freed <= 0:
                continue
            while ci < len(cands):
                repl = cands[ci]
                ci += 1
                px = close[repl][i]
                if px and px > 0 and not np.isnan(px):
                    target[repl] = int(np.floor(freed / px))
                    break

    excluded: set = set()
    for i in range(n):
        d = master[i]
        if rfr_daily and cash > 0:                     # 현금 무위험수익
            cash *= (1 + rfr_daily)
        if (borrow_daily or funding_daily) and positions:   # 숏 차입·레버리지 펀딩
            sv = sum(abs(p.shares) * close[s][i] for s, p in positions.items()
                     if p.shares < 0 and not np.isnan(close[s][i]))
            # 증거금 요구액 Σ(margin_rate×노티오널). 선물은 부분증거금이라 nav를 넘는
            # 부분만 현금 차입 → 그만큼만 펀딩. 현금주식(rate=1)이면 gross 그대로(기존 동일).
            margin_req = sum(margin_rate(s) * abs(p.shares) * close[s][i]
                             for s, p in positions.items() if not np.isnan(close[s][i]))
            navv = cash + sum(p.shares * close[s][i] for s, p in positions.items()
                              if not np.isnan(close[s][i]))
            cash -= sv * borrow_daily + max(0.0, margin_req - navv) * funding_daily
        if pending is not None and defer:
            _execute(pending[0], i, pending[1])
            pending = None
        mc_target = None                               # 마진콜 — EOD 자기자본비율(nav/gross)<유지율
        if maint and positions:
            navv = cash + sum(p.shares * close[s][i] for s, p in positions.items()
                              if not np.isnan(close[s][i]))
            grossv = sum(abs(p.shares) * close[s][i] for s, p in positions.items()
                         if not np.isnan(close[s][i]))
            if grossv > 0 and navv < maint * grossv:   # 목표 레버리지(nav×L)로 강제 복원(nav≤0이면 전량)
                scale = min(1.0, max(0.0, navv * lev / grossv)) if navv > 0 else 0.0
                mc_target = {s: int(positions[s].shares * scale) for s in positions}
        if mc_target is not None:
            target, reasons, act = mc_target, {s: "마진콜" for s in positions}, True
        elif _is_rebalance(i, last, master, period, ent.every_n_days):
            excluded = set()
            target, reasons = _compute_target(i, d), {}
            last = i
            act = True
        else:
            target = {s: positions[s].shares for s in positions}   # 보유 유지
            reasons = {}
            for s in list(positions.keys()):
                cl, hi = close[s][i], high[s][i]
                av = atr[s][i] if atr[s] is not None else np.nan
                p = positions[s]
                if not np.isnan(hi) and hi > p.peak_high:
                    p.peak_high = float(hi)
                if not np.isnan(cl) and cl > p.peak_close:
                    p.peak_close = float(cl)
                if np.isnan(cl):
                    continue
                r = _exit_reason(p, i, cl, hi, av, sell_arr[s], exits,
                                 sign=1.0 if p.shares > 0 else -1.0)
                if r:
                    target[s] = 0
                    reasons[s] = r
                    excluded.add(s)
            if reasons and refill_mode == "replace":
                _refill(target, reasons, i, d, excluded)
            act = bool(reasons)
        if act:
            if defer:
                pending = (target, reasons)
            else:
                _execute(target, i, reasons)
        nav = cash
        for s, p in positions.items():
            cl = close[s][i]
            nav += p.shares * (cl if not np.isnan(cl) else p.entry_price)
        equity[i] = nav

    eq = pd.Series(equity, index=master)
    ov = pos.overlays
    if ov.vol_target or ov.max_drawdown_stop:          # 포트폴리오 노출 overlay(수익률 레벨)
        ret = eq.pct_change().fillna(0.0)
        if ov.vol_target:
            real = ret.rolling(63).std() * np.sqrt(TRADING_DAYS)
            scale = (((ov.vol_target / 100.0) / real.replace(0, np.nan))
                     .shift(1).clip(upper=3.0).fillna(1.0))
            ret = ret * scale
        if ov.max_drawdown_stop:
            ret = _apply_dd_control(ret, ov.max_drawdown_stop, ov.max_drawdown_soft)
        eq = (1 + ret).cumprod() * sim.initial_capital
    equity_s = eq.rename("전략")
    benchmark_s = _benchmark_cols(close, master, sim.initial_capital)
    trades_df = pd.DataFrame(trades)
    met = finalize_metrics(_metrics(equity_s, benchmark_s, trades_df),
                           equity_s, benchmark_s, trades_df)
    eq_mean = float(equity_s.mean())
    met["turnover"] = float(turnover_notional / n / eq_mean * 100) if eq_mean > 0 else 0.0
    return {"success": True, "error": None, "equity": equity_s, "benchmark": benchmark_s,
            "trades": trades_df, "metrics": met}


def _benchmark_cols(close: dict, master, initial_capital: float) -> pd.Series:
    """유니버스 동일가중 buy&hold 벤치 (스케줄 경로 — close dict 기반)."""
    bench = np.zeros(len(master), dtype=float)
    w = initial_capital / max(1, len(close))
    for s, arr in close.items():
        ser = pd.Series(arr, index=master).ffill().bfill()
        first = ser.iloc[0] if not ser.empty else 0
        if first and first > 0:
            bench += (ser / first * w).to_numpy()
    return pd.Series(bench, index=master, name="Buy&Hold")

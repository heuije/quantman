"""장중 stop loss — KIS WebSocket tick 기반 즉각 매도 발동.

Phase 32: 매도/청산이 일원화된 sell_rules의 가격 기반 트리거(익절/손절/트레일링/
ATR 트레일링)를 장중 실시간으로 평가한다. tick이 들어올 때마다 다음 우선순위로
평가하고 트리거 발생 시 즉시 KIS 매도 발주:

  1. 익절 (cur ≥ entry × (1 + tp%))
  2. 손절 (cur ≤ entry × (1 + sl%))   sl은 음수
  3. 트레일링 % (cur ≤ peak × (1 - trail%))
  4. ATR 트레일링 (cur ≤ peak - atr × mult)

보유 기간·매도 조건(dataset 기반)은 매일 사이클에서 평가 — 여기선 가격 기반만.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

import quant_core as qc
from quant_core.exec_defaults import merged_execution

log = logging.getLogger("localapp.intraday_stop")


def evaluate_price_trigger(sr: "qc.SellRules", cur_price: float,
                            entry_price: float, peak_price: float,
                            atr_14: float | None = None) -> str | None:
    """tick 가격에 대해 매도 트리거 평가. 사유 문자열 또는 None.

    가격 기반 4개 규칙만 — 보유 기간·매도 조건은 EOD 사이클이 담당.
    """
    if entry_price <= 0 or cur_price <= 0:
        return None
    cur_ret = (cur_price - entry_price) / entry_price * 100

    if sr.take_profit is not None and cur_ret >= sr.take_profit:
        return "익절(intraday)"
    if sr.stop_loss is not None and cur_ret <= sr.stop_loss:
        return "손절(intraday)"
    if (sr.trail_pct is not None and peak_price > 0
            and cur_price <= peak_price * (1 - sr.trail_pct / 100)):
        return "트레일링스톱(intraday)"
    if (sr.trail_atr_mult is not None and peak_price > 0
            and atr_14 is not None and atr_14 > 0
            and cur_price <= peak_price - atr_14 * sr.trail_atr_mult):
        return "ATR트레일링(intraday)"
    return None


class IntradayStopManager:
    """보유 포지션의 장중 stop 평가·발주 매니저.

    WebSocket 콜백(`on_tick`)이 종목·가격을 받아 평가하고 트리거 시 매도 발주.
    재진입 회피를 위해 한 사이클(=하루) 안에 같은 ledger_key를 두 번 매도하지 않는다.
    """

    def __init__(self, broker, get_ledger: Callable[[], dict],
                 get_strat_def: Callable[[str], dict | None],
                 submit_sell_fn: Callable[..., None],
                 dataset: dict | None = None):
        """
        Args:
            broker: KIS broker (price/sell_limit/account_snapshot)
            get_ledger: ledger dict {ledger_key: {symbol, qty, entry_price, peak_price, ...}} 반환
            get_strat_def: strategy_id로 strat_def dict 조회
            submit_sell_fn: 매도 발주 함수 — signature (ledger_key, strat_name, symbol, qty, ref_price, policy, reason, decisions)
            dataset: ATR 트레일링용 (atr_14 lookup)
        """
        self.broker = broker
        self._get_ledger = get_ledger
        self._get_strat_def = get_strat_def
        self._submit_sell = submit_sell_fn
        self.dataset = dataset or {}
        self._sold_today: set[str] = set()
        self._lock = threading.Lock()
        self.decisions: list[dict] = []   # 누적 매도 결정 로그

    def _atr14_of(self, symbol: str) -> float | None:
        df = self.dataset.get(symbol)
        if df is None or "atr_14" not in getattr(df, "columns", []):
            return None
        try:
            v = float(df["atr_14"].iloc[-1] or 0.0)
            return v if v > 0 else None
        except Exception:
            return None

    def on_tick(self, symbol: str, price: float) -> None:
        """WebSocket tick callback. 가격 변동마다 호출됨.

        보유 종목 중 해당 symbol을 가진 모든 ledger entry 평가 → 트리거 시 매도.
        """
        if price <= 0:
            return
        with self._lock:
            ledger = self._get_ledger()
            atr_val = self._atr14_of(symbol)
            for ledger_key, pos in list(ledger.items()):
                if pos.get("symbol") != symbol:
                    continue
                if ledger_key in self._sold_today:
                    continue

                strat_def = self._get_strat_def(pos.get("strategy_id", ""))
                if strat_def is None:
                    continue

                # peak_price 갱신 (트레일링용)
                peak = max(float(pos.get("peak_price") or pos.get("entry_price") or 0),
                           price)
                pos["peak_price"] = peak

                # sell_rules 추출 — qc.Strategy로 변환해 _migrate_legacy 거치게
                try:
                    strat = qc.Strategy(**strat_def)
                    sr = strat.sell_rules
                except Exception as e:
                    log.warning("strat 파싱 실패 [%s]: %s", ledger_key, e)
                    continue

                reason = evaluate_price_trigger(
                    sr, price, float(pos.get("entry_price") or 0), peak, atr_val)
                if reason is None:
                    continue

                # 트리거! 매도 발주
                policy = merged_execution(strat_def.get("execution"))
                qty = int(pos.get("qty") or 0)
                if qty <= 0:
                    continue
                strat_name = pos.get("strategy_name", "")
                try:
                    self._submit_sell(
                        ledger_key, strat_name, symbol, qty, price,
                        policy, reason, self.decisions)
                    self._sold_today.add(ledger_key)
                    log.info("[intraday-stop] %s 매도 발주: %s @ %s원 (사유 %s)",
                              symbol, qty, price, reason)
                except Exception as e:
                    log.error("[intraday-stop] %s 매도 발주 실패: %s", symbol, e)

    def reset_daily(self) -> None:
        """매일 시작 시 호출 — 'sold today' 셋 초기화."""
        with self._lock:
            self._sold_today.clear()
            self.decisions.clear()

    def held_symbols(self) -> set[str]:
        """현재 보유 종목 코드 셋 — WebSocket 구독 갱신용."""
        ledger = self._get_ledger()
        return {pos.get("symbol") for pos in ledger.values() if pos.get("symbol")}

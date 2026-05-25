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
import time
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

        # L-04: KIS 실 잔고 TTL 캐시. tick마다 account_snapshot을 부르면 rate limit
        # 압박 → 60초 TTL로 캐시. 캐시 미스 시 1회 호출, 실패하면 None 반환.
        self._snap_cache: dict | None = None
        self._snap_cache_ts: float = 0.0
        self._snap_ttl: float = 60.0

        # Q5 Tier 2 — 장중 kill switch monitor. start_monitor() 호출 시 활성화.
        # period: 60초(AL-2). on_trigger: ks 발동 시 호출되는 외부 핸들러
        # (intraday_loop이 cycle 호출/push를 담당).
        self._ks_monitor_thread: threading.Thread | None = None
        self._ks_stop_flag: threading.Event | None = None
        self._ks_daily_loss_limit_pct: float | None = None
        self._ks_on_trigger: Callable[[], None] | None = None

    def _atr14_of(self, symbol: str) -> float | None:
        df = self.dataset.get(symbol)
        if df is None or "atr_14" not in getattr(df, "columns", []):
            return None
        try:
            v = float(df["atr_14"].iloc[-1] or 0.0)
            return v if v > 0 else None
        except Exception:
            return None

    def _broker_qty_of(self, symbol: str) -> int | None:
        """KIS 실 잔고 보유수량 (TTL 캐시). 모르면 None(스냅샷 실패 + 캐시 없음).

        L-04: 장중 사용자가 HTS/MTS에서 수동 매도한 경우 ledger는 그대로지만 KIS
        잔고는 0. ledger 기반으로 매도 발주하면 over-sell(KIS reject 또는 short-sell)
        → 사고. 발주 직전 broker 실 보유로 클램프.
        """
        now = time.monotonic()
        if self._snap_cache is None or (now - self._snap_cache_ts) > self._snap_ttl:
            try:
                self._snap_cache = self.broker.account_snapshot()
                self._snap_cache_ts = now
            except Exception as e:
                log.warning("account_snapshot 실패 — 캐시 유지: %s", e)
                if self._snap_cache is None:
                    return None  # 알 수 없음 → 호출부는 안전하게 skip
        total = 0
        for p in self._snap_cache.get("positions", []):
            if p.get("symbol") == symbol:
                total += int(p.get("qty") or 0)
        return total

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

                # L-04: over-sell 방지 — KIS 실 잔고로 클램프.
                # 사용자가 장중 HTS/MTS에서 수동 매도했어도 ledger엔 잔존 가능.
                bqty = self._broker_qty_of(symbol)
                if bqty is None:
                    # 스냅샷 조회 실패 + 캐시 없음 → 다음 tick에 재시도(skip 1회).
                    log.warning("[intraday-stop] %s broker 잔고 미상 — 1tick skip",
                                symbol)
                    continue
                if bqty <= 0:
                    # 외부에서 이미 매도됨 → ledger orphan. 오늘은 더 시도하지 않음.
                    # 15:35 reconcile_with_kis가 ledger 자동 정리.
                    log.info("[intraday-stop] %s broker 보유 0 (외부 매도 추정) — "
                             "오늘 추가 시도 skip (사유 %s)", symbol, reason)
                    self._sold_today.add(ledger_key)
                    continue
                if bqty < qty:
                    log.info("[intraday-stop] %s qty 클램프 ledger=%d → broker=%d",
                             symbol, qty, bqty)
                    qty = bqty

                # Phase 56 — 매도 룰별 sell_pct (intraday tp/sl/trail/atr).
                sell_pct = qc.sell_pct_for_reason(sr, reason)
                sell_qty = max(1, int(qty * sell_pct / 100.0))
                sell_qty = min(sell_qty, qty)

                strat_name = pos.get("strategy_name", "")
                try:
                    self._submit_sell(
                        ledger_key, strat_name, symbol, sell_qty, price,
                        policy, reason, self.decisions)
                    self._sold_today.add(ledger_key)
                    log.info("[intraday-stop] %s 매도 발주: %s @ %s원 (사유 %s)",
                              symbol, sell_qty, price, reason)
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

    # ── Q5 Tier 2: 장중 kill switch monitor ─────────────────────────────────
    def start_monitor(self, daily_loss_limit_pct: float,
                      on_trigger: Callable[[], None],
                      period_sec: float = 60.0) -> None:
        """장중 kill switch monitor thread 시작.

        period_sec(기본 60초, AL-2)마다 account_snapshot으로 통합 자본 평가 →
        day_start 대비 -daily_loss_limit_pct% 도달 시 on_trigger 호출.

        on_trigger는 intraday_loop이 제공: trader.cancel_all_pending + 빈 cycle
        재호출(청산 패스) + 서버 push를 담당.

        snap_cache(L-04)와 cache_ts를 공유 — 60초 TTL이라 모니터 평가용으로도 사용.
        """
        if self._ks_monitor_thread and self._ks_monitor_thread.is_alive():
            log.info("[ks-monitor] 이미 실행 중")
            return
        self._ks_daily_loss_limit_pct = float(daily_loss_limit_pct)
        self._ks_on_trigger = on_trigger
        self._ks_stop_flag = threading.Event()
        t = threading.Thread(target=self._ks_monitor_loop, daemon=True,
                              name="ks-monitor",
                              kwargs={"period_sec": period_sec})
        self._ks_monitor_thread = t
        t.start()
        log.info("[ks-monitor] 시작 — period=%.0fs, limit=-%.2f%%",
                  period_sec, daily_loss_limit_pct)

    def stop_monitor(self) -> None:
        """장 마감(15:30) 시 호출. monitor thread 종료 대기."""
        sf = self._ks_stop_flag
        if sf is not None:
            sf.set()
        t = self._ks_monitor_thread
        if t is not None and t.is_alive():
            t.join(timeout=5)
        self._ks_monitor_thread = None
        self._ks_stop_flag = None

    def _ks_monitor_loop(self, period_sec: float) -> None:
        """모니터 루프 — period_sec 주기로 _evaluate_once. 예외는 로그만 남기고 계속.

        의도적으로 _broker_qty_of와 같은 캐시(self._snap_cache)를 갱신하므로 tick
        핸들러도 신선한 잔고를 본다. 단, snap 호출 자체는 60초 TTL이라 rate limit
        압박 약함.
        """
        sf = self._ks_stop_flag
        limit = self._ks_daily_loss_limit_pct
        on_trigger = self._ks_on_trigger
        if sf is None or limit is None or on_trigger is None:
            log.warning("[ks-monitor] 초기화 누락 — 종료")
            return
        # 시작 직후 즉시 1회 평가는 하지 않음 (cycle이 방금 평가했을 가능성).
        while not sf.wait(period_sec):
            try:
                self._ks_evaluate_once(limit, on_trigger)
            except Exception as e:
                log.error("[ks-monitor] 평가 예외: %s", e)
        log.info("[ks-monitor] 종료")

    def _ks_evaluate_once(self, daily_loss_limit_pct: float,
                          on_trigger: Callable[[], None]) -> bool:
        """1회 평가. 발동되면 on_trigger 호출하고 monitor는 그대로 계속(중복 발동은
        killswitch.is_active 게이트가 막음). 반환: 발동 여부.
        """
        from . import killswitch
        if killswitch.is_active():
            return False
        # snap 캐시 강제 갱신 — monitor는 60초 주기라 캐시 TTL과 일치
        now = time.monotonic()
        if self._snap_cache is None or (now - self._snap_cache_ts) > self._snap_ttl:
            try:
                self._snap_cache = self.broker.account_snapshot()
                self._snap_cache_ts = now
            except Exception as e:
                log.warning("[ks-monitor] snap 실패 — 다음 주기 재시도: %s", e)
                return False
        bal = self._snap_cache.get("balance", {})
        equity = _ks_unified_equity_krw(bal)
        reason = killswitch.check_daily_loss(equity, daily_loss_limit_pct)
        if not reason:
            return False
        killswitch.activate(reason)
        log.critical("[ks-monitor] kill switch 발동: %s", reason)
        try:
            on_trigger()
        except Exception as e:
            log.error("[ks-monitor] on_trigger 핸들러 실패: %s", e)
        return True


def _ks_unified_equity_krw(bal: dict) -> float:
    """trader._unified_equity_krw와 동일 로직 (순환 import 회피용 사본).

    국내 평가 + 해외 평가(KRW) + USD 현금(KRW 환산).
    """
    dom = float(bal.get("total_eval", 0) or 0)
    foreign = float(bal.get("foreign_eval_krw", 0) or 0)
    usd_cash = float(bal.get("cash_usd", 0) or 0)
    fx = float(bal.get("fx_usdkrw", 0) or 0)
    return dom + foreign + usd_cash * fx

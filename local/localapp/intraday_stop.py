"""장중 stop loss — KIS WebSocket tick 기반 즉각 매도 발동.

IR position.exit의 가격 기반 트리거(익절/손절/트레일링/ATR 트레일링)를 장중
실시간으로 평가한다. tick이 들어올 때마다 다음 우선순위로 평가하고 트리거
발생 시 즉시 KIS 매도 발주:

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

from quant_core.exec_defaults import merged_execution
from quant_core.ir_engine import StrategyIR
from quant_core.ir_engine import live as ir_live

log = logging.getLogger("localapp.intraday_stop")


class IntradayStopManager:
    """보유 포지션의 장중 stop 평가·발주 매니저.

    WebSocket 콜백(`on_tick`)이 종목·가격을 받아 평가하고 트리거 시 매도 발주.
    재진입 회피를 위해 한 사이클(=하루) 안에 같은 ledger_key를 두 번 매도하지 않는다.
    """

    def __init__(self, broker, get_ledger: Callable[[], dict],
                 submit_sell_fn: Callable[..., None],
                 dataset: dict | None = None):
        """
        Args:
            broker: KIS broker (price/sell_limit/account_snapshot)
            get_ledger: ledger dict {ledger_key: {symbol, qty, entry_price, peak_price, definition, ...}} 반환
            submit_sell_fn: 매도 발주 함수 — signature (ledger_key, strat_name, symbol, qty, ref_price, policy, reason, decisions)
            dataset: ATR 트레일링용 (atr_14 lookup)
        """
        self.broker = broker
        self._get_ledger = get_ledger
        self._submit_sell = submit_sell_fn
        # 이 매니저가 **실제로 브로커에 접수시킨** 손절 매도 건수. decisions 증가분은
        # 발주 수의 대용이 될 수 없다 — 접수됐지만 미체결이면 decision이 없고,
        # 멱등 차단·예외·거부는 decision을 남긴다(양방향 오집계).
        self.submitted_count = 0
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
        except (ValueError, TypeError, IndexError):
            # 데이터 형식/결측만 흡수(ATR 트레일 1종만 skip, 나머지 stop은 평가).
            # KeyError·AttributeError 등 코드 결함은 전파해 silent 무력화를 막는다.
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
        from .trader import held_qty_from_snapshot
        return held_qty_from_snapshot(self._snap_cache, symbol)

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

                # 전략 정의는 원장 엔트리에 자기완결로 저장됨(_apply_fill) — EOD 청산
                # 경로(_cycle_body)와 동일하게 pos["definition"]을 직접 쓴다. strategy_id
                # 재조회는 엔트리에 그 키가 없어 빈값으로 실패하던 취약 경로였음.
                strat_def = pos.get("definition")
                if not strat_def:
                    continue

                # peak_price 갱신 (트레일링용)
                peak = max(float(pos.get("peak_price") or pos.get("entry_price") or 0),
                           price)
                pos["peak_price"] = peak

                # 청산 사유 — IR(전략 연구소) position.exit의 가격기반 4규칙을
                # 엔진 임계 공식(intraday_exit_reason)으로 평가한다.
                entry_price = float(pos.get("entry_price") or 0)
                try:
                    ir = StrategyIR.model_validate(strat_def)
                except Exception as e:
                    log.warning("IR strat 파싱 실패 [%s]: %s", ledger_key, e)
                    continue
                reason = ir_live.intraday_exit_reason(
                    ir, cur_price=price, entry_price=entry_price,
                    peak_price=peak, atr_v=atr_val)
                if reason is None:
                    continue

                # 트리거! 매도 발주
                policy = merged_execution(strat_def.get("execution"))
                qty = int(pos.get("qty") or 0)
                if qty <= 0:
                    continue

                # L-04: over-sell 방지 — KIS 실 잔고로 클램프.
                # 사용자가 장중 HTS/MTS에서 수동 매도했어도 ledger엔 잔존 가능.
                from .trader import clamp_sell_qty
                clamped = clamp_sell_qty(self._broker_qty_of(symbol), qty)
                if clamped is None:
                    # 스냅샷 조회 실패 + 캐시 없음 → 다음 tick에 재시도(skip 1회).
                    log.warning("[intraday-stop] %s broker 잔고 미상 — 1tick skip",
                                symbol)
                    continue
                if clamped <= 0:
                    # 외부에서 이미 매도됨 → ledger orphan. 오늘은 더 시도하지 않음.
                    # 15:50 reconcile_with_kis가 ledger 자동 정리.
                    log.info("[intraday-stop] %s broker 보유 0 (외부 매도 추정) — "
                             "오늘 추가 시도 skip (사유 %s)", symbol, reason)
                    self._sold_today.add(ledger_key)
                    continue
                if clamped < qty:
                    log.info("[intraday-stop] %s qty 클램프 ledger=%d → broker=%d",
                             symbol, qty, clamped)
                qty = clamped

                # IR position.exit은 per-rule 매도 비중이 없으므로 전량(100%) 청산.
                sell_qty = qty

                strat_name = pos.get("strategy_name", "")
                try:
                    # 반환값 = 브로커가 실제로 주문을 받았나. 멱등 차단·거부는 False라
                    # 발주 건수로 세지 않는다(catch-up 배너가 이 수를 유저에게 보고).
                    placed = self._submit_sell(
                        ledger_key, strat_name, symbol, sell_qty, price,
                        policy, reason, self.decisions)
                    self._sold_today.add(ledger_key)
                    if placed:
                        self.submitted_count += 1
                    log.info("[intraday-stop] %s 매도 %s: %s @ %s원 (사유 %s)",
                              symbol, "발주" if placed else "미발주(차단·거부)",
                              sell_qty, price, reason)
                except Exception as e:
                    log.error("[intraday-stop] %s 매도 발주 실패: %s", symbol, e)

    def reset_daily(self) -> None:
        """매일 시작 시 호출 — 'sold today' 셋 초기화."""
        with self._lock:
            self._sold_today.clear()
            self.decisions.clear()
            self.submitted_count = 0

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
    """trader._unified_equity_krw 위임 — 통합자산(KRW) 단일 출처 (US-F2).

    과거엔 '동일 로직 사본'이었으나 trader 쪽이 선물 평가(futures_eval_krw)를 합산하도록
    갱신될 때 이 사본이 따라가지 못해, 장중 kill-switch monitor가 선물 손익을 무시하던
    drift가 생겼다(국내·해외 선물 공통). 중복 제거가 근본 해결 — 함수 내 lazy import로
    순환을 피한다(이 모듈의 held_qty_from_snapshot/clamp_sell_qty와 동일 패턴)."""
    from .trader import _unified_equity_krw
    return _unified_equity_krw(bal)

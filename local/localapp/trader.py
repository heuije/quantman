"""모의투자 트레이딩 로직 (Phase 9 best practices 적용).

핵심 변경 사항:
- 시장가 → 지정가 + price tolerance (어제 종가 기준 한도)
- 갭 필터 (전일 종가 vs 현재가 갭 > 임계값이면 신규 진입 폐기)
- ATR 변동성 보정 포지션 사이징 (atr_risk 모드)
- 일일 손실 한도 + kill switch (자본 대비 −3% 도달 시 자동 청산 + 차단)
- 라이브 슬리피지 측정 (의도가 vs 체결가 bps 누적)

청산 우선순위는 백테스트와 동일: 익절 → 손절 → 트레일링 → 보유기간 → 매도신호.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import quant_core as qc
from quant_core.exec_defaults import merged_execution

from .broker import Broker
from .config import (EQUITY_PATH, LEDGER_PATH, PENDING_ORDERS_PATH,
                     REBALANCE_PATH, TRADES_PATH)
from . import analytics, intents, killswitch, order_log

log = logging.getLogger("localapp.trader")

# Q5(AL-4): cycle ↔ settlement ↔ 장중 kill switch trigger의 직렬화 락.
# 모듈 레벨로 두는 이유: trader 인스턴스가 cycle/settlement에서 매번 새로
# 만들어지므로 인스턴스 lock으로는 직렬화가 안 된다. 같은 PC 단일 프로세스
# 가정이라 모듈 락이 안전. 모든 진입(trader.cycle, run_post_close_settlement,
# intraday monitor의 trigger 핸들러)이 이 락을 acquire 후 진입한다.
_CYCLE_LOCK = threading.Lock()


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("파일 파싱 실패, 기본값 사용: %s", path)
    return default


def _save_json(path: Path, obj) -> None:
    """원자적 저장: tmp에 쓰고 os.replace로 교체 (L-02 수정).

    write_text는 truncate-then-write라 도중에 크래시하면 파일이 깨진 채 남고
    다음 boot에서 _load_json이 default={}로 폴백 → 보유 원장 소실 위험.
    tmp 파일에 완전히 쓴 뒤 os.replace로 원자 교체하면, 어떤 시점에 종료돼도
    파일은 항상 이전 완전본 또는 새 완전본 중 하나만 보인다.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def kst_today() -> date:
    """현재 KST 날짜 — 사용자 PC tz와 무관하게 한국 시장 기준 (L-06 수정).

    원장·intent·체결 dedup의 'today' 키가 PC tz에 따라 달라지면 한국장 거래일이
    어긋난다(여행/해외 거주 사용자). 명시적으로 KST 환산.
    """
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def _policy(strat_def: dict) -> dict:
    """전략 정의에서 ExecutionPolicy를 추출하고 글로벌 default와 병합."""
    return merged_execution(strat_def.get("execution") if strat_def else None)


def _currency_of(symbol: str) -> str:
    """결제 통화 — 미국 종목이면 USD, 그 외 KRW. 사이징·틱·잔고 단위 결정."""
    from . import market_index
    return "USD" if market_index.is_us(symbol) else "KRW"


def _market_group_safe(symbol: str) -> str:
    """시장 그룹('US'|'KRX') — 라우팅 불확실 시 국내 기본(안전)."""
    from . import market_index
    try:
        return market_index.market_group_of(symbol)
    except Exception:
        return "KRX"


def _unified_equity_krw(bal: dict) -> float:
    """국내+해외 통합 자산(KRW) — kill switch·drawdown용 계좌 전체 equity.

    국내 평가금액 + 외화 평가총액(KRW) + USD 예수금(KRW 환산).
    """
    dom = float(bal.get("total_eval", 0) or 0)
    foreign = float(bal.get("foreign_eval_krw", 0) or 0)
    usd_cash = float(bal.get("cash_usd", 0) or 0)
    fx = float(bal.get("fx_usdkrw", 0) or 0)
    return dom + foreign + usd_cash * fx


def _gap_pct(prev_close: float, cur_price: float) -> float:
    """갭 % (양수 = 갭상승, 음수 = 갭하락)."""
    if prev_close <= 0:
        return 0.0
    return (cur_price - prev_close) / prev_close * 100


def _evaluate_exit(strat: qc.Strategy, held_days: int,
                   dataset: dict, symbol: str) -> str | None:
    """EOD 매도 평가 — 신호·시간 기반만. 가격 기반(익절/손절/트레일)은
    intraday_loop가 장중 tick으로 전담 (Phase 38.2).

    매도 발주는 어제 종가 기준 지정가로 나가므로, 갭 발생 시 EOD 가격
    트리거를 다시 평가해도 미체결 — 09:00 정규장 시작 후 intraday가 즉시
    같은 평가로 처리하는 게 정상 경로. 여기선 신호/시간 기반만 평가해
    KIS 현재가 호출과 이중 발주 제거.

    Phase 41 — 매도 조건의 [이 종목] placeholder는 symbol(현재 보유 종목)로
    치환되어 평가된다. 좌변 종목이 명시되어 있으면 그대로 (legacy 호환).
    """
    sr = strat.sell_rules
    if sr.hold_days is not None and held_days >= sr.hold_days:
        return "보유기간"
    if sr.conditions:
        mask = qc.build_signal_mask(
            dataset, [c.model_dump() for c in sr.conditions], sr.logic,
            current_symbol=symbol)
        if not mask.empty and bool(mask.iloc[-1]):
            return "매도조건"
    return None


def _business_days_between(start: date, end: date) -> int:
    """start 다음날부터 end까지의 영업일(월~금) 수. end ≤ start면 0."""
    if end <= start:
        return 0
    n = 0
    d = start
    while d < end:
        d = d + timedelta(days=1)
        if d.weekday() < 5:               # 0=월 … 4=금
            n += 1
    return n


def _rebalance_due(period: str, last_iso: str | None, today: date,
                   every_n_days: int | None = None) -> bool:
    """리밸런싱 주기 게이팅 — 마지막 실행 일자 기준으로 오늘 회전할지 판정.

    last_iso=None(첫 실행)이면 항상 True. daily=날짜 바뀌면, weekly=ISO주 바뀌면,
    monthly=월 바뀌면, every_n_days=마지막 실행 후 영업일 ≥ N이면 True.
    같은 날 중복 실행은 항상 막는다.
    """
    if last_iso is None:
        return True
    try:
        last = date.fromisoformat(last_iso)
    except (TypeError, ValueError):
        return True
    if today <= last:
        return False                      # 같은 날(또는 과거) — 중복 방지
    if period == "weekly":
        return today.isocalendar()[:2] != last.isocalendar()[:2]
    if period == "monthly":
        return (today.year, today.month) != (last.year, last.month)
    if period == "every_n_days":
        n = every_n_days if (every_n_days and every_n_days > 0) else 1
        return _business_days_between(last, today) >= n
    return True                           # daily (기본) — 날짜만 바뀌면


def _atr_qty(capital: float, atr: float, policy: dict, cur_price: float) -> int:
    """ATR 기반 포지션 사이징. cap에 의해 단일종목 한도로 클램프(설정된 경우만)."""
    risk = capital * policy["atr_risk_pct"] / 100.0
    risk_per_share = atr * policy["atr_mult"]
    if risk_per_share <= 0:
        return 0
    qty = int(risk // risk_per_share)
    # 단일 종목 비중 상한 — None이면 한도 없음(OFF).
    mp = policy.get("max_position_pct")
    if mp is not None and cur_price > 0:
        cap_qty = int((capital * float(mp) / 100.0) // cur_price)
        qty = min(qty, cap_qty)
    return max(0, qty)


class Trader:
    """Broker에 의존하는 모의투자 실행기. 보유 원장을 로컬에 유지한다.

    원장 항목은 전략 정의를 함께 보관하므로, 플랫폼에서 전략이 삭제돼도
    (고아 포지션) 저장된 규칙으로 안전하게 청산할 수 있다.
    """

    def __init__(self, broker: Broker):
        self.broker = broker
        self.ledger: dict[str, dict] = _load_json(LEDGER_PATH, {})
        self.equity: list[dict] = _load_json(EQUITY_PATH, [])
        self.pending: dict[str, dict] = _load_json(PENDING_ORDERS_PATH, {})
        # 전략별 마지막 리밸런싱 일자 (sid → "YYYY-MM-DD") — 주기 게이팅
        self.rebalance_state: dict[str, str] = _load_json(REBALANCE_PATH, {})
        # 미국 매수여력 모드 (cycle에서 risk_limits로 설정). 기본 통합증거금.
        self._us_bp_mode: str = "integrated"
        # Q5: 체결 후(_apply_fill) 즉시 kill switch 평가용 한도. cycle 진입 시
        # risk_limits에서 채워진다. 호출자가 설정 안 했으면 평가 skip(보수적 무동작).
        self._daily_loss_limit_pct: float | None = None
        # Q5: kill switch 발동 시 추가 동작을 외부에 알리는 hook (intraday_loop이
        # 보유 종목 강제 청산 cycle을 트리거하도록). None이면 발동만 기록.
        self._ks_trigger_hook = None
        # Q5(데드락 방지): cycle 진입 중 플래그. _apply_fill의 ks 평가/hook 호출은
        # cycle 외부에서만 동작 — cycle 내부의 _apply_fill(이전 미체결 정리,
        # _wait_pending 폴링)에서 hook이 cycle을 재호출하면 _CYCLE_LOCK 데드락 +
        # 무한 재귀 위험. cycle은 진입부에서 이미 ks를 평가하므로 중복 평가 불필요.
        # 진짜 필요 케이스는 intraday_loop의 _on_exec_event(별 thread).
        self._in_cycle = False

    # ── 영속화 ────────────────────────────────────────────────────────────────

    def _save(self):
        # L-02: 4파일 모두 원자적 저장 (_save_json은 tmp+os.replace 패턴).
        # 4파일 cross-consistency는 여전히 미보장(파일별 원자성만)이지만, 부분
        # truncate에 의한 원장 소실은 차단된다.
        _save_json(LEDGER_PATH, self.ledger)
        _save_json(EQUITY_PATH, self.equity)
        _save_json(PENDING_ORDERS_PATH, self.pending)
        _save_json(REBALANCE_PATH, self.rebalance_state)

    def _log_trade(self, event: dict):
        with open(TRADES_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    # ── Phase 40 — KIS 잔고 ↔ ledger 정합성 자동 정정 ──────────────────────
    def reconcile_with_kis(self, today_iso: str | None = None) -> dict:
        """KIS 실 잔고와 ledger를 비교 → ledger_orphans는 자동 차감/제거.

        external_extras(외부 매수)는 ledger 손대지 않음 (자동매매가 매수한 게 아니므로).
        반환: reconcile dict + applied 변경 내역 + 거래 기록 카운트.

        호출 시점: 15:35 post_close_settlement (08:55 메인 사이클 직전엔 위험).
        """
        today = today_iso or kst_today().isoformat()
        try:
            snap = self.broker.account_snapshot()
        except Exception as e:
            log.error("reconcile: KIS 잔고 조회 실패 — skip: %s", e)
            return {"error": f"KIS 잔고 조회 실패: {e}"}

        result = analytics.reconcile_ledger(snap.get("positions", []), self.ledger)
        orphans = result.get("ledger_orphans", [])
        applied: list[dict] = []

        if orphans:
            plans = analytics.plan_orphan_adjustments(orphans)
            for p in plans:
                sid = p["sid"]
                if sid not in self.ledger:
                    continue
                lg = self.ledger[sid]
                removed = p["removed_qty"]
                if removed <= 0:
                    continue
                # 거래 기록: 외부 매도로 분류
                ev = {
                    "ts": today, "action": "external_close",
                    "symbol": p["symbol"], "qty": removed,
                    "price": float(lg.get("entry_price", 0) or 0),
                    "strategy": lg.get("strategy_name", ""),
                    "reason": "HTS/MTS 수동 매도 추정 — reconcile 자동 차감",
                    "sid": sid,
                }
                self._log_trade(ev)
                if p["fully_closed"]:
                    del self.ledger[sid]
                    log.warning("reconcile: ledger 제거 [%s] %s qty %d → 0 (외부 매도 추정)",
                                  sid, p["symbol"], p["old_qty"])
                else:
                    lg["qty"] = p["new_qty"]
                    log.warning("reconcile: ledger 차감 [%s] %s qty %d → %d (외부 매도 추정)",
                                  sid, p["symbol"], p["old_qty"], p["new_qty"])
                applied.append(p)
            self._save()
        else:
            log.info("reconcile: drift 없음 (in_sync %d종목)", len(result.get("in_sync", [])))

        result["applied"] = applied
        result["external_extras_count"] = len(result.get("external_extras", []))
        result["has_drift"] = bool(applied) or bool(result.get("external_extras"))
        return result

    def _safe_price(self, symbol: str) -> float | None:
        try:
            px = self.broker.price(symbol)
            return px if px > 0 else None
        except Exception as e:
            log.error("가격 조회 실패 [%s]: %s", symbol, e)
            return None

    # ── 미체결 추적·해제 ──────────────────────────────────────────────────────

    def _resolve_pending(self, decisions: list[dict]) -> None:
        """이전 사이클에서 남은 미체결 주문의 현재 상태를 갱신.

        Q7(DAY 단일): 로컬 timeout cancel 제거. KIS가 정규장 마감(15:30)에 미체결
        분을 자동 cancel하므로 우리는 상태 조회로 cancelled를 인지하고 ledger·
        pending을 정리하기만 한다. 일중에 limit 도달 시 자연 체결 허용.
        """
        if not self.pending:
            return
        for order_no, p in list(self.pending.items()):
            try:
                st = self.broker.order_status(order_no, p.get("symbol"))
            except Exception as e:
                log.warning("주문상태 조회 실패 [%s]: %s", order_no, e)
                continue
            status = st.get("status", "unknown")
            filled = int(st.get("filled_qty", 0) or 0)
            fill_px = float(st.get("fill_price", 0) or 0)

            if status == "filled" and filled > 0:
                self._apply_fill(order_no, p, filled, fill_px, decisions)
                del self.pending[order_no]
            elif status == "partial":
                # 부분체결: 채운 만큼만 반영하고 잔여는 계속 추적
                already = int(p.get("filled_so_far", 0))
                delta = filled - already
                if delta > 0:
                    self._apply_fill(order_no, p, delta, fill_px, decisions,
                                      partial=True)
                    p["filled_so_far"] = filled
            elif status == "cancelled":
                order_log.log_order("cancelled", p["symbol"], p["side"], p["qty"],
                                    order_no=order_no,
                                    intended_price=p.get("intended_price"),
                                    limit_price=p.get("limit_price"),
                                    strategy_name=p.get("strategy_name", ""))
                decisions.append(order_log.decision(
                    "unfilled", p.get("strategy_id", ""),
                    p.get("strategy_name", ""), p["symbol"],
                    "미체결 cancelled (KIS 마감 자동 취소 또는 외부 취소)"))
                del self.pending[order_no]
            # else: 여전히 미체결 — 다음 폴링/사이클에서 재확인. 로컬 timeout 없음.

    def _apply_fill(self, order_no: str, p: dict, filled_qty: int,
                    fill_price: float, decisions: list[dict],
                    partial: bool = False) -> None:
        """체결을 원장·이벤트 로그에 반영."""
        sid = str(p.get("strategy_id", ""))
        symbol = p["symbol"]
        side = p["side"]
        intended = p.get("intended_price")
        today = kst_today().isoformat()

        order_log.log_order("partial" if partial else "filled", symbol, side,
                             filled_qty, order_no=order_no,
                             intended_price=intended,
                             limit_price=p.get("limit_price"),
                             fill_price=fill_price,
                             strategy_name=p.get("strategy_name", ""),
                             reason=p.get("reason", ""))

        if side == "buy":
            if sid in self.ledger:
                # 추가 매수 — 평균단가 갱신
                lg = self.ledger[sid]
                total = lg["qty"] + filled_qty
                # L-05 — 정상 경로엔 두 값 모두 양수라 안전하나, 경로 변경 또는
                # 비정상 fill_qty/ledger qty=0 잔존 시 ZeroDivisionError 잠재.
                # 1줄 가드로 명시. 둘 다 0이면 의미 없는 호출이므로 조용히 return.
                if total <= 0:
                    return
                lg["entry_price"] = (lg["entry_price"] * lg["qty"]
                                      + fill_price * filled_qty) / total
                lg["qty"] = total
            else:
                self.ledger[sid] = {
                    "symbol": symbol, "qty": filled_qty,
                    "entry_date": today, "entry_price": fill_price,
                    "peak_price": fill_price,
                    "strategy_name": p.get("strategy_name", ""),
                    "definition": p.get("definition", {}),
                }
            ev = {"ts": today, "action": "buy", "symbol": symbol,
                  "qty": filled_qty, "price": fill_price,
                  "strategy": p.get("strategy_name", ""), "reason": "매수신호"}
            self._log_trade(ev)
            decisions.append(order_log.decision(
                "bought", sid, p.get("strategy_name", ""), symbol,
                f"{filled_qty}주 @ {fill_price:,.0f}원",
                {"intended": intended, "fill": fill_price}))
        else:
            if sid in self.ledger:
                lg = self.ledger[sid]
                lg["qty"] -= filled_qty
                if lg["qty"] <= 0:
                    del self.ledger[sid]
            ev = {"ts": today, "action": "sell", "symbol": symbol,
                  "qty": filled_qty, "price": fill_price,
                  "strategy": p.get("strategy_name", ""),
                  "reason": p.get("reason", "")}
            self._log_trade(ev)
            decisions.append(order_log.decision(
                "sold", sid, p.get("strategy_name", ""), symbol,
                f"{filled_qty}주 @ {fill_price:,.0f}원 ({p.get('reason', '')})"))

        # Q5 Tier 1 — 체결 직후 kill switch 평가. 시초가 매수가 장중에 잡혀 자본이
        # day_start 대비 -X% 도달하는 정확한 순간을 잡는다. _daily_loss_limit_pct가
        # 설정되어 있을 때만 평가(cycle 또는 intraday_loop가 설정).
        # 단, cycle 내부에서 호출된 _apply_fill은 skip — cycle이 진입부에서 이미
        # 평가했고, hook이 cycle을 재호출하면 _CYCLE_LOCK 데드락 + 무한 재귀.
        if self._daily_loss_limit_pct is not None and not self._in_cycle:
            fired = self.evaluate_killswitch_now(
                self._daily_loss_limit_pct, decisions)
            if fired and self._ks_trigger_hook is not None:
                try:
                    self._ks_trigger_hook("apply_fill")
                except Exception as e:
                    log.error("[ks-hook] apply_fill 트리거 핸들러 실패: %s", e)

    # ── Q5: 장중 kill switch (Tier 1·2 공용 평가/실행 helpers) ─────────────────

    def evaluate_killswitch_now(self, daily_loss_limit_pct: float,
                                  decisions: list[dict] | None = None) -> bool:
        """현재 KIS 잔고 기반 통합 자본을 평가해 일일 손실 한도 초과 시 발동.

        반환: 발동되어 새로 active 됐으면 True (이미 active였거나 미도달이면 False).
        decisions가 주어지면 발동 사유를 결정 로그에 기록.

        Q5: 사이클 시점(08:55/15:35)만 평가하던 기존 동작에 더해, 체결 후(_apply_fill)
        와 장중 60초 monitor에서도 동일 임계로 평가하기 위한 공용 진입점.
        """
        if killswitch.is_active():
            return False
        try:
            snap = self.broker.account_snapshot()
            equity = _unified_equity_krw(snap["balance"])
        except Exception as e:
            log.warning("[ks-eval] account_snapshot 실패 — skip: %s", e)
            return False
        reason = killswitch.check_daily_loss(equity, daily_loss_limit_pct)
        if not reason:
            return False
        killswitch.activate(reason)
        log.critical("[ks-eval] kill switch 발동: %s", reason)
        if decisions is not None:
            decisions.append(order_log.decision(
                "kill_switch", "", "", "", reason))
        return True

    def cancel_all_pending(self, decisions: list[dict] | None = None) -> int:
        """미체결 주문 전체를 KIS에 즉시 cancel 발주. Q5 발동 시 자금 노출 차단용.

        업계 표준(FCA): kill switch 발동 시 "cancel all outstanding orders". 보유분
        강제 청산은 다음 사이클이 책임지지만, 미체결 매수가 늦게 잡혀 손실을 키우는
        시나리오를 차단한다. cancel 자체 실패는 다음 사이클의 _resolve_pending이
        KIS 상태 조회로 정리.

        반환: cancel 시도한 주문 건수.
        """
        if not self.pending:
            return 0
        n = 0
        for order_no, p in list(self.pending.items()):
            try:
                self.broker.cancel(order_no, p["symbol"], p["qty"])
                n += 1
                if decisions is not None:
                    decisions.append(order_log.decision(
                        "cancelled", p.get("strategy_id", ""),
                        p.get("strategy_name", ""), p["symbol"],
                        f"kill switch — 미체결 즉시 취소 ({order_no})"))
            except Exception as e:
                log.warning("[ks-cancel] %s 취소 실패: %s", order_no, e)
        log.info("[ks-cancel] %d건 cancel 시도", n)
        return n

    # ── 주문 발주 helpers ────────────────────────────────────────────────────

    def _submit_buy(self, sid: str, strat_name: str, strat_def: dict,
                    symbol: str, qty: int, ref_price: float, policy: dict,
                    decisions: list[dict], catchup: bool = False) -> None:
        # L-01: 발주 직전 intent journal에 submitting 기록(fsync). 크래시-재기동
        # 시 reconcile이 KIS 당일 주문 조회로 매칭 → 중복 발주 방지.
        today_iso = kst_today().isoformat()
        intent_id = intents.new_intent_id()
        intents.begin(today_iso, intent_id, sid, strat_name, symbol, "buy",
                      qty, ref_price)
        use_limit = bool(policy["use_limit"])

        # catch-up + 시장가 매수: 시초가 limit으로 변환.
        # 이유: 정상 cycle의 시장가는 09:00 시초가에 체결되나 catch-up은 09:30
        # 현재가에 체결 → 백테스트 가정(시가 + slippage)과 어긋남. 시가 × (1 +
        # bt_slippage_bps) limit으로 변환하면 백테스트 모델과 alignment + selection
        # bias 없음(가격은 시가 fixed). ref_price(어제 종가)는 유지 — apply_daily_
        # price_limit이 prev_close 기준 ±30% cap 정확히 계산하도록.
        if catchup and not use_limit:
            open_price = self.broker.today_open(symbol)
            if open_price <= 0:
                log.warning("[catch-up] %s 시가 조회 실패 — 매수 skip", symbol)
                intents.mark_failed(today_iso, intent_id, "no_open_price")
                decisions.append(order_log.decision(
                    "skip_no_open_price", sid, strat_name, symbol,
                    "catch-up: 당일 시가 조회 실패"))
                return
            slip = qc.DEFAULT_EXECUTION["bt_slippage_bps"] / 10_000.0
            limit = qc.round_to_tick(open_price * (1 + slip),
                                       direction="up",
                                       currency=_currency_of(symbol))
            limit = qc.apply_daily_price_limit(
                limit, ref_price, "buy", _currency_of(symbol))
            try:
                r = self.broker.buy_limit(symbol, qty, limit)
            except Exception as e:
                intents.mark_failed(today_iso, intent_id,
                                      f"buy_limit (catchup): {e}")
                log.error("[catch-up] %s 시초가 limit 발주 실패: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol,
                    f"catch-up 발주 예외: {e}"))
                return
            log.info("[catch-up] %s 시장가→시초가 limit: open=%s limit=%s",
                      symbol, open_price, limit)
        elif use_limit:
            limit = qc.round_to_tick(
                ref_price * (1 + policy["buy_tolerance_pct"] / 100.0),
                direction="up", currency=_currency_of(symbol))
            # 한국 ±30% 가격제한폭 사전 클램프 — KIS 서버 거부 누적 방지
            limit = qc.apply_daily_price_limit(
                limit, ref_price, "buy", _currency_of(symbol))
            try:
                r = self.broker.buy_limit(symbol, qty, limit)
            except Exception as e:
                intents.mark_failed(today_iso, intent_id, f"buy_limit: {e}")
                log.error("매수 지정가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"발주 예외: {e}"))
                return
        else:
            limit = 0
            try:
                r = self.broker.buy(symbol, qty)
            except Exception as e:
                intents.mark_failed(today_iso, intent_id, f"buy: {e}")
                log.error("매수 시장가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"발주 예외: {e}"))
                return
        # KIS 응답 수신 — submitted 마감(order_no가 빈 문자면 거부 처리는 _after_submit이 함)
        intents.mark_submitted(today_iso, intent_id, r.get("order_no", "") or "")
        self._after_submit(r, sid, strat_name, strat_def, symbol, "buy", qty,
                            ref_price, limit, policy, decisions, reason="매수신호")

    def _submit_sell(self, sid: str, strat_name: str, symbol: str, qty: int,
                     ref_price: float, policy: dict, reason: str,
                     decisions: list[dict]) -> None:
        # L-01: 매도도 동일 멱등 보호 — 크래시 시 over-sell 방지(L-04와 중복 안전망).
        today_iso = kst_today().isoformat()
        intent_id = intents.new_intent_id()
        intents.begin(today_iso, intent_id, sid, strat_name, symbol, "sell",
                      qty, ref_price)
        use_limit = bool(policy["use_limit"])
        # Phase 38.9 — 매도 tolerance 단일화. 신호·청산 모두 같은 값.
        tol = policy["sell_tolerance_pct"]
        if use_limit:
            limit = qc.round_to_tick(ref_price * (1 - tol / 100.0),
                                      direction="down", currency=_currency_of(symbol))
            # 한국 ±30% 가격제한폭 사전 클램프 — 하한가 cap
            limit = qc.apply_daily_price_limit(
                limit, ref_price, "sell", _currency_of(symbol))
            try:
                r = self.broker.sell_limit(symbol, qty, limit)
            except Exception as e:
                intents.mark_failed(today_iso, intent_id, f"sell_limit: {e}")
                log.error("매도 지정가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"발주 예외: {e}"))
                return
        else:
            limit = 0
            try:
                r = self.broker.sell(symbol, qty)
            except Exception as e:
                intents.mark_failed(today_iso, intent_id, f"sell: {e}")
                log.error("매도 시장가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"발주 예외: {e}"))
                return
        intents.mark_submitted(today_iso, intent_id, r.get("order_no", "") or "")
        self._after_submit(r, sid, strat_name, None, symbol, "sell", qty,
                            ref_price, limit, policy, decisions, reason=reason)

    def _after_submit(self, r: dict, sid: str, strat_name: str,
                      strat_def: dict | None, symbol: str, side: str, qty: int,
                      intended_price: float, limit_price: int,
                      policy: dict, decisions: list[dict], reason: str) -> None:
        """submit 결과를 후처리: pending 등록 / 즉시 체결 반영 / 거부 로깅."""
        order_no = r.get("order_no", "")
        if not r.get("success"):
            order_log.log_order("rejected", symbol, side, qty,
                                 order_no=order_no,
                                 intended_price=intended_price,
                                 limit_price=limit_price,
                                 strategy_name=strat_name, reason=reason,
                                 extra={"msg": r.get("message", "")})
            decisions.append(order_log.decision(
                "rejected", sid, strat_name, symbol,
                f"{side} {qty}주 거부: {r.get('message', '')}"))
            return
        p = {
            "order_no": order_no, "strategy_id": sid,
            "strategy_name": strat_name, "symbol": symbol, "side": side,
            "qty": qty, "limit_price": limit_price,
            "intended_price": intended_price,
            "submitted_ts": time.time(),
            # Q7: timeout_sec 필드 제거 — _resolve_pending이 timeout cancel을
            # 더 이상 사용하지 않음. KIS DAY 정책으로 마감 시 자동 cancel.
            "definition": strat_def or {}, "reason": reason,
            "filled_so_far": 0,
        }
        order_log.log_order("submitted", symbol, side, qty, order_no=order_no,
                             intended_price=intended_price,
                             limit_price=limit_price, strategy_name=strat_name,
                             reason=reason)
        # 일부 KIS 즉시체결 응답엔 체결 정보가 포함돼 있다 — pending 단계 건너뛰고 즉시 반영.
        filled = int(r.get("filled_qty", 0) or 0)
        fill_price = float(r.get("price", 0) or 0)
        if filled >= qty and fill_price > 0:
            self._apply_fill(order_no, p, filled, fill_price, decisions)
            return
        # 그렇지 않으면 pending에 등록 → 다음 사이클 또는 _wait_pending이 폴링
        self.pending[order_no] = p

    def _wait_pending(self, timeout_sec: int, poll_sec: int,
                      decisions: list[dict]) -> None:
        """이번 사이클에 제출한 주문들이 체결되기를 짧게 기다린다.

        timeout 안에 안 잡힌 건은 _resolve_pending이 다음 사이클에 처리.
        """
        if not self.pending:
            return
        end = time.time() + timeout_sec
        while time.time() < end and self.pending:
            time.sleep(poll_sec)
            self._resolve_pending(decisions)

    # Phase 38.4: _enter_screener·_buy_screener_pick 제거 — 진입은 preview path 전용.
    # 자동 선택 매칭은 서버 preview_engine이 18:15에 수행해 by_strategy에 담아 보냄.

    def _try_buy_one_symbol(self, ledger_key: str, strategy_id: str,
                              strat_name: str, strat_def: dict,
                              strat: "qc.Strategy", symbol: str,
                              dataset: dict, equity_now: float,
                              decisions: list[dict],
                              catchup: bool = False) -> bool:
        """수동(단일/다중) 종목 1개에 대해 사이징 + 발주 (EOD-순수 모델).

        Phase 30: 매수 path는 전일 종가만으로 결정. KIS 현재가 호출 없음.
          - 발주 지정가 = 전일 종가 × (1 + buy_tolerance_pct%)
          - 사이징 분모도 전일 종가
          - 갭 필터 없음 — 갭상승 시 발주가 초과로 미체결 → 자연 회피
          - 매수 신호 평가는 호출 전에 1회만 수행 (다중 후보 모두에 동일 적용)
        잔고는 매 호출마다 재조회해 다중 매수 중 자금 소진을 정확히 반영한다.
        """
        sdf = dataset.get(symbol)
        if sdf is None or len(sdf) == 0 or "Close" not in sdf.columns:
            # dataset에 없는 종목 — 전일 종가 없음. 자동 fallback 없이 명시적 skip.
            decisions.append(order_log.decision(
                "skip_no_data", strategy_id, strat_name, symbol,
                "전일 종가 없음 — 매수 대상 종목이 dataset에 없음 (서버 dataset 갱신 대기)"))
            return False

        prev_close = float(sdf["Close"].iloc[-1])
        if prev_close <= 0:
            decisions.append(order_log.decision(
                "skip_no_data", strategy_id, strat_name, symbol,
                "전일 종가가 0 — 데이터 이상"))
            return False

        # Phase 48 — 거래정지·관리종목·투자위험 자동 차단. KIS broker가 거부로
        # 2차 안전망을 제공하나 사이클 중 불필요한 발주 시도를 줄인다. status를
        # 알 수 없는 종목(서버 데이터 누락)은 일반 종목으로 취급 (보수 안전 fallback).
        status = (getattr(self, "_krx_status", None) or {}).get(symbol) or {}
        if status.get("is_halt"):
            decisions.append(order_log.decision(
                "skip_halted", strategy_id, strat_name, symbol,
                "거래정지·정리매매 종목 — 매수 발주 차단"))
            return False
        if status.get("is_managed"):
            decisions.append(order_log.decision(
                "skip_managed", strategy_id, strat_name, symbol,
                "관리·투자위험·투자경고 종목 — 매수 발주 차단"))
            return False

        # Phase 48 P1-D — 일일 거래 한도 차단 (한도 활성 시만 호출).
        tcount_limit = getattr(self, "_daily_trade_count_limit", 0)
        tturn_limit = getattr(self, "_daily_turnover_limit_krw", 0)
        if tcount_limit > 0 or tturn_limit > 0:
            today_iso = kst_today().isoformat()
            tcount, tturn = self._today_buy_summary(today_iso)
            if tcount_limit > 0 and tcount >= tcount_limit:
                decisions.append(order_log.decision(
                    "skip_daily_count", strategy_id, strat_name, symbol,
                    f"일일 거래 횟수 한도 도달 ({tcount}/{tcount_limit}) — 매수 차단"))
                return False
            if tturn_limit > 0 and tturn >= tturn_limit:
                decisions.append(order_log.decision(
                    "skip_daily_turnover", strategy_id, strat_name, symbol,
                    f"일일 거래 대금 한도 도달 ({tturn:,}/{tturn_limit:,} KRW) — 매수 차단"))
                return False

        policy = _policy(strat_def)

        # 통화별 가용자금 결정.
        #  - 미국: psamount(매수가능금액) — KIS 통합증거금을 반영한 USD 주문가능액.
        #    USD 예수금이 0이어도 KRW 담보로 주문 가능하므로 예수금이 아니라
        #    "주문가능액"을 기준으로 사이징한다. max_qty로 상한도 클램프.
        #  - 국내: KRW 예수금.
        # cash·capital·prev_close 단위를 종목 통화로 일치시킨다.
        ccy = _currency_of(symbol)
        max_cap = None
        try:
            if ccy == "USD":
                # 매수여력 모드 (사용자 설정): integrated=통합증거금(주문가능액) /
                # usd_cash=USD 예수금 한정(보수적, FX 노출 없음).
                mode = getattr(self, "_us_bp_mode", "integrated")
                if mode == "usd_cash":
                    bal = self.broker.account_snapshot()["balance"]
                    cash = float(bal.get("cash_usd", 0) or 0)
                    fx = float(bal.get("fx_usdkrw", 0) or 0)
                else:   # integrated (기본)
                    bp = self.broker.buying_power_usd(symbol, prev_close)
                    cash = float(bp.get("usd_orderable", 0) or 0)
                    fx = float(bp.get("fx_usdkrw", 0) or 0)
                    max_cap = int(bp.get("max_qty", 0) or 0)
                # equity_now는 KRW 통합자산 → USD 환산해 atr capital에 사용
                capital = (equity_now / fx) if (fx > 0 and equity_now > 0) else cash
            else:
                # KRX 사이징은 국내 현금만 필요 — 해외 API 2건 skip (효율)
                cash = float(self.broker.account_snapshot(
                    overseas=False)["balance"]["cash"])
                capital = equity_now if equity_now > 0 else cash
        except Exception as e:
            log.error("가용자금 조회 실패 [%s]: %s", symbol, e)
            decisions.append(order_log.decision(
                "error", strategy_id, strat_name, symbol,
                f"가용자금 조회 실패: {e}"))
            return False

        # 사이징 — 전일 종가 기준 (cash·prev_close 모두 종목 통화)
        # Phase 47 — 4지 모드 (fixed_amount / pct_cash / equal_weight / atr_risk).
        # 모든 모드는 max_position_pct가 설정된 경우 단일 종목 비중 상한 클램프
        # (None = OFF, 한도 없음).
        mode = policy["sizing_mode"]
        _mp = policy.get("max_position_pct")
        cap_qty = (int((capital * float(_mp) / 100.0) // prev_close)
                     if _mp is not None and prev_close > 0 else None)
        if mode == "atr_risk":
            atr_val = 0.0
            if "atr_14" in sdf.columns:
                atr_val = float(sdf["atr_14"].iloc[-1] or 0.0)
            if atr_val <= 0:
                decisions.append(order_log.decision(
                    "skip_no_atr", strategy_id, strat_name, symbol,
                    "ATR 데이터 없음 — atr_risk 모드 진입 불가 "
                    "(사이징을 정률/정액으로 변경하거나 dataset 보강 필요)"))
                return False
            qty = _atr_qty(capital, atr_val, policy, prev_close)
        elif mode == "fixed_amount":
            # 정액: 한 종목당 amount_krw 원. 통화 단위는 종목 통화와 일치한다고 가정
            # (KRW 종목엔 KRW 금액). 미국 종목 fixed_amount는 별도 cycle 검토.
            amount = float(policy.get("amount_krw") or 0)
            qty = int(amount // prev_close)
        elif mode == "equal_weight":
            # 균등 분배: 자본 ÷ 동시 보유 한도. screener_limit이 1이면 정률 100%와 동일.
            slot = capital / max(int(strat.screener_limit or 1), 1)
            qty = int(slot // prev_close)
        else:  # pct_cash (default) — 현행 정률
            qty = int(cash * strat.amount_pct / 100.0 // prev_close)

        # L-10 — max_position_pct가 설정된 경우만 단일 종목 비중 상한 클램프.
        # capital은 통화 일치(KRW/USD)된 값. cap_qty=None이면 한도 없음(OFF).
        if cap_qty is not None:
            qty = min(qty, cap_qty)
        # 가용 현금 한도 (모든 모드 공통)
        qty = min(qty, int(cash // prev_close))

        # 미국: 주문가능수량(통합증거금 상한) 초과 방지
        if max_cap is not None:
            qty = min(qty, max_cap)

        if qty <= 0:
            decisions.append(order_log.decision(
                "skip_funds", strategy_id, strat_name, symbol,
                f"수량 부족 (현금 {cash:,.0f} / 전일종가 {prev_close:,.0f})"))
            return False

        # L-01 멱등 게이트 — 오늘 같은 (sid, symbol, buy)로 이미 발주됐다면 skip.
        # 크래시 후 재기동 + reconcile이 submitted/ambiguous로 마감했으면 차단.
        today_iso = kst_today().isoformat()
        if intents.is_active(today_iso, ledger_key, symbol, "buy"):
            decisions.append(order_log.decision(
                "skip_idempotent", strategy_id, strat_name, symbol,
                "오늘 이미 발주된 intent 존재 — 중복 차단"))
            log.info("[L-01] 중복 매수 차단 %s/%s", ledger_key, symbol)
            return False

        # 발주가는 _submit_buy 내부에서 prev_close × (1 + tolerance%) 계산.
        # catchup=True면 시장가 매수만 시초가 limit으로 변환 (지정가는 그대로).
        self._submit_buy(ledger_key, strat_name, strat_def, symbol, qty,
                          prev_close, policy, decisions, catchup=catchup)
        return True

    # ── 메인 사이클 ───────────────────────────────────────────────────────────

    def _enter_from_preview(self, by_strategy: list[dict], strategies: list[dict],
                              dataset: dict, equity_now: float,
                              decisions: list[dict],
                              sold_this_cycle: set[str],
                              market: str = "KRX",
                              catchup: bool = False) -> None:
        """Phase 37: 서버 preview의 candidates 종목을 직접 발주.

        매수 신호 재평가는 skip (preview가 어제 18:15에 이미 평가).
        잔고·사이징은 _try_buy_one_symbol이 발주 직전 KIS 재조회로 재계산 →
        밤사이 수동 거래·입금 반영. 보유/한도·중복 진입 체크는 기존과 동일.

        market: 이번 사이클 시장 그룹. 해당 시장 후보만 진입(미국 종목은 미국
        정규장 사이클에서만 발주). 다른 시장 후보는 skip한다.

        candidates의 종목 코드는 신뢰하되 dataset에 없는 종목은 skip
        (방어적 — preview·dataset가 같은 서버 상태에서 만들어졌으면 일치).
        """
        strat_def_by_id = {str(s["id"]): (s.get("name", ""), s.get("definition", {}))
                             for s in strategies}
        n_preview_used = 0
        for entry in by_strategy:
            sid = str(entry.get("strategy_id", ""))
            cands = entry.get("candidates") or []
            if not cands:
                continue
            name_def = strat_def_by_id.get(sid)
            if name_def is None:
                # 서버 preview에 있지만 로컬엔 배정 안 된 전략 — skip
                continue
            strat_name, strat_def = name_def
            trade_sym = strat_def.get("trade_symbol", "")
            mode, targets = qc.parse_trade_symbols(trade_sym)

            # 보유/한도 체크
            if mode == "screener":
                prefix = f"{sid}:"
                held_keys = {k for k in self.ledger if k.startswith(prefix)}
                screener_limit = int(strat_def.get("screener_limit", 1) or 1)
                slots_left = screener_limit - len(held_keys)
                if slots_left <= 0:
                    decisions.append(order_log.decision(
                        "skip_held", sid, strat_name, "",
                        f"자동 선택 한도 충족 ({len(held_keys)}/{screener_limit})"))
                    continue
                # Phase 55 — OFF mode lock-in: 한 번 매수 후 빈 슬롯 안 채움.
                # rebalance dict는 frontend가 그대로 보낸 형식. legacy enabled도 지원.
                rb = strat_def.get("rebalance") or {}
                rb_mode = rb.get("mode") or (
                    "replace" if rb.get("enabled") else "hold")
                if rb_mode == "off" and len(held_keys) > 0:
                    decisions.append(order_log.decision(
                        "skip_locked", sid, strat_name, "",
                        f"리밸런싱 OFF — 보유 {len(held_keys)}/{screener_limit} lock-in"))
                    continue
                is_multi_key = True
            elif len(targets) > 1:
                prefix = f"{sid}:"
                held_keys = {k for k in self.ledger if k.startswith(prefix)}
                screener_limit = int(strat_def.get("screener_limit", 1) or 1)
                slots_left = screener_limit - len(held_keys)
                if slots_left <= 0:
                    decisions.append(order_log.decision(
                        "skip_held", sid, strat_name, trade_sym,
                        f"수동 다중 한도 충족 ({len(held_keys)}/{screener_limit})"))
                    continue
                is_multi_key = True
            else:
                if sid in self.ledger or sid in sold_this_cycle:
                    decisions.append(order_log.decision(
                        "skip_held", sid, strat_name, trade_sym,
                        "이미 보유 또는 당일 청산"))
                    continue
                slots_left = 1
                is_multi_key = False

            try:
                strat = qc.Strategy(**strat_def)
            except Exception as e:
                log.warning("전략 파싱 실패 [%s]: %s", strat_name, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, "",
                    f"전략 파싱 실패: {e}"))
                continue

            bought = 0
            for c in cands:
                if bought >= slots_left:
                    break
                symbol = c.get("symbol", "")
                if not symbol:
                    continue
                # 시장 배칭 — 이번 사이클 시장의 후보만 진입
                if _market_group_safe(symbol) != market:
                    continue
                ledger_key = f"{sid}:{symbol}" if is_multi_key else sid
                if ledger_key in self.ledger or ledger_key in sold_this_cycle:
                    continue
                if self._try_buy_one_symbol(
                        ledger_key, sid, strat_name, strat_def, strat,
                        symbol, dataset, equity_now, decisions,
                        catchup=catchup):
                    bought += 1
                    n_preview_used += 1

        log.info("preview 경로 진입 완료 — %d종목 발주 (신호 재평가 skip)",
                  n_preview_used)

    def _rebalance_reason(self, ledger_key: str, strat, symbol: str,
                          members_by_sid: dict, due_cache: dict,
                          today: date) -> str | None:
        """리밸런싱 매도 사유 — 자동선택 상위 N에서 탈락한 보유분이면 '리밸런싱'.

        안전 가드:
          - rebalance.mode != "replace"거나 자동선택 전략이 아니면 None.
            (off·hold 모드는 탈락 매도 안 함 — 매도 룰만 동작.)
          - 멤버십 데이터가 없으면(서버 preview 누락/빈 값) None — 절대 매도 안 함.
          - 주기가 아직 도래 안 했으면 None.
          - 종목이 여전히 상위 N에 있으면 None(유지).
        """
        rb = getattr(strat, "rebalance", None)
        if rb is None or getattr(rb, "mode", "hold") != "replace":
            return None
        mode, _ = qc.parse_trade_symbols(strat.trade_symbol or "")
        if mode != "screener":
            return None
        sid = ledger_key.split(":", 1)[0]
        members = members_by_sid.get(sid)
        if not members:
            return None                    # 멤버십 데이터 없음 — 안전상 매도 안 함
        if sid not in due_cache:
            due_cache[sid] = _rebalance_due(
                rb.period, self.rebalance_state.get(sid), today,
                getattr(rb, "every_n_days", None))
        if not due_cache[sid]:
            return None
        if symbol in members:
            return None                    # 여전히 상위 N — 유지
        return "리밸런싱"

    def _today_buy_summary(self, today_iso: str) -> tuple[int, int]:
        """오늘자 매수 거래의 (횟수, 누적 금액 KRW) 반환 (Phase 48 P1-D).

        TRADES_PATH(JSONL)를 한 번 스캔. 일 단위 한도 체크용이라 매수만 카운트.
        한도 비활성(둘 다 0)이면 호출 자체 skip하므로 비용은 활성 사용자만.
        """
        count = 0
        turnover = 0
        if not TRADES_PATH.exists():
            return 0, 0
        try:
            with open(TRADES_PATH, encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("action") != "buy":
                        continue
                    ts = str(ev.get("ts") or "")
                    if not ts.startswith(today_iso):
                        continue
                    count += 1
                    qty = int(ev.get("qty", 0) or 0)
                    price = float(ev.get("price", 0) or 0)
                    turnover += qty * int(price)
        except Exception as e:
            log.warning("[P1-D] today_buy_summary 읽기 실패: %s", e)
        return count, turnover

    def _in_kis_maintenance_window(self, now_kst: datetime) -> bool:
        """KIS 정기 점검 시간대 추정 (Phase 48 P1-B).

        KIS 공식 시간이 문서화되지 않아 eFriend Plus 공지·관찰 기반 보수적 추정:
          - 평일(월~금) 03:00 ~ 06:00 — 시스템 점검
          - 토요일 17:00 이후 ~ 일요일 종일 ~ 월요일 07:00 — 주말 점검
        시장 사이클(08:55, 15:45 등)은 이 윈도우 밖이라 정상 운영 영향 없음.
        Edge: 사용자가 cycle을 수동 트리거할 때 보호. False면 정상 진행.
        """
        wd = now_kst.weekday()  # 0=월, 5=토, 6=일
        h = now_kst.hour
        if wd == 5 and h >= 17:                      # 토 저녁
            return True
        if wd == 6:                                    # 일 종일
            return True
        if wd == 0 and h < 7:                         # 월 새벽
            return True
        if wd in (1, 2, 3, 4) and 3 <= h < 6:         # 평일 새벽 점검
            return True
        return False

    def cycle(self, strategies: list[dict], dataset: dict,
              today: date | None = None,
              buy_candidates: list[dict] | None = None,
              risk_limits: dict | None = None,
              market: str = "KRX",
              krx_status: dict[str, dict] | None = None,
              catchup: bool = False) -> dict:
        """전략 목록을 1회 평가하고 매매한 뒤 동기화용 스냅샷을 반환한다.

        market: 이번 사이클이 다룰 시장 그룹('KRX' 또는 'US'). 청산은 해당 시장
        보유분만, 진입은 해당 시장 후보만 처리한다 — 시장별 정규장 시각에 맞춰
        분리 실행하기 위함. kill switch·drawdown은 계좌 전체(통합 equity) 기준.

        buy_candidates(by_strategy 리스트, 비어있어도 list)가 신규 진입 source.
        Phase 38.4: 항상 preview 경로 — buy_candidates가 빈 리스트면 진입 0,
        청산은 정상. 호출자(runner)가 preview 누락 시 []로 전달.

        risk_limits(Phase 38.7/38.10): 사용자 위험 한도. 예:
          {"kill_switch_daily_loss_pct": 2.0, "max_drawdown_pct": 15.0}
        키가 없거나 None이면 글로벌 default 사용.

        Q5(AL-4): cycle/settlement/장중 ks 트리거의 직렬화. _CYCLE_LOCK을 acquire
        후 진입 — 동시 진입을 막아 broker.account_snapshot·발주 순서를 보존한다.
        """
        # Q5: 외부 호출자가 이미 락을 쥔 채로 cycle을 호출하는 경우(예: 장중 ks
        # 핸들러)도 대비해 RLock이 아닌 Lock을 쓰되, 모든 진입은 같은 thread가
        # 중첩 호출하지 않도록 호출 규약으로 강제한다. timeout=None으로 blocking.
        with _CYCLE_LOCK:
            return self._cycle_locked(strategies, dataset, today,
                                       buy_candidates, risk_limits, market,
                                       krx_status, catchup=catchup)

    def _cycle_locked(self, strategies, dataset, today, buy_candidates,
                       risk_limits, market, krx_status,
                       catchup: bool = False) -> dict:
        # Q5(데드락 방지): _in_cycle 플래그를 try/finally로 보장 — 예외 발생 시에도
        # 반드시 reset되어야 다음 cycle에서 _apply_fill의 평가가 정상 동작.
        self._in_cycle = True
        # Phase 48 — 종목 상태 dict는 인스턴스에 저장해 _try_buy_one_symbol에서 사용.
        # cycle 단위 stale 안전 (dict는 cycle 시작 시 fresh, 다음 cycle에서 다시 받음).
        self._krx_status: dict[str, dict] = krx_status or {}
        try:
            return self._cycle_body(strategies, dataset, today,
                                     buy_candidates, risk_limits, market,
                                     catchup=catchup)
        finally:
            self._in_cycle = False

    def _cycle_body(self, strategies, dataset, today, buy_candidates,
                     risk_limits, market, catchup: bool = False) -> dict:
        today = today or kst_today()
        decisions: list[dict] = []

        # Phase 48 P1-B — KIS 정기 점검 시간대 자동 차단.
        # 점검 시간에는 사이클 진입 X (KIS 응답이 비정상이라 안전 우선).
        now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
        if self._in_kis_maintenance_window(now_kst):
            decisions.append(order_log.decision(
                "skip_maintenance", "", "", "",
                "KIS 정기 점검 시간대 — 자동매매 보류 (점검 후 다음 cron에서 자동 재개)"))
            log.info("[P1-B] KIS 점검 시간대 — cycle skip (%s)",
                      now_kst.strftime("%a %H:%M"))
            return {"balance": {"cash": 0, "total_eval": 0},
                    "positions": [], "equity": self.equity[-365:],
                    "trades": [], "decisions": decisions,
                    "cycle_summary": {"skipped_reason": "kis_maintenance"}}

        # ── 0. 이전 사이클 미체결 정리 ─────────────────────────────────────
        self._resolve_pending(decisions)

        # ── 1. 자본·day_start 갱신, kill switch 평가 ──────────────────────
        # equity는 계좌 전체 통합(국내+해외, KRW) — kill switch는 시장 무관 계좌 단위.
        # Phase 48 P1-B — 헬스체크 강화. 잔고 조회 실패는 KIS API 단절 신호이므로
        # 0으로 fallback하지 말고 cycle 전체를 중단 (잘못된 equity로 매도 평가 방지).
        try:
            snap_pre = self.broker.account_snapshot()
            equity_now = _unified_equity_krw(snap_pre["balance"])
        except Exception as e:
            log.error("[P1-B] KIS 잔고 조회 실패 — cycle 중단: %s", e)
            decisions.append(order_log.decision(
                "skip_kis_health", "", "", "",
                f"KIS API 응답 실패 — 자동매매 보류 (다음 사이클 재시도): {e}"))
            return {"balance": {"cash": 0, "total_eval": 0},
                    "positions": [], "equity": self.equity[-365:],
                    "trades": [], "decisions": decisions,
                    "cycle_summary": {"skipped_reason": "kis_health_fail"}}

        killswitch.update_day_start(equity_now, today.isoformat())
        ks_state = killswitch.load()
        ks_active = bool(ks_state.get("active"))

        # 글로벌 default를 미사용 시에도 적용하기 위해 빈 policy로 시작
        global_policy = merged_execution(None)

        # Phase 38.7 — 사용자 설정 우선, null이면 글로벌 default
        rl = risk_limits or {}
        # 미국 매수여력 모드 (사용자 설정) — _try_buy_one_symbol 사이징에 반영
        self._us_bp_mode = rl.get("us_buying_power_mode") or "integrated"
        # 일일 손실 한도: user 모니터링 설정에서만 가져옴. None이면 OFF.
        # (ExecutionPolicy.daily_loss_limit_pct 제거됨 — 종목 단위 실시간 매도로 위험 처리.)
        daily_loss_limit_pct = rl.get("kill_switch_daily_loss_pct")
        # max_drawdown 한도: user setting 우선, 없으면 global_policy. None이면 OFF.
        _user_dd = rl.get("max_drawdown_pct")
        _global_dd = global_policy.get("max_drawdown_pct")
        max_drawdown_limit_pct = _user_dd if _user_dd is not None else _global_dd
        # Q5: 체결 후 즉시 평가용으로 인스턴스에 저장. None이면 평가 skip.
        self._daily_loss_limit_pct = (float(daily_loss_limit_pct)
                                        if daily_loss_limit_pct is not None else None)
        # Phase 48 P1-D — 일일 거래 한도 (0 = 비활성).
        self._daily_turnover_limit_krw = int(rl.get("daily_turnover_limit_krw") or 0)
        self._daily_trade_count_limit = int(rl.get("daily_trade_count_limit") or 0)

        if not ks_active and daily_loss_limit_pct is not None:
            reason = killswitch.check_daily_loss(
                equity_now, daily_loss_limit_pct)
            if reason:
                killswitch.activate(reason)
                ks_active = True
                ks_state = killswitch.load()

        # Phase 38.10 — 누적 drawdown 측정 (자본 고점 대비). kill switch와 별개.
        # peak는 equity log의 max + 현재 equity 중 큰 값.
        peak_equity = equity_now
        for e in self.equity:
            v = float(e.get("value") or 0)
            if v > peak_equity:
                peak_equity = v
        drawdown_pct = 0.0
        if peak_equity > 0:
            drawdown_pct = (equity_now - peak_equity) / peak_equity * 100
        # max_drawdown_limit_pct=None이면 한도 없음(OFF) — drawdown 차단 평가 skip.
        drawdown_active = (max_drawdown_limit_pct is not None
                            and drawdown_pct <= -abs(float(max_drawdown_limit_pct)))
        if drawdown_active:
            log.warning(
                "drawdown 한도 도달 — 자본 고점 %s원 → 현재 %s원 (%.2f%%, 한도 -%.2f%%)",
                f"{peak_equity:,.0f}", f"{equity_now:,.0f}",
                drawdown_pct, float(max_drawdown_limit_pct))

        # ── 2. 청산 패스 (Phase 38.2: 신호·시간 기반만 — 가격은 intraday가 담당) ──
        # 리밸런싱 멤버십 — 서버 preview의 자동선택 상위 N 종목 (sid → [symbols]).
        # 데이터가 없으면(빈 dict) 리밸런싱 매도는 발동하지 않는다(안전).
        members_by_sid: dict[str, list] = {}
        for entry in (buy_candidates or []):
            esid = str(entry.get("strategy_id", ""))
            if esid:
                members_by_sid[esid] = entry.get("screener_members") or []
        rebalance_due_cache: dict[str, bool] = {}

        sold_this_cycle: set[str] = set()
        for sid, pos in list(self.ledger.items()):
            # 시장 배칭 — 이번 사이클 시장의 보유분만 청산 (미국 보유분은
            # 미국 정규장 사이클에서만 매도, 그 반대도 동일).
            if _market_group_safe(pos["symbol"]) != market:
                continue
            try:
                strat = qc.Strategy(**pos["definition"])
            except Exception as e:
                log.warning("원장 전략 파싱 실패 [%s]: %s", sid, e)
                continue

            held = (today - date.fromisoformat(pos["entry_date"])).days
            reason = _evaluate_exit(strat, held, dataset, pos["symbol"])
            # kill switch 활성 시 모든 보유 강제 청산
            if ks_active and not reason:
                reason = "kill-switch"
            # 리밸런싱 — 자동선택 상위 N에서 탈락한 보유분 매도 (주기 게이팅)
            if not reason and not ks_active:
                reason = self._rebalance_reason(
                    sid, strat, pos["symbol"],
                    members_by_sid, rebalance_due_cache, today)

            if not reason:
                continue

            # ref_price는 dataset 전일 종가. 없으면 KIS 현재가로 fallback.
            sdf = dataset.get(pos["symbol"])
            ref_price = 0.0
            if sdf is not None and len(sdf) > 0 and "Close" in sdf.columns:
                try:
                    ref_price = float(sdf["Close"].iloc[-1])
                except Exception:
                    ref_price = 0.0
            if ref_price <= 0:
                cur = self._safe_price(pos["symbol"])
                if cur is None or cur <= 0:
                    log.warning("청산 ref_price 없음 [%s] — 다음 사이클로 연기",
                                pos["symbol"])
                    continue
                ref_price = cur

            policy = _policy(pos.get("definition"))
            # L-01 멱등 게이트 — 오늘 같은 (sid, symbol, sell) intent가 활성이면 skip
            today_iso = kst_today().isoformat()
            if intents.is_active(today_iso, sid, pos["symbol"], "sell"):
                log.info("[L-01] 중복 매도 차단 %s/%s", sid, pos["symbol"])
                continue
            # Phase 56 — 매도 룰별 sell_pct. reason → key 매핑 후 보유 수량 ×%.
            sell_pct = qc.sell_pct_for_reason(strat.sell_rules, reason)
            sell_qty = max(1, int(pos["qty"] * sell_pct / 100.0))
            sell_qty = min(sell_qty, int(pos["qty"]))   # 보유분 초과 방지
            self._submit_sell(sid, pos.get("strategy_name", ""), pos["symbol"],
                              sell_qty, ref_price, policy, reason, decisions)
            # sold_this_cycle은 sid 단위 — 부분 매도여도 같은 cycle 중복 매도 차단.
            sold_this_cycle.add(sid)

        # 리밸런싱을 평가한(주기 도래) 전략은 오늘자로 기록 — 같은 주기 재발동 방지.
        # 매도가 없었어도 주기는 소진된 것으로 본다.
        for rsid, due in rebalance_due_cache.items():
            if due:
                self.rebalance_state[rsid] = today.isoformat()

        # ── 3. 진입 패스 (kill switch·drawdown 활성 시 건너뜀, preview 전용) ──
        if ks_active:
            decisions.append(order_log.decision(
                "skip_killswitch", "", "", "",
                f"신규 진입 차단 — {ks_state.get('reason', '')}"))
        elif drawdown_active:
            decisions.append(order_log.decision(
                "skip_drawdown", "", "", "",
                f"신규 진입 차단 — 누적 drawdown {drawdown_pct:.2f}% "
                f"(한도 -{float(max_drawdown_limit_pct):.1f}%)"))
        elif buy_candidates is not None:
            self._enter_from_preview(buy_candidates, strategies, dataset,
                                       equity_now, decisions, sold_this_cycle,
                                       market=market, catchup=catchup)

        # ── 4. 미체결 짧게 대기 (시초가 동시호가 직후 대부분 잡힘) ───────
        # Q7: 300초 → 60초 (post_submit_wait_sec). DAY 정책으로 못 잡힌 분은
        # 다음 사이클 또는 KIS 마감 자동 cancel이 정리.
        self._wait_pending(global_policy["post_submit_wait_sec"],
                           global_policy["poll_interval_sec"], decisions)

        # ── 5. 최종 스냅샷 ────────────────────────────────────────────────
        snap = self.broker.account_snapshot()
        self.equity.append({"date": today.isoformat(),
                            "value": snap["balance"]["total_eval"]})
        self._save()

        try:
            broker_pending = self.broker.pending_orders()
        except Exception as e:
            log.warning("미체결 조회 실패: %s", e)
            broker_pending = []

        cycle_summary = {
            "today": today.isoformat(),
            "market": market,                        # Phase 7 catch-up — 시장 식별
            "kind": "catchup_cycle" if catchup else "cycle",   # catch-up 구분
            "n_strategies": len(strategies),
            "n_bought": sum(1 for d in decisions if d["action"] == "bought"),
            "n_sold": sum(1 for d in decisions if d["action"] == "sold"),
            "n_skip_gap": sum(1 for d in decisions if d["action"] == "skip_gap"),
            "n_skip_signal": sum(1 for d in decisions if d["action"] == "skip_signal"),
            "n_skip_held": sum(1 for d in decisions if d["action"] == "skip_held"),
            "n_rejected": sum(1 for d in decisions if d["action"] == "rejected"),
            "n_unfilled": sum(1 for d in decisions if d["action"] == "unfilled"),
            "n_errors": sum(1 for d in decisions if d["action"] == "error"),
            "kill_switch": ks_active,
            "equity_pre": equity_now,
            "equity_post": float(snap["balance"]["total_eval"]),
            # Phase 38.10 — drawdown 모니터
            "drawdown_pct": round(drawdown_pct, 3),
            "peak_equity": round(peak_equity, 2),
            "drawdown_active": drawdown_active,
            "max_drawdown_limit_pct": (float(max_drawdown_limit_pct)
                                          if max_drawdown_limit_pct is not None else None),
        }
        order_log.log_cycle(decisions, cycle_summary)

        # 포지션 풍부화 + 분석 집계 (Monitor용)
        positions_rich = analytics.enrich_positions(
            snap["positions"], self.ledger, today.isoformat())

        return {
            "balance": snap["balance"],
            "positions": positions_rich,
            "equity": self.equity[-365:],
            "trades": [d for d in decisions if d["action"] in ("bought", "sold")],
            "decisions": decisions,
            "broker_pending": broker_pending,
            "pending_local": list(self.pending.values()),
            "recent_orders": order_log.read_orders(50),
            "recent_cycles": order_log.read_cycles(10),
            "slippage": order_log.slippage_stats(),
            "kill_switch": killswitch.load(),
            "cycle_summary": cycle_summary,
            # Phase 13 — Monitor 고도화
            "strategy_pnl": analytics.strategy_pnl_summary(),
            "slippage_by_hour": analytics.slippage_by_hour(),
            "rejection_reasons": analytics.rejection_reasons(),
            "drawdown": analytics.drawdown_state(),
            "health": analytics.local_health(),
        }

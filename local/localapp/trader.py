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
import time
from datetime import date
from pathlib import Path

import quant_core as qc
from quant_core.exec_defaults import merged_execution

from .broker import Broker
from .config import (EQUITY_PATH, LEDGER_PATH, PENDING_ORDERS_PATH, TRADES_PATH)
from . import analytics, killswitch, order_log

log = logging.getLogger("localapp.trader")


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("파일 파싱 실패, 기본값 사용: %s", path)
    return default


def _save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _policy(strat_def: dict) -> dict:
    """전략 정의에서 ExecutionPolicy를 추출하고 글로벌 default와 병합."""
    return merged_execution(strat_def.get("execution") if strat_def else None)


def _gap_pct(prev_close: float, cur_price: float) -> float:
    """갭 % (양수 = 갭상승, 음수 = 갭하락)."""
    if prev_close <= 0:
        return 0.0
    return (cur_price - prev_close) / prev_close * 100


def _evaluate_exit(strat: qc.Strategy, cur_price: float, entry_price: float,
                   peak_price: float, held_days: int,
                   dataset: dict) -> str | None:
    """청산 사유 (없으면 None). 백테스트와 동일한 우선순위."""
    ex = strat.exit_rules
    cur_ret = (cur_price - entry_price) / entry_price * 100 if entry_price else 0.0

    if ex.take_profit is not None and cur_ret >= ex.take_profit:
        return "익절"
    if ex.stop_loss is not None and cur_ret <= ex.stop_loss:
        return "손절"
    if (ex.trail_pct is not None and peak_price > 0
            and cur_price <= peak_price * (1 - ex.trail_pct / 100)):
        return "트레일링스톱"
    if ex.hold_days is not None and held_days >= ex.hold_days:
        return "보유기간"
    if strat.sell and strat.sell.conditions:
        mask = qc.build_signal_mask(
            dataset, [c.model_dump() for c in strat.sell.conditions],
            strat.sell.logic)
        if not mask.empty and bool(mask.iloc[-1]):
            return "매도신호"
    return None


def _atr_qty(capital: float, atr: float, policy: dict, cur_price: float) -> int:
    """ATR 기반 포지션 사이징. cap에 의해 단일종목 한도로 클램프."""
    risk = capital * policy["atr_risk_pct"] / 100.0
    risk_per_share = atr * policy["atr_mult"]
    if risk_per_share <= 0:
        return 0
    qty = int(risk // risk_per_share)
    # 단일 종목 비중 상한
    cap_qty = int((capital * policy["max_position_pct"] / 100.0) // cur_price)
    return max(0, min(qty, cap_qty))


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

    # ── 영속화 ────────────────────────────────────────────────────────────────

    def _save(self):
        _save_json(LEDGER_PATH, self.ledger)
        EQUITY_PATH.write_text(json.dumps(self.equity, ensure_ascii=False),
                                encoding="utf-8")
        _save_json(PENDING_ORDERS_PATH, self.pending)

    def _log_trade(self, event: dict):
        with open(TRADES_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

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

        체결 → 원장 반영. 타임아웃 → 취소. 부분체결 → 부분만 반영.
        """
        if not self.pending:
            return
        now = time.time()
        for order_no, p in list(self.pending.items()):
            try:
                st = self.broker.order_status(order_no)
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
                del self.pending[order_no]
            else:
                # 여전히 미체결: 타임아웃 검사
                ts = float(p.get("submitted_ts", now))
                if now - ts > p.get("timeout_sec", 300):
                    try:
                        self.broker.cancel(order_no, p["symbol"], p["qty"])
                    except Exception as e:
                        log.warning("타임아웃 취소 실패 [%s]: %s", order_no, e)
                    order_log.log_order("timeout", p["symbol"], p["side"], p["qty"],
                                        order_no=order_no,
                                        intended_price=p.get("intended_price"),
                                        limit_price=p.get("limit_price"),
                                        strategy_name=p.get("strategy_name", ""))
                    decisions.append(order_log.decision(
                        "unfilled", p.get("strategy_id", ""),
                        p.get("strategy_name", ""), p["symbol"],
                        f"미체결 타임아웃 ({p.get('timeout_sec')}초)"))
                    del self.pending[order_no]

    def _apply_fill(self, order_no: str, p: dict, filled_qty: int,
                    fill_price: float, decisions: list[dict],
                    partial: bool = False) -> None:
        """체결을 원장·이벤트 로그에 반영."""
        sid = str(p.get("strategy_id", ""))
        symbol = p["symbol"]
        side = p["side"]
        intended = p.get("intended_price")
        today = date.today().isoformat()

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

    # ── 주문 발주 helpers ────────────────────────────────────────────────────

    def _submit_buy(self, sid: str, strat_name: str, strat_def: dict,
                    symbol: str, qty: int, ref_price: float, policy: dict,
                    decisions: list[dict]) -> None:
        use_limit = bool(policy["use_limit"])
        if use_limit:
            limit = qc.round_to_tick(
                ref_price * (1 + policy["buy_tolerance_pct"] / 100.0),
                direction="up")
            try:
                r = self.broker.buy_limit(symbol, qty, limit)
            except Exception as e:
                log.error("매수 지정가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"발주 예외: {e}"))
                return
        else:
            limit = 0
            try:
                r = self.broker.buy(symbol, qty)
            except Exception as e:
                log.error("매수 시장가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"발주 예외: {e}"))
                return
        self._after_submit(r, sid, strat_name, strat_def, symbol, "buy", qty,
                            ref_price, limit, policy, decisions, reason="매수신호")

    def _submit_sell(self, sid: str, strat_name: str, symbol: str, qty: int,
                     ref_price: float, policy: dict, reason: str,
                     decisions: list[dict]) -> None:
        use_limit = bool(policy["use_limit"])
        # 청산은 잡혀야 하므로 더 공격적인 tolerance 사용
        tol = policy["exit_tolerance_pct"]
        if use_limit:
            limit = qc.round_to_tick(ref_price * (1 - tol / 100.0),
                                      direction="down")
            try:
                r = self.broker.sell_limit(symbol, qty, limit)
            except Exception as e:
                log.error("매도 지정가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"발주 예외: {e}"))
                return
        else:
            limit = 0
            try:
                r = self.broker.sell(symbol, qty)
            except Exception as e:
                log.error("매도 시장가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"발주 예외: {e}"))
                return
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
            "timeout_sec": int(policy["unfilled_timeout_sec"]),
            "definition": strat_def or {}, "reason": reason,
            "filled_so_far": 0,
        }
        order_log.log_order("submitted", symbol, side, qty, order_no=order_no,
                             intended_price=intended_price,
                             limit_price=limit_price, strategy_name=strat_name,
                             reason=reason)
        # 일부 브로커(MockBroker, 일부 KIS 즉시체결)는 submit 응답에 체결정보 포함.
        # 그런 경우 pending 단계를 건너뛰고 바로 체결 반영.
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

    # ── 메인 사이클 ───────────────────────────────────────────────────────────

    def cycle(self, strategies: list[dict], dataset: dict,
              today: date | None = None) -> dict:
        """전략 목록을 1회 평가하고 매매한 뒤 동기화용 스냅샷을 반환한다."""
        today = today or date.today()
        decisions: list[dict] = []

        # ── 0. 이전 사이클 미체결 정리 ─────────────────────────────────────
        self._resolve_pending(decisions)

        # ── 1. 자본·day_start 갱신, kill switch 평가 ──────────────────────
        try:
            snap_pre = self.broker.account_snapshot()
            equity_now = float(snap_pre["balance"]["total_eval"])
        except Exception as e:
            log.error("잔고 조회 실패: %s", e)
            equity_now = 0.0
            snap_pre = {"balance": {"cash": 0, "total_eval": 0}, "positions": []}

        killswitch.update_day_start(equity_now, today.isoformat())
        ks_state = killswitch.load()
        ks_active = bool(ks_state.get("active"))

        # 글로벌 default를 미사용 시에도 적용하기 위해 빈 policy로 시작
        global_policy = merged_execution(None)

        if not ks_active:
            reason = killswitch.check_daily_loss(
                equity_now, global_policy["daily_loss_limit_pct"])
            if reason:
                killswitch.activate(reason)
                ks_active = True

        # ── 2. 청산 패스 ──────────────────────────────────────────────────
        sold_this_cycle: set[str] = set()
        for sid, pos in list(self.ledger.items()):
            cur = self._safe_price(pos["symbol"])
            if cur is None:
                continue
            pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), cur)

            try:
                strat = qc.Strategy(**pos["definition"])
            except Exception as e:
                log.warning("원장 전략 파싱 실패 [%s]: %s", sid, e)
                continue

            held = (today - date.fromisoformat(pos["entry_date"])).days
            reason = _evaluate_exit(strat, cur, pos["entry_price"],
                                    pos["peak_price"], held, dataset)
            # kill switch 활성 시 모든 보유 강제 청산
            if ks_active and not reason:
                reason = "kill-switch"

            if not reason:
                continue

            policy = _policy(pos.get("definition"))
            self._submit_sell(sid, pos.get("strategy_name", ""), pos["symbol"],
                              pos["qty"], cur, policy, reason, decisions)
            sold_this_cycle.add(sid)

        # ── 3. 진입 패스 (kill switch 활성 시 건너뜀) ────────────────────
        if ks_active:
            decisions.append(order_log.decision(
                "skip_killswitch", "", "", "",
                f"신규 진입 차단 — {ks_state.get('reason', '')}"))
        else:
            for s in strategies:
                sid = str(s["id"])
                strat_name = s.get("name", "")
                strat_def = s["definition"]
                if sid in self.ledger or sid in sold_this_cycle:
                    decisions.append(order_log.decision(
                        "skip_held", sid, strat_name,
                        strat_def.get("trade_symbol", ""),
                        "이미 보유 또는 당일 청산"))
                    continue
                try:
                    strat = qc.Strategy(**strat_def)
                except Exception as e:
                    log.warning("전략 파싱 실패 [%s]: %s", strat_name, e)
                    decisions.append(order_log.decision(
                        "error", sid, strat_name, "",
                        f"전략 파싱 실패: {e}"))
                    continue
                try:
                    if not qc.evaluate_buy_signal(strat, dataset):
                        decisions.append(order_log.decision(
                            "skip_signal", sid, strat_name, strat.trade_symbol,
                            "매수 신호 미충족"))
                        continue
                except Exception as e:
                    log.error("매수 신호 평가 실패 [%s]: %s", strat.name, e)
                    decisions.append(order_log.decision(
                        "error", sid, strat_name, strat.trade_symbol,
                        f"신호 평가 실패: {e}"))
                    continue

                cur = self._safe_price(strat.trade_symbol)
                if cur is None:
                    decisions.append(order_log.decision(
                        "error", sid, strat_name, strat.trade_symbol,
                        "가격 조회 실패"))
                    continue

                policy = _policy(strat_def)

                # 3-1. 갭 필터
                sdf = dataset.get(strat.trade_symbol)
                prev_close = 0.0
                if sdf is not None and len(sdf) >= 2 and "Close" in sdf.columns:
                    prev_close = float(sdf["Close"].iloc[-2])
                gap = _gap_pct(prev_close, cur) if prev_close > 0 else 0.0
                if prev_close > 0 and abs(gap) > policy["gap_filter_pct"]:
                    decisions.append(order_log.decision(
                        "skip_gap", sid, strat_name, strat.trade_symbol,
                        f"갭 필터 ({gap:+.2f}% > ±{policy['gap_filter_pct']}%)",
                        {"prev_close": prev_close, "cur_price": cur}))
                    continue

                # 3-2. 사이징
                try:
                    cash = self.broker.account_snapshot()["balance"]["cash"]
                except Exception as e:
                    log.error("잔고 조회 실패: %s", e)
                    decisions.append(order_log.decision(
                        "error", sid, strat_name, strat.trade_symbol,
                        f"잔고 조회 실패: {e}"))
                    break

                if policy["sizing_mode"] == "atr_risk":
                    atr_val = 0.0
                    if sdf is not None and "atr_14" in sdf.columns and len(sdf):
                        atr_val = float(sdf["atr_14"].iloc[-1] or 0.0)
                    capital = equity_now if equity_now > 0 else float(cash)
                    qty = _atr_qty(capital, atr_val, policy, cur)
                    # 잔고 한도
                    qty = min(qty, int(float(cash) // cur))
                else:
                    qty = int(float(cash) * strat.amount_pct / 100.0 // cur)

                if qty <= 0:
                    decisions.append(order_log.decision(
                        "skip_funds", sid, strat_name, strat.trade_symbol,
                        f"수량 부족 (현금 {cash:,.0f} / 가격 {cur:,.0f})"))
                    continue

                # 3-3. 주문 발주
                self._submit_buy(sid, strat_name, strat_def, strat.trade_symbol,
                                  qty, cur, policy, decisions)

        # ── 4. 미체결 짧게 대기 (시초가 동시호가 직후 대부분 잡힘) ───────
        self._wait_pending(global_policy["unfilled_timeout_sec"],
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

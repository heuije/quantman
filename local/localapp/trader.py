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

    # ── Phase 40 — KIS 잔고 ↔ ledger 정합성 자동 정정 ──────────────────────
    def reconcile_with_kis(self, today_iso: str | None = None) -> dict:
        """KIS 실 잔고와 ledger를 비교 → ledger_orphans는 자동 차감/제거.

        external_extras(외부 매수)는 ledger 손대지 않음 (자동매매가 매수한 게 아니므로).
        반환: reconcile dict + applied 변경 내역 + 거래 기록 카운트.

        호출 시점: 15:35 post_close_settlement (08:55 메인 사이클 직전엔 위험).
        """
        from datetime import date
        today = today_iso or date.today().isoformat()
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
        # Phase 38.9 — 매도 tolerance 단일화. 신호·청산 모두 같은 값.
        tol = policy["sell_tolerance_pct"]
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
                              decisions: list[dict]) -> bool:
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

        policy = _policy(strat_def)

        try:
            cash = self.broker.account_snapshot()["balance"]["cash"]
        except Exception as e:
            log.error("잔고 조회 실패: %s", e)
            decisions.append(order_log.decision(
                "error", strategy_id, strat_name, symbol,
                f"잔고 조회 실패: {e}"))
            return False

        # 사이징 — 전일 종가 기준
        if policy["sizing_mode"] == "atr_risk":
            atr_val = 0.0
            if "atr_14" in sdf.columns:
                atr_val = float(sdf["atr_14"].iloc[-1] or 0.0)
            if atr_val <= 0:
                decisions.append(order_log.decision(
                    "skip_no_atr", strategy_id, strat_name, symbol,
                    "ATR 데이터 없음 — atr_risk 모드 진입 불가 "
                    "(사이징을 자본 비율로 변경하거나 dataset 보강 필요)"))
                return False
            capital = equity_now if equity_now > 0 else float(cash)
            qty = _atr_qty(capital, atr_val, policy, prev_close)
            qty = min(qty, int(float(cash) // prev_close))
        else:
            qty = int(float(cash) * strat.amount_pct / 100.0 // prev_close)

        if qty <= 0:
            decisions.append(order_log.decision(
                "skip_funds", strategy_id, strat_name, symbol,
                f"수량 부족 (현금 {cash:,.0f} / 전일종가 {prev_close:,.0f})"))
            return False

        # 발주가는 _submit_buy 내부에서 prev_close × (1 + tolerance%) 계산
        self._submit_buy(ledger_key, strat_name, strat_def, symbol, qty,
                          prev_close, policy, decisions)
        return True

    # ── 메인 사이클 ───────────────────────────────────────────────────────────

    def _enter_from_preview(self, by_strategy: list[dict], strategies: list[dict],
                              dataset: dict, equity_now: float,
                              decisions: list[dict],
                              sold_this_cycle: set[str]) -> None:
        """Phase 37: 서버 preview의 candidates 종목을 직접 발주.

        매수 신호 재평가는 skip (preview가 어제 18:15에 이미 평가).
        잔고·사이징은 _try_buy_one_symbol이 발주 직전 KIS 재조회로 재계산 →
        밤사이 수동 거래·입금 반영. 보유/한도·중복 진입 체크는 기존과 동일.

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
                ledger_key = f"{sid}:{symbol}" if is_multi_key else sid
                if ledger_key in self.ledger or ledger_key in sold_this_cycle:
                    continue
                if self._try_buy_one_symbol(
                        ledger_key, sid, strat_name, strat_def, strat,
                        symbol, dataset, equity_now, decisions):
                    bought += 1
                    n_preview_used += 1

        log.info("preview 경로 진입 완료 — %d종목 발주 (신호 재평가 skip)",
                  n_preview_used)

    def cycle(self, strategies: list[dict], dataset: dict,
              today: date | None = None,
              buy_candidates: list[dict] | None = None,
              risk_limits: dict | None = None) -> dict:
        """전략 목록을 1회 평가하고 매매한 뒤 동기화용 스냅샷을 반환한다.

        buy_candidates(by_strategy 리스트, 비어있어도 list)가 신규 진입 source.
        Phase 38.4: 항상 preview 경로 — buy_candidates가 빈 리스트면 진입 0,
        청산은 정상. 호출자(runner)가 preview 누락 시 []로 전달.

        risk_limits(Phase 38.7/38.10): 사용자 위험 한도. 예:
          {"kill_switch_daily_loss_pct": 2.0, "max_drawdown_pct": 15.0}
        키가 없거나 None이면 글로벌 default 사용.
        """
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

        # Phase 38.7 — 사용자 설정 우선, null이면 글로벌 default
        rl = risk_limits or {}
        daily_loss_limit_pct = (rl.get("kill_switch_daily_loss_pct")
                                  if rl.get("kill_switch_daily_loss_pct") is not None
                                  else global_policy["daily_loss_limit_pct"])
        max_drawdown_limit_pct = (rl.get("max_drawdown_pct")
                                    if rl.get("max_drawdown_pct") is not None
                                    else global_policy["max_drawdown_pct"])

        if not ks_active:
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
        drawdown_active = drawdown_pct <= -abs(max_drawdown_limit_pct)
        if drawdown_active:
            log.warning(
                "drawdown 한도 도달 — 자본 고점 %s원 → 현재 %s원 (%.2f%%, 한도 -%.2f%%)",
                f"{peak_equity:,.0f}", f"{equity_now:,.0f}",
                drawdown_pct, max_drawdown_limit_pct)

        # ── 2. 청산 패스 (Phase 38.2: 신호·시간 기반만 — 가격은 intraday가 담당) ──
        sold_this_cycle: set[str] = set()
        for sid, pos in list(self.ledger.items()):
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
            self._submit_sell(sid, pos.get("strategy_name", ""), pos["symbol"],
                              pos["qty"], ref_price, policy, reason, decisions)
            sold_this_cycle.add(sid)

        # ── 3. 진입 패스 (kill switch·drawdown 활성 시 건너뜀, preview 전용) ──
        if ks_active:
            decisions.append(order_log.decision(
                "skip_killswitch", "", "", "",
                f"신규 진입 차단 — {ks_state.get('reason', '')}"))
        elif drawdown_active:
            decisions.append(order_log.decision(
                "skip_drawdown", "", "", "",
                f"신규 진입 차단 — 누적 drawdown {drawdown_pct:.2f}% "
                f"(한도 -{max_drawdown_limit_pct:.1f}%)"))
        elif buy_candidates is not None:
            self._enter_from_preview(buy_candidates, strategies, dataset,
                                       equity_now, decisions, sold_this_cycle)

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
            # Phase 38.10 — drawdown 모니터
            "drawdown_pct": round(drawdown_pct, 3),
            "peak_equity": round(peak_equity, 2),
            "drawdown_active": drawdown_active,
            "max_drawdown_limit_pct": float(max_drawdown_limit_pct),
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

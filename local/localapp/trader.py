"""모의투자 트레이딩 로직.

core 엔진으로 전략을 평가하고 Broker로 주문한다. 일봉 종가 기준 —
매수 신호 시 진입, 청산 규칙(익절·손절·트레일링·보유기간·매도신호) 중
먼저 트리거되는 것으로 청산한다. 백테스트 엔진과 동일한 우선순위.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import quant_core as qc

from .broker import Broker
from .config import EQUITY_PATH, LEDGER_PATH, TRADES_PATH

log = logging.getLogger("localapp.trader")


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("파일 파싱 실패, 기본값 사용: %s", path)
    return default


def _evaluate_exit(strat: qc.Strategy, cur_price: float, entry_price: float,
                   peak_price: float, held_days: int,
                   dataset: dict) -> str | None:
    """청산 사유를 반환 (없으면 None). 백테스트와 동일한 우선순위."""
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


class Trader:
    """Broker에 의존하는 모의투자 실행기. 보유 원장을 로컬에 유지한다.

    원장 항목은 전략 정의를 함께 보관하므로, 플랫폼에서 전략이 삭제돼도
    (고아 포지션) 저장된 규칙으로 안전하게 청산할 수 있다.
    """

    def __init__(self, broker: Broker):
        self.broker = broker
        self.ledger: dict[str, dict] = _load_json(LEDGER_PATH, {})
        self.equity: list[dict] = _load_json(EQUITY_PATH, [])

    def _save(self):
        LEDGER_PATH.write_text(json.dumps(self.ledger, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        EQUITY_PATH.write_text(json.dumps(self.equity, ensure_ascii=False),
                               encoding="utf-8")

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

    def cycle(self, strategies: list[dict], dataset: dict,
              today: date | None = None) -> dict:
        """전략 목록을 1회 평가하고 매매한 뒤 동기화용 스냅샷을 반환한다."""
        today = today or date.today()
        events: list[dict] = []
        sold_this_cycle: set[str] = set()

        # ── 청산 패스 ──────────────────────────────────────────────────────
        for sid, pos in list(self.ledger.items()):
            cur = self._safe_price(pos["symbol"])
            if cur is None:
                continue                                  # 가격 불명 → 보류
            pos["peak_price"] = max(pos.get("peak_price", pos["entry_price"]), cur)

            try:
                strat = qc.Strategy(**pos["definition"])
            except Exception as e:
                log.warning("원장 전략 파싱 실패 [%s]: %s", sid, e)
                continue

            held = (today - date.fromisoformat(pos["entry_date"])).days
            reason = _evaluate_exit(strat, cur, pos["entry_price"],
                                    pos["peak_price"], held, dataset)
            if not reason:
                continue
            try:
                r = self.broker.sell(pos["symbol"], pos["qty"])
            except Exception as e:
                log.error("매도 주문 실패 [%s]: %s", pos["symbol"], e)
                continue
            if r.get("success"):
                ev = {"ts": today.isoformat(), "action": "sell",
                      "symbol": pos["symbol"], "qty": pos["qty"],
                      "strategy": pos.get("strategy_name", ""), "reason": reason}
                events.append(ev)
                self._log_trade(ev)
                del self.ledger[sid]
                sold_this_cycle.add(sid)
                log.info("매도 %s %d주 (%s)", pos["symbol"], pos["qty"], reason)
            else:
                log.warning("매도 거부 [%s]: %s", pos["symbol"], r.get("message"))

        # ── 진입 패스 ──────────────────────────────────────────────────────
        for s in strategies:
            sid = str(s["id"])
            if sid in self.ledger or sid in sold_this_cycle:
                continue                                  # 보유 중 / 당일 청산
            try:
                strat = qc.Strategy(**s["definition"])
            except Exception as e:
                log.warning("전략 파싱 실패 [%s]: %s", s.get("name"), e)
                continue
            try:
                if not qc.evaluate_buy_signal(strat, dataset):
                    continue
            except Exception as e:
                log.error("매수 신호 평가 실패 [%s]: %s", strat.name, e)
                continue

            cur = self._safe_price(strat.trade_symbol)
            if cur is None:
                continue
            try:
                cash = self.broker.account_snapshot()["balance"]["cash"]
            except Exception as e:
                log.error("잔고 조회 실패: %s", e)
                break
            qty = int(cash * strat.amount_pct / 100 // cur)
            if qty <= 0:
                log.info("매수 수량 부족: %s (현금 %.0f, 가격 %.0f)",
                         strat.trade_symbol, cash, cur)
                continue
            try:
                r = self.broker.buy(strat.trade_symbol, qty)
            except Exception as e:
                log.error("매수 주문 실패 [%s]: %s", strat.trade_symbol, e)
                continue
            if r.get("success"):
                self.ledger[sid] = {
                    "symbol": strat.trade_symbol, "qty": qty,
                    "entry_date": today.isoformat(), "entry_price": cur,
                    "peak_price": cur, "strategy_name": strat.name,
                    "definition": s["definition"],
                }
                ev = {"ts": today.isoformat(), "action": "buy",
                      "symbol": strat.trade_symbol, "qty": qty,
                      "strategy": strat.name, "reason": "매수신호"}
                events.append(ev)
                self._log_trade(ev)
                log.info("매수 %s %d주 @ %.0f", strat.trade_symbol, qty, cur)
            else:
                log.warning("매수 거부 [%s]: %s", strat.trade_symbol, r.get("message"))

        # ── 스냅샷 ─────────────────────────────────────────────────────────
        snap = self.broker.account_snapshot()
        self.equity.append({"date": today.isoformat(),
                            "value": snap["balance"]["total_eval"]})
        self._save()
        return {
            "balance": snap["balance"],
            "positions": snap["positions"],
            "equity": self.equity[-365:],
            "trades": events,
        }

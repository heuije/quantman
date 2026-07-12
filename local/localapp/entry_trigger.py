"""워치리스트 장중 돌파 진입 매니저 — watchlist_trigger_v1 (장중 템플릿 설계 §4).

intraday_loop(매도 전용)이 이 매니저를 물려 장중 **진입** 감시를 더한다: tick의 현재가가
전일 종가 대비 임계(%) 이상이면 발화 — 판정 숫자의 단일 출처는 IR 정규형 신호
(quant_core.ir_engine.templates.watchlist_params — 백테스트 fill="trigger"와 같은 임계).

발화 시 진입은 기존 경로 전면 재사용: `_enter_from_preview`(합성 후보 1건,
entry_window="intraday")가 커버리지·계좌·사이징·L-01 멱등 등 종목 게이트를 전수 상속하고,
발화 전 `_close_entry_blocked`(킬스위치·일일손실·drawdown — 이름은 '종가'지만 의미는 신규
진입 게이트 미러)가 선다. 멱등은 2중: **디스크 발화 기록(전략×종목×일 1회 — M9: 장수명
인스턴스라 디스크가 SSOT)** + trader의 보유 멱등. 기록 저장이 발주보다 먼저다 — 저장 실패
시 발주하지 않는다(중복 발주 방지가 기회 손실보다 우선, fail-safe).
"""

from __future__ import annotations

import json
import logging
import threading

from . import order_log
from .config import APP_DIR
from .sync_client import push_snapshot
from .trader import _CYCLE_LOCK, _unified_equity_krw, kst_today

log = logging.getLogger("localapp.entry_trigger")

FIRED_PATH = APP_DIR / "trigger_fired.json"


class EntryTriggerManager:
    """워치리스트 템플릿 전략들의 장중 발화·진입. 시장=KRX 전용(템플릿 선언과 일치)."""

    def __init__(self, trader, broker, strategies: list[dict], dataset: dict,
                 risk_limits: dict | None, market: str = "KRX"):
        from quant_core.ir_engine import StrategyIR
        from quant_core.ir_engine.templates import WATCHLIST_TRIGGER, watchlist_params

        self.trader, self.broker = trader, broker
        self.rl = risk_limits or {}
        self.market = market
        self._dataset = dataset
        self._fire_lock = threading.Lock()      # tick 폭주 직렬화 — 발주 중 재발화 차단
        self._watch: dict[str, list[dict]] = {}  # sym → [{sid,name,thr,max}]
        self._prev_close: dict[str, float] = {}
        self._strategy_by_id: dict[str, dict] = {}

        for s in strategies or []:
            d = s.get("definition") or {}
            if ((d.get("template") or {}).get("id")) != WATCHLIST_TRIGGER:
                continue
            try:
                p = watchlist_params(StrategyIR.model_validate(d))
            except Exception as e:  # noqa: BLE001 — 결함 전략 1개가 나머지 감시를 막으면 안 됨
                log.warning("[트리거] 전략 #%s 파라미터 추출 실패 — 감시 제외: %s",
                            s.get("id"), e)
                continue
            sid = str(s.get("id"))
            self._strategy_by_id[sid] = s
            for sym in p["symbols"]:
                pc = self._last_close(dataset, sym)
                if pc is None or pc <= 0:
                    # 판정 기준가(전일 종가) 없이 감시하면 오발화 — 그 종목만 제외·표면화.
                    log.warning("[트리거] %s 전일 종가 없음 — 전략 #%s에서 감시 제외", sym, sid)
                    continue
                self._prev_close[sym] = pc
                self._watch.setdefault(sym, []).append(
                    {"sid": sid, "name": s.get("name", ""),
                     "thr": float(p["threshold_pct"]), "max": int(p["max_entries"])})

    @staticmethod
    def _last_close(dataset: dict, sym: str):
        df = dataset.get(sym)
        if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
            return None
        try:
            return float(df["Close"].dropna().iloc[-1])
        except (IndexError, TypeError, ValueError):
            return None

    @property
    def active(self) -> bool:
        return bool(self._watch)

    def watch_symbols(self) -> set[str]:
        return set(self._watch)

    # ── 발화 기록 (디스크 SSOT — 날짜 키로 자연 리셋) ────────────────────────────
    @staticmethod
    def _load_fired() -> dict:
        try:
            return json.loads(FIRED_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _save_fired(data: dict) -> None:
        FIRED_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # ── tick 경로 ────────────────────────────────────────────────────────────────
    def on_tick(self, sym: str, price: float) -> None:
        entries = self._watch.get(sym)
        if not entries or price is None or price <= 0:
            return
        pc = self._prev_close.get(sym, 0.0)
        if pc <= 0:
            return
        chg = (price / pc - 1.0) * 100.0
        for e in entries:
            if chg >= e["thr"]:
                self._fire(e, sym, price, chg)

    def _fire(self, e: dict, sym: str, price: float, chg: float) -> None:
        with self._fire_lock:
            fired = self._load_fired()
            today = kst_today().isoformat()
            day = fired.get(today) or {}
            key = f"{e['sid']}:{sym}"
            if key in day:
                return                                   # 전략×종목×일 1회(재발화 무시)
            n_ok = sum(1 for k, v in day.items()
                       if k.startswith(e["sid"] + ":") and v == "ok")
            if n_ok >= e["max"]:
                day[key] = "capped"                      # 상한 도달 — 기록해 재틱 로그 스팸 차단
                fired[today] = day
                self._save_fired(fired)
                log.info("[트리거] 전략 #%s 일일 진입 상한(%d) 도달 — %s 발화 무시",
                         e["sid"], e["max"], sym)
                return
            # 멱등 기록을 발주보다 먼저 — 저장 실패면 발주하지 않는다(중복 방지 우선).
            day[key] = "pending"
            fired[today] = day
            self._save_fired(fired)

            log.info("[트리거] 발화 — 전략 #%s %s +%.1f%%(임계 %.1f%%) @%.0f",
                     e["sid"], sym, chg, e["thr"], price)
            decisions: list[dict] = []
            status = "ok"
            try:
                with _CYCLE_LOCK:                        # 사이클(종가창·ks)과 직렬화
                    self.trader.reload_state()           # M9 — 디스크 SSOT 최신화
                    snap = self.broker.account_snapshot()
                    equity = _unified_equity_krw(snap.get("balance") or {})
                    if self.trader._close_entry_blocked(equity, snap, self.rl, decisions):
                        status = "blocked"               # 킬스위치·손실한도·drawdown — 진입 차단
                    else:
                        self.trader._enter_from_preview(
                            [{"strategy_id": e["sid"],
                              "candidates": [{"symbol": sym, "direction": "long"}]}],
                            [self._strategy_by_id[e["sid"]]], self._dataset, equity,
                            decisions, set(), market=self.market,
                            entry_window="intraday")
            except Exception as ex:  # noqa: BLE001 — 발화 1건 실패가 loop를 죽이면 안 됨
                status = "error"
                log.error("[트리거] 진입 실행 실패(%s %s): %s", e["sid"], sym, ex)
                decisions.append(order_log.decision(
                    "trigger_error", e["sid"], e["name"], sym, f"트리거 진입 실패: {ex}"))
            fired = self._load_fired()                   # 발주 중 다른 발화와 병합
            fired.setdefault(today, {})[key] = status
            self._save_fired(fired)
            self._push(decisions)

    def _push(self, decisions: list[dict]) -> None:
        """발화 결과를 서버로 — ks-trigger push와 동형(실패는 경고만, 발주 결과는 로컬 SSOT)."""
        try:
            snap = self.broker.account_snapshot()
            push_snapshot({
                "balance": snap.get("balance", {}),
                "positions": snap.get("positions", []),
                "decisions": decisions,
                "cycle_summary": {"today": kst_today().isoformat(),
                                  "kind": "intraday_trigger",
                                  "n_decisions": len(decisions)},
            })
        except Exception as ex:  # noqa: BLE001
            log.warning("[트리거] push 실패: %s", ex)

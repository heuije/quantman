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
from quant_core.exec_defaults import instrument_spec, merged_execution
from quant_core.futures_expiry import roll_lead_days

from .broker import Broker
from .config import (EQUITY_PATH, LEDGER_PATH, PENDING_ORDERS_PATH,
                     TRADES_PATH)
from . import analytics, intents, killswitch, order_log, state_store

log = logging.getLogger("localapp.trader")

# Q5(AL-4)/M3: 트레이더 공유 상태(ledger·pending·equity) 변경의 단일 직렬화 락.
# 모듈 레벨로 두는 이유: trader 인스턴스가 cycle/settlement에서 매번 새로
# 만들어지므로 인스턴스 lock으로는 직렬화가 안 된다. 같은 PC 단일 프로세스
# 가정이라 모듈 락이 안전. cycle/settlement뿐 아니라 _apply_fill·_after_submit·
# cancel_all_pending·reconcile_with_kis 등 모든 변경 진입점이 이 락 안에서만
# ledger/pending을 만진다 — WS 체결 thread·60초 monitor·스케줄러 cycle이 같은
# dict를 동시 변경하는 race를 차단한다.
# RLock인 이유: cycle이 락을 쥔 채 _resolve_pending→_apply_fill로 재진입하므로
# 같은 thread 재획득이 필요하다(일반 Lock이면 self-deadlock). 데드락의 다른
# 경로(_apply_fill→ks hook→cycle)는 hook을 임계구역 밖에서 호출해 회피한다.
_CYCLE_LOCK = threading.RLock()


def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("파일 파싱 실패, 기본값 사용: %s", path)
    return default


def _save_json(path: Path, obj) -> None:
    """민감 상태 원자적 저장 + owner-only ACL — state_store 단일 경로 위임 (R5).

    원장·equity·pending 등은 잔고·손익을 담은 민감정보다. 원자성(L-02 크래시 시
    부분기록 손상 방지)과 ACL(같은 PC 타 사용자 차단)을 항상 함께 보장한다.
    """
    state_store.save_json(path, obj)


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
    """결제 통화 — 미국 종목이면 USD, 그 외 KRW. 사이징·잔고 단위 결정.

    ⚠ 선물엔 이 함수를 사이징 통화로 쓰지 않는다(해외선물을 USD로 보면 buying_power_usd
    주식 API로 라우팅돼 깨짐). 지정가 호가단위는 _round_limit이 instrument_spec으로 분기."""
    from . import market_index
    return "USD" if market_index.is_us(symbol) else "KRW"


def _round_limit(price: float, direction: str, symbol: str) -> float:
    """지정가 호가단위 라운딩 — 선물은 계약 틱(instrument_spec.tick), 주식은 통화별 호가표(C3).

    선물(KOSPI200 0.05·GC 0.10·NQ 0.25·BTC 5 등)은 계약 틱 그리드에 맞춰야 KIS가 수용한다 —
    통화별 정수 라운딩은 BTC(틱5)처럼 그리드를 이탈하거나 0.05·0.25 틱 정밀도를 잃는다.
    round_to_tick은 tick>0이면 통화 무관 틱 배수로 라운딩하므로 선물엔 tick만 넘긴다.
    주식은 tick=0(기본) → 통화별 호가표 그대로 — **주식 동작 byte-identical**."""
    spec = instrument_spec(symbol)
    if spec.asset_class == "futures":
        return qc.round_to_tick(price, direction=direction, tick=spec.tick)
    return qc.round_to_tick(price, direction=direction, currency=_currency_of(symbol))


def _market_group_safe(symbol: str) -> str:
    """시장 그룹('US'|'KRX') — 라우팅 불확실 시 국내 기본(안전)."""
    from . import market_index
    try:
        return market_index.market_group_of(symbol)
    except Exception:
        return "KRX"


def _unified_equity_krw(bal: dict) -> float:
    """국내+해외 통합 자산(KRW) — kill switch·drawdown용 계좌 전체 equity.

    주식 국내 평가금액 + 외화 평가총액(KRW) + USD 예수금(KRW 환산) + 선물계좌 추정예탁자산(KRW).

    선물계좌는 별도 KIS 계좌라 그 equity(BrokerRouter가 잔고병합 시 채우는 futures_eval_krw)를
    합산해야 kill-switch·drawdown이 선물 PnL을 인지한다(미합산 시 선물 손익이 안전망에 안 잡힘).
    ⚠단일 풀 모델: 주식+선물을 한 위험풀로 본다 — 선물 단독 급락이 주식 자본에 희석돼 보호가
    약해질 수 있다(주식 비중 큰 사용자). 계좌별 분리 kill-switch는 Phase 3(실거래) 전 재검토.
    선물 미등록 사용자는 futures_eval_krw 키 부재 → 주식 동작 byte-identical.
    """
    dom = float(bal.get("total_eval", 0) or 0)
    foreign = float(bal.get("foreign_eval_krw", 0) or 0)
    usd_cash = float(bal.get("cash_usd", 0) or 0)
    fx = float(bal.get("fx_usdkrw", 0) or 0)
    futures = float(bal.get("futures_eval_krw", 0) or 0)
    return dom + foreign + usd_cash * fx + futures


def _gap_pct(prev_close: float, cur_price: float) -> float:
    """갭 % (양수 = 갭상승, 음수 = 갭하락)."""
    if prev_close <= 0:
        return 0.0
    return (cur_price - prev_close) / prev_close * 100


def _exit_reason_for(defn: dict, held_days: int,
                     dataset: dict, symbol: str,
                     *, is_close: bool = False) -> tuple[str | None, object | None]:
    """청산 사유를 IR 엔진(전략 연구소)으로 평가. (reason, None) 튜플 반환.

    ir_engine.live.cycle_exit_reason이 보유기간+매도조건 Node를 백테스트와 동일한
    IR exit 스펙으로 평가한다. 두 번째 원소는 항상 None — 호출자는 None이면
    리밸런스·sell_rules(operand) 경로를 건너뛰고 전량(100%) 청산한다.
    파싱 실패는 예외 전파(호출자가 잡아 skip).

    is_close: 종가 사이클 여부. 당일매매(hold_days==0)만 영향 — 종가 사이클(True)에서만
    "당일청산". 일반 cycle 루프는 종가 사이클이 아니므로 기본 False(종전 동작 byte-identical).
    """
    from quant_core.ir_engine import StrategyIR
    from quant_core.ir_engine import live as ir_live
    ir = StrategyIR.model_validate(defn)
    return ir_live.cycle_exit_reason(
        ir, held_days=held_days, dataset=dataset, symbol=symbol,
        is_close=is_close), None


def held_qty_from_snapshot(snap: dict, symbol: str, side: str = "long") -> int:
    """account_snapshot positions에서 symbol·side의 실 보유 수량 합 — 단일 출처(L-04).

    청산 발주 직전 over-close 클램프의 공유 헬퍼. 장중 손절(intraday_stop)과 EOD
    cycle 청산이 같은 기준으로 'KIS가 실제로 들고 있는 수량'을 읽어, ledger가
    외부 수동매매로 drift해도 보유 초과 청산을 발주하지 않는다.

    M5b: side 인지(롱/숏 분리). 주식·롱은 side='long' 기본(positions에 side 없으면 norm_side가
    long→포함) — 종전 동작 보존. 숏 환매 클램프는 side='short'로 호출.
    """
    from .analytics import norm_side
    return sum(int(p.get("qty") or 0)
               for p in (snap or {}).get("positions", [])
               if p.get("symbol") == symbol and norm_side(p.get("side")) == side)


def clamp_sell_qty(broker_qty: int | None, ledger_qty: int) -> int | None:
    """L-04 매도 수량 클램프(단일 출처) — 모든 매도 경로가 같은 규칙으로 KIS 실
    보유에 맞춰 수량을 제한한다. EOD cycle·장중 tick 손절이 각자 다르게 구현해
    한쪽(EOD)이 클램프를 통째로 빠뜨렸던 결함 class(8cd5e8b 사후 보완)를 막는다.

    반환: None=잔고 미상(skip·재시도), 0=외부 매도로 보유 0(skip), 그 외=min(보유, 원장).
    호출부는 None/0의 부수효과(로그·sold_today·decision)만 각자 처리한다.
    """
    if broker_qty is None:
        return None
    if broker_qty <= 0:
        return 0
    return min(int(broker_qty), int(ledger_qty))


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
        # 미국 매수여력 모드 (cycle에서 risk_limits로 설정). 기본 통합증거금.
        self._us_bp_mode: str = "integrated"
        # Q5: 체결 후(_apply_fill) 즉시 kill switch 평가용 한도. cycle 진입 시
        # risk_limits에서 채워진다. 호출자가 설정 안 했으면 평가 skip(보수적 무동작).
        self._daily_loss_limit_pct: float | None = None
        # Q5: kill switch 발동 시 추가 동작을 외부에 알리는 hook (intraday_loop이
        # 보유 종목 강제 청산 cycle을 트리거하도록). None이면 발동만 기록.
        self._ks_trigger_hook = None
        # Q5(데드락 방지): "현재 스레드가 cycle 안인가"를 나타내는 thread-local 플래그.
        # _apply_fill의 ks 평가/hook은 cycle 외부 스레드에서만 동작 — cycle 내부의
        # _apply_fill(미체결 정리·_wait_pending 폴링)에서 hook이 cycle을 재호출하면
        # _CYCLE_LOCK 재진입+무한재귀. 인스턴스 bool이면 cycle이 도는 동안 다른
        # 스레드(WS _on_exec_event)의 *정당한* 체결 ks 평가까지 오억제되므로(REV-D)
        # thread-local로 둔다. 아래 _in_cycle property가 이 tls에 위임한다.
        self._cycle_tls = threading.local()
        # 미국 정상 cycle에서 True — _submit_buy/_submit_sell이 예약주문(개장 전
        # 접수 → 개시 자동전송)으로 라우팅. _cycle_body가 매 cycle 갱신.
        self._reserved_us = False

    @property
    def _in_cycle(self) -> bool:
        """현재 스레드가 cycle 본문 안인지(thread-local). cycle 스레드만 True."""
        return getattr(getattr(self, "_cycle_tls", None), "active", False)

    @_in_cycle.setter
    def _in_cycle(self, value: bool) -> None:
        # __new__로 __init__ 우회한 테스트 스텁도 안전하도록 tls를 lazy 생성.
        tls = getattr(self, "_cycle_tls", None)
        if tls is None:
            tls = threading.local()
            object.__setattr__(self, "_cycle_tls", tls)
        tls.active = bool(value)

    # ── 영속화 ────────────────────────────────────────────────────────────────

    def _save(self):
        # L-02: 4파일 모두 원자적 저장 (_save_json은 tmp+os.replace 패턴).
        # 4파일 cross-consistency는 여전히 미보장(파일별 원자성만)이지만, 부분
        # truncate에 의한 원장 소실은 차단된다.
        _save_json(LEDGER_PATH, self.ledger)
        _save_json(EQUITY_PATH, self.equity)
        _save_json(PENDING_ORDERS_PATH, self.pending)

    def _log_trade(self, event: dict):
        # 체결·거래 기록은 민감 — state_store 위임 (R5, 최초 생성 시 owner-only ACL).
        state_store.append_jsonl(event, TRADES_PATH)

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

        # M3: self.ledger 비교(reconcile_ledger)부터 차감·_save까지 단일 락 안에서
        # — 같은 락을 쓰는 cycle·WS 체결이 비교와 변경 사이에 ledger를 바꿔
        # stale 계획으로 차감하는 race를 막는다. account_snapshot(네트워크)은
        # self.ledger를 읽지 않으므로 락 밖에 둔다. (settlement 경로는 이미 이
        # 락을 쥐고 들어오며 RLock이라 재진입 안전, GUI 수동 호출 경로를 닫는다.)
        with _CYCLE_LOCK:
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
        # M3/INV-CONC-1: pending 순회·order_status·_apply_fill·del을 단일 락으로
        # 직렬화한다. WS 체결 스레드(_on_exec_event)가 같은 pending을 동시 변경하는
        # race(이중 del→KeyError, 부분반영 재가산→over-position)를 차단. RLock이라
        # cycle/settlement가 이미 락을 쥔 채 호출해도 재진입 안전.
        with _CYCLE_LOCK:
            self._resolve_pending_locked(decisions)

    def _resolve_pending_locked(self, decisions: list[dict]) -> None:
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
                # filled/partial 모두 KIS 누적(tot_ccld_qty) 기준 — 이미 WS/이전 폴링이
                # 반영한 filled_so_far를 차감해 잔여 delta만 반영한다. 차감 안 하면
                # 부분 선반영분이 재가산돼 over-position(INV-FILL-1 위반).
                already = int(p.get("filled_so_far", 0) or 0)
                delta = filled - already
                if delta > 0:
                    self._apply_fill(order_no, p, delta, fill_px, decisions)
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

    def _record_contract_meta(self, sid: str, symbol: str) -> None:
        """신규 진입 ledger에 라이브 계약코드·만기일ISO 부착 (M6 만기 자동청산).

        만기일은 진입 시점에 확정해 보관한다(현 front-month는 보유 중 롤로 바뀌므로 진입 때
        고정). 비선물·브로커 미지원(SimBroker·구 배선)·해석 실패 시 키를 붙이지 않는다 —
        **주식은 키가 안 붙어 ledger byte-identical**, 선물은 만기 미해석 시 백스톱만 비활성."""
        if not qc.is_futures(symbol):
            return                            # 주식: 만기 개념 없음 → 키 무부착(무변경)
        lg = self.ledger.get(sid)
        if lg is None:
            return
        fn = getattr(self.broker, "contract_expiry", None)
        if fn is None:                        # SimBroker·구 배선 등 — 만기 해석 미지원
            return
        try:
            code, exp = fn(symbol)
        except Exception as e:                # noqa: BLE001 — 해석 실패는 미기록(진입 비차단)
            log.warning("계약/만기 해석 실패 [%s]: %s", symbol, e)
            return
        if code:
            lg["contract_code"] = code
        if exp:
            lg["expiry_date"] = exp.isoformat()
        else:
            log.warning("선물 진입 만기일 미해석 [%s] — 만기 백스톱 비활성", symbol)

    def _expiry_close_reason(self, pos: dict, today: date) -> str | None:
        """만기 백스톱(M6 tier-2) — 선물이 만기 임박이면 강제청산 사유, 아니면 None.

        유저 청산규칙(_exit_reason_for)이 None일 때만 평가된다(tier-1 우선: 유저 롤오버/청산
        규칙이 먼저, 미발동 시 이 백스톱이 만기 前 강제청산해 물리인도·현금정산으로 포지션이
        사라지는 사고를 막는다). 만기일은 진입 시 ledger에 기록(_record_contract_meta).
        lead = 상품 default_roll(days_before:N), 없으면 5일(futures_expiry.roll_lead_days).
        만기 미기록(M6 이전 진입 등)이면 만기를 알 수 없어 백스톱 불가 → 경고 후 None."""
        if not qc.is_futures(pos.get("symbol", "")):
            return None
        exp_iso = pos.get("expiry_date")
        if not exp_iso:
            log.warning("선물 보유 만기일 미기록 [%s] — 만기 백스톱 평가 불가(수동 점검 필요)",
                        pos.get("symbol"))
            return None
        try:
            exp = date.fromisoformat(exp_iso)
        except (TypeError, ValueError):
            return None
        lead = roll_lead_days(instrument_spec(pos["symbol"]).default_roll)
        if today >= exp - timedelta(days=lead):
            return f"만기 자동청산(만기 {exp_iso})"
        return None

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

        # M3: self.ledger 읽기-수정-쓰기를 단일 락으로 직렬화 — WS 체결 thread·
        # monitor·cycle이 같은 원장을 동시 변경하는 race(lost update) 차단.
        with _CYCLE_LOCK:
            # M4 — 선물 인지 회계: ledger에 side(long/short) 추적, 청산 시 정산손익 기록.
            # 주식(equity)은 항상 long·multiplier=1이라 기존 경로와 동일(side="long" 키만 추가).
            # 선물 정산손익 = (청산−진입)×계약수×승수×부호(롱+1/숏−1).
            spec = instrument_spec(symbol)
            is_fut = spec.asset_class == "futures"
            mult = spec.multiplier
            realized: float | None = None       # 선물 청산 실현손익(정산)
            pxs = f"${fill_price:,.2f}" if spec.currency == "USD" else f"{fill_price:,.2f}"

            if side == "buy":
                lg = self.ledger.get(sid)
                if lg is not None and lg.get("side", "long") == "short":
                    # 숏 환매(close/축소) — 선물만 숏 보유 가능. 정산 = (진입−청산)×계약×승수.
                    realized = (lg["entry_price"] - fill_price) * filled_qty * mult
                    lg["qty"] -= filled_qty
                    if lg["qty"] <= 0:
                        del self.ledger[sid]
                elif lg is not None:
                    # 추가 매수(롱) — 평균단가 갱신
                    total = lg["qty"] + filled_qty
                    # L-05 — 정상 경로엔 두 값 모두 양수라 안전하나, 경로 변경 또는
                    # 비정상 fill_qty/ledger qty=0 잔존 시 ZeroDivisionError 잠재. 1줄 가드.
                    if total <= 0:
                        return
                    lg["entry_price"] = (lg["entry_price"] * lg["qty"]
                                          + fill_price * filled_qty) / total
                    lg["qty"] = total
                else:
                    # 신규 롱 진입
                    self.ledger[sid] = {
                        "symbol": symbol, "qty": filled_qty,
                        "entry_date": today, "entry_price": fill_price,
                        "peak_price": fill_price, "side": "long",
                        "strategy_name": p.get("strategy_name", ""),
                        "definition": p.get("definition", {}),
                    }
                    self._record_contract_meta(sid, symbol)
                ev = {"ts": today, "action": "buy", "symbol": symbol,
                      "qty": filled_qty, "price": fill_price,
                      "strategy": p.get("strategy_name", ""),
                      "reason": ("숏청산" if realized is not None else "매수신호")}
                if realized is not None:
                    ev["realized_pnl"] = round(realized, 2)
                self._log_trade(ev)
                if is_fut:
                    detail = f"{filled_qty}계약 @ {pxs}"
                    if realized is not None:
                        detail += f" 정산 {realized:+,.0f}"
                else:
                    detail = f"{filled_qty}주 @ {fill_price:,.0f}원"
                decisions.append(order_log.decision(
                    "bought", sid, p.get("strategy_name", ""), symbol, detail,
                    {"intended": intended, "fill": fill_price}))
            else:
                lg = self.ledger.get(sid)
                if lg is not None and lg.get("side", "long") == "short":
                    # 숏 추가(확대) — 평균단가 갱신
                    total = lg["qty"] + filled_qty
                    if total <= 0:
                        return
                    lg["entry_price"] = (lg["entry_price"] * lg["qty"]
                                          + fill_price * filled_qty) / total
                    lg["qty"] = total
                elif lg is not None:
                    # 롱 청산/축소 — 선물이면 정산손익 = (청산−진입)×계약×승수.
                    if is_fut:
                        realized = (fill_price - lg["entry_price"]) * filled_qty * mult
                    lg["qty"] -= filled_qty
                    if lg["qty"] <= 0:
                        del self.ledger[sid]
                elif is_fut:
                    # 신규 숏 진입 (선물만 — sell-to-open). 주식은 보유 없는 매도=무동작(보존).
                    self.ledger[sid] = {
                        "symbol": symbol, "qty": filled_qty,
                        "entry_date": today, "entry_price": fill_price,
                        "peak_price": fill_price, "side": "short",
                        "strategy_name": p.get("strategy_name", ""),
                        "definition": p.get("definition", {}),
                    }
                    self._record_contract_meta(sid, symbol)
                ev = {"ts": today, "action": "sell", "symbol": symbol,
                      "qty": filled_qty, "price": fill_price,
                      "strategy": p.get("strategy_name", ""),
                      "reason": p.get("reason", "")}
                if realized is not None:
                    ev["realized_pnl"] = round(realized, 2)
                self._log_trade(ev)
                if is_fut:
                    detail = f"{filled_qty}계약 @ {pxs}"
                    if realized is not None:
                        detail += f" 정산 {realized:+,.0f}"
                    if p.get("reason"):
                        detail += f" ({p.get('reason')})"
                else:
                    detail = f"{filled_qty}주 @ {fill_price:,.0f}원 ({p.get('reason', '')})"
                decisions.append(order_log.decision(
                    "sold", sid, p.get("strategy_name", ""), symbol, detail))

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
        # M3: pending 스냅샷만 락 안에서 — KIS cancel(네트워크)은 락 밖에서 수행.
        # cancel은 self.pending을 변경하지 않으므로(회수는 _resolve_pending이 담당)
        # 스냅샷 후 락을 놓아 critical section을 짧게 유지한다.
        with _CYCLE_LOCK:
            if not self.pending:
                return 0
            items = list(self.pending.items())
        n = 0
        for order_no, p in items:
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

    def _is_reserved_us(self, symbol: str) -> bool:
        """미국 예약주문 라우팅 여부 — 정상 cycle US 진입(_reserved_us)이고 USD 종목.

        M3: _submit_buy/_submit_sell에 동일 derivation이 중복돼 있던 것을 단일화.
        _reserved_us는 __init__/매 cycle에서 항상 설정되므로 getattr 방어 불필요.

        ⚠ M5b: **선물 제외**. 예약주문은 *대상 시장이 닫힌 시점*(미국주식)에 예약하는 흐름인데,
        선물은 국내(KRX 주간)·해외(CME 거의 24h) 모두 예약 개념이 없다(KisFuturesBroker는
        예약 메서드가 NotImplementedError 가드). 통화 휴리스틱상 해외선물(USD)이 여기 걸려
        롱 진입조차 깨지던 것을 차단 — 선물은 즉시주문(buy_limit/sell_limit) 경로로 간다.
        """
        return (self._reserved_us and _currency_of(symbol) == "USD"
                and not qc.is_futures(symbol))

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

        # 미국 예약매수 — 개장 전(접수창) 발주, 정규장 개시에 KIS가 자동 전송.
        # KIS는 미국 시장가 매수가 없어 지정가만 가능 → buy_tolerance 반영 limit.
        # catch-up과 배타적(_reserved_us는 정상 cycle US에서만 True).
        is_resv = self._is_reserved_us(symbol)
        if is_resv:
            limit = _round_limit(
                ref_price * (1 + policy["buy_tolerance_pct"] / 100.0),
                "up", symbol)
            limit = qc.apply_daily_price_limit(
                limit, ref_price, "buy", _currency_of(symbol))
            try:
                r = self.broker.buy_resv_limit(symbol, qty, limit)
            except Exception as e:
                intents.mark_failed(today_iso, intent_id, f"buy_resv_limit: {e}")
                log.error("미국 예약매수 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"예약발주 예외: {e}"))
                return
            log.info("[us-resv] %s 예약매수 지정가 limit=%s", symbol, limit)
        # catch-up + 시장가 매수: 시초가 limit으로 변환.
        # 이유: 정상 cycle의 시장가는 09:00 시초가에 체결되나 catch-up은 09:30
        # 현재가에 체결 → 백테스트 가정(시가 + slippage)과 어긋남. 시가 × (1 +
        # bt_slippage_bps) limit으로 변환하면 백테스트 모델과 alignment + selection
        # bias 없음(가격은 시가 fixed). ref_price(어제 종가)는 유지 — apply_daily_
        # price_limit이 prev_close 기준 ±30% cap 정확히 계산하도록.
        elif catchup and not use_limit:
            open_price = self.broker.today_open(symbol)
            if open_price <= 0:
                # v0.9.7-beta — PR-1 정당 fallback (KIS API 진짜 한계 대비).
                # _open_overseas는 HHDFS76200200으로 변경돼 정상 케이스는 open 받음.
                # 그래도 실패 시(휴장일·통신 오류 등) prev_close * (1+slippage)로
                # 보수적 매수 시도. catch-up을 silent skip하지 않고 사용자 매수
                # 의지 존중 — 최악의 경우에도 어제 종가에서 slippage 비용만큼
                # 비싸게 사는 보수적 가격.
                open_price = ref_price  # = prev_close * (1 + buy_tolerance_pct%)
                log.info("[catch-up] %s 시가 미제공 — prev_close 기반 발주 "
                          "(open=%.4f)", symbol, open_price)
            slip = qc.DEFAULT_EXECUTION["bt_slippage_bps"] / 10_000.0
            limit = _round_limit(open_price * (1 + slip), "up", symbol)
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
            limit = _round_limit(
                ref_price * (1 + policy["buy_tolerance_pct"] / 100.0),
                "up", symbol)
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
        # L-01: 매도 멱등 단일 게이트(모든 매도 경로 공유) — 오늘 같은 (sid, symbol)
        # 매도 intent가 활성이면 재발주 차단. EOD cycle·장중 tick 손절·catch-up이
        # 첫 매도 미체결(KIS 잔고 미감소)인 동안 같은 포지션을 동시 평가해도 이중매도를
        # 막는다. intent journal이 cycle/장중/catch-up·재기동을 가로지르는 단일 출처.
        today_iso = kst_today().isoformat()
        if intents.is_active(today_iso, sid, symbol, "sell"):
            log.info("[L-01] 중복 매도 차단 %s/%s", sid, symbol)
            return
        intent_id = intents.new_intent_id()
        intents.begin(today_iso, intent_id, sid, strat_name, symbol, "sell",
                      qty, ref_price)
        use_limit = bool(policy["use_limit"])
        # Phase 38.9 — 매도 tolerance 단일화. 신호·청산 모두 같은 값.
        tol = policy["sell_tolerance_pct"]
        # 미국 예약매도 — MOO(장개시시장가)로 개시가 체결. 개장 전 접수.
        is_resv = self._is_reserved_us(symbol)
        if is_resv:
            limit = 0
            try:
                r = self.broker.sell_resv_moo(symbol, qty)
            except Exception as e:
                intents.mark_failed(today_iso, intent_id, f"sell_resv_moo: {e}")
                log.error("미국 예약매도 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"예약발주 예외: {e}"))
                return
            log.info("[us-resv] %s 예약매도 MOO", symbol)
        elif use_limit:
            limit = _round_limit(ref_price * (1 - tol / 100.0), "down", symbol)
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

    def _submit_close_short(self, sid: str, strat_name: str, symbol: str, qty: int,
                            ref_price: float, policy: dict, reason: str,
                            decisions: list[dict]) -> None:
        """숏 포지션 환매(buy-to-close) — 청산이므로 매수 주문이나 의미는 청산.

        _submit_sell의 매수판. 선물 전용(숏은 선물만)이라 예약주문 분기 없음(선물은 즉시주문).
        멱등 게이트는 'buy' intent. _after_submit side='buy' → _apply_fill(M4)이 숏 환매로
        해석해 ledger 숏을 차감·정산손익 기록. tolerance는 매수(위로 허용).
        """
        today_iso = kst_today().isoformat()
        if intents.is_active(today_iso, sid, symbol, "buy"):
            log.info("[L-01] 중복 환매 차단 %s/%s", sid, symbol)
            return
        intent_id = intents.new_intent_id()
        intents.begin(today_iso, intent_id, sid, strat_name, symbol, "buy", qty, ref_price)
        if bool(policy["use_limit"]):
            limit = _round_limit(ref_price * (1 + policy["buy_tolerance_pct"] / 100.0),
                                 "up", symbol)
            limit = qc.apply_daily_price_limit(limit, ref_price, "buy", _currency_of(symbol))
            try:
                r = self.broker.buy_limit(symbol, qty, limit)
            except Exception as e:
                intents.mark_failed(today_iso, intent_id, f"buy_limit(close): {e}")
                log.error("숏 환매 지정가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"환매 발주 예외: {e}"))
                return
        else:
            limit = 0
            try:
                r = self.broker.buy(symbol, qty)
            except Exception as e:
                intents.mark_failed(today_iso, intent_id, f"buy(close): {e}")
                log.error("숏 환매 시장가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"환매 발주 예외: {e}"))
                return
        intents.mark_submitted(today_iso, intent_id, r.get("order_no", "") or "")
        self._after_submit(r, sid, strat_name, None, symbol, "buy", qty,
                            ref_price, limit, policy, decisions, reason=reason)

    def _submit_open_short(self, sid: str, strat_name: str, strat_def: dict,
                           symbol: str, qty: int, ref_price: float, policy: dict,
                           decisions: list[dict]) -> None:
        """숏 진입(sell-to-open) — 매도 주문이나 의미는 신규 숏 포지션 개시. 선물 전용.

        _submit_buy의 매도판. strat_def를 _after_submit에 전달해 ledger에 definition 저장(나중
        _submit_close_short가 그 청산규칙으로 환매). 멱등 'sell' intent. _after_submit side='sell'
        → _apply_fill(M4)이 보유없는 선물 매도를 숏진입으로 해석. 선물 전용→예약 분기 없음.
        """
        today_iso = kst_today().isoformat()
        intent_id = intents.new_intent_id()
        intents.begin(today_iso, intent_id, sid, strat_name, symbol, "sell", qty, ref_price)
        if bool(policy["use_limit"]):
            limit = _round_limit(ref_price * (1 - policy["sell_tolerance_pct"] / 100.0),
                                 "down", symbol)
            limit = qc.apply_daily_price_limit(limit, ref_price, "sell", _currency_of(symbol))
            try:
                r = self.broker.sell_limit(symbol, qty, limit)
            except Exception as e:
                intents.mark_failed(today_iso, intent_id, f"sell_limit(open): {e}")
                log.error("숏 진입 지정가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"숏진입 발주 예외: {e}"))
                return
        else:
            limit = 0
            try:
                r = self.broker.sell(symbol, qty)
            except Exception as e:
                intents.mark_failed(today_iso, intent_id, f"sell(open): {e}")
                log.error("숏 진입 시장가 발주 실패 [%s]: %s", symbol, e)
                decisions.append(order_log.decision(
                    "error", sid, strat_name, symbol, f"숏진입 발주 예외: {e}"))
                return
        intents.mark_submitted(today_iso, intent_id, r.get("order_no", "") or "")
        self._after_submit(r, sid, strat_name, strat_def, symbol, "sell", qty,
                            ref_price, limit, policy, decisions, reason="숏진입")

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
        # 그렇지 않으면 pending에 등록 → 다음 사이클 또는 _wait_pending이 폴링.
        # M3: pending 등록도 cycle·WS 체결 thread와 같은 락으로 직렬화.
        with _CYCLE_LOCK:
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
                              symbol: str,
                              dataset: dict, equity_now: float,
                              decisions: list[dict],
                              catchup: bool = False,
                              cand_direction: str | None = None) -> bool:
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

        # M5c/M5d — 진입 방향. long(기본)=매수진입·short=매도진입(sell-to-open, 선물만).
        # long_short(부호방향 directional)는 종목별 당일 방향을 preview 후보(_select 부호)가
        # 가져온다 → cand_direction으로 체결(엔진 _direction_for 거울). 방향 정보 없는
        # long_short 후보(구 preview·횡단 랭킹)는 무음 롱전환 방지 위해 명시 skip.
        direction = (strat_def.get("position") or {}).get("direction", "long")
        if direction == "long_short":
            if cand_direction not in ("long", "short"):
                decisions.append(order_log.decision(
                    "skip_unsupported", strategy_id, strat_name, symbol,
                    "long_short 후보에 방향 정보 없음 — 부호방향 directional 전략만 라이브 가능"))
                return False
            direction = cand_direction
        is_short = direction == "short"

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
                bal = self.broker.account_snapshot(overseas=False)["balance"]
                if qc.is_futures(symbol):
                    # 선물 주문은 선물계좌 가용증거금현금으로 사이징(주식계좌 현금이 아님).
                    # 미배선/구브로커(키 없음)면 주식 cash로 graceful fallback.
                    cash = float(bal.get("futures_order_cash") or bal.get("cash") or 0)
                else:
                    cash = float(bal["cash"])
                capital = equity_now if equity_now > 0 else cash
        except Exception as e:
            log.error("가용자금 조회 실패 [%s]: %s", symbol, e)
            decisions.append(order_log.decision(
                "error", strategy_id, strat_name, symbol,
                f"가용자금 조회 실패: {e}"))
            return False

        # 사이징 — 전일 종가 기준 (cash·prev_close 모두 종목 통화).
        # IR(전략 연구소)은 position.sizing(이벤트 진입 예산)으로 사이징한다.
        # ir_live.event_buy_qty가 백테스트 엔진 _budget과 동일(amount_krw 또는
        # cash×amount_pct%, 단일 유니버스=100%) + max_position_pct 캡까지 처리한다.
        from quant_core.ir_engine import StrategyIR
        from quant_core.ir_engine import live as ir_live
        qty = ir_live.event_buy_qty(StrategyIR.model_validate(strat_def),
                                    cash=cash, prev_close=prev_close, capital=capital)

        # 가용 현금 한도 — 주식만. 선물은 event_buy_qty가 이미 증거금으로 클램프했고,
        # cash//prev_close(현금÷지수가)는 선물 계약수에 무의미(과대 → 비바인딩)하므로 제외.
        if not qc.is_futures(symbol):
            qty = min(qty, int(cash // prev_close))

        # 미국: 주문가능수량(통합증거금 상한) 초과 방지
        if max_cap is not None:
            qty = min(qty, max_cap)

        if qty <= 0:
            decisions.append(order_log.decision(
                "skip_funds", strategy_id, strat_name, symbol,
                f"수량 부족 (현금 {cash:,.0f} / 전일종가 {prev_close:,.0f})"))
            return False

        # L-01 멱등 게이트 — 오늘 같은 (sid, symbol, 진입방향)로 이미 발주됐다면 skip.
        # 진입측 = short면 'sell'(sell-to-open)·long이면 'buy'. 크래시 재기동 + reconcile이
        # submitted/ambiguous로 마감했으면 차단.
        entry_side = "sell" if is_short else "buy"
        today_iso = kst_today().isoformat()
        if intents.is_active(today_iso, ledger_key, symbol, entry_side):
            decisions.append(order_log.decision(
                "skip_idempotent", strategy_id, strat_name, symbol,
                "오늘 이미 발주된 intent 존재 — 중복 차단"))
            log.info("[L-01] 중복 진입 차단 %s/%s (%s)", ledger_key, symbol, entry_side)
            return False

        # 발주가는 submit 내부에서 prev_close × (1 ± tolerance%) 계산.
        if is_short:
            # 숏 진입(sell-to-open) — 선물 전용. 예약·catchup 없음(선물은 즉시주문).
            self._submit_open_short(ledger_key, strat_name, strat_def, symbol, qty,
                                    prev_close, policy, decisions)
        else:
            # 롱 진입(buy). catchup=True면 시장가 매수만 시초가 limit으로 변환(지정가는 그대로).
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
            # IR(전략 연구소)은 universe.kind로 다중키 여부를 결정한다. 후보(cands)는
            # 서버 preview가 이미 선정했다.
            uni_kind = (strat_def.get("universe") or {}).get("kind", "single")
            is_multi_key = uni_kind in ("list", "all")
            if is_multi_key:
                prefix = f"{sid}:"
                held_keys = {k for k in self.ledger if k.startswith(prefix)}
                slots_left = max(0, len(cands) - len(held_keys))
                if slots_left <= 0:
                    decisions.append(order_log.decision(
                        "skip_held", sid, strat_name, "",
                        f"IR 보유 한도 충족 ({len(held_keys)}종목)"))
                    continue
            else:
                if sid in self.ledger or sid in sold_this_cycle:
                    decisions.append(order_log.decision(
                        "skip_held", sid, strat_name, "", "이미 보유 또는 당일 청산"))
                    continue
                slots_left = 1

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
                        ledger_key, sid, strat_name, strat_def,
                        symbol, dataset, equity_now, decisions,
                        catchup=catchup, cand_direction=c.get("direction")):
                    bought += 1
                    n_preview_used += 1

        log.info("preview 경로 진입 완료 — %d종목 발주 (신호 재평가 skip)",
                  n_preview_used)

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

    def liquidate_all_held(self) -> dict:
        """비상 전량 청산 — 일반 cycle과 독립. dataset·preview·전략파싱·시장스코프 무의존.

        근본 원칙: 비상정지는 안전 기능이므로 최소 의존·격리돼야 한다. 보유분을 파는
        데엔 신호·dataset이 필요 없으므로, 청산 대상을 **브로커 계좌 실보유**
        (account_snapshot)에서 직접 취득한다 — ledger/전략 파싱/dataset 다운로드와 무관.
        전략이 삭제된 고아·외부 매수분도 보유면 청산된다(run_cycle 재사용 시 dataset
        다운로드 hang·파싱 실패·단일 시장 스코프에 막히던 구조적 결함을 닫음).

        시세는 KIS 현재가(_safe_price), 못 구하면 시장가 주문(가격 의존 제거). KR·US·선물
        보유 전체를 시장 무관하게 즉시 매도(롱)·환매(숏). 열린 시장은 체결, 닫힌 시장은
        broker가 거부를 반환(decisions에 rejected로 명확 표면화). 매도 경로는 정규
        cycle과 동일한 _submit_sell/_submit_close_short(멱등 게이트·pending 기록 공유).

        호출자(gui LIQUIDATE_ALL)가 killswitch.activate를 먼저 수행하고 이 결과를
        push_snapshot한다.
        """
        decisions: list[dict] = []
        today = kst_today()
        # 직전 미체결 상태 먼저 정리(이중매도 방지 게이트 정합).
        self._resolve_pending(decisions)
        try:
            snap = self.broker.account_snapshot()
        except Exception as e:
            log.error("[비상청산] KIS 잔고 조회 실패 — 청산 보류(다음 시도 가능): %s", e)
            decisions.append(order_log.decision(
                "skip_kis_health", "", "", "", f"KIS 잔고 조회 실패: {e}"))
            return self._state_payload(decisions, today, kind="emergency_liquidation")

        global_policy = merged_execution(None)
        market_policy = {**global_policy, "use_limit": False}   # 시세 없으면 시장가
        positions = snap.get("positions") or []
        log.info("[비상청산] 보유 %d종 전량 청산 시작 (dataset 무의존)", len(positions))
        for pos in positions:
            symbol = str(pos.get("symbol") or "")
            qty = int(pos.get("qty") or 0)
            # KIS 국내선물 잔고는 side를 'buy'/'sell'로 줘서 raw 비교('short')는
            # 'sell' 숏을 롱으로 오인 → 청산(환매) 대신 추가 매도 = 숏 2배 확대
            # (리뷰 D5-3). norm_side가 표기 분열을 닫는 단일 출처.
            from .analytics import norm_side
            side = norm_side(pos.get("side"))
            if not symbol or qty <= 0:
                continue
            ref_price = self._safe_price(symbol) or 0.0
            policy = global_policy if ref_price > 0 else market_policy
            # 비상 청산은 보유 sid 무관 — 종목 단위 멱등 키.
            sid = f"liquidate:{symbol}"
            if side == "short":
                self._submit_close_short(sid, "비상청산", symbol, qty,
                                         ref_price, policy, "kill-switch", decisions)
            else:
                self._submit_sell(sid, "비상청산", symbol, qty,
                                  ref_price, policy, "kill-switch", decisions)
        # 즉시 체결/거부 반영(시장가 체결·장마감 거부를 결과에 표면화).
        self._resolve_pending(decisions)
        return self._state_payload(decisions, today, kind="emergency_liquidation")

    def liquidate_day_trades(self, dataset: dict, instrument_class: str, *,
                             market: str = "KRX") -> dict:
        """당일매매(hold_days==0) 종가 청산 사이클 — 일반 cycle과 독립·additive (Stage B).

        당일매매는 백테스트에서 진입 바 종가에 청산한다. 라이브는 사이클이 아침·종가로
        나뉘므로 종가 단일가 발주창(주식 15:20~15:30·선물 15:35~15:45)에 이 사이클을 돌려
        당일매매 포지션만 종가 기준 청산 → backtest=live를 맞춘다(아침 cycle은 is_close=False라
        당일매매를 건드리지 않는다 — cycle_exit_reason Stage B 분기).

        instrument_class ∈ {"stock","futures"} — 주식/선물 종가 발주창이 다르므로(스케줄러가
        분리 cron으로 호출) 종목 클래스로 라우팅한다. 유저 execution.use_limit를 존중(_policy)
        — 시장가/지정가. 청산 수량은 main loop와 동일하게 KIS 실보유로 클램프(L-04, snap_pre).
        파싱 실패 고아는 hold_days 불명 → skip(main loop·Monitor가 표면화).
        """
        decisions: list[dict] = []
        today = kst_today()
        snap_pre = self.broker.account_snapshot()   # KIS 실보유(clamp 기준) — main loop와 동일
        for sid, pos in list(self.ledger.items()):
            if _market_group_safe(pos["symbol"]) != market:
                continue
            # instrument_class 라우팅: 선물 vs 주식
            is_fut = qc.is_futures(pos["symbol"])
            if instrument_class == "futures" and not is_fut:
                continue
            if instrument_class == "stock" and is_fut:
                continue
            # 당일매매(hold_days==0)만 — 정의에서 직접 읽음(파싱 실패 고아는 hold_days 불명 → skip)
            hold_days = (((pos.get("definition") or {}).get("position") or {})
                         .get("exit") or {}).get("hold_days")
            if hold_days != 0:
                continue
            held = (today - date.fromisoformat(pos["entry_date"])).days
            try:
                reason, _ = _exit_reason_for(
                    pos["definition"], held, dataset, pos["symbol"], is_close=True)
            except Exception as e:
                # 파싱 실패 → skip(고아는 main loop·monitor가 처리)
                log.warning("종가청산 정의 파싱 실패 [%s]: %s", sid, e)
                continue
            if not reason:
                # 방어적 — is_close=True면 "당일청산"이 떠야 정상. None이면 청산 보류.
                continue
            # ref_price = 현재가(종가 무렵). 종가 매도는 전일종가가 아니라 현재가 기준.
            ref_price = self._safe_price(pos["symbol"]) or 0.0
            if ref_price <= 0:
                sdf = dataset.get(pos["symbol"])
                if sdf is not None and len(sdf) and "Close" in sdf.columns:
                    ref_price = float(sdf["Close"].iloc[-1])
            if ref_price <= 0:
                log.warning("종가청산 ref_price 없음 [%s] — skip", pos["symbol"])
                continue
            policy = _policy(pos.get("definition"))   # 유저 execution.use_limit 존중 — 시장가/지정가
            sell_qty = int(pos["qty"])
            # L-04: 발주 직전 KIS 실보유로 클램프 — 외부 수동매도 over-sell 방지(main loop와 동일).
            pos_side = pos.get("side", "long")
            held_now = held_qty_from_snapshot(snap_pre, pos["symbol"], pos_side)
            clamped = clamp_sell_qty(held_now, sell_qty)
            if not clamped:                           # None=잔고 미상, 0=외부 매도(보유 0) → skip
                log.info("[종가청산] %s KIS 실보유 0/미상 — 발주 skip", pos["symbol"])
                continue
            if pos_side == "short":
                self._submit_close_short(sid, pos.get("strategy_name", ""), pos["symbol"],
                                         clamped, ref_price, policy, reason, decisions)
            else:
                self._submit_sell(sid, pos.get("strategy_name", ""), pos["symbol"],
                                  clamped, ref_price, policy, reason, decisions)
        # 즉시 체결/거부 반영(단일가 체결·발주창 외 거부를 결과에 표면화).
        self._resolve_pending(decisions)
        return self._state_payload(decisions, today, kind="day_trade_close")

    def state_snapshot(self) -> dict:
        """현 상태(잔고·포지션·kill_switch) 스냅샷 — 거래 없이 상태 변경(kill-switch 해제·
        일시정지·재개·주문취소 등)을 웹에 즉시 반영하기 위한 push용. decisions 없음·cycle
        로그 미기록(타임라인 오염 방지). auto_status는 push_snapshot이 일괄 주입한다.
        """
        return self._state_payload([], kst_today(), kind="state_sync",
                                   record_cycle=False)

    def _state_payload(self, decisions: list[dict], today: date, *,
                       kind: str = "emergency_liquidation",
                       record_cycle: bool = True) -> dict:
        """현재 잔고·포지션·결정·kill_switch를 Monitor용 스냅샷 payload로 — 정규 cycle 출력
        (_cycle_body 꼬리)은 건드리지 않는다(주식 골든 byte-identical 보존, blast radius 0).
        비상청산(kind=emergency_liquidation)·상태동기화(kind=state_sync) 공용 빌더.
        """
        try:
            snap = self.broker.account_snapshot()
            balance = snap.get("balance", {}) or {}
            positions = snap.get("positions", []) or []
        except Exception as e:
            log.warning("[%s] 스냅샷 조회 실패: %s", kind, e)
            balance, positions = {}, []
        self._save()
        try:
            broker_pending = self.broker.pending_orders()
        except Exception:
            broker_pending = []
        cycle_summary = {
            "today": today.isoformat(),
            "market": "ALL",
            "kind": kind,
            "n_sold": sum(1 for d in decisions if d["action"] == "sold"),
            "n_rejected": sum(1 for d in decisions if d["action"] == "rejected"),
            "n_unfilled": sum(1 for d in decisions if d["action"] == "unfilled"),
            "n_errors": sum(1 for d in decisions if d["action"] == "error"),
            "kill_switch": bool(killswitch.load().get("active")),
        }
        if record_cycle:
            order_log.log_cycle(decisions, cycle_summary)
        positions_rich = analytics.enrich_positions(
            positions, self.ledger, today.isoformat())
        return {
            "balance": balance,
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
            "drawdown": analytics.drawdown_state(),
            "health": analytics.local_health(),
        }

    def cycle(self, strategies: list[dict], dataset: dict,
              today: date | None = None,
              buy_candidates: list[dict] | None = None,
              risk_limits: dict | None = None,
              market: str = "KRX",
              krx_status: dict[str, dict] | None = None,
              catchup: bool = False,
              reserved: bool = False,
              cycle_id: str = "") -> dict:
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
                                       krx_status, catchup=catchup,
                                       reserved=reserved, cycle_id=cycle_id)

    def _cycle_locked(self, strategies, dataset, today, buy_candidates,
                       risk_limits, market, krx_status,
                       catchup: bool = False, reserved: bool = False,
                       cycle_id: str = "") -> dict:
        # Q5(데드락 방지): _in_cycle 플래그를 try/finally로 보장 — 예외 발생 시에도
        # 반드시 reset되어야 다음 cycle에서 _apply_fill의 평가가 정상 동작.
        self._in_cycle = True
        # Phase 48 — 종목 상태 dict는 인스턴스에 저장해 _try_buy_one_symbol에서 사용.
        # cycle 단위 stale 안전 (dict는 cycle 시작 시 fresh, 다음 cycle에서 다시 받음).
        self._krx_status: dict[str, dict] = krx_status or {}
        # 미국 예약주문 라우팅 — 개장 전 entry cycle에서만 True. 장중 손절/킬스위치
        # 청산 re-entry(market="US"여도 reserved=False)는 즉시 시장 주문이어야 하므로
        # market 추론이 아닌 명시 파라미터로 받는다. try/finally로 반드시 reset.
        self._reserved_us = bool(reserved)
        try:
            return self._cycle_body(strategies, dataset, today,
                                     buy_candidates, risk_limits, market,
                                     catchup=catchup, cycle_id=cycle_id)
        finally:
            self._in_cycle = False
            self._reserved_us = False

    def _cycle_body(self, strategies, dataset, today, buy_candidates,
                     risk_limits, market, catchup: bool = False,
                     cycle_id: str = "") -> dict:
        today = today or kst_today()
        decisions: list[dict] = []

        # 옛 Phase 48 P1-B의 시간 기반 KIS 점검 가드(평일 03:00~06:00 등)는 제거.
        # 이유: KIS 공식 점검 시간 doc 부재로 보수적 추정 차단했으나, 실측 probe
        # (~/.quant-platform/probes/kis_maintenance.jsonl) 결과 03:28+ 정상 응답
        # 다수 → 가드 over-conservative. 미장 마감(KST 05:00) 직전 catch-up 매수
        # 차단되는 실 사고 (2026-05-28). KIS 진짜 점검 중이면 아래 잔고 health
        # check가 즉시 cycle 중단 (skip_kis_health). 4원칙 PR-1 — 추정 fallback
        # 보다 실측 fallback이 본질적 해결.

        # ── 0. 이전 사이클 미체결 정리 ─────────────────────────────────────
        log.info("[cycle-body] 미체결 정리 시작 (pending=%d건)", len(self.pending))
        _t0 = time.monotonic()
        self._resolve_pending(decisions)
        log.info("[cycle-body] 미체결 정리 완료 %.1fs (잔여=%d건) — 잔고 조회 시작",
                  time.monotonic() - _t0, len(self.pending))

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
                    "cycle_summary": {"skipped_reason": "kis_health_fail",
                                       "cycle_id": cycle_id}}

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
        sold_this_cycle: set[str] = set()
        for sid, pos in list(self.ledger.items()):
            # 시장 배칭 — 이번 사이클 시장의 보유분만 청산 (미국 보유분은
            # 미국 정규장 사이클에서만 매도, 그 반대도 동일).
            if _market_group_safe(pos["symbol"]) != market:
                continue
            held = (today - date.fromisoformat(pos["entry_date"])).days
            parse_failed = False
            try:
                reason, _ = _exit_reason_for(
                    pos["definition"], held, dataset, pos["symbol"])
            except Exception as e:
                # 정의 파싱 실패(고아) — 청산 규칙을 평가할 수 없다. kill switch 발동
                # 중엔 "모든 보유 강제 청산" 의도를 지켜야 하므로 아래에서 강제 사유를
                # 부여하고, 그 외엔 자동매도하지 않고 청산 불가 고아로 표면화한다.
                log.warning("원장 전략 파싱 실패 [%s]: %s", sid, e)
                reason = None
                parse_failed = True
            # kill switch 활성 시 모든 보유 강제 청산(파싱 실패 고아 포함).
            if ks_active and not reason:
                reason = "kill-switch"

            # M6 tier-2 만기 백스톱: 유저 청산규칙 미발동 + 선물 만기 임박 → 강제청산.
            # ledger 기록 만기 기반이라 정의 파싱 실패(고아)여도 평가된다 — 물리인도/현금정산
            # 으로 포지션이 사라지기 전에 닫는 안전망(고아도 만기 임박이면 닫아야 안전).
            if not reason and qc.is_futures(pos["symbol"]):
                reason = self._expiry_close_reason(pos, today)

            if not reason:
                # 파싱 실패 고아(구 스키마 등)는 청산 규칙 평가 불가 → 자동 청산이 안 된다
                # (kill-switch 외 탈출구 없음). 임의 매도는 하지 않되, 사용자가 웹에서
                # 인지·수동 정리하도록 명시 표면화한다(Monitor 경고로 노출).
                if parse_failed:
                    decisions.append(order_log.decision(
                        "unparseable_orphan", sid, pos.get("strategy_name", ""),
                        pos["symbol"], "전략 정의 파싱 실패 — 자동 청산 불가(수동 정리 필요)"))
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
            # L-01 매도 멱등은 _submit_sell 진입부 단일 게이트가 담당(전 매도 경로 공유).
            # IR(전략 연구소)은 per-rule 매도 비중이 없으므로 전량(100%) 청산.
            sell_qty = int(pos["qty"])
            # L-04(EOD): 발주 직전 KIS 실 보유로 클램프 — 외부 수동매도 시 over-sell
            # 방지(intraday 손절과 동일 안전망, 같은 헬퍼). snap_pre는 cycle 진입부
            # 잔고 재사용이라 추가 KIS 호출 없음. ledger drift는 settlement reconcile이 정리.
            pos_side = pos.get("side", "long")
            held = held_qty_from_snapshot(snap_pre, pos["symbol"], pos_side)
            clamped = clamp_sell_qty(held, sell_qty)   # snap_pre 기반이라 None 아님
            if not clamped:                            # 0 = 외부 매도(보유 0)
                log.info("[L-04 EOD] %s KIS 실 보유 0 (외부 매도 추정) — 청산 발주 skip",
                          pos["symbol"])
                decisions.append(order_log.decision(
                    "skip_oversell", sid, pos.get("strategy_name", ""), pos["symbol"],
                    "KIS 실 보유 0 — 외부 매도 추정, 청산 발주 skip"))
                continue
            if clamped < sell_qty:
                log.info("[L-04 EOD] %s 청산 수량 클램프 ledger=%d → broker=%d",
                          pos["symbol"], sell_qty, clamped)
            sell_qty = clamped
            # M5b: 청산 방향은 포지션 side대로 — 롱=매도청산(_submit_sell), 숏=환매(buy-to-close).
            if pos_side == "short":
                self._submit_close_short(sid, pos.get("strategy_name", ""), pos["symbol"],
                                         sell_qty, ref_price, policy, reason, decisions)
            else:
                self._submit_sell(sid, pos.get("strategy_name", ""), pos["symbol"],
                                  sell_qty, ref_price, policy, reason, decisions)
            # sold_this_cycle은 sid 단위 — 같은 cycle 중복 청산 차단.
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
            "cycle_id": cycle_id,                    # 시작 저널(cycle_started)과 join
            "n_strategies": len(strategies),
            "n_bought": sum(1 for d in decisions if d["action"] == "bought"),
            "n_sold": sum(1 for d in decisions if d["action"] == "sold"),
            "n_skip_held": sum(1 for d in decisions if d["action"] == "skip_held"),
            "n_rejected": sum(1 for d in decisions if d["action"] == "rejected"),
            "n_unfilled": sum(1 for d in decisions if d["action"] == "unfilled"),
            "n_errors": sum(1 for d in decisions if d["action"] == "error"),
            "n_unparseable_orphan": sum(
                1 for d in decisions if d["action"] == "unparseable_orphan"),
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

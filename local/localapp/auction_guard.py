"""동시호가 가드(#16 · 2026-07-19 유저 확정) — 창내 수동 개입의 실시간 정합.

원칙: 동시호가 미체결은 시장가든 지정가든 "전량 체결" 가정(유저 확정 — 지정가
유찰분은 창 종료 후 기존 수렴이 흡수). 목표(전략 의도)와 어긋나는 창내 개입을
장이 닫히기 전에 **주문 수정**으로 정합한다 — 반대 주문을 새로 내지 않으므로
같은 계좌 매수·매도가 단일가에 맞붙는 자전(가장매매) 소지가 원천적으로 없다.

  ① 자동 주문 유저 변경(취소·감량) → 잔여 전량 취소 + intent failed 마킹 후
     해당 창 사이클 재실행(멱등 재계획이 정확 수량을 재발주). 정정으로 수량
     증가가 불가한 브로커 제약상 "깨끗이 지우고 재발주"가 유일한 복원 경로.
  ② 외부(수동) 미체결이 목표를 초과 → **최신 주문부터 전량 취소**(유저 확정).
     부분 정정(감량)은 브로커 실측 전이라 미사용 — 다음 주문을 통째로 취소하면
     초과분을 넘는 경우 그 직전에서 멈춘다(과잉 개입 금지 — 잔여는 개장+1분
     수렴/익일 수렴 몫).

스코프 = KRX 동시호가 4창(선물 개장·주식 개장·주식 마감·선물 마감). 창 중엔
단일가 체결 전이라 보유·원장이 변하지 않아 창 시작 스냅샷으로 고정 가능하고,
"자동 주문이 브로커 미체결에 없음 = 유저 취소"가 성립한다(체결 소멸 불가).

검산식(틱마다·심볼별):  D = 예상 최종 − 목표 최종
    = [보유 + own잔여 + 외부잔여] − [원장 + 이번 창 own 의도]
넷팅 book leg(원장 즉시 기장)과 A1 선반영이 양변에 대칭으로 들어 있어, A1이
이미 인수한 외부 미체결은 D=0으로 자연 보호된다(과잉 트리밍 없음 — 테스트 고정).

관측: 모든 개입은 order_log 결정 + cycles.jsonl(kind=auction_guard)로 기록되고,
재실행이 발생하면 그 사이클의 스냅샷 push가 서버 타임라인에 반영된다. GUI 전용
배너는 미배선(후속) — 로그·타임라인이 1차 관측면.
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

import quant_core as qc

from . import intents, order_log

log = logging.getLogger("localapp.auction_guard")
KST = ZoneInfo("Asia/Seoul")

# 유저가 가드 복원과 반복 충돌(재취소)할 때의 창내 심볼당 재발주 상한 — 무한
# 맞대응 방지. 초과 시 관측만 남기고 물러난다(잔여는 수렴 몫).
_MAX_RESTORES_PER_SYMBOL = 2


@dataclass(frozen=True)
class GuardPlan:
    """한 틱의 조치 계획 — plan_guard_actions(순수)의 산출물."""
    cancels: list = field(default_factory=list)        # [(order_no, symbol, remain_qty)]
    fail_intents: list = field(default_factory=list)   # [(intent_id, reason)]
    restored_symbols: list = field(default_factory=list)  # own 복원 대상 심볼(카운트용)
    rerun: bool = False
    decisions: list = field(default_factory=list)


def _signed(side, qty) -> int:
    return int(qty or 0) * (1 if str(side) == "buy" else -1)


def plan_guard_actions(positions_signed: dict, ledger_signed: dict,
                       own_intents: list, own_pendings: list,
                       ext_pendings: list,
                       restores_done: dict | None = None) -> GuardPlan:
    """한 틱의 조치 계획(순수 — IO 없음, 단위 테스트 대상).

    own_intents: 이번 창의 활성 자동 의도 [{intent_id, symbol, side, qty, order_no}]
    own_pendings / ext_pendings: 브로커 미체결 [{order_no, symbol, side, remain_qty}]
    restores_done: 창 누적 심볼별 own 복원 횟수(상한 가드).
    """
    restores_done = restores_done or {}
    cancels: list = []
    fails: list = []
    restored: list = []
    decisions: list = []
    rerun = False
    syms = ({i["symbol"] for i in own_intents}
            | {p["symbol"] for p in own_pendings}
            | {p["symbol"] for p in ext_pendings}
            | set(positions_signed) | set(ledger_signed))
    for sym in sorted(syms):
        oi = [i for i in own_intents if i["symbol"] == sym]
        op = [p for p in own_pendings if p["symbol"] == sym]
        oi_signed = sum(_signed(i["side"], i["qty"]) for i in oi)
        op_signed = sum(_signed(p["side"], p["remain_qty"]) for p in op)

        # ① 자동 주문 유저 변경 감지 — 의도(저널)와 브로커 잔여가 어긋남.
        if op_signed != oi_signed:
            if restores_done.get(sym, 0) >= _MAX_RESTORES_PER_SYMBOL:
                decisions.append(order_log.decision(
                    "guard_own_giveup", "", "", sym,
                    f"자동 주문 복원 {_MAX_RESTORES_PER_SYMBOL}회 도달 — 창내 중단"
                    "(유저 의사 존중·잔여는 수렴 몫)"))
                continue
            for p in op:
                cancels.append((str(p["order_no"]), sym, int(p["remain_qty"] or 0)))
            for i in oi:
                fails.append((i["intent_id"],
                              "auction_guard: 자동 주문 유저 변경 감지 — 재발주"))
            decisions.append(order_log.decision(
                "guard_own_restore", "", "", sym,
                f"자동 주문 변경 감지(의도 {oi_signed:+d} vs 잔여 {op_signed:+d})"
                " — 잔여 취소 후 창내 재발주"))
            restored.append(sym)
            rerun = True
            continue        # 복원 틱엔 외부 트리밍 보류 — 재발주로 목표 재정립 후 다음 틱

        # ② 외부(수동) 미체결 초과 — 최신 주문부터 전량 취소.
        exts = sorted((p for p in ext_pendings if p["symbol"] == sym),
                      key=lambda p: str(p.get("order_no", "")), reverse=True)
        ext_signed = sum(_signed(p["side"], p["remain_qty"]) for p in exts)
        d = ((positions_signed.get(sym, 0) + op_signed + ext_signed)
             - (ledger_signed.get(sym, 0) + oi_signed))
        if d == 0 or not exts:
            continue
        want = "buy" if d > 0 else "sell"
        remain = abs(d)
        stopped_short = False
        for p in exts:
            if remain == 0:
                break
            if str(p["side"]) != want:
                continue
            q = int(p["remain_qty"] or 0)
            if q > remain:
                stopped_short = True
                break
            cancels.append((str(p["order_no"]), sym, q))
            decisions.append(order_log.decision(
                "guard_ext_trim", "", "", sym,
                f"목표 초과 수동 미체결 취소(최신 우선) — {p['side']} {q}"
                f" (주문 {p['order_no']})"))
            remain -= q
        if remain > 0:
            why = ("다음 수동 주문 통째 취소는 과잉이라 보류(부분정정 실측 전)"
                   if stopped_short else "취소 가능한 같은 방향 수동 주문 소진")
            decisions.append(order_log.decision(
                "guard_ext_residual", "", "", sym,
                f"수동 초과 잔여 {want} {remain} — {why} · 수렴 몫"))
    return GuardPlan(cancels, fails, restored, rerun, decisions)


def run_auction_guard(instrument_class: str, window: str,
                      start_hm: tuple, until_hms: tuple,
                      poll_sec: float = 10.0) -> dict:
    """한 동시호가 창을 감시·정합 — scheduler cron 진입점(창 종료까지 블로킹).

    instrument_class ∈ {"stock","futures"} · window ∈ {"open","close"} — 재실행을
    아침(run_cycle)/종가(run_close_cycle)로 라우팅. start_hm = 이 창의 자동 발주
    시작 시각(그 이후 own intent만 이번 창 소속), until_hms = 감시 종료(마감 단일가
    수십 초 전 — 취소 접수 여유).
    """
    from quant_core import market_calendar as mc

    from .runner import make_broker, run_close_cycle, run_cycle
    from .trader import _CYCLE_LOCK, Trader, _market_group_safe, kst_now, kst_today

    today = kst_today()
    try:
        if not mc.is_session_day("KR", today):
            log.info("KRX 휴장일 — auction guard skip (%s/%s)", window, instrument_class)
            return {"status": "skipped_holiday"}
    except Exception as e:
        # 캘린더 조회 불가(범위 밖 등) — 회차 가드(L-03)와 동일하게 차단하지 않음.
        log.warning("auction guard: 캘린더 판정 불가 — 감시 진행: %s", e)

    try:
        broker = make_broker()
    except Exception as e:
        log.warning("auction guard: broker 준비 실패 — skip: %s", e)
        return {"status": "no_broker"}
    if getattr(broker, "pending_orders", None) is None:
        log.info("auction guard: 미체결 조회 미지원 브로커 — skip")
        return {"status": "unsupported"}
    trader = Trader(broker)

    def _in_scope_sym(sym: str) -> bool:
        return (_market_group_safe(sym) == "KRX"
                and (instrument_class == "futures") == qc.is_futures(sym))

    def _pend_scope(p: dict) -> bool:
        mg = "KRX" if str(p.get("market") or "") in ("DOMESTIC", "KRX") else "US"
        return mg == "KRX" and str(p.get("asset_class") or "") == instrument_class

    # 창 중엔 단일가 체결 전이라 보유가 변하지 않는다 — 시작 스냅샷 고정.
    try:
        snap = broker.account_snapshot()
    except Exception as e:
        log.warning("auction guard: 잔고 스냅샷 실패 — skip: %s", e)
        return {"status": "no_snapshot"}
    if (snap.get("balance") or {}).get("fetch_failed"):
        # §14와 동일 보수 — 부분 스냅샷으론 목표·보유 대조가 오판할 수 있다.
        log.warning("auction guard: 잔고 부분 조회 — skip(수렴 몫)")
        return {"status": "partial_snapshot"}
    positions_signed: dict = {}
    for p in snap.get("positions") or []:
        sym = p.get("symbol") or ""
        if not sym or p.get("symbol_unmapped") or not _in_scope_sym(sym):
            continue
        q = int(p.get("qty") or 0)
        positions_signed[sym] = positions_signed.get(sym, 0) + (
            -q if str(p.get("side")) == "short" else q)

    since_iso = datetime.combine(today, time(*start_hm), tzinfo=KST).isoformat()
    until = datetime.combine(today, time(*until_hms), tzinfo=KST)
    restores: dict = {}
    all_decisions: list = []
    n_cancel = 0

    while kst_now() < until:
        try:
            pend = [p for p in (broker.pending_orders() or []) if _pend_scope(p)]
            own_list = [i for i in intents.submitted_window(today.isoformat(), since_iso)
                        if _in_scope_sym(i["symbol"])]
            own_nos = {i["order_no"] for i in own_list if i["order_no"]}
            own_p = [p for p in pend if str(p.get("order_no")) in own_nos]
            ext_p = [p for p in pend if str(p.get("order_no")) not in own_nos]
            trader.reload_state()
            ledger_signed: dict = {}
            for pos in trader.ledger.values():
                sym = pos["symbol"]
                if not _in_scope_sym(sym):
                    continue
                q = int(pos["qty"])
                ledger_signed[sym] = ledger_signed.get(sym, 0) + (
                    -q if pos.get("side", "long") == "short" else q)

            plan = plan_guard_actions(positions_signed, ledger_signed,
                                      own_list, own_p, ext_p, restores)
            if plan.cancels or plan.fail_intents:
                ok_all = True
                with _CYCLE_LOCK:
                    for order_no, sym, qty in plan.cancels:
                        try:
                            broker.cancel(order_no, sym, qty)
                            n_cancel += 1
                        except Exception as e:
                            # 취소 실패 — intent 마킹·재발주를 보류(옛 주문이 살아있는
                            # 채 재발주하면 이중 발주). 다음 틱이 재평가한다.
                            log.warning("guard 취소 실패 [%s 주문 %s]: %s",
                                        sym, order_no, e)
                            ok_all = False
                            break
                    if ok_all:
                        for iid, reason in plan.fail_intents:
                            intents.mark_failed(today.isoformat(), iid, reason)
                if ok_all:
                    for s in plan.restored_symbols:
                        restores[s] = restores.get(s, 0) + 1
                    all_decisions.extend(plan.decisions)
                    for d in plan.decisions:
                        log.warning("[가드] %s %s — %s", d.get("action"),
                                    d.get("symbol"), d.get("reason"))
                    if plan.rerun:
                        try:
                            if window == "open":
                                run_cycle(market="KRX",
                                          instrument_class=instrument_class,
                                          trigger="auction_guard")
                            else:
                                run_close_cycle("KRX", instrument_class)
                        except Exception as e:
                            log.exception("guard 재실행 실패: %s", e)
        except Exception as e:
            log.exception("auction guard tick 예외: %s", e)
        _time.sleep(poll_sec)

    if all_decisions:
        order_log.log_cycle(all_decisions, {
            "market": "KRX", "kind": "auction_guard", "window": window,
            "instrument_class": instrument_class, "n_cancel": n_cancel,
            "today": today.isoformat()})
    return {"status": "done", "n_cancel": n_cancel,
            "n_decisions": len(all_decisions)}

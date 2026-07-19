# -*- coding: utf-8 -*-
"""동시호가 가드(#16) — plan_guard_actions(순수 계획)·intents.submitted_window·루프 단위.

검산식 D = [보유 + own잔여 + 외부잔여] − [원장 + 이번 창 own **전략** 의도] 를 고정하고
(드리프트 교정 의도는 목표변 제외 — G3), 유저 확정 규칙(전량 체결 가정·최신 주문부터·
전량 취소만·과잉 개입 금지)과 2026-07-19 감사 수정(G1 취소 거절 검사·G2 우주 제한·
G4 주문번호 정규형/side)을 잠근다. 스케줄 배선은 tests/test_timeline_bef.py가 커버.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))

from localapp import intents
from localapp.auction_guard import GuardPlan, _canon, _sort_key, plan_guard_actions

S = "코스피200선물"


def _own(iid, side, qty, order_no, strategy_id="s"):
    return {"intent_id": iid, "strategy_id": strategy_id, "symbol": S,
            "side": side, "qty": qty, "ref_price": 300.0, "order_no": order_no}


def _pend(order_no, side, remain, sym=S):
    return {"order_no": order_no, "symbol": sym, "side": side,
            "remain_qty": remain}


# ── 검산식·A1 보호 ───────────────────────────────────────────────────────────
def test_no_deviation_no_action_a1_incorporated_protected():
    """A1 인수 형상: 원장4(북킹)+의도1 vs 보유0+own1+수동4 → D=0 — 수동 4 보호."""
    plan = plan_guard_actions({S: 0}, {S: 4}, [_own("i1", "buy", 1, "101")],
                              [_pend("101", "buy", 1)], [_pend("050", "buy", 4)])
    assert plan == GuardPlan()


def test_late_external_excess_trims_latest_first():
    """늦은 수동 매수 3(주문 200) → 최신 것만 취소, A1 인수분(050)은 보호."""
    plan = plan_guard_actions({S: 0}, {S: 4}, [_own("i1", "buy", 1, "101")],
                              [_pend("101", "buy", 1)],
                              [_pend("050", "buy", 4), _pend("200", "buy", 3)])
    assert plan.cancels == [("200", S, 3)]
    assert plan.rerun is False and plan.fail_intents == []


def test_stop_before_overshoot_full_cancel_only():
    """초과 5 — 최신(3) 취소 후 다음(4)은 잔여 2를 넘어 통째 취소가 과잉 → 중단·기록."""
    plan = plan_guard_actions({S: 0}, {S: 2}, [], [],
                              [_pend("1", "buy", 4), _pend("2", "buy", 3)])
    assert plan.cancels == [("2", S, 3)]
    assert any(d["action"] == "guard_ext_residual" for d in plan.decisions)


def test_opposite_direction_external_cancelled():
    """롱 유지(원장=보유=5) 중 수동 매도 2 → D=−2 → 매도 미체결 취소."""
    plan = plan_guard_actions({S: 5}, {S: 5}, [], [], [_pend("9", "sell", 2)])
    assert plan.cancels == [("9", S, 2)]


def test_exit_window_math_and_late_manual_sell():
    """청산 창: 원장5·own 매도의도 5(잔여5) → D=0. 수동 매도 3 추가 → 그것만 취소."""
    own = [_own("x1", "sell", 5, "700")]
    ok = plan_guard_actions({S: 5}, {S: 5}, own, [_pend("700", "sell", 5)], [])
    assert ok == GuardPlan()
    plan = plan_guard_actions({S: 5}, {S: 5}, own, [_pend("700", "sell", 5)],
                              [_pend("900", "sell", 3)])
    assert plan.cancels == [("900", S, 3)]


# ── own(자동 주문) 유저 변경 복원 ─────────────────────────────────────────────
def test_own_reduced_restore_cancels_and_reruns_ext_deferred():
    """자동 5를 유저가 2로 감량 — 잔여 취소+intent 해제+재실행. ext는 이번 틱 보류."""
    plan = plan_guard_actions({S: 0}, {S: 0}, [_own("i1", "buy", 5, "101")],
                              [_pend("101", "buy", 2)], [_pend("300", "buy", 9)])
    assert plan.cancels == [("101", S, 2)]
    assert plan.fail_intents == [
        ("i1", "auction_guard: 자동 주문 유저 변경 감지 — 재발주")]
    assert plan.rerun is True and plan.restored_symbols == [S]
    assert not [c for c in plan.cancels if c[0] == "300"]


def test_own_fully_cancelled_restore_without_cancels():
    plan = plan_guard_actions({S: 0}, {S: 0}, [_own("i1", "buy", 5, "101")], [], [])
    assert plan.cancels == [] and plan.rerun is True
    assert plan.fail_intents[0][0] == "i1"


def test_restore_cap_gives_up():
    """심볼당 복원 상한 도달 — 조치 없이 관측만(유저 의사 존중·수렴 몫)."""
    plan = plan_guard_actions({S: 0}, {S: 0}, [_own("i1", "buy", 5, "101")], [], [],
                              restores_done={S: 2})
    assert plan.cancels == [] and plan.fail_intents == [] and plan.rerun is False
    assert any(d["action"] == "guard_own_giveup" for d in plan.decisions)


# ── intents.submitted_window — 창 필터·상태 필터 ─────────────────────────────
def test_submitted_window_filters(tmp_path):
    p = tmp_path / "intents.jsonl"
    d = "2026-06-01"
    rows = [
        # 아침 창(창 시작 이전) — 제외돼야
        {"ts": "2026-06-01T08:35:00+09:00", "date": d, "phase": "submitting",
         "intent_id": "early", "strategy_id": "s", "strategy_name": "n",
         "symbol": S, "side": "buy", "qty": 5, "ref_price": 300.0},
        {"ts": "2026-06-01T08:35:01+09:00", "date": d, "phase": "submitted",
         "intent_id": "early", "order_no": "E1"},
        # 이번 창 — submitted(포함)
        {"ts": "2026-06-01T15:40:02+09:00", "date": d, "phase": "submitting",
         "intent_id": "w1", "strategy_id": "s", "strategy_name": "n",
         "symbol": S, "side": "buy", "qty": 3, "ref_price": 300.0},
        {"ts": "2026-06-01T15:40:03+09:00", "date": d, "phase": "submitted",
         "intent_id": "w1", "order_no": "W1"},
        # 이번 창 — failed로 종결(가드 해제됨) → 제외
        {"ts": "2026-06-01T15:40:04+09:00", "date": d, "phase": "submitting",
         "intent_id": "w2", "strategy_id": "s", "strategy_name": "n",
         "symbol": S, "side": "sell", "qty": 2, "ref_price": 300.0},
        {"ts": "2026-06-01T15:40:05+09:00", "date": d, "phase": "submitted",
         "intent_id": "w2", "order_no": "W2"},
        {"ts": "2026-06-01T15:40:06+09:00", "date": d, "phase": "failed",
         "intent_id": "w2", "error": "user cancel"},
        # 이번 창 — submitting(주문번호 미상·ambiguous) → 제외
        {"ts": "2026-06-01T15:40:07+09:00", "date": d, "phase": "submitting",
         "intent_id": "w3", "strategy_id": "s", "strategy_name": "n",
         "symbol": S, "side": "buy", "qty": 9, "ref_price": 300.0},
    ]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                 encoding="utf-8")
    out = intents.submitted_window(d, "2026-06-01T15:39:00+09:00", path=p)
    assert [(o["intent_id"], o["order_no"], o["side"], o["qty"])
            for o in out] == [("w1", "W1", "buy", 3)]
    assert out[0]["strategy_id"] == "s"          # G3 — 드리프트 판별용 노출


# ── 2026-07-19 감사 수정 — G2 우주 제한 · G3 드리프트 제외 · G4 정규형 ─────────
def test_unmanaged_symbol_untouched():
    """G2 — own 의도·원장에 없는 심볼(비전략 보유·수동 전용)은 가드 불간섭.

    종전엔 positions∪pendings 전체가 우주라 원장 0·보유 +14 심볼의 수동 매수를
    D=+14+q로 취소했다 — 수렴 엔진의 '목표 없음=hold'와 모순."""
    kq = "코스닥150선물"
    plan = plan_guard_actions({kq: 14}, {}, [], [],
                              [_pend("77", "buy", 2, sym=kq)])
    assert plan == GuardPlan()


def test_drift_intent_excluded_from_target_side():
    """G3 — 드리프트 교정 의도(원장 불변)는 목표변 제외·좌변 잔여엔 포함.

    원장 −3·보유 −12·드리프트 buy 9: 종전 산식은 D=−9로 시작해 수동 매수 초과
    (+5)를 want=sell로 방치했다. 수정 후 D=0 시작·초과 매수만 정확히 취소."""
    drift = _own("d1", "buy", 9, "500", strategy_id="DRIFT:open")
    ok = plan_guard_actions({S: -12}, {S: -3}, [drift],
                            [_pend("500", "buy", 9)], [])
    assert ok == GuardPlan()
    plan = plan_guard_actions({S: -12}, {S: -3}, [drift],
                              [_pend("500", "buy", 9)], [_pend("900", "buy", 5)])
    assert plan.cancels == [("900", S, 5)] and plan.rerun is False


def test_ambiguous_side_holds_symbol():
    """G4ⓑ — side 판독 불가 미체결(KIS 선물 필드 미검증 "")은 부호 불능 — 보류·관측."""
    plan = plan_guard_actions({S: 0}, {S: 2}, [], [], [_pend("1", "", 3)])
    assert plan.cancels == [] and plan.rerun is False
    assert any(d["action"] == "guard_skip_ambiguous" for d in plan.decisions)


def test_trim_latest_numeric_not_lexicographic():
    """정렬 — 무패딩 번호 자릿수 경계(9993 vs 12345)에서 숫자 기준 최신 우선."""
    plan = plan_guard_actions({S: 0}, {S: 0}, [], [],
                              [_pend("9993", "buy", 1), _pend("12345", "buy", 1)])
    assert [c[0] for c in plan.cancels] == ["12345", "9993"]


def test_canon_and_sort_key():
    """G4ⓐ — KIS zero-pad ODNO ↔ 체결조회 strip 번호가 같은 정규형으로 매칭."""
    assert _canon("0000031808") == _canon(" 31808 ") == "31808"
    assert _sort_key("9993") < _sort_key("12345")


# ── run_auction_guard 루프 — G0 시간대·G1 취소 거절·복원 확정 (1틱 하니스) ────
def _run_one_tick(monkeypatch, broker, submitted, read_today):
    """가드 루프를 정확히 1틱 실행하는 하니스 — 시간·달력·러너·저널 격리.

    이 하니스 자체가 G0 회귀 잠금이다: until이 naive면 kst_now(aware) 비교에서
    TypeError로 한 틱도 못 돈다(v0.9.79 릴리스본의 실결함 — 수정 검증)."""
    import datetime as _dt

    from quant_core import market_calendar as mc

    from localapp import auction_guard as ag
    from localapp import runner as runner_mod
    from localapp import trader as trader_mod

    KST = ag.KST
    t0 = _dt.datetime(2026, 6, 1, 8, 36, tzinfo=KST)
    calls = {"n": 0}

    def fake_now():
        calls["n"] += 1
        return t0 if calls["n"] <= 1 else t0.replace(hour=9)

    class _FakeTrader:
        def __init__(self, b):
            self.ledger = {}

        def reload_state(self):
            pass

    reruns: list = []
    failed: list = []
    cycles: list = []
    monkeypatch.setattr(trader_mod, "kst_now", fake_now)
    monkeypatch.setattr(trader_mod, "kst_today", lambda: t0.date())
    monkeypatch.setattr(trader_mod, "Trader", _FakeTrader)
    monkeypatch.setattr(mc, "is_session_day", lambda m, d: True)
    monkeypatch.setattr(runner_mod, "make_broker", lambda: broker)
    monkeypatch.setattr(runner_mod, "run_cycle",
                        lambda **kw: reruns.append(kw) or {})
    monkeypatch.setattr(runner_mod, "run_close_cycle",
                        lambda *a: reruns.append(a) or {})
    monkeypatch.setattr(ag._time, "sleep", lambda s: None)
    monkeypatch.setattr(intents, "submitted_window",
                        lambda d, since, path=None: list(submitted))
    monkeypatch.setattr(intents, "_read_today", lambda d, path=None: list(read_today))
    monkeypatch.setattr(intents, "mark_failed",
                        lambda d, iid, reason: failed.append(iid))
    monkeypatch.setattr(ag.order_log, "log_cycle",
                        lambda dec, meta: cycles.append((dec, meta)))
    out = ag.run_auction_guard("futures", "open", (8, 34), (8, 44, 30))
    return out, reruns, failed, broker


class _LoopBroker:
    """pending 조회 시퀀스·취소 응답을 주입하는 루프 하니스 브로커."""

    def __init__(self, pend_seq, cancel_resp):
        self._seq = [list(x) for x in pend_seq]
        self._cancel_resp = cancel_resp
        self.cancelled: list = []

    def pending_orders(self):
        return list(self._seq.pop(0)) if len(self._seq) > 1 else list(self._seq[0])

    def cancel(self, order_no, symbol, qty):
        self.cancelled.append((order_no, symbol, qty))
        return self._cancel_resp

    def account_snapshot(self):
        return {"balance": {}, "positions": []}


def _fut_pend(order_no, side, remain):
    return {"order_no": order_no, "symbol": S, "side": side, "remain_qty": remain,
            "market": "DOMESTIC", "asset_class": "futures"}


def test_loop_cancel_rejection_blocks_reorder(monkeypatch):
    """G1 — 취소 거절({"success": False})이면 intent 해제·재실행 금지(이중 발주 차단)."""
    own = [_own("i1", "buy", 5, "101")]
    pend = [_fut_pend("101", "buy", 2)]           # 유저 감량 상태 지속
    broker = _LoopBroker([pend], {"success": False, "message": "거절"})
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker, own, [{"order_no": "101"}])
    assert b.cancelled == [("101", S, 2)]         # 취소는 시도했으나
    assert failed == [] and reruns == []          # 거절 → 해제·재발주 모두 보류
    assert out["n_cancel"] == 0


def test_loop_restore_confirms_then_reorders(monkeypatch):
    """복원 정상 경로 — 재확인 조회(race 봉합) 후 취소 성공·소멸 확인 → 해제+재실행.

    pending 시퀀스: 최초 조회·복원 재확인 조회는 감량 잔여 노출, 취소 후 확인
    조회는 소멸([]) — 그때만 mark_failed·run_cycle이 발화해야 한다."""
    own = [_own("i1", "buy", 5, "0000000101")]    # 저널은 zero-pad(KIS 형)
    pend = [_fut_pend("101", "buy", 2)]           # 조회는 무패딩 — 정규형 매칭(G4ⓐ)
    broker = _LoopBroker([pend, pend, []], {"success": True})
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker, own, [{"order_no": "0000000101"}])
    assert b.cancelled == [("101", S, 2)]
    assert failed == ["i1"]
    assert len(reruns) == 1 and reruns[0]["instrument_class"] == "futures"
    assert out["n_cancel"] == 1

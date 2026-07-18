# -*- coding: utf-8 -*-
"""동시호가 가드(#16) — plan_guard_actions(순수 계획)·intents.submitted_window 단위.

검산식 D = [보유 + own잔여 + 외부잔여] − [원장 + 이번 창 own 의도] 를 고정하고,
유저 확정 규칙(전량 체결 가정·최신 주문부터·전량 취소만·과잉 개입 금지)을 잠근다.
루프/스케줄 배선은 tests/test_timeline_bef.py(잡 레지스트리)가 커버.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))

from localapp import intents
from localapp.auction_guard import GuardPlan, plan_guard_actions

S = "코스피200선물"


def _own(iid, side, qty, order_no):
    return {"intent_id": iid, "symbol": S, "side": side, "qty": qty,
            "ref_price": 300.0, "order_no": order_no}


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

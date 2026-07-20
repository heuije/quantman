# -*- coding: utf-8 -*-
"""동시호가 가드(#16) — plan_guard_actions(순수 계획)·intents.submitted_window·루프 단위.

검산식 D = [보유 + own잔여 + 외부잔여] − [원장 + 이번 창 own **전략** 의도] 를 고정하고
(드리프트 교정 의도는 목표변 제외 — G3), 유저 확정 규칙(전량 체결 가정·최신 주문부터·
초과분만 취소)과 2026-07-19 감사 수정(G1 취소 거절 검사·G2 우주 제한·G4 주문번호
정규형/side)을 잠근다. 부분취소는 실측(2026-07-20 LS CFOAT00300 — 수리·원주문번호
유지·잔량만 감소)으로 열렸고, 그 계약을 여기서 잠근다. 스케줄 배선은
tests/test_timeline_bef.py가 커버.
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


# 루프 하니스의 가짜 현재시각(_run_one_tick) = 2026-06-01 08:36 KST.
# own 의도 기본 accepted_ts는 그보다 120초 앞 — 접수 반영 유예(_OWN_SETTLE_SEC=60)를
# 이미 지난 상태라, 유예 게이트가 아니라 복원 로직 자체를 검증하게 된다.
_ACCEPTED_OLD = "2026-06-01T08:34:00+09:00"


def _own(iid, side, qty, order_no, strategy_id="s", accepted_ts=_ACCEPTED_OLD):
    return {"intent_id": iid, "strategy_id": strategy_id, "symbol": S,
            "side": side, "qty": qty, "ref_price": 300.0, "order_no": order_no,
            "accepted_ts": accepted_ts}


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


def test_overshoot_splits_into_full_then_partial_cancel():
    """초과 5 — 최신(3) 전량취소 + 다음(4)에서 2만 부분취소 → 잔여 0·residual 없음.

    종전 계약은 "다음 주문 통째 취소는 과잉"이라 그 직전에서 멈추고(stopped_short)
    초과 2를 개장후 수렴에 넘겼다(왕복 수수료·슬리피지). 부분취소 실측(2026-07-20)
    으로 창내에서 초과분만 정확히 걷는다 — 순서(최신 우선)·수량·분류를 잠근다."""
    plan = plan_guard_actions({S: 0}, {S: 2}, [], [],
                              [_pend("1", "buy", 4), _pend("2", "buy", 3)])
    assert plan.cancels == [("2", S, 3), ("1", S, 2)]
    assert plan.partial_cancels == {"1"}          # 전량취소분("2")은 미포함
    assert not [d for d in plan.decisions if d["action"] == "guard_ext_residual"]


def test_indivisible_manual_order_partially_cancelled():
    """쪼갤 수 없던 큰 수동 주문(잔량 3)에 초과 1 — 통째 취소 대신 1만 부분취소."""
    plan = plan_guard_actions({S: 0}, {S: 2}, [], [], [_pend("41", "buy", 3)])
    assert plan.cancels == [("41", S, 1)]
    assert plan.partial_cancels == {"41"}
    assert not [d for d in plan.decisions if d["action"] == "guard_ext_residual"]
    trim = [d for d in plan.decisions if d["action"] == "guard_ext_trim"]
    assert len(trim) == 1 and "buy 1 of 3 부분취소" in trim[0]["reason"]


def test_residual_only_when_same_side_manual_exhausted():
    """같은 방향 수동을 다 걷어도 초과가 남으면 여전히 residual("소진") — 수렴 몫.

    보유5 vs 원장1(드리프트 4) + 수동 매수 2 → 초과 6인데 취소 가능한 건 2뿐."""
    plan = plan_guard_actions({S: 5}, {S: 1}, [], [], [_pend("7", "buy", 2)])
    assert plan.cancels == [("7", S, 2)] and plan.partial_cancels == set()
    res = [d for d in plan.decisions if d["action"] == "guard_ext_residual"]
    assert len(res) == 1 and "소진" in res[0]["reason"]


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


# ── A1 흡수분 소멸 — 살아있는 주문 0인데 D≠0이면 창내 재실행 ─────────────────
# §19 A1은 동시호가 수동 미체결을 "곧 체결될 보유"로 선반영한다. 그 전제로 우리 청산이
# 넷팅(book)돼 **실주문 0건**이 되는데, 유저가 그 수동 주문을 취소하면 물리 노출이
# 남는다. 아침창은 08:46 개장후 수렴이 교정하지만 **종가창은 장 종료(선물 15:45)로
# 교정 기회가 없다**(scheduler.py "익일 개장 carry" 주석 = §19.2 원리적 한계) —
# 창 안에서 잡지 못하면 오버나이트가 확정된다.
#
# 검산식 D는 이미 이 어긋남을 정확히 계산하고 있었다. 놓친 건 `not exts` 단축이
# 그 값을 버리고 있었다는 것 — 자를 외부 주문이 없다고 해서 어긋남이 없는 게 아니다.


def test_uncovered_gap_reruns_when_no_live_order_remains():
    """A1 흡수분 취소 — 원장0(북킹됨)·보유4·살아있는 주문 0 → D=4 → 창내 재실행.

    수동 매도4를 믿고 청산을 book했는데 유저가 취소한 형상. 자를 외부 주문이
    없으므로 종전엔 `not exts`로 조용히 넘어갔다(= 오버나이트 확정).
    """
    # gap_streak={S: 1} = 직전 틱에도 같은 갭을 봤다(연속 2틱 = 반영 지연 배제).
    plan = plan_guard_actions({S: 4}, {}, [], [], [], target_symbols={S},
                              gap_streak={S: 1})
    assert plan.rerun is True, "목표와 현실이 어긋났는데 재실행이 발동하지 않음"
    assert plan.cancels == [] and plan.fail_intents == [], \
        "미달 보정은 취소·intent 되돌림이 아니라 재실행으로만"
    assert [d for d in plan.decisions if d["action"] == "guard_uncovered_gap"]


def test_uncovered_gap_not_triggered_while_external_alive():
    """A1 인수분이 **살아있는 동안**은 무행동 — 전량 체결 가정이 아직 유효하다."""
    plan = plan_guard_actions({S: 4}, {}, [], [], [_pend("050", "sell", 4)],
                              target_symbols={S})
    assert plan == GuardPlan(), "수동 주문이 살아있는데 재실행하면 이중 발주"


def test_uncovered_gap_not_triggered_while_own_order_live():
    """우리 주문이 살아있으면 재실행하지 않는다 — 단일가에 체결될 몫이다.

    보유15·원장10·own 매도10(잔여10): D=5지만 own이 in-flight이므로 창내
    재발주는 이중 발주 위험만 만든다(DRIFT 멱등이 막아도 헛사이클).
    """
    own = [_own("x1", "sell", 10, "700")]
    plan = plan_guard_actions({S: 15}, {S: 10}, own, [_pend("700", "sell", 10)], [])
    assert plan.rerun is False


def test_uncovered_gap_first_observation_does_not_order():
    """🔴 한 틱만 보인 갭으로는 절대 발주하지 않는다 — 반영 지연 배제.

    미체결 조회는 10~20초 지연이 실측됐고(2026-07-20) 폴링은 10초다. 살아있는
    수동 주문이 한 틱 안 보인 사이 재실행하면 drift 매도가 나가고, 그 수동 주문도
    단일가에 체결돼 **오버셀**이 된다 — 브랜치 ①이 _OWN_SETTLE_SEC로 막는 것과
    정확히 같은 부류. (이 유예가 없던 초안을 적대적 리뷰가 CRITICAL로 잡았다.)
    """
    plan = plan_guard_actions({S: 4}, {}, [], [], [], target_symbols={S})
    assert plan.rerun is False, "첫 관측만으로 실주문을 유발하면 오버셀 위험"
    assert plan.gap_seen == [S], "관측 자체는 보고돼야 다음 틱에 연속으로 센다"
    assert [d for d in plan.decisions if d["action"] == "guard_gap_pending"]


def test_uncovered_gap_holds_when_own_intent_alive_even_if_broker_shows_none():
    """저널엔 our 의도가 있는데 브로커 미체결에 안 보이는 구간 — 무개입.

    `op`(브로커)만 보면 부족하다: DRIFT 의도가 전략 의도를 상쇄해 oi_total==0이면
    브랜치 ①도 통과하므로, 우리 주문이 살아있는데 분기 ③이 발동할 수 있었다.
    """
    own = [_own("x1", "sell", 5, "700"), _own("d1", "buy", 5, "701",
                                              strategy_id="DRIFT:close")]
    plan = plan_guard_actions({S: 4}, {}, own, [], [], target_symbols={S},
                              gap_streak={S: 5})
    assert plan.rerun is False


def test_uncovered_gap_ignores_symbol_without_confirmed_target():
    """G2 유지 — 이번 창이 목표를 세우지 않은 심볼(비전략 보유)은 불간섭."""
    plan = plan_guard_actions({S: 4}, {}, [], [], [], target_symbols=set())
    assert plan == GuardPlan(), "목표 없는 심볼까지 재실행하면 G2 위반"


def test_uncovered_gap_rerun_capped():
    """재실행이 D를 못 지우는 경우(사이징0·하드컷) 창 내내 반복하지 않는다."""
    plan = plan_guard_actions({S: 4}, {}, [], [], [], converge_reruns={S: 2},
                              target_symbols={S}, gap_streak={S: 1})
    assert plan.rerun is False
    assert [d for d in plan.decisions if d["action"] == "guard_gap_giveup"]


def test_uncovered_gap_counts_symbol_for_cap():
    """재실행 발동 시 상한 카운트를 위해 심볼을 보고한다."""
    plan = plan_guard_actions({S: 4}, {}, [], [], [], target_symbols={S},
                              gap_streak={S: 1})
    assert plan.gap_symbols == [S]


# ── 창 누적 유효 잔량(trimmed_to) — 부분취소 반복 발행 차단 ──────────────────
# 실측 2026-07-20: 취소가 브로커 미체결 조회에 반영되기까지 10~20초 걸리는데 가드
# 폴링은 10초다. 옛 잔량 그대로 초과분을 다시 계산하면 같은 주문을 두 번 자른다 —
# 종전 전량취소는 재발행이 브로커 거절("이미 취소")로 무해했지만 부분취소는 거절되지
# 않고 실제로 더 취소된다(과잉 개입).
def test_trim_not_reissued_while_broker_shows_stale_remain():
    """틱1에서 자른 몫은 틱2의 유효 잔량에서 빠져 D=0 — 재취소 없음."""
    t1 = plan_guard_actions({S: 0}, {S: 2}, [], [], [_pend("41", "buy", 3)])
    assert t1.cancels == [("41", S, 1)] and t1.trim_to == {"41": 2}
    # 틱2 — 조회는 아직 3(미반영). 창 누적이 유효 잔량을 2로 눌러 D=0.
    t2 = plan_guard_actions({S: 0}, {S: 2}, [], [], [_pend("41", "buy", 3)],
                            trimmed_to={"41": 2})
    assert t2 == GuardPlan()


def test_trim_cap_not_double_subtracted_after_broker_reflects():
    """조회가 취소를 반영한 뒤(3→2)엔 조회값이 진실 — 누적을 한 번 더 빼지 않는다.

    누적을 "이미 취소한 수량"으로 두고 조회 잔량에서 빼는 형태는 반영된 순간부터
    이중 차감돼 D가 계속 1 낮게 나온다. 반영은 창 초반(10~20초)에 끝나므로 그
    왜곡이 창의 대부분을 지배한다. 여기선 새로 들어온 수동 매수 1이 명백한
    초과인데도 취소되지 않는 형태(초과 방치)로 드러난다."""
    assert plan_guard_actions({S: 0}, {S: 2}, [], [], [_pend("41", "buy", 2)],
                              trimmed_to={"41": 2}) == GuardPlan()
    plan = plan_guard_actions({S: 0}, {S: 2}, [], [],
                              [_pend("41", "buy", 2), _pend("90", "buy", 1)],
                              trimmed_to={"41": 2})
    assert plan.cancels == [("90", S, 1)] and plan.partial_cancels == set()


def test_trim_accumulation_allows_further_justified_cancel():
    """누적은 과잉만 막는다 — 초과가 커지면 정당한 추가 취소는 그대로 나간다.

    유효 잔량 2를 전부 걷으므로 부분취소가 아니다(원 주문 3 중 1은 이미 요청분)."""
    plan = plan_guard_actions({S: 0}, {S: 0}, [], [], [_pend("41", "buy", 3)],
                              trimmed_to={"41": 2})
    assert plan.cancels == [("41", S, 2)]
    assert plan.partial_cancels == set() and plan.trim_to == {"41": 0}


def test_fully_trimmed_order_skipped_not_recancelled():
    """전량취소를 이미 요청한 주문은 조회에 남아 있어도 유효 잔량 0 — 재취소 없음."""
    plan = plan_guard_actions({S: 0}, {S: 0}, [], [], [_pend("41", "buy", 3)],
                              trimmed_to={"41": 0})
    assert plan == GuardPlan()


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


# ── 접수 반영 유예(_OWN_SETTLE_SEC) — "미체결에 없음"≠"유저 취소" ──────────────
# 브로커 미체결 조회는 접수를 즉시 반영하지 않는다(실측 2026-07-20: 취소가 t+10s엔
# 미반영·t+20s엔 반영). 폴링은 10초라 반영 전 틱에서 own 주문이 통째로 안 보인다.
# 그걸 유저 취소로 읽으면 intent를 풀고 재실행 → 원주문이 살아있는 채 같은 주문이
# 한 번 더 나가 **이중 발주**(동시호가 단일가라 둘 다 체결)가 된다.
import datetime as _dt  # noqa: E402

_NOW = _dt.datetime(2026, 6, 1, 8, 36, tzinfo=_dt.timezone(_dt.timedelta(hours=9)))


def _at(sec_ago: float) -> str:
    return (_NOW - _dt.timedelta(seconds=sec_ago)).isoformat()


def test_fresh_own_intent_holds_instead_of_restoring():
    """접수 10초 전 own 의도가 미체결에 안 보임 — 유예 내라 복원하지 않는다."""
    plan = plan_guard_actions(
        {S: 0}, {S: 0}, [_own("i1", "buy", 5, "101", accepted_ts=_at(10))], [], [],
        now=_NOW)
    assert plan.cancels == [] and plan.fail_intents == [] and plan.rerun is False
    hold = [d for d in plan.decisions if d["action"] == "guard_hold_unsettled"]
    assert len(hold) == 1 and "유예" in hold[0]["reason"]


def test_settled_own_intent_still_restores():
    """유예를 지난(120초 전 접수) 의도는 종전대로 복원 — 기능을 죽이지 않았다."""
    plan = plan_guard_actions(
        {S: 0}, {S: 0}, [_own("i1", "buy", 5, "101", accepted_ts=_at(120))], [], [],
        now=_NOW)
    assert plan.rerun is True and plan.fail_intents[0][0] == "i1"
    assert not [d for d in plan.decisions if d["action"] == "guard_hold_unsettled"]


def test_missing_accepted_ts_is_treated_as_unsettled():
    """accepted_ts 부재(구버전 저널·필드 유실) — 나이를 모르면 보수적으로 보류."""
    plan = plan_guard_actions(
        {S: 0}, {S: 0}, [_own("i1", "buy", 5, "101", accepted_ts="")], [], [],
        now=_NOW)
    assert plan.rerun is False
    assert any(d["action"] == "guard_hold_unsettled" for d in plan.decisions)


def test_settle_gate_is_per_symbol_and_only_gates_branch_one():
    """유예는 own 브랜치만 막는다 — 같은 틱의 **다른 심볼** ext 트리밍은 그대로.

    유예가 전역 보류였다면 초과 수동 주문이 창을 넘겨 수렴 몫으로 밀린다."""
    kq = "코스닥150선물"
    fresh = dict(_own("i1", "buy", 5, "101", accepted_ts=_at(5)))
    ext = {"order_no": "300", "symbol": kq, "side": "buy", "remain_qty": 2}
    plan = plan_guard_actions({S: 0, kq: 0}, {S: 0, kq: 0},
                              [fresh], [], [ext], now=_NOW)
    assert plan.cancels == [("300", kq, 2)]          # 다른 심볼 ext는 정상 취소
    assert plan.rerun is False                        # own 심볼은 보류
    assert any(d["action"] == "guard_hold_unsettled" for d in plan.decisions)


def test_now_none_keeps_legacy_time_independent_contract():
    """now 미전달(단위테스트 기본) — 유예 미적용으로 종전 판정 그대로."""
    plan = plan_guard_actions(
        {S: 0}, {S: 0}, [_own("i1", "buy", 5, "101", accepted_ts=_at(1))], [], [])
    assert plan.rerun is True


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
def _run_one_tick(monkeypatch, broker, submitted, read_today, cycles_today=(),
                  ledger=None, ticks=1):
    """가드 루프를 정확히 `ticks`틱 실행하는 하니스 — 시간·달력·러너·저널 격리.

    이 하니스 자체가 G0 회귀 잠금이다: until이 naive면 kst_now(aware) 비교에서
    TypeError로 한 틱도 못 돈다(v0.9.79 릴리스본의 실결함 — 수정 검증).
    ticks≥2는 창 누적 상태(restores·trimmed_to)가 틱을 넘어 유지되는지 검증용."""
    import datetime as _dt

    from quant_core import market_calendar as mc

    from localapp import auction_guard as ag
    from localapp import runner as runner_mod
    from localapp import trader as trader_mod

    KST = ag.KST
    t0 = _dt.datetime(2026, 6, 1, 8, 36, tzinfo=KST)
    # 틱 경계는 **루프 끝의 sleep**으로 센다 — kst_now() 호출 횟수로 세면 본문에서
    # 시각을 한 번 더 읽는 것만으로(예: plan_guard_actions의 반영유예 판정) 창이
    # 조기 종료돼 테스트가 프로덕션과 다른 횟수를 돈다.
    slept = {"n": 0}

    def fake_now():
        return t0 if slept["n"] < ticks else t0.replace(hour=9)

    def fake_sleep(_s):
        slept["n"] += 1

    class _FakeTrader:
        def __init__(self, b):
            self.ledger = dict(ledger or {})

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
    monkeypatch.setattr(ag._time, "sleep", fake_sleep)
    monkeypatch.setattr(intents, "submitted_window",
                        lambda d, since, path=None: list(submitted))
    monkeypatch.setattr(intents, "_read_today", lambda d, path=None: list(read_today))
    monkeypatch.setattr(intents, "mark_failed",
                        lambda d, iid, reason: failed.append(iid))
    monkeypatch.setattr(ag.order_log, "log_cycle",
                        lambda dec, meta: cycles.append((dec, meta)))
    monkeypatch.setattr(ag.order_log, "read_cycles",
                        lambda limit=80: list(cycles_today))
    out = ag.run_auction_guard("futures", "open", (8, 34), (8, 44, 30))
    return out, reruns, failed, broker


class _LoopBroker:
    """pending 조회 시퀀스·취소 응답을 주입하는 루프 하니스 브로커."""

    def __init__(self, pend_seq, cancel_resp, positions=None):
        # 예외 원소는 조회 실패 주입 — 그대로 보관하고 호출 시 raise.
        self._seq = [x if isinstance(x, BaseException) else list(x)
                     for x in pend_seq]
        self._cancel_resp = cancel_resp
        self._positions = list(positions or [])
        self.cancelled: list = []
        self.partials: list = []          # cancel(partial=…) 수신 기록

    def pending_orders(self, *, strict: bool = False):
        nxt = self._seq.pop(0) if len(self._seq) > 1 else self._seq[0]
        # 시퀀스 원소가 예외면 조회 실패 재현 — strict 소비자(가드)는 이걸 빈 목록으로
        # 강등받으면 안 된다(실패를 공집합으로 읽으면 own 오판 → 이중 발주).
        if isinstance(nxt, BaseException):
            raise nxt
        return list(nxt)

    def cancel(self, order_no, symbol, qty, *, partial=False):
        self.cancelled.append((order_no, symbol, qty))
        self.partials.append(partial)
        return self._cancel_resp

    def account_snapshot(self):
        return {"balance": {}, "positions": list(self._positions)}


def _fut_pend(order_no, side, remain, sym=S):
    return {"order_no": order_no, "symbol": sym, "side": side, "remain_qty": remain,
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


def test_loop_pending_query_failure_holds_tick_not_treated_as_empty(monkeypatch):
    """미체결 조회 실패는 **공집합이 아니다** — 그 틱 전면 보류(실행부 회귀).

    브로커 어댑터들은 조회 실패를 로그+빈 목록으로 강등한다(스냅샷·관측엔 그게 맞다).
    가드가 그 빈 목록을 그대로 받으면 own 주문이 "사라졌다"로 보여 유저 취소로 오판,
    intent를 풀고 재실행해 **이중 발주**한다. 그래서 가드만 strict=True로 조회하고,
    예외가 오면 아무 판정도 하지 않는다.

    스텁은 **실제 어댑터 거동을 그대로 재현**한다 — strict=False면 빈 목록 강등,
    strict=True면 전파. 그래야 가드에서 strict를 빼는 순간 이 테스트가 실제로
    깨진다(강등된 []를 유저 취소로 읽어 복원이 발화)."""
    own = [_own("i1", "buy", 5, "101")]            # 유예는 이미 지난 의도

    class _DegradingBroker(_LoopBroker):
        def pending_orders(self, *, strict: bool = False):
            if strict:
                raise RuntimeError("t0434 타임아웃")
            return []                              # 어댑터의 실패→빈목록 강등

    broker = _DegradingBroker([[]], {"success": True})
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker, own, [{"order_no": "101"}])
    assert b.cancelled == [] and failed == [] and reruns == []
    assert out["n_cancel"] == 0


def test_loop_fresh_intent_not_restored(monkeypatch):
    """실행부가 now를 넘겨 반영 유예가 **실제로** 걸리는지(배선 회귀).

    순수 함수 테스트는 now를 직접 넣지만, run_auction_guard가 now 전달을 빠뜨리면
    유예는 코드에 있어도 라이브에서 무효다(now=None → 게이트 미적용). 하니스 시각은
    08:36이므로 08:35:40 접수(20초 전)는 유예 안 — 복원이 나가면 안 된다."""
    own = [_own("i1", "buy", 5, "101", accepted_ts="2026-06-01T08:35:40+09:00")]
    broker = _LoopBroker([[]], {"success": True})      # 미체결에 안 보임
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker, own, [{"order_no": "101"}])
    assert b.cancelled == [] and failed == [] and reruns == []


def test_loop_guard_requests_strict_pending(monkeypatch):
    """가드는 strict=True로 조회한다 — 어댑터의 실패→빈목록 강등을 받지 않겠다는 의사."""
    seen: list = []

    class _B(_LoopBroker):
        def pending_orders(self, *, strict: bool = False):
            seen.append(strict)
            return []

    _run_one_tick(monkeypatch, _B([[]], {"success": True}), [], [])
    assert seen and all(seen), f"strict=True로 호출돼야 — 실제 {seen}"


def test_loop_partial_cancel_does_not_block_own_restore(monkeypatch):
    """부분취소가 섞인 plan에서 own 복원이 헛보류되지 않는다(실행부 회귀).

    취소 확정 재확인은 "전량 취소분이 소멸했는가"만 물어야 한다. 부분취소분은
    원주문번호를 유지한 채 잔량만 줄어 살아남는 게 정상이라(실측 2026-07-20),
    이를 '취소 미확정'으로 읽으면 같은 틱에 섞인 다른 심볼의 own 복원이 매 틱
    보류돼 창을 넘긴다. 심볼 정렬상 kq(부분취소)가 먼저·S(복원)가 나중."""
    kq = "코스닥150선물"
    own = [_own("i1", "buy", 5, "101")]
    pend = [_fut_pend("101", "buy", 2),               # own — 유저가 5→2 감량
            _fut_pend("300", "buy", 3, sym=kq)]       # ext — 원장 1 대비 초과 2
    after = [_fut_pend("300", "buy", 1, sym=kq)]      # 부분취소 후 생존(3→1)
    broker = _LoopBroker([pend, pend, after], {"success": True})
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker, own, [{"order_no": "101"}],
        ledger={"p1": {"symbol": kq, "qty": 1, "side": "long"}})
    assert b.cancelled == [("300", kq, 2), ("101", S, 2)]   # 부분 2 · 전량 2
    assert b.partials == [True, False]      # 부분취소 의사만 어댑터에 전달(후속①)
    assert failed == ["i1"] and len(reruns) == 1            # 보류 아님 — 복원 진행
    assert out["n_cancel"] == 2


# ── 실행부 — 취소 수리분만 창 누적에 기록(후속②) ─────────────────────────────
def _trim_loop(monkeypatch, cancel_resp):
    """같은 상황을 2틱 굴리는 공통 하니스 — 조회는 두 틱 모두 옛 잔량 3을 노출.

    원장 2 · 보유 0 · 수동 매수 3 → 초과 1(부분취소). 브로커 반영 지연(10~20초)이
    폴링(10초)보다 길다는 실측을 그대로 재현한다."""
    broker = _LoopBroker([[_fut_pend("300", "buy", 3)]], cancel_resp)
    return _run_one_tick(monkeypatch, broker, [], [],
                         cycles_today=[_cycle_rec("cycle", "futures")],
                         ledger={"p1": {"symbol": S, "qty": 2, "side": "long"}},
                         ticks=2)


def test_loop_accepted_trim_not_reissued_next_tick(monkeypatch):
    """취소가 수리되면 창 누적에 기록돼 다음 틱이 같은 초과분을 다시 자르지 않는다."""
    out, reruns, failed, b = _trim_loop(monkeypatch, {"success": True})
    assert b.cancelled == [("300", S, 1)]      # 틱2는 무취소
    assert b.partials == [True] and out["n_cancel"] == 1


def test_loop_rejected_trim_not_accumulated_retries_next_tick(monkeypatch):
    """거절된 취소는 누적하지 않는다 — 기록하면 반대로 과소 취소(초과 방치)가 된다."""
    out, reruns, failed, b = _trim_loop(monkeypatch,
                                        {"success": False, "message": "거절"})
    assert b.cancelled == [("300", S, 1), ("300", S, 1)]    # 두 틱 모두 재시도
    assert out["n_cancel"] == 0


def test_loop_race_replan_also_honors_trim_accumulation(monkeypatch):
    """own 복원 race 재조회의 **재계획**도 창 누적을 받아야 한다(누락 회귀).

    fail_intents가 잡히면 9c race 봉합으로 한 번 더 조회·재계획하는데, 그 두 번째
    plan_guard_actions에 trimmed_to를 안 넘기면 같은 틱에 섞인 다른 심볼의 ext가
    옛 잔량 기준으로 다시 잘린다 — 첫 계획만 고쳐선 안 되는 이유.
    틱1에 kq를 부분취소(3→1 목표)하고, 틱2에 own 감량으로 재계획 경로를 태운다."""
    kq = "코스닥150선물"
    own = [_own("i1", "buy", 5, "101")]
    t1 = [_fut_pend("101", "buy", 5), _fut_pend("300", "buy", 3, sym=kq)]
    t2 = [_fut_pend("101", "buy", 2), _fut_pend("300", "buy", 3, sym=kq)]
    broker = _LoopBroker([t1, t2, t2, [_fut_pend("300", "buy", 3, sym=kq)]],
                         {"success": True})
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker, own, [{"order_no": "101"}],
        ledger={"p1": {"symbol": kq, "qty": 1, "side": "long"}}, ticks=2)
    # 틱1 ext 부분취소 2 · 틱2 own 전량취소 2뿐 — kq 재취소 없음.
    assert b.cancelled == [("300", kq, 2), ("101", S, 2)]
    assert b.partials == [True, False]
    assert failed == ["i1"] and len(reruns) == 1 and out["n_cancel"] == 2


# ── 목표 확정 게이트(2026-07-20 유저 정련) — 전멸 시 수동 존중·확정 후 정합 ──
def _cycle_rec(kind, icls, ts="2026-06-01T08:35:20+09:00", error=None):
    s = {"kind": kind, "market": "KRX", "instrument_class": icls}
    if error:
        s["error"] = error
    return {"ts": ts, "summary": s}


def test_loop_holds_before_target_confirmed(monkeypatch):
    """사이클 완료 기록도 own 의도도 없으면(전멸·발주 전 지연) 수동 미체결을
    취소하지 않는다 — stale 원장 강제 금지. guard_hold_no_target 관측 1회만."""
    pend = [_fut_pend("300", "buy", 3)]
    broker = _LoopBroker([pend], {"success": True})
    out, reruns, failed, b = _run_one_tick(monkeypatch, broker, [], [])
    assert b.cancelled == [] and reruns == [] and failed == []
    assert out["n_cancel"] == 0 and out["n_decisions"] == 1


def test_loop_gate_opens_via_zero_order_cycle_record(monkeypatch):
    """0건 발주로 완료된 사이클 기록 = "변화 없음" 확정 목표 — 원장 기준 정합 재개."""
    pend = [_fut_pend("300", "sell", 2)]
    broker = _LoopBroker([pend], {"success": True},
                         positions=[{"symbol": S, "qty": 5, "side": "long"}])
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker, [], [],
        cycles_today=[_cycle_rec("cycle", "futures")],
        ledger={"p1": {"symbol": S, "qty": 5, "side": "long"}})
    assert b.cancelled == [("300", S, 2)]
    assert out["n_cancel"] == 1


def test_loop_errored_cycle_record_keeps_hold(monkeypatch):
    """error로 끝난 사이클 기록은 목표 확정이 아니다 — 보류 유지(catchup 계약 미러)."""
    pend = [_fut_pend("300", "sell", 2)]
    broker = _LoopBroker([pend], {"success": True},
                         positions=[{"symbol": S, "qty": 5, "side": "long"}])
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker, [], [],
        cycles_today=[_cycle_rec("cycle", "futures", error="서버 불가")],
        ledger={"p1": {"symbol": S, "qty": 5, "side": "long"}})
    assert b.cancelled == [] and out["n_cancel"] == 0


def test_window_summary_predicate(monkeypatch):
    """창별 kind 매핑·클래스 일치(full-scope None 포함)·당일 ts·무-error 술어.

    반환이 bool에서 **요약 dict**로 바뀌었다 — 목표 확정 여부(None 여부)와
    target_symbols(가드 우주 확장분)를 한 번의 읽기로 함께 답한다.
    """
    import datetime as _dt

    from localapp import auction_guard as ag

    today = _dt.date(2026, 6, 1)
    rows = [_cycle_rec("day_trade_close", "futures"),
            _cycle_rec("cycle", "stock"),
            _cycle_rec("cycle", "futures", ts="2026-05-30T08:35:00+09:00")]
    monkeypatch.setattr(ag.order_log, "read_cycles", lambda limit=80: rows)
    assert ag._window_summary(today, "close", "futures") is not None
    assert ag._window_summary(today, "open", "futures") is None
    monkeypatch.setattr(ag.order_log, "read_cycles",
                        lambda limit=80: [_cycle_rec("catchup_cycle", None)])
    assert ag._window_summary(today, "open", "futures") is not None


def test_window_summary_returns_latest_for_reruns(monkeypatch):
    """창내 재실행이 새 기록을 남기므로 **최신** 요약을 써야 한다 — 옛 목표로
    판정하면 방금 재수립한 목표를 못 본다(read_cycles는 최신순)."""
    import datetime as _dt

    from localapp import auction_guard as ag

    today = _dt.date(2026, 6, 1)
    newer = _cycle_rec("cycle", "futures")
    newer["summary"]["target_symbols"] = ["NEW"]
    older = _cycle_rec("cycle", "futures")
    older["summary"]["target_symbols"] = ["OLD"]
    monkeypatch.setattr(ag.order_log, "read_cycles", lambda limit=80: [newer, older])
    assert ag._window_summary(today, "open", "futures")["target_symbols"] == ["NEW"]


# ── A1 흡수분 소멸 — 루프 배선(창 누적 상한이 실제로 동작하는가) ─────────────
# 하니스는 개장창(window="open")을 돈다 — 목표 미달 판정 자체는 창 무관이고,
# 재실행 라우팅(open→run_cycle · close→run_close_cycle)은 기존 복원 테스트가 덮는다.
# 실제 위험이 큰 쪽은 종가창이다(장 종료로 창밖 교정 기회가 없음 — §19.2).
def _fut_pos(qty, sym=S, side="long"):
    return {"symbol": sym, "qty": qty, "side": side}


# 목표 확정 게이트(guard_hold_no_target) 통과용 — 이번 창 담당 사이클의 무-error
# 완료 기록. A1 오상쇄는 **사이클이 돌아 book 정산을 마친 뒤** 생기는 형상이라
# 실제로도 이 기록이 있는 상태다.
_CYCLE_DONE = ({"ts": "2026-06-01T08:35:00+09:00",
                "summary": {"kind": "cycle", "market": "KRX",
                            "instrument_class": "futures",
                            "target_symbols": [S]}},)


def test_loop_uncovered_gap_triggers_rerun_on_second_tick(monkeypatch):
    """원장0(북킹됨)·보유4·미체결 전무 → **연속 2틱** 관측 후 창내 재실행.

    A1이 인수한 수동 주문을 유저가 취소한 형상. 창 안에서 잡지 못하면 종가창에선
    장 종료로 교정 기회가 사라져 오버나이트가 확정된다. 단, 첫 틱은 미체결 조회
    반영 지연일 수 있어 조치하지 않는다(가드 브랜치 ①의 _OWN_SETTLE_SEC와 같은 부류).
    """
    broker = _LoopBroker([[]], {"success": True}, positions=[_fut_pos(4)])
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker, [], [], cycles_today=_CYCLE_DONE, ledger={}, ticks=1)
    assert reruns == [], "첫 틱에 발주하면 반영 지연 헛갭에서 오버셀"

    broker2 = _LoopBroker([[]], {"success": True}, positions=[_fut_pos(4)])
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker2, [], [], cycles_today=_CYCLE_DONE, ledger={}, ticks=2)
    assert len(reruns) == 1, f"2틱 연속 갭인데 재실행 미발동: {reruns}"
    assert b.cancelled == [] and failed == [], "미달 보정은 취소·intent 되돌림 없이"


def test_loop_uncovered_gap_rerun_capped_across_ticks(monkeypatch):
    """재실행이 D를 못 지워도(러너 stub이라 보유 불변) 창 누적 상한에서 멈춘다."""
    broker = _LoopBroker([[]], {"success": True}, positions=[_fut_pos(4)])
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker, [], [], cycles_today=_CYCLE_DONE, ledger={}, ticks=8)
    assert len(reruns) == 2, f"상한(_MAX_CONVERGE_RERUNS=2) 초과: {len(reruns)}회"


def test_loop_resolved_own_order_is_never_reclassified_as_external(monkeypatch):
    """🔴 N1의 가드측 안전 속성 — 종결된 own 주문이 **외부(수동)로 오분류되지 않는다**.

    N1 수정으로 `submitted_window`가 resolved intent를 제외하게 됐다(활성만 반환).
    그러면 그 주문번호가 `own_win_nos`에서 빠지는데, 만약 ext로 떨어지면 가드가
    **자기 주문을 수동 주문으로 보고 취소**한다. 실제로는 `all_own_nos`가
    `_read_today` 전건(phase 무관)에서 오므로 여전히 걸려 '다른 창 own = 불간섭'으로
    분류된다. 이 경로가 깨지면 조용한 자기주문 취소가 되므로 회귀로 고정한다.
    """
    # submitted_window(=이번 창 활성)엔 없지만 저널엔 order_no가 남은 상태 = 종결됨.
    broker = _LoopBroker([[_fut_pend("777", "sell", 3)]], {"success": True},
                         positions=[_fut_pos(3)])
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker, [], [{"order_no": "777"}],
        cycles_today=_CYCLE_DONE,
        ledger={"s1": {"symbol": S, "qty": 3, "side": "long"}})
    assert b.cancelled == [], f"종결된 자기 주문을 수동으로 오인해 취소함: {b.cancelled}"


def test_loop_no_gap_rerun_when_ledger_matches_broker(monkeypatch):
    """정상(원장=보유) — 재실행 없음. 거짓 발동은 헛사이클·이중 발주 위험."""
    broker = _LoopBroker([[]], {"success": True}, positions=[_fut_pos(4)])
    out, reruns, failed, b = _run_one_tick(
        monkeypatch, broker, [], [], cycles_today=_CYCLE_DONE,
        ledger={"s1": {"symbol": S, "qty": 4, "side": "long"}})
    assert reruns == []

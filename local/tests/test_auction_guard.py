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
    calls = {"n": 0}

    def fake_now():
        calls["n"] += 1
        return t0 if calls["n"] <= ticks else t0.replace(hour=9)

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
    monkeypatch.setattr(ag._time, "sleep", lambda s: None)
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
        self._seq = [list(x) for x in pend_seq]
        self._cancel_resp = cancel_resp
        self._positions = list(positions or [])
        self.cancelled: list = []
        self.partials: list = []          # cancel(partial=…) 수신 기록

    def pending_orders(self):
        return list(self._seq.pop(0)) if len(self._seq) > 1 else list(self._seq[0])

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


def test_target_confirmed_predicate(monkeypatch):
    """창별 kind 매핑·클래스 일치(full-scope None 포함)·당일 ts·무-error 술어."""
    import datetime as _dt

    from localapp import auction_guard as ag

    today = _dt.date(2026, 6, 1)
    rows = [_cycle_rec("day_trade_close", "futures"),
            _cycle_rec("cycle", "stock"),
            _cycle_rec("cycle", "futures", ts="2026-05-30T08:35:00+09:00")]
    monkeypatch.setattr(ag.order_log, "read_cycles", lambda limit=80: rows)
    assert ag._target_confirmed(today, "close", "futures") is True
    assert ag._target_confirmed(today, "open", "futures") is False
    monkeypatch.setattr(ag.order_log, "read_cycles",
                        lambda limit=80: [_cycle_rec("catchup_cycle", None)])
    assert ag._target_confirmed(today, "open", "futures") is True

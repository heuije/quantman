# -*- coding: utf-8 -*-
"""목표상태 수렴 E2E — kr-target-reconciliation.md §6 시뮬레이션 재현 + §12 적대 케이스.

§6: 2026-07-13 종가 → 07-14 종가(mwmw 실측 수동매매 주입)를 SimBroker 위에서 그대로
재현한다. 날짜는 하니스 기준(2026-06-01 월 → 06-02 화)으로 이식 — 구조는 동일:

  D1 15:40  #29 진입5 · 수동 롱4 보유   → 순매수 1 (수동4 인수)
  D2 08:35  수동 매도5 (브로커 0)
  D2 08:55  #29 청산 · #27 진입6        → 순매수 6 (상쇄5 + 잔여1 + 복원5)
  D2 낮     수동 매도3 · 매도1 (브로커 2)
  D2 15:40  #27 청산 · #29 진입5        → 순매수 3 (상쇄5 + drift 3)

검증(기대효과 §16): E2 phantom 0 · E3 오버셀 0(매도 주문 자체 없음) · E4 최종 drift 0 ·
E5 잔여 노출 0 · E6 순주문 1·6·3 정확 재현 · E7 판정불가=hold.
"""
from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

_LOCAL = Path(__file__).resolve().parent.parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

import pandas as pd

SYM = "코스피200선물"

_DUMMY_SIGNAL = {"op": "compare", "inputs": {
    "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
    "right": {"op": "const", "params": {"value": 0}}}, "params": {"op": ">"}}


def _spec():
    from quant_core.exec_defaults import instrument_spec
    return instrument_spec(SYM)


def _def29(amount: int) -> dict:
    """오버나이트 롱 — 종가 진입(fill=close)·hold_days=1(익일 아침 청산)."""
    return {"name": "오버나이트롱", "engine": "ir", "query": "simulate",
            "universe": {"kind": "single", "symbols": [SYM]},
            "signal": _DUMMY_SIGNAL,
            "position": {"direction": "long",
                         "sizing": {"mode": "fixed_amount", "amount_krw": amount},
                         "entry": {"mode": "on_signal"},
                         "exit": {"hold_days": 1}},
            "simulation": {"fill": "close"}}


def _def27(amount: int) -> dict:
    """역추종 — 시가 진입(fill=next_open)·당일 종가 청산(hold_days=0)."""
    return {"name": "역추종", "engine": "ir", "query": "simulate",
            "universe": {"kind": "single", "symbols": [SYM]},
            "signal": _DUMMY_SIGNAL,
            "position": {"direction": "long",
                         "sizing": {"mode": "fixed_amount", "amount_krw": amount},
                         "entry": {"mode": "on_signal"},
                         "exit": {"hold_days": 0}},
            "simulation": {"fill": "next_open"}}


def _ds(last_iso: str, close: float) -> dict:
    idx = pd.date_range(end=last_iso, periods=8, freq="B")
    v = [close] * 8
    return {SYM: pd.DataFrame(
        {"Open": v, "High": v, "Low": v, "Close": v}, index=idx)}


def _fill_all(t, broker, price: float, since: int) -> None:
    """since 이후 제출분 전량 체결 처리 + 해소."""
    for o in broker.submitted[since:]:
        broker.mark_filled(o["order_no"], o["qty"], price)
    t._resolve_pending([])


def _trades():
    from localapp import trader as tr
    if not Path(tr.TRADES_PATH).exists():
        return []
    out = []
    with open(tr.TRADES_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def test_section6_simulation_replay(isolated_trader, monkeypatch):
    """§6 전체 재현 — 순주문 1·6·3, phantom 0, 매도 0건, 최종 원장=브로커=롱5."""
    t, broker = isolated_trader
    spec = _spec()
    rate = spec.init_margin_rate or 0.10
    mult = spec.multiplier
    # 사이징: floor(amount / (가격×승수×증거금률)) — 300~304 가격대에서 5/6계약이 되게.
    a29 = int(5.5 * 304 * mult * rate)
    a27 = int(6.5 * 301 * mult * rate)
    strats = [
        {"id": "29", "name": "오버나이트롱", "definition": _def29(a29), "account_ref": None},
        {"id": "27", "name": "역추종", "definition": _def27(a27), "account_ref": None}]
    by = [{"strategy_id": "29", "candidates": [{"symbol": SYM}]},
          {"strategy_id": "27", "candidates": [{"symbol": SYM}]}]
    broker._balance.update({"cash": 1_000_000_000, "total_eval": 1_000_000_000,
                            "futures_order_cash_kr": 1_000_000_000})

    # ── D1 15:40 종가창 — #29 진입5 · 수동 롱4 선재(브로커) → 순매수 1 ──────────
    broker._prices[SYM] = 300.0
    broker.set_positions([{"symbol": SYM, "qty": 4, "side": "long", "avg_price": 300.0}])
    p1 = t.run_close_netting(by, strats, _ds("2026-05-29", 300.0),
                             market="KRX", instrument_class="futures", risk_limits={})
    orders1 = list(broker.submitted)
    assert [(o["side"], o["qty"]) for o in orders1] == [("buy", 1)], \
        f"§6 D1: 수동4 인수 → 순매수 1만 — 실제 {orders1}"
    assert p1["cycle_summary"]["n_drift"] == 0
    _fill_all(t, broker, 300.0, since=0)
    assert t.ledger["29"]["qty"] == 5 and t.ledger["29"]["entry_price"] == 300.0
    broker.set_positions([{"symbol": SYM, "qty": 5, "side": "long", "avg_price": 300.0}])

    # ── D2 — 날짜 전진(06-02) · 08:35 수동 매도5 → 브로커 0 ─────────────────────
    from localapp import trader as tr
    monkeypatch.setattr(tr, "kst_today", lambda: datetime.date(2026, 6, 2))
    monkeypatch.setattr(tr, "kst_now", lambda: datetime.datetime(
        2026, 6, 2, 8, 56, tzinfo=ZoneInfo("Asia/Seoul")))
    broker.set_positions([])
    broker._prices[SYM] = 301.0
    ds2 = _ds("2026-06-01", 301.0)

    # ── D2 08:55 아침 — #29 청산 · #27 진입6 → 순매수 6 (상쇄5+잔여1+복원5) ──────
    n_before = len(broker.submitted)
    p2 = t._cycle_body(strats, ds2, None, by, {}, "KRX")
    orders2 = broker.submitted[n_before:]
    assert all(o["side"] == "buy" for o in orders2), \
        f"§6 D2 아침: 매도 0건(order355 부류 소멸) — 실제 {orders2}"
    assert sum(o["qty"] for o in orders2) == 6, f"순매수 6 — 실제 {orders2}"
    cs2 = p2["cycle_summary"]
    assert cs2["n_netted"] == 5 and cs2["n_drift"] == 5
    _fill_all(t, broker, 301.0, since=n_before)
    assert "29" not in t.ledger, "#29 청산 완결(상쇄 5 — fresh 301 정산)"
    assert t.ledger["27"]["qty"] == 6 and t.ledger["27"]["entry_price"] == 301.0
    broker.set_positions([{"symbol": SYM, "qty": 6, "side": "long", "avg_price": 301.0}])

    # ── D2 낮 — 수동 매도3 · 매도1 → 브로커 2 ──────────────────────────────────
    broker.set_positions([{"symbol": SYM, "qty": 2, "side": "long", "avg_price": 301.0}])

    # ── D2 15:40 종가 — #27 청산 · #29 재진입5 → 순매수 3 (상쇄5 + drift 3) ─────
    broker._prices[SYM] = 304.0
    n_before = len(broker.submitted)
    p3 = t.run_close_netting(by, strats, ds2,
                             market="KRX", instrument_class="futures", risk_limits={})
    orders3 = broker.submitted[n_before:]
    assert all(o["side"] == "buy" for o in orders3), \
        f"§6 D2 종가: 매도 0건 — 실제 {orders3}"
    assert sum(o["qty"] for o in orders3) == 3, f"순매수 3 — 실제 {orders3}"
    cs3 = p3["cycle_summary"]
    assert cs3["n_netted"] == 5 and cs3["n_drift"] == 3
    _fill_all(t, broker, 304.0, since=n_before)
    broker.set_positions([{"symbol": SYM, "qty": 5, "side": "long", "avg_price": 304.0}])

    # ── 최종 검증 — §16 기대효과 ────────────────────────────────────────────────
    # E4/E5: 원장 = 브로커 = 롱5(#29) — drift 0·의도외 노출 0.
    assert set(t.ledger) == {"29"}
    assert t.ledger["29"]["qty"] == 5 and t.ledger["29"]["entry_price"] == 304.0
    # E2 phantom 0: 모든 체결·정산가가 이 시뮬의 fresh 참조가 집합에만 속한다.
    trades = _trades()
    prices = {float(ev.get("price") or 0) for ev in trades if ev.get("price")}
    assert prices <= {300.0, 301.0, 304.0}, f"phantom 가격 유입: {prices}"
    # 실현손익 = #29 (301−300)×5×mult + #27 (304−301)×6×mult — 전부 fresh 정산.
    realized = sum(float(ev.get("realized_pnl") or 0) for ev in trades)
    expected = (301.0 - 300.0) * 5 * mult + (304.0 - 301.0) * 6 * mult
    assert abs(realized - expected) < 1e-6, f"실현손익 {realized:,.0f} ≠ {expected:,.0f}"
    # E6: 사이클별 순주문 = 매수 1·6·3 (§6 표) — 위 개별 assert의 요약 재확인.
    assert [sum(o["qty"] for o in orders1),
            sum(o["qty"] for o in orders2),
            sum(o["qty"] for o in orders3)] == [1, 6, 3]


def _def_pct(pct: int = 100) -> dict:
    """선물 롱 — 시가 진입·pct_cash 사이징(model-A: orderable×사용률%가 병목)."""
    return {"name": "역추종", "engine": "ir", "query": "simulate",
            "universe": {"kind": "single", "symbols": [SYM]},
            "signal": _DUMMY_SIGNAL,
            "position": {"direction": "long",
                         "sizing": {"mode": "pct_cash", "amount_pct": pct},
                         "entry": {"mode": "on_signal"},
                         "exit": {"hold_days": 0}},
            "simulation": {"fill": "next_open"}}


def test_flat_capacity_sizing_manual_same_direction(isolated_trader):
    """§18 빈-상태 사이징 — 원장 밖 롱 2 보유 + 자동 롱 진입.

    orderable(추가 8) + 보유 되돌림 2 = 빈 상태 여력 10 → target 10 → net 매수 8
    (수동 2는 book으로 인수). 수정 전엔 크레딧 0 → target 8 → net 매수 6(과소)."""
    t, broker = isolated_trader
    broker._prices[SYM] = 300.0
    broker._balance["futures_order_cash_kr"] = 1_000_000_000
    broker.set_positions([{"symbol": SYM, "qty": 2, "side": "long", "avg_price": 300.0}])
    broker.orderable_qty = lambda symbol, side, price: 8   # 추가 8(= flat 10 − 보유 2)
    by = [{"strategy_id": "27", "candidates": [{"symbol": SYM}]}]
    strats = [{"id": "27", "name": "역추종", "definition": _def_pct(100),
               "account_ref": None}]
    payload = t._cycle_body(strats, _ds("2026-05-29", 300.0), None, by, {}, "KRX")
    buys = [o for o in broker.submitted if o["side"] == "buy"]
    assert sum(o["qty"] for o in buys) == 8, \
        f"빈-상태(10) − 보유(2) = 순매수 8이어야 — 실제 {[(o['side'],o['qty']) for o in broker.submitted]}"
    assert not [o for o in broker.submitted if o["side"] == "sell"]
    assert payload["cycle_summary"]["n_drift"] == 0     # 수동 2는 인수(book)·drift 아님
    # 관측(2026-07-20) — 흡수 사실이 명시돼야 한다. mwmw 07-20 조사에서 "브로커 4계약
    # 중 2는 무엇인가"를 넷팅 leg로 역산해야 했던 것의 근본 해소.
    cs = payload["cycle_summary"]
    assert cs["n_absorbed_external"] == 1
    absorbed = [d for d in payload["decisions"] if d["action"] == "absorbed_external"]
    assert len(absorbed) == 1 and absorbed[0]["symbol"] == SYM
    assert "2" in absorbed[0]["reason"]                 # 흡수 수량이 사유에 남음
    # 관측 — 목표 수량의 근거(브로커 원값 8 + 되돌림 크레딧 2 = 10, 100% → 목표 10)
    tr = cs["sizing"][f"{SYM}|long"]
    assert tr["orderable_raw"] == 8 and tr["credit"] == 2
    assert tr["capacity"] == 10 and tr["target"] == 10


def test_flat_capacity_manual_credit_applies_opposite_direction(isolated_trader):
    """§18.2 정정 — 원장 밖 **롱 2** 보유 + 자동 **숏** 진입(부호뒤집기)에도 빈-상태 크레딧.

    수동은 "전부 취소한 잔고 기준"(§18.1 유저 원칙)이라 진입 방향과 무관하게 되돌린다.
    근거(2026-07-16 LS 문서·실측 확정): CFOAQ10100은 신규(NewOrdAbleQty)와 청산
    (LqdtOrdAbleQty)을 분리 반환(예시 38=36+2)하고 우리는 신규만 읽는다 → 보유를 더해야
    빈-상태. 이중계상 아님. 신규 8 + 보유 2 = 10 → target 숏10 → net = −10 − (+2) = 매도 12.
    정정 전엔 크레딧 0 → target 숏8 → 매도 10(과소)."""
    t, broker = isolated_trader
    broker._prices[SYM] = 300.0
    broker._balance["futures_order_cash_kr"] = 1_000_000_000
    broker.set_positions([{"symbol": SYM, "qty": 2, "side": "long", "avg_price": 300.0}])
    captured = {}

    def _oq(symbol, side, price):       # side 기록 + 신규여력 8 반환(=flat 10 − 보유 2)
        captured[side] = price
        return 8

    broker.orderable_qty = _oq
    d = _def_pct(100)
    d["position"]["direction"] = "short"
    by = [{"strategy_id": "s", "candidates": [{"symbol": SYM, "direction": "short"}]}]
    strats = [{"id": "s", "name": "숏", "definition": d, "account_ref": None}]
    t._cycle_body(strats, _ds("2026-05-29", 300.0), None, by, {}, "KRX")
    assert "sell" in captured, "숏 진입은 orderable(sell)로 사이징"
    sells = [o for o in broker.submitted if o["side"] == "sell"]
    assert sum(o["qty"] for o in sells) == 12, \
        ("빈-상태(신규8+보유2=10) 숏 target − 롱보유(+2) = 매도 12여야 — 실제 "
         f"{[(o['side'], o['qty']) for o in broker.submitted]}")
    assert not [o for o in broker.submitted if o["side"] == "buy"]


def test_flat_capacity_manual_short_long_entry_mwmw_shape(isolated_trader):
    """§18.2 정정 — mwmw 2026-07-16 실측 형상 재현: 수동 **숏 4** + 자동 **롱** 진입 50%.

    실측: 신규매수 5(NewOrdAbleQty)·청산매수 4(LqdtOrdAbleQty) → 빈-상태 9.
    크레딧 미적용이던 정정 전: 5×50% = 롱2 → 매수 6 (실제로 이렇게 체결됐다).
    정정 후: (5+4)×50% = floor(4.5) = 롱4 → net = +4 − (−4) = 매수 8 (≤ 총 매수가능 9)."""
    t, broker = isolated_trader
    broker._prices[SYM] = 300.0
    broker._balance["futures_order_cash_kr"] = 1_000_000_000
    broker.set_positions([{"symbol": SYM, "qty": 4, "side": "short", "avg_price": 310.0}])
    broker.orderable_qty = lambda symbol, side, price: 5   # 신규 5(= flat 9 − 보유 4)
    d = _def_pct(100)
    d["position"]["sizing"] = {"mode": "equal_weight", "futures_margin_pct": 50.0}
    by = [{"strategy_id": "29", "candidates": [{"symbol": SYM, "direction": "long"}]}]
    strats = [{"id": "29", "name": "오버나이트", "definition": d, "account_ref": None}]
    t._cycle_body(strats, _ds("2026-05-29", 300.0), None, by, {}, "KRX")
    buys = [o for o in broker.submitted if o["side"] == "buy"]
    assert sum(o["qty"] for o in buys) == 8, \
        ("빈-상태(5+4=9)×50% = 롱4 target − 숏보유(−4) = 매수 8이어야 — 실제 "
         f"{[(o['side'], o['qty']) for o in broker.submitted]}")
    assert not [o for o in broker.submitted if o["side"] == "sell"]


def test_flat_capacity_manual_opposite_gated_on_unconfirmed_broker(isolated_trader):
    """§18.2 게이트 — orderable '신규 전용' **미확정** 브로커(KIS TTTO5105R 실측 전)는
    반대편 수동 크레딧을 같은-편 키로 강등(보류) → 크레딧 0의 v0.9.75 거동으로 안전 후퇴.

    합산(신규+청산) 의미일 가능성이 남은 브로커에 방향무관 크레딧을 더하면 orderable에
    이미 포함된 청산분과 이중계상(의도 초과 레버리지) — 실측 확정 시 플래그 한 줄로 해제."""
    t, broker = isolated_trader
    broker.orderable_new_only = lambda symbol: False           # 미확정 브로커 모델
    broker._prices[SYM] = 300.0
    broker._balance["futures_order_cash_kr"] = 1_000_000_000
    broker.set_positions([{"symbol": SYM, "qty": 4, "side": "short", "avg_price": 310.0}])
    broker.orderable_qty = lambda symbol, side, price: 5
    d = _def_pct(100)
    d["position"]["sizing"] = {"mode": "equal_weight", "futures_margin_pct": 50.0}
    by = [{"strategy_id": "29", "candidates": [{"symbol": SYM, "direction": "long"}]}]
    strats = [{"id": "29", "name": "오버나이트", "definition": d, "account_ref": None}]
    t._cycle_body(strats, _ds("2026-05-29", 300.0), None, by, {}, "KRX")
    buys = [o for o in broker.submitted if o["side"] == "buy"]
    assert sum(o["qty"] for o in buys) == 6, \
        ("게이트: 크레딧 미적용 5×50% = 롱2 target − 숏보유(−4) = 매수 6이어야 — 실제 "
         f"{[(o['side'], o['qty']) for o in broker.submitted]}")


def _reversal_day2_sells(t, broker, monkeypatch, new_only: bool) -> int:
    """갈아타기 하니스 — D1 #29 롱5 진입·체결 → D2 아침 #29 청산 + 숏(pct 100·
    orderable(sell)=8) 진입. 반환 = D2 총 매도 계약수(매수 0건은 내부 assert)."""
    spec = _spec()
    rate = spec.init_margin_rate or 0.10
    mult = spec.multiplier
    broker._balance.update({"cash": 1_000_000_000, "total_eval": 1_000_000_000,
                            "futures_order_cash_kr": 1_000_000_000})
    broker._prices[SYM] = 300.0
    a29 = int(5.5 * 300 * mult * rate)
    strats29 = [{"id": "29", "name": "오버나이트롱", "definition": _def29(a29),
                 "account_ref": None}]
    t.run_close_netting([{"strategy_id": "29", "candidates": [{"symbol": SYM}]}],
                        strats29, _ds("2026-05-29", 300.0),
                        market="KRX", instrument_class="futures", risk_limits={})
    _fill_all(t, broker, 300.0, since=0)
    assert t.ledger["29"]["qty"] == 5
    broker.set_positions([{"symbol": SYM, "qty": 5, "side": "long", "avg_price": 300.0}])

    from localapp import trader as tr
    monkeypatch.setattr(tr, "kst_today", lambda: datetime.date(2026, 6, 2))
    monkeypatch.setattr(tr, "kst_now", lambda: datetime.datetime(
        2026, 6, 2, 8, 56, tzinfo=ZoneInfo("Asia/Seoul")))
    broker._prices[SYM] = 301.0
    broker.orderable_qty = lambda symbol, side, price: 8       # 신규(sell) 8
    broker.orderable_new_only = lambda symbol: new_only
    d = _def_pct(100)
    d["position"]["direction"] = "short"
    n_before = len(broker.submitted)
    t._cycle_body(
        strats29 + [{"id": "s", "name": "숏", "definition": d, "account_ref": None}],
        _ds("2026-06-01", 301.0), None,
        [{"strategy_id": "s", "candidates": [{"symbol": SYM, "direction": "short"}]}],
        {}, "KRX")
    orders = broker.submitted[n_before:]
    assert not [o for o in orders if o["side"] == "buy"], \
        f"갈아타기: 매수 0건(단일 순방향)이어야 — 실제 {orders}"
    return sum(o["qty"] for o in orders if o["side"] == "sell")


def test_reversal_credit_applies_on_new_only_broker(isolated_trader, monkeypatch):
    """§18.2(2026-07-18 정정) 갈아타기 — 롱 청산분이 같은 사이클 **숏 진입** 사이징에 크레딧.

    신규 전용 확정 브로커(LS류): 숏 target = floor((orderable 8 + 청산크레딧 5)×100%) = 13
    → net = −13 − (+5) = **매도 18**(청산 5 + 숏 13·단일 순방향). 정정 전(같은-편 한정)엔
    크레딧 0 → 매도 13이었다."""
    t, broker = isolated_trader
    assert _reversal_day2_sells(t, broker, monkeypatch, new_only=True) == 18


def test_reversal_credit_gated_on_unconfirmed_broker(isolated_trader, monkeypatch):
    """§18.2 게이트 — 미확정 브로커(KIS 실측 전)는 갈아타기 크레딧 보류: 숏 target =
    floor(8×100%) = 8 → 매도 13(청산 5 + 숏 8). 실측 후 플래그 전환 시 위 테스트 거동."""
    t, broker = isolated_trader
    assert _reversal_day2_sells(t, broker, monkeypatch, new_only=False) == 13


def test_a1_external_pending_same_direction_nets(isolated_trader):
    """§19 A1 배선 E2E — 동시호가 창 수동 **미체결** 매수 4를 effective current로 반영.

    진입 target 5인데 수동 pending 매수 4가 있으면 자동은 **순매수 1**만(체결 후 수동4+자동1
    =5=target·과매수 방지). A1 없으면 매수 5 → 08:45에 4+5=9 과매수. 저널 비어 own 없음 →
    pending 전량 external. (§6 D1의 '수동 보유4' 버전을 '수동 미체결4'로 — 넷팅 결과 동일)."""
    t, broker = isolated_trader
    spec = _spec()
    rate = spec.init_margin_rate or 0.10
    mult = spec.multiplier
    broker._prices[SYM] = 300.0
    broker._balance.update({"cash": 1_000_000_000, "total_eval": 1_000_000_000,
                            "futures_order_cash_kr": 1_000_000_000})
    broker.orderable_qty = lambda symbol, side, price: 100      # 넉넉 — fixed_amount(5)가 바인딩
    broker.pending_orders = lambda: [
        {"order_no": "MANUAL1", "symbol": SYM, "side": "buy", "remain_qty": 4,
         "market": "DOMESTIC", "asset_class": "futures"}]
    a29 = int(5.5 * 300 * mult * rate)                         # → 진입 5계약
    strats = [{"id": "29", "name": "오버나이트롱", "definition": _def29(a29),
               "account_ref": None}]
    by = [{"strategy_id": "29", "candidates": [{"symbol": SYM}]}]
    p = t.run_close_netting(by, strats, _ds("2026-05-29", 300.0),
                            market="KRX", instrument_class="futures", risk_limits={})
    orders = [(o["side"], o["qty"]) for o in broker.submitted]
    assert orders == [("buy", 1)], \
        f"§19 A1: target5 − 수동pending4 = 순매수 1 — 실제 {orders}"
    assert p["cycle_summary"]["n_drift"] == 0        # 수동 pending은 인수(book)·drift 아님
    # 관측(2026-07-20) — A1 인수 결과가 **원격(서버 스냅샷)에서 보여야** 한다.
    # 종전엔 `[A1] 외부(수동) 미체결 반영: {...}` INFO 로그가 유일한 흔적이라
    # 유저 PC 없이는 "얼마를 인수했는지" 진단이 불가능했다.
    a1 = [d for d in p["decisions"] if d["action"] == "external_pending"]
    assert len(a1) == 1 and "+4" in a1[0]["reason"], \
        f"A1 인수 결정이 남아야 — 실제 {[d['action'] for d in p['decisions']]}"
    _fill_all(t, broker, 300.0, since=0)
    assert t.ledger["29"]["qty"] == 5                # 전략이 flat target 5 소유(수동4 인수+자동1)


def test_a1_query_failure_is_observable(isolated_trader):
    """A1 조회 실패는 **동작이 바뀌는** 사건 — 결정으로 표면화(종전 WARNING 로그뿐).

    실패 시 A1 인수를 건너뛰므로 창내 과매수가 가능하고 개장후 수렴이 백업한다.
    원격에서 이 분기를 구분할 수 없으면 "왜 과매수했나"를 진단할 수 없다."""
    t, broker = isolated_trader
    broker._prices[SYM] = 300.0
    broker._balance.update({"cash": 1_000_000_000, "total_eval": 1_000_000_000,
                            "futures_order_cash_kr": 1_000_000_000})
    broker.orderable_qty = lambda symbol, side, price: 100

    def _boom():
        raise RuntimeError("t0434 timeout")
    broker.pending_orders = _boom
    strats = [{"id": "29", "name": "오버나이트롱", "definition": _def29(300 * 250_000),
               "account_ref": None}]
    by = [{"strategy_id": "29", "candidates": [{"symbol": SYM}]}]
    p = t.run_close_netting(by, strats, _ds("2026-05-29", 300.0),
                            market="KRX", instrument_class="futures", risk_limits={})
    fail = [d for d in p["decisions"] if d["action"] == "external_pending_unavailable"]
    assert len(fail) == 1 and "t0434 timeout" in fail[0]["reason"]


def test_a1_opposite_pending_excluded_normal_order(isolated_trader):
    """§19 A1 — 반대 방향 수동 미체결(매도)은 A1에서 제외 → 자동은 정상 target 발주.

    진입 target 5, 수동 pending 매도 3(반대) → net = target 5(제외·불변). 반대방향은 동시호가
    자전·증거금 상충 위험이라 방향무관 08:52 수렴이 처리(여기선 08:35 정상 발주만 확인)."""
    t, broker = isolated_trader
    spec = _spec()
    rate = spec.init_margin_rate or 0.10
    mult = spec.multiplier
    broker._prices[SYM] = 300.0
    broker._balance.update({"cash": 1_000_000_000, "total_eval": 1_000_000_000,
                            "futures_order_cash_kr": 1_000_000_000})
    broker.orderable_qty = lambda symbol, side, price: 100
    broker.pending_orders = lambda: [
        {"order_no": "MANUAL2", "symbol": SYM, "side": "sell", "remain_qty": 3,
         "market": "DOMESTIC", "asset_class": "futures"}]
    a29 = int(5.5 * 300 * mult * rate)
    strats = [{"id": "29", "name": "오버나이트롱", "definition": _def29(a29),
               "account_ref": None}]
    by = [{"strategy_id": "29", "candidates": [{"symbol": SYM}]}]
    t.run_close_netting(by, strats, _ds("2026-05-29", 300.0),
                        market="KRX", instrument_class="futures", risk_limits={})
    orders = [(o["side"], o["qty"]) for o in broker.submitted]
    assert orders == [("buy", 5)], \
        f"§19 A1: 반대방향 pending 제외 → 정상 순매수 5 — 실제 {orders}"


def test_external_symbol_liquidated_by_drift(isolated_trader):
    """§9③ 비전략 심볼 청산 — 전략 무관 브로커 보유 → target 0 → drift 매도(원장 불변)."""
    t, broker = isolated_trader
    ext = "코스닥150선물"
    broker._prices[ext] = 900.0
    broker.set_positions([{"symbol": ext, "qty": 3, "side": "long", "avg_price": 900.0}])
    payload = t._cycle_body([], {}, None, [], {}, "KRX")
    sells = [o for o in broker.submitted if o["side"] == "sell"]
    assert [(o["symbol"], o["qty"]) for o in sells] == [(ext, 3)]
    assert payload["cycle_summary"]["n_drift"] == 3
    # 체결돼도 원장 불변(drift 플래그) + 표면화.
    broker.mark_filled(sells[0]["order_no"], 3, 900.0)
    decisions: list = []
    t._resolve_pending(decisions)
    assert t.ledger == {}
    assert any(d["action"] == "drift_corrected" for d in decisions)


def test_unparseable_position_held_not_zeroed(isolated_trader):
    """§13 목표 없음 ≠ 목표 0 — 파싱 불가 보유는 청산·복원 어느 쪽도 발주하지 않는다."""
    t, broker = isolated_trader
    broker._prices[SYM] = 300.0
    t.ledger["bad"] = {"symbol": SYM, "qty": 3, "side": "long", "entry_price": 300.0,
                       "peak_price": 300.0, "entry_date": "2026-05-29",
                       "strategy_name": "고아", "definition": {"garbage": True}}
    broker.set_positions([])          # 수동 전량 매도 상태 — 순진 구현이면 '복원 매수 3'
    payload = t._cycle_body([], _ds("2026-05-29", 300.0), None, [], {}, "KRX")
    assert broker.submitted == [], "판정불가 심볼은 이번 사이클 수렴 자체를 보류(hold)"
    assert any(d["action"] == "unparseable_orphan" for d in payload["decisions"])
    assert t.ledger["bad"]["qty"] == 3


def test_exit_without_ref_price_held(isolated_trader):
    """§13 — 청산 due인데 참조가 부재(stale 번들 + live 0) → 0으로 오인 청산 금지(hold)."""
    t, broker = isolated_trader
    broker._prices[SYM] = 0.0                       # live 없음
    spec = _spec()
    t.ledger["29"] = {"symbol": SYM, "qty": 5, "side": "long", "entry_price": 300.0,
                      "peak_price": 300.0, "entry_date": "2026-05-29",
                      "strategy_name": "오버나이트롱",
                      "definition": _def29(int(5.5 * 304 * spec.multiplier
                                               * (spec.init_margin_rate or 0.1)))}
    broker.set_positions([{"symbol": SYM, "qty": 5, "side": "long", "avg_price": 300.0}])
    stale = _ds("2026-05-20", 1210.5)               # 07-14형 stale 봉
    payload = t._cycle_body([], stale, None, [], {}, "KRX")
    assert broker.submitted == [], "참조가 없는 청산은 phantom 정산 위험 — hold"
    assert t.ledger["29"]["qty"] == 5
    # stale 가격(1210.5)이 어떤 기록에도 유입되지 않는다(E1·E2).
    assert all(float(ev.get("price") or 0) != 1210.5 for ev in _trades())


def test_inflight_exit_rerun_no_double_order(isolated_trader):
    """§14·§17.1 — 청산 주문 in-flight 중 사이클 재실행(08:40/42·catchup)이 이중
    발주하지 않는다: 활성 intent가 심볼을 hold시키고 drift 산술도 안 돈다."""
    t, broker = isolated_trader
    spec = _spec()
    broker._prices[SYM] = 300.0
    a = int(5.5 * 304 * spec.multiplier * (spec.init_margin_rate or 0.1))
    t.ledger["29"] = {"symbol": SYM, "qty": 5, "side": "long", "entry_price": 300.0,
                      "peak_price": 300.0, "entry_date": "2026-05-29",
                      "strategy_name": "오버나이트롱", "definition": _def29(a)}
    broker.set_positions([{"symbol": SYM, "qty": 5, "side": "long",
                           "avg_price": 300.0}])
    ds = _ds("2026-05-29", 300.0)
    # 1차 사이클 — 청산 매도 5 발주(미체결 유지)
    t._cycle_body([], ds, None, [], {}, "KRX")
    first = [(o["side"], o["qty"]) for o in broker.submitted]
    assert first == [("sell", 5)]
    # 2차 재실행(창내 재시도/catchup) — intent 활성 → 심볼 hold → 추가 발주 0
    t._cycle_body([], ds, None, [], {}, "KRX")
    assert [(o["side"], o["qty"]) for o in broker.submitted] == first, \
        "in-flight 재실행이 이중 발주하면 안 됨"
    assert t.ledger["29"]["qty"] == 5


def test_morning_futures_scope_excludes_stocks(isolated_trader):
    """문제 10(§15.3-⑥) — 08:35 선물 사이클은 주식을 건드리지 않는다(스코프 가드).

    주식 보유가 수동매도로 drift돼 있어도(원장 5 vs 브로커 0) 선물 사이클은
    복원·청산 어느 쪽도 발주하지 않는다 — 주식은 08:55 주식 사이클 소관."""
    t, broker = isolated_trader
    broker._prices["005930"] = 70000.0
    t.ledger["s1"] = {"symbol": "005930", "qty": 5, "side": "long",
                      "entry_price": 70000.0, "peak_price": 70000.0,
                      "entry_date": "2026-05-29", "strategy_name": "주식전략",
                      "definition": {"name": "s", "engine": "ir", "query": "simulate",
                                     "universe": {"kind": "single", "symbols": ["005930"]},
                                     "signal": _DUMMY_SIGNAL,
                                     "position": {"direction": "long",
                                                  "sizing": {"mode": "pct_cash",
                                                             "amount_pct": 10},
                                                  "entry": {"mode": "on_signal"},
                                                  "exit": {"hold_days": 30}},
                                     "simulation": {"fill": "next_open"}}}
    broker.set_positions([])          # 주식 수동 전량 매도 상태
    idx = pd.date_range(end="2026-05-29", periods=8, freq="B")
    ds = {"005930": pd.DataFrame({"Open": [70000.0] * 8, "High": [70000.0] * 8,
                                  "Low": [70000.0] * 8, "Close": [70000.0] * 8},
                                 index=idx)}
    t._cycle_body([], ds, None, [], {}, "KRX", instrument_class="futures")
    assert broker.submitted == [], "선물 스코프 사이클이 주식을 발주하면 안 됨"
    assert t.ledger["s1"]["qty"] == 5


def test_fetch_failed_holds_held_symbols(isolated_trader):
    """§14 가용성 가드 — 잔고 부분조회 시 보유 심볼 수렴 보류(오버셀·오인 청산 방지)."""
    t, broker = isolated_trader
    spec = _spec()
    broker._prices[SYM] = 300.0
    broker._balance["fetch_failed"] = ["futures"]
    t.ledger["29"] = {"symbol": SYM, "qty": 5, "side": "long", "entry_price": 300.0,
                      "peak_price": 300.0, "entry_date": "2026-06-01",
                      "strategy_name": "오버나이트롱",
                      "definition": _def29(int(5.5 * 304 * spec.multiplier
                                               * (spec.init_margin_rate or 0.1)))}
    broker.set_positions([])                        # 스냅샷 자체가 불완전
    payload = t._cycle_body([], _ds("2026-05-29", 300.0), None, [], {}, "KRX")
    assert broker.submitted == [], "부분 스냅샷으로 drift 교정/청산 금지"
    assert t.ledger["29"]["qty"] == 5
    assert any(d["action"] == "drift_eval_skipped" for d in payload["decisions"])

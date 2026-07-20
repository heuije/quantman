"""R2·R5 — 발주 부정종결 판정 수리 + 장부 lost-update 직렬화.

1. 라우터 pending 병합(주식+선물·계약코드 정규화) — §19 A1·broker_pending 맹점.
2. 공개 seam(daily_orders_today)으로 intents reconcile이 라우터 관통(종전 dead).
3. no_fill 나이 하한(CY-2) — 방금 제출분을 미접수로 오확정해 이중발주 켜지 않게.
4. DAY 만료 익일 회수(R2-②) — 제출일 체결내역 확인 후에만 종결·지각 체결은 기장.
5. R5 — 사이클 락 확보 직후 reload_state(lost-update 창 제거)·업데이트 게이트
   cycle_in_flight.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest

from localapp import intents, updater
from localapp.broker_router import BrokerRouter
from localapp.trader import Trader

_KST = ZoneInfo("Asia/Seoul")


# ── 1. 라우터 pending 병합 ───────────────────────────────────────────────────


class _StockB:
    def pending_orders(self):
        return [{"order_no": "1", "symbol": "005930", "asset_class": "stock"}]

    def daily_orders_today(self):
        return [{"odno": "1", "pdno": "005930"}]


class _FutB:
    def pending_orders(self):
        return [{"order_no": "9", "symbol": "101W9000", "asset_class": "futures"}]


def _router(stock=None, fut=None):
    return BrokerRouter(stock, fut, resolve=lambda s: None,
                        dataset_for_code=lambda c: "코스피200선물" if c == "101W9000" else None)


def test_router_merges_stock_and_futures_pending():
    out = _router(_StockB(), _FutB()).pending_orders()
    assert {o["order_no"] for o in out} == {"1", "9"}
    fut = next(o for o in out if o["order_no"] == "9")
    assert fut["symbol"] == "코스피200선물"          # 계약코드 → 데이터셋 심볼 정규화


def test_router_one_side_failure_keeps_other():
    class _BadStock:
        def pending_orders(self):
            raise RuntimeError("kis down")

    out = _router(_BadStock(), _FutB()).pending_orders()
    assert [o["order_no"] for o in out] == ["9"]


def test_router_delegates_public_seam():
    """공개 이름은 __getattr__로 stock 위임 — reconcile이 라우터를 관통(R2-③)."""
    r = _router(_StockB(), _FutB())
    assert hasattr(r, "daily_orders_today")
    assert r.daily_orders_today()[0]["odno"] == "1"


# ── 1-b. 라우터 fills_on — 심볼 라우팅 + 미지원(None) 계약 ────────────────────


def test_router_fills_on_routes_by_symbol():
    """__getattr__(stock 우선) 위임이면 선물 pending을 KIS 일별체결에서 찾아 오종결한다."""
    seen = []

    class _S:
        def fills_on(self, ymd, symbol=None):
            seen.append(("stock", ymd, symbol))
            return [{"odno": "1", "filled_qty": 1, "fill_price": 70000.0}]

    class _F:
        def fills_on(self, ymd, symbol=None):
            seen.append(("fut", ymd, symbol))
            return [{"odno": "9", "filled_qty": 2, "fill_price": 1102.5}]

    r = _router(_S(), _F())
    assert r.fills_on("20260720", "코스피200선물")[0]["odno"] == "9"
    assert r.fills_on("20260720", "005930")[0]["odno"] == "1"
    assert seen == [("fut", "20260720", "코스피200선물"),
                    ("stock", "20260720", "005930")]     # symbol 그대로 전달


def test_router_fills_on_none_when_broker_unsupported():
    """미지원(KIS 선물처럼 fills_on 부재) → None. 빈 리스트(지원+무내역)와 반드시 구분."""
    class _S:
        def fills_on(self, ymd, symbol=None):
            return []

    r = _router(_S(), _FutB())                            # _FutB엔 fills_on 없음
    assert r.fills_on("20260720", "코스피200선물") is None
    assert r.fills_on("20260720", "005930") == []


def test_router_fills_on_none_for_overseas_futures():
    """해외선물(CME)은 None — LS fills_on은 국내(CFOAQ00600) 전용이라 항상 무흔적 오종결."""
    class _F:
        def fills_on(self, ymd, symbol=None):
            raise AssertionError("CME는 국내 체결조회로 라우팅되면 안 된다")

    assert _router(None, _F()).fills_on("20260720", "금선물") is None


# ── 2·3. intents reconcile — 공개 seam + no_fill 나이 하한 ──────────────────


def _seed(ts_iso: str, iid: str = "i1"):
    return {"ts": ts_iso, "date": "2026-07-18", "intent_id": iid,
            "strategy_id": "s", "strategy_name": "n", "symbol": "005930",
            "side": "buy", "qty": 1, "ref_price": 100.0}


def test_reconcile_uses_public_seam_and_age_floor(monkeypatch, tmp_path):
    """0-매칭이라도 intent가 60초 미만이면 too_fresh(게이트 유지) — 이중발주 방지."""
    fresh = _seed(datetime.now(_KST).isoformat())
    monkeypatch.setattr(intents, "list_submitting_today", lambda d, path=None: [fresh])
    marks = []
    monkeypatch.setattr(intents, "mark_failed",
                        lambda d, i, r, path=None: marks.append(("failed", i)))
    monkeypatch.setattr(intents, "mark_submitted",
                        lambda d, i, o, path=None: marks.append(("submitted", i)))

    class _B:
        def daily_orders_today(self):
            return []                      # 무흔적

    r = intents.reconcile_submitting(_B(), "2026-07-18", path=tmp_path / "x.jsonl")
    assert r["no_fill"] == 0 and not marks
    assert r["details"][0]["outcome"] == "too_fresh"


def test_reconcile_no_fill_after_age_floor(monkeypatch, tmp_path):
    old = _seed((datetime.now(_KST) - timedelta(minutes=10)).isoformat())
    monkeypatch.setattr(intents, "list_submitting_today", lambda d, path=None: [old])
    marks = []
    monkeypatch.setattr(intents, "mark_failed",
                        lambda d, i, r, path=None: marks.append(("failed", i)))

    class _B:
        def daily_orders_today(self):
            return []

    r = intents.reconcile_submitting(_B(), "2026-07-18", path=tmp_path / "x.jsonl")
    assert r["no_fill"] == 1 and marks == [("failed", "i1")]


def test_reconcile_skips_without_seam(monkeypatch, tmp_path):
    """seam 미지원(LS 등) — 종전대로 보수적 skip(게이트 유지)."""
    old = _seed((datetime.now(_KST) - timedelta(minutes=10)).isoformat())
    monkeypatch.setattr(intents, "list_submitting_today", lambda d, path=None: [old])

    class _NoSeam:
        pass

    r = intents.reconcile_submitting(_NoSeam(), "2026-07-18",
                                     path=tmp_path / "x.jsonl")
    assert r["kis_query_failed"] == 1


# ── 4. DAY 만료 익일 회수 ────────────────────────────────────────────────────


def _pending(symbol="005930", days_ago=1.2):
    return {"order_no": "42", "strategy_id": "s", "strategy_name": "n",
            "symbol": symbol, "side": "buy", "qty": 3, "filled_so_far": 0,
            "submitted_ts": time.time() - days_ago * 86400,
            "intended_price": 100.0, "limit_price": 100.0, "kind": "진입"}


def _trader_with(broker):
    t = Trader(broker)
    t.pending = {"42": _pending()}
    return t


def test_reclaim_no_trace_finalizes(monkeypatch):
    class _B:
        def fills_on(self, ymd, symbol=None):
            return []                     # 제출일 체결내역 무흔적

    t = _trader_with(_B())
    decisions: list = []
    ok = t._reclaim_expired_pending("42", t.pending["42"],
                                     date.today() - timedelta(days=1), decisions)
    assert ok and "42" not in t.pending
    assert decisions and "DAY 만료 확정" in decisions[0]["reason"]


def test_reclaim_late_fill_is_booked(monkeypatch):
    class _B:
        def fills_on(self, ymd, symbol=None):
            return [{"odno": "42", "filled_qty": 3, "fill_price": 101.0,
                     "cancelled": False}]

    t = _trader_with(_B())
    booked = []
    monkeypatch.setattr(t, "_apply_fill",
                        lambda order_no, p, qty, px, dec, **k:
                        booked.append((order_no, qty, px)))
    ok = t._reclaim_expired_pending("42", t.pending["42"],
                                     date.today() - timedelta(days=1), [])
    assert ok and "42" not in t.pending
    assert booked == [("42", 3, 101.0)]   # 지각 체결이 기장됨(미기장 실손 차단)


def test_reclaim_out_of_scope_returns_false():
    class _B:
        pass                              # fills_on 미지원

    t = _trader_with(_B())
    assert t._reclaim_expired_pending("42", t.pending["42"],
                                       date.today() - timedelta(days=1), []) is False
    # 선물 심볼도 브로커가 미지원이면 동일(7일 GC 백스톱 유지)
    t2 = _trader_with(_B())
    t2.pending["42"]["symbol"] = "코스피200선물"
    assert t2._reclaim_expired_pending("42", t2.pending["42"],
                                        date.today() - timedelta(days=1), []) is False


# ── 4-b. 선물 익일 회수 — LS CFOAQ00600 어휘 실측(2026-07-20) 후 범위 편입 ──────


def _futures_trader(broker):
    t = _trader_with(broker)
    t.pending["42"]["symbol"] = "코스피200선물"
    return t


def test_reclaim_futures_late_fill_is_booked(monkeypatch):
    """선물 지각 체결도 기장 후 종결 — 미기장 체결이 drift 되팔기로 새던 실손 차단."""
    class _B:
        def fills_on(self, ymd, symbol=None):
            return [{"odno": "42", "filled_qty": 3, "fill_price": 1102.5}]

    t = _futures_trader(_B())
    booked = []
    monkeypatch.setattr(t, "_apply_fill",
                        lambda order_no, p, qty, px, dec, **k:
                        booked.append((order_no, qty, px)))
    ok = t._reclaim_expired_pending("42", t.pending["42"],
                                     date.today() - timedelta(days=1), [])
    assert ok and "42" not in t.pending
    assert booked == [("42", 3, 1102.5)]


def test_reclaim_futures_no_trace_finalizes():
    """선물 좀비 pending(무흔적)은 익일 확정 종결 — 7일 GC까지 잔존하던 부류 해소."""
    class _B:
        def fills_on(self, ymd, symbol=None):
            return []

    t = _futures_trader(_B())
    decisions: list = []
    ok = t._reclaim_expired_pending("42", t.pending["42"],
                                     date.today() - timedelta(days=1), decisions)
    assert ok and "42" not in t.pending
    assert decisions and "DAY 만료 확정" in decisions[0]["reason"]


def test_reclaim_passes_symbol_to_fills_on():
    """symbol을 넘겨야 라우터가 선물 브로커로 라우팅한다(안 넘기면 KIS 오조회)."""
    seen = {}

    class _B:
        def fills_on(self, ymd, symbol=None):
            seen.update(ymd=ymd, symbol=symbol)
            return []

    t = _futures_trader(_B())
    sub_day = date.today() - timedelta(days=1)
    t._reclaim_expired_pending("42", t.pending["42"], sub_day, [])
    assert seen == {"ymd": sub_day.strftime("%Y%m%d"), "symbol": "코스피200선물"}


def test_reclaim_none_result_keeps_gc_backstop():
    """라우터 None(미지원) → 종결 금지. 빈 리스트(무체결 확정)로 읽으면 오종결 실손."""
    class _B:
        def fills_on(self, ymd, symbol=None):
            return None

    t = _futures_trader(_B())
    assert t._reclaim_expired_pending("42", t.pending["42"],
                                       date.today() - timedelta(days=1), []) is False
    assert "42" in t.pending          # 추적 유지 — 7일 GC가 최후 방어


# ── 5. R5 — 락 안 reload + 업데이트 게이트 ──────────────────────────────────


def test_cycle_reloads_state_inside_lock(monkeypatch):
    class _B:
        pass

    t = Trader(_B())
    calls = []
    monkeypatch.setattr(t, "reload_state", lambda: calls.append("reload"))
    monkeypatch.setattr(t, "_cycle_body",
                        lambda *a, **k: calls.append("body") or {"ok": True})
    out = t.cycle([], {}, buy_candidates=[], risk_limits={}, market="KRX")
    assert out == {"ok": True}
    assert calls == ["reload", "body"]    # reload가 본문보다 먼저(락 안)


def test_update_window_blocks_cycle_in_flight():
    now = datetime(2026, 7, 18, 17, 0, tzinfo=_KST)
    safe, reason = updater.is_safe_update_window(
        now, intraday_running=False, pending_count=0, cycle_in_flight=True)
    assert safe is False and "사이클" in reason
    safe2, _ = updater.is_safe_update_window(
        now, intraday_running=False, pending_count=0, cycle_in_flight=False)
    assert safe2 is True

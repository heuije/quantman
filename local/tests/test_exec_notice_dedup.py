"""L-09 회귀 — KIS H0STCNI0 체결 통보 중복 dedup.

KIS가 같은 체결 이벤트를 두 번 보내면 ledger qty가 이중 가산되어 over-position
이 된다. (STCK_CNTG_HOUR, CNTG_QTY, CNTG_UNPR) 3-tuple로 dedup.

검증:
1. 동일 이벤트 두 번 도달 시 _apply_fill이 1회만 호출됨
2. 부분 체결 두 건이 시각이 다르면 정상 누적
3. dedup_key가 pending[order_no]에 영속(json 직렬화 호환)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))


def _make_trader_with_pending(order_no: str = "00012345"):
    """trader mock — pending에 buy 주문 1개, _apply_fill 호출 추적."""
    trader = MagicMock()
    trader.pending = {
        order_no: {
            "symbol": "005930", "side": "buy", "qty": 10,
            "filled_so_far": 0,
            "strategy_name": "T",
            "ledger_key": "T:005930",
        }
    }
    apply_calls = []

    def _apply_fill(o_no, p, filled_qty, fill_price, decisions, partial):
        apply_calls.append({
            "order_no": o_no, "filled_qty": filled_qty,
            "fill_price": fill_price, "partial": partial,
        })

    trader._apply_fill.side_effect = _apply_fill
    return trader, apply_calls


def _broker_mock():
    b = MagicMock()
    b.account_snapshot.return_value = {"balance": {}, "positions": []}
    return b


def _evt(qty: int, price: float, hour: str = "100530",
         order_no: str = "00012345", cntg_yn: str = "2") -> dict:
    """H0STCNI0 체결 이벤트 mock."""
    return {
        "CNTG_YN": cntg_yn,
        "ODER_NO": order_no,
        "STCK_SHRN_ISCD": "005930",
        "CNTG_QTY": str(qty),
        "CNTG_UNPR": str(price),
        "STCK_CNTG_HOUR": hour,
        "RFUS_YN": "",
    }


def test_dedup_blocks_duplicate_event(monkeypatch):
    """같은 evt 두 번 도달 → _apply_fill 1회만 호출."""
    from localapp import intraday_loop
    # push_snapshot 더미화 (서버 호출 회피)
    monkeypatch.setattr(intraday_loop, "push_snapshot",
                         lambda *a, **kw: None)
    trader, apply_calls = _make_trader_with_pending()
    broker = _broker_mock()
    evt = _evt(qty=10, price=72000.0)

    intraday_loop._on_exec_event(trader, broker, evt)
    intraday_loop._on_exec_event(trader, broker, evt)  # 같은 이벤트 두 번

    assert len(apply_calls) == 1, "중복 이벤트가 dedup되지 않음"


def test_partial_fills_with_different_times_accumulate(monkeypatch):
    """시각이 다른 부분 체결 두 건은 정상 누적 (둘 다 적용)."""
    from localapp import intraday_loop
    monkeypatch.setattr(intraday_loop, "push_snapshot",
                         lambda *a, **kw: None)
    trader, apply_calls = _make_trader_with_pending()
    broker = _broker_mock()

    # 1차 부분 체결 5주 @ 10:05:30
    intraday_loop._on_exec_event(
        trader, broker, _evt(qty=5, price=72000.0, hour="100530"))
    # filled_so_far 갱신 시뮬레이션 (실제 _apply_fill이 처리하는 부분)
    trader.pending["00012345"]["filled_so_far"] = 5
    # 2차 부분 체결 5주 @ 10:05:45 (시각 다름)
    intraday_loop._on_exec_event(
        trader, broker, _evt(qty=5, price=72000.0, hour="100545"))

    assert len(apply_calls) == 2, "정상 부분 체결이 누적 안 됨"
    assert apply_calls[0]["partial"] is True
    # 두 번째는 전량 도달 → partial=False
    assert apply_calls[1]["partial"] is False


def test_same_time_same_qty_same_price_treated_as_duplicate(monkeypatch):
    """같은 (시각, 수량, 가격) → 진짜 중복으로 판정."""
    from localapp import intraday_loop
    monkeypatch.setattr(intraday_loop, "push_snapshot",
                         lambda *a, **kw: None)
    trader, apply_calls = _make_trader_with_pending()
    broker = _broker_mock()

    evt1 = _evt(qty=5, price=72000.0, hour="100530")
    evt2 = _evt(qty=5, price=72000.0, hour="100530")  # 동일

    intraday_loop._on_exec_event(trader, broker, evt1)
    intraday_loop._on_exec_event(trader, broker, evt2)

    assert len(apply_calls) == 1


def test_different_price_not_deduped(monkeypatch):
    """같은 시각·수량이라도 가격이 다르면 별개 체결로 인정."""
    from localapp import intraday_loop
    monkeypatch.setattr(intraday_loop, "push_snapshot",
                         lambda *a, **kw: None)
    trader, apply_calls = _make_trader_with_pending()
    broker = _broker_mock()

    intraday_loop._on_exec_event(
        trader, broker, _evt(qty=5, price=72000.0, hour="100530"))
    trader.pending["00012345"]["filled_so_far"] = 5
    intraday_loop._on_exec_event(
        trader, broker, _evt(qty=5, price=72100.0, hour="100530"))

    assert len(apply_calls) == 2


def test_dedup_key_persisted_in_pending(monkeypatch):
    """dedup_key가 pending[order_no]['_dedup_keys']에 list로 저장 (json 호환)."""
    from localapp import intraday_loop
    monkeypatch.setattr(intraday_loop, "push_snapshot",
                         lambda *a, **kw: None)
    trader, _ = _make_trader_with_pending()
    broker = _broker_mock()
    intraday_loop._on_exec_event(
        trader, broker, _evt(qty=5, price=72000.0, hour="100530"))

    p = trader.pending["00012345"]
    assert "_dedup_keys" in p
    assert isinstance(p["_dedup_keys"], list)
    assert p["_dedup_keys"][0] == ["100530", 5, 72000.0]


def test_acceptance_notice_not_deduped(monkeypatch):
    """접수 통보(CNTG_YN='1')는 체결 아니므로 dedup 분기 진입 전 return."""
    from localapp import intraday_loop
    monkeypatch.setattr(intraday_loop, "push_snapshot",
                         lambda *a, **kw: None)
    trader, apply_calls = _make_trader_with_pending()
    broker = _broker_mock()

    intraday_loop._on_exec_event(
        trader, broker, _evt(qty=10, price=72000.0, cntg_yn="1"))

    assert apply_calls == []
    # _dedup_keys도 안 생김 — 접수 통보는 dedup 대상이 아님
    assert "_dedup_keys" not in trader.pending["00012345"]

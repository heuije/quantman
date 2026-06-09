"""구조적 회귀 — 비상 전량 청산(LIQUIDATE_ALL)은 일반 cycle과 독립이어야 한다.

근본 결함(이 테스트가 막는 것): 비상 청산이 run_cycle을 재사용해 dataset 번들 다운로드·
전략 파싱·preview·단일 시장 스코프에 의존 → 다운로드 hang·파싱 실패·장마감 중 하나라도
걸리면 비상정지 전체가 막혔다(실측: dataset 다운로드에서 hang, 주문 0건).

재설계 계약(Trader.liquidate_all_held):
- 청산 대상 = **브로커 계좌 실보유**(account_snapshot) — ledger/전략/dataset 무관.
  전략이 삭제된 고아·외부 매수분도 보유면 청산된다.
- dataset 인자 없음(신호 평가 불필요 — 보유 수량 전량 매도).
- 시장 스코프 없음 — KR·US·선물 보유 전체를 한 번에.
- 시세는 KIS 현재가, 없으면 시장가 주문(가격 의존 제거).
- 롱=매도, 숏=환매(buy-to-close).
"""

from __future__ import annotations

import inspect


def test_liquidates_all_broker_held_without_ledger_or_dataset(isolated_trader):
    """원장·전략·dataset이 전혀 없어도 브로커 실보유 전량을 청산(구조적 핵심)."""
    trader, broker = isolated_trader
    broker.set_positions([
        {"symbol": "005930", "qty": 10, "avg_price": 70000},   # KR — 원장에 없음(고아/외부)
        {"symbol": "AAPL", "qty": 5, "avg_price": 200},        # US — 원장에 없음
    ])
    broker._prices.update({"005930": 71000.0, "AAPL": 210.0})
    assert trader.ledger == {}                                  # 전략/원장 전무

    payload = trader.liquidate_all_held()

    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert {s["symbol"] for s in sells} == {"005930", "AAPL"}   # 보유 전량 청산
    assert {s["symbol"]: s["qty"] for s in sells} == {"005930": 10, "AAPL": 5}
    assert payload["cycle_summary"]["kind"] == "emergency_liquidation"


def test_signature_has_no_dataset_dependency():
    """dataset/preview/strategies 인자가 없어야 한다(의존 제거를 시그니처로 보장)."""
    from localapp.trader import Trader
    params = set(inspect.signature(Trader.liquidate_all_held).parameters) - {"self"}
    assert params == set(), f"비상 청산은 무인자여야 함(외부 의존 0): {params}"


def test_no_price_falls_back_to_market_order(isolated_trader):
    """시세 조회 실패(0)면 시장가 주문(limit=0) — 가격 못 구해도 청산 시도."""
    trader, broker = isolated_trader
    broker.set_positions([{"symbol": "005930", "qty": 3, "avg_price": 70000}])
    # _prices 비움 → broker.price()=0 → _safe_price None → 시장가
    trader.liquidate_all_held()
    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["limit"] == 0                               # 시장가(지정가 아님)


def test_short_position_buys_to_close(isolated_trader):
    """숏 보유는 환매(buy-to-close)로 청산."""
    trader, broker = isolated_trader
    broker.set_positions([
        {"symbol": "005930", "qty": 4, "side": "long", "avg_price": 70000},
        {"symbol": "101W09", "qty": 2, "side": "short", "avg_price": 300},
    ])
    broker._prices.update({"005930": 71000.0, "101W09": 310.0})
    trader.liquidate_all_held()
    sells = [s for s in broker.submitted if s["side"] == "sell"]
    buys = [s for s in broker.submitted if s["side"] == "buy"]
    assert any(s["symbol"] == "005930" and s["qty"] == 4 for s in sells)  # 롱 매도
    assert any(b["symbol"] == "101W09" and b["qty"] == 2 for b in buys)   # 숏 환매


def test_skips_zero_qty_and_empty_holdings(isolated_trader):
    """보유 0/빈 계좌면 주문 0건 — 예외 없이 정상 종료."""
    trader, broker = isolated_trader
    broker.set_positions([{"symbol": "005930", "qty": 0, "avg_price": 70000}])
    payload = trader.liquidate_all_held()
    assert [s for s in broker.submitted if s["side"] in ("buy", "sell")] == []
    assert payload["cycle_summary"]["kind"] == "emergency_liquidation"


def test_state_snapshot_reflects_killswitch_without_trading(isolated_trader):
    """state_snapshot은 거래 없이 현 kill-switch 상태를 반영(웹 실시간 동기화용)."""
    from localapp import killswitch
    trader, broker = isolated_trader
    broker.set_positions([{"symbol": "005930", "qty": 5, "avg_price": 70000}])

    killswitch.activate("test")
    snap = trader.state_snapshot()
    assert snap["cycle_summary"]["kind"] == "state_sync"
    assert snap["cycle_summary"]["kill_switch"] is True
    assert snap["kill_switch"]["active"] is True
    assert [s for s in broker.submitted if s["side"] in ("buy", "sell")] == []  # 거래 없음

    killswitch.reset()
    snap2 = trader.state_snapshot()
    assert snap2["cycle_summary"]["kill_switch"] is False
    assert snap2["kill_switch"]["active"] is False
    assert snap2["positions"]                              # 잔고·포지션은 그대로 실림

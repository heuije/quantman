"""국내 발주 = 시장가 단일 (지정가 옵션 제거 후 회귀).

배경: 국내(주식·선물)는 동시호가 단일가에 시장가로 체결돼 지정가 대비 슬리피지
손해가 없고, IR빌더가 지정가 *가격*을 표현할 수단이 없어 지정가 옵션이 dead+청산
footgun이었다 → 제거. 미국주식만 KIS 제약으로 지정가(예약) 유지.

계약: 국내 진입/청산/숏은 시장가(broker.buy/sell, limit=0). 거부(success=False)는
주문 미생성이므로 intent를 failed로 마감해 멱등 게이트를 풀어 당일 재시도를 허용(D5-5).
"""

from __future__ import annotations

from quant_core.exec_defaults import merged_execution


def _policy():
    return dict(merged_execution(None))


def test_krx_buy_is_market_order(isolated_trader):
    """국내 즉시 매수 = 시장가(broker.buy, limit=0) — 가격 미지정."""
    trader, broker = isolated_trader
    decisions: list[dict] = []
    trader._submit_buy("s1", "전략", {}, "005930", 1, 70000.0, _policy(), decisions)
    buys = [s for s in broker.submitted if s["side"] == "buy"]
    assert len(buys) == 1
    assert buys[0]["limit"] == 0, f"국내 매수가 지정가로 나감: {buys[0]['limit']}"


def test_krx_sell_is_market_order(isolated_trader):
    """국내 즉시 매도 = 시장가(broker.sell, limit=0)."""
    trader, broker = isolated_trader
    decisions: list[dict] = []
    trader._submit_sell("s2", "전략", "005930", 1, 70000.0, _policy(), "청산", decisions)
    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["limit"] == 0, f"국내 매도가 지정가로 나감: {sells[0]['limit']}"


def test_futures_short_entry_close_are_market(isolated_trader):
    """선물 숏 진입/환매도 시장가(limit=0)."""
    trader, broker = isolated_trader
    decisions: list[dict] = []
    trader._submit_open_short("s3", "양방향", {}, "코스피200선물", 1, 420.0,
                              _policy(), decisions)
    trader._submit_close_short("s3", "양방향", "코스피200선물", 1, 420.0,
                               _policy(), "청산", decisions)
    assert [s["limit"] for s in broker.submitted] == [0, 0]


def test_rejected_order_releases_intent_for_retry(isolated_trader, monkeypatch):
    """KIS 거부(주문 미생성) → intent failed 마감 → 멱등 게이트 해제 (D5-5).

    이전: 거부돼도 'submitted'로 남아 같은 날 동일 (전략,종목,side) 재시도가 전부
    차단(06-09 장종료 거부가 그날 재시도 불가였던 메커니즘). 지금은 거부 후 게이트가
    풀려 다음 발주가 KIS까지 다시 도달한다.
    """
    from localapp import intents
    trader, broker = isolated_trader
    calls = {"n": 0}

    def _reject(symbol, qty):
        calls["n"] += 1
        return {"success": False, "message": "모의투자 장시작전 입니다.", "order_no": ""}

    monkeypatch.setattr(broker, "sell", _reject)
    decisions: list[dict] = []
    trader._submit_sell("s4", "전략", "005930", 1, 70000.0, _policy(), "청산", decisions)
    assert calls["n"] == 1
    assert any(d["action"] == "rejected" for d in decisions)
    # 거부 후 멱등 게이트가 풀려 있어야(intent failed) 재시도가 KIS까지 도달
    assert not intents.is_active("2026-06-01", "s4", "005930", "sell")
    trader._submit_sell("s4", "전략", "005930", 1, 70000.0, _policy(), "청산", decisions)
    assert calls["n"] == 2, "거부 intent가 'submitted'로 남아 당일 재시도가 차단됨"

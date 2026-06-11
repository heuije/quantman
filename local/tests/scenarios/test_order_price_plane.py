"""가격 평면 정책 회귀 — 주문 지정가는 거래 평면(발주 직전 실시간 시세)에서만.

2026-06-11 선물 만기일 인시던트: 연속물 dataset 종가(1219.1)×1.01 지정가가
실제 거래 계약의 상한가를 초과 → "모의투자 상/하한가 오류" 거부 → 진입 실종.
근본 = ① 연구 평면(백테스트용 dataset) 값이 그대로 주문 가격이 되는 평면 혼용,
② 가격밴드 가드가 주식 ±30% 전용(자산군 비인지), ③ 거부돼도 intent가
'submitted'로 남아 당일 재시도 차단(D5-5).

계약: 즉시 지정가 = broker.quote_band(실시간 현재가+거래소 상·하한가) 기준.
시세 실패 시 dataset 폴백 금지(발주 보류+표면화). 거부는 intent 해제.
"""

from __future__ import annotations

import datetime

from quant_core.exec_defaults import merged_execution


def _policy():
    p = dict(merged_execution(None))
    p["use_limit"] = True
    return p


def test_immediate_buy_anchors_on_live_quote_not_dataset(isolated_trader):
    """지정가 기준 = 실시간 현재가 — 호출자가 넘긴 연구값(ref_price)이 아님."""
    trader, broker = isolated_trader
    broker._prices["코스피200선물"] = 1250.0          # 실계약 실시간 현재가
    decisions: list[dict] = []
    # ref_price=1219.1 = 연속물 dataset 값(인시던트 재현) — 사이징·저널 용도로만 전달
    trader._submit_buy("s1", "양방향", {}, "코스피200선물", 1, 1219.1,
                       _policy(), decisions)
    buys = [s for s in broker.submitted if s["side"] == "buy"]
    assert len(buys) == 1
    # 살아있는 1250 기준 ×(1+tol) — 연구값 1219.1 기준(≈1231)이면 1250 미만이라 판별됨
    assert buys[0]["limit"] >= 1250.0, \
        f"지정가가 연구 평면 값 기준으로 산출됨: {buys[0]['limit']}"


def test_quote_failure_blocks_order_without_dataset_fallback(isolated_trader, monkeypatch):
    """시세 조회 실패 → 발주 보류 + 표면화 + intent 해제 — dataset 폴백 금지."""
    from localapp import intents
    trader, broker = isolated_trader
    monkeypatch.setattr(broker, "quote_band",
                        lambda s: {"price": 0.0, "upper": None, "lower": None})
    decisions: list[dict] = []
    trader._submit_buy("s2", "양방향", {}, "코스피200선물", 1, 1219.1,
                       _policy(), decisions)
    assert [s for s in broker.submitted if s["side"] == "buy"] == []
    assert any(d["action"] == "skip_quote" for d in decisions)
    # intent가 점유로 남으면 다음 사이클 정당 재시도까지 막힌다
    assert not intents.is_active("2026-06-01", "s2", "코스피200선물", "buy")


def test_band_clamp_uses_broker_limits(isolated_trader, monkeypatch):
    """거래소 제공 상한가로 클램프 — 자체 ±% 재계산이 아니라 브로커 값."""
    trader, broker = isolated_trader
    monkeypatch.setattr(broker, "quote_band",
                        lambda s: {"price": 1219.1, "upper": 1225.0, "lower": None})
    decisions: list[dict] = []
    trader._submit_buy("s3", "양방향", {}, "코스피200선물", 1, 1219.1,
                       _policy(), decisions)
    buys = [s for s in broker.submitted if s["side"] == "buy"]
    assert len(buys) == 1
    assert buys[0]["limit"] == 1225.0, \
        f"상한가(1225) 클램프 실패: {buys[0]['limit']} — 만기일 거부 재현 위험"


def test_krx_futures_buy_clamped_to_realtime_band(isolated_trader, monkeypatch):
    """KRX 선물 매수 지정가 ≤ 직전가×1.01 (실시간가격제한, 틱 내림).

    06-11 실측: 현재가 1235.15에 tol 1% 틱 올림 → 1247.55가 밴드 상단
    1247.50을 1틱 초과해 "모의투자 상/하한가 오류" 거부. 클램프로 1247.50.
    """
    trader, broker = isolated_trader
    monkeypatch.setattr(broker, "quote_band",
                        lambda s: {"price": 1235.15, "upper": None, "lower": None})
    decisions: list[dict] = []
    trader._submit_buy("s5", "양방향", {}, "코스피200선물", 1, 1208.5,
                       _policy(), decisions)
    buys = [s for s in broker.submitted if s["side"] == "buy"]
    assert len(buys) == 1
    assert abs(buys[0]["limit"] - 1247.50) < 1e-9, \
        f"실시간가격제한 클램프 실패: {buys[0]['limit']} (거부 재현 값=1247.55)"


def test_krx_futures_sell_clamped_to_realtime_band(isolated_trader, monkeypatch):
    """KRX 선물 매도 지정가 ≥ 직전가×0.99 (틱 올림) — 종가 청산(매도)도 동일 밴드.

    매수만 고치면 15:43 당일청산 매도가 같은 1틱 초과로 거부된다(대칭 필수).
    """
    trader, broker = isolated_trader
    monkeypatch.setattr(broker, "quote_band",
                        lambda s: {"price": 1235.15, "upper": None, "lower": None})
    decisions: list[dict] = []
    trader._submit_sell("s6", "양방향", "코스피200선물", 1, 1208.5,
                        _policy(), "청산", decisions)
    sells = [s for s in broker.submitted if s["side"] == "sell"]
    assert len(sells) == 1
    assert abs(sells[0]["limit"] - 1222.80) < 1e-9, \
        f"실시간가격제한 클램프 실패: {sells[0]['limit']} (틱 내림이면 1222.75)"


def test_stock_limit_not_clamped_by_futures_band(isolated_trader, monkeypatch):
    """주식은 실시간가격제한 비대상 — tol 틱 올림 그대로(기존 동작 보존)."""
    trader, broker = isolated_trader
    monkeypatch.setattr(broker, "quote_band",
                        lambda s: {"price": 70050.0, "upper": None, "lower": None})
    decisions: list[dict] = []
    trader._submit_buy("s7", "주식전략", {}, "005930", 1, 70000.0,
                       _policy(), decisions)
    buys = [s for s in broker.submitted if s["side"] == "buy"]
    assert len(buys) == 1
    # 70050×1.01=70750.5 → 호가단위(100) 올림 70800 — ±1% 클램프(70700)가 아님
    assert buys[0]["limit"] == 70800, f"주식 지정가 변화: {buys[0]['limit']}"


def test_rejected_order_releases_intent_for_retry(isolated_trader, monkeypatch):
    """KIS 거부(주문 미생성) → intent 해제 → 당일 재시도 가능 (리뷰 D5-5).

    이전: mark_submitted가 success 무관 선행 + 거부 후 마감 없음 → 같은 날
    동일 (전략,종목,side) 재시도가 [L-01] 게이트에 전부 차단 — 06-09 장종료
    거부 건이 그날 내내 재시도 불가였던 메커니즘.
    """
    trader, broker = isolated_trader
    broker._prices["코스피200선물"] = 1250.0
    calls = {"n": 0}

    def _reject(symbol, qty, limit_price):
        calls["n"] += 1
        return {"success": False, "message": "모의투자 상/하한가 오류",
                "order_no": ""}

    monkeypatch.setattr(broker, "buy_limit", _reject)
    decisions: list[dict] = []
    trader._submit_buy("s4", "양방향", {}, "코스피200선물", 1, 1219.1,
                       _policy(), decisions)
    assert calls["n"] == 1
    assert any(d["action"] == "rejected" for d in decisions)
    # 거부 후 재시도 — 게이트가 풀려 있어야 KIS까지 다시 도달한다
    trader._submit_buy("s4", "양방향", {}, "코스피200선물", 1, 1219.1,
                       _policy(), decisions)
    assert calls["n"] == 2, "거부 intent가 'submitted'로 남아 당일 재시도가 차단됨"

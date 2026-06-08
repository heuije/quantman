"""M9 F4 — 포트폴리오 평가금액이 선물 승수를 반영하는지(_position_value).

선물 1계약 가치 = 가격 × 계약수 × 승수. 승수를 빼면 1계약을 1주처럼 저평가해 비중·상관
노출이 왜곡된다. 주식은 multiplier=1 → 무변경(byte-identical). 순수 헬퍼 단위검증.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.routers.portfolio import _position_value


def test_value_equity_multiplier_one_unchanged():
    # 주식: eval_price × qty × 1 — 기존과 동일
    assert _position_value({"symbol": "005930", "eval_price": 70000, "qty": 10}) == 700_000


def test_value_domestic_futures_applies_multiplier():
    # KOSPI200 선물 승수 250,000 — 가격필드는 settle_price(국내 잔고)
    v = _position_value({"symbol": "코스피200선물", "settle_price": 350.0, "qty": 2})
    assert v == 350.0 * 2 * 250_000


def test_value_overseas_futures_applies_multiplier():
    # 금선물(GC) 승수 100 — 가격필드는 eval_price(해외 잔고)
    v = _position_value({"symbol": "금선물", "eval_price": 1950.0, "qty": 1})
    assert v == 1950.0 * 100


def test_value_price_field_fallback():
    # eval_price 없으면 settle_price, 그것도 없으면 avg_price
    assert _position_value({"symbol": "005930", "avg_price": 50000, "qty": 1}) == 50_000
    assert _position_value({"symbol": "코스피200선물", "avg_price": 351.0, "qty": 1}) == 351.0 * 250_000


def test_value_zero_when_no_price():
    assert _position_value({"symbol": "005930", "qty": 10}) == 0

"""M5a — reconcile_ledger (symbol, side) 인지 단위검증.

롱/숏을 분리 매칭(같은 종목 헤지·숏 구분) + KIS side 표현 정규화(buy/sell·long/short·주식 없음).
주식(전부 롱)은 종전 symbol-only 동작과 동치.
"""
from __future__ import annotations

from localapp.analytics import norm_side, reconcile_ledger


def test_norm_side_normalizes_representations():
    assert norm_side("long") == "long" and norm_side("buy") == "long"
    assert norm_side("매수") == "long" and norm_side(None) == "long" and norm_side("") == "long"
    assert norm_side("short") == "short" and norm_side("sell") == "short" and norm_side("매도") == "short"


def test_equity_all_long_in_sync_unchanged():
    # 주식: KIS positions에 side 없음 → long. ledger side="long". 종전과 동치.
    kis = [{"symbol": "005930", "qty": 10}]
    ledger = {"s1": {"symbol": "005930", "qty": 10, "side": "long"}}
    r = reconcile_ledger(kis, ledger)
    assert r["in_sync"] == ["005930"] and not r["ledger_orphans"] and not r["external_extras"]


def test_equity_legacy_ledger_without_side_defaults_long():
    # M4 이전 ledger 항목엔 side 키 없음 → norm_side가 long 기본(하위호환).
    kis = [{"symbol": "005930", "qty": 10}]
    ledger = {"s1": {"symbol": "005930", "qty": 10}}     # side 키 없음
    r = reconcile_ledger(kis, ledger)
    assert r["in_sync"] == ["005930"]


def test_long_and_short_same_symbol_separated():
    # 같은 종목에 롱+숏 — 분리 매칭(symbol-only면 3계약으로 합산돼 오판).
    kis = [{"symbol": "금선물", "qty": 2, "side": "long"},
           {"symbol": "금선물", "qty": 1, "side": "short"}]
    ledger = {"L": {"symbol": "금선물", "qty": 2, "side": "long"},
              "S": {"symbol": "금선물", "qty": 1, "side": "short"}}
    r = reconcile_ledger(kis, ledger)
    assert sorted(r["in_sync"]) == ["금선물", "금선물"]      # 롱·숏 각각 in_sync
    assert not r["ledger_orphans"] and not r["external_extras"]


def test_short_external_close_is_orphan():
    # ledger 숏 1계약인데 KIS 숏 0(외부 환매) → orphan(side=short).
    ledger = {"S": {"symbol": "금선물", "qty": 1, "side": "short"}}
    r = reconcile_ledger([], ledger)
    assert len(r["ledger_orphans"]) == 1
    o = r["ledger_orphans"][0]
    assert o["symbol"] == "금선물" and o["side"] == "short" and o["shortfall"] == 1


def test_domestic_futures_kis_side_buy_sell_normalized():
    # KIS 국내선물 잔고 side="buy"/"sell" → long/short 정규화 매칭.
    kis = [{"symbol": "코스피200선물", "qty": 1, "side": "sell"}]   # 숏 보유
    ledger = {"S": {"symbol": "코스피200선물", "qty": 1, "side": "short"}}
    r = reconcile_ledger(kis, ledger)
    assert r["in_sync"] == ["코스피200선물"] and not r["ledger_orphans"]


def test_short_side_mismatch_not_confused_with_long():
    # KIS는 롱 2, ledger는 숏 2(같은 종목·수량) — side 다르면 in_sync 아님(각각 orphan/extra).
    kis = [{"symbol": "금선물", "qty": 2, "side": "long"}]
    ledger = {"S": {"symbol": "금선물", "qty": 2, "side": "short"}}
    r = reconcile_ledger(kis, ledger)
    assert not r["in_sync"]
    assert len(r["ledger_orphans"]) == 1 and r["ledger_orphans"][0]["side"] == "short"
    assert len(r["external_extras"]) == 1 and r["external_extras"][0]["side"] == "long"

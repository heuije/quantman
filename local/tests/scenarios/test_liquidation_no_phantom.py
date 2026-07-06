"""R6/D6 — 비상청산(liquidation) booking 불변식 회귀.

I7: 비상청산 체결은 신규 원장 포지션을 절대 만들지 않는다(차감 또는 외부청산 기록만).
I8: 매칭은 합성 sid가 아니라 (종목, 반대 side) 기준(commingle 시 결정적 순서 차감).

트리거: 2026-07-06 모의(LS 국내선물) — 비상청산 buy4가 원장 숏을 차감하지 못하고 합성 sid
`liquidate:코스피200선물`로 신규 롱4 유령을 생성. 수정 전이면 첫 테스트가 실패한다.
"""
from __future__ import annotations

import json

from localapp import trader as tr


def _pending(sid, symbol, side, **extra):
    p = {"strategy_id": sid, "symbol": symbol, "side": side,
         "strategy_name": extra.pop("strategy_name", "t"),
         "definition": {}, "reason": extra.pop("reason", "")}
    p.update(extra)
    return p


def _trades():
    path = tr.TRADES_PATH
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


# ── 07-06 재현: 비상청산 buy가 원장 숏을 차감하고 신규 롱 유령을 만들지 않는다 ──────────
def test_liquidation_buy_closes_short_no_phantom(isolated_trader):
    trader, _ = isolated_trader
    # 전략17이 코스피200선물 숏10 보유 (07-06 12:33 상황)
    trader._apply_fill("O1", _pending("17", "코스피200선물", "sell"), 10, 1284.96, [])
    assert trader.ledger["17"]["side"] == "short" and trader.ledger["17"]["qty"] == 10
    # 비상청산 buy4 (합성 sid·liquidation=True) — 수정 전이면 신규 롱4 유령 생성
    dec: list = []
    trader._apply_fill(
        "O2", _pending("liquidate:코스피200선물", "코스피200선물", "buy",
                       strategy_name="비상청산", liquidation=True), 4, 1283.96, dec)
    assert "liquidate:코스피200선물" not in trader.ledger        # I7: 유령 없음
    assert trader.ledger["17"]["side"] == "short"                # I8: 종목 숏 차감
    assert trader.ledger["17"]["qty"] == 6                       # 10 − 4
    assert any(d.get("action") == "liquidated" for d in dec), dec


# ── 원장 미추적 외부 보유분 청산: 원장 무변화·유령 없음 ─────────────────────────────
def test_liquidation_external_holding_no_phantom(isolated_trader):
    trader, _ = isolated_trader
    dec: list = []
    trader._apply_fill(
        "O1", _pending("liquidate:금선물", "금선물", "buy",
                       strategy_name="비상청산", liquidation=True), 3, 1900.0, dec)
    assert trader.ledger == {}                                   # I7: 원장 무변화
    assert any(d.get("action") == "external_liquidated" for d in dec), dec


# ── 비상청산 sell이 원장 롱을 청산·유령 숏 없음 + 정산손익 기록 ──────────────────────
def test_liquidation_sell_closes_long_no_phantom(isolated_trader):
    trader, _ = isolated_trader
    trader._apply_fill("O1", _pending("s1", "금선물", "buy"), 2, 1900.0, [])
    dec: list = []
    trader._apply_fill(
        "O2", _pending("liquidate:금선물", "금선물", "sell",
                       strategy_name="비상청산", liquidation=True), 2, 1950.0, dec)
    assert trader.ledger == {}                                   # 롱 청산·유령 숏 없음
    ev = _trades()[-1]
    assert ev.get("realized_pnl") == (1950 - 1900) * 2 * 100     # 10000
    assert any(d.get("action") == "liquidated" for d in dec), dec


# ── commingle: 한 종목을 두 전략이 보유 — 결정적 순서(sid 사전순)로 부분 청산 ─────────
def test_liquidation_commingle_partial_deterministic(isolated_trader):
    trader, _ = isolated_trader
    # 같은 종목 숏: sid "17" 6계약, sid "20" 4계약 (동일 entry_date → sid 사전순)
    trader._apply_fill("O1", _pending("17", "금선물", "sell"), 6, 1950.0, [])
    trader._apply_fill("O2", _pending("20", "금선물", "sell"), 4, 1960.0, [])
    dec: list = []
    # 비상청산 buy8 → "17"(6) 전량 + "20"(2) 부분 청산, "20" 숏2 잔존
    trader._apply_fill(
        "O3", _pending("liquidate:금선물", "금선물", "buy",
                       strategy_name="비상청산", liquidation=True), 8, 1900.0, dec)
    assert "17" not in trader.ledger                             # 먼저 전량 청산
    assert trader.ledger["20"]["side"] == "short"
    assert trader.ledger["20"]["qty"] == 2                       # 4 − 2
    assert "liquidate:금선물" not in trader.ledger                # 유령 없음


# ── 회귀 가드: liquidation=False(기본) 경로는 종전 동작 유지 ──────────────────────
def test_normal_buy_without_position_still_opens_long(isolated_trader):
    trader, _ = isolated_trader
    # 비상청산 아닌 일반 buy — 보유 없는 선물 = 신규 롱(기존 동작 보존)
    trader._apply_fill("O1", _pending("s1", "금선물", "buy"), 2, 1900.0, [])
    assert trader.ledger["s1"]["side"] == "long" and trader.ledger["s1"]["qty"] == 2

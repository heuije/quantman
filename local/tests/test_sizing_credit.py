"""§18 사이징 크레딧 순수 헬퍼 단위 — credit_for / consume_credit / _credit_key.

두 풀의 의미 계약(§18.2 정정 2026-07-18 — 방향무관이 원칙)을 고정한다:
  (symbol, None) = **방향무관 풀(원칙)** — 계획 청산·원장 밖(수동) 보유 모두 "빈-상태
                   잔고 기준"으로 진입 방향과 무관하게 크레딧(갈아타기 포함).
  (symbol, side) = **같은-편 강등 풀** — 선물인데 브로커 orderable의 '신규 전용' 여부가
                   미확정(KIS TTTO5105R 실측 대기)일 때 _credit_key가 강등하는 안전 게이트
                   (합산 의미면 반대편 orderable에 청산분이 이미 포함 → 이중계상 방지).

시나리오 레벨(원장·발주·체결)은 tests/scenarios/test_target_reconciliation.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL_DIR = Path(__file__).resolve().parent.parent
_CORE_DIR = _LOCAL_DIR.parent / "core"
for _p in (str(_LOCAL_DIR), str(_CORE_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from localapp.netting import Intent
from localapp.trader import Trader, consume_credit, credit_for

S = "코스피200선물"


# ── credit_for — 조회 계약 ────────────────────────────────────────────────────
def test_credit_none_or_empty_is_zero():
    assert credit_for(None, S, "long") == 0.0
    assert credit_for({}, S, "long") == 0.0


def test_same_side_pool_applies_only_to_same_side():
    """같은-편 강등 풀은 그 방향에만 — 미확정 브로커(KIS 실측 전) 게이트의 메커니즘."""
    cred = {(S, "long"): 3}
    assert credit_for(cred, S, "long") == 3.0
    assert credit_for(cred, S, "short") == 0.0


def test_manual_pool_applies_regardless_of_entry_side():
    """수동(원장 밖) 크레딧은 방향 무관 — §18.2 정정의 핵심."""
    cred = {(S, None): 4}
    assert credit_for(cred, S, "long") == 4.0
    assert credit_for(cred, S, "short") == 4.0


def test_pools_sum():
    cred = {(S, "long"): 3, (S, None): 4}
    assert credit_for(cred, S, "long") == 7.0     # 같은-편 3 + 수동 4
    assert credit_for(cred, S, "short") == 4.0    # 수동 4만


def test_other_symbol_not_credited():
    cred = {("다른선물", "long"): 5, ("다른선물", None): 5}
    assert credit_for(cred, S, "long") == 0.0


# ── consume_credit — 다중 진입 순차 소진(E2·N9) ───────────────────────────────
def test_consume_drains_same_side_pool_first():
    cred = {(S, "long"): 3, (S, None): 4}
    consume_credit(cred, S, "long", 2)
    assert credit_for(cred, S, "long") == 5.0     # 7 − 2
    assert cred[(S, "long")] == 1                 # 같은-편 먼저
    assert cred[(S, None)] == 4                   # 수동 풀 그대로


def test_consume_spills_into_manual_pool():
    cred = {(S, "long"): 3, (S, None): 4}
    consume_credit(cred, S, "long", 5)
    assert cred[(S, "long")] == 0                 # 같은-편 3 전부
    assert cred[(S, None)] == 2                   # 부족분 2는 수동 풀에서
    assert credit_for(cred, S, "long") == 2.0     # 7 − 5


def test_consume_overshoot_goes_negative_and_shrinks_next_entry():
    """초과 소진은 음수 — 다음 진입의 orderable을 그만큼 깎는다(기존 동작 보존)."""
    cred = {(S, "long"): 2}
    consume_credit(cred, S, "long", 5)
    assert credit_for(cred, S, "long") == -3.0    # 2 − 5


def test_consume_manual_pool_shared_across_directions():
    """수동 풀은 방향 공유 — 롱 진입이 쓴 여력을 숏 진입이 재사용하면 안 된다."""
    cred = {(S, None): 4}
    consume_credit(cred, S, "long", 3)
    assert credit_for(cred, S, "short") == 1.0    # 4 − 3 (숏도 줄어든 풀을 본다)


def test_consume_sum_decreases_exactly_by_consumed():
    """검산 항등식: 소진 후 두 풀 합 = 소진 전 합 − consumed."""
    for same, manual, consumed in ((3, 4, 2), (3, 4, 9), (0, 5, 5), (5, 0, 7)):
        cred = {(S, "long"): same, (S, None): manual}
        before = credit_for(cred, S, "long")
        consume_credit(cred, S, "long", consumed)
        assert credit_for(cred, S, "long") == before - consumed


# ── _credit_key / _freed_capacity — 방향무관 원칙 + 미확정 브로커 강등(§18.2) ──
class _StubBroker:
    def __init__(self, new_only: bool):
        self._new_only = new_only

    def orderable_new_only(self, symbol):
        return self._new_only


class _StubTrader:
    """Trader의 크레딧 키잉·기준가만 빌려 쓰는 검증용 껍데기 — self.broker만 사용한다."""
    _orderable_new_only = Trader._orderable_new_only
    _credit_key = Trader._credit_key
    _freed_capacity = Trader._freed_capacity
    _stock_credit_price = Trader._stock_credit_price
    _safe_price = Trader._safe_price

    def __init__(self, broker):
        self.broker = broker


def _exit_intent(symbol: str, qty: int, side: str, ref: float) -> Intent:
    return Intent(sid="s:x", strategy_id="s", strategy_name="", contract_key=symbol,
                  symbol=symbol, kind="exit", position_side=side,
                  order_side=("sell" if side == "long" else "buy"), qty=qty,
                  ref_price=ref, entry_price=ref, mult=1.0, currency="KRW",
                  definition={})


def test_credit_key_direction_free_on_new_only_broker():
    """신규 전용 확정 브로커(LS류) — 선물 크레딧도 방향무관 풀(원칙 경로)."""
    assert _StubTrader(_StubBroker(True))._credit_key(S, "long") == (S, None)


def test_credit_key_demotes_futures_on_unconfirmed_broker():
    """미확정 브로커(KIS TTTO5105R 실측 전) — 같은-편 키 강등(반대편 이중계상 방지)."""
    assert _StubTrader(_StubBroker(False))._credit_key(S, "long") == (S, "long")


def test_credit_key_demotes_when_flag_not_exposed():
    """orderable_new_only 미구현 브로커(Mock·구버전) — 보수 강등."""
    class _Legacy:
        pass
    assert _StubTrader(_Legacy())._credit_key(S, "short") == (S, "short")


def test_credit_key_stock_always_direction_free():
    """주식은 현금(예수금) 크레딧 — orderable 의미와 무관, 항상 방향무관."""
    assert _StubTrader(_StubBroker(False))._credit_key("005930", "long") == ("005930", None)


def test_freed_capacity_futures_keying_follows_broker_confirmation():
    lg = _exit_intent(S, 5, "long", 300.0)
    assert _StubTrader(_StubBroker(True))._freed_capacity([lg]) == {(S, None): 5}
    assert _StubTrader(_StubBroker(False))._freed_capacity([lg]) == {(S, "long"): 5}


def test_freed_capacity_stock_cash_amount_direction_free():
    """실시간 조회 전패(스텁 브로커 seam 없음) → 청산 의도 ref_price 최종 폴백(종전 거동)."""
    lg = _exit_intent("005930", 3, "long", 10_000.0)
    assert _StubTrader(_StubBroker(False))._freed_capacity([lg]) == {("005930", None): 30_000.0}


# ── _stock_credit_price — 실시간(예상체결가) 우선 폴백 사슬(§18·2026-07-19) ────
def test_stock_credit_price_prefers_expected_fill():
    class _B(_StubBroker):
        def expected_fill_price(self, symbol):
            return 12_345.0

        def price(self, symbol):
            return 11_111.0

    assert _StubTrader(_B(True))._stock_credit_price("005930", None) == 12_345.0


def test_stock_credit_price_falls_back_live_then_bundle():
    class _BLive(_StubBroker):
        def expected_fill_price(self, symbol):
            raise RuntimeError("blip")                 # 예상가 실패 → 현재가 폴백

        def price(self, symbol):
            return 11_111.0

    assert _StubTrader(_BLive(True))._stock_credit_price("005930", None) == 11_111.0

    import pandas as pd
    ds = {"005930": pd.DataFrame({"Close": [10_000.0]},
                                 index=pd.date_range("2026-05-29", periods=1))}
    assert _StubTrader(_StubBroker(True))._stock_credit_price("005930", ds) == 10_000.0


def test_freed_capacity_stock_uses_live_price_over_intent_ref():
    """청산 의도 ref(전일 종가 10,000)보다 실시간 추정(12,000)이 크레딧 기준."""
    class _B(_StubBroker):
        def expected_fill_price(self, symbol):
            return 12_000.0

    lg = _exit_intent("005930", 3, "long", 10_000.0)
    assert _StubTrader(_B(True))._freed_capacity([lg]) == {("005930", None): 36_000.0}


# ── 어댑터 플래그·라우터 위임 계약 ────────────────────────────────────────────
def test_broker_flags_contract():
    """KIS는 TTTO5105R ord_psbl_qty 의미 실측(모의 왕복) 확정 전 False 잠금."""
    from localapp.kis_futures_broker import KisFuturesBroker
    from localapp.ls_futures_broker import LsFuturesBroker
    assert LsFuturesBroker.ORDERABLE_NEW_ONLY is True
    assert KisFuturesBroker.ORDERABLE_NEW_ONLY is False


def test_router_delegates_flag_and_defaults_false():
    from localapp.broker_router import BrokerRouter

    class _Fut:
        ORDERABLE_NEW_ONLY = True

    r = BrokerRouter(stock=None, futures=_Fut(), resolve=lambda s: "101V6000")
    assert r.orderable_new_only(S) is True
    assert r.orderable_new_only("005930") is False      # 주식 — 선물 게이트 무관

    r2 = BrokerRouter(stock=None, futures=object(), resolve=lambda s: None)
    assert r2.orderable_new_only(S) is False            # 플래그 미선언 어댑터 — 보수


def test_router_expected_fill_price_delegation():
    from localapp.broker_router import BrokerRouter

    class _Stock:
        def expected_fill_price(self, symbol):
            return 70_500.0

    r = BrokerRouter(stock=_Stock(), futures=None, resolve=lambda s: s)
    assert r.expected_fill_price("005930") == 70_500.0
    # 미구현 주식 어댑터(LS 등)·주식 미구성 — 0.0(호출자 현재가·번들 폴백)
    assert BrokerRouter(stock=object(), futures=None,
                        resolve=lambda s: s).expected_fill_price("005930") == 0.0
    assert BrokerRouter(stock=None, futures=None,
                        resolve=lambda s: s).expected_fill_price("005930") == 0.0

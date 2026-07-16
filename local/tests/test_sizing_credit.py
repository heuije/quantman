"""§18 사이징 크레딧 순수 헬퍼 단위 — trader.credit_for / consume_credit.

두 풀의 의미 계약을 고정한다:
  (symbol, side) = 계획 청산이 되돌려줄 여력 — **같은-편 진입에만**(리버설은 남은 여력 기준).
  (symbol, None) = 원장 밖(수동/외부) 보유 — **방향 무관**(§18.1 "전부 취소한 잔고 기준").

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

from localapp.trader import consume_credit, credit_for

S = "코스피200선물"


# ── credit_for — 조회 계약 ────────────────────────────────────────────────────
def test_credit_none_or_empty_is_zero():
    assert credit_for(None, S, "long") == 0.0
    assert credit_for({}, S, "long") == 0.0


def test_same_side_pool_applies_only_to_same_side():
    """계획 청산 크레딧은 같은-편 진입에만 — 리버설엔 미적용(유저 모델)."""
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

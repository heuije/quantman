"""리밸런싱 매도 판정 단위 테스트 — 주기 게이팅 + 멤버십 탈락 안전 가드.

실제 KIS 발주는 검증하지 않는다(로컬앱 모의투자 영역). 여기선 '언제·무엇을
매도 대상으로 판정하는가'의 순수 로직만 검증한다.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# localapp 패키지 경로 추가 (platform/local)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))

import quant_core as qc
from localapp.trader import Trader, _rebalance_due


# ── 주기 게이팅 ───────────────────────────────────────────────────────────────

def test_due_first_run_always_true():
    assert _rebalance_due("daily", None, date(2026, 5, 22)) is True


def test_due_same_day_blocked():
    assert _rebalance_due("daily", "2026-05-22", date(2026, 5, 22)) is False


def test_due_daily_next_day():
    assert _rebalance_due("daily", "2026-05-21", date(2026, 5, 22)) is True


def test_due_weekly_same_week():
    # 2026-05-18(월) ~ 같은 ISO주 → 미도래
    assert _rebalance_due("weekly", "2026-05-18", date(2026, 5, 22)) is False


def test_due_weekly_new_week():
    assert _rebalance_due("weekly", "2026-05-18", date(2026, 5, 25)) is True


def test_due_monthly_same_month():
    assert _rebalance_due("monthly", "2026-05-02", date(2026, 5, 22)) is False


def test_due_monthly_new_month():
    assert _rebalance_due("monthly", "2026-05-31", date(2026, 6, 1)) is True


# ── 멤버십 탈락 판정 (_rebalance_reason) ──────────────────────────────────────

def _trader() -> Trader:
    # broker는 _rebalance_reason에서 쓰이지 않으므로 None으로 충분.
    return Trader(broker=None)  # type: ignore[arg-type]


def _strat(enabled=True, period="daily", trade_symbol="screener:custom"):
    return qc.Strategy(name="t", trade_symbol=trade_symbol,
                       rebalance={"enabled": enabled, "period": period})


def test_dropped_member_triggers_sell():
    t = _trader()
    t.rebalance_state = {}
    members = {"7": ["005930", "000660"]}
    due: dict = {}
    # 보유 035420은 상위 N에서 탈락 → 매도
    r = t._rebalance_reason("7:035420", _strat(), "035420", members, due, date(2026, 5, 22))
    assert r == "리밸런싱"


def test_still_member_keeps():
    t = _trader()
    t.rebalance_state = {}
    members = {"7": ["005930", "000660"]}
    r = t._rebalance_reason("7:005930", _strat(), "005930", members, {}, date(2026, 5, 22))
    assert r is None


def test_empty_members_never_sells():
    """안전 가드 — 멤버십 데이터 없으면 절대 매도 안 함 (대량 청산 방지)."""
    t = _trader()
    t.rebalance_state = {}
    r = t._rebalance_reason("7:035420", _strat(), "035420", {"7": []}, {}, date(2026, 5, 22))
    assert r is None
    r2 = t._rebalance_reason("7:035420", _strat(), "035420", {}, {}, date(2026, 5, 22))
    assert r2 is None


def test_disabled_rebalance_none():
    t = _trader()
    r = t._rebalance_reason("7:035420", _strat(enabled=False), "035420",
                            {"7": ["005930"]}, {}, date(2026, 5, 22))
    assert r is None


def test_manual_mode_none():
    t = _trader()
    r = t._rebalance_reason("7:035420", _strat(trade_symbol="005930,000660"),
                            "035420", {"7": ["005930"]}, {}, date(2026, 5, 22))
    assert r is None


def test_not_due_blocks_sell():
    t = _trader()
    t.rebalance_state = {"7": "2026-05-22"}   # 오늘 이미 함
    r = t._rebalance_reason("7:035420", _strat(period="daily"), "035420",
                            {"7": ["005930"]}, {}, date(2026, 5, 22))
    assert r is None

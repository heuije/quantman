"""급등락→회귀(reversion_analysis) 결정적 단언 테스트.

핵심 게이트 (합성 가격으로 손계산 대조):
- find_pivots: ZigZag 트레일링 피벗(고/저 교대) 결정성 + 레버리지 ÷환산.
- reversion_events: 트리거일·run_account·reversion_account(레버리지 ×환산·역추세 부호 통일).
- 청산: 역행 경로가 계좌 -100% 닿으면 liquidated·reversion=-1.0·liquidation_day.
- 방향 분리·gap 디클러스터·summary(평균/중앙값/성공률/청산율)·sweep 격자.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import pandas as pd
import pytest

_CORE_DIR = Path(__file__).resolve().parent.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from quant_core.oil_futures import (  # noqa: E402
    DOWN_EXHAUSTION,
    UP_EXHAUSTION,
    REVERSION_SWEEP_AXES,
    Pivot,
    ReversionEvent,
    find_pivots,
    reversion_events,
    reversion_summary,
    reversion_sweep,
)
from quant_core.oil_futures.reversion_analysis import (  # noqa: E402
    _decluster_events_by_gap,
)


def _mk(closes) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="B"),
        "open": list(closes),
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "close": list(closes),
        "volume": [1000] * n,
    })


# ───── find_pivots ──────────────────────────────────────────────────────

def test_find_pivots_alternating_known():
    # 100→120(고점)→90(저점)→110. reversal 10%(계좌)·lev1 → 지수 10%.
    df = _mk([100, 110, 120, 115, 100, 90, 95, 100, 110])
    pivots = find_pivots(df, reversal_account_pct=10, leverage=1)
    assert [p.kind for p in pivots] == ["low", "high", "low"]
    assert [p.price for p in pivots] == [100, 120, 90]


def test_find_pivots_leverage_divides_threshold():
    # reversal 50%(계좌)/lev10 == reversal 5%(계좌)/lev1 == 지수 5% → 동일 피벗.
    df = _mk([101, 110, 104, 99, 102, 106])
    a = find_pivots(df, reversal_account_pct=50, leverage=10)
    b = find_pivots(df, reversal_account_pct=5, leverage=1)
    assert [(p.kind, p.price) for p in a] == [(p.kind, p.price) for p in b]
    assert [(p.kind, p.price) for p in a] == [("low", 101), ("high", 110), ("low", 99)]


def test_find_pivots_short_series_empty():
    assert find_pivots(_mk([100]), reversal_account_pct=5, leverage=1) == []


# ───── reversion_events: 트리거 + 레버리지 + 부호 ─────────────────────────

def test_reversion_event_down_exhaustion_known():
    # 피벗 고점 110 → -11.8%(지수) 도달 97일 트리거(계좌 -118%) → 롱 → 2일 후 104 반등.
    df = _mk([101, 110, 104, 97, 100, 104])
    evs = reversion_events(
        df, reversal_account_pct=50, run_account_pct=100, horizon=2, leverage=10,
    )
    assert len(evs) == 1
    e = evs[0]
    assert e.direction == DOWN_EXHAUSTION
    assert e.pivot_price == 110
    assert e.trigger_price == 97
    assert e.run_account == pytest.approx(10 * (97 / 110 - 1))   # 지수 누적 × 10
    assert e.liquidated is False
    assert e.liquidation_day is None
    # 역추세 롱, 2일 후: 10 × (104/97 - 1)
    assert e.reversion_account == pytest.approx(10 * (104 / 97 - 1))
    assert e.reversion_account > 0                       # 회귀(되돌림) +부호


def test_reversion_event_liquidation():
    # 트리거 97 진입(롱) 후 다음날 86으로 추가 -11.3%(지수) → 계좌 -113% → 청산.
    df = _mk([101, 110, 104, 97, 86, 93])
    evs = reversion_events(
        df, reversal_account_pct=50, run_account_pct=100, horizon=2, leverage=10,
    )
    assert len(evs) == 1
    e = evs[0]
    assert e.direction == DOWN_EXHAUSTION
    assert e.trigger_price == 97
    assert e.liquidated is True
    assert e.liquidation_day == 1
    assert e.reversion_account == pytest.approx(-1.0)    # 전손


def test_reversion_event_direction_filter():
    df = _mk([101, 110, 104, 97, 100, 104])
    down = reversion_events(
        df, reversal_account_pct=50, run_account_pct=100, horizon=2, leverage=10,
        direction=DOWN_EXHAUSTION,
    )
    up = reversion_events(
        df, reversal_account_pct=50, run_account_pct=100, horizon=2, leverage=10,
        direction=UP_EXHAUSTION,
    )
    assert len(down) == 1 and down[0].direction == DOWN_EXHAUSTION
    assert up == []


def test_reversion_up_exhaustion_short_sign():
    # 저점 90 → +20%(지수, 계좌 +200%) 상승소진 트리거 → 숏 → 이후 하락하면 회귀 +.
    # 90→97(아직 +7.8%)→108(트리거, +20%)→100→95. lev10·run 100%(계좌)=지수 10%.
    df = _mk([100, 90, 97, 108, 100, 95])
    evs = reversion_events(
        df, reversal_account_pct=50, run_account_pct=100, horizon=2, leverage=10,
        direction=UP_EXHAUSTION,
    )
    assert len(evs) == 1
    e = evs[0]
    assert e.direction == UP_EXHAUSTION
    assert e.trigger_price == 108
    # 숏: pos=-1 → reversion = -10 × (95/108 - 1) > 0 (가격 하락 = 되돌림)
    assert e.reversion_account == pytest.approx(-10 * (95 / 108 - 1))
    assert e.reversion_account > 0


# ───── summary ──────────────────────────────────────────────────────────

def _ev(direction, reversion, liquidated=False):
    ts = pd.Timestamp("2020-01-01")
    return ReversionEvent(
        pivot_date=ts, pivot_price=100.0, trigger_date=ts, trigger_price=90.0,
        direction=direction, run_account=-1.0, reversion_account=reversion,
        liquidated=liquidated, liquidation_day=(1 if liquidated else None),
    )


def test_reversion_summary_mean_median_rates():
    # 4건 회귀(+) + 1건 청산(-100%). 평균 -3%, 중앙값 +20%, 성공 80%, 청산 20%.
    evs = [
        _ev(DOWN_EXHAUSTION, 0.30), _ev(DOWN_EXHAUSTION, 0.25),
        _ev(DOWN_EXHAUSTION, 0.20), _ev(DOWN_EXHAUSTION, 0.10),
        _ev(DOWN_EXHAUSTION, -1.0, liquidated=True),
    ]
    s = reversion_summary(evs)
    d = s[DOWN_EXHAUSTION]
    assert d["n"] == 5
    assert d["mean_reversion"] == pytest.approx(-3.0)     # (30+25+20+10-100)/5
    assert d["median_reversion"] == pytest.approx(20.0)
    assert d["success_rate"] == pytest.approx(80.0)
    assert d["liquidation_rate"] == pytest.approx(20.0)
    # 빈 방향은 nan
    assert s[UP_EXHAUSTION]["n"] == 0
    assert math.isnan(s[UP_EXHAUSTION]["mean_reversion"])


# ───── gap 디클러스터 ─────────────────────────────────────────────────────

def test_decluster_by_gap_same_direction():
    dates = pd.date_range("2020-01-01", periods=11, freq="B")
    date_to_i = {pd.Timestamp(d): k for k, d in enumerate(dates)}
    # 같은 방향 트리거가 영업일 인덱스 0·2·10. gap=5 → 0 유지, 2 제거, 10 유지.
    evs = []
    for idx in (0, 2, 10):
        evs.append(ReversionEvent(
            pivot_date=dates[idx], pivot_price=100.0,
            trigger_date=dates[idx], trigger_price=90.0,
            direction=DOWN_EXHAUSTION, run_account=-1.0, reversion_account=0.1,
            liquidated=False, liquidation_day=None,
        ))
    kept = _decluster_events_by_gap(evs, gap=5, date_to_i=date_to_i)
    assert [date_to_i[pd.Timestamp(e.trigger_date)] for e in kept] == [0, 10]


# ───── sweep ──────────────────────────────────────────────────────────────

def test_reversion_sweep_shape_and_axes():
    df = _mk([101, 110, 104, 97, 100, 104])
    cells = reversion_sweep(
        df, row_axis="run", col_axis="horizon",
        row_values=[100, 120], col_values=[1, 2],
        reversal_account_pct=50, run_account_pct=100, horizon=2, leverage=10,
        gap=0, direction=DOWN_EXHAUSTION,
    )
    assert len(cells) == 4
    assert {(c.row, c.col) for c in cells} == {(0, 0), (0, 1), (1, 0), (1, 1)}
    # run=100·horizon=2 칸(row0,col1) = 단일 호출과 일치.
    target = next(c for c in cells if c.row == 0 and c.col == 1)
    direct = reversion_summary(reversion_events(
        df, reversal_account_pct=50, run_account_pct=100, horizon=2, leverage=10,
        direction=DOWN_EXHAUSTION,
    ))[DOWN_EXHAUSTION]
    assert target.n == direct["n"]
    assert target.mean_reversion == pytest.approx(direct["mean_reversion"])


def test_reversion_sweep_rejects_bad_axes():
    df = _mk([101, 110, 104, 99, 102, 106])
    with pytest.raises(ValueError):
        reversion_sweep(
            df, row_axis="run", col_axis="run", row_values=[100], col_values=[100],
            reversal_account_pct=50, run_account_pct=100, horizon=2, leverage=10,
            gap=0, direction=DOWN_EXHAUSTION,
        )
    with pytest.raises(ValueError):
        reversion_sweep(
            df, row_axis="bogus", col_axis="horizon", row_values=[1], col_values=[1],
            reversal_account_pct=50, run_account_pct=100, horizon=2, leverage=10,
            gap=0, direction=DOWN_EXHAUSTION,
        )

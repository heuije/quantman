"""진입 추세 분석(trend_analysis) + 신호 옵션(smooth_window/min_gap_days) 테스트.

핵심 게이트:
- 기본값(smooth_window=1, min_gap_days=0)은 기존 generate_signals와 byte-identical(골든 보존).
- trend_events의 past/forward 수익률·이벤트 수를 결정적으로 단언.
- trend_regression의 HAC 표준오차를 statsmodels와 1e-6 대조.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# 기존 core 테스트 컨벤션: 패키지 루트를 sys.path에 추가 (editable install 우회).
_CORE_DIR = Path(__file__).resolve().parent.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from quant_core.oil_futures import (  # noqa: E402
    ScanCell,
    Side,
    TrendEvent,
    TrendRegression,
    build_oil_trend_excel,
    generate_signals,
    trend_events,
    trend_explanatory_scan,
    trend_matched,
    trend_regression,
)


def _mk(closes, highs=None, lows=None) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="B"),
        "open": list(closes),
        "high": list(highs) if highs is not None else [c + 1 for c in closes],
        "low": list(lows) if lows is not None else [c - 1 for c in closes],
        "close": list(closes),
        "volume": [1000] * n,
    })


# ───── trend_events: 전체 영업일 ─────────────────────────────────────────

def test_trend_events_all_days_known_values():
    closes = [100, 102, 104, 106, 108, 110, 112, 114, 116, 118, 120, 122]
    df = _mk(closes)
    evs = trend_events(df, lookback=2, horizon=2)
    # t in range(2, 12-2) = 2..9 → 8 events
    assert len(evs) == 8
    e0 = evs[0]                                   # t=2
    assert e0.close == 104
    assert e0.past_return == pytest.approx(104 / 100 - 1)
    assert e0.forward_return == pytest.approx(108 / 104 - 1)
    last = evs[-1]                                # t=9
    assert last.close == 118
    assert last.past_return == pytest.approx(118 / 114 - 1)
    assert last.forward_return == pytest.approx(122 / 118 - 1)


def test_trend_events_window_boundaries():
    closes = list(range(100, 110))               # 10 pts
    df = _mk(closes)
    evs = trend_events(df, lookback=3, horizon=2)
    # range(3, 10-2) = 3..7 → 5 events
    assert len(evs) == 5
    assert evs[0].date == pd.Timestamp(df["date"].iloc[3])
    assert evs[-1].date == pd.Timestamp(df["date"].iloc[7])


def test_trend_events_empty_when_window_too_large():
    df = _mk(list(range(100, 105)))              # 5 pts
    assert trend_events(df, lookback=3, horizon=3) == []


# ───── trend_events: 신호 앵커 ───────────────────────────────────────────

def test_trend_events_signal_anchor_threshold():
    highs = [80, 82, 86, 84, 83, 82, 80, 81, 87, 85, 84, 83]
    closes = [80, 81, 85, 83, 82, 81, 80, 80, 86, 84, 83, 82]
    df = _mk(closes, highs=highs, lows=[78] * 12)
    # raw short@85 크로스: i=2 (86≥85>82), i=8 (87≥85>81)
    evs = trend_events(df, lookback=2, horizon=2, side=Side.SHORT, threshold=85)
    assert len(evs) == 2
    assert {e.date for e in evs} == {
        pd.Timestamp(df["date"].iloc[2]),
        pd.Timestamp(df["date"].iloc[8]),
    }
    # 앵커 이벤트의 past/forward도 그 인덱스 기준
    e_first = next(e for e in evs if e.date == pd.Timestamp(df["date"].iloc[2]))
    assert e_first.past_return == pytest.approx(85 / 80 - 1)      # close[2]/close[0]
    assert e_first.forward_return == pytest.approx(82 / 85 - 1)   # close[4]/close[2]


def test_trend_events_side_threshold_must_pair():
    df = _mk(list(range(100, 110)))
    with pytest.raises(ValueError):
        trend_events(df, lookback=2, horizon=2, side=Side.SHORT)   # threshold 누락


# ───── trend_regression: HAC vs statsmodels ─────────────────────────────

def test_trend_regression_hac_matches_statsmodels():
    sm = pytest.importorskip("statsmodels.api")
    rng = np.random.default_rng(0)
    closes = [100.0]
    for _ in range(280):
        closes.append(closes[-1] * (1 + rng.normal(0, 0.012)))
    df = _mk([round(c, 4) for c in closes])
    evs = trend_events(df, lookback=10, horizon=20)
    reg = trend_regression(evs, horizon=20)
    assert reg is not None and reg.n == len(evs)

    x = np.array([e.past_return for e in evs])
    y = np.array([e.forward_return for e in evs])
    res = sm.OLS(y, sm.add_constant(x)).fit(
        cov_type="HAC", cov_kwds={"maxlags": min(20, len(evs) - 1)}
    )
    assert reg.slope == pytest.approx(float(res.params[1]), abs=1e-9, rel=1e-9)
    assert reg.intercept == pytest.approx(float(res.params[0]), abs=1e-9, rel=1e-9)
    assert reg.hac_se == pytest.approx(float(res.bse[1]), abs=1e-9, rel=1e-6)


def test_trend_regression_none_when_few_events():
    evs = [TrendEvent(pd.Timestamp("2020-01-01"), 100.0, 0.0, 0.0)]
    assert trend_regression(evs, horizon=5) is None


def test_trend_regression_recovers_known_slope():
    # forward = 0.5 * past 정확한 선형 → slope≈0.5, r²≈1
    events = [
        TrendEvent(pd.Timestamp("2020-01-01") + pd.Timedelta(days=i),
                   100.0, p, 0.5 * p)
        for i, p in enumerate(np.linspace(-0.2, 0.2, 50))
    ]
    reg = trend_regression(events, horizon=5)
    assert reg is not None
    assert reg.slope == pytest.approx(0.5, abs=1e-9)
    assert reg.r_squared == pytest.approx(1.0, abs=1e-9)


# ───── 신호 옵션 (방법 1·3) ──────────────────────────────────────────────

def test_signal_options_default_byte_identical():
    highs = [80, 82, 86, 84, 83, 88, 80, 81, 87, 85]
    lows = [78, 74, 76, 79, 72, 73, 70, 69, 73, 77]
    closes = [80, 81, 85, 83, 80, 84, 78, 76, 84, 82]
    df = _mk(closes, highs=highs, lows=lows)
    base = generate_signals(df, short_thresholds=[85], long_thresholds=[72])
    explicit = generate_signals(
        df, short_thresholds=[85], long_thresholds=[72],
        smooth_window=1, min_gap_days=0,
    )
    assert base == explicit


def test_smooth_window_suppresses_one_day_spike():
    # 1일 스파이크(90) → 원시는 발화, 3일 평균은 임계 85 미달로 무발화.
    highs = [70, 71, 72, 90, 72, 71, 70, 71, 72, 73]
    closes = [70, 71, 72, 73, 72, 71, 70, 71, 72, 73]
    df = _mk(closes, highs=highs, lows=[60] * 10)
    raw = generate_signals(df, short_thresholds=[85])
    smoothed = generate_signals(df, short_thresholds=[85], smooth_window=3)
    assert len(raw) == 1
    assert smoothed == []


def test_min_gap_days_debounces_oscillation():
    # 임계 85를 i=1, i=3에서 두 번 교차 → 기본 2건, min_gap 5면 1건.
    highs = [70, 86, 70, 87, 70, 70, 70, 70]
    closes = [70, 80, 70, 80, 70, 70, 70, 70]
    df = _mk(closes, highs=highs, lows=[60] * 8)
    no_gap = generate_signals(df, short_thresholds=[85])
    gapped = generate_signals(df, short_thresholds=[85], min_gap_days=5)
    assert len(no_gap) == 2
    assert len(gapped) == 1
    assert gapped[0].date == pd.Timestamp(df["date"].iloc[1])   # 선발화 유지


def test_smooth_window_invalid_raises():
    df = _mk(list(range(100, 110)))
    with pytest.raises(ValueError):
        generate_signals(df, short_thresholds=[105], smooth_window=0)


# ───── trend_explanatory_scan: (L,H) 격자 설명력 ─────────────────────────

def test_explanatory_scan_grid_price_band_and_structure():
    closes = list(range(100, 150))               # 100..149, 50 pts (+1/일)
    df = _mk(closes)
    cells = trend_explanatory_scan(
        df, lookbacks=[2, 3], horizons=[2, 3],
        price_lo=110, price_hi=120, oos_split=0.5,
    )
    assert len(cells) == 4                        # 2×2 격자
    assert all(isinstance(c, ScanCell) for c in cells)
    by_key = {(c.lookback, c.horizon): c for c in cells}
    assert set(by_key) == {(2, 2), (2, 3), (3, 2), (3, 3)}
    # 종가 ∈ [110,120] = close[t]=100+t → t∈10..20 = 11건 (모든 L,H 동일, t범위 안).
    c = by_key[(2, 2)]
    assert c.n == 11
    assert not math.isnan(c.r_squared) and 0.0 <= c.r_squared <= 1.0
    assert not math.isnan(c.slope) and not math.isnan(c.intercept)
    # n=11 → train=6/test=5 (≥3) → OOS·부호안정성 계산됨.
    assert c.oos_r_squared is not None and c.oos_slope is not None
    assert isinstance(c.sign_stable, bool)


def test_explanatory_scan_draws_low_sample_down_to_n3():
    # 표본이 적어도(3≤n) 격자를 그린다 — 회귀 불가능한 n<3만 비운다(신뢰 임계는
    # 컴퓨트가 아니라 표시 관심사라 여기서 비우지 않음).
    closes = list(range(100, 150))
    df = _mk(closes)
    # 종가 ∈ [110,120] = 11건 — 옛 min_n=30 게이트면 비었을 저표본이지만 이제 그려진다.
    drawn = trend_explanatory_scan(df, [2], [2], 110, 120)
    assert len(drawn) == 1 and drawn[0].n == 11
    assert not math.isnan(drawn[0].r_squared) and not math.isnan(drawn[0].slope)
    # 종가 ∈ [110,111] = 2건(<3, 선 적합 불가) → 비움(nan·None).
    blank = trend_explanatory_scan(df, [2], [2], 110, 111)
    assert len(blank) == 1 and blank[0].n == 2
    assert math.isnan(blank[0].r_squared) and blank[0].oos_r_squared is None


def test_explanatory_scan_deterministic():
    closes = list(range(100, 150))
    df = _mk(closes)
    kw = dict(lookbacks=[2, 3, 4], horizons=[2, 5], price_lo=105, price_hi=140)
    a = trend_explanatory_scan(df, **kw)
    b = trend_explanatory_scan(df, **kw)
    key = lambda cs: [(c.lookback, c.horizon, c.n, c.r_squared, c.slope, c.sign_stable) for c in cs]
    assert key(a) == key(b)


# ───── trend_matched + 엑셀 내보내기 ─────────────────────────────────────

def test_trend_matched_band_filter_and_decluster():
    closes = list(range(100, 150))               # 100..149 (+1/일)
    df = _mk(closes)
    # gap=0: 종가 ∈ [110,120] = close[t]=100+t → t∈10..20 = 11건. 단조라 past_return 항상 양수
    # → 과거 증감율 범위 [0,100]%면 전부 통과.
    ev, raw = trend_matched(df, 2, 2, 110, 120, 0, 100, gap=0)
    assert raw == 11 and len(ev) == 11
    # gap=5: 11건이 연속일이라 5영업일 간격으로 솎이면 ⌈11/5⌉=3건(t=10,15,20).
    ev5, raw5 = trend_matched(df, 2, 2, 110, 120, 0, 100, gap=5)
    assert raw5 == 11 and len(ev5) == 3
    # 과거 증감율 범위로 추가 필터(단조상승이라 음수 구간은 0건).
    none_neg, _ = trend_matched(df, 2, 2, 110, 120, -100, -1, gap=0)
    assert none_neg == []


def test_build_oil_trend_excel_bytes():
    closes = list(range(100, 150))
    df = _mk(closes)
    ev, raw = trend_matched(df, 2, 2, 110, 120, 0, 100, gap=0)
    data = build_oil_trend_excel(
        ev, raw, lookback=2, horizon=2, price_lo=110, price_hi=120,
        change_lo=0, change_hi=100, gap=0, name="원유", price_sym="$",
    )
    assert isinstance(data, bytes) and len(data) > 0
    assert data[:2] == b"PK"                      # xlsx = zip 시그니처


def test_explanatory_scan_gap_declusters():
    closes = list(range(100, 150))
    df = _mk(closes)
    # 종가 ∈ [110,120] = 11 연속일. gap=0 → 11건, gap=5 → t=10,15,20 = 3건.
    no_gap = trend_explanatory_scan(df, [2], [2], 110, 120, gap=0)
    gapped = trend_explanatory_scan(df, [2], [2], 110, 120, gap=5)
    assert no_gap[0].n == 11
    assert gapped[0].n == 3

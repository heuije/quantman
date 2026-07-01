"""strategy_curve — 혼합전략 일일수익·자본곡선·스트릭 엔진 테스트."""
import numpy as np
import pandas as pd
import pytest

from quant_core.oil_futures.strategy_curve import (
    COLD_STREAK,
    HOT_STREAK,
    composite_strategy_daily,
    daily_return_streak_events,
    streak_summary,
    strategy_segments,
)

# 공용 6영업일 프레임(같은 날짜 — US/KR 휴장 정렬은 exact-제외 merge_asof로 t→t-1).
_DATES = pd.to_datetime(
    ["2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-08", "2020-01-09"]
)
# S&P 종가: 등락 교대 → 코스피 t 신호 = -sign(t-1 전일대비).
_SNP = pd.Series([3000, 3030, 3000, 3030, 3000, 3030.0], index=_DATES)


def _kospi(opens, closes):
    return pd.DataFrame({
        "date": _DATES, "open": opens, "high": closes, "low": opens, "close": closes,
    })


def test_signal_and_returns_leverage1():
    # open/close 설계로 r1·r2 손계산.
    kospi = _kospi(
        opens=[100, 100, 100, 100, 100, 100],
        closes=[100, 100, 90, 110, 90, 110.0],
    )
    df = composite_strategy_daily(kospi, _SNP, leverage=1.0)

    # 신호: D0·D1 결측(전전 세션 없음)→0, D2=숏(-1), D3=롱(+1), D4=숏, D5=롱.
    # r1 = s*(close/open-1): D2 숏·close<open → +0.1, D3 롱·close>open → +0.1
    r1 = df["r1"].to_numpy()
    assert r1[0] == pytest.approx(0.0) and r1[1] == pytest.approx(0.0)
    assert r1[2] == pytest.approx(0.1)      # 숏: -1*(90/100-1)=+0.1
    assert r1[3] == pytest.approx(0.1)      # 롱: +1*(110/100-1)=+0.1
    # r2 = open_t/close_{t-1}-1 (오버나이트 롱): D0 없음→0, D3 = 100/90-1
    r2 = df["r2"].to_numpy()
    assert r2[0] == pytest.approx(0.0)
    assert r2[3] == pytest.approx(100 / 90 - 1)
    assert r2[4] == pytest.approx(100 / 110 - 1)
    # idx_ret = r1+r2
    assert df["idx_ret"].to_numpy()[3] == pytest.approx(0.1 + (100 / 90 - 1))
    # leverage=1 → daily == idx_ret (청산 없음)
    assert np.allclose(df["daily_return"].to_numpy(), df["idx_ret"].to_numpy())
    # 자본곡선(복리): 손계산 최종값
    eq = df["close"].to_numpy()
    assert eq[0] == pytest.approx(1.0) and eq[2] == pytest.approx(1.1)
    assert eq[-1] == pytest.approx(1.0 * 1.0 * 1.1 * (1 + 0.1 + 100 / 90 - 1)
                                   * (1 + 0.1 + 100 / 110 - 1) * (1 + 0.1 + 100 / 90 - 1))
    assert not df["liquidated"].any()
    assert (df["segment"] == 0).all()


def test_leverage_scales_daily_return():
    kospi = _kospi([100, 100, 100, 100, 100, 100], [100, 100, 90, 110, 90, 110.0])
    d1 = composite_strategy_daily(kospi, _SNP, leverage=1.0)["idx_ret"].to_numpy()
    d5 = composite_strategy_daily(kospi, _SNP, leverage=5.0)["daily_return"].to_numpy()
    # 청산 안 걸리는 날은 daily = 5*idx_ret
    assert np.allclose(d5, np.maximum(-1.0, 5.0 * d1))


def test_liquidation_floor_and_segment_reset():
    # D2에서 롱인데 close 대폭락(85) → idx_ret≈-0.15, leverage=10 → daily=max(-1,-1.5)=-1 청산.
    # D2 신호=숏(-1)이라 하락이 +가 됨 → 롱 신호(D3) 위치를 폭락시키자.
    # D3=롱(+1): open=100, close=50 → r1=-0.5; r2=100/close_D2-1. close_D2 작게.
    kospi = _kospi(
        opens=[100, 100, 100, 100, 100, 100],
        closes=[100, 100, 100, 50, 100, 110.0],
    )
    df = composite_strategy_daily(kospi, _SNP, leverage=10.0)
    # D3: 롱, r1 = 50/100-1 = -0.5; r2 = 100/100-1 = 0 → idx_ret=-0.5; daily=max(-1,-5)=-1 청산.
    assert df["daily_return"].to_numpy()[3] == pytest.approx(-1.0)
    assert bool(df["liquidated"].to_numpy()[3]) is True
    assert df["close"].to_numpy()[3] == pytest.approx(0.0)     # 청산일 equity 0
    # 청산 후 세그먼트 증가 · 다음날 새 자본 1.0에서 재시작
    assert df["segment"].to_numpy()[3] == 0
    assert df["segment"].to_numpy()[4] == 1
    assert df["close"].to_numpy()[4] == pytest.approx(1.0 * (1 + df["daily_return"].to_numpy()[4]))

    segs = strategy_segments(df)
    assert len(segs) == 2                       # 청산 전/후 두 세그먼트
    assert not segs[0]["liquidated"].any() and not segs[1]["liquidated"].any()
    assert (segs[0]["close"] > 0).all() and (segs[1]["close"] > 0).all()


# ── 렌즈 B: 스트릭 엔진 ────────────────────────────────────────────────

def _dates(n):
    return pd.to_datetime(pd.date_range("2020-01-01", periods=n, freq="D")).to_numpy()


def test_streak_hot_cold_detection_and_forward():
    # 관측창 3일 평균이 +2% 이상=과열/-2% 이하=과냉. 이후 2일 누적수익.
    # d: [.03,.03,.03, .05,-.02, ...]; t=2(idx) 관측창 [.03,.03,.03] avg=3%>=2% → HOT.
    d = np.array([0.03, 0.03, 0.03, 0.05, -0.02, 0.01, 0.01, 0.01])
    dates = _dates(len(d))
    evs = daily_return_streak_events(d, dates, window=3, threshold_pct=2.0, horizon=2)
    assert len(evs) >= 1
    e0 = evs[0]
    assert e0.direction == HOT_STREAK
    assert e0.lookback_avg == pytest.approx(0.03)
    # 이후 2일(d[3],d[4]) 누적 = (1.05)(0.98)-1
    assert e0.fwd_return == pytest.approx(1.05 * 0.98 - 1.0)
    assert e0.liquidated is False


def test_streak_non_overlapping_windows():
    # 계속 과열이면 신호 후 horizon만큼 건너뛰어 forward 윈도우가 겹치지 않아야.
    d = np.full(12, 0.05)          # 항상 평균 5% >= 2% (과열)
    dates = _dates(12)
    evs = daily_return_streak_events(d, dates, window=2, threshold_pct=2.0, horizon=3)
    # 첫 신호 t=1(window-1), 이후 t += 3 → 1,4,7 ... forward [t+1..t+3] 비겹침
    idxs = [np.where(dates == np.datetime64(e.signal_date))[0][0] for e in evs]
    for a, b in zip(idxs, idxs[1:]):
        assert b - a >= 3          # 최소 horizon 간격


def test_streak_cold_and_liquidation():
    # 과냉 신호 후 forward에 -100% 하루 → fwd_return=-1, liquidated True.
    d = np.array([-0.03, -0.03, -0.03, -1.0, 0.5, 0.0, 0.0])
    dates = _dates(len(d))
    evs = daily_return_streak_events(d, dates, window=3, threshold_pct=2.0, horizon=2)
    assert evs[0].direction == COLD_STREAK
    assert evs[0].fwd_return == pytest.approx(-1.0)     # (1-1)(1.5)-1 = -1
    assert evs[0].liquidated is True


def test_streak_direction_filter_and_summary():
    d = np.array([0.03, 0.03, 0.03, 0.01, -0.03, -0.03, -0.03, 0.01, 0.0, 0.0])
    dates = _dates(len(d))
    hot = daily_return_streak_events(d, dates, window=3, threshold_pct=2.0, horizon=2, direction=HOT_STREAK)
    assert hot and all(e.direction == HOT_STREAK for e in hot)
    allev = daily_return_streak_events(d, dates, window=3, threshold_pct=2.0, horizon=2)
    summ = streak_summary(allev)
    assert set(summ) == {HOT_STREAK, COLD_STREAK}
    # 요약의 표본수·평균이 이벤트 집계와 일치.
    for dir_name, blk in summ.items():
        evs = [e for e in allev if e.direction == dir_name]
        assert blk["n"] == len(evs)
        if evs:
            assert blk["mean_fwd"] == pytest.approx(sum(e.fwd_return * 100 for e in evs) / len(evs))
            assert blk["success_rate"] == pytest.approx(
                100.0 * sum(1 for e in evs if e.fwd_return > 0) / len(evs))


def test_streak_empty_when_no_signal():
    d = np.zeros(10)               # 평균 0 → 임계 미달
    evs = daily_return_streak_events(d, _dates(10), window=3, threshold_pct=2.0, horizon=2)
    assert evs == []
    summ = streak_summary([])
    assert summ[HOT_STREAK]["n"] == 0 and np.isnan(summ[HOT_STREAK]["mean_fwd"])

"""진입 직전 추세 → 미래 수익률 분석 (인터랙티브 탐색기 백엔드).

각 시점 t에서:
    past_return    = close[t] / close[t-lookback] - 1     (진입 직전 추세)
    forward_return = close[t+horizon] / close[t] - 1       (이후 수익률)

이벤트는 두 모드로 뽑는다:
- 전체 영업일(베이스라인): lookback ≤ t < n-horizon 인 모든 t.
- 신호 앵커(임계별): generate_signals 의 크로스일 t (임계값별로 추세-수익 관계가 다른지 확인).

forward~past OLS + Newey-West(HAC) 회귀로 추세가 미래 수익을 예측하는지 측정한다.

정직한 한계:
- forward 는 close-to-close 서술용 — 실제 백테스트(익일 시가 진입·비용·SL/TP)와 다르다.
  추세-수익 *관계 측정*이지 거래 손익이 아니다.
- forward 윈도우가 겹쳐 자기상관 → 일반 OLS 표준오차가 과소. HAC(maxlags=horizon)로 보정.
- 회귀는 numpy + 표준 라이브러리(math)만 사용 — 런타임은 scipy/statsmodels 비의존.
  테스트가 statsmodels HAC 와 1e-6 대조해 정합성을 보장한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .signals import Side, generate_signals


@dataclass(frozen=True)
class TrendEvent:
    """한 시점의 (진입 직전 추세, 이후 수익률) 쌍."""

    date: pd.Timestamp
    close: float
    past_return: float      # close[t] / close[t-lookback] - 1
    forward_return: float   # close[t+horizon] / close[t] - 1


@dataclass(frozen=True)
class TrendRegression:
    """forward_return ~ past_return 단변량 OLS + Newey-West(HAC) 결과."""

    slope: float            # β: 추세 1단위 증가당 미래수익 변화
    intercept: float
    r_squared: float
    n: int
    hac_se: float           # Newey-West(Bartlett, maxlags=horizon) slope 표준오차
    hac_t_stat: float       # slope / hac_se
    hac_p_value: float      # 2·(1-Φ(|t|)), Φ=정규근사(math.erf)


def trend_events(
    df: pd.DataFrame,
    lookback: int,
    horizon: int,
    side: Side | None = None,
    threshold: float | None = None,
    smooth_window: int = 1,
    min_gap_days: int = 0,
) -> list[TrendEvent]:
    """df → TrendEvent 리스트.

    side·threshold 를 함께 주면 신호 크로스일만(임계별), 둘 다 생략하면 전체 영업일.
    smooth_window·min_gap_days 는 신호 앵커 모드에서 generate_signals 에 전달된다
    (전체 영업일 모드에선 무의미 — 무시).
    """
    if lookback < 1:
        raise ValueError("lookback은 1 이상이어야 함")
    if horizon < 1:
        raise ValueError("horizon은 1 이상이어야 함")
    if (side is None) != (threshold is None):
        raise ValueError("side와 threshold는 함께 지정하거나 함께 생략해야 함")

    close = df["close"].to_numpy()
    date = df["date"].to_numpy()
    n = len(df)

    if side is None:
        # 전체 영업일: 과거·미래 윈도우가 둘 다 존재하는 t.
        idxs: list[int] = list(range(lookback, n - horizon))
    else:
        short_th = [float(threshold)] if side == Side.SHORT else []
        long_th = [float(threshold)] if side == Side.LONG else []
        sigs = generate_signals(
            df, short_thresholds=short_th, long_thresholds=long_th,
            smooth_window=smooth_window, min_gap_days=min_gap_days,
        )
        date_to_idx = {pd.Timestamp(d): i for i, d in enumerate(date)}
        idxs = []
        for s in sigs:
            i = date_to_idx.get(s.date)
            if i is not None and lookback <= i < n - horizon:
                idxs.append(i)

    events: list[TrendEvent] = []
    for i in idxs:
        c0 = float(close[i])
        cp = float(close[i - lookback])
        cf = float(close[i + horizon])
        if cp <= 0 or c0 <= 0:   # prepare_wti가 종가>0을 보장하나 방어적.
            continue
        events.append(TrendEvent(
            date=pd.Timestamp(date[i]),
            close=c0,
            past_return=c0 / cp - 1.0,
            forward_return=cf / c0 - 1.0,
        ))
    return events


def _normal_cdf(z: float) -> float:
    """표준정규 누적분포 Φ(z) — math.erf 기반(scipy 비의존)."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def trend_regression(events: list[TrendEvent], horizon: int) -> TrendRegression | None:
    """forward_return ~ past_return OLS + Newey-West HAC. n<3 이면 None.

    HAC 공분산 (Bartlett 커널, maxlags=horizon — statsmodels .fit(cov_type="HAC") 일치):
        Var(β) = (XᵀX)⁻¹ S (XᵀX)⁻¹
        S = Σ uₜuₜᵀ + Σ_{L=1}^{maxlags} (1-L/(maxlags+1))·(Γ_L + Γ_Lᵀ),  uₜ=xₜ·eₜ
    (statsmodels get_robustcov_results의 HAC 경로는 use_correction 기본 False — 소표본
     n/(n-k) 보정을 적용하지 않는다. 동일 정의로 맞춤.)
    """
    n = len(events)
    if n < 3:
        return None

    x = np.array([e.past_return for e in events], dtype=float)
    y = np.array([e.forward_return for e in events], dtype=float)
    X = np.column_stack([np.ones(n), x])

    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    intercept, slope = float(beta[0]), float(beta[1])
    resid = y - X @ beta
    ss_res = float(resid @ resid)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r_squared = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # Newey-West HAC 공분산
    xtx_inv = np.linalg.inv(X.T @ X)
    maxlags = min(int(horizon), n - 1)
    u = X * resid[:, None]                 # n×2 score 기여
    S = u.T @ u
    for lag in range(1, maxlags + 1):
        w = 1.0 - lag / (maxlags + 1.0)
        gamma = u[lag:].T @ u[:-lag]
        S += w * (gamma + gamma.T)
    cov = xtx_inv @ S @ xtx_inv

    var_slope = float(cov[1, 1])
    hac_se = math.sqrt(var_slope) if var_slope > 0 else 0.0
    t_stat = slope / hac_se if hac_se > 0 else 0.0
    p_value = 2.0 * (1.0 - _normal_cdf(abs(t_stat)))

    return TrendRegression(
        slope=slope,
        intercept=intercept,
        r_squared=r_squared,
        n=n,
        hac_se=hac_se,
        hac_t_stat=float(t_stat),
        hac_p_value=float(p_value),
    )

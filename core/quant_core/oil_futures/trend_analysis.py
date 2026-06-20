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


@dataclass(frozen=True)
class ScanCell:
    """한 (lookback, horizon) 칸의 설명력 + 표본 밖(OOS) 안정성."""

    lookback: int
    horizon: int
    n: int                        # 종가범위 조건 후 표본수
    r_squared: float              # in-sample R² (forward~past, 종가범위 조건). n<3(회귀 불가) 시 nan
    slope: float                  # in-sample 기울기 β. n<3 시 nan
    intercept: float
    hac_p_value: float            # HAC p (겹침 자기상관 보정)
    oos_r_squared: float | None   # test 반쪽 R² (회귀 불가 시 None)
    oos_slope: float | None       # test 반쪽 기울기
    sign_stable: bool             # train·test 기울기 동부호 (둘 다 회귀 가능)
    mean_forward: float           # 매칭(디클러스터) 건의 평균 향후 H일 증감율 (%). n==0이면 nan
    up_ratio: float               # 향후 증감율>0 비율 (%). n==0이면 nan


def _decluster_by_gap(events: list[TrendEvent], gap: int, pos: dict) -> list[TrendEvent]:
    """G영업일 이내 연속/겹친 이벤트는 1건만(그리디). events 는 날짜 오름차순 가정.
    pos = {Timestamp: 영업일 인덱스} (영업일 간격 계산용)."""
    if gap <= 0:
        return events
    kept: list[TrendEvent] = []
    last = -(10 ** 9)
    for e in events:
        i = pos.get(pd.Timestamp(e.date))
        if i is None:
            continue
        if i - last >= gap:
            kept.append(e)
            last = i
    return kept


def trend_explanatory_scan(
    df: pd.DataFrame,
    lookbacks: list[int],
    horizons: list[int],
    price_lo: float,
    price_hi: float,
    oos_split: float = 0.5,
    gap: int = 0,
) -> list[ScanCell]:
    """(L,H) 격자 × 종가범위 조건 → 칸마다 forward~past 설명력 + OOS 안정성.

    각 칸:
      - events = trend_events(df, L, H) 중 종가 ∈ [price_lo, price_hi] 인 것만
        (gap>0이면 G영업일 디클러스터 — 겹침·레짐집중 표본 과대평가 방지, 결과/음영과 일관).
      - in-sample 회귀: trend_regression → r2/slope/intercept/hac_p.
      - OOS: events 를 날짜순(오름차순 보장) 앞 oos_split / 뒤로 나눠 각각 회귀 →
        test 의 r2·slope, train·test 기울기 동부호면 sign_stable.

    **표본 하한 = 회귀의 통계적 최소(n≥3)뿐** — 선을 적합할 수 없는 n<3만 비운다
    (r_squared=nan). 표본이 적어도(3≤n) 격자를 그려 사용자가 전체 형상을 보게 한다.
    "신뢰 임계"(예 n<30 저신뢰)는 컴퓨트가 아니라 표시 관심사이므로 여기서 비우지
    않고 호출측(서버 응답 min_n + 웹 저신뢰 음영·경고)이 판단한다.

    설명력은 **종가범위만** 조건으로 한다 — 증감율 밴드를 넣으면 x축이 절단돼
    R²·기울기 추정이 불안정해진다(범위 절단 편향). 증감율 밴드는 호출측에서
    '질문 지점'으로만 쓴다.

    ⚠ 데이터 스누핑: 여러 칸을 스캔해 R² 최댓값(또는 p 최솟값) 한 칸을 고르면
    과최적화(다중비교)다. 호출측은 (1) 전체 격자를 보여 고-R²가 연속영역인지,
    (2) sign_stable·OOS·표본수로 견고성을 판단하게 하고, 다중비교·저표본 경고를 노출해야 한다.
    """
    cells: list[ScanCell] = []
    pos = {pd.Timestamp(d): i for i, d in enumerate(df["date"].to_numpy())} if gap > 0 else {}
    for L in lookbacks:
        for H in horizons:
            if L < 1 or H < 1:
                continue
            evs = [e for e in trend_events(df, int(L), int(H))
                   if price_lo <= e.close <= price_hi]
            evs = _decluster_by_gap(evs, int(gap), pos)
            n = len(evs)
            # 평균 향후수익률·상승비율은 회귀 불가(n<3)여도 n≥1이면 계산 — 수익률 지표용.
            fwd = [e.forward_return * 100.0 for e in evs]
            mean_fwd = (sum(fwd) / n) if n else float("nan")
            up_ratio = (100.0 * sum(1 for v in fwd if v > 0) / n) if n else float("nan")
            if n < 3:
                cells.append(ScanCell(
                    lookback=int(L), horizon=int(H), n=n,
                    r_squared=float("nan"), slope=float("nan"),
                    intercept=float("nan"), hac_p_value=float("nan"),
                    oos_r_squared=None, oos_slope=None, sign_stable=False,
                    mean_forward=mean_fwd, up_ratio=up_ratio,
                ))
                continue
            reg = trend_regression(evs, int(H))
            # OOS: 날짜순 앞쪽 train / 뒤쪽 test (evs 는 trend_events 가 오름차순 생성).
            k = int(round(n * oos_split))
            reg_tr = trend_regression(evs[:k], int(H))
            reg_te = trend_regression(evs[k:], int(H))
            sign_stable = bool(reg_tr and reg_te and reg_tr.slope * reg_te.slope > 0)
            cells.append(ScanCell(
                lookback=int(L), horizon=int(H), n=n,
                r_squared=(reg.r_squared if reg else float("nan")),
                slope=(reg.slope if reg else float("nan")),
                intercept=(reg.intercept if reg else float("nan")),
                hac_p_value=(reg.hac_p_value if reg else float("nan")),
                oos_r_squared=(reg_te.r_squared if reg_te else None),
                oos_slope=(reg_te.slope if reg_te else None),
                sign_stable=sign_stable,
                mean_forward=mean_fwd, up_ratio=up_ratio,
            ))
    return cells


@dataclass(frozen=True)
class SweepCell:
    """2D 스윕 한 칸 — 행/열 축 값 인덱스 + 표본·평균향후·상승비율·R²."""

    row: int                  # 행 축 값 인덱스
    col: int                  # 열 축 값 인덱스
    n: int
    mean_forward: float       # 평균 향후 H일 증감율 (%). n==0이면 nan
    up_ratio: float           # 향후 증감율>0 비율 (%). n==0이면 nan
    r_squared: float          # forward~past R². n<3이면 nan


SWEEP_AXES = ("close", "change", "lookback", "horizon")


def trend_sweep_2d(
    df: pd.DataFrame,
    *,
    row_axis: str,
    col_axis: str,
    row_values: list,         # close/change 축=(lo,hi) 밴드 / lookback/horizon 축=정수
    col_values: list,
    lookback: int,            # 스윕 안 하는 축의 고정값
    horizon: int,
    price_lo: float,
    price_hi: float,
    change_lo: float,
    change_hi: float,
    gap: int = 0,
) -> list[SweepCell]:
    """4축{close·change·lookback·horizon} 중 2축을 격자로 스윕 → 칸마다 표본·평균향후·상승비율·R².

    스윕 안 하는 2축은 고정값(lookback/horizon/price_*/change_*). close/change 축 값은 (lo,hi)
    밴드, lookback/horizon 축 값은 정수. 셀 = 종가밴드 ∧ 증감율밴드 매칭 → gap 디클러스터 →
    지표((L,H) 설명력 지도와 동일 파이프라인의 4축 일반화). 값 하나씩 넣지 않고 한눈에 비교용.
    """
    if row_axis not in SWEEP_AXES or col_axis not in SWEEP_AXES:
        raise ValueError(f"축은 {SWEEP_AXES} 중 하나")
    if row_axis == col_axis:
        raise ValueError("행 축과 열 축은 달라야 함")
    pos = {pd.Timestamp(d): i for i, d in enumerate(df["date"].to_numpy())} if gap > 0 else {}
    ev_cache: dict[tuple[int, int], list] = {}

    def _events(L: int, H: int) -> list:
        key = (L, H)
        if key not in ev_cache:
            ev_cache[key] = trend_events(df, L, H)   # (L,H) 고정 축이면 1회만 계산
        return ev_cache[key]

    cells: list[SweepCell] = []
    for ri, rv in enumerate(row_values):
        for ci, cv in enumerate(col_values):
            L, H = int(lookback), int(horizon)
            clo, chi, chlo, chhi = price_lo, price_hi, change_lo, change_hi
            for axis, val in ((row_axis, rv), (col_axis, cv)):
                if axis == "lookback":
                    L = int(val)
                elif axis == "horizon":
                    H = int(val)
                elif axis == "close":
                    clo, chi = float(val[0]), float(val[1])
                else:  # change
                    chlo, chhi = float(val[0]), float(val[1])
            if L < 1 or H < 1:
                cells.append(SweepCell(ri, ci, 0, float("nan"), float("nan"), float("nan")))
                continue
            evs = [e for e in _events(L, H)
                   if clo <= e.close <= chi and chlo <= e.past_return * 100.0 <= chhi]
            evs = _decluster_by_gap(evs, int(gap), pos)
            n = len(evs)
            fwd = [e.forward_return * 100.0 for e in evs]
            mean_fwd = (sum(fwd) / n) if n else float("nan")
            up_r = (100.0 * sum(1 for v in fwd if v > 0) / n) if n else float("nan")
            r2 = float("nan")
            if n >= 3:
                reg = trend_regression(evs, H)
                r2 = reg.r_squared if reg else float("nan")
            cells.append(SweepCell(ri, ci, n, mean_fwd, up_r, r2))
    return cells


def trend_matched(
    df: pd.DataFrame,
    lookback: int,
    horizon: int,
    price_lo: float,
    price_hi: float,
    change_lo: float,
    change_hi: float,
    gap: int = 0,
) -> tuple[list[TrendEvent], int]:
    """종가범위 ∧ 과거증감율범위(%) 매칭 이벤트를 G영업일 디클러스터(웹 탐색기와 동일 정의).

    trend_events(전체 영업일) → close∈[price_lo,price_hi] ∧ past_return*100∈[change_lo,change_hi]
    필터 → G영업일 이내 연속/겹친 매칭은 1건만(독립 표본). 반환: (declustered, raw_n).
    엑셀 내보내기·서버 집계가 화면 결과와 일치하도록 쓰는 공유 헬퍼.
    """
    evs = trend_events(df, int(lookback), int(horizon))
    matched = [
        e for e in evs
        if price_lo <= e.close <= price_hi
        and change_lo <= e.past_return * 100.0 <= change_hi
    ]
    raw_n = len(matched)
    if gap <= 0:
        return matched, raw_n
    pos = {pd.Timestamp(d): i for i, d in enumerate(df["date"].to_numpy())}
    return _decluster_by_gap(matched, int(gap), pos), raw_n

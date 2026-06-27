"""급등락 → 회귀(REVERSION) 분석 — 트레일링 피벗 후 평균회귀 탐색기.

기존 `trend_analysis`(고정 윈도우 past→forward)와 달리 **이벤트 구동**이다:

  1. 추세 시작(피벗)  — ZigZag 트레일링. running 극점에서 `전환임계`만큼 역행하면
     그 극점을 추세 전환점(피벗)으로 잠근다.
  2. 급등락 완성(트리거) — 피벗 대비 누적이 `급등락임계`(±)에 처음 도달하는 날.
  3. 회귀 측정         — 그 시점에 **역추세 진입**(하락소진→롱 / 상승소진→숏)했다고 보고,
     `N영업일` 후 계좌 수익률(되돌림=회귀)을 측정.

선물이므로 **모든 % 는 레버리지 반영 계좌 기준**이다. 계좌수익률 = 지수수익률 × 레버리지
(고정계약·정액명목 → 선형). 엔진은 내부적으로 지수 가격에서 `÷레버리지`로 환산해 같은
이벤트를 잡고(전환·급등락 임계), 결과는 `×레버리지`로 계좌화한다.

**청산(마진콜):** 트리거 진입 후 N일 안에 일봉 종가 경로가 역행으로 계좌 −100%(증거금 전손)에
닿으면 청산으로 본다(전손 = reversion −100%). 청산가는 종가 경로·전손 기준의 단순 정의 —
유지증거금/일중 저점이면 더 일찍 청산될 수 있어 보수적이지 않을 수 있다(서술용 한계).

정직한 한계:
- 종가 기반 피벗·종가-종가 forward — 실제 백테스트(비용·익일시가·SL/TP) 아님. 관계 측정.
- 트리거·forward는 PIT-clean(미래 미참조)이나 피벗 가격은 과거 running 극점(트레일링 특성).
- 기술통계(평균·중앙값·비율)만 — 겹침 자기상관 통계검정은 안 함(회귀 탐색기와 역할 분리).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

DOWN_EXHAUSTION = "down_exhaustion"   # 하락소진 → 롱(반등 베팅)
UP_EXHAUSTION = "up_exhaustion"       # 상승소진 → 숏(되돌림 베팅)
_DIRECTIONS = (DOWN_EXHAUSTION, UP_EXHAUSTION)


@dataclass(frozen=True)
class Pivot:
    """ZigZag 트레일링 피벗(고/저 교대)."""

    date: pd.Timestamp
    price: float
    kind: str               # "high" | "low"


@dataclass(frozen=True)
class ReversionEvent:
    """피벗→트리거→N일 회귀 한 건. 모든 수익률은 레버리지 반영 계좌 기준."""

    pivot_date: pd.Timestamp
    pivot_price: float
    trigger_date: pd.Timestamp
    trigger_price: float
    direction: str          # DOWN_EXHAUSTION(→롱) | UP_EXHAUSTION(→숏)
    run_account: float      # 트리거 시점 누적(계좌, 부호 상승+/하락-) = lev*(trigger/pivot-1)
    reversion_account: float  # N일 후 역추세 포지션 계좌수익률(청산 시 -1.0). +면 회귀, -면 추세지속
    liquidated: bool
    liquidation_day: int | None   # 청산까지 영업일(트리거 후). 청산 안 했으면 None


def find_pivots(df: pd.DataFrame, *, reversal_account_pct: float, leverage: float) -> list[Pivot]:
    """ZigZag 트레일링 피벗(종가 기준). 고/저 교대 리스트(날짜 ASC).

    계좌 reversal% → 지수 reversal `r = reversal_account_pct / leverage / 100`.
    running 고점에서 종가가 `r`만큼 하락하면 그 고점을 high 피벗 확정·하락 전환(대칭).
    """
    if reversal_account_pct <= 0:
        raise ValueError("reversal_account_pct는 0보다 커야 함")
    if leverage < 1:
        raise ValueError("leverage는 1 이상이어야 함")

    closes = df["close"].to_numpy()
    dates = df["date"].to_numpy()
    n = len(df)
    if n < 2:
        return []

    r = reversal_account_pct / leverage / 100.0
    pivots: list[Pivot] = []

    # 미확정(trend=0) 구간엔 고·저를 동시에 추적, 먼저 r 역행을 트리거하는 쪽이 첫 피벗.
    max_i, max_p = 0, float(closes[0])
    min_i, min_p = 0, float(closes[0])
    trend = 0   # 0=미확정, +1=상승레그(고점 추적), -1=하락레그(저점 추적)

    for i in range(1, n):
        c = float(closes[i])
        if trend >= 0 and c > max_p:
            max_p, max_i = c, i
        if trend <= 0 and c < min_p:
            min_p, min_i = c, i

        if trend in (0, 1) and max_p > 0 and c <= max_p * (1.0 - r):
            pivots.append(Pivot(date=pd.Timestamp(dates[max_i]), price=max_p, kind="high"))
            trend = -1
            min_p, min_i = c, i           # 하락레그: 현재부터 저점 추적
        elif trend in (0, -1) and min_p > 0 and c >= min_p * (1.0 + r):
            pivots.append(Pivot(date=pd.Timestamp(dates[min_i]), price=min_p, kind="low"))
            trend = +1
            max_p, max_i = c, i           # 상승레그: 현재부터 고점 추적

    return pivots


def _decluster_events_by_gap(
    events: list[ReversionEvent], gap: int, date_to_i: dict
) -> list[ReversionEvent]:
    """방향별로 G영업일 이내 연속 트리거는 1건만(그리디·선발화 유지). events는 트리거 ASC 가정."""
    by_dir: dict[str, list[ReversionEvent]] = {}
    for e in events:
        by_dir.setdefault(e.direction, []).append(e)
    kept: list[ReversionEvent] = []
    for evs in by_dir.values():
        last = -(10 ** 9)
        for e in evs:
            ti = date_to_i[pd.Timestamp(e.trigger_date)]
            if ti - last >= gap:
                kept.append(e)
                last = ti
    kept.sort(key=lambda e: e.trigger_date)
    return kept


def reversion_events(
    df: pd.DataFrame,
    *,
    reversal_account_pct: float,
    run_account_pct: float,
    horizon: int,
    leverage: float,
    gap: int = 0,
    direction: str | None = None,
) -> list[ReversionEvent]:
    """피벗별 leg(피벗→다음 피벗)에서 |계좌누적|이 run_account_pct에 처음 도달하는 날=트리거.

    트리거에서 역추세 진입(하락소진→롱·상승소진→숏)의 N일 후 계좌수익률(청산 반영) 측정.
    direction(DOWN_EXHAUSTION/UP_EXHAUSTION) 지정 시 그 방향만. gap>0이면 트리거 G영업일 디클러스터.
    """
    if run_account_pct <= 0:
        raise ValueError("run_account_pct는 0보다 커야 함")
    if horizon < 1:
        raise ValueError("horizon은 1 이상이어야 함")
    if leverage < 1:
        raise ValueError("leverage는 1 이상이어야 함")
    if gap < 0:
        raise ValueError("gap은 0 이상이어야 함")
    if direction is not None and direction not in _DIRECTIONS:
        raise ValueError(f"direction은 {_DIRECTIONS} 중 하나 또는 None")

    closes = df["close"].to_numpy()
    dates = df["date"].to_numpy()
    n = len(df)
    pivots = find_pivots(df, reversal_account_pct=reversal_account_pct, leverage=leverage)
    if not pivots:
        return []

    run_idx = run_account_pct / leverage / 100.0     # 지수 기준 누적 임계
    date_to_i = {pd.Timestamp(d): k for k, d in enumerate(dates)}
    events: list[ReversionEvent] = []

    for p_idx, piv in enumerate(pivots):
        pi = date_to_i[pd.Timestamp(piv.date)]
        leg_end = (date_to_i[pd.Timestamp(pivots[p_idx + 1].date)]
                   if p_idx + 1 < len(pivots) else n - 1)
        is_down = piv.kind == "high"
        dir_name = DOWN_EXHAUSTION if is_down else UP_EXHAUSTION
        if direction is not None and direction != dir_name:
            continue
        if piv.price <= 0:
            continue

        # 트리거: leg 안에서 |지수 누적|이 run_idx에 처음 도달.
        trig = None
        for t in range(pi + 1, leg_end + 1):
            cum = float(closes[t]) / piv.price - 1.0
            if (cum <= -run_idx) if is_down else (cum >= run_idx):
                trig = t
                break
        if trig is None or trig + horizon >= n:   # 완전한 forward 윈도우 필요
            continue

        # 역추세 포지션: 하락소진→롱(+1), 상승소진→숏(-1).
        pos = 1.0 if is_down else -1.0
        trig_price = float(closes[trig])
        liquidated, liq_day, reversion = False, None, None
        for k in range(1, horizon + 1):
            ret = pos * leverage * (float(closes[trig + k]) / trig_price - 1.0)
            if ret <= -1.0:
                liquidated, liq_day, reversion = True, k, -1.0
                break
        if not liquidated:
            reversion = pos * leverage * (float(closes[trig + horizon]) / trig_price - 1.0)

        events.append(ReversionEvent(
            pivot_date=pd.Timestamp(piv.date), pivot_price=piv.price,
            trigger_date=pd.Timestamp(dates[trig]), trigger_price=trig_price,
            direction=dir_name,
            run_account=leverage * (trig_price / piv.price - 1.0),
            reversion_account=float(reversion), liquidated=liquidated, liquidation_day=liq_day,
        ))

    if gap > 0:
        events = _decluster_events_by_gap(events, int(gap), date_to_i)
    return events


def _median(sorted_vals: list[float]) -> float:
    m = len(sorted_vals)
    if m == 0:
        return float("nan")
    mid = m // 2
    if m % 2:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2.0


def reversion_summary(events: list[ReversionEvent]) -> dict:
    """방향별(DOWN_EXHAUSTION·UP_EXHAUSTION) 요약 — 모든 % 계좌 기준.

    {n, mean_reversion, median_reversion, success_rate, liquidation_rate}.
    평균·중앙값에 청산(-100%) 포함. success = reversion_account>0 비율(청산은 실패).
    """
    out: dict[str, dict] = {}
    for dir_name in _DIRECTIONS:
        evs = [e for e in events if e.direction == dir_name]
        n = len(evs)
        if n == 0:
            out[dir_name] = {
                "n": 0, "mean_reversion": float("nan"), "median_reversion": float("nan"),
                "success_rate": float("nan"), "liquidation_rate": float("nan"),
            }
            continue
        rev = sorted(e.reversion_account * 100.0 for e in evs)
        out[dir_name] = {
            "n": n,
            "mean_reversion": sum(rev) / n,
            "median_reversion": _median(rev),
            "success_rate": 100.0 * sum(1 for e in evs if e.reversion_account > 0) / n,
            "liquidation_rate": 100.0 * sum(1 for e in evs if e.liquidated) / n,
        }
    return out


@dataclass(frozen=True)
class ReversionSweepCell:
    """2D 스윕 한 칸 — 한 방향의 요약 지표(계좌 %)."""

    row: int
    col: int
    n: int
    mean_reversion: float    # n==0이면 nan
    success_rate: float      # n==0이면 nan
    liquidation_rate: float  # n==0이면 nan


REVERSION_SWEEP_AXES = ("reversal", "run", "horizon")


def reversion_sweep(
    df: pd.DataFrame,
    *,
    row_axis: str,
    col_axis: str,
    row_values: list,
    col_values: list,
    reversal_account_pct: float,
    run_account_pct: float,
    horizon: int,
    leverage: float,
    gap: int,
    direction: str,
) -> list[ReversionSweepCell]:
    """{reversal·run·horizon} 중 2축 격자 스윕(나머지 고정) → 칸마다 한 방향 요약 지표.

    값 하나씩 넣지 않고 "어느 (임계, N일) 조합에서 회귀가 큰가"를 한눈에 비교.
    """
    if row_axis not in REVERSION_SWEEP_AXES or col_axis not in REVERSION_SWEEP_AXES:
        raise ValueError(f"축은 {REVERSION_SWEEP_AXES} 중 하나")
    if row_axis == col_axis:
        raise ValueError("행 축과 열 축은 달라야 함")
    if direction not in _DIRECTIONS:
        raise ValueError(f"direction은 {_DIRECTIONS} 중 하나")

    cells: list[ReversionSweepCell] = []
    for ri, rv in enumerate(row_values):
        for ci, cv in enumerate(col_values):
            rev_pct, run_pct, hz = reversal_account_pct, run_account_pct, horizon
            for axis, val in ((row_axis, rv), (col_axis, cv)):
                if axis == "reversal":
                    rev_pct = float(val)
                elif axis == "run":
                    run_pct = float(val)
                else:
                    hz = int(val)
            evs = reversion_events(
                df, reversal_account_pct=rev_pct, run_account_pct=run_pct,
                horizon=hz, leverage=leverage, gap=gap, direction=direction,
            )
            s = reversion_summary(evs)[direction]
            cells.append(ReversionSweepCell(
                ri, ci, s["n"], s["mean_reversion"], s["success_rate"], s["liquidation_rate"],
            ))
    return cells

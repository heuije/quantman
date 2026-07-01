"""코스피200선물 혼합전략의 일일 실현수익·자본곡선·스트릭 분석 (REVERSION 코스피 전용).

REVERSION 대시보드의 코스피200 기준 시리즈를 원지수 가격 대신 이 **혼합전략**으로 대체한다.

전략(둘을 동시 운용):
  전략1(S&P 신호 당일매매): 직전 미국 S&P500 세션이 **하락 마감** → 코스피200선물 시가매수·종가매도(당일 롱);
    **상승 마감** → 시가매도·종가청산(당일 숏).  부호 s(t) = -sign(S&P 전일대비).
    r1(t) = s(t) * (close_t / open_t - 1)                       # 지수 기준 당일 수익
  전략2(오버나이트 롱): 매일 종가매수·익일 시가매도.
    r2(t) = open_t / close_{t-1} - 1                            # 지수 기준 오버나이트 수익
  두 다리는 시간상 인접(오버나이트→당일)하나 각자 1단위 명목의 별개 북 → 일별 지수수익 합산:
    idx_ret(t) = r1(t) + r2(t)

선물이므로 **레버리지 N배**: 일일 실현 계좌수익 d(t) = max(-1, N * idx_ret(t)).
  floor(-1) = 하루에 원금 이상 잃을 수 없음(강제청산). 자본곡선은 d를 복리 누적하되,
  청산일(d=-1)엔 계좌 전손 → **다음날 새 자본 1.0으로 재시작**(세그먼트 분할). 전 구간 생존.

S&P 신호 정렬: 코스피 date t 직전에 **끝난** 마지막 미국 세션의 전일대비를 쓴다(휴장 불일치 대응·
미래참조 없음 — 미국 세션 t는 코스피 t 개장 시점에 아직 안 끝났으므로 exact 제외).

두 렌즈:
  A(자본곡선): equity에 기존 급등락→회귀(reversion_events, leverage=1 — 곡선이 이미 계좌기준).
  B(일일수익): d 시계열에 관측창 평균 ±θ 돌파→이후 H일 누적수익(daily_return_streak_events).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

HOT_STREAK = "hot_streak"     # 과열: 관측창 일일수익 평균 >= +θ
COLD_STREAK = "cold_streak"   # 과냉: 관측창 일일수익 평균 <= -θ
_STREAK_DIRECTIONS = (HOT_STREAK, COLD_STREAK)

_LIQ_EPS = 1e-12   # d <= -1 판정 여유(부동소수)


def composite_strategy_daily(
    kospi: pd.DataFrame,
    snp_close: pd.Series,
    *,
    leverage: float,
) -> pd.DataFrame:
    """혼합전략의 일별 프레임을 만든다.

    입력:
      kospi     — date, open, high, low, close (date ASC, 정제됨)
      snp_close — S&P500 종가 시리즈(index=Timestamp)
      leverage  — 계좌 레버리지 N배 (>=1)

    반환 DataFrame(date ASC), 컬럼:
      date, close(=equity 자본곡선), daily_return(d, 계좌 소수), idx_ret, r1, r2, snp_ret,
      kospi_open, kospi_close, liquidated(bool), segment(int, 청산 리셋 경계마다 증가)
    close=equity 로 두어 reversion_events(leverage=1)의 '가격'으로 그대로 투입한다.
    """
    if leverage < 1:
        raise ValueError("leverage는 1 이상이어야 함")
    k = kospi.copy()
    k["date"] = pd.to_datetime(k["date"])
    k = k.dropna(subset=["open", "close"]).sort_values("date").reset_index(drop=True)
    if len(k) < 2:
        raise ValueError("kospi 데이터가 2행 미만")

    sc = pd.Series(snp_close).dropna()
    sc.index = pd.to_datetime(sc.index)
    sc = sc.sort_index()
    # S&P 전일대비, 코스피 date 직전 미국세션에 정렬(exact 제외 = 미래참조 방지).
    snp_ret = sc.pct_change()
    snp_df = pd.DataFrame({"date": snp_ret.index, "snp_ret": snp_ret.to_numpy()}).dropna()
    merged = pd.merge_asof(
        k[["date", "open", "close"]], snp_df, on="date",
        direction="backward", allow_exact_matches=False,
    )

    opn = merged["open"].to_numpy(dtype=float)
    cls = merged["close"].to_numpy(dtype=float)
    snp = merged["snp_ret"].to_numpy(dtype=float)
    prev_close = np.concatenate([[np.nan], cls[:-1]])

    # s = -sign(S&P 전일대비): 하락(-)→+1(롱)·상승(+)→-1(숏)·결측/0→0(전략1 스킵)
    s = np.where(np.isnan(snp), 0.0, np.sign(-snp))
    r1 = np.nan_to_num(s * (cls / opn - 1.0), nan=0.0)          # 당일(지수)
    r2 = np.nan_to_num(opn / prev_close - 1.0, nan=0.0)          # 오버나이트(지수)
    idx_ret = r1 + r2
    daily = np.maximum(-1.0, leverage * idx_ret)                 # 계좌 일일수익(청산 floor)

    # 자본곡선(복리) + 청산 리셋(세그먼트). 청산일은 equity 0·해당 세그먼트 종료, 다음날 1.0 재시작.
    n = len(k)
    equity = np.empty(n, dtype=float)
    liquidated = np.zeros(n, dtype=bool)
    segment = np.zeros(n, dtype=int)
    base = 1.0
    seg = 0
    for t in range(n):
        d = daily[t]
        if d <= -1.0 + _LIQ_EPS:            # 강제청산: 그 날 전손
            equity[t] = 0.0
            liquidated[t] = True
            segment[t] = seg
            base = 1.0                       # 다음날 새 자본
            seg += 1
        else:
            base = base * (1.0 + d)
            equity[t] = base
            segment[t] = seg

    return pd.DataFrame({
        "date": merged["date"].to_numpy(),
        "close": equity,                     # reversion_events 입력(=자본곡선)
        "daily_return": daily,
        "idx_ret": idx_ret,
        "r1": r1,
        "r2": r2,
        "snp_ret": np.nan_to_num(snp, nan=0.0),
        "kospi_open": opn,
        "kospi_close": cls,
        "liquidated": liquidated,
        "segment": segment,
    })


def strategy_segments(df: pd.DataFrame) -> list[pd.DataFrame]:
    """청산 리셋 경계로 나눈 각 세그먼트의 **생존 구간**(equity>0·미청산) sub-df 리스트.

    렌즈 A는 세그먼트별로 reversion_events 를 돌려 병합한다(청산일 0·리셋 점프를 회귀에서 배제).
    2행 미만 세그먼트는 제외(피벗 불가).
    """
    out: list[pd.DataFrame] = []
    for _, g in df.groupby("segment", sort=True):
        alive = g[~g["liquidated"]]
        if len(alive) >= 2:
            out.append(alive.reset_index(drop=True))
    return out


@dataclass(frozen=True)
class StreakEvent:
    """관측창 스트릭 신호 한 건 — 렌즈 B. 모든 % 는 레버리지 반영 계좌 기준."""

    signal_date: pd.Timestamp
    lookback_avg: float       # 관측창 W일 일일수익 평균(계좌, 소수)
    direction: str            # HOT_STREAK(과열) | COLD_STREAK(과냉)
    fwd_return: float         # 이후 H일 누적 계좌수익(소수, 청산 시 -1.0)
    liquidated: bool          # 이후 H일 중 하루라도 계좌 -100% 도달


def daily_return_streak_events(
    daily_return: np.ndarray,
    dates: np.ndarray,
    *,
    window: int,
    threshold_pct: float,
    horizon: int,
    direction: str | None = None,
) -> list[StreakEvent]:
    """렌즈 B: 관측창 W일 일일수익 평균이 ±θ% 돌파 → 이후 H일 누적수익.

    신호일 t: A_t = mean(d[t-W+1 .. t]).  A_t >= +θ → 과열(HOT), A_t <= -θ → 과냉(COLD).
    이후 H일 누적수익 = ∏(1 + d[t+1 .. t+H]) - 1 (d 는 계좌 소수·청산 floor 반영 → 청산 시 -1.0).
    **비겹침**: 신호 발생 시 이후 H일을 건너뛰어 forward 윈도우가 겹치지 않게 한다.
    direction 지정 시 그 방향만(스윕용).
    """
    if window < 1:
        raise ValueError("window는 1 이상이어야 함")
    if horizon < 1:
        raise ValueError("horizon은 1 이상이어야 함")
    if threshold_pct <= 0:
        raise ValueError("threshold_pct는 0보다 커야 함")
    if direction is not None and direction not in _STREAK_DIRECTIONS:
        raise ValueError(f"direction은 {_STREAK_DIRECTIONS} 중 하나 또는 None")

    d = np.asarray(daily_return, dtype=float)
    n = len(d)
    theta = threshold_pct / 100.0
    events: list[StreakEvent] = []

    t = window - 1
    while t < n:
        if t + horizon >= n:            # 완전한 forward 윈도우 필요(이후 t는 더 짧음)
            break
        avg = float(np.mean(d[t - window + 1: t + 1]))
        dir_name = None
        if avg >= theta:
            dir_name = HOT_STREAK
        elif avg <= -theta:
            dir_name = COLD_STREAK
        if dir_name is None or (direction is not None and dir_name != direction):
            t += 1
            continue

        fwd = d[t + 1: t + 1 + horizon]
        cum = float(np.prod(1.0 + fwd) - 1.0)      # d>=-1 → prod>=0 → cum>=-1; 청산 시 0→cum=-1
        liq = bool(np.min(fwd) <= -1.0 + _LIQ_EPS)
        events.append(StreakEvent(
            signal_date=pd.Timestamp(dates[t]),
            lookback_avg=avg,
            direction=dir_name,
            fwd_return=cum,
            liquidated=liq,
        ))
        t += horizon                                # 비겹침: forward 윈도우만큼 건너뜀

    return events


def streak_summary(events: list[StreakEvent]) -> dict:
    """방향별(HOT_STREAK·COLD_STREAK) 요약 — 모든 % 계좌 기준.

    {n, mean_fwd, median_fwd, success_rate, liquidation_rate}. 평균·중앙값에 청산(-100%) 포함,
    success = fwd_return>0 비율(모멘텀 지속=과열+ / 과냉 반등+ 둘 다 +면 성공).
    """
    out: dict[str, dict] = {}
    for dir_name in _STREAK_DIRECTIONS:
        evs = [e for e in events if e.direction == dir_name]
        n = len(evs)
        if n == 0:
            out[dir_name] = {
                "n": 0, "mean_fwd": float("nan"), "median_fwd": float("nan"),
                "success_rate": float("nan"), "liquidation_rate": float("nan"),
            }
            continue
        fwd = sorted(e.fwd_return * 100.0 for e in evs)
        mid = n // 2
        median = fwd[mid] if n % 2 else (fwd[mid - 1] + fwd[mid]) / 2.0
        out[dir_name] = {
            "n": n,
            "mean_fwd": sum(fwd) / n,
            "median_fwd": median,
            "success_rate": 100.0 * sum(1 for e in evs if e.fwd_return > 0) / n,
            "liquidation_rate": 100.0 * sum(1 for e in evs if e.liquidated) / n,
        }
    return out

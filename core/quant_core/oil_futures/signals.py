"""장중 high/low 임계값 첫-터치 신호 생성기.

Short 신호 (위로 첫 터치 → 평균회귀 가정으로 매도):
    오늘 high ≥ 임계 ∧ 어제 high < 임계
Long 신호 (아래로 첫 터치 → 평균회귀 가정으로 매수):
    오늘 low ≤ 임계 ∧ 어제 low > 임계

전일 비교가 자연스러운 hysteresis 역할을 해서, 임계값 근처 진동 시
중복 신호 발화를 차단한다. (엑셀 원본 #1 한계 — close cross + 의도 불일치 — 보완.)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

import pandas as pd


class Side(str, Enum):
    """포지션 방향. 엑셀의 [Short 전략] / [Long 전략] 그룹에 대응."""

    SHORT = "short"  # 위로 첫 터치 → 매도
    LONG = "long"    # 아래로 첫 터치 → 매수


@dataclass(frozen=True)
class Signal:
    """단일 신호 이벤트.

    entry_ref_close 는 참고용(엑셀 원본과 대조하기 좋음).
    실제 진입가는 backtest 단계에서 '다음 영업일 시가'로 별도 결정한다.
    """

    date: pd.Timestamp
    side: Side
    threshold: float
    entry_ref_close: float


def generate_signals(
    df: pd.DataFrame,
    short_thresholds: Iterable[float] = (),
    long_thresholds: Iterable[float] = (),
) -> list[Signal]:
    """일봉 DataFrame → 신호 리스트.

    df는 date ASC 정렬된 OHLC + close 컬럼을 가져야 한다 (load_wti 결과 호환).
    빈 임계값 리스트는 그 방향 신호를 만들지 않는다.
    """
    short_thresholds = list(short_thresholds)
    long_thresholds = list(long_thresholds)
    if not short_thresholds and not long_thresholds:
        return []

    # numpy 어레이로 인덱스 접근 (대용량에서 iterrows 대비 수십 배 빠름)
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    date = df["date"].to_numpy()
    n = len(df)

    sigs: list[Signal] = []
    for i in range(1, n):
        h_today, h_prev = high[i], high[i - 1]
        l_today, l_prev = low[i], low[i - 1]
        c_today = close[i]
        d_today = pd.Timestamp(date[i])

        for th in short_thresholds:
            if h_today >= th > h_prev:
                sigs.append(Signal(d_today, Side.SHORT, float(th), float(c_today)))

        for th in long_thresholds:
            if l_today <= th < l_prev:
                sigs.append(Signal(d_today, Side.LONG, float(th), float(c_today)))

    return sigs

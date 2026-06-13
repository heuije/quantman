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
    smooth_window: int = 1,
    min_gap_days: int = 0,
) -> list[Signal]:
    """일봉 DataFrame → 신호 리스트.

    df는 date ASC 정렬된 OHLC + close 컬럼을 가져야 한다 (prepare_wti 결과 호환).
    빈 임계값 리스트는 그 방향 신호를 만들지 않는다.

    smooth_window>1 (방법 1): 당일 고/저 대신 최근 N일 고/저 SMA로 교차 판정 — 진입
        직전 추세를 반영한다. 1일 스파이크는 평균이 안 따라와 미발화, 지속 추세만 발화
        (하락 중 데드캣 바운스 vs 지속 상승 구분). =1이면 현행(원시 고/저).
    min_gap_days>0 (방법 3): 같은 (side, threshold) 신호 발화 후 M영업일 이내 재발화를
        억제한다 — 임계값 근처 진동 노이즈 디바운스(선발화 유지). =0이면 현행.

    기본값 (smooth_window=1, min_gap_days=0) 은 기존 동작과 byte-identical.
    """
    short_thresholds = list(short_thresholds)
    long_thresholds = list(long_thresholds)
    if not short_thresholds and not long_thresholds:
        return []
    if smooth_window < 1:
        raise ValueError("smooth_window는 1 이상이어야 함")
    if min_gap_days < 0:
        raise ValueError("min_gap_days는 0 이상이어야 함")

    # numpy 어레이로 인덱스 접근 (대용량에서 iterrows 대비 수십 배 빠름)
    if smooth_window > 1:
        # 고/저를 N일 SMA로 평활. 앞 N-1행은 NaN → 아래 비교가 자동 False라 무발화.
        high = df["high"].rolling(smooth_window).mean().to_numpy()
        low = df["low"].rolling(smooth_window).mean().to_numpy()
    else:
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    date = df["date"].to_numpy()
    n = len(df)

    # (side, threshold)별 마지막 발화 인덱스 — min_gap_days 이내 재발화 억제용.
    last_short: dict[float, int] = {}
    last_long: dict[float, int] = {}

    sigs: list[Signal] = []
    for i in range(1, n):
        h_today, h_prev = high[i], high[i - 1]
        l_today, l_prev = low[i], low[i - 1]
        c_today = close[i]
        d_today = pd.Timestamp(date[i])

        for th in short_thresholds:
            if h_today >= th > h_prev:
                last = last_short.get(th)
                if last is None or i - last >= min_gap_days:
                    sigs.append(Signal(d_today, Side.SHORT, float(th), float(c_today)))
                    last_short[th] = i

        for th in long_thresholds:
            if l_today <= th < l_prev:
                last = last_long.get(th)
                if last is None or i - last >= min_gap_days:
                    sigs.append(Signal(d_today, Side.LONG, float(th), float(c_today)))
                    last_long[th] = i

    return sigs

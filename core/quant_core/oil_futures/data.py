"""WTI 일봉 CSV 로더 + 검증.

CSV 스키마: date(YYYY-MM-DD), open, high, low, close, volume(int|nan)
- ASC 정렬 (오래된 것 → 최신)
- 필수 컬럼 NaN 0건 가정 (loader에서 검증)
- high/low 정합성 어긋난 행은 경고만 (드물게 데이터 소스 오류 있음 — 백테스트 자체는 동작)
"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# 패키지 기준 데이터 경로: core/data/wti_daily.csv
DEFAULT_CSV = Path(__file__).resolve().parents[2] / "data" / "wti_daily.csv"

REQUIRED_COLS = ("date", "open", "high", "low", "close")


def load_wti(path: Path | str = DEFAULT_CSV) -> pd.DataFrame:
    """WTI 일봉 데이터 로드 → date(datetime) + OHLCV(float) DataFrame.

    Raises:
        FileNotFoundError: CSV 없음.
        ValueError: 필수 컬럼 누락 / 가격 컬럼에 NaN.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"WTI CSV 없음: {path}")

    df = pd.read_csv(path, parse_dates=["date"])

    missing = set(REQUIRED_COLS) - set(df.columns)
    if missing:
        raise ValueError(f"CSV에 필수 컬럼 누락: {missing}")

    df = df.sort_values("date").reset_index(drop=True)

    price_cols = ["open", "high", "low", "close"]
    nan_mask = df[price_cols].isna().any(axis=1)
    if nan_mask.any():
        bad = df[nan_mask]
        raise ValueError(
            f"가격 컬럼에 NaN {len(bad)}건. 첫 5건:\n{bad.head().to_string()}"
        )

    # high/low 정합성 (백테스트 진행은 유지, 경고만)
    inv_mask = (df["high"] < df[["open", "close"]].max(axis=1)) | (
        df["low"] > df[["open", "close"]].min(axis=1)
    )
    if inv_mask.any():
        log.warning(
            "high/low 정합 오류 %d건 (high<max(open,close) 또는 low>min(open,close))",
            int(inv_mask.sum()),
        )

    return df

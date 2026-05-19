"""
데이터셋 로딩 헬퍼.

저장된 parquet → 지표 계산까지 끝낸 dict[symbol, DataFrame]를 반환한다.
백테스트 엔진과 분석 엔진이 곧바로 받을 수 있는 형태.
"""

from __future__ import annotations

import pandas as pd

from .data_fetcher import load_all, load_fund_all
from .indicators import compute_all


def load_dataset(with_indicators: bool = True) -> dict[str, pd.DataFrame]:
    """전체 심볼을 로드한다. with_indicators=True면 지표 컬럼까지 계산해 반환."""
    raw = load_all()
    if not with_indicators:
        return raw
    funds = load_fund_all()
    return {sym: compute_all(df, funds.get(sym)) for sym, df in raw.items()}

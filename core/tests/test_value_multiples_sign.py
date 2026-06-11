"""가치 배수(PBR·PER) 비양수 분모 처리 — '저평가' 랭킹 오염 근본수정.

PBR=Close/BVPS, PER=Close/EPS는 분모(자기자본·순이익)가 음수면 음수 배수를 낸다.
음수 배수는 '싼 것'이 아니라 자본잠식·적자 신호인데, "저평가"=낮은값 오름차순 랭킹에선
가장 작은(가장 음수) 값이 1위로 올라 부실주가 '가장 저평가'로 둔갑한다(prod 실측:
하이딥 PBR=-45.99 1위). 가치 배수는 분모 비양수면 미정의(NaN)가 표준.
"""
import numpy as np
import pandas as pd

from quant_core.indicators import add_fundamentals


def _price(n: int = 5) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0,
                         "Close": 100.0, "Volume": 1e6}, index=idx)


def _fund(equity: float, ni: float, shares: float = 1000.0) -> pd.DataFrame:
    return pd.DataFrame({"shares_outstanding": shares,
                         "stockholders_equity": equity, "ttm_ni": ni},
                        index=pd.to_datetime(["2024-01-01"]))


def test_pb_ratio_nan_when_book_value_nonpositive():
    df = _price()
    assert add_fundamentals(df, _fund(equity=-5e5, ni=1e5))["pb_ratio"].isna().all()
    assert add_fundamentals(df, _fund(equity=0.0, ni=1e5))["pb_ratio"].isna().all()
    pos = add_fundamentals(df, _fund(equity=5e5, ni=1e5))["pb_ratio"].dropna()
    assert len(pos) and (pos > 0).all()                     # 양수 BVPS → 양수 PBR 정상


def test_trailing_pe_nan_when_earnings_nonpositive():
    df = _price()
    assert add_fundamentals(df, _fund(equity=5e5, ni=-1e5))["trailing_pe"].isna().all()
    assert add_fundamentals(df, _fund(equity=5e5, ni=0.0))["trailing_pe"].isna().all()
    pos = add_fundamentals(df, _fund(equity=5e5, ni=1e5))["trailing_pe"].dropna()
    assert len(pos) and (pos > 0).all()

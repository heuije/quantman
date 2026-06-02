import pandas as pd
import numpy as np
import pytest
from quant_core.oil_futures.data import prepare_wti


def _raw():
    # Date 인덱스 + 대문자 컬럼(플랫폼 캐시 형식), 비정렬 + 음수 + NaN 포함
    idx = pd.to_datetime(["2020-04-21", "2020-04-20", "2020-04-17", "2020-04-22"])
    return pd.DataFrame(
        {"Open": [10.0, 18.0, 25.0, 14.0],
         "High": [13.0, 20.0, 26.0, 16.0],
         "Low":  [6.0, -40.3, 24.0, 12.0],
         "Close":[11.0, -37.6, 25.0, 13.0],   # 2020-04-20 음수
         "Volume":[1.0, 2.0, 3.0, np.nan]},
        index=idx,
    )


def test_prepare_wti_normalizes_and_sorts():
    out = prepare_wti(_raw())
    assert list(out.columns)[:5] == ["date", "open", "high", "low", "close"]
    assert out["date"].is_monotonic_increasing
    assert str(out["date"].dtype).startswith("datetime")


def test_prepare_wti_drops_nonpositive_close():
    out = prepare_wti(_raw())
    assert (out["close"] > 0).all()
    assert pd.Timestamp("2020-04-20") not in set(out["date"])


def test_prepare_wti_accepts_lowercase_date_column():
    raw = _raw().reset_index().rename(columns={"index": "Date"})
    out = prepare_wti(raw)
    assert len(out) == 3  # 음수 1건 제거


def test_prepare_wti_raises_on_empty():
    with pytest.raises(ValueError):
        prepare_wti(pd.DataFrame())


def test_prepare_wti_raises_on_missing_column():
    raw = _raw().drop(columns=["Close"])
    with pytest.raises(ValueError):
        prepare_wti(raw)


def test_new_usd_futures_registered_without_collision():
    from quant_core import data_fetcher as dfetch
    for key, sym in [("나스닥선물", "NQ=F"), ("은선물(COMEX)", "SI=F"),
                     ("비트코인선물", "BTC=F")]:
        assert dfetch.YFINANCE_SYMBOLS.get(key) == sym
        assert key in dfetch.ASSET_SYMBOLS
        assert dfetch.SYMBOL_CATEGORY.get(key) == "자산"
    # 기존 프록시 키는 그대로 보존(충돌 없음)
    assert dfetch.FDR_SYMBOLS["나스닥100선물"] == "304940"
    assert dfetch.FDR_SYMBOLS["은선물"] == "144600"

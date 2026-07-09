"""OHLC 컬럼 가드 회귀 — 2026-07-07 자동매매 전면다운 재현.

인시던트: PR#325가 발행한 국채 수익률 시리즈(['Close','date']만 — High/Low/Volume 없음)가
dataset_scope ALL_SYMBOLS(macro 안전망)로 로컬 사이클 dataset에 자동 유입 →
load_dataset_for(with_indicators=True) → compute_all → add_atr가 df["High"] 무가드
접근 → KeyError: 'High' → dataset 로드 전체 사망 → 모든 사이클(아침/종가/미장) 크래시.

부류: "High/Low/Volume을 요구하는 지표의 무가드 접근". Volume 지표(add_volume_ratio·
add_adv)는 이미 올바른 관용구(컬럼 없으면 NaN 컬럼)를 갖고 있었다 — 같은 관용구를
High 의존 2곳(add_atr·add_high_deviation)에 적용해 부류를 닫는다.

계약: Close-only 시리즈도 compute_all을 통과하며, 계산 불가 지표는 NaN "컬럼으로 존재"
(스키마 안정 — 다운스트림 조건 평가는 NaN=미정의로 처리). full OHLCV는 종전과 동일.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_CORE_DIR = Path(__file__).resolve().parent.parent
if str(_CORE_DIR) not in sys.path:
    sys.path.insert(0, str(_CORE_DIR))

from quant_core.indicators import add_atr, add_high_deviation, compute_all


def _bond_like(n=300):
    """인시던트 재현 — 국채 수익률 parquet 형상(['Close']만, 날짜 인덱스).

    단조 시리즈는 RSI 분모(loss)=0으로 전부 NaN이 수학적으로 맞으므로,
    등락 있는 시리즈로 구성(Close 기반 지표가 실제 계산됨을 확인하기 위함).
    """
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    close = 3.5 + 0.5 * np.sin(np.arange(n) / 7.0)
    return pd.DataFrame({"Close": close}, index=idx)


def _ohlcv(n=300):
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    close = pd.Series(np.linspace(100, 130, n), index=idx)
    return pd.DataFrame({
        "Open": close * 0.99, "High": close * 1.02, "Low": close * 0.98,
        "Close": close, "Volume": np.full(n, 1000.0),
    }, index=idx)


# ── 인시던트 재현: Close-only가 compute_all을 죽이지 않는다 ────────────────────
def test_compute_all_close_only_does_not_crash():
    out = compute_all(_bond_like())
    # Close 기반 지표는 정상 계산(전략 조건 'S&P500.pct_change_1d' 류가 이 경로).
    assert out["pct_change_1d"].notna().sum() > 0
    assert out["rsi_14"].notna().sum() > 0


def test_compute_all_close_only_missing_indicators_are_nan_columns():
    """계산 불가 지표는 '없음'이 아니라 NaN 컬럼 — 스키마 안정(Volume 관용구와 동일)."""
    out = compute_all(_bond_like())
    for col in ("atr_14", "atr_14_pct", "high_dev_20d", "volume_ratio", "adv_20d"):
        assert col in out.columns, col
        assert out[col].isna().all(), col


def test_add_atr_close_only_returns_nan():
    out = add_atr(_bond_like())
    assert out["atr_14"].isna().all() and out["atr_14_pct"].isna().all()


def test_add_high_deviation_close_only_returns_nan():
    out = add_high_deviation(_bond_like())
    assert out["high_dev_20d"].isna().all()


# ── 회귀 가드: full OHLCV는 가드 전과 동일 값 ─────────────────────────────────
def test_add_atr_full_ohlcv_unchanged():
    df = _ohlcv()
    out = add_atr(df)
    high, low, prev_close = df["High"], df["Low"], df["Close"].shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(),
                    (low - prev_close).abs()], axis=1).max(axis=1)
    expected = tr.rolling(14).mean()
    pd.testing.assert_series_equal(out["atr_14"], expected, check_names=False)


def test_add_high_deviation_full_ohlcv_unchanged():
    df = _ohlcv()
    out = add_high_deviation(df)
    roll_high = df["High"].rolling(20).max()
    expected = (df["Close"] - roll_high) / roll_high.replace(0, np.nan) * 100
    pd.testing.assert_series_equal(out["high_dev_20d"], expected, check_names=False)


def test_compute_all_full_ohlcv_atr_present():
    out = compute_all(_ohlcv())
    assert out["atr_14"].notna().sum() > 0
    assert out["high_dev_20d"].notna().sum() > 0

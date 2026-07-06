"""bonds feed — 국가 커브 fetch→frame 조립·만기 계약 (네트워크 무의존·monkeypatch)."""
from __future__ import annotations

import pandas as pd

from quant_core.data.feeds import bonds


def test_fetch_curve_builds_frame(monkeypatch):
    # 소스 dict 모사 — 2일 × 일부 만기(없는 만기는 NaN 컬럼)
    data = {"2026-06-30": {"3M": 3.85, "10Y": 4.40},
            "2026-07-01": {"3M": 3.87, "2Y": 4.17, "10Y": 4.48}}
    monkeypatch.setattr(bonds, "_fetch_dict", lambda cc, start: data)
    df = bonds.fetch_curve("US")
    assert list(df.index.strftime("%Y-%m-%d")) == ["2026-06-30", "2026-07-01"]
    assert list(df.columns) == bonds.maturities("US")          # 컬럼 순서 = 만기 순서(커브)
    assert df.loc[pd.Timestamp("2026-07-01"), "10Y"] == 4.48
    assert pd.isna(df.loc[pd.Timestamp("2026-06-30"), "2Y"])   # 그날 없는 만기 = NaN
    assert len(df) == 2


def test_fetch_curve_empty(monkeypatch):
    monkeypatch.setattr(bonds, "_fetch_dict", lambda cc, start: {})
    assert bonds.fetch_curve("US").empty                        # 무데이터 → 빈 DF(가짜 0 금지)


def test_fetch_curve_transient_failure(monkeypatch):
    def boom(cc, start):
        raise RuntimeError("network")
    monkeypatch.setattr(bonds, "_fetch_dict", boom)
    assert bonds.fetch_curve("US").empty                        # 예외 → 빈 DF(적재 안 함)


def test_maturities_order():
    assert bonds.maturities("US")[0] == "1M" and bonds.maturities("US")[-1] == "30Y"
    assert bonds.maturities("XX") == []


def test_macro_symbols_naming_and_kr_excluded():
    ms = set(bonds.macro_symbols())
    assert "미국채10년" in ms and "미국채1개월" in ms and "미국채30년" in ms   # US 전만기
    assert "일본국채40년" in ms and "유로존국채3개월" in ms and "중국국채3개월" in ms
    assert not any(s.startswith("한국") for s in ms)          # KR은 KRX 국고채가 매크로 SSOT → 제외
    assert bonds._macro_name("KR", "10Y") is None


def test_macro_catalog_matches_data_fetcher():
    """국채 만기물 카탈로그: 피드 발행(bonds.macro_symbols) == data_fetcher.MACRO_BONDS_SYMBOLS.
    (data_fetcher _BOND_TENORS와 피드 실제 발행이 어긋나면 챗 인지↔서빙 갭 — 드리프트 차단.)"""
    from quant_core import data_fetcher as dfm
    assert set(bonds.macro_symbols()) == set(dfm.MACRO_BONDS_SYMBOLS)


def test_write_tenor_series_publishes_named_parquets(monkeypatch):
    """refresh가 커브 스토어 + 만기별 매크로 parquet(Close)을 발행 — KR은 _macro_name None으로 제외."""
    import pandas as pd
    written = {}
    monkeypatch.setattr(bonds, "write_parquet_atomic", lambda df, p: written.__setitem__(str(p), df))
    idx = pd.to_datetime(["2026-06-30", "2026-07-01"])
    monkeypatch.setattr(bonds, "fetch_curve",
                        lambda cc, start="1900-01-01": pd.DataFrame(
                            {"3M": [3.85, 3.87], "10Y": [4.40, 4.48]}, index=idx))
    bonds.refresh("US")
    keys = list(written)
    assert any(k.endswith("US.parquet") for k in keys)           # 커브 스토어
    assert any(k.endswith("미국채3개월.parquet") for k in keys)    # 만기별 매크로 심볼
    assert any(k.endswith("미국채10년.parquet") for k in keys)
    tenor_df = next(df for k, df in written.items() if k.endswith("미국채10년.parquet"))
    assert list(tenor_df.columns) == ["Close"] and tenor_df["Close"].tolist() == [4.40, 4.48]

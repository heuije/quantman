"""Task 2 — 미니코스피200선물 데이터 앨리어스 + 빌더 노출 테스트.

미니와 정규는 동일 KOSPI200 지수(동일 포인트)를 호가하므로 가격 데이터를 공유한다.
엔진 승수(미니=50k vs 정규=250k)만 다르며, 이는 Task 1(카탈로그)에서 이미 처리됨.
이 테스트는 그 공유 구조(alias)와 빌더 노출을 검증한다.
"""

from quant_core import data_fetcher as df
from quant_core.data_fetcher import symbol_category


def test_mini_parquet_path_aliases_to_regular():
    assert df._parquet_path("미니코스피200선물") == df._parquet_path("코스피200선물")


def test_regular_path_unchanged():
    assert df._parquet_path("코스피200선물").name == "코스피200선물.parquet"


def test_mini_in_symbol_category():
    assert symbol_category("미니코스피200선물") == "자산"


def test_mini_in_index_when_regular_parquet_exists():
    import pytest
    if not df._parquet_path("코스피200선물").exists():
        pytest.skip("정규 parquet 부재 환경")
    idx = df.dataset_symbol_index()
    assert "미니코스피200선물" in idx
    assert idx["미니코스피200선물"]["has_ohlc"] is True


def test_load_all_and_index_include_mini_via_alias(tmp_path, monkeypatch):
    """I-1 회귀(스킵 없음): 정규 parquet만 있어도 load_all·dataset_symbol_index 둘 다 미니를
    alias로 포함한다. 빌더 목록과 실제 데이터셋 양쪽이 정합해야 서버 preview가 미니를
    '전일 종가 없음'으로 드롭하지 않는다(라이브 진입 후보로 노출)."""
    import pandas as pd
    monkeypatch.setattr(df, "DATA_DIR", tmp_path)
    reg = pd.DataFrame(
        {"Open": [400.0, 401.0], "High": [402.0, 402.0], "Low": [399.0, 400.0],
         "Close": [400.5, 401.5], "Volume": [1e6, 1e6]},
        index=pd.to_datetime(["2026-05-27", "2026-05-28"]))
    reg.to_parquet(df._parquet_path("코스피200선물"))
    # 실제 데이터셋 빌더(load_all): 미니 키가 정규 시리즈로 생성
    allsyms = df.load_all()
    assert "코스피200선물" in allsyms and "미니코스피200선물" in allsyms
    pd.testing.assert_frame_equal(allsyms["미니코스피200선물"], allsyms["코스피200선물"])
    # 빌더 목록(dataset_symbol_index): 미니가 has_ohlc로 포함
    idx = df.dataset_symbol_index()
    assert "미니코스피200선물" in idx and idx["미니코스피200선물"]["has_ohlc"] is True

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


# ── JP: 누적 + 당월 병합 (누적 파일이 전월까지만 담는 구조적 지연 해소) ──────────────

_MOF_ALL = (
    "Interest Rate (Historical),,,,,,,,,,,,,,,(Unit : %)\n"
    "Date,1Y,2Y,3Y,4Y,5Y,6Y,7Y,8Y,9Y,10Y,15Y,20Y,25Y,30Y,40Y\n"
    "2026/6/29,1.173,1.411,1.547,1.753,1.916,2.053,2.201,2.367,2.5,2.7,3.2,3.6,3.8,3.87,3.79\n"
    "2026/6/30,1.165,1.382,1.531,1.755,1.937,2.075,2.231,2.398,2.55,2.69,3.27,3.62,3.88,3.87,3.79\n"
)
_MOF_CURRENT = (
    "Interest Rate (July 2026),,,,,,,,,,,,,,,(Unit : %)\n"
    "Date,1Y,2Y,3Y,4Y,5Y,6Y,7Y,8Y,9Y,10Y,15Y,20Y,25Y,30Y,40Y\n"
    "2026/7/1,1.164,1.4,1.554,1.777,1.959,2.099,2.255,2.421,2.57,2.71,3.28,3.63,3.88,3.88,3.8\n"
    "2026/7/23,1.242,1.499,1.619,1.845,2.015,2.168,2.32,2.498,2.63,2.776,3.34,3.64,3.96,3.92,3.85\n"
    ",,,,,,,,,,,,,,,\n"
    '"  ¦If you cannot download the latest csv data, please clear the cache."\n'
)


def test_parse_mof_csv_skips_notes_and_blank_rows():
    out = bonds._parse_mof_csv(_MOF_CURRENT, "1900-01-01")
    assert set(out) == {"2026-07-01", "2026-07-23"}          # 안내문·빈 행 자연 제외
    assert out["2026-07-23"]["10Y"] == 2.776


def test_jp_curve_merges_current_month(monkeypatch):
    """누적(전월까지) + 당월 병합 — 당월 데이터가 실제로 실린다(24일 지연 부류 해소)."""
    monkeypatch.setattr(bonds, "_JP_ALL_URL", "http://all")
    monkeypatch.setattr(bonds, "_JP_CURRENT_URL", "http://cur")
    import requests

    def fake_get(url, **kw):
        class R:
            text = _MOF_ALL if url == "http://all" else _MOF_CURRENT

            def raise_for_status(self):
                return None
        return R()
    monkeypatch.setattr(requests, "get", fake_get)
    out = bonds._jp_curve("1900-01-01")
    assert "2026-07-23" in out, "당월 파일이 병합돼야(누적만 읽으면 6/30에서 멈춤)"
    assert "2026-06-30" in out                                # 누적 이력도 보존
    assert out["2026-07-23"]["10Y"] == 2.776


def test_jp_curve_current_month_failure_is_non_fatal(monkeypatch):
    """당월 파일 일시 실패 → 누적본으로 graceful(전체 수집 손실 없음)."""
    monkeypatch.setattr(bonds, "_JP_ALL_URL", "http://all")
    monkeypatch.setattr(bonds, "_JP_CURRENT_URL", "http://cur")
    import requests

    def fake_get(url, **kw):
        if url == "http://cur":
            raise RuntimeError("timeout")

        class R:
            text = _MOF_ALL

            def raise_for_status(self):
                return None
        return R()
    monkeypatch.setattr(requests, "get", fake_get)
    out = bonds._jp_curve("1900-01-01")
    assert "2026-06-30" in out and "2026-07-23" not in out


# ── KR: KRX 국고채 지표물(일별) 재사용 — 네트워크·신규 심볼 없음 ────────────────────

def test_kr_curve_reads_krx_macro_parquets(monkeypatch, tmp_path):
    """FRED 월간 대신 이미 매일 수집중인 국고채3년·10년 parquet을 읽어 커브 조립."""
    import pandas as pd
    idx = pd.to_datetime(["2026-07-22", "2026-07-23"])
    store = {"국고채3년": pd.DataFrame({"Close": [2.51, 2.55]}, index=idx),
             "국고채10년": pd.DataFrame({"Close": [3.11, 3.15]}, index=idx)}

    class P:
        def __init__(self, name):
            self.name = name

        def exists(self):
            return self.name in store
    monkeypatch.setattr(bonds, "_macro_path", lambda n: P(n))
    monkeypatch.setattr(bonds, "read_parquet_safe", lambda p: store.get(p.name))

    out = bonds._kr_curve("1900-01-01")
    assert out["2026-07-23"] == {"3Y": 2.55, "10Y": 3.15}
    assert bonds.maturities("KR") == ["3Y", "10Y"]
    assert bonds.COUNTRIES["KR"][1] == "일별"            # 화면 빈도 라벨도 실제와 일치


def test_kr_curve_missing_parquet_is_graceful(monkeypatch):
    """콜드 볼륨(파일 없음) → 빈 결과 → refresh가 기존 저장본 보존(가짜 0 금지)."""
    class P:
        def __init__(self, name):
            self.name = name

        def exists(self):
            return False
    monkeypatch.setattr(bonds, "_macro_path", lambda n: P(n))
    assert bonds._kr_curve("1900-01-01") == {}


def test_kr_publishes_no_macro_symbols():
    """KR 교체가 ALL_SYMBOLS를 건드리지 않음 — 자동매매 데이터셋 불변의 핵심 가드."""
    from quant_core import data_fetcher as dfm
    assert not any("국고채" in s for s in bonds.macro_symbols())
    assert bonds._macro_name("KR", "3Y") is None
    # 국고채3년/10년은 krx_openapi 소유(MACRO_KRX_SYMBOLS) — 국채 피드가 발행하지 않는다
    assert "국고채3년" in dfm.MACRO_KRX_SYMBOLS
    assert "국고채3년" not in set(dfm.MACRO_BONDS_SYMBOLS)


# ── CN: ChinaBond 커브(FRED 단종 대체) ──────────────────────────────────────────

def _cn_xlsx_bytes():
    """ChinaBond 연도 XLSX와 동일 형상(long 포맷)의 인메모리 파일."""
    import io

    import pandas as pd
    df = pd.DataFrame({
        "Date": ["2026/07/23", "2026/07/23", "2026/07/23", "2026/07/24", "2026/07/24",
                 "2026/07/24", "2026/07/24"],
        "Instructions for Standard Terms": ["0d", "3m", "10y", "0d", "3m", "10y", "9m"],
        "Standard Terms(Yrs)": [0.0, 0.25, 10.0, 0.0, 0.25, 10.0, 0.75],
        "Yield(%)": [0.91, 1.1, 1.72, 0.9229, 1.0986, 1.7282, 1.10],
    })
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    return buf.getvalue()


def test_parse_cn_year_xlsx_selects_standard_tenors():
    """long 포맷 → {date: {만기}}. 비표준 만기(0d·9m)는 제외, 라벨은 대문자 규약."""
    out = bonds.parse_cn_year_xlsx(_cn_xlsx_bytes())
    assert set(out) == {"2026-07-23", "2026-07-24"}
    assert out["2026-07-24"] == {"3M": 1.0986, "10Y": 1.7282}   # 0d·9m 제외
    assert "0D" not in out["2026-07-23"] and "9M" not in out["2026-07-24"]


def test_cn_curve_fails_whole_window_on_year_error(monkeypatch):
    """한 해라도 실패하면 전체 실패 — 반쪽 이력이 저장본을 덮어 구멍 내는 것 방지."""
    import requests
    calls = []

    def fake_get(url, params=None, **kw):
        calls.append(params["year"])
        if params["year"] == bonds._CN_FIRST_YEAR + 1:
            raise RuntimeError("transient")

        class R:
            content = _cn_xlsx_bytes()

            def raise_for_status(self):
                return None
        return R()
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(bonds.time, "sleep", lambda s: None)
    import pytest
    with pytest.raises(RuntimeError):
        bonds._cn_curve("1900-01-01")
    # fetch_curve는 그 예외를 삼켜 빈 DF → refresh가 기존 저장본 보존
    monkeypatch.setattr(bonds, "_fetch_dict", bonds._fetch_dict)
    assert bonds.fetch_curve("CN", "1900-01-01").empty


def test_cn_macro_symbols_are_additive():
    """중국 만기 확장은 **순수 가산** — 기존 중국국채3개월이 사라지지 않는다(참조 깨짐 방지)."""
    from quant_core import data_fetcher as dfm
    ms = set(bonds.macro_symbols())
    assert "중국국채3개월" in ms                     # 종전 유일 심볼 보존
    assert {"중국국채10년", "중국국채30년", "중국국채50년"} <= ms
    assert set(dfm.MACRO_BONDS_SYMBOLS) == ms       # 드리프트 가드(카탈로그 정합)


# ── staleness: 조용한 소스 단종 표면화 ────────────────────────────────────────────

def test_stale_countries_flags_frozen_source(monkeypatch):
    """HTTP 200이지만 옛 데이터만 주는 소스(중국 2023-11 정지)를 마지막 날짜 나이로 탐지."""
    import datetime as _dt
    import pandas as pd
    curves = {
        "US": pd.DataFrame({"10Y": [4.7]}, index=pd.to_datetime(["2026-07-23"])),
        "CN": pd.DataFrame({"3M": [2.88]}, index=pd.to_datetime(["2023-11-01"])),
    }
    monkeypatch.setattr(bonds, "load", lambda cc: curves.get(cc))
    stale = bonds.stale_countries(today=_dt.date(2026, 7, 24))
    assert "CN" in stale and stale["CN"]["last"] == "2023-11-01"
    assert stale["CN"]["age_days"] > 900
    assert "US" not in stale                               # 신선한 소스는 조용


def test_stale_ignores_uncollected(monkeypatch):
    monkeypatch.setattr(bonds, "load", lambda cc: None)
    assert bonds.stale_countries() == {}                   # 미수집은 수집 로그 담당(중복 경보 금지)


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

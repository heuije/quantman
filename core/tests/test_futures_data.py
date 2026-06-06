"""실선물 CSV 시드 — investing.com 과거데이터 정제 파싱.

clean_investing_csv가 investing.com 내보내기 특이점(날짜 내부 공백·천단위 콤마·거래량 K/M
접미사·최신→과거 역순)을 올바른 OHLCV(오름차순 DatetimeIndex)로 정제하는지. 실데이터
KOSPI200 선물 백테스트 정합의 전제(잘못 파싱하면 가격·정렬이 틀어져 회계가 깨짐).

    cd platform/core && python -m pytest tests/test_futures_data.py -q
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pandas as pd

_CORE = Path(__file__).resolve().parent.parent
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import quant_core.data_fetcher as df_mod
from quant_core.data_fetcher import (_parse_investing_volume, clean_investing_csv,
                                     fetch_kis_futures_daily, kis_futures_daily_to_ohlcv)

# 실제 파일 형식 표본(최신→과거 역순, 날짜 공백, 콤마, 거래량 K/빈값)
_CSV = (
    '"날짜","종가","시가","고가","저가","거래량","변동 %"\n'
    '"2026- 06- 02","1,434.95","1,413.50","1,444.65","1,405.50","18.45K","1.77%"\n'
    '"2026- 06- 01","1,409.95","1,416.40","1,451.05","1,360.00","175.97K","-0.55%"\n'
    '"2010- 01- 01","222.38","221.00","223.00","220.00","","0.00%"\n'
)


def test_parse_investing_volume():
    assert _parse_investing_volume("18.45K") == 18450.0
    assert _parse_investing_volume("175.97K") == 175970.0
    assert _parse_investing_volume("2.3M") == 2_300_000.0
    assert _parse_investing_volume("") == 0.0
    assert _parse_investing_volume("-") == 0.0
    assert _parse_investing_volume(None) == 0.0


def test_clean_investing_csv_structure():
    df = clean_investing_csv(io.StringIO(_CSV))
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    # 역순 입력 → 오름차순 정렬(과거가 먼저)
    assert list(df.index) == sorted(df.index)
    assert df.index[0] == pd.Timestamp("2010-01-01")
    assert df.index[-1] == pd.Timestamp("2026-06-02")
    # 날짜 내부 공백 제거 → NaT 없음
    assert df.index.notna().all()
    # 천단위 콤마 제거·float
    assert df.loc["2026-06-02", "Close"] == 1434.95
    assert df.loc["2026-06-02", "High"] == 1444.65
    # 거래량 K 환산 + 빈값 → 0
    assert df.loc["2026-06-02", "Volume"] == 18450.0
    assert df.loc["2010-01-01", "Volume"] == 0.0


# ── KIS 일봉 증분(FHKIF03020100) — 공식 spec 필드 매핑 + DI fetch (모의로 단위검증) ──

# KIS 공식 응답 예시 형식(국내선물옵션_기본시세.xlsx). 최신→과거 역순.
_KIS_OUTPUT2 = [
    {"stck_bsop_date": "20260605", "futs_prpr": "345.70", "futs_oprc": "344.00",
     "futs_hgpr": "346.00", "futs_lwpr": "343.50", "acml_vol": "178446"},
    {"stck_bsop_date": "20260604", "futs_prpr": "344.70", "futs_oprc": "342.00",
     "futs_hgpr": "345.00", "futs_lwpr": "341.00", "acml_vol": "150000"},
    {"stck_bsop_date": "bad", "futs_prpr": "x"},   # 손상 행 → 스킵
]


def test_kis_futures_daily_to_ohlcv_mapping():
    df = kis_futures_daily_to_ohlcv(_KIS_OUTPUT2)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert len(df) == 2                                  # 손상 행 스킵
    assert list(df.index) == sorted(df.index)            # 오름차순
    assert df.loc["2026-06-05", "Close"] == 345.70       # futs_prpr → Close
    assert df.loc["2026-06-05", "Open"] == 344.00        # futs_oprc → Open
    assert df.loc["2026-06-04", "High"] == 345.00
    assert df.loc["2026-06-05", "Volume"] == 178446.0


def test_fetch_kis_futures_daily_builds_params_and_appends(tmp_path, monkeypatch):
    monkeypatch.setattr(df_mod, "DATA_DIR", tmp_path)    # 격리 데이터 디렉터리(프로덕션 미터치)
    captured = {}

    def fake_request(tr_id, params):
        captured["tr_id"] = tr_id
        captured["params"] = params
        return {"output2": _KIS_OUTPUT2}

    out = fetch_kis_futures_daily("코스피200선물", fake_request, iscd="101S06",
                                  start="20260601", end="20260605")
    # 요청이 공식 spec 파라미터로 구성됐는가
    assert captured["tr_id"] == "FHKIF03020100"
    assert captured["params"]["FID_COND_MRKT_DIV_CODE"] == "F"
    assert captured["params"]["FID_INPUT_ISCD"] == "101S06"
    assert captured["params"]["FID_PERIOD_DIV_CODE"] == "D"
    # 매핑·append 결과
    assert out.loc["2026-06-05", "Close"] == 345.70
    assert (tmp_path / "코스피200선물.parquet").exists()   # 실제 저장됨

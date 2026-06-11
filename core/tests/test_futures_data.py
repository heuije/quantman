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
                                     fetch_kis_futures_daily, kis_futures_daily_to_ohlcv,
                                     seed_kis_futures_full)

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


def test_fetch_kis_futures_daily_drops_bars_beyond_end(tmp_path, monkeypatch):
    """PIT 불변식 — end(확정 세션) 너머의 형성 중 당일 봉은 canonical에 넣지 않는다.

    2026-06-11 만기일 인시던트(F1): 장중 boot refresh가 형성 중 당일 봉을 dataset에
    넣어 '전일 종가'(iloc[-1])가 미확정 장중값으로 오염 — preview 기준가·백테스트가
    장중 값을 보게 됨. KIS가 요청 범위 밖 봉을 돌려줘도 필터링되어야 한다.
    """
    monkeypatch.setattr(df_mod, "DATA_DIR", tmp_path)

    def fake_request(tr_id, params):
        return {"output2": _KIS_OUTPUT2 + [
            {"stck_bsop_date": "20260606", "futs_prpr": "999.99", "futs_oprc": "999.0",
             "futs_hgpr": "999.99", "futs_lwpr": "999.0", "acml_vol": "1"},  # end 너머(형성 중)
        ]}

    out = fetch_kis_futures_daily("코스피200선물", fake_request, iscd="101S06",
                                  start="20260601", end="20260605")
    assert str(out.index.max().date()) == "2026-06-05",         f"end 너머 봉이 canonical에 들어감: {out.index.max()}"


def test_seed_kis_futures_full_paginates_and_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(df_mod, "DATA_DIR", tmp_path)
    # 기존 ETF orphan(2020, ~₩2.8만) — 덮어써져야 함(스케일 혼합 방지)
    df_mod._save("코스피200선물", pd.DataFrame(
        {"Open": [28000.0], "High": [28000.0], "Low": [28000.0], "Close": [28000.0], "Volume": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2020-01-02")])))
    all_dates = pd.bdate_range("2026-01-02", "2026-06-05")

    def fake_kis(tr_id, params):                       # 호출당 ~40건 캡 → 페이지네이션 강제
        s, e = params["FID_INPUT_DATE_1"], params["FID_INPUT_DATE_2"]
        win = [d for d in all_dates if s <= d.strftime("%Y%m%d") <= e][-40:]
        return {"output2": [{"stck_bsop_date": d.strftime("%Y%m%d"), "futs_prpr": "344.0",
                             "futs_oprc": "343.0", "futs_hgpr": "345.0", "futs_lwpr": "342.0",
                             "acml_vol": "100"} for d in win]}

    res = seed_kis_futures_full("코스피200선물", fake_kis, "A01606", floor="20260101")
    # 페이지네이션으로 전체 영업일 수집(오름차순)
    assert len(res) == len(all_dates)
    assert str(res.index[0].date()) == "2026-01-02" and str(res.index[-1].date()) == "2026-06-05"
    # ETF orphan 덮어써짐 — 선물 스케일(344)만, 2020 ETF 행 없음
    assert res["Close"].max() == 344.0
    assert pd.Timestamp("2020-01-02") not in res.index
    saved = df_mod._load_existing("코스피200선물")           # 디스크도 덮어쓰기
    assert pd.Timestamp("2020-01-02") not in saved.index and len(saved) == len(all_dates)


# ── 번들 정제완료 CSV(깊은 과거 2010+) 시드 — 전략연구소 parquet의 깊은 base ──────────

def test_seed_from_clean_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(df_mod, "DATA_DIR", tmp_path)
    # 번들 포맷: date,open,high,low,close,volume 오름차순. 초기행 volume 빈값(투자닷컴 스냅샷 특성).
    csv = tmp_path / "clean.csv"
    csv.write_text(
        "date,open,high,low,close,volume\n"
        "2010-01-01,222.38,222.38,222.38,222.38,\n"
        "2010-01-04,222.38,224.38,222.38,223.58,\n"
        "2026-06-02,1413.5,1444.65,1405.5,1434.95,18450.0\n",
        encoding="utf-8")
    # 기존 얕은 KIS orphan(2025, 단일계약) — 깊은 CSV로 덮어써져야 함
    df_mod._save("코스피200선물", pd.DataFrame(
        {"Open": [340.0], "High": [340.0], "Low": [340.0], "Close": [340.0], "Volume": [1.0]},
        index=pd.DatetimeIndex([pd.Timestamp("2025-09-01")])))

    df = df_mod.seed_from_clean_csv("코스피200선물", csv)
    assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]
    assert list(df.index) == sorted(df.index)                 # 오름차순
    assert df.index[0] == pd.Timestamp("2010-01-01")          # 깊은 과거 base
    assert df.index[-1] == pd.Timestamp("2026-06-02")
    assert df.loc["2026-06-02", "Close"] == 1434.95
    assert df.loc["2026-06-02", "Volume"] == 18450.0
    assert df.loc["2010-01-01", "Volume"] == 0.0              # 빈값 → 0
    # 얕은 orphan 덮어써짐 — 깊은 연속물만(2025 단일계약 행 없음)
    assert pd.Timestamp("2025-09-01") not in df.index
    saved = df_mod._load_existing("코스피200선물")
    assert saved.index[0] == pd.Timestamp("2010-01-01") and pd.Timestamp("2025-09-01") not in saved.index

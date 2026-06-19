"""KOSPI200 선물 데이터 갱신 — 번들 CSV 깊은 시드 배선 검증(네트워크·KIS 없음).

_refresh_kospi_futures가 KIS 미설정 상태에서도 번들 정적 CSV로 전략연구소 parquet에 깊은
과거(2010+)를 시드하는지. 전략연구소 코스피200선물 백테스트가 얕은 단일계약이 아니라 깊은
연속물 위에서 돌아가는지의 전제(회귀 방지 — KIS 덮어쓰기로 얕아졌던 이슈의 근본수정).

    cd platform/server && python -m pytest tests/test_kospi_futures_refresh.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_SERVER_DIR = Path(__file__).resolve().parent.parent
# 이 워크트리의 core를 최우선 — 다른 워크트리(editable install)의 quant_core가 잡히는 것 방지.
_CORE_DIR = _SERVER_DIR.parent / "core"
for _p in (str(_CORE_DIR), str(_SERVER_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def test_refresh_seeds_deep_csv_without_kis(tmp_path, monkeypatch):
    import quant_core.data_fetcher as dfm
    from app import kis_data_client, main as m

    monkeypatch.setattr(dfm, "DATA_DIR", tmp_path)                              # 격리 — 프로덕션 미터치
    monkeypatch.setattr(kis_data_client, "get_kis_data_client", lambda: None)  # KIS 미설정 경로

    m._refresh_kospi_futures()

    saved = dfm._load_existing("코스피200선물")
    assert saved is not None and not saved.empty
    assert saved.index.min() == pd.Timestamp("2010-01-01")     # 깊은 과거 base(번들 CSV)
    assert saved.index.max() >= pd.Timestamp("2026-06-02")
    assert float(saved["Close"].max()) < 5000                  # 지수포인트 스케일(ETF 2.8만 아님)
    assert len(saved) >= 4000                                  # 16년치 일봉


def _write_clean_csv(path, dates):
    df = pd.DataFrame({"date": [d.strftime("%Y-%m-%d") for d in dates],
                       "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000})
    df.to_csv(path, index=False)
    return path


def test_csv_coverage_gap_detects_internal_holes(tmp_path):
    """csv_coverage_gap: 완전 parquet=False, 내부 결손(행 누락·NaN Close)=True, 빈=True."""
    import quant_core.data_fetcher as dfm

    dates = pd.bdate_range("2010-01-01", "2012-12-31")
    csv = _write_clean_csv(tmp_path / "k.csv", dates)
    full = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0,
                         "Volume": 1000.0}, index=dates)
    assert dfm.csv_coverage_gap(full, csv) is False                 # 완전 — 재시드 불필요
    assert dfm.csv_coverage_gap(full[full.index.year != 2011], csv) is True   # 2011 통째 결손
    nanned = full.copy()
    nanned.loc[nanned.index.year == 2011, "Close"] = float("nan")
    assert dfm.csv_coverage_gap(nanned, csv) is True               # 행은 있으나 Close NaN
    assert dfm.csv_coverage_gap(pd.DataFrame(), csv) is True       # 빈 parquet


def test_refresh_reseeds_internally_gappy_parquet(tmp_path, monkeypatch):
    """근본 회귀가드: '깊지만(min=2010) 내부 구멍난' parquet이 refresh에서 CSV 재시드로 복구.

    구버그(needs_deep가 min-date만 봄)에선 깊이 통과로 재시드 스킵 → 구멍 영구. 본 수정으로
    공백 감지 → 재시드 → 결손 연도 복구(인사이트 엔진 2021/2023/2024 거래0의 데이터 근본)."""
    import quant_core.data_fetcher as dfm
    from app import kis_data_client, main as m

    monkeypatch.setattr(dfm, "DATA_DIR", tmp_path)
    monkeypatch.setattr(kis_data_client, "get_kis_data_client", lambda: None)

    m._refresh_kospi_futures()                                     # 1) 정상 깊은 시드
    full = dfm._load_existing("코스피200선물")
    assert int((full.index.year == 2021).sum()) > 100             # 정상엔 2021 존재

    gappy = full[full.index.year != 2021]                         # 2) 2021만 도려낸 오염(min은 2010 유지)
    dfm._save("코스피200선물", gappy)
    assert int((dfm._load_existing("코스피200선물").index.year == 2021).sum()) == 0

    m._refresh_kospi_futures()                                     # 3) 공백 감지 → 재시드 → 복구
    healed = dfm._load_existing("코스피200선물")
    assert int((healed.index.year == 2021).sum()) > 100           # 2021 복구됨

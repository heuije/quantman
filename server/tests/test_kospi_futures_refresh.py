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

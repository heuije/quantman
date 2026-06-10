"""fundamental_kr 백필 견고성·관측.

(1) 빈결과(데이터 없음) 종목은 마커를 남겨 fresh_days 동안 재시도 안 함 — 금융주·상폐가
    매일 예산을 갉아먹던 비효율을 차단. (2) coverage()는 파일시스템 스캔만으로 진척을 집계.

    cd core && pytest tests/test_fundamental_kr_backfill.py -v
"""
import os
import time

import pandas as pd
import pytest

fk = pytest.importorskip("quant_core.data.feeds.fundamental_kr")


def _isolate(monkeypatch, tmp_path):
    """_fund_path를 tmp로 격리 — 마커·coverage는 _fund_path 파생이라 함께 격리됨."""
    monkeypatch.setattr(fk, "_fund_path", lambda c: tmp_path / f"{c.replace('/', '_')}.parquet")


def test_empty_result_writes_marker_and_skips_next_run(monkeypatch, tmp_path):
    """빈결과 종목 → 마커 기록 + 다음 실행에서 skip(예산 미소모)."""
    _isolate(monkeypatch, tmp_path)
    n = {"calls": 0}

    def fake_one(c, years):
        n["calls"] += 1
        return pd.DataFrame()                      # 빈결과(금융주·상폐 모사)

    monkeypatch.setattr(fk, "fetch_one", fake_one)

    r1 = fk.fetch(["055550"], [2025], budget_calls=100)
    assert r1["empty"] == 1 and r1["ok"] == 0
    assert fk._marker_path("055550").exists()       # 마커 기록
    assert n["calls"] == 1

    r2 = fk.fetch(["055550"], [2025], budget_calls=100)   # 마커 신선 → skip
    assert n["calls"] == 1                           # 재시도 안 함
    assert r2["calls"] == 0                          # 예산 미소모


def test_success_writes_parquet_and_clears_stale_marker(monkeypatch, tmp_path):
    """만료된 마커가 있어도, 데이터가 생기면 parquet 기록 + 마커 제거."""
    _isolate(monkeypatch, tmp_path)
    m = fk._marker_path("005930")
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text("")
    old = time.time() - 200 * 86400                  # fresh_days(80) 밖 = 만료
    os.utime(m, (old, old))

    df = pd.DataFrame({"pb_ratio": [1.5]}, index=pd.to_datetime(["2025-03-31"]))
    monkeypatch.setattr(fk, "fetch_one", lambda c, y: df)

    r = fk.fetch(["005930"], [2025], budget_calls=100)
    assert r["ok"] == 1
    assert fk._fund_path("005930").exists()
    assert not fk._marker_path("005930").exists()    # 스테일 마커 정리


def test_fresh_parquet_still_skipped(monkeypatch, tmp_path):
    """회귀: 최근 수집된 parquet는 그대로 skip(기존 동작 보존)."""
    _isolate(monkeypatch, tmp_path)
    pd.DataFrame({"pb_ratio": [1.0]}).to_parquet(fk._fund_path("000660"))
    called = {"n": 0}
    monkeypatch.setattr(fk, "fetch_one", lambda c, y: called.__setitem__("n", called["n"] + 1))

    r = fk.fetch(["000660"], [2025], budget_calls=100)
    assert called["n"] == 0                          # fetch_one 호출 안 됨
    assert r["calls"] == 0


def test_coverage_counts_parquet_markers_and_recent(monkeypatch, tmp_path):
    """coverage(): parquet 보유·빈결과 마커·최근24h 적재를 파일시스템 스캔으로 집계."""
    _isolate(monkeypatch, tmp_path)
    pd.DataFrame({"x": [1]}).to_parquet(fk._fund_path("005930"))      # 보유(최근)
    pd.DataFrame({"x": [1]}).to_parquet(fk._fund_path("000660"))      # 보유(오래됨)
    old = time.time() - 10 * 86400
    os.utime(fk._fund_path("000660"), (old, old))
    fk._write_marker("055550")                                        # 빈결과 마커
    # 999999 = 미수집

    cov = fk.coverage(["005930", "000660", "055550", "999999"])
    assert cov["have_fundamentals"] == 2
    assert cov["empty_marked"] == 1
    assert cov["written_last_24h"] == 1                               # 005930만 최근
    assert cov["newest_mtime"] is not None and cov["oldest_mtime"] is not None

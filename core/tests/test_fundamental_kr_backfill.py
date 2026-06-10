"""fundamental_kr 백필 견고성·관측·한도구분.

(1) genuine 빈결과(데이터 없음=OpenDART 013)는 마커로 fresh_days 재시도 차단.
(2) **한도초과(020)·네트워크 오류**는 마커 금지 + 청크 중단 — false 빈결과(한도를 무데이터로
    오인) 방지. fetch_one은 (df, rate_limited) 반환.
(3) coverage()·clear_markers()는 파일시스템 스캔/정리.

    cd core && pytest tests/test_fundamental_kr_backfill.py -v
"""
import os
import time

import pandas as pd
import pytest

fk = pytest.importorskip("quant_core.data.feeds.fundamental_kr")


def _isolate(monkeypatch, tmp_path):
    """_fund_path를 tmp로 격리 — 마커·coverage·clear는 _fund_path 파생이라 함께 격리됨."""
    monkeypatch.setattr(fk, "_fund_path", lambda c: tmp_path / f"{c.replace('/', '_')}.parquet")


def test_is_common_share_handles_voting_rights_labels():
    """주식총수 'se' 라벨이 필러마다 달라도 보통주 행을 robust 판별(shares=null→pb 미산출 근본수정).

    실측 라벨(전부 보통주): 정확매칭 '보통주'는 LG화학만 잡고 나머지 5종을 놓쳤었다."""
    commons = ["보통주", "의결권 있는 주식\n(보통주)", "의결권있는 주식",
               "의결권 있는\n보통주", "의결권이 있는주식(보통주)"]
    for se in commons:
        assert fk._is_common_share(se) is True, f"보통주로 판별돼야: {se!r}"
    # 우선주(의결권 없는)·합계·비고는 제외
    for se in ["우선주", "의결권 없는 주식\n(우선주)", "의결권없는 주식", "합계", "비고"]:
        assert fk._is_common_share(se) is False, f"보통주가 아니어야: {se!r}"


def test_delete_shares_null_targets_only_null(monkeypatch, tmp_path):
    """마이그레이션: shares_outstanding 마지막값 null인 parquet만 삭제, 정상은 보존."""
    _isolate(monkeypatch, tmp_path)
    pd.DataFrame({"shares_outstanding": [1e6, 7e7]}).to_parquet(fk._fund_path("OK"))
    pd.DataFrame({"shares_outstanding": [1e6, None]}).to_parquet(fk._fund_path("NULL"))
    res = fk.delete_shares_null(["OK", "NULL", "MISSING"])
    assert res == {"deleted": 1, "kept": 1, "no_col": 0}
    assert fk._fund_path("OK").exists() and not fk._fund_path("NULL").exists()


def test_empty_result_writes_marker_and_skips_next_run(monkeypatch, tmp_path):
    """genuine 빈결과(데이터 없음) → 마커 기록 + 다음 실행 skip(예산 미소모)."""
    _isolate(monkeypatch, tmp_path)
    n = {"calls": 0}

    def fake_one(c, years):
        n["calls"] += 1
        return pd.DataFrame(), False               # 빈결과·한도아님(013)

    monkeypatch.setattr(fk, "fetch_one", fake_one)

    r1 = fk.fetch(["055550"], [2025], budget_calls=100)
    assert r1["empty"] == 1 and r1["ok"] == 0 and r1["rate_limited"] == 0
    assert fk._marker_path("055550").exists()
    assert n["calls"] == 1

    r2 = fk.fetch(["055550"], [2025], budget_calls=100)   # 마커 신선 → skip
    assert n["calls"] == 1 and r2["calls"] == 0


def test_rate_limited_no_marker_and_stops_chunk(monkeypatch, tmp_path):
    """OpenDART 한도(rate_limited) → 마커 금지 + 청크 즉시 중단(false 빈결과·망치질 방지)."""
    _isolate(monkeypatch, tmp_path)
    n = {"calls": 0}

    def fake_one(c, years):
        n["calls"] += 1
        return pd.DataFrame(), True                # 한도 신호

    monkeypatch.setattr(fk, "fetch_one", fake_one)

    r = fk.fetch(["005930", "000660"], [2025], budget_calls=1000)
    assert r["rate_limited"] == 1 and r["ok"] == 0 and r["empty"] == 0
    assert n["calls"] == 1                          # 첫 종목에서 중단(망치질 없음)
    assert not fk._marker_path("005930").exists()   # 마커 금지 → 재시도 유지
    assert not fk._marker_path("000660").exists()


def test_success_writes_parquet_and_clears_stale_marker(monkeypatch, tmp_path):
    """만료된 마커가 있어도, 데이터가 생기면 parquet 기록 + 마커 제거."""
    _isolate(monkeypatch, tmp_path)
    m = fk._marker_path("005930")
    m.parent.mkdir(parents=True, exist_ok=True)
    m.write_text("")
    old = time.time() - 200 * 86400                  # fresh_days(80) 밖 = 만료
    os.utime(m, (old, old))

    df = pd.DataFrame({"pb_ratio": [1.5]}, index=pd.to_datetime(["2025-03-31"]))
    monkeypatch.setattr(fk, "fetch_one", lambda c, y: (df, False))

    r = fk.fetch(["005930"], [2025], budget_calls=100)
    assert r["ok"] == 1
    assert fk._fund_path("005930").exists()
    assert not fk._marker_path("005930").exists()    # 스테일 마커 정리


def test_fresh_parquet_still_skipped(monkeypatch, tmp_path):
    """회귀: 최근 수집된 parquet는 그대로 skip(기존 동작 보존)."""
    _isolate(monkeypatch, tmp_path)
    pd.DataFrame({"pb_ratio": [1.0]}).to_parquet(fk._fund_path("000660"))
    called = {"n": 0}

    def fake_one(c, y):
        called["n"] += 1
        return pd.DataFrame(), False

    monkeypatch.setattr(fk, "fetch_one", fake_one)
    r = fk.fetch(["000660"], [2025], budget_calls=100)
    assert called["n"] == 0 and r["calls"] == 0      # fetch_one 호출 안 됨


def test_coverage_counts_parquet_markers_and_recent(monkeypatch, tmp_path):
    """coverage(): parquet 보유·빈결과 마커·최근24h 적재를 파일시스템 스캔으로 집계."""
    _isolate(monkeypatch, tmp_path)
    pd.DataFrame({"x": [1]}).to_parquet(fk._fund_path("005930"))      # 보유(최근)
    pd.DataFrame({"x": [1]}).to_parquet(fk._fund_path("000660"))      # 보유(오래됨)
    old = time.time() - 10 * 86400
    os.utime(fk._fund_path("000660"), (old, old))
    fk._write_marker("055550")                                        # 빈결과 마커

    cov = fk.coverage(["005930", "000660", "055550", "999999"])
    assert cov["have_fundamentals"] == 2
    assert cov["empty_marked"] == 1
    assert cov["written_last_24h"] == 1
    assert cov["newest_mtime"] is not None and cov["oldest_mtime"] is not None


def test_clear_markers_removes_all(monkeypatch, tmp_path):
    """clear_markers(): false 마커 회복 — 모든 .empty 제거(다음 청크가 재수집·재분류)."""
    _isolate(monkeypatch, tmp_path)
    fk._write_marker("005930")
    fk._write_marker("000660")
    pd.DataFrame({"x": [1]}).to_parquet(fk._fund_path("207940"))      # parquet은 남겨야

    removed = fk.clear_markers()
    assert removed == 2
    assert not fk._marker_path("005930").exists()
    assert not fk._marker_path("000660").exists()
    assert fk._fund_path("207940").exists()                          # parquet 미삭제

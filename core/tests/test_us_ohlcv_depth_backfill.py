"""US OHLCV 깊이 백필(backfill_overseas_depth) 단위 테스트 — KR 깊이 백필의 US 미러.

과거 backfill_start=2015 코호트의 floor(2010) 이전 갭을 yf.download 배치로 소급 prepend.
depth-done 마커·budget·transient 실패 재시도 규약이 KR과 동일한지, 그리고 US 고유의
min_date 그룹 배치(같은 min을 공유하는 종목들 = 1콜)를 검증한다. 외부 yfinance는 fake.
"""

from __future__ import annotations

import pandas as pd

from quant_core import data_fetcher as df_mod


class _FakeYF:
    """ticker별 상장일(ipo)로 [start, end) 영업일 OHLCV 생성 — yf.download 대체(멀티컬럼)."""

    def __init__(self, ipo: dict[str, pd.Timestamp]):
        self.ipo = ipo
        self.calls: list[tuple] = []

    def download(self, tickers, start=None, end=None, **kw):
        self.calls.append((tuple(tickers), str(start), str(end)))
        frames: dict[str, pd.DataFrame] = {}
        for t in tickers:
            ipo = self.ipo.get(t)
            if ipo is None:
                continue
            lo = max(pd.to_datetime(start), ipo)
            hi = pd.to_datetime(end) - pd.Timedelta(days=1)   # yf end는 exclusive
            if lo > hi:
                continue
            idx = pd.bdate_range(lo, hi)
            if len(idx) == 0:
                continue
            frames[t] = pd.DataFrame(
                {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 100},
                index=idx)
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, axis=1)     # 컬럼 MultiIndex(level0=ticker) — yf 배치와 동형


def _seed(start: str, end: str) -> pd.DataFrame:
    idx = pd.bdate_range(start, end)
    return pd.DataFrame(
        {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1}, index=idx)


def _setup(monkeypatch, tmp_path, ipo):
    monkeypatch.setattr(df_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(df_mod, "mark_data_dirty", lambda: None)
    monkeypatch.setattr(df_mod.time, "sleep", lambda *_: None)
    fake = _FakeYF(ipo)
    monkeypatch.setattr(df_mod, "yf", fake)
    return fake


def test_deepen_young_and_already_deep(monkeypatch, tmp_path):
    fake = _setup(monkeypatch, tmp_path, {
        "AAA": pd.Timestamp("2008-01-01"),   # 깊은 이력 보유 → 2010까지 소급돼야
        "CCC": pd.Timestamp("2018-06-01"),   # young(상장 2018) → 더 깊이 불가
    })
    df_mod._save("AAA", _seed("2015-01-02", "2015-12-31"))   # 2015 캡 코호트
    df_mod._save("BBB", _seed("2009-12-01", "2015-12-31"))   # 이미 floor 이전부터 깊음
    df_mod._save("CCC", _seed("2018-06-01", "2018-12-31"))

    res = df_mod.backfill_overseas_depth(["AAA", "BBB", "CCC"], budget_symbols=10)

    assert df_mod._load_existing("AAA").index.min() <= pd.Timestamp("2010-01-31")
    assert not any("BBB" in c[0] for c in fake.calls)        # 이미 깊음 → fetch 없음
    assert df_mod._load_existing("CCC").index.min() >= pd.Timestamp("2018-01-01")
    assert res["deepened"] == 1
    assert res["young"] >= 1                                 # CCC(빈 응답 → young)
    assert res["done_total"] >= 2                            # AAA·BBB(+수렴 시 CCC)


def test_same_min_date_grouped_into_one_batch(monkeypatch, tmp_path):
    """2015 캡 코호트처럼 min_date를 공유하는 종목들은 배치 1콜로 처리(효율 핵심)."""
    fake = _setup(monkeypatch, tmp_path, {
        "AAA": pd.Timestamp("2008-01-01"),
        "DDD": pd.Timestamp("2008-01-01"),
    })
    df_mod._save("AAA", _seed("2015-01-02", "2015-12-31"))
    df_mod._save("DDD", _seed("2015-01-02", "2015-12-31"))

    df_mod.backfill_overseas_depth(["AAA", "DDD"], budget_symbols=10)

    assert len(fake.calls) == 1                              # 같은 min → 1 배치콜
    assert set(fake.calls[0][0]) == {"AAA", "DDD"}


def test_idempotent_after_done(monkeypatch, tmp_path):
    fake = _setup(monkeypatch, tmp_path, {"AAA": pd.Timestamp("2008-01-01")})
    df_mod._save("AAA", _seed("2015-01-02", "2015-12-31"))

    df_mod.backfill_overseas_depth(["AAA"])
    fake.calls.clear()
    res2 = df_mod.backfill_overseas_depth(["AAA"])
    assert fake.calls == []
    assert res2["deepened"] == 0


def test_batch_exception_not_marked(monkeypatch, tmp_path):
    """네트워크 예외 → 마커 금지·재시도 대상 유지(KR과 동일 규약)."""
    fake = _setup(monkeypatch, tmp_path, {"AAA": pd.Timestamp("2008-01-01")})
    df_mod._save("AAA", _seed("2015-01-02", "2015-12-31"))

    def _boom(*a, **k):
        raise RuntimeError("network")
    monkeypatch.setattr(fake, "download", _boom)

    res = df_mod.backfill_overseas_depth(["AAA"])
    assert res["fail"] == 1
    assert res["done_total"] == 0


def test_empty_response_marks_young_and_converges(monkeypatch, tmp_path):
    """무예외 빈 응답 = young 마커(KR/FDR와 동일 신뢰 규약) — young 그룹이 영원히
    재시도되지 않고 백필이 수렴한다(예외만 실패·재시도)."""
    fake = _setup(monkeypatch, tmp_path, {"AAA": pd.Timestamp("2008-01-01")})
    df_mod._save("AAA", _seed("2015-01-02", "2015-12-31"))
    monkeypatch.setattr(fake, "download", lambda *a, **k: pd.DataFrame())

    res = df_mod.backfill_overseas_depth(["AAA"])
    assert res["young"] == 1
    assert res["done_total"] == 1            # 마커 → 재호출 시 0비용
    res2 = df_mod.backfill_overseas_depth(["AAA"])
    assert res2["young"] == 0 and res2["fail"] == 0


def test_budget_caps_attempts(monkeypatch, tmp_path):
    fake = _setup(monkeypatch, tmp_path, {
        "AAA": pd.Timestamp("2008-01-01"),
        "DDD": pd.Timestamp("2008-01-01"),
    })
    df_mod._save("AAA", _seed("2015-01-02", "2015-12-31"))
    df_mod._save("DDD", _seed("2015-01-02", "2015-12-31"))

    df_mod.backfill_overseas_depth(["AAA", "DDD"], budget_symbols=1)
    assert sum(len(c[0]) for c in fake.calls) == 1           # 1 종목만 시도

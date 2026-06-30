"""P2-A — KR OHLCV 깊이 백필(기존 종목을 2010까지 소급 prepend) 단위 테스트.

일일 fetch_korean_stocks는 *앞으로만* 증분하므로 기존 종목의 floor 이전 과거는 못 채운다.
backfill_korean_stocks_depth가 그 갭만 1회 prepend하고, depth-done 마커로 완료 종목을
영구 skip(완주=네트워크 0비용)하는지 검증한다. 외부 FDR은 fake로 대체(네트워크 없음).
"""

from __future__ import annotations

import pandas as pd

from quant_core import data_fetcher as df_mod


class _FakeFDR:
    """code별 상장일(ipo)을 받아 [start,end] 범위의 영업일 OHLCV를 생성. 호출 기록."""

    def __init__(self, ipo: dict[str, pd.Timestamp]):
        self.ipo = ipo
        self.calls: list[tuple] = []

    def DataReader(self, code, start, end=None):
        self.calls.append((code, str(start), str(end)))
        ipo = self.ipo.get(code)
        if ipo is None:
            return pd.DataFrame()
        start_ts = pd.to_datetime(start)
        end_ts = pd.to_datetime(end) if end else pd.Timestamp("2024-12-31")
        lo = max(start_ts, ipo)
        if lo > end_ts:
            return pd.DataFrame()
        idx = pd.bdate_range(lo, end_ts)
        if len(idx) == 0:
            return pd.DataFrame()
        return pd.DataFrame(
            {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 100},
            index=idx)


def _seed(start: str, end: str) -> pd.DataFrame:
    idx = pd.bdate_range(start, end)
    return pd.DataFrame(
        {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1}, index=idx)


def _setup(monkeypatch, tmp_path, ipo):
    monkeypatch.setattr(df_mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(df_mod, "mark_data_dirty", lambda: None)
    fake = _FakeFDR(ipo)
    monkeypatch.setattr(df_mod, "fdr", fake)
    return fake


def test_deepen_young_and_already_deep(monkeypatch, tmp_path):
    fake = _setup(monkeypatch, tmp_path, {
        "AAA": pd.Timestamp("2008-01-01"),   # 깊은 이력 보유 → 2010까지 소급돼야
        "CCC": pd.Timestamp("2018-06-01"),   # young(상장 2018) → 더 깊이 불가
    })
    df_mod._save("AAA", _seed("2015-01-01", "2015-12-31"))   # 2015부터만 보유
    df_mod._save("BBB", _seed("2009-12-01", "2015-12-31"))   # 이미 floor 이전부터 깊음
    df_mod._save("CCC", _seed("2018-06-01", "2018-12-31"))   # 상장 후로만 보유

    res = df_mod.backfill_korean_stocks_depth(
        ["AAA", "BBB", "CCC"], floor="2010-01-01", budget_symbols=10)

    # AAA: 2010까지 prepend됨
    assert df_mod._load_existing("AAA").index.min() <= pd.Timestamp("2010-01-31")
    # BBB: 이미 깊음 → fetch 시도 자체 없음(데이터⊇floor)
    assert not any(c[0] == "BBB" for c in fake.calls)
    # CCC: young → 데이터 변화 없음
    assert df_mod._load_existing("CCC").index.min() >= pd.Timestamp("2018-01-01")

    assert res["deepened"] == 1          # AAA
    assert res["young"] == 1             # CCC
    assert res["done_total"] == 3        # 셋 다 마커(완료)


def test_idempotent_after_done(monkeypatch, tmp_path):
    """완주 후 재호출 → FDR 호출 0(마커로 영구 skip = 0비용 수렴)."""
    fake = _setup(monkeypatch, tmp_path, {"AAA": pd.Timestamp("2008-01-01")})
    df_mod._save("AAA", _seed("2015-01-01", "2015-12-31"))

    df_mod.backfill_korean_stocks_depth(["AAA"], floor="2010-01-01")
    fake.calls.clear()
    res2 = df_mod.backfill_korean_stocks_depth(["AAA"], floor="2010-01-01")

    assert fake.calls == []
    assert res2["deepened"] == 0


def test_transient_failure_not_marked(monkeypatch, tmp_path):
    """네트워크 예외는 마커에 넣지 않음 → 다음 청크에서 재시도된다."""
    fake = _setup(monkeypatch, tmp_path, {"AAA": pd.Timestamp("2008-01-01")})
    df_mod._save("AAA", _seed("2015-01-01", "2015-12-31"))

    def _boom(code, start, end=None):
        raise RuntimeError("network")
    monkeypatch.setattr(fake, "DataReader", _boom)

    res = df_mod.backfill_korean_stocks_depth(["AAA"], floor="2010-01-01")
    assert res["fail"] == 1
    assert res["done_total"] == 0        # 마커 안 됨 → 재시도 대상 유지


def test_budget_caps_fetch_attempts(monkeypatch, tmp_path):
    """budget_symbols는 한 청크의 실제 fetch 시도 수를 제한한다."""
    fake = _setup(monkeypatch, tmp_path, {
        "AAA": pd.Timestamp("2008-01-01"),
        "DDD": pd.Timestamp("2008-01-01"),
    })
    df_mod._save("AAA", _seed("2015-01-01", "2015-12-31"))
    df_mod._save("DDD", _seed("2015-01-01", "2015-12-31"))

    df_mod.backfill_korean_stocks_depth(["AAA", "DDD"], floor="2010-01-01",
                                        budget_symbols=1)
    assert len(fake.calls) == 1          # 한 청크 = 1 종목만 시도

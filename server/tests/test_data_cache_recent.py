"""get_projected(recent_days) — SELECT as-of 스냅샷 경량 로드 검증.

recent_days=N이면 종목별 최근 N행만 계산 → 전 유니버스 전체이력 프로젝션(OOM·타임아웃 근본원인)
회피. N > 신호 룩백이면 *최신 행* 값은 full 계산과 동일(SELECT는 최신 단면만 사용). 기본(None)은
전체이력(백테스트 불변).

    cd platform && pytest server/tests/test_data_cache_recent.py -v
"""
import numpy as np
import pandas as pd
import pytest

dc = pytest.importorskip("app.data_cache")


def _raw():
    idx = pd.date_range("2018-01-01", periods=600, freq="B")
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0.05, 1.0, 600))
    df = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Volume": np.full(600, 1e6)}, index=idx)
    return {"AAA": df, "BBB": df.copy()}


def test_recent_days_tails_and_latest_matches(monkeypatch):
    monkeypatch.setattr(dc, "get_raw_dataset", lambda: _raw())
    monkeypatch.setattr(dc.data_fetcher, "load_fund_all", lambda: {})
    col = "ma_dev_20d"     # 20일 MA 괴리율 — 룩백 20 < 300
    full = dc.get_projected([col])
    recent = dc.get_projected([col], recent_days=300)
    assert len(full["AAA"]) == 600 and len(recent["AAA"]) == 300
    # 최신 행은 full과 동일(룩백 충족) — SELECT가 쓰는 as-of 단면이 정확함을 보장.
    assert abs(float(full["AAA"][col].iloc[-1]) - float(recent["AAA"][col].iloc[-1])) < 1e-9


def test_recent_days_none_is_full(monkeypatch):
    """기본(None) = 전체이력 — 백테스트·분포 경로 불변."""
    monkeypatch.setattr(dc, "get_raw_dataset", lambda: _raw())
    monkeypatch.setattr(dc.data_fetcher, "load_fund_all", lambda: {})
    out = dc.get_projected(["ma_dev_20d"])
    assert len(out["AAA"]) == 600

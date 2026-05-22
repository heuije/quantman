"""P3 — us_metrics 기술지표 enrich 단위테스트 (네트워크 없음).

build_metrics(dataset, caps)에 합성 df를 주입해, dataset(compute_all)의 기술지표
컬럼이 metric으로 surface되는지 + NaN→None + 부재 컬럼 생략 + 펀더멘털 None을 검증.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import us_metrics_cache


@pytest.fixture()
def _patched(monkeypatch):
    monkeypatch.setattr(us_metrics_cache.data_fetcher, "load_sp500",
                        lambda: [{"symbol": "AAPL", "name": "애플"}])
    monkeypatch.setattr(us_metrics_cache.kis_master_cache, "get_master_list",
                        lambda: [{"symbol": "AAPL", "name": "애플",
                                  "market": "NAS", "kind": "stock"}])


def _df():
    return pd.DataFrame({
        "Close": [100.0, 110.0, 121.0],
        "Volume": [1000.0, 2000.0, 3000.0],
        "rsi_14": [40.0, 50.0, 55.5],
        "ma_dev_20d": [1.0, 2.0, 3.3],
        "pct_change_20d": [5.0, 6.0, 7.7],
        "atr_14": [float("nan"), 2.0, float("nan")],   # 마지막 NaN → None
        # "streak" 컬럼 없음 → metric에서 생략돼야 함
    })


def test_enrich_surfaces_indicators(_patched):
    m = us_metrics_cache.build_metrics(dataset={"AAPL": _df()},
                                       caps={"AAPL": 1e12})["AAPL"]
    assert m["rsi_14"] == pytest.approx(55.5)
    assert m["ma_dev_20d"] == pytest.approx(3.3)
    assert m["pct_change_20d"] == pytest.approx(7.7)


def test_enrich_nan_becomes_none(_patched):
    m = us_metrics_cache.build_metrics(dataset={"AAPL": _df()},
                                       caps={"AAPL": 1e12})["AAPL"]
    assert m["atr_14"] is None          # 마지막 값이 NaN


def test_enrich_omits_absent_columns(_patched):
    m = us_metrics_cache.build_metrics(dataset={"AAPL": _df()},
                                       caps={"AAPL": 1e12})["AAPL"]
    assert "streak" not in m            # df에 없는 컬럼은 추가 안 함


def test_base_fields_preserved(_patched):
    m = us_metrics_cache.build_metrics(dataset={"AAPL": _df()},
                                       caps={"AAPL": 1e12})["AAPL"]
    assert m["close"] == pytest.approx(121.0)
    assert m["pct_change_1d"] == pytest.approx((121.0 / 110.0 - 1) * 100)
    assert m["market_cap"] == 1e12
    assert m["currency"] == "USD"
    assert m["per"] is None and m["pbr"] is None     # 펀더멘털 미지원

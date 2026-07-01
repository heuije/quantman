"""코스피 혼합전략 REVERSION 배선 — 렌즈 A(전략곡선 회귀) · 렌즈 B(일일수익 스트릭).

_kospi_strategy_df 를 합성 전략곡선으로 스텁(데이터캐시·S&P 불필요). 실제 계산 단언은
core/tests/test_strategy_curve.py — 여기선 배선·형태·코스피 전용 게이트·curve 필드.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.deps import get_current_user
from app.routers import futures


def _synth_curve() -> pd.DataFrame:
    """합성 전략곡선: close=자본곡선(급등락 leg용 진동), daily_return=과열/과냉 블록."""
    t = np.arange(240)
    close = 110.0 + 20.0 * np.sin(t / 8.0)            # 렌즈 A: ±18% 진동 → 회귀 이벤트
    daily = 0.02 * np.sign(np.sin(t / 15.0))          # 렌즈 B: +2%/-2% 블록 → 과열/과냉
    daily = np.where(daily == 0.0, 0.02, daily)
    return pd.DataFrame({
        "date": pd.date_range("2015-01-01", periods=len(t), freq="B"),
        "close": close,
        "daily_return": daily,
        "idx_ret": daily / 5.0,
        "r1": daily / 10.0, "r2": daily / 10.0, "snp_ret": 0.0,
        "kospi_open": close, "kospi_close": close,
        "liquidated": False, "segment": 0,
    })


def _synth_oil() -> pd.DataFrame:
    t = np.arange(200)
    closes = 110.0 + 20.0 * np.sin(t / 8.0)
    return pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=len(closes), freq="B"),
        "open": closes, "high": closes + 1, "low": closes - 1, "close": closes,
        "volume": [1000] * len(closes),
    })


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(futures, "_df", lambda symbol: _synth_oil())
    monkeypatch.setattr(futures, "_kospi_strategy_df", lambda leverage: _synth_curve())
    app = FastAPI()
    app.include_router(futures.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "email": "t@t"}
    return TestClient(app)


# 렌즈 A: 전략곡선(lev1 계좌%) — 진동폭 안에서 임계 선택.
_QA = "reversal=10&run=25&horizon=10&leverage=5"


def test_kospi_reversion_uses_strategy_curve(monkeypatch):
    client = _client(monkeypatch)
    r = client.get(f"/futures/kospi/reversion?{_QA}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["strategy"] is True                    # 코스피=전략곡선
    assert len(body["curve"]) > 0                       # 배경 시리즈 동봉
    assert set(body["curve"][0]) == {"date", "v"}
    assert len(body["events"]) >= 1


def test_non_kospi_reversion_not_strategy(monkeypatch):
    client = _client(monkeypatch)
    body = client.get(f"/futures/oil/reversion?reversal=20&run=80&horizon=10&leverage=10").json()
    assert body["strategy"] is False                   # 원지수
    assert len(body["curve"]) > 0                       # 종가 시리즈 동봉


def test_kospi_reversion_sweep(monkeypatch):
    client = _client(monkeypatch)
    r = client.get(f"/futures/kospi/reversion-sweep?row_axis=run&col_axis=horizon&direction=down&{_QA}")
    assert r.status_code == 200, r.text
    body = r.json()
    nrow, ncol = len(body["row_labels"]), len(body["col_labels"])
    assert len(body["cells"]) == nrow * ncol
    assert any(c["n"] > 0 for c in body["cells"])


# ── 렌즈 B: 스트릭 ──────────────────────────────────────────────────────

def test_streak_kospi_shape(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/futures/kospi/reversion-streak?window=10&threshold=1&horizon=5&leverage=5")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["window"] == 10 and body["horizon"] == 5
    assert len(body["daily"]) > 0 and set(body["daily"][0]) == {"date", "v"}
    for d in ("hot_streak", "cold_streak"):
        assert set(body[d]) == {"n", "mean_fwd", "median_fwd", "success_rate", "liquidation_rate"}
    assert len(body["events"]) >= 1                     # 블록 데이터 → 과열·과냉 신호 발생
    e = body["events"][0]
    assert set(e) == {"signal_date", "fwd_end_date", "lookback_avg", "direction",
                      "fwd_return", "liquidated"}
    assert e["direction"] in ("hot_streak", "cold_streak")


def test_streak_kospi_only(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/futures/oil/reversion-streak").status_code == 404


def test_streak_validation(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/futures/kospi/reversion-streak?window=0").status_code == 422
    assert client.get("/futures/kospi/reversion-streak?threshold=0").status_code == 422
    assert client.get("/futures/kospi/reversion-streak?horizon=0").status_code == 422
    assert client.get("/futures/kospi/reversion-streak?leverage=0.5").status_code == 422


def test_streak_requires_auth():
    app = FastAPI()
    app.include_router(futures.router)
    assert TestClient(app).get("/futures/kospi/reversion-streak").status_code == 401

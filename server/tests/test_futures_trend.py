"""/futures/{symbol}/trend-events 엔드포인트 스모크 (인증·검증·응답 형태).

데이터 캐시(_df)를 합성 df로 스텁하고 인증 의존성을 오버라이드한다.
app.main lifespan/DB 불요 — 라우터만 띄운다(test_futures_auth.py 패턴).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.deps import get_current_user
from app.routers import futures


def _synthetic_df() -> pd.DataFrame:
    closes = list(range(100, 140))          # 40 영업일, 단조 상승
    n = len(closes)
    return pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=n, freq="B"),
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "close": closes,
        "volume": [1000] * n,
    })


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(futures, "_df", lambda symbol: _synthetic_df())
    app = FastAPI()
    app.include_router(futures.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "email": "t@t"}
    return TestClient(app)


def test_trend_events_requires_auth():
    app = FastAPI()
    app.include_router(futures.router)
    assert TestClient(app).get("/futures/oil/trend-events").status_code == 401


def test_trend_events_all_days(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/futures/oil/trend-events?lookback=3&horizon=5")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "all"
    assert body["side"] is None and body["threshold"] is None
    assert len(body["events"]) == 40 - 3 - 5      # range(3, 40-5) → 32
    assert body["regression"]["n"] == len(body["events"])
    assert set(body["events"][0]) == {"date", "close", "past_return", "forward_return"}


def test_trend_events_signal_anchor(monkeypatch):
    client = _client(monkeypatch)
    # 단조 상승이라 high가 110을 위로 첫 터치하는 신호가 존재 → signal 모드 200
    r = client.get("/futures/oil/trend-events?lookback=3&horizon=5&side=short&threshold=110")
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "signal"


def test_trend_events_validation(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/futures/oil/trend-events?lookback=0").status_code == 422
    assert client.get("/futures/oil/trend-events?horizon=0").status_code == 422
    # side만 주고 threshold 누락 → 422
    assert client.get("/futures/oil/trend-events?side=short").status_code == 422

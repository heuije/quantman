"""/futures/{symbol}/reversion · /reversion-sweep 엔드포인트 스모크.

데이터 캐시(_df)를 합성 진동 시리즈로 스텁하고 인증을 오버라이드(test_futures_trend.py 패턴).
실제 회귀 계산 단언은 core/tests/test_reversion_analysis.py가 담당 — 여기선 배선·검증·형태.
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


def _synthetic_df() -> pd.DataFrame:
    # 110±20 진동(주기 ~50일, 4사이클) — 충분한 급등락 leg로 양방향 이벤트 발생.
    t = np.arange(200)
    closes = 110.0 + 20.0 * np.sin(t / 8.0)
    return pd.DataFrame({
        "date": pd.date_range("2020-01-01", periods=len(closes), freq="B"),
        "open": closes,
        "high": closes + 1,
        "low": closes - 1,
        "close": closes,
        "volume": [1000] * len(closes),
    })


def _client(monkeypatch) -> TestClient:
    monkeypatch.setattr(futures, "_df", lambda symbol: _synthetic_df())
    app = FastAPI()
    app.include_router(futures.router)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "email": "t@t"}
    return TestClient(app)


# 작은 진폭에도 잡히게 임계를 진동폭 안에서 선택(reversal 2%·run 8% 지수, lev10).
_Q = "reversal=20&run=80&horizon=10&leverage=10"


def test_reversion_requires_auth():
    app = FastAPI()
    app.include_router(futures.router)
    assert TestClient(app).get("/futures/oil/reversion").status_code == 401


def test_reversion_shape_and_events(monkeypatch):
    client = _client(monkeypatch)
    r = client.get(f"/futures/oil/reversion?{_Q}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["leverage"] == 10
    for d in ("down_exhaustion", "up_exhaustion"):
        assert set(body[d]) == {
            "n", "mean_reversion", "median_reversion", "success_rate", "liquidation_rate"
        }
    assert len(body["events"]) >= 1
    e = body["events"][0]
    assert set(e) == {
        "pivot_date", "pivot_price", "trigger_date", "trigger_price", "direction",
        "run_account", "reversion_account", "liquidated", "liquidation_day",
    }
    assert e["direction"] in ("down_exhaustion", "up_exhaustion")


def test_reversion_validation(monkeypatch):
    client = _client(monkeypatch)
    assert client.get("/futures/oil/reversion?reversal=0").status_code == 422
    assert client.get("/futures/oil/reversion?run=0").status_code == 422
    assert client.get("/futures/oil/reversion?horizon=0").status_code == 422
    assert client.get("/futures/oil/reversion?leverage=0.5").status_code == 422


def test_reversion_sweep_shape(monkeypatch):
    client = _client(monkeypatch)
    r = client.get(f"/futures/oil/reversion-sweep?row_axis=run&col_axis=horizon&direction=down&{_Q}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["direction"] == "down_exhaustion"
    nrow, ncol = len(body["row_labels"]), len(body["col_labels"])
    assert nrow >= 1 and ncol == 5            # horizon 축 = 5개 사다리
    assert len(body["cells"]) == nrow * ncol
    assert any(c["n"] > 0 for c in body["cells"])   # 일부 칸은 표본 존재


def test_reversion_sweep_rejects_bad_axes(monkeypatch):
    client = _client(monkeypatch)
    assert client.get(
        f"/futures/oil/reversion-sweep?row_axis=run&col_axis=run&{_Q}"
    ).status_code == 422
    assert client.get(
        f"/futures/oil/reversion-sweep?row_axis=bogus&col_axis=horizon&{_Q}"
    ).status_code == 422

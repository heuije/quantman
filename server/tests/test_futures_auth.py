"""선물 라우터 인증 게이트 + 미지원 종목 회귀 — /futures/*는 로그인(JWT) 전용.

토큰 없으면 401(데이터 접근 전 차단). app.main lifespan/DB 불요.
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.routers import futures


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(futures.router)
    return TestClient(app)


def test_get_endpoints_require_auth():
    client = _client()
    for path in ["/futures/instruments",
                 "/futures/oil/data-info", "/futures/oil/latest-price",
                 "/futures/oil/prices", "/futures/oil/grid", "/futures/gold/grid",
                 "/futures/oil/signals", "/futures/oil/seasonality",
                 "/futures/oil/macro-context"]:
        assert client.get(path).status_code == 401, f"{path} should require auth"


def test_post_endpoints_require_auth():
    client = _client()
    for path in ["/futures/oil/backtest", "/futures/nasdaq/walkforward"]:
        assert client.post(path, json={}).status_code == 401, f"{path} should require auth"

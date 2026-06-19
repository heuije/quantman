"""POST /ir/strategy/export.xlsx — 증빙 엑셀 엔드포인트.

검증:
- simulate(1회 백테스트) IR → 200 + .xlsx(zip 매직 PK) + Content-Disposition.
- 비-simulate 결과(select 등) → 400(엑셀 형상 미지원 게이트, P2 예정).

데이터 로딩은 monkeypatch(_load_ir_dataset)로 합성 dataset 주입 — parquet·매니페스트 불요.
인메모리 SQLite + 최소 앱(test_strategies_ir 패턴).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.db import get_session
from app.models import User
from app.routers import ir as ir_router
from app.security import create_access_token


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"Open": closes, "High": [c * 1.001 for c in closes],
                         "Low": [c * 0.999 for c in closes], "Close": closes,
                         "Volume": [1e6] * len(closes)}, index=idx)


# 꾸준한 상승 → Close > 3일 평균(신호 참) → on_signal 진입·기간말 청산.
_DATASET = {"005930": _df([100 * 1.01 ** i for i in range(40)])}
_SIGNAL3 = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {
        "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
        "right": {"op": "ts_mean", "params": {"window": 3},
                  "inputs": {"signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}},
    },
}
_SIM_IR = {
    "name": "엑셀 증빙 테스트 전략",
    "universe": {"kind": "single", "symbols": ["005930"]},
    "signal": _SIGNAL3,
    "position": {"direction": "long",
                 "sizing": {"mode": "pct_cash", "amount_pct": 10.0},
                 "entry": {"mode": "on_signal"}, "exit": {}, "overlays": {}},
    "simulation": {"initial_capital": 10_000_000},
    "study": {"axis": "none"},
}


def _build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        u = User(email="t@example.com")
        s.add(u); s.commit(); s.refresh(u)
        uid = u.id
    app = FastAPI()
    app.include_router(ir_router.router)

    def _ov():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _ov
    return TestClient(app), create_access_token(uid)


def _auth(tok: str):
    return {"Authorization": f"Bearer {tok}"}


def test_export_returns_xlsx_for_simulate(monkeypatch):
    monkeypatch.setattr(ir_router, "_load_ir_dataset", lambda body: (_DATASET, None))
    client, tok = _build()
    r = client.post("/ir/strategy/export.xlsx", headers=_auth(tok), json=_SIM_IR)
    assert r.status_code == 200, r.text
    assert r.content[:2] == b"PK"          # .xlsx = zip 매직
    assert len(r.content) > 5000
    assert "spreadsheetml" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]


def test_export_returns_xlsx_for_select(monkeypatch):
    """비-simulate 형상(select)도 엑셀 export(P2) — 200 + .xlsx (형상별 디스패치)."""
    sel = {"success": True, "query": "select", "as_of": "2020-01-01",
           "universe_size": 1, "eligible_size": 1,
           "results": [{"symbol": "AAA", "score": 1.0, "sector": "X", "metrics": {"pb_ratio": 0.8}}]}
    monkeypatch.setattr(ir_router, "_load_ir_dataset", lambda body: (_DATASET, None))
    monkeypatch.setattr(ir_router, "strategy_from_spec", lambda *a, **k: sel)
    client, tok = _build()
    r = client.post("/ir/strategy/export.xlsx", headers=_auth(tok), json=_SIM_IR)
    assert r.status_code == 200, r.text
    assert r.content[:2] == b"PK"


def test_export_rejects_failed_run(monkeypatch):
    """분석 실행 실패(success=False)는 400."""
    monkeypatch.setattr(ir_router, "_load_ir_dataset", lambda body: (_DATASET, None))
    monkeypatch.setattr(ir_router, "strategy_from_spec",
                        lambda *a, **k: {"success": False, "error": "데이터 없음"})
    client, tok = _build()
    r = client.post("/ir/strategy/export.xlsx", headers=_auth(tok), json=_SIM_IR)
    assert r.status_code == 400
    assert "데이터 없음" in r.json()["detail"]

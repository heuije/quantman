"""S5 — /ir/strategy 핸들러 헤드리스 검증 (StrategyIR 전체 구조 수용).

명세 P1-6·S5. 팩터·펼침·거부 경로를 HTTP 핸들러 직접 호출로 고정.

    cd platform && pytest tests/test_server_ir_strategy.py -v
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core"))
sys.path.insert(0, str(ROOT / "server"))

ir = pytest.importorskip("app.routers.ir")
from app.routers.ir import ir_strategy  # noqa: E402


def _multi():
    idx = pd.date_range("2020-01-01", periods=252, freq="B")

    def mk(drift, mom):
        close = 100 * (1 + drift) ** np.arange(252)
        return pd.DataFrame({
            "Open": close, "High": close * 1.001, "Low": close * 0.999,
            "Close": close, "Volume": 1e6, "momentum_12_1m": float(mom),
            "ma_dev_20d": np.where(np.arange(252) % 2 == 0, 1.0, -1.0),
        }, index=idx)
    return {"AAA": mk(0.003, 10.0), "BBB": mk(-0.001, -5.0), "CCC": mk(0.0, -2.0)}


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    # all/screener는 컬럼 프로젝션(get_projected) 경로 — 합성 dataset 반환.
    monkeypatch.setattr(ir.data_cache, "get_projected",
                        lambda columns, symbols=None, recent_days=None: _multi())
    # strat: 조합 등 결정 불가 폴백 경로(get_dataset)도 동일 합성 dataset.
    monkeypatch.setattr(ir, "get_dataset", _multi)
    # single/list 경로(load_dataset_for)도 합성 — describe 단일/포트폴리오 리서치 테스트용.
    monkeypatch.setattr(ir.qc, "load_dataset_for", lambda needed: _multi())
    # 게이트는 test_data_layer에서 검증 — 여기선 실행 경로만 보므로 manifest 생략(None=게이트 skip).
    monkeypatch.setattr(ir, "build_dataset_manifest", lambda *a, **k: None)


def _factor_body(top_n=1, direction="long", study=None):
    b = {
        "signal": {"op": "data", "params": {"ref": "momentum_12_1m"}},
        "universe": {"kind": "all"},
        "position": {"direction": direction, "sizing": {"mode": "equal_weight"},
                     "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": top_n}},
        "simulation": {"initial_capital": 1e7},
    }
    if study:
        b["study"] = study
    return b


def test_factor_strategy():
    res = ir_strategy(_factor_body(), user=None)
    assert res["success"], res
    assert "metrics" in res and res["metrics"]["n_trades"] == 0  # 리밸런스 path(trades 미집계)
    assert isinstance(res["equity"], list) and len(res["equity"]) > 0


def test_long_short_strategy():
    res = ir_strategy(_factor_body(direction="long_short"), user=None)
    assert res["success"]
    assert res["metrics"]["total_return"] is not None


def test_parameter_sweep():
    body = _factor_body(study={"axis": "parameter",
                               "param_grid": [{"path": "position.entry.top_n",
                                               "values": [1, 2]}]})
    res = ir_strategy(body, user=None)
    assert res["success"] and res["axis"] == "parameter"
    assert set(res["buckets"].keys()) == {"top_n=1", "top_n=2"}
    assert "axes" in res


def test_parameter_sweep_2d_grid():
    """비용 민감도 — commission × slippage 데카르트곱 격자."""
    body = _factor_body(study={"axis": "parameter", "param_grid": [
        {"path": "simulation.commission", "values": [0.0, 0.001]},
        {"path": "simulation.slippage", "values": [0.0, 0.001]}]})
    res = ir_strategy(body, user=None)
    assert res["success"] and res["axis"] == "parameter"
    assert len(res["buckets"]) == 4
    for b in res["buckets"].values():
        assert "mdd" in b and "cagr" in b      # 버킷 풍부지표(갭 A)


def test_rejects_on_signal_score():
    """on_signal 진입 + score 신호 → 구조 규칙 위반 거부."""
    body = {
        "signal": {"op": "data", "params": {"ref": "momentum_12_1m"}},  # score
        "universe": {"kind": "single", "symbols": ["AAA"]},
        "position": {"entry": {"mode": "on_signal"}},
    }
    res = ir_strategy(body, user=None)
    assert res["success"] is False
    assert any(i["rule"] == "S-entry" for i in res["issues"])


def test_ir_backtest_persisted_to_saved_strategy():
    """저장된 IR 전략 백테스트(strategy_id 동봉)는 BacktestRun으로 영속 → 백테스트 내역에 표시.

    operand /backtest/run이 하던 영속화를 IR 경로로 이전(내 전략 '백테스트 내역' 탭 데이터).
    strategy_id 없는 시범 백테스트는 저장 안 됨(orphan 정책)도 함께 확인.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine

    from app.db import get_session
    from app.models import User
    from app.routers import ir as ir_router
    from app.routers import strategies as strat_router
    from app.security import create_access_token

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        u = User(email="bt@example.com"); s.add(u); s.commit(); s.refresh(u); uid = u.id
    app = FastAPI()
    app.include_router(ir_router.router)
    app.include_router(strat_router.router)

    def _ov():
        with Session(eng) as s:
            yield s
    app.dependency_overrides[get_session] = _ov
    c = TestClient(app)
    H = {"Authorization": f"Bearer {create_access_token(uid)}"}

    sdef = _factor_body()
    sid = c.post("/strategies", headers=H,
                 json={"definition": sdef, "run_mode": "draft", "engine": "ir"}).json()["id"]

    # 1) strategy_id 동봉 백테스트 → 영속
    r = c.post("/ir/strategy", headers=H, json={**sdef, "strategy_id": sid})
    assert r.status_code == 200 and r.json().get("success"), r.text
    bts = c.get(f"/strategies/{sid}/backtests", headers=H).json()
    assert len(bts) == 1, bts
    assert bts[0]["version_no"] == 1
    assert "total_return" in (bts[0]["metrics"] or {})

    # 2) strategy_id 없는 시범 백테스트 → 저장 안 됨(내역 그대로 1건)
    c.post("/ir/strategy", headers=H, json=sdef)
    assert len(c.get(f"/strategies/{sid}/backtests", headers=H).json()) == 1


def _compose_client():
    """TestClient + 2 유저 — 전략 조합(strat:) HTTP 경로·소유 경계 검증용."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine

    from app.db import get_session
    from app.models import User
    from app.routers import ir as ir_router
    from app.routers import strategies as strat_router
    from app.security import create_access_token

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    uids = {}
    with Session(eng) as s:
        for name in ("u1", "u2"):
            u = User(email=f"{name}@example.com"); s.add(u); s.commit(); s.refresh(u)
            uids[name] = u.id
    app = FastAPI()
    app.include_router(ir_router.router)
    app.include_router(strat_router.router)

    def _ov():
        with Session(eng) as s:
            yield s
    app.dependency_overrides[get_session] = _ov
    c = TestClient(app)
    hdr = {k: {"Authorization": f"Bearer {create_access_token(v)}"} for k, v in uids.items()}
    return c, hdr


def _parent_referencing(sid):
    return {"signal": {"op": "ts_delta", "params": {"window": 20},
                       "inputs": {"signal": {"op": "data", "params": {"ref": "Close"}}}},
            "universe": {"kind": "list", "symbols": [f"strat:{sid}"]},
            "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                         "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 1}},
            "simulation": {"initial_capital": 1e7}}


def test_strategy_composition_via_route():
    """저장 전략을 strat:<id>로 합성 자산화 — F3류 전략-of-전략 HTTP 경로."""
    c, hdr = _compose_client()
    sid = c.post("/strategies", headers=hdr["u1"],
                 json={"definition": _factor_body(), "run_mode": "draft", "engine": "ir"}).json()["id"]
    r = c.post("/ir/strategy", headers=hdr["u1"], json=_parent_referencing(sid))
    assert r.status_code == 200 and r.json().get("success"), r.text


def test_composition_blocks_cross_user_reference():
    """타 유저 전략을 strat:로 참조 불가(privacy 경계) — resolver가 None 반환 → 실패."""
    c, hdr = _compose_client()
    sid = c.post("/strategies", headers=hdr["u1"],
                 json={"definition": _factor_body(), "run_mode": "draft", "engine": "ir"}).json()["id"]
    r = c.post("/ir/strategy", headers=hdr["u2"], json=_parent_referencing(sid))   # u2가 u1 전략 참조
    assert not r.json().get("success")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ── 리서치 질의 라우팅 — serialize_backtest KeyError('trades') 회귀 차단 (P6) ─────

def test_select_research_no_trades_keyerror():
    """select 결과는 trades 없는 분석 — serialize_backtest 경로로 가면 KeyError 500.

    research 브랜치가 결과를 그대로 JSON 통과시키는지 고정(프로덕션 /ir/strategy 500 근본수정).
    """
    body = {"query": "select", "universe": {"kind": "all"},
            "signal": {"op": "data", "params": {"ref": "momentum_12_1m"}},
            "select": {"top_n": 2, "descending": True}}
    res = ir_strategy(body, user=None)
    assert res["success"] and res["query"] == "select"
    assert isinstance(res.get("results"), list) and len(res["results"]) == 2


def test_describe_report_research_no_keyerror():
    body = {"query": "describe", "universe": {"kind": "single", "symbols": ["AAA"]},
            "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}
    res = ir_strategy(body, user=None)
    assert res["success"] and res.get("report") == "single" and res["symbol"] == "AAA"
    assert "price" in res and "risk" in res and "fundamentals" in res


def test_describe_portfolio_research_no_keyerror():
    body = {"query": "describe", "universe": {"kind": "portfolio", "symbols": ["AAA", "BBB"]},
            "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}
    res = ir_strategy(body, user=None)
    assert res["success"] and res.get("report") == "portfolio"
    assert "concentration" in res and "sector_exposure" in res


def test_extremize_research_no_keyerror():
    body = _factor_body(study={"axis": "entity", "reduction": "extremize",
                               "assets": ["AAA", "BBB", "CCC"],
                               "objective": {"metric": "cum_return", "direction": "max"}})
    res = ir_strategy(body, user=None)
    assert res["success"] and res.get("reduction") == "extremize"
    assert res["best"]["label"] == "AAA" and isinstance(res.get("ranked"), list)

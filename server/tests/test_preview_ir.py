"""Stage 2-A — IR 전략 다음날 후보 평가기(preview_engine._evaluate_ir_strategy).

수렴의 본질 검증: live preview가 ir_engine의 마지막 바 신호·선택을 그대로 써서
"다음날 진입할 종목"을 backtest와 일치하게 고르는가.

합성 dataset으로 마지막 바 신호가 참인 종목을 통제하고, 평가기가 정확히 그 종목만
후보로 + 합리적 qty를 내는지 확인. build_user_preview의 engine 디스패치도 검증.
freshness 게이트는 별도 관심사라 monkeypatch로 통과시킨다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient  # noqa: F401 (harness 패턴 일관)
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import preview_engine
from app.models import Strategy, SyncSnapshot, User


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-05-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes}, index=idx)


# 000001: 상승 → 마지막 종가(30)가 3일 평균(21.67) 위 → 신호 참.
# 000002: 하락 → 마지막 종가(5)가 3일 평균(11.67) 아래 → 신호 거짓.
_DATASET = {
    "000001": _df([10, 11, 12, 13, 14, 15, 16, 17, 18, 30]),
    "000002": _df([30, 28, 26, 24, 22, 20, 18, 16, 14, 5]),
}

# compare(Close > ts_mean(Close, 3)) — per-symbol(__SELF__).
_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {
        "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
        "right": {"op": "ts_mean", "params": {"window": 3},
                  "inputs": {"signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}},
    },
}


def _ir_def(entry_mode="on_signal", sizing_mode="pct_cash", amount_pct=10.0):
    return {
        "name": "IR 모멘텀",
        "universe": {"kind": "list", "symbols": ["000001", "000002"]},
        "signal": _SIGNAL,
        "position": {
            "direction": "long",
            "sizing": {"mode": sizing_mode, "amount_pct": amount_pct, "vol_window": 20},
            "entry": {"mode": entry_mode, "rebalance": "monthly"},
            "exit": {}, "overlays": {},
        },
        "simulation": {"initial_capital": 10_000_000},
        "study": {"axis": "none"},
    }


@pytest.fixture(autouse=True)
def _no_freshness_gate(monkeypatch):
    """freshness(거래정지·stale)는 별도 테스트 관심사 — 합성 과거 데이터를 통과시킨다."""
    monkeypatch.setattr(preview_engine, "_data_freshness_ok",
                        lambda *a, **k: (True, ""))


# ── 평가기 직접 — 마지막 바 신호 일치 ──────────────────────────────────────────

def test_ir_evaluator_picks_last_bar_signal_symbol():
    out = preview_engine._evaluate_ir_strategy(
        _ir_def(entry_mode="on_signal", sizing_mode="pct_cash", amount_pct=10.0),
        _DATASET, cash=10_000_000.0, held_keys=set(), master_by_code={})
    assert out["signal_passed"] is True, out
    syms = [c["symbol"] for c in out["candidates"]]
    assert syms == ["000001"], out          # 000002는 마지막 바 신호 거짓 → 제외
    c = out["candidates"][0]
    # pct_cash 10% = 100만 / 종가 30 = 33333주
    assert c["qty"] == 33333, c
    assert c["currency"] == "KRW"
    assert c["sizing_mode"] == "pct_cash"


def test_ir_evaluator_scheduled_equal_weight():
    out = preview_engine._evaluate_ir_strategy(
        _ir_def(entry_mode="scheduled", sizing_mode="equal_weight"),
        _DATASET, cash=10_000_000.0, held_keys=set(), master_by_code={})
    assert out["signal_passed"] is True
    syms = [c["symbol"] for c in out["candidates"]]
    assert syms == ["000001"]               # 조건 참 1종목 → 비중 100%
    # equal_weight 단일 → 전액(1000만)/30 = 333333주
    assert out["candidates"][0]["qty"] == 333333, out["candidates"][0]


def test_ir_evaluator_no_signal_no_candidates():
    """마지막 바 신호가 모두 거짓이면 후보 0 + signal_passed False."""
    flat = {"000002": _df([30, 28, 26, 24, 22, 20, 18, 16, 14, 5])}
    d = _ir_def()
    d["universe"] = {"kind": "single", "symbols": ["000002"]}
    out = preview_engine._evaluate_ir_strategy(
        d, flat, cash=10_000_000.0, held_keys=set(), master_by_code={})
    assert out["signal_passed"] is False
    assert out["candidates"] == []


# ── build_user_preview engine 디스패치 ─────────────────────────────────────────

def _seed_db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        user = User(email="ir@example.com")
        s.add(user); s.commit(); s.refresh(user)
        uid = user.id
        s.add(Strategy(user_id=uid, name="IR 모멘텀", run_mode="paper",
                       engine="ir", definition=_ir_def()))
        s.add(SyncSnapshot(user_id=uid, device_id=1,
                           payload={"balance": {"cash": 10_000_000}, "positions": []}))
        s.commit()
    return eng, uid


def test_build_user_preview_dispatches_ir(monkeypatch):
    eng, uid = _seed_db()
    # build_user_preview는 컬럼 프로젝션(_preview_dataset→get_projected)을 쓴다.
    # 합성 dataset을 양 경로(프로젝션·strat: 폴백)에 주입한다.
    monkeypatch.setattr(preview_engine, "get_projected",
                        lambda columns, symbols=None: _DATASET)
    monkeypatch.setattr(preview_engine, "get_dataset", lambda: _DATASET)
    monkeypatch.setattr(preview_engine.kis_master_cache, "get_master_list", lambda: [])
    with Session(eng) as session:
        preview = preview_engine.build_user_preview(session, uid, "test")
    assert preview["available"] is True, preview
    bs = preview["by_strategy"]
    assert len(bs) == 1 and bs[0]["strategy_name"] == "IR 모멘텀"
    cands = bs[0]["candidates"]
    assert [c["symbol"] for c in cands] == ["000001"], bs[0]


def test_build_user_preview_skips_operand(monkeypatch):
    """IR 단일 체제 — 레거시 operand 전략은 preview에서 skip(IR만 평가)."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        user = User(email="op@example.com")
        s.add(user); s.commit(); s.refresh(user)
        uid = user.id
        s.add(Strategy(user_id=uid, name="레거시", run_mode="paper", engine="operand",
                       definition={"name": "레거시", "trade_symbol": "000001",
                                   "buy": {"conditions": [], "logic": "AND"},
                                   "amount_pct": 10}))
        s.add(SyncSnapshot(user_id=uid, device_id=1,
                           payload={"balance": {"cash": 10_000_000}, "positions": []}))
        s.commit()
    # build_user_preview는 컬럼 프로젝션(_preview_dataset→get_projected)을 쓴다.
    # 합성 dataset을 양 경로(프로젝션·strat: 폴백)에 주입한다.
    monkeypatch.setattr(preview_engine, "get_projected",
                        lambda columns, symbols=None: _DATASET)
    monkeypatch.setattr(preview_engine, "get_dataset", lambda: _DATASET)
    monkeypatch.setattr(preview_engine.kis_master_cache, "get_master_list", lambda: [])
    with Session(eng) as session:
        preview = preview_engine.build_user_preview(session, uid, "test")
    # operand 전략은 디스패치에서 skip → by_strategy에 없음
    assert all(b["strategy_name"] != "레거시" for b in preview.get("by_strategy", []))


def test_build_user_preview_releases_connection_during_compute(monkeypatch):
    """C1(근본): 무거운 계산(_preview_dataset→get_projected, _evaluate_ir_strategy)
    동안 세션이 트랜잭션(=DB 커넥션)을 쥐고 있지 않아야 한다.

    외부 Neon 서버리스 Postgres가 유휴 연결을 suspend 중 끊으면 '늙은' 연결로의
    commit이 'server conn crashed?'로 실패해 preview 재생성이 통째로 누락되던
    근본 결함을 제거한 것이 C1. 계산 진입 시점에 session.in_transaction()이 False
    여야 함을 직접 단언한다(수정 전: 읽기 후 커넥션을 쥔 채 계산 → True → red).
    """
    eng, uid = _seed_db()
    monkeypatch.setattr(preview_engine, "get_dataset", lambda: _DATASET)
    monkeypatch.setattr(preview_engine.kis_master_cache, "get_master_list", lambda: [])
    seen: dict = {}

    def _spy_projected(columns, symbols=None):
        # 이 호출은 build_user_preview의 (B) 순수 계산 단계에서 일어난다.
        seen["in_txn"] = session.in_transaction()
        return _DATASET

    monkeypatch.setattr(preview_engine, "get_projected", _spy_projected)
    with Session(eng) as session:
        preview = preview_engine.build_user_preview(session, uid, "test")
    assert preview["available"] is True, preview
    assert seen.get("in_txn") is False, \
        "C1 위반: 무거운 계산 동안 세션이 DB 커넥션(트랜잭션)을 점유 중"

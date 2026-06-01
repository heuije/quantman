"""Task 12b — 정적 세부조건 라이브 바스켓(Strategy.live_basket) 형성·고정·초기화.

정적(refresh=='once_at_start') 세부조건 전략은 모의/실전 전환 후 첫 preview에서
당일 자격집합으로 바스켓을 1회 형성하고 그 후로는 고정한다(라이브는 backtest start가
아니라 go-live 시점에 동결). 동적(each_rebalance)은 바스켓을 만들지 않고 매번 재선별한다.

검증:
  · 정적 형성·persist — 첫 build_user_preview가 s.live_basket을 당일 자격집합으로 설정.
  · 정적 고정 — 자격이 뒤집힐 dataset으로 재실행해도 바스켓·후보가 형성 시점 집합 유지.
  · 동적 미persist — refresh=='each_rebalance'는 live_basket이 None으로 남음.
  · PUT 초기화 — 전략 수정 시 live_basket=None으로 재형성 준비.
  · 백테스트 무관 — live_basket은 IR이 아니므로 코어 백테스트가 읽지 않음(동적/정적 동일).

freshness 게이트는 별도 관심사라 monkeypatch로 통과시킨다(test_preview_ir와 동일 패턴).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app import preview_engine
from app.db import get_session
from app.models import Strategy, SyncSnapshot, User
from app.routers import strategies as strategies_router
from app.security import create_access_token

_SYMS = ["000001", "000002", "000003", "000004"]


# ── 조건/신호 빌더 (프리미티브 조합 — test_screener.py와 동일 어휘) ───────────────

def _data(ref):
    return {"op": "data", "params": {"ref": ref}}


def _const(v):
    return {"op": "const", "params": {"value": v}}


# 세부조건 = 시총(market_cap) 횡단순위 상위 2(count·desc). 시점값으로 자격 판정 → dataset의
# market_cap을 바꾸면 당일 자격집합이 뒤집힌다(정적 고정 검증용).
_RANK_TOP2 = {
    "op": "compare", "params": {"op": "<="},
    "inputs": {"left": {"op": "rank", "params": {"descending": True, "unit": "count"},
                        "inputs": {"signal": _data("market_cap")}},
               "right": _const(2)},
}

# 매수 신호 = Close > ts_mean(Close, 3) (per-symbol). 상승 종목은 마지막 바에서 참.
_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {"left": _data("__SELF__.Close"),
               "right": {"op": "ts_mean", "params": {"window": 3},
                         "inputs": {"signal": _data("__SELF__.Close")}}},
}


def _ir_def(refresh: str) -> dict:
    """list 유니버스(4종목) + 시총 top2 세부조건 + scheduled 진입(on_signal 아님 →
    이벤트+세부조건 라이브 차단 게이트에 안 걸림)."""
    return {
        "name": "정적 세부조건",
        "universe": {"kind": "list", "symbols": list(_SYMS),
                     "screener": {"condition": _RANK_TOP2, "refresh": refresh}},
        "signal": _SIGNAL,
        "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                     "entry": {"mode": "scheduled", "rebalance": "monthly", "top_n": 4},
                     "exit": {}, "overlays": {}},
        "simulation": {"initial_capital": 10_000_000},
        "sweep": {"axis": "none"},
    }


def _mk(market_cap: float) -> pd.DataFrame:
    """상승 Close(신호 항상 참) + 고정 market_cap. market_cap이 자격(횡단순위)을 가른다."""
    idx = pd.date_range("2026-05-01", periods=12, freq="B")
    closes = np.linspace(10.0, 30.0, 12)
    return pd.DataFrame(
        {"Open": closes, "High": closes, "Low": closes, "Close": closes,
         "Volume": 1e6, "market_cap": float(market_cap)}, index=idx)


# A·B 대형(top2) → 형성 시점 자격집합 = {000001, 000002}
_DATASET_AB = {"000001": _mk(100), "000002": _mk(90), "000003": _mk(10), "000004": _mk(8)}
# 자격 뒤집힘 — C·D 대형(top2). 정적이면 바스켓은 여전히 A·B여야 한다.
_DATASET_CD = {"000001": _mk(10), "000002": _mk(8), "000003": _mk(100), "000004": _mk(90)}


@pytest.fixture(autouse=True)
def _no_freshness_gate(monkeypatch):
    monkeypatch.setattr(preview_engine, "_data_freshness_ok", lambda *a, **k: (True, ""))


# ── DB 시드 헬퍼 ──────────────────────────────────────────────────────────────

def _seed(refresh: str):
    """인메모리 DB + paper 정적/동적 전략 1개 + 잔고 스냅샷. (engine, uid, sid) 반환."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        user = User(email="basket@example.com")
        s.add(user); s.commit(); s.refresh(user)
        uid = user.id
        strat = Strategy(user_id=uid, name="정적 세부조건", run_mode="paper",
                         engine="ir", definition=_ir_def(refresh))
        s.add(strat)
        s.add(SyncSnapshot(user_id=uid, device_id=1,
                           payload={"balance": {"cash": 10_000_000}, "positions": []}))
        s.commit(); s.refresh(strat)
        sid = strat.id
    return eng, uid, sid


def _patch_dataset(monkeypatch, dataset):
    monkeypatch.setattr(preview_engine, "get_projected",
                        lambda columns, symbols=None: dataset)
    monkeypatch.setattr(preview_engine, "get_dataset", lambda: dataset)
    monkeypatch.setattr(preview_engine.kis_master_cache, "get_master_list", lambda: [])


# ── 1) 정적 형성·persist ───────────────────────────────────────────────────────

def test_static_forms_and_persists_basket(monkeypatch):
    eng, uid, sid = _seed("once_at_start")
    _patch_dataset(monkeypatch, _DATASET_AB)
    with Session(eng) as session:
        preview = preview_engine.build_user_preview(session, uid, "test")
    assert preview["available"] is True, preview
    # 첫 preview가 당일 top2(=A,B)로 바스켓 형성·persist
    with Session(eng) as session:
        row = session.get(Strategy, sid)
        assert row.live_basket == ["000001", "000002"], row.live_basket
    # 후보도 형성 집합과 일치
    cands = [c["symbol"] for c in preview["by_strategy"][0]["candidates"]]
    assert cands == ["000001", "000002"], preview["by_strategy"][0]


# ── 2) 정적 고정 — 자격이 뒤집혀도 형성 시점 집합 유지 ──────────────────────────

def test_static_holds_frozen_basket(monkeypatch):
    eng, uid, sid = _seed("once_at_start")
    # 1차: A,B로 형성
    _patch_dataset(monkeypatch, _DATASET_AB)
    with Session(eng) as session:
        preview_engine.build_user_preview(session, uid, "test")
    with Session(eng) as session:
        assert session.get(Strategy, sid).live_basket == ["000001", "000002"]

    # 2차: 자격이 C,D로 뒤집힐 dataset으로 재실행 — 바스켓·후보는 여전히 A,B
    _patch_dataset(monkeypatch, _DATASET_CD)
    with Session(eng) as session:
        preview2 = preview_engine.build_user_preview(session, uid, "test")
    with Session(eng) as session:
        row = session.get(Strategy, sid)
        assert row.live_basket == ["000001", "000002"], row.live_basket   # 불변
    cands = [c["symbol"] for c in preview2["by_strategy"][0]["candidates"]]
    # 동적이었다면 C,D였을 것 — 고정 바스켓이라 A,B (재선별 안 함)
    assert cands == ["000001", "000002"], preview2["by_strategy"][0]


# ── 3) 동적 미persist ──────────────────────────────────────────────────────────

def test_dynamic_never_persists_basket(monkeypatch):
    eng, uid, sid = _seed("each_rebalance")
    _patch_dataset(monkeypatch, _DATASET_AB)
    with Session(eng) as session:
        preview = preview_engine.build_user_preview(session, uid, "test")
    assert preview["available"] is True
    with Session(eng) as session:
        assert session.get(Strategy, sid).live_basket is None
    # 동적이라도 후보는 당일 자격집합(A,B)으로 정상 평가
    cands = [c["symbol"] for c in preview["by_strategy"][0]["candidates"]]
    assert cands == ["000001", "000002"], preview["by_strategy"][0]


def test_dynamic_reflows_when_eligibility_flips(monkeypatch):
    """동적은 dataset이 바뀌면 후보도 따라 바뀐다(정적 고정과의 대비)."""
    eng, uid, _sid = _seed("each_rebalance")
    _patch_dataset(monkeypatch, _DATASET_CD)   # 자격 = C,D
    with Session(eng) as session:
        preview = preview_engine.build_user_preview(session, uid, "test")
    cands = [c["symbol"] for c in preview["by_strategy"][0]["candidates"]]
    assert cands == ["000003", "000004"], preview["by_strategy"][0]


# ── 3b) 형성일 자격 0개 → persist 안 함, 다음날 재시도(specified 동작) ──────────

# 필터형 세부조건 = market_cap > 50. 형성일 전부 미달이면 자격 0개 → 바스켓 미형성·재시도.
_FILTER_MC_GT_50 = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {"left": _data("market_cap"), "right": _const(50.0)},
}


def _ir_def_filter() -> dict:
    d = _ir_def("once_at_start")
    d["universe"]["screener"]["condition"] = _FILTER_MC_GT_50
    return d


def test_static_empty_formation_day_retries(monkeypatch):
    """형성일에 자격 종목이 0개면 바스켓을 만들지 않고 None 유지 — 다음 preview에서 재시도.

    backtest의 once_at_start(빈 바스켓 영구 동결)와 다른 라이브 의미: 전환일이 마침 무자격
    이라고 전략을 영구 무거래로 만들지 않는다(자격 종목이 생기는 첫 날 형성)."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        user = User(email="empty@example.com")
        s.add(user); s.commit(); s.refresh(user)
        uid = user.id
        strat = Strategy(user_id=uid, name="필터 정적", run_mode="paper",
                         engine="ir", definition=_ir_def_filter())
        s.add(strat)
        s.add(SyncSnapshot(user_id=uid, device_id=1,
                           payload={"balance": {"cash": 10_000_000}, "positions": []}))
        s.commit(); s.refresh(strat)
        sid = strat.id

    # 형성일: 전 종목 market_cap ≤ 50 → 자격 0개 → 미형성
    low = {c: _mk(10) for c in _SYMS}
    _patch_dataset(monkeypatch, low)
    with Session(eng) as session:
        preview_engine.build_user_preview(session, uid, "test")
    with Session(eng) as session:
        assert session.get(Strategy, sid).live_basket is None    # 미형성

    # 다음날: 000001만 자격(>50) → 그날 형성
    eligible = {"000001": _mk(100), "000002": _mk(10),
                "000003": _mk(10), "000004": _mk(10)}
    _patch_dataset(monkeypatch, eligible)
    with Session(eng) as session:
        preview_engine.build_user_preview(session, uid, "test")
    with Session(eng) as session:
        assert session.get(Strategy, sid).live_basket == ["000001"]


# ── 4) PUT 초기화 ──────────────────────────────────────────────────────────────

def _build_route_app(eng, uid, monkeypatch):
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: set(_SYMS))
    app = FastAPI()
    app.include_router(strategies_router.router)

    def _override():
        with Session(eng) as s:
            yield s
    app.dependency_overrides[get_session] = _override
    return TestClient(app), create_access_token(uid)


def test_put_clears_basket(monkeypatch):
    eng, uid, sid = _seed("once_at_start")
    # 바스켓 형성
    _patch_dataset(monkeypatch, _DATASET_AB)
    with Session(eng) as session:
        preview_engine.build_user_preview(session, uid, "test")
    with Session(eng) as session:
        assert session.get(Strategy, sid).live_basket == ["000001", "000002"]

    # PUT으로 전략 수정 → live_basket 초기화
    client, tok = _build_route_app(eng, uid, monkeypatch)
    body = {"definition": _ir_def("once_at_start"), "run_mode": "paper", "engine": "ir"}
    r = client.put(f"/strategies/{sid}", headers={"Authorization": f"Bearer {tok}"}, json=body)
    assert r.status_code == 200, r.text
    with Session(eng) as session:
        assert session.get(Strategy, sid).live_basket is None


# ── 5) 백테스트 무관 — live_basket은 IR이 아니므로 코어가 읽지 않음 ─────────────

def test_backtest_ignores_live_basket():
    """코어 백테스트(run_unified/_run_scheduled)는 live_basket을 모른다(IR에 없음).

    정적/동적 IR 정의는 그 자체로 backtest를 결정 — live_basket은 서버 라이브 상태일 뿐.
    backtest start 동결(_apply_refresh)은 코어 책임이고 live go-live 동결과 별개임을 확인."""
    sys.path.insert(0, str(_SERVER_DIR.parent / "core"))
    from quant_core.ir_engine import strategy_from_spec
    from quant_core.ir_engine.spec import StrategyIR

    # live_basket은 StrategyIR 스키마에 존재하지 않는다(IR atomic).
    assert "live_basket" not in StrategyIR.model_fields

    # 정적 정의의 backtest는 live_basket 없이도 정상 동작(코어가 IR만 본다).
    res = strategy_from_spec(_ir_def("once_at_start"), _DATASET_AB)
    assert res["success"], res

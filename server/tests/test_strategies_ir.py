"""Stage 1 (IR 수렴) — 전략 저장/불러오기 라운드트립 + 레거시 무손상.

전략 연구소(IR)가 "전략 만들기"(operand)를 대체하는 단계의 토대: 같은 /strategies
테이블에 engine 판별자로 두 표현을 공존시킨다. 검증:
  - IR 전략 create → get(engine='ir', 정의 보존) → update(버전 스냅샷) 라운드트립.
  - 레거시 operand 전략 create/get 무손상(engine='operand' 기본).
  - 교차 검증 — IR 정의를 operand engine으로 저장하면 422(침묵 손상 차단).

네트워크/lifespan 없이 인메모리 SQLite + 최소 앱.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.db import get_session
from app.models import User
from app.routers import strategies as strategies_router
from app.security import create_access_token

# ── 픽스처 정의 ───────────────────────────────────────────────────────────────

_IR_SIGNAL = {
    "op": "compare", "params": {"op": ">"},
    "inputs": {
        "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
        "right": {"op": "ts_mean", "params": {"window": 20},
                  "inputs": {"signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}},
    },
}
_IR_DEF = {
    "name": "연구소 모멘텀",
    "universe": {"kind": "single", "symbols": ["005930"]},
    "signal": _IR_SIGNAL,
    "position": {"direction": "long", "entry": {"mode": "on_signal"}},
    "simulation": {"initial_capital": 5_000_000},
}
_OPERAND_DEF = {
    "name": "레거시 룰",
    "trade_symbol": "005930",
    "buy": {"conditions": [], "logic": "AND"},
    "amount_pct": 10,
}


def _build():
    """인메모리 DB + strategies 라우터 + 시드 유저. (TestClient, jwt) 반환."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="t@example.com")
        s.add(user); s.commit(); s.refresh(user)
        user_id = user.id

    app = FastAPI()
    app.include_router(strategies_router.router)

    def _override():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _override
    return TestClient(app), create_access_token(user_id)


def _auth(tok: str):
    return {"Authorization": f"Bearer {tok}"}


def _seed_master(monkeypatch, codes) -> None:
    """G5 capability 게이트가 자산군을 판정하도록 KIS 마스터를 codes로 고정한다.

    게이트는 kis_master_cache.get_master_list()에서 심볼→market을 얻어 자산군을
    매핑한다(주식: KOSPI→kr_equity). 테스트 환경엔 마스터(네트워크)가 없으므로
    필요한 국내주식 코드를 KOSPI로 주입한다. 선물(코스피200선물 등)은 instrument_category가
    권위라 마스터 없이도 판정되므로 이 시드가 필요 없다."""
    rows = [{"symbol": c, "name": c, "market": "KOSPI"} for c in codes]
    monkeypatch.setattr("app.kis_master_cache.get_master_list", lambda: rows)


# ── IR 라운드트립 ─────────────────────────────────────────────────────────────

def test_ir_strategy_create_and_get_roundtrip():
    client, tok = _build()
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": _IR_DEF, "run_mode": "draft", "engine": "ir"})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["engine"] == "ir"
    assert created["name"] == "연구소 모멘텀"        # StrategyIR.name에서 도출

    sid = created["id"]
    got = client.get(f"/strategies/{sid}", headers=_auth(tok)).json()
    assert got["engine"] == "ir"
    # 정의 라운드트립 — 신호 트리·유니버스 보존
    assert got["definition"]["signal"]["op"] == "compare"
    assert got["definition"]["universe"]["symbols"] == ["005930"]
    assert got["definition"]["position"]["entry"]["mode"] == "on_signal"


def test_ir_strategy_update_snapshots_version(monkeypatch):
    # 모의 승격은 capability 게이트(G5)를 통과해야 한다 — 005930을 KOSPI 마스터로 시드해
    # kr_equity(KIS ok)로 판정되게 한다(테스트 환경엔 네트워크 마스터 없음).
    _seed_master(monkeypatch, {"005930"})
    client, tok = _build()
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "draft",
                            "engine": "ir"}).json()["id"]
    # 신호 변경 후 update
    edited = dict(_IR_DEF)
    edited["name"] = "연구소 모멘텀 v2"
    r = client.put(f"/strategies/{sid}", headers=_auth(tok),
                   json={"definition": edited, "run_mode": "paper", "engine": "ir"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "연구소 모멘텀 v2"
    assert body["engine"] == "ir"
    assert body["run_mode"] == "paper"
    # 변경 전 정의가 버전으로 보존 (initial v1 + manual_edit v2)
    versions = client.get(f"/strategies/{sid}/versions", headers=_auth(tok)).json()
    assert len(versions) >= 1


def test_ir_strategy_listed_with_engine():
    client, tok = _build()
    client.post("/strategies", headers=_auth(tok),
                json={"definition": _IR_DEF, "run_mode": "draft", "engine": "ir"})
    rows = client.get("/strategies", headers=_auth(tok)).json()
    assert len(rows) == 1
    assert rows[0]["engine"] == "ir"


# ── IR 단일 체제 — 기본 engine·operand 거부 ───────────────────────────────────

def test_default_engine_is_ir():
    """engine 미지정 create는 ir (IR 단일 체제 기본값)."""
    client, tok = _build()
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": _IR_DEF, "run_mode": "draft"})
    assert r.status_code == 201, r.text
    assert r.json()["engine"] == "ir"


def test_operand_engine_rejected():
    """operand engine은 더 이상 지원 안 함 — 422 (레거시 제거)."""
    client, tok = _build()
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": _OPERAND_DEF, "run_mode": "draft", "engine": "operand"})
    assert r.status_code == 422, r.text


def test_invalid_engine_rejected():
    client, tok = _build()
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": _IR_DEF, "run_mode": "draft", "engine": "bogus"})
    assert r.status_code == 422, r.text


# ── 레버리지 리서치 게이트 — leverage>1은 백테스트 전용(모의·실전 차단) ────────

def _lev_def(lev: float) -> dict:
    d = dict(_IR_DEF)
    d["simulation"] = {**_IR_DEF["simulation"], "leverage": lev}
    return d


def test_leverage_draft_allowed():
    """레버리지>1 전략은 draft(백테스트/리서치)로 저장 가능."""
    client, tok = _build()
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": _lev_def(2.0), "run_mode": "draft", "engine": "ir"})
    assert r.status_code == 201, r.text


def test_leverage_paper_rejected():
    """레버리지>1 전략을 모의로 저장하면 422(실거래 현금계좌로 체결 불가)."""
    client, tok = _build()
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": _lev_def(2.0), "run_mode": "paper", "engine": "ir"})
    assert r.status_code == 422, r.text


def test_leverage_live_rejected():
    """레버리지>1 전략을 실전으로 저장하면 422."""
    client, tok = _build()
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": _lev_def(3.0), "run_mode": "live", "engine": "ir"})
    assert r.status_code == 422, r.text


def test_unleveraged_paper_allowed(monkeypatch):
    """레버리지=1(기본)은 모의 적용 정상 — 게이트 회귀 가드."""
    # 모의 승격은 capability 게이트 통과 필요 — _IR_DEF 종목(005930)을 KOSPI로 시드.
    _seed_master(monkeypatch, {"005930"})
    client, tok = _build()
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": _lev_def(1.0), "run_mode": "paper", "engine": "ir"})
    assert r.status_code == 201, r.text


def test_leverage_promote_to_live_rejected_on_update():
    """draft 레버리지 전략을 update로 실전 승격하려 하면 422."""
    client, tok = _build()
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _lev_def(2.0), "run_mode": "draft",
                            "engine": "ir"}).json()["id"]
    r = client.put(f"/strategies/{sid}", headers=_auth(tok),
                   json={"definition": _lev_def(2.0), "run_mode": "live", "engine": "ir"})
    assert r.status_code == 422, r.text


# ── M1 방향 게이트 — long_short·비선물 숏은 라이브 미지원(4계층 정합) ─────────────
# 게이트 함수를 직접 호출(IR 검증 분리 — long_short의 score 요구 등과 무관하게 게이트만 검증).

def _gate(run_mode, direction, symbols, ack_unverified=False):
    return strategies_router._assert_live_tradable(
        run_mode, {"position": {"direction": direction},
                   "universe": {"kind": "single", "symbols": symbols}},
        account_broker="kis", ack_unverified=ack_unverified)


def test_gate_blocks_long_short_paper():
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _gate("paper", "long_short", ["005930"])
    assert e.value.status_code == 422


def test_gate_blocks_long_short_live():
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _gate("live", "long_short", ["005930"])
    assert e.value.status_code == 422


def test_gate_blocks_short_on_equity():
    # 현금계좌로 주식 공매도 불가 → 비선물 숏은 차단
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _gate("paper", "short", ["005930"])
    assert e.value.status_code == 422


def test_gate_allows_short_on_futures():
    # 숏+선물은 게이트 통과(선물은 sell-to-open 지원). kr_futures는 verified=False라
    # live 승격엔 미검증 확인(ack)이 필요 — capability 게이트의 새 규칙.
    _gate("live", "short", ["코스피200선물"], ack_unverified=True)   # 예외 없으면 통과


def test_gate_allows_long_on_equity(monkeypatch):
    _seed_master(monkeypatch, {"005930"})
    _gate("paper", "long", ["005930"])            # 회귀 가드(long은 기존대로 허용)


def test_gate_draft_skips_direction_checks():
    # draft(백테스트)는 게이트 자체를 건너뜀 — long_short·숏 모두 허용
    _gate("draft", "long_short", ["005930"])
    _gate("draft", "short", ["005930"])


# ── M5d 부호방향 long_short 게이트 — on_signal directional + 선물만 라이브 허용 ──────

def _gate_ls(run_mode, mode, symbols, ack_unverified=False):
    return strategies_router._assert_live_tradable(
        run_mode, {"position": {"direction": "long_short", "entry": {"mode": mode}},
                   "universe": {"kind": "single", "symbols": list(symbols)}},
        account_broker="kis", ack_unverified=ack_unverified)


def test_gate_allows_directional_long_short_futures():
    # on_signal 부호방향 long_short + 선물은 라이브 가능(종목별 독립 방향, 엔진 _direction_for seam).
    # kr_futures는 verified=False라 live는 미검증 확인(ack) 필요. paper는 ack 불필요.
    _gate_ls("paper", "on_signal", ["코스피200선물"])                    # 예외 없으면 통과
    _gate_ls("live", "on_signal", ["코스피200선물"], ack_unverified=True)


def test_gate_blocks_directional_long_short_equity():
    # 부호방향이라도 비선물은 숏 레그 불가(현금계좌 공매도 불가) → 차단
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _gate_ls("paper", "on_signal", ["005930"])
    assert e.value.status_code == 422


def test_gate_blocks_scheduled_long_short_futures():
    # scheduled long_short = 횡단 랭킹 → 라이브 단방향 체결기가 재현 못 함 → 선물이어도 차단
    # (G2 방향 게이트에서 차단 — G5 capability에 도달하기 전).
    import pytest
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _gate_ls("paper", "scheduled", ["코스피200선물"])
    assert e.value.status_code == 422


# ── 당일매매 게이트 — Stage B: 종가청산 사이클 배선됨 → hold_days=0 paper/live 허용 ──

def _gate_exit(run_mode, hold_days, symbols=("005930",)):
    return strategies_router._assert_live_tradable(
        run_mode, {"position": {"direction": "long", "exit": {"hold_days": hold_days}},
                   "universe": {"kind": "single", "symbols": list(symbols)}},
        account_broker="kis")


def test_gate_allows_intraday_day_trade_paper(monkeypatch):
    # Stage B: 자동매매 종가청산 사이클(liquidate_day_trades + scheduler)이 배선돼
    # hold_days=0 당일매매가 paper/live로 승격 가능 — 게이트가 더는 차단하지 않는다.
    # 005930=kr_equity(KIS ok·verified)라 live도 ack 불필요.
    _seed_master(monkeypatch, {"005930"})
    _gate_exit("paper", 0)            # 예외 없으면 통과
    _gate_exit("live", 0)


def test_gate_allows_hold_days_positive(monkeypatch):
    # hold_days>=1은 기존대로 허용(회귀 가드)
    _seed_master(monkeypatch, {"005930"})
    _gate_exit("paper", 5)            # 예외 없으면 통과


def test_gate_draft_skips_intraday_check():
    # draft(백테스트)는 당일매매 허용
    _gate_exit("draft", 0)


# M1a 선물 라이브 개방 플래그(QP_FUTURES_LIVE_ENABLED) 테스트 4종은 삭제됨 —
# 플래그·화이트리스트가 capability 게이트(G5)로 대체돼 더는 존재하지 않는다.
# 선물 라이브 동작은 test_gate_allows_short_on_futures(kr_futures+ack)와
# test_autotrade_gate.py(국내선물 통과·해외선물 "준비 중" 차단·미검증 ack)가 커버한다.


# ── 비매매 유니버스·이벤트 세부조건 게이트 — 모의/실전 승격 차단 ─────────────────
#
# tradable 판정은 KIS 마스터(네트워크 다운로드)에 의존해 테스트 환경에선 비어 있다
# (테스트는 네트워크 미사용). 따라서 가드 차단/허용 분기는 strategies_router의
# tradable_symbols를 고정 집합으로 monkeypatch해 결정론적으로 검증한다.
# 단, 실제 헬퍼(app.symbols.tradable_symbols)의 멤버십 로직은 별도 테스트에서
# 마스터·인덱스 캐시를 인메모리 시드해 네트워크 없이 직접 exercise한다(아래 마지막).

# entry.mode=scheduled — 전체/단일 유니버스가 논리검증(validate_strategy)을 통과해
# 가드까지 도달하게 한다(on_signal + all 유니버스는 검증에서 먼저 막힘).
_SCHED_ENTRY = {"mode": "scheduled", "schedule": {"freq": "monthly"}}


def _uni_def(universe: dict, entry: dict | None = None) -> dict:
    return {"name": "t", "universe": universe, "signal": _IR_SIGNAL,
            "position": {"direction": "long", "entry": entry or _SCHED_ENTRY},
            "simulation": {"initial_capital": 5_000_000}}


def _patch_tradable(monkeypatch, codes: set[str]) -> None:
    # G5는 더는 화이트리스트가 아니라 capability 검사 — 국내주식 코드를 KOSPI 마스터로
    # 시드하면 kr_equity(KIS ok·verified)로 판정돼 게이트를 통과한다.
    _seed_master(monkeypatch, codes)


def test_nontradable_symbol_paper_rejected(monkeypatch):
    """비매매 종목(자동매매 불가)이 섞이면 모의 적용 422 — 로컬앱이 주문 못 함."""
    _patch_tradable(monkeypatch, {"005930"})
    client, tok = _build()
    d = _uni_def({"kind": "list", "symbols": ["005930", "S&P500"]})
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": d, "run_mode": "paper", "engine": "ir"})
    assert r.status_code == 422, r.text
    assert "S&P500" in r.json()["detail"]


def test_empty_all_universe_paper_rejected(monkeypatch):
    """전체(kind=all·심볼 미선택) 유니버스는 모의·실전 불가 422 — 종목 직접 선택 필요."""
    _patch_tradable(monkeypatch, {"005930"})
    client, tok = _build()
    d = _uni_def({"kind": "all", "symbols": []})
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": d, "run_mode": "paper", "engine": "ir"})
    assert r.status_code == 422, r.text
    assert "직접 선택" in r.json()["detail"]


def test_event_screener_paper_rejected(monkeypatch):
    """이벤트 진입(on_signal) + universe.screener.condition은 라이브 미지원 422."""
    _patch_tradable(monkeypatch, {"005930"})
    client, tok = _build()
    cond = {"op": "compare", "params": {"op": ">"},
            "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                       "right": {"op": "const", "params": {"value": 1000}}}}
    d = _uni_def({"kind": "list", "symbols": ["005930"], "screener": {"condition": cond}},
                 entry={"mode": "on_signal"})
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": d, "run_mode": "paper", "engine": "ir"})
    assert r.status_code == 422, r.text
    assert "백테스트 전용" in r.json()["detail"]


def test_tradable_universe_paper_allowed(monkeypatch):
    """매매가능 종목만 선택하면 모의 적용 정상(가드 회귀 가드)."""
    _patch_tradable(monkeypatch, {"005930", "000660"})
    client, tok = _build()
    d = _uni_def({"kind": "list", "symbols": ["005930", "000660"]})
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": d, "run_mode": "paper", "engine": "ir"})
    assert r.status_code == 201, r.text


def test_event_screener_draft_allowed(monkeypatch):
    """이벤트 진입 + 세부조건은 draft(백테스트)로는 저장 가능 — 가드는 모의/실전만."""
    _patch_tradable(monkeypatch, {"005930"})
    client, tok = _build()
    cond = {"op": "compare", "params": {"op": ">"},
            "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                       "right": {"op": "const", "params": {"value": 1000}}}}
    d = _uni_def({"kind": "list", "symbols": ["005930"], "screener": {"condition": cond}},
                 entry={"mode": "on_signal"})
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": d, "run_mode": "draft", "engine": "ir"})
    assert r.status_code == 201, r.text


def test_real_tradable_helper_membership(monkeypatch):
    """실제 헬퍼(app.symbols.tradable_symbols)의 멤버십 로직을 네트워크 없이 exercise.

    마스터·인덱스 캐시를 인메모리 시드: 마스터에 005930만 두면 데이터 인덱스에
    OHLC가 있는 005930은 tradable, 마스터에 없는 합성지수(S&P500)는 비매매가 된다.
    parquet 인덱스가 환경에 없으면(클린 CI) skip — 가드 분기 테스트는 위에서
    monkeypatch로 이미 결정론적으로 커버한다.
    """
    import pytest
    from datetime import datetime, timezone
    from app import data_cache, kis_master_cache
    from app.symbols import tradable_symbols

    idx = data_cache.get_symbol_index()
    if "005930" not in idx or "S&P500" not in idx:
        pytest.skip("데이터 인덱스(parquet) 미존재 — 실제 헬퍼 검증 불가 환경")

    with kis_master_cache._lock:
        kis_master_cache._state["by_symbol"] = {
            "005930": {"name": "삼성전자", "market": "KOSPI",
                       "kind": "stock", "currency": "KRW"}}
        kis_master_cache._state["symbols"] = {"005930"}
        kis_master_cache._state["fetched_at"] = datetime.now(timezone.utc)
    try:
        t = tradable_symbols()
        assert "005930" in t            # 마스터 존재 + OHLC 보유 → 매매가능
        assert "S&P500" not in t        # 합성지수: 마스터 없음 → 비매매
    finally:
        with kis_master_cache._lock:    # 캐시 오염 방지 — 다른 테스트 격리
            kis_master_cache._state["by_symbol"] = {}
            kis_master_cache._state["symbols"] = set()
            kis_master_cache._state["fetched_at"] = None


# ── 삭제 게이트 + 강등 timestamp 초기화 (전략 삭제 라이프사이클) ──────────────────

def test_delete_draft_strategy_allowed():
    """draft 전략은 자유 삭제 — 자동매매 중이 아니므로."""
    client, tok = _build()
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "draft", "engine": "ir"}).json()["id"]
    r = client.delete(f"/strategies/{sid}", headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert client.get(f"/strategies/{sid}", headers=_auth(tok)).status_code == 404


def test_delete_paper_strategy_blocked(monkeypatch):
    """모의(paper) 자동매매 중 전략은 삭제 불가 409 — 먼저 정지(draft 전환)해야 한다.

    근본: 보유 포지션이 통지 없이 고아가 되는 사고의 원천(서버 무점검 삭제)을
    진입점에서 차단. 서버는 보안상 실시간 보유를 모르므로 권위 있는 단일 신호
    run_mode로 게이트한다.
    """
    _patch_tradable(monkeypatch, {"005930"})
    client, tok = _build()
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "paper", "engine": "ir"}).json()["id"]
    r = client.delete(f"/strategies/{sid}", headers=_auth(tok))
    assert r.status_code == 409, r.text
    # 차단됐으므로 전략은 그대로 존재
    assert client.get(f"/strategies/{sid}", headers=_auth(tok)).status_code == 200


def test_delete_live_strategy_blocked(monkeypatch):
    """실전(live) 자동매매 중 전략도 동일하게 삭제 불가 409 (자금 안전)."""
    _patch_tradable(monkeypatch, {"005930"})
    client, tok = _build()
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "live", "engine": "ir"}).json()["id"]
    r = client.delete(f"/strategies/{sid}", headers=_auth(tok))
    assert r.status_code == 409, r.text


def test_demote_to_draft_clears_started_timestamps(monkeypatch):
    """paper/live→draft 강등 시 활성기간 timestamp를 초기화 — 재승격 시 stale 기준점 방지."""
    _patch_tradable(monkeypatch, {"005930"})
    client, tok = _build()
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "live", "engine": "ir"}).json()["id"]
    # 직접 live 생성 → live_started_at 세팅됨
    assert client.get(f"/strategies/{sid}", headers=_auth(tok)).json()["live_started_at"] is not None
    # draft로 강등(=정지)
    r = client.put(f"/strategies/{sid}", headers=_auth(tok),
                   json={"definition": _IR_DEF, "run_mode": "draft", "engine": "ir"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_mode"] == "draft"
    assert body["live_started_at"] is None
    assert body["paper_started_at"] is None


def test_stop_endpoint_demotes_to_draft(monkeypatch):
    """정지(/stop) — paper/live 전략을 draft로 내리고 활성기간 timestamp 초기화.

    비파괴: 삭제와 달리 정의·버전·백테스트를 보존한다. 정지 후엔 삭제 가능.
    """
    _patch_tradable(monkeypatch, {"005930"})
    client, tok = _build()
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "live", "engine": "ir"}).json()["id"]
    r = client.post(f"/strategies/{sid}/stop", headers=_auth(tok))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_mode"] == "draft"
    assert body["live_started_at"] is None
    # 정지 후엔 삭제 게이트 통과
    assert client.delete(f"/strategies/{sid}", headers=_auth(tok)).status_code == 200


def test_stop_endpoint_idempotent_on_draft():
    """이미 draft면 정지는 no-op 200 (멱등)."""
    client, tok = _build()
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "draft", "engine": "ir"}).json()["id"]
    r = client.post(f"/strategies/{sid}/stop", headers=_auth(tok))
    assert r.status_code == 200, r.text
    assert r.json()["run_mode"] == "draft"


def test_stop_endpoint_no_version_pollution(monkeypatch):
    """정지는 정의 편집이 아니므로 버전 스냅샷을 만들지 않는다."""
    _patch_tradable(monkeypatch, {"005930"})
    client, tok = _build()
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "live", "engine": "ir"}).json()["id"]
    before = len(client.get(f"/strategies/{sid}/versions", headers=_auth(tok)).json())
    client.post(f"/strategies/{sid}/stop", headers=_auth(tok))
    after = len(client.get(f"/strategies/{sid}/versions", headers=_auth(tok)).json())
    assert after == before


# ── 논리 정합성 게이트 — 무의미·모순 로직은 저장 차단(모든 모드) ────────────────

def _def_with_signal(sig: dict) -> dict:
    return {"name": "t", "universe": {"kind": "single", "symbols": ["005930"]},
            "signal": sig, "position": {"direction": "long", "entry": {"mode": "on_signal"}},
            "simulation": {"initial_capital": 5_000_000}}


_CONST_SIG = {"op": "compare", "params": {"op": ">"},
              "inputs": {"left": {"op": "const", "params": {"value": 5}},
                         "right": {"op": "const", "params": {"value": 0}}}}
_SELF_CMP_SIG = {"op": "compare", "params": {"op": ">"},
                 "inputs": {"left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                            "right": {"op": "data", "params": {"ref": "__SELF__.Close"}}}}


def test_save_rejects_constant_signal_even_draft():
    """상수 신호(시장 미참조) → draft 저장도 422 (모든 저장 에러 차단)."""
    client, tok = _build()
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": _def_with_signal(_CONST_SIG), "run_mode": "draft", "engine": "ir"})
    assert r.status_code == 422, r.text


def test_save_rejects_self_comparison():
    """X > X (동어반복/모순) → 422."""
    client, tok = _build()
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": _def_with_signal(_SELF_CMP_SIG), "run_mode": "draft", "engine": "ir"})
    assert r.status_code == 422, r.text


def test_ir_validate_endpoint_flags_constant_signal(monkeypatch):
    """/ir/validate가 백테스트 없이 논리 오류를 이슈로 반환(UI 실시간 검증). 데이터셋 불요."""
    from app.routers import ir as ir_router
    monkeypatch.setattr(ir_router, "get_dataset", lambda: {})   # 데이터셋 로드 회피

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        u = User(email="t@example.com"); s.add(u); s.commit(); s.refresh(u); uid = u.id
    app = FastAPI()
    app.include_router(ir_router.router)

    def _ov():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _ov
    client = TestClient(app)
    tok = create_access_token(uid)

    r = client.post("/ir/validate", headers=_auth(tok), json=_def_with_signal(_CONST_SIG))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert any(i["rule"] == "M-const" for i in body["issues"])


# ── 실전 승격 시 손익률 기준자본(live_capital_at_start) 캡처 ──────────────────────
#
# 결함: live 승격 경로가 live_capital_at_start를 한 번도 set하지 않아 stats의 pnl_pct가
# 영구 None이었다. 승격 시점에 최신 SyncSnapshot.payload.balance.total_eval(총 평가금액)을
# 기준자본으로 캡처한다. 스냅샷/평가금액 부재 시 best-effort로 None(없는 값 미창조).

def _build_eng():
    """_build()의 변형 — engine·user_id도 반환해 스냅샷 시딩·DB 필드 직접 검증을 허용."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(email="cap@example.com")
        s.add(user); s.commit(); s.refresh(user)
        uid = user.id
    app = FastAPI()
    app.include_router(strategies_router.router)

    def _override():
        with Session(engine) as s:
            yield s
    app.dependency_overrides[get_session] = _override
    return TestClient(app), create_access_token(uid), engine, uid


def _seed_snapshot(engine, uid: int, *, total_eval, pnl=500_000):
    """승격 전 잔고 스냅샷 시딩 — 평가금액 + 전략명 매칭 누적손익.

    total_eval=None이면 balance에 평가금액을 넣지 않는다(부재 케이스 재현)."""
    from app.models import SyncSnapshot
    balance = {} if total_eval is None else {"total_eval": total_eval}
    with Session(engine) as s:
        s.add(SyncSnapshot(user_id=uid, device_id=1, payload={
            "balance": balance,
            "strategy_pnl": {"by_strategy": [
                {"strategy": _IR_DEF["name"], "pnl": pnl, "trades": 3, "win_rate": 0.6}]},
            "positions": []}))
        s.commit()


def _capital_of(engine, sid: int):
    """저장된 전략의 live_capital_at_start 필드를 DB에서 직접 읽는다(StrategyOut 미노출)."""
    from sqlmodel import select as _select
    from app.models import Strategy
    with Session(engine) as s:
        return s.exec(_select(Strategy).where(Strategy.id == sid)).first().live_capital_at_start


def test_live_create_captures_capital_at_start(monkeypatch):
    """create로 직접 live 생성 시 최신 스냅샷 평가금액을 기준자본으로 캡처 →
    DB 필드가 채워지고 stats의 pnl_pct가 계산된다(영구 None 결함 회귀 가드)."""
    _patch_tradable(monkeypatch, {"005930"})
    client, tok, engine, uid = _build_eng()
    _seed_snapshot(engine, uid, total_eval=10_000_000, pnl=500_000)
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "live", "engine": "ir"}).json()["id"]
    assert _capital_of(engine, sid) == 10_000_000
    stats = client.get(f"/strategies/{sid}/stats", headers=_auth(tok)).json()
    assert stats["pnl_total"] == 500_000
    assert round(stats["pnl_pct"], 6) == 5.0          # 50만 / 1000만 * 100


def test_live_promote_via_update_captures_capital(monkeypatch):
    """draft→live 승격(update)도 기준자본을 캡처한다."""
    _patch_tradable(monkeypatch, {"005930"})
    client, tok, engine, uid = _build_eng()
    _seed_snapshot(engine, uid, total_eval=20_000_000, pnl=1_000_000)
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "draft", "engine": "ir"}).json()["id"]
    assert _capital_of(engine, sid) is None           # draft 생성 — 미캡처
    r = client.put(f"/strategies/{sid}", headers=_auth(tok),
                   json={"definition": _IR_DEF, "run_mode": "live", "engine": "ir"})
    assert r.status_code == 200, r.text
    assert _capital_of(engine, sid) == 20_000_000
    stats = client.get(f"/strategies/{sid}/stats", headers=_auth(tok)).json()
    assert round(stats["pnl_pct"], 6) == 5.0          # 100만 / 2000만 * 100


def test_demote_to_draft_clears_capital_base(monkeypatch):
    """draft 강등 시 _clear_active_period가 기준자본을 None으로 초기화 →
    pnl_pct 다시 보류(재승격 시 stale 기준점 방지). 손익 자체(pnl_total)는 유지."""
    _patch_tradable(monkeypatch, {"005930"})
    client, tok, engine, uid = _build_eng()
    _seed_snapshot(engine, uid, total_eval=10_000_000, pnl=500_000)
    sid = client.post("/strategies", headers=_auth(tok),
                      json={"definition": _IR_DEF, "run_mode": "live", "engine": "ir"}).json()["id"]
    assert _capital_of(engine, sid) == 10_000_000
    r = client.put(f"/strategies/{sid}", headers=_auth(tok),
                   json={"definition": _IR_DEF, "run_mode": "draft", "engine": "ir"})
    assert r.status_code == 200, r.text
    assert _capital_of(engine, sid) is None
    stats = client.get(f"/strategies/{sid}/stats", headers=_auth(tok)).json()
    assert stats["pnl_total"] == 500_000              # 손익은 여전히 노출
    assert stats["pnl_pct"] is None                   # 기준자본 None → 손익률 보류


def test_live_promote_without_valid_eval_leaves_capital_none(monkeypatch):
    """best-effort — 평가금액(total_eval) 부재 스냅샷이면 기준자본 None(없는 값 미창조).
    승격은 차단되지 않고(게이트 없음) 201, pnl_total 노출·pnl_pct만 보류."""
    _patch_tradable(monkeypatch, {"005930"})
    client, tok, engine, uid = _build_eng()
    _seed_snapshot(engine, uid, total_eval=None, pnl=500_000)   # balance에 평가금액 없음
    r = client.post("/strategies", headers=_auth(tok),
                    json={"definition": _IR_DEF, "run_mode": "live", "engine": "ir"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert _capital_of(engine, sid) is None
    stats = client.get(f"/strategies/{sid}/stats", headers=_auth(tok)).json()
    assert stats["pnl_total"] == 500_000
    assert stats["pnl_pct"] is None

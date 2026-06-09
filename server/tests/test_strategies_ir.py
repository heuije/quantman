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
    # 모의 승격은 매매가능 유니버스를 요구한다(tradable 게이트). 테스트 환경엔
    # KIS 마스터가 없어(네트워크 미사용) 헬퍼를 _IR_DEF의 종목 집합으로 고정한다.
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"005930"})
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
    # 모의 승격은 매매가능 유니버스를 요구 — _IR_DEF 종목으로 tradable 헬퍼 고정.
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"005930"})
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

def _gate(run_mode, direction, symbols):
    from fastapi import HTTPException  # noqa: F401 (가독성)
    return strategies_router._assert_live_tradable(
        run_mode, {"position": {"direction": direction},
                   "universe": {"kind": "single", "symbols": symbols}})


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


def test_gate_allows_short_on_futures(monkeypatch):
    # 숏+선물은 게이트 통과(선물은 sell-to-open 지원) — ③ 위해 선물을 tradable로 고정
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"코스피200선물"})
    _gate("live", "short", ["코스피200선물"])     # 예외 없으면 통과


def test_gate_allows_long_on_equity(monkeypatch):
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"005930"})
    _gate("paper", "long", ["005930"])            # 회귀 가드(long은 기존대로 허용)


def test_gate_draft_skips_direction_checks():
    # draft(백테스트)는 게이트 자체를 건너뜀 — long_short·숏 모두 허용
    _gate("draft", "long_short", ["005930"])
    _gate("draft", "short", ["005930"])


# ── 당일매매 게이트 — hold_days=0은 자동매매 종가청산 미배선 → paper/live 차단 ──────

def _gate_exit(run_mode, hold_days, symbols=("005930",)):
    return strategies_router._assert_live_tradable(
        run_mode, {"position": {"direction": "long", "exit": {"hold_days": hold_days}},
                   "universe": {"kind": "single", "symbols": list(symbols)}})


def test_gate_blocks_intraday_day_trade_paper(monkeypatch):
    import pytest
    from fastapi import HTTPException
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"005930"})
    with pytest.raises(HTTPException) as e:
        _gate_exit("paper", 0)
    assert e.value.status_code == 422
    assert "당일매매" in e.value.detail


def test_gate_blocks_intraday_day_trade_live(monkeypatch):
    import pytest
    from fastapi import HTTPException
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"005930"})
    with pytest.raises(HTTPException) as e:
        _gate_exit("live", 0)
    assert e.value.status_code == 422


def test_gate_allows_hold_days_positive(monkeypatch):
    # hold_days>=1은 기존대로 허용(회귀 가드)
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"005930"})
    _gate_exit("paper", 5)            # 예외 없으면 통과


def test_gate_draft_skips_intraday_check():
    # draft(백테스트)는 당일매매 허용
    _gate_exit("draft", 0)


# ── M1a: 선물 라이브 개방 플래그(QP_FUTURES_LIVE_ENABLED) — KOSPI200만, 기본 OFF ────
def test_m1a_futures_blocked_when_flag_off(monkeypatch):
    # 플래그 OFF(기본) → KOSPI200 선물은 tradable에 없어 차단(현재 동작·휴면)
    import pytest
    from fastapi import HTTPException
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"005930"})
    monkeypatch.delenv("QP_FUTURES_LIVE_ENABLED", raising=False)
    with pytest.raises(HTTPException) as e:
        _gate("live", "long", ["코스피200선물"])
    assert e.value.status_code == 422


def test_m1a_futures_allowed_when_flag_on(monkeypatch):
    # 플래그 ON → KOSPI200 선물 라이브 승격 허용(M8 검증 후 운영자가 켬)
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"005930"})
    monkeypatch.setenv("QP_FUTURES_LIVE_ENABLED", "1")
    _gate("live", "long", ["코스피200선물"])       # 예외 없으면 통과


def test_m1a_short_kospi200_allowed_when_flag_on(monkeypatch):
    # 숏+KOSPI200(M1b forward-compat) + M1a 개방 → 통과
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"005930"})
    monkeypatch.setenv("QP_FUTURES_LIVE_ENABLED", "1")
    _gate("live", "short", ["코스피200선물"])


def test_m1a_only_kospi200_overseas_futures_still_blocked(monkeypatch):
    # KOSPI200만 개방 — 금선물(해외)은 플래그 ON이어도 tradable에 없어 차단
    import pytest
    from fastapi import HTTPException
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: {"005930"})
    monkeypatch.setenv("QP_FUTURES_LIVE_ENABLED", "1")
    with pytest.raises(HTTPException):
        _gate("live", "long", ["금선물"])


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
    monkeypatch.setattr(strategies_router, "tradable_symbols", lambda: set(codes))


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

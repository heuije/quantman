"""자동매매 건강 평가기(evaluate_health) 단위 테스트 + mwmw 인시던트 replay.

evaluate_health는 순수함수라 DB 불요 — 고정 now·실제 스냅샷 형상 주입으로 결정적 검증.
핵심: 2026-07-13 mwmw CON.parquet 사고의 **실제 pre-fix 스냅샷 형상**을 넣어 RED가 나오는지
(=이 모니터가 있었다면 사고를 잡았을지)를 실데이터로 증명한다.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import get_session
from app.health_monitor import (AMBER, GREEN, RED, UNKNOWN, compute_user_health,
                                evaluate_health, scan_and_alert)
from app.models import (Device, HeartbeatEvent, Strategy, SyncSnapshot, User)
from app.routers import admin as admin_router
from app.security import create_access_token


def _engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False},
                      poolclass=StaticPool)
    SQLModel.metadata.create_all(e)
    return e

_NOW = datetime(2026, 7, 13, 6, 56, tzinfo=timezone.utc)
_FRESH_HB = _NOW - timedelta(minutes=3)      # 앱 살아있음
_FRESH_SNAP = _NOW - timedelta(minutes=3)


def _eval(payload, *, hb=_FRESH_HB, snap=_FRESH_SNAP, live=2, now=_NOW, prior=None):
    return evaluate_health(payload, heartbeat_at=hb, snapshot_at=snap,
                           live_strategies=live, now=now, prior=prior)


def _codes(res):
    return {a["code"] for a in res["alert_reasons"]}


# ─────────────────────────────────────────────────────────────────────────
# mwmw CON.parquet 인시던트 replay (결정적 증거)
# ─────────────────────────────────────────────────────────────────────────

def test_mwmw_prefix_snapshot_flags_red():
    """mwmw 실제 07-13 08:56 pre-fix(v0.9.70) 스냅샷 형상 — 이 모니터가 RED로 잡아야 한다.

    구버전이라 diagnostics 부재. 하지만 decisions[].skip_no_data + n_bought=0 + 라이브 전략
    만으로 dead-man's-switch가 발동 → **텔레메트리 이전 데이터로도 사고를 잡는다**는 증거.
    """
    payload = {
        "cycle_summary": {"kind": "cycle", "market": "KRX", "n_buy_placed": 0,
                          "n_bought": 0, "n_sell_placed": 0, "n_errors": 0, "n_rejected": 0},
        "decisions": [{"action": "skip_no_data", "symbol": "코스피200선물",
                       "reason": "전일 종가 없음 — 매수 대상 종목이 dataset에 없음 (서버 dataset 갱신 대기)"}],
        # diagnostics 없음(구버전 클라)
    }
    res = _eval(payload)
    assert res["overall"] == RED
    assert res["conditions"]["decision_output"]["status"] == RED
    assert res["conditions"]["order_placement"]["status"] == RED
    assert "no_orders_data_starved" in _codes(res)


def test_mwmw_prefix_with_diagnostics_flags_red():
    """v0.9.72 유저였다면: diagnostics.dataset.loaded=1/129까지 실려 더 명확히 RED."""
    payload = {
        "cycle_summary": {"kind": "cycle", "market": "KRX", "n_bought": 0},
        "decisions": [{"action": "skip_no_data", "symbol": "코스피200선물", "reason": "..."}],
        "diagnostics": {"app_version": "0.9.72-beta",
                        "bundle": {"result": "ok", "n_files": 1, "n_failed": 128},
                        "dataset": {"needed": 129, "loaded": 1, "missing_sample": ["코스피200선물"]}},
    }
    res = _eval(payload)
    assert res["overall"] == RED
    assert res["conditions"]["data_freshness"]["status"] == RED
    assert "no_orders_data_starved" in _codes(res)


def test_mwmw_postfix_close_cycle_no_alert():
    """mwmw 실제 07-13 15:41 post-fix(v0.9.72) 종가 스냅샷 — 회복 후엔 알림 없어야 한다.

    발주(submitted)는 됐고 n_bought=0(미체결)이지만 데이터 결손·skip 없음 → dead-man's-switch
    미발동. 종가사이클은 diagnostics 미부착이라 data_freshness=UNKNOWN이나 RED 아님(오탐 방지 핵심).
    """
    payload = {
        "cycle_summary": {"kind": "day_trade_close", "market": "KRX",
                          "n_bought": 0, "n_errors": 0, "n_rejected": 0},
        "decisions": [],
    }
    res = _eval(payload)
    assert res["overall"] != RED
    assert res["alert_reasons"] == []


def test_mwmw_postfix_full_cycle_green():
    """회복 후 아침 풀사이클(diagnostics 포함, 129/129 로드) — data_freshness GREEN."""
    payload = {
        "cycle_summary": {"kind": "cycle", "market": "KRX", "n_bought": 3},
        "diagnostics": {"app_version": "0.9.72-beta",
                        "bundle": {"result": "ok", "n_files": 24045, "n_failed": 0},
                        "dataset": {"needed": 129, "loaded": 129, "missing_sample": []}},
    }
    res = _eval(payload)
    assert res["conditions"]["data_freshness"]["status"] == GREEN
    assert res["conditions"]["order_placement"]["status"] == GREEN
    assert res["overall"] == GREEN


# ─────────────────────────────────────────────────────────────────────────
# 조건별 단위 테스트
# ─────────────────────────────────────────────────────────────────────────

def test_no_live_strategies_is_not_paged():
    """라이브 전략 0 — 실행 대상 아니므로 dead-man's-switch·앱다운 미발동."""
    res = _eval({"cycle_summary": {"kind": "cycle", "n_bought": 0},
                 "decisions": [{"action": "skip_no_data", "symbol": "X"}]}, live=0)
    assert res["conditions"]["runtime"]["status"] == GREEN
    assert "no_orders_data_starved" not in _codes(res)


def test_bundle_failed_is_red_and_paged():
    payload = {"cycle_summary": {"kind": "cycle", "n_bought": 0},
               "diagnostics": {"bundle": {"result": "failed", "error_type": "WinError"},
                               "dataset": {"needed": 129, "loaded": 0}}}
    res = _eval(payload)
    assert res["conditions"]["data_freshness"]["status"] == RED
    assert "bundle_failed" in _codes(res)


def test_low_load_ratio_is_red():
    payload = {"cycle_summary": {"kind": "cycle", "n_bought": 0},
               "diagnostics": {"bundle": {"result": "ok"},
                               "dataset": {"needed": 100, "loaded": 20}}}
    res = _eval(payload)
    assert res["conditions"]["data_freshness"]["status"] == RED


def test_broker_missing_is_red_and_paged():
    payload = {"cycle_summary": {"kind": "cycle", "n_bought": 0},
               "health": {"account_handles": []}}
    res = _eval(payload)
    assert res["conditions"]["broker_ready"]["status"] == RED
    assert "broker_missing" in _codes(res)


def test_cycle_error_is_red_and_paged():
    payload = {"cycle_summary": {"kind": "cycle", "error": "KIS 잔고 조회 실패", "n_bought": 0}}
    res = _eval(payload)
    assert res["conditions"]["cycle_execution"]["status"] == RED
    assert "cycle_error" in _codes(res)


def test_stale_heartbeat_is_amber_not_paged():
    """heartbeat 끊김 = AMBER(대시보드 표시)이나 자동 페이징 안 함(야간 오탐 방지)."""
    payload = {"cycle_summary": {"kind": "cycle", "n_bought": 1}}
    res = _eval(payload, hb=_NOW - timedelta(hours=2))
    assert res["conditions"]["runtime"]["status"] == AMBER
    assert res["alert_reasons"] == []


def test_reconcile_drift_is_amber_not_paged():
    """mwmw excess:4 형상 — 대시보드엔 AMBER로 뜨되 자동 페이징 안 함(양성 잦음)."""
    payload = {"cycle_summary": {"kind": "post_close_settlement", "n_bought": 0},
               "reconciliation": {"has_drift": False,
                                  "external_extras": [{"symbol": "코스피200선물", "excess": 4}]}}
    res = _eval(payload)
    assert res["conditions"]["reconciliation"]["status"] == AMBER
    assert res["alert_reasons"] == []


def test_healthy_full_cycle_all_green():
    payload = {"cycle_summary": {"kind": "cycle", "market": "KRX", "n_bought": 2},
               "diagnostics": {"bundle": {"result": "ok"},
                               "dataset": {"needed": 50, "loaded": 50}},
               "health": {"account_handles": [{"account_id": "uuid", "broker": "kis"}]},
               "reconciliation": {"has_drift": False, "external_extras": []}}
    res = _eval(payload)
    assert res["overall"] == GREEN
    assert res["alert_reasons"] == []


def test_settlement_cycle_zero_orders_not_flagged():
    """정산·state_sync는 매매 사이클이 아니라 0발주가 정상 — dead-man's-switch 미발동."""
    for kind in ("post_close_settlement", "state_sync", "post_open_reconcile"):
        payload = {"cycle_summary": {"kind": kind, "n_bought": 0},
                   "decisions": [{"action": "skip_no_data", "symbol": "X"}]}
        res = _eval(payload)
        assert "no_orders_data_starved" not in _codes(res), kind


# ─────────────────────────────────────────────────────────────────────────
# Phase 2 — 클라 emit 플래그 소비 (broker_ready · n_skip_no_data)
# ─────────────────────────────────────────────────────────────────────────

def test_broker_ready_false_is_red_and_paged():
    """health.broker_ready=False(자격증명 실패) → RED + 페이징."""
    res = _eval({"cycle_summary": {"kind": "cycle", "n_bought": 0},
                 "health": {"broker_ready": False, "active_broker": "kis"}})
    assert res["conditions"]["broker_ready"]["status"] == RED
    assert "broker_missing" in _codes(res)


def test_broker_ready_true_is_green():
    res = _eval({"cycle_summary": {"kind": "cycle", "n_bought": 1},
                 "health": {"broker_ready": True, "active_broker": "kis"}})
    assert res["conditions"]["broker_ready"]["status"] == GREEN


def test_token_expiry_warning_is_amber_not_paged():
    """브로커 준비됐으나 클라가 보낸 토큰 만료 경고 → AMBER(대시보드만, 서버 tz 재해석 안 함)."""
    res = _eval({"cycle_summary": {"kind": "cycle", "n_bought": 1},
                 "health": {"broker_ready": True,
                            "warnings": ["KIS 토큰 만료 — 재발급 필요"]}})
    assert res["conditions"]["broker_ready"]["status"] == AMBER
    assert res["alert_reasons"] == []


def test_n_skip_no_data_aggregate_flags_red_without_decisions_list():
    """cycle_summary.n_skip_no_data 집계만으로(decisions 리스트 없이) RED — P2 승격 효과."""
    res = _eval({"cycle_summary": {"kind": "cycle", "n_bought": 0, "n_skip_no_data": 5}})
    assert res["conditions"]["decision_output"]["status"] == RED
    assert res["conditions"]["order_placement"]["status"] == RED
    assert "no_orders_data_starved" in _codes(res)


def test_old_client_no_broker_flag_stays_backward_compatible():
    """구버전 클라(broker_ready 없음)는 기존 account_handles 로직대로 — 하위호환."""
    res_missing = _eval({"cycle_summary": {"kind": "cycle", "n_bought": 0},
                         "health": {"account_handles": []}})
    assert res_missing["conditions"]["broker_ready"]["status"] == RED
    res_ok = _eval({"cycle_summary": {"kind": "cycle", "n_bought": 1},
                    "health": {"account_handles": [{"account_id": "u", "broker": "kis"}]}})
    assert res_ok["conditions"]["broker_ready"]["status"] == GREEN


# ─────────────────────────────────────────────────────────────────────────
# C4 전략유효성 — 데이터 stale 스킵 false-positive 수정 (2026-07-13)
# ─────────────────────────────────────────────────────────────────────────

def test_c4_stale_only_skips_is_green():
    """데이터 stale로만 스킵된 전략은 무효 아님 → C4 GREEN.

    mwmw 저녁 선물 stale(연속물 T+1 아침 재구성)이 '전략 무효 AMBER'로 오표시되던 회귀 가드.
    """
    payload = {"cycle_summary": {"kind": "post_close_settlement"},
               "next_day_preview": {"by_strategy": [
                   {"strategy_id": 27, "skipped": [
                       {"symbol": "코스피200선물", "reason": "데이터 stale (last=2026-07-10, 3일 지연)"}]},
                   {"strategy_id": 29, "skipped": [
                       {"symbol": "코스피200선물", "reason": "데이터 stale (last=2026-07-10, 3일 지연)"}]}]}}
    res = _eval(payload)
    assert res["conditions"]["strategy_validity"]["status"] == GREEN


def test_c4_structural_invalid_skip_is_amber():
    """유니버스 공집합 등 **구조적** 무효는 여전히 AMBER."""
    payload = {"cycle_summary": {"kind": "cycle"},
               "next_day_preview": {"by_strategy": [
                   {"strategy_id": 1, "skipped": [
                       {"symbol": "X", "reason": "유니버스에 종목이 없습니다"}]}]}}
    res = _eval(payload)
    assert res["conditions"]["strategy_validity"]["status"] == AMBER


def test_c4_mixed_stale_and_structural_is_amber():
    """stale 전략과 섞여도 하나라도 구조적 무효면 AMBER."""
    payload = {"cycle_summary": {"kind": "cycle"},
               "next_day_preview": {"by_strategy": [
                   {"strategy_id": 27, "skipped": [{"symbol": "코스피200선물", "reason": "데이터 stale"}]},
                   {"strategy_id": 1, "skipped": [{"symbol": "X", "reason": "IR 파싱 실패"}]}]}}
    res = _eval(payload)
    assert res["conditions"]["strategy_validity"]["status"] == AMBER


def test_c4_all_valid_is_green():
    payload = {"cycle_summary": {"kind": "cycle"},
               "next_day_preview": {"by_strategy": [
                   {"strategy_id": 27, "skipped": []},
                   {"strategy_id": 29, "skipped": []}]}}
    res = _eval(payload)
    assert res["conditions"]["strategy_validity"]["status"] == GREEN


# ─────────────────────────────────────────────────────────────────────────
# compute_user_health (DB-backed) + /admin/health 게이트
# ─────────────────────────────────────────────────────────────────────────

def _seed_user(s, email, *, live=1, created_days=5):
    u = User(email=email, created_at=_NOW - timedelta(days=created_days))
    s.add(u); s.commit(); s.refresh(u)
    for i in range(live):
        s.add(Strategy(user_id=u.id, name=f"strat{i}", run_mode="live"))
    s.commit()
    return u


def _seed_snapshot(s, user_id, payload, *, hb_min=3, snap_min=3):
    dev = Device(user_id=user_id, name="pc", token_hash=f"h{user_id}",
                 last_seen_at=_NOW - timedelta(minutes=snap_min))
    s.add(dev); s.commit(); s.refresh(dev)
    s.add(HeartbeatEvent(user_id=user_id, at=_NOW - timedelta(minutes=hb_min)))
    s.add(SyncSnapshot(user_id=user_id, device_id=dev.id,
                       received_at=_NOW - timedelta(minutes=snap_min), payload=payload))
    s.commit()


def test_compute_user_health_flags_mwmw_prefix():
    """mwmw 사고 형상을 DB에 넣고 compute_user_health가 RED 행을 내는지(엔드투엔드)."""
    with Session(_engine()) as s:
        u = _seed_user(s, "mwmw@example.com", live=2)
        _seed_snapshot(s, u.id, {
            "cycle_summary": {"kind": "cycle", "market": "KRX", "n_bought": 0},
            "decisions": [{"action": "skip_no_data", "symbol": "코스피200선물"}]})
        rows = compute_user_health(s, now=_NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["email"] == "mwmw@example.com"
    assert r["live_strategies"] == 2
    assert r["overall"] == RED
    assert "no_orders_data_starved" in {a["code"] for a in r["alert_reasons"]}


def test_compute_user_health_live_user_no_snapshot_is_amber():
    """라이브 전략 있는데 스냅샷 아예 없는 유저(설치했으나 push 0)도 평가되며 runtime AMBER."""
    with Session(_engine()) as s:
        _seed_user(s, "silent@example.com", live=1)
        rows = compute_user_health(s, now=_NOW)
    assert len(rows) == 1
    assert rows[0]["snapshot_at"] is None
    assert rows[0]["conditions"]["runtime"]["status"] == AMBER


def test_compute_user_health_red_sorts_first():
    """RED 유저가 GREEN 유저보다 위로 정렬된다(운영자가 문제 유저를 먼저 본다)."""
    with Session(_engine()) as s:
        healthy = _seed_user(s, "healthy@example.com", live=1)
        _seed_snapshot(s, healthy.id, {
            "cycle_summary": {"kind": "cycle", "n_bought": 2},
            "diagnostics": {"bundle": {"result": "ok"},
                            "dataset": {"needed": 50, "loaded": 50}}})
        broken = _seed_user(s, "broken@example.com", live=1)
        _seed_snapshot(s, broken.id, {
            "cycle_summary": {"kind": "cycle", "n_bought": 0},
            "decisions": [{"action": "skip_no_data", "symbol": "X"}]})
        rows = compute_user_health(s, now=_NOW)
    assert [r["email"] for r in rows][0] == "broken@example.com"
    assert rows[0]["overall"] == RED


def _gate_app():
    eng = _engine()
    with Session(eng) as s:
        admin = User(email="test@mystock.io")     # ADMIN_EMAILS 기본 포함
        normal = User(email="joe@example.com")
        s.add_all([admin, normal]); s.commit()
        s.refresh(admin); s.refresh(normal)
        admin_id, normal_id = admin.id, normal.id
    app = FastAPI(); app.include_router(admin_router.router)

    def _ov():
        with Session(eng) as s:
            yield s
    app.dependency_overrides[get_session] = _ov
    return (TestClient(app), create_access_token(admin_id),
            create_access_token(normal_id))


def test_admin_health_gate_blocks_non_admin():
    client, _a, normal_jwt = _gate_app()
    r = client.get("/admin/health", headers={"Authorization": f"Bearer {normal_jwt}"})
    assert r.status_code == 403


def test_admin_health_gate_allows_admin():
    client, admin_jwt, _n = _gate_app()
    r = client.get("/admin/health", headers={"Authorization": f"Bearer {admin_jwt}"})
    assert r.status_code == 200
    assert "users" in r.json()


# ─────────────────────────────────────────────────────────────────────────
# scan_and_alert (dead-man's-switch 전이 발송)
# ─────────────────────────────────────────────────────────────────────────

def test_scan_and_alert_sends_once_then_suppresses():
    """RED 전이 시 1회 발송, 같은 이상 반복 시 재발송 안 함(스팸 방지)."""
    sent: list[str] = []
    _send = lambda m: (sent.append(m), True)[1]
    with Session(_engine()) as s:
        u = _seed_user(s, "mwmw@example.com", live=1)
        _seed_snapshot(s, u.id, {
            "cycle_summary": {"kind": "cycle", "n_bought": 0},
            "decisions": [{"action": "skip_no_data", "symbol": "코스피200선물"}]})
        r1 = scan_and_alert(s, now=_NOW, send=_send)
        assert r1["alerts_sent"] == 1
        # 15분 뒤 재스캔 — 같은 alert 집합 → 재발송 안 함
        r2 = scan_and_alert(s, now=_NOW + timedelta(minutes=15), send=_send)
        assert r2["alerts_sent"] == 0
    assert len(sent) == 1
    assert "자동매매 건강 이상" in sent[0]
    assert "mwmw@example.com" in sent[0]


def test_scan_and_alert_clears_state_on_recovery():
    """이상→회복 시 상태 클리어 → 재이상 시 다시 발송되도록."""
    from app.models import HealthAlertState
    sent: list[str] = []
    _send = lambda m: (sent.append(m), True)[1]
    with Session(_engine()) as s:
        u = _seed_user(s, "mwmw@example.com", live=1)
        dev = Device(user_id=u.id, name="pc", token_hash="h",
                     last_seen_at=_NOW - timedelta(minutes=3))
        s.add(dev); s.commit(); s.refresh(dev)
        s.add(HeartbeatEvent(user_id=u.id, at=_NOW - timedelta(minutes=3)))
        # 이상 스냅샷 → 발송
        s.add(SyncSnapshot(user_id=u.id, device_id=dev.id, received_at=_NOW - timedelta(minutes=3),
              payload={"cycle_summary": {"kind": "cycle", "n_bought": 0},
                       "decisions": [{"action": "skip_no_data", "symbol": "X"}]}))
        s.commit()
        assert scan_and_alert(s, now=_NOW, send=_send)["alerts_sent"] == 1
        # 회복 스냅샷(정상 발주) 도착 → 상태 클리어
        s.add(SyncSnapshot(user_id=u.id, device_id=dev.id, received_at=_NOW,
              payload={"cycle_summary": {"kind": "cycle", "n_bought": 2},
                       "diagnostics": {"bundle": {"result": "ok"},
                                       "dataset": {"needed": 50, "loaded": 50}}}))
        s.commit()
        r = scan_and_alert(s, now=_NOW + timedelta(minutes=15), send=_send)
        assert r["alerts_sent"] == 0
        st = s.get(HealthAlertState, u.id)
        assert st.alert_codes == "" and st.recovered_at is not None

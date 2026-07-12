# -*- coding: utf-8 -*-
"""자동매매 템플릿 승격 게이트(§2.3) + 앱버전 게이트(§2.6) + preview 장중 스캔 대기(§3.3).

계약: 템플릿 전략은 일반 게이트의 kind=all·screener 차단을 우회하는 **별도 검증 세트**를
지나며, 모의(paper)는 브로커 스캔 TR 실측 전 차단, 라이브는 로컬앱 최소버전(최신 스냅샷
payload.app_version) 미달·미보고 시 차단(fail-safe). 템플릿 없는 일반 IR의 게이트는 무변경.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers.strategies import _assert_live_tradable, _validate, _ver_key


def _tpl_def(thr: float = 29.5) -> dict:
    return {
        "name": "상한가 마감형", "query": "simulate",
        "universe": {"kind": "all"},
        "signal": {"op": "compare", "params": {"op": ">="}, "inputs": {
            "left": {"op": "data", "params": {"ref": "__SELF__.pct_change_1d"}},
            "right": {"op": "const", "params": {"value": thr}}}},
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 100},
                     "entry": {"mode": "on_signal"},
                     "exit": {"hold_days": 1, "fill": "next_open"}},
        "simulation": {"fill": "close"},
        "template": {"id": "limit_up_close_v1", "max_daily_entries": 3},
    }


# ── 저장 검증(_validate) — draft 포함 전 모드에서 S-template 강제 ────────────────

def test_draft_save_validates_template_pattern():
    name, norm = _validate("ir", _tpl_def())
    assert norm["template"]["id"] == "limit_up_close_v1"


def test_save_rejects_pattern_deviation():
    d = _tpl_def()
    d["simulation"]["fill"] = "next_open"          # 템플릿 요건 이탈
    with pytest.raises(HTTPException) as e:
        _validate("ir", d)
    assert e.value.status_code == 422


# ── 승격 게이트 — 템플릿 분기 ────────────────────────────────────────────────

def test_draft_mode_passes_gate():
    _assert_live_tradable("draft", _tpl_def())     # no raise


def test_paper_blocked_until_measured():
    with pytest.raises(HTTPException) as e:
        _assert_live_tradable("paper", _tpl_def(), account_broker="kis")
    assert "모의투자 미지원" in str(e.value.detail)


def test_live_pure_gate_passes_both_brokers():
    # session 미제공 = 순수 검사(앱버전 skip) — KIS·LS 패리티: 두 브로커 모두 선언 지원.
    _assert_live_tradable("live", _tpl_def(), account_broker="kis")
    _assert_live_tradable("live", _tpl_def(), account_broker="ls")


def test_generic_gate_unchanged_for_plain_ir():
    # 템플릿 태그 없는 kind=all 전략은 종전대로 차단(회귀 가드 — 분기 오염 없음).
    d = _tpl_def()
    d.pop("template")
    with pytest.raises(HTTPException) as e:
        _assert_live_tradable("paper", d, account_broker="kis")
    assert "종목을 직접 선택" in str(e.value.detail)


# ── 앱버전 게이트(§2.6) — 최신 스냅샷 payload.app_version ────────────────────

class _Snap:
    def __init__(self, payload):
        self.payload = payload


class _Res:
    def __init__(self, snap):
        self._s = snap

    def first(self):
        return self._s


class _Sess:
    def __init__(self, snap):
        self._snap = snap

    def exec(self, _q):
        return _Res(self._snap)


def test_ver_key_ordering():
    assert _ver_key("0.9.72-beta") == (0, 9, 72)
    assert _ver_key("0.9.72-beta") > _ver_key("0.9.71-beta")
    assert _ver_key(None) == (0,)                  # 파싱 불가 → 최저(미달 처리)


def test_app_version_gate_pass_and_block():
    ok = _Sess(_Snap({"app_version": "0.9.72-beta"}))
    _assert_live_tradable("live", _tpl_def(), account_broker="kis",
                          session=ok, user_id=1)   # no raise

    old = _Sess(_Snap({"app_version": "0.9.71-beta"}))
    with pytest.raises(HTTPException) as e:
        _assert_live_tradable("live", _tpl_def(), account_broker="kis",
                              session=old, user_id=1)
    assert "로컬앱" in str(e.value.detail)

    no_snap = _Sess(None)                          # 스냅샷 없음(앱 미가동·구앱) — fail-safe 차단
    with pytest.raises(HTTPException):
        _assert_live_tradable("live", _tpl_def(), account_broker="kis",
                              session=no_snap, user_id=1)

    unreported = _Sess(_Snap({}))                  # 구앱: app_version 미보고 — 차단
    with pytest.raises(HTTPException):
        _assert_live_tradable("live", _tpl_def(), account_broker="kis",
                              session=unreported, user_id=1)


# ── preview — 템플릿 전략은 EOD 후보 대신 '장중 스캔 대기' 표면화(§3.3) ───────

def test_preview_marks_scan_waiting():
    from app.preview_engine import _evaluate_ir_strategy
    out = _evaluate_ir_strategy(_tpl_def(), {}, 1_000_000.0, set(), {})
    assert out["candidates"] == []
    assert any("장중 스캔 대기" in (sk.get("reason") or "") for sk in out["skipped"])


# ── P2: watchlist_trigger_v1 — 합산 admission control(§4)·preview 트리거 대기 ──

def _wl_def(symbols=("123450", "005930"), thr=10.0):
    return {
        "name": "돌파", "query": "simulate",
        "universe": {"kind": "list", "symbols": list(symbols)},
        "signal": {"op": "compare", "params": {"op": ">="}, "inputs": {
            "left": {"op": "data", "params": {"ref": "__SELF__.high_change_1d"}},
            "right": {"op": "const", "params": {"value": thr}}}},
        "position": {"direction": "long",
                     "sizing": {"mode": "pct_cash", "amount_pct": 100},
                     "entry": {"mode": "on_signal"},
                     "exit": {"hold_days": 3}},
        "simulation": {"fill": "trigger"},
        "template": {"id": "watchlist_trigger_v1", "max_daily_entries": 2},
    }


def test_watchlist_draft_save_and_pure_live_gate():
    _validate("ir", _wl_def())                                    # 저장 검증 통과
    _assert_live_tradable("live", _wl_def(), account_broker="kis")   # 순수 게이트(예산·앱버전 skip)
    _assert_live_tradable("live", _wl_def(), account_broker="ls")
    with pytest.raises(HTTPException):                            # 모의 — 실측 전 차단
        _assert_live_tradable("paper", _wl_def(), account_broker="kis")


def test_watchlist_budget_admission(monkeypatch):
    import app.routers.strategies as st
    monkeypatch.setattr(st, "_watch_used", lambda s, u, e: 29)    # 운용 중 29종목
    ok = _Sess(_Snap({"app_version": "0.9.73-beta"}))
    with pytest.raises(HTTPException) as ei:                      # 29+2 > 30 → 거부
        _assert_live_tradable("live", _wl_def(), account_broker="kis",
                              session=ok, user_id=1)
    assert "감시 예산 초과" in str(ei.value.detail)
    monkeypatch.setattr(st, "_watch_used", lambda s, u, e: 28)    # 28+2 ≤ 30 → 통과
    _assert_live_tradable("live", _wl_def(), account_broker="kis",
                          session=ok, user_id=1)


def test_watchlist_app_version_gate(monkeypatch):
    import app.routers.strategies as st
    monkeypatch.setattr(st, "_watch_used", lambda s, u, e: 0)
    old = _Sess(_Snap({"app_version": "0.9.72-beta"}))            # < 0.9.73 → 차단
    with pytest.raises(HTTPException) as ei:
        _assert_live_tradable("live", _wl_def(), account_broker="kis",
                              session=old, user_id=1)
    assert "로컬앱" in str(ei.value.detail)


def test_preview_marks_trigger_waiting():
    from app.preview_engine import _evaluate_ir_strategy
    out = _evaluate_ir_strategy(_wl_def(), {}, 1_000_000.0, set(), {})
    assert out["candidates"] == []
    assert any("장중 트리거 대기" in (sk.get("reason") or "") for sk in out["skipped"])

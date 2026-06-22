"""_LsAuth._post 토큰 무효화 자가복구 — 만료 전 토큰 500 → read는 새 토큰 1회 재인증,
order는 재인증·재시도 안 함(이중 발주 차단). 실측 근거: 2026-06-22 모의 중복로그인으로
만료 전 캐시 토큰이 500, 새 토큰 200 → 브로커가 죽은 토큰 무한 재사용하던 결함 수정."""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

import pytest  # noqa: E402
from localapp import ls_broker as lb  # noqa: E402


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._p = payload or {}
        self.text = text          # LS 500 본문(rsp_cd 분기용)

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError("HTTP %d" % self.status_code)


def _make(monkeypatch):
    b = object.__new__(lb.LsBroker)
    b.base = lb._BASE
    forces = []

    def fake_token(force=False):
        forces.append(force)
        return "FRESH" if force else "CACHED"   # 무효화된 캐시=CACHED, 새 토큰=FRESH

    monkeypatch.setattr(b, "_token", fake_token, raising=False)
    monkeypatch.setattr(lb._GLOBAL_THROTTLE, "acquire", lambda: None, raising=False)
    return b, forces


def test_post_reauths_on_500_for_reads(monkeypatch):
    """read 500(무효화 토큰) → 새 토큰 1회 재인증 → 200 자가복구."""
    b, forces = _make(monkeypatch)
    posts = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(headers["authorization"])
        if "CACHED" in headers["authorization"]:
            return _Resp(500)
        return _Resp(200, {"rsp_cd": "00000", "ok": True})

    monkeypatch.setattr(lb.requests, "post", fake_post, raising=False)
    out = b._post("/stock/accno", "t0424", {"x": 1})
    assert out == {"rsp_cd": "00000", "ok": True}
    assert forces == [False, True]            # 캐시 먼저 → 1회 강제 재인증
    assert "Bearer FRESH" in posts[-1]        # 마지막 호출은 새 토큰


def test_post_no_reauth_no_retry_on_500_for_orders(monkeypatch):
    """order 500 → 재인증·재시도 안 함(이중 발주 차단). 단 1회 호출 후 raise."""
    b, forces = _make(monkeypatch)
    posts = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(headers["authorization"])
        return _Resp(500)

    monkeypatch.setattr(lb.requests, "post", fake_post, raising=False)
    with pytest.raises(Exception):
        b._post("/stock/order", "CSPAT00601", {"x": 1}, is_order=True)
    assert len(posts) == 1                    # 재시도 없음(이중 발주 차단)
    assert forces == [False]                  # 강제 재인증 없음


def test_post_reauth_once_then_real_500_raises(monkeypatch):
    """재인증해도 계속 500(진짜 서버에러)이면 재시도 소진 후 raise — 무한 재인증 방지."""
    b, forces = _make(monkeypatch)
    posts = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(headers["authorization"])
        return _Resp(500)                     # 새 토큰으로도 계속 500

    monkeypatch.setattr(lb.requests, "post", fake_post, raising=False)
    monkeypatch.setattr(lb.time, "sleep", lambda *a: None, raising=False)
    with pytest.raises(Exception):
        b._post("/stock/accno", "t0424", {"x": 1})
    assert forces.count(True) == 1            # 재인증은 정확히 1회(무한 루프 아님)


def test_post_no_reauth_on_ratelimit_500(monkeypatch):
    """호출한도 500(IGW00201)은 재인증 안 함(토큰 발급이 한도 악화) — backoff 재시도만."""
    b, forces = _make(monkeypatch)
    monkeypatch.setattr(lb.time, "sleep", lambda *a: None, raising=False)
    posts = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(headers["authorization"])
        return _Resp(500, text='{"rsp_cd":"IGW00201","rsp_msg":"호출 거래건수를 초과하였습니다."}')

    monkeypatch.setattr(lb.requests, "post", fake_post, raising=False)
    with pytest.raises(Exception):
        b._post("/overseas-stock/accno", "COSOQ00201", {"x": 1})
    assert True not in forces                 # 재인증 없음(한도초과)
    assert len(posts) >= 2                     # backoff 재시도는 함


def test_post_field_error_raises_immediately(monkeypatch):
    """필드오류 500(IGW40014)은 즉시 실패 — 재시도·재인증 무의미(요청이 잘못됨)."""
    b, forces = _make(monkeypatch)
    posts = []

    def fake_post(url, headers=None, json=None, timeout=None):
        posts.append(1)
        return _Resp(500, text='{"rsp_cd":"IGW40014","rsp_msg":"... Character U is neither a decimal ..."}')

    monkeypatch.setattr(lb.requests, "post", fake_post, raising=False)
    with pytest.raises(Exception):
        b._post("/overseas-stock/accno", "COSOQ00201", {"x": 1})
    assert len(posts) == 1                     # 단 1회(즉시 실패, 재시도 없음)
    assert True not in forces                  # 재인증 없음

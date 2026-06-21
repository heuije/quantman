"""ls_health.test_credentials 회귀 — 토큰 발급 성공/실패 판정.

LS 토큰 엔드포인트 응답을 mock해 {ok,msg} 계약을 잠근다(실제 네트워크 없음).
연결 테스트는 토큰 발급만 검증한다(잔고/주문 TR은 Phase C 실측 전까지 초안).
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_valid_credentials_ok(monkeypatch):
    from localapp import ls_health
    monkeypatch.setattr(ls_health.requests, "post",
                        lambda *a, **k: _Resp(200, {"access_token": "TOK", "expires_in": 50000}))
    r = ls_health.test_credentials("AK", "SK", virtual=True)
    assert r["ok"] is True
    assert "모의투자" in r["msg"]


def test_invalid_key_http_error_surfaces_rsp_msg(monkeypatch):
    from localapp import ls_health
    monkeypatch.setattr(ls_health.requests, "post",
                        lambda *a, **k: _Resp(401, {"rsp_msg": "유효하지 않은 appkey"}))
    r = ls_health.test_credentials("BAD", "SK", virtual=True)
    assert r["ok"] is False
    assert "유효하지 않은 appkey" in r["msg"]


def test_http_200_without_access_token_is_failure(monkeypatch):
    from localapp import ls_health
    monkeypatch.setattr(ls_health.requests, "post",
                        lambda *a, **k: _Resp(200, {"error_description": "scope 오류"}))
    r = ls_health.test_credentials("AK", "SK", virtual=False)
    assert r["ok"] is False
    assert "scope 오류" in r["msg"]


def test_network_error_is_caught(monkeypatch):
    from localapp import ls_health

    def _boom(*a, **k):
        raise ls_health.requests.RequestException("conn refused")

    monkeypatch.setattr(ls_health.requests, "post", _boom)
    r = ls_health.test_credentials("AK", "SK", virtual=True)
    assert r["ok"] is False
    assert "네트워크" in r["msg"]

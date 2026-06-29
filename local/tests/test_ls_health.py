"""ls_health.test_credentials 회귀 — 토큰 발급 + 계좌 조회(read TR) 판정.

LS 토큰 엔드포인트 응답을 mock해 {ok,msg} 계약을 잠근다(실제 네트워크 없음).
연결 테스트는 (1) 토큰 발급 + (2) read TR(rsp_cd 00000) 성공으로 appkey 계좌컨텍스트가
라이브임을 검증한다(token-only보다 강화). LS는 appkey=계좌단위라 read TR에 입력 계좌번호를
보내지도 echo하지도 않으므로(2026-06-29 모의 캡처) 계좌번호 자체는 read-only로 검증 불가 —
응답에 계좌번호가 *우연히* echo되면(실전 가능) read-back으로 확인만 한다.
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


# ── 토큰 발급 단계 (요청 mock — _issue_token 내부의 requests.post) ────────────────

def test_valid_credentials_ok(monkeypatch):
    """토큰 OK + read TR rsp_cd 00000(echo 없음) → ok=True, 정직한 한계 메시지."""
    from localapp import ls_health
    monkeypatch.setattr(ls_health.requests, "post",
                        lambda *a, **k: _Resp(200, {"access_token": "TOK", "expires_in": 50000}))
    monkeypatch.setattr(ls_health, "_acct_probe",
                        lambda creds, kind: {"rsp_cd": "00000", "t0424OutBlock": {}})
    r = ls_health.test_credentials("AK", "SK", "5544332211", virtual=True, account_kind="stock")
    assert r["ok"] is True
    assert "모의투자" in r["msg"]


def test_invalid_key_http_error_surfaces_rsp_msg(monkeypatch):
    """토큰 발급 HTTP 오류 → ok=False, rsp_msg 표면화 (read TR 미도달)."""
    from localapp import ls_health
    monkeypatch.setattr(ls_health.requests, "post",
                        lambda *a, **k: _Resp(401, {"rsp_msg": "유효하지 않은 appkey"}))
    r = ls_health.test_credentials("BAD", "SK", "5544332211", virtual=True, account_kind="stock")
    assert r["ok"] is False
    assert "유효하지 않은 appkey" in r["msg"]


def test_http_200_without_access_token_is_failure(monkeypatch):
    """토큰 엔드포인트 200이지만 access_token 없음 → ok=False (read TR 미도달)."""
    from localapp import ls_health
    monkeypatch.setattr(ls_health.requests, "post",
                        lambda *a, **k: _Resp(200, {"error_description": "scope 오류"}))
    r = ls_health.test_credentials("AK", "SK", "5544332211", virtual=False, account_kind="stock")
    assert r["ok"] is False
    assert "scope 오류" in r["msg"]


def test_network_error_is_caught(monkeypatch):
    """토큰 발급 네트워크 오류 → ok=False, '네트워크' 메시지."""
    from localapp import ls_health

    def _boom(*a, **k):
        raise ls_health.requests.RequestException("conn refused")

    monkeypatch.setattr(ls_health.requests, "post", _boom)
    r = ls_health.test_credentials("AK", "SK", "5544332211", virtual=True, account_kind="stock")
    assert r["ok"] is False
    assert "네트워크" in r["msg"]


# ── read TR(계좌 조회) 단계 — _issue_token + _acct_probe 둘 다 patch ──────────────

def test_token_fail_returns_not_ok():
    """_issue_token이 빈 토큰 반환(발급 실패) → read TR 미시도, ok=False."""
    from unittest.mock import patch
    from localapp import ls_health
    with patch("localapp.ls_health._issue_token", return_value=""):
        out = ls_health.test_credentials("k", "s", "5544332211", virtual=True, account_kind="stock")
    assert out["ok"] is False


def test_probe_rsp_00000_no_echo_returns_ok():
    """토큰 OK + read TR rsp_cd 00000(계좌 echo 없음) → ok=True, appkey 한계 정직 표면화."""
    from unittest.mock import patch
    from localapp import ls_health
    with patch("localapp.ls_health._issue_token", return_value="T"), \
         patch("localapp.ls_health._acct_probe",
               return_value={"rsp_cd": "00000", "t0424OutBlock": {}}):
        out = ls_health.test_credentials("k", "s", "5544332211", virtual=True, account_kind="stock")
    assert out["ok"] is True
    assert "appkey" in out["msg"]


def test_probe_rsp_00000_with_account_echo_returns_ok_confirmed():
    """토큰 OK + rsp_cd 00000 + 응답에 입력 계좌번호 echo → ok=True, '확인' 메시지(read-back)."""
    from unittest.mock import patch
    from localapp import ls_health
    with patch("localapp.ls_health._issue_token", return_value="T"), \
         patch("localapp.ls_health._acct_probe",
               return_value={"rsp_cd": "00000",
                             "CFOAQ50600OutBlock1": {"AcntNo": "5544332211", "BalAmt": "0"}}):
        out = ls_health.test_credentials("k", "s", "5544-33-2211", virtual=False, account_kind="futures")
    assert out["ok"] is True
    assert "확인" in out["msg"]


def test_probe_nonzero_rsp_returns_not_ok():
    """토큰 OK + read TR rsp_cd != 00000 → ok=False (메시지에 rsp_cd 노출)."""
    from unittest.mock import patch
    from localapp import ls_health
    with patch("localapp.ls_health._issue_token", return_value="T"), \
         patch("localapp.ls_health._acct_probe",
               return_value={"rsp_cd": "IGW00121", "rsp_msg": "권한 없음"}):
        out = ls_health.test_credentials("k", "s", "5544332211", virtual=True, account_kind="stock")
    assert out["ok"] is False
    assert "IGW00121" in out["msg"]


def test_probe_raises_returns_not_ok():
    """토큰 OK + read TR 예외(네트워크/5xx) → ok=False."""
    from unittest.mock import patch
    from localapp import ls_health
    with patch("localapp.ls_health._issue_token", return_value="T"), \
         patch("localapp.ls_health._acct_probe", side_effect=RuntimeError("conn")):
        out = ls_health.test_credentials("k", "s", "9999999999", virtual=True, account_kind="futures")
    assert out["ok"] is False
    assert "conn" in out["msg"]


def test_overseas_futures_is_token_only(monkeypatch):
    """비국내 kind(해외선물 등) → 토큰만 검증, read TR 미시도, ok=True + '후속' 안내."""
    from unittest.mock import patch
    from localapp import ls_health
    called = {"probe": False}

    def _probe(*a, **k):
        called["probe"] = True
        return {"rsp_cd": "00000"}

    with patch("localapp.ls_health._issue_token", return_value="T"), \
         patch("localapp.ls_health._acct_probe", _probe):
        out = ls_health.test_credentials("k", "s", "1111111111", virtual=False,
                                         account_kind="overseas_futures")
    assert out["ok"] is True
    assert "후속" in out["msg"]
    assert called["probe"] is False   # read TR(_acct_probe) 미호출 — 해외선물은 별도 컨텍스트

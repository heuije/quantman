"""kis_futures_health.test_credentials — 저장 전 KIS 선물 계좌 검증 (모의 결정적).

kis_health(주식)의 대칭. 토큰 발급 + 선물 잔고조회(VTFO6118R/CTFO6118R)로 계좌번호까지
검증한다. requests.post/get를 mock해 {ok,msg,...} 계약을 잠근다(실제 네트워크 없음).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import kis_futures_health


def _resp(status, body):
    m = MagicMock()
    m.status_code = status
    m.json.return_value = body
    return m


def test_token_fail_returns_not_ok():
    with patch("localapp.kis_futures_health.requests.post",
               return_value=_resp(403, {"error_description": "invalid appkey"})):
        out = kis_futures_health.test_credentials("k", "s", "12345678-03", virtual=True)
    assert out["ok"] is False
    assert "appkey" in out["msg"].lower() or "토큰" in out["msg"]


def test_balance_ok_returns_ok():
    token = _resp(200, {"access_token": "T"})
    # output2 = 계좌요약(증거금 등). rt_cd=="0" 성공.
    bal = _resp(200, {"rt_cd": "0", "output2": [{"dnca_tot_amt": "1000000"}]})
    with patch("localapp.kis_futures_health.requests.post", return_value=token), \
         patch("localapp.kis_futures_health.requests.get", return_value=bal):
        out = kis_futures_health.test_credentials("k", "s", "12345678-03", virtual=True)
    assert out["ok"] is True


def test_balance_reject_returns_not_ok():
    token = _resp(200, {"access_token": "T"})
    bad = _resp(200, {"rt_cd": "1", "msg_cd": "40570000", "msg1": "계좌번호 오류"})
    with patch("localapp.kis_futures_health.requests.post", return_value=token), \
         patch("localapp.kis_futures_health.requests.get", return_value=bad):
        out = kis_futures_health.test_credentials("k", "s", "99999999-03", virtual=True)
    assert out["ok"] is False
    assert "계좌" in out["msg"] or "40570000" in (out.get("msg_cd") or "")

"""KisFuturesBroker 주문/취소 응답이 Broker 프로토콜 정규형을 반환하는지 회귀 (근본수정).

근본 버그: 선물 브로커의 주문/취소 메서드가 raw KIS json(rt_cd·output.ODNO)을 그대로
반환했다. 주식 KisBroker·SimBroker는 {success, order_no, message, msg_cd}로 정규화하고,
Trader._after_submit은 r["success"]/r["order_no"]를 읽는다(trader.py:863-864). 따라서 선물
브로커가 raw를 반환하면 모든 선물 주문이 success=falsy로 '거부' 처리되고 order_no 추적이
불가해 KIS(주문 접수)와 ledger(거부로 인식)가 발산한다. E2E가 SimBroker(정규형 준수) 위에서만
돌아 이 비정합이 노출되지 않았다 — 부류 버그라 전 주문/취소 메서드를 전수 검증한다.

    cd platform/local && python -m pytest tests/test_futures_broker_resp.py -q
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent   # tests → local
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

import pytest

from localapp import kis_futures_broker as kfb
from localapp.kis_futures_broker import KisFuturesBroker, normalize_order_resp


# ── 순수 정규화 함수 ─────────────────────────────────────────────────────────────
def test_normalize_success_extracts_odno():
    r = normalize_order_resp(
        {"rt_cd": "0", "msg1": "정상처리", "msg_cd": "40610000",
         "output": {"ODNO": "0000123456"}})
    assert r == {"success": True, "message": "정상처리",
                 "msg_cd": "40610000", "order_no": "0000123456"}


def test_normalize_reject_is_not_success():
    r = normalize_order_resp({"rt_cd": "1", "msg1": "잔고부족", "output": {}})
    assert r["success"] is False
    assert r["order_no"] == ""
    assert r["message"] == "잔고부족"


def test_normalize_output_as_list():
    # 일부 TR은 output을 단일원소 배열로 반환
    r = normalize_order_resp({"rt_cd": "0", "output": [{"ODNO": "0009"}]})
    assert r["order_no"] == "0009"


def test_normalize_missing_output():
    r = normalize_order_resp({"rt_cd": "0"})
    assert r["order_no"] == ""
    assert r["success"] is True


def test_normalize_never_leaks_raw_keys():
    r = normalize_order_resp({"rt_cd": "0", "output": {"ODNO": "1"}})
    assert "rt_cd" not in r and "output" not in r


# ── 전 주문/취소 메서드가 정규형을 반환하는지 (부류 닫기 — 전수) ──────────────────
class _Resp:
    def __init__(self, payload):
        self.content = json.dumps(payload).encode("utf-8")

    def raise_for_status(self):
        pass


def _broker_with_fakes(monkeypatch, payload):
    """creds·네트워크 없이 국내+해외 컨텍스트가 채워진 브로커 — requests.post를 가짜로 대체."""
    b = object.__new__(KisFuturesBroker)
    # 국내 컨텍스트
    b.key, b.secret, b.base = "K", "S", "https://x"
    b.cano, b.acnt_prdt_cd, b.virtual = "12345678", "03", True
    b._tok, b._tok_exp = "TOK", time.time() + 100000
    # 해외 컨텍스트
    b._ov_key, b._ov_secret, b._ov_base = "OK", "OS", "https://y"
    b._ov_cano, b._ov_acnt_prdt_cd = "47272165", "08"
    b._ov_tok, b._ov_tok_exp = "OTOK", time.time() + 100000
    monkeypatch.setattr(kfb.requests, "post", lambda *a, **k: _Resp(payload))
    return b


_OK = {"rt_cd": "0", "msg1": "정상", "msg_cd": "X", "output": {"ODNO": "0000777"}}

# (메서드명, 인자) — 주문/취소를 발신하는 모든 메서드 전수
_ORDER_METHODS = [
    ("buy_limit", ("A01606", 1, 350.0)),
    ("sell_limit", ("A01606", 1, 350.0)),
    ("buy", ("A01606", 1)),
    ("sell", ("A01606", 1)),
    ("cancel", ("0000777", "A01606", 1)),
    ("overseas_buy_limit", ("GCM26", 1, 2400.0)),
    ("overseas_sell_limit", ("GCM26", 1, 2400.0)),
    ("overseas_buy", ("GCM26", 1)),
    ("overseas_sell", ("GCM26", 1)),
    ("overseas_cancel", ("0000777", 1, "20260609")),
]


@pytest.mark.parametrize("method,args", _ORDER_METHODS)
def test_all_order_methods_return_normalized(monkeypatch, method, args):
    b = _broker_with_fakes(monkeypatch, _OK)
    r = getattr(b, method)(*args)
    assert isinstance(r, dict), f"{method} 반환이 dict 아님"
    assert r["success"] is True, f"{method} success 누락/falsy"
    assert r["order_no"] == "0000777", f"{method} order_no 미정규화"
    assert "rt_cd" not in r, f"{method} raw KIS 응답 누출(정규화 안 됨)"


# ── 읽기 GET 재시도 (KIS 게이트웨이 간헐 5xx — idempotent read만, 주문 POST 제외) ──
class _GetResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self.reason = "Server Error"
        self.content = json.dumps(payload or {}).encode("utf-8")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise kfb.requests.HTTPError(f"{self.status_code}", response=self)


def test_read_get_retries_transient_5xx_then_succeeds(monkeypatch):
    """간헐 500 두 번 후 200 → 재시도로 성공(실측 KIS 게이트웨이 거동 2026-06-09)."""
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)   # 테스트 가속
    seq = iter([_GetResp(500), _GetResp(500), _GetResp(200, {"output1": {"futs_prpr": "1178.25"}})])
    monkeypatch.setattr(kfb.requests, "get", lambda *a, **k: next(seq))
    b = object.__new__(KisFuturesBroker)
    data = b._read_get("https://x/p", {}, {})
    assert data["output1"]["futs_prpr"] == "1178.25"


def test_read_get_raises_after_persistent_5xx(monkeypatch):
    """지속 500이면 3회 시도 후 HTTPError 전파(무한 재시도/조용한 무시 금지)."""
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    monkeypatch.setattr(kfb.requests, "get", lambda *a, **k: _GetResp(500))
    b = object.__new__(KisFuturesBroker)
    with pytest.raises(kfb.requests.HTTPError):
        b._read_get("https://x/p", {}, {})

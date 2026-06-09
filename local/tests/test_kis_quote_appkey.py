"""이중 KIS 앱키 — 모의=주문 앱키, 시세=실전 앱키 라우팅 + 필수 검증.

KIS는 시세를 실전 도메인 전용으로 제공하고 모의 앱키를 거부(EGW02004)한다. 모의(virtual)
사용 시 시세 조회에 별도 실전 앱키가 필수이고, 시세 호출만 그 키로 라우팅돼야 한다.
네트워크 없음 — requests.get/token을 stub.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LOCAL_DIR = Path(__file__).resolve().parent.parent
if str(_LOCAL_DIR) not in sys.path:
    sys.path.insert(0, str(_LOCAL_DIR))

from localapp import kis_broker as kb
from localapp.kis_broker import KisBroker, _REAL, _VTS


def _broker(virtual: bool = True):
    """__init__ 우회 — 라우팅에 필요한 속성만 주입 + 토큰 stub(네트워크 차단)."""
    b = KisBroker.__new__(KisBroker)
    b.virtual = virtual
    b.key, b.secret = "MOCKKEY", "MOCKSEC"
    b.base = _VTS if virtual else _REAL
    b.quote_base = _REAL
    b.quote_key = "REALKEY" if virtual else "MOCKKEY"
    b.quote_secret = "REALSEC" if virtual else "MOCKSEC"
    b._token_fp, b._quote_fp = "tfp", "qfp"
    b._token = lambda: "MOCK_TOKEN"          # type: ignore
    b._quote_token = lambda: "REAL_TOKEN"    # type: ignore
    return b


def test_headers_use_separate_appkeys():
    b = _broker(virtual=True)
    assert b._headers("X")["appkey"] == "MOCKKEY"
    assert b._headers("X")["authorization"] == "Bearer MOCK_TOKEN"
    assert b._quote_headers("X")["appkey"] == "REALKEY"     # 시세=실전 앱키
    assert b._quote_headers("X")["authorization"] == "Bearer REAL_TOKEN"


def test_get_retry_routes_quote_to_real_appkey(monkeypatch):
    """시세 도메인(실전) 호출은 실전 앱키, 주문 도메인(모의) 호출은 모의 앱키 헤더."""
    b = _broker(virtual=True)
    seen = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"rt_cd": "0", "output": {"last": "1"}}

    def fake_get(url, headers=None, **kw):
        seen["appkey"] = headers["appkey"]
        return FakeResp()

    monkeypatch.setattr(kb.requests, "get", fake_get)
    monkeypatch.setattr(kb._GLOBAL_THROTTLE, "acquire", lambda: None)
    monkeypatch.setattr(kb, "_kis_check", lambda r: r.json())

    b._get_retry("/price", "HHDFS00000300", {}, base=b.quote_base)
    assert seen["appkey"] == "REALKEY", seen                # 시세 → 실전 앱키

    b._get_retry("/order-inq", "TR", {}, base=b.base)
    assert seen["appkey"] == "MOCKKEY", seen                # 주문/잔고 → 모의 앱키


def test_init_requires_quote_appkey_when_virtual(monkeypatch):
    """모의인데 시세용 실전 앱키 없으면 명확히 차단(필수화)."""
    monkeypatch.setattr(kb, "load_kis", lambda: {
        "app_key": "MOCK", "app_secret": "S", "account_no": "123-01", "virtual": True})
    with pytest.raises(RuntimeError, match="시세"):
        KisBroker()


def test_init_virtual_with_quote_appkey_ok(monkeypatch):
    monkeypatch.setattr(kb, "load_kis", lambda: {
        "app_key": "MOCK", "app_secret": "S", "account_no": "123-01", "virtual": True,
        "quote_app_key": "REAL", "quote_app_secret": "RS"})
    b = KisBroker()
    assert b.key == "MOCK" and b.quote_key == "REAL"        # 주문=모의, 시세=실전


def test_init_real_reuses_appkey_for_quote(monkeypatch):
    """실전이면 주문 앱키가 곧 실전 → 시세도 동일 키 재사용(별도 불필요)."""
    monkeypatch.setattr(kb, "load_kis", lambda: {
        "app_key": "REALK", "app_secret": "RS", "account_no": "123-01", "virtual": False})
    b = KisBroker()
    assert b.quote_key == "REALK" and b.quote_secret == "RS"

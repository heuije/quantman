"""active_broker SSOT + make_broker 분기 회귀 — 기본 KIS 무변경 보장."""
from __future__ import annotations
import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

import pytest


def test_active_broker_defaults_to_kis(monkeypatch):
    from localapp import secrets_store
    monkeypatch.setattr(secrets_store.keyring, "get_password", lambda *a, **k: None)
    assert secrets_store.get_active_broker() == "kis"   # 미설정 → kis (기존 사용자 무변경)


def test_set_get_active_broker_roundtrip(monkeypatch):
    from localapp import secrets_store
    store = {}
    monkeypatch.setattr(secrets_store.keyring, "set_password",
                        lambda svc, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(secrets_store.keyring, "get_password",
                        lambda svc, k: store.get(k))
    secrets_store.set_active_broker("ls")
    assert secrets_store.get_active_broker() == "ls"


def test_save_load_ls_credentials(monkeypatch):
    from localapp import secrets_store
    store = {}
    monkeypatch.setattr(secrets_store.keyring, "set_password",
                        lambda svc, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(secrets_store.keyring, "get_password",
                        lambda svc, k: store.get(k))
    secrets_store.save_ls("AK", "SK", "5550-1234", virtual=True)
    creds = secrets_store.load_ls()
    assert creds["app_key"] == "AK" and creds["account_no"] == "5550-1234"
    assert creds["app_secret"] == "SK"   # 보안 핵심 필드도 라운드트립 보장
    assert creds["virtual"] is True


def test_make_broker_routes_to_ls(monkeypatch):
    """active_broker=ls면 LsBroker, 기본이면 KisBroker 경로."""
    from localapp import runner, secrets_store
    monkeypatch.setattr(secrets_store, "get_active_broker", lambda: "ls")
    monkeypatch.setattr(secrets_store, "load_ls", lambda: {"app_key": "x"})
    # make_broker가 매 호출 `from .ls_broker import LsBroker`를 재실행하므로, 같은
    # sys.modules 객체(lb)의 LsBroker 속성을 patch하면 그 값을 집어온다(A5 해제 시 그대로 통과).
    import localapp.ls_broker as lb
    monkeypatch.setattr(lb, "LsBroker", lambda: "LS_INSTANCE")
    monkeypatch.setattr(runner, "load_kis", lambda: None)
    assert runner.make_broker() == "LS_INSTANCE"

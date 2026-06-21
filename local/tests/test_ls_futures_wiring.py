"""LS 선물 자격증명 슬롯 + make_broker 라우터 분기 회귀."""
from __future__ import annotations
import sys
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

import pytest


def test_save_load_ls_futures(monkeypatch):
    from localapp import secrets_store
    store = {}
    monkeypatch.setattr(secrets_store.keyring, "set_password",
                        lambda svc, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(secrets_store.keyring, "get_password",
                        lambda svc, k: store.get(k))
    secrets_store.save_ls_futures("AK", "SK", "5550-9999", virtual=True)
    c = secrets_store.load_ls_futures()
    assert c["app_key"] == "AK" and c["account_no"] == "5550-9999" and c["virtual"] is True


def test_make_broker_ls_returns_router_when_futures(monkeypatch):
    """LS 활성 + 선물 자격증명 → BrokerRouter(LsBroker, LsFuturesBroker)."""
    from localapp import runner, secrets_store
    monkeypatch.setattr(secrets_store, "get_active_broker", lambda: "ls")
    monkeypatch.setattr(secrets_store, "load_ls", lambda: {"app_key": "x"})
    monkeypatch.setattr(secrets_store, "load_ls_futures", lambda: {"app_key": "f"})
    import localapp.ls_broker as lb
    import localapp.ls_futures_broker as lfb
    monkeypatch.setattr(lb, "LsBroker", lambda: "LS_STOCK")
    monkeypatch.setattr(lfb, "LsFuturesBroker", lambda: "LS_FUT")
    b = runner.make_broker()
    from localapp.broker_router import BrokerRouter
    assert isinstance(b, BrokerRouter)


def test_make_broker_ls_stock_only_when_no_futures(monkeypatch):
    """LS 활성 + 선물 자격증명 없음 → LsBroker 단독(국내주식, 무변경)."""
    from localapp import runner, secrets_store
    monkeypatch.setattr(secrets_store, "get_active_broker", lambda: "ls")
    monkeypatch.setattr(secrets_store, "load_ls", lambda: {"app_key": "x"})
    monkeypatch.setattr(secrets_store, "load_ls_futures", lambda: None)
    import localapp.ls_broker as lb
    monkeypatch.setattr(lb, "LsBroker", lambda: "LS_STOCK")
    assert runner.make_broker() == "LS_STOCK"

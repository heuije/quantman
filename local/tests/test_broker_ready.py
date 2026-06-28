"""broker_ready / active_cred_ok 일반화 — 자산군 슬롯 ≥1이면 ready (선물 단독 포함)."""
import pytest

from localapp import secrets_store


@pytest.fixture
def stub(monkeypatch):
    state = {"broker": "kis", "kis": None, "kis_fut": None, "kis_ovf": None,
             "ls": None, "ls_fut": None, "ls_ovf": None}
    monkeypatch.setattr(secrets_store, "get_active_broker", lambda: state["broker"])
    monkeypatch.setattr(secrets_store, "load_kis", lambda: state["kis"])
    monkeypatch.setattr(secrets_store, "load_kis_futures", lambda: state["kis_fut"])
    monkeypatch.setattr(secrets_store, "load_kis_overseas_futures", lambda: state["kis_ovf"])
    monkeypatch.setattr(secrets_store, "load_ls", lambda: state["ls"])
    monkeypatch.setattr(secrets_store, "load_ls_futures", lambda: state["ls_fut"])
    monkeypatch.setattr(secrets_store, "load_ls_overseas_futures", lambda: state["ls_ovf"])
    return state


def test_kis_stock_ready(stub):
    stub["kis"] = {"app_key": "x"}
    assert secrets_store.broker_ready("kis") is True
    assert secrets_store.active_cred_ok() is True


def test_kis_futures_only_ready(stub):
    """주식 없이 KIS 국내선물만 — ready (P3 핵심)."""
    stub["kis_fut"] = {"app_key": "y"}
    assert secrets_store.broker_ready("kis") is True


def test_kis_empty_not_ready(stub):
    assert secrets_store.broker_ready("kis") is False
    assert secrets_store.active_cred_ok() is False


def test_ls_futures_only_ready(stub):
    """주식 없이 LS 국내선물만 — ready (사장님 원증상의 핵심)."""
    stub["broker"] = "ls"
    stub["ls_fut"] = {"app_key": "y"}
    assert secrets_store.broker_ready("ls") is True
    assert secrets_store.active_cred_ok() is True


def test_ls_overseas_futures_only_ready(stub):
    stub["broker"] = "ls"
    stub["ls_ovf"] = {"app_key": "z"}
    assert secrets_store.broker_ready("ls") is True


def test_ls_empty_not_ready(stub):
    stub["broker"] = "ls"
    assert secrets_store.broker_ready("ls") is False


def test_active_cred_ok_uses_active_broker(stub):
    """active_cred_ok = broker_ready(get_active_broker())."""
    stub["broker"] = "ls"
    stub["ls_fut"] = {"app_key": "y"}    # LS 선물만
    stub["kis"] = None
    assert secrets_store.active_cred_ok() is True   # 활성=ls라 LS 슬롯 기준

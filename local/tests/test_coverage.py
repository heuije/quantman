"""coverage 모듈 단위 테스트 — 자격증명 슬롯 → 커버 자산군 매핑 (keyring 불요)."""
import pytest

from localapp import coverage, secrets_store


@pytest.fixture
def stub_slots(monkeypatch):
    """secrets_store 로더를 메모리 state로 스텁. 각 테스트가 state 키를 채워 슬롯 시뮬레이트."""
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


def test_kis_stock_covers_both_equity(stub_slots):
    stub_slots["kis"] = {"app_key": "x"}
    assert coverage.covered_categories() == {"kr_equity", "us_equity"}


def test_kis_stock_plus_kr_futures(stub_slots):
    stub_slots["kis"] = {"app_key": "x"}
    stub_slots["kis_fut"] = {"app_key": "y"}
    assert coverage.covered_categories() == {"kr_equity", "us_equity", "kr_futures"}


def test_kis_kr_futures_only(stub_slots):
    stub_slots["kis_fut"] = {"app_key": "y"}
    assert coverage.covered_categories() == {"kr_futures"}


def test_kis_overseas_futures_only(stub_slots):
    stub_slots["kis_ovf"] = {"app_key": "z"}
    assert coverage.covered_categories() == {"us_futures"}


def test_ls_stock_is_kr_equity_only(stub_slots):
    stub_slots["broker"] = "ls"
    stub_slots["ls"] = {"app_key": "x"}
    assert coverage.covered_categories() == {"kr_equity"}


def test_ls_kr_futures_only(stub_slots):
    stub_slots["broker"] = "ls"
    stub_slots["ls_fut"] = {"app_key": "y"}
    assert coverage.covered_categories() == {"kr_futures"}


def test_no_credentials_covers_nothing(stub_slots):
    assert coverage.covered_categories() == set()


def test_missing_detects_uncovered_futures(stub_slots):
    stub_slots["kis"] = {"app_key": "x"}   # 주식만
    assert coverage.missing_categories(["005930", "코스피200선물"]) == {"kr_futures"}


def test_missing_empty_when_all_covered(stub_slots):
    stub_slots["kis"] = {"app_key": "x"}
    stub_slots["kis_fut"] = {"app_key": "y"}
    assert coverage.missing_categories(["005930", "코스피200선물"]) == set()


def test_missing_ignores_empty_symbols(stub_slots):
    stub_slots["kis"] = {"app_key": "x"}
    assert coverage.missing_categories(["", "005930"]) == set()

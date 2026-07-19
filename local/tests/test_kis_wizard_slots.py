"""KIS wizard 계좌 종류 슬롯 — Step 3 저장·검증 분기 (gui.SettingsApp, Tk 없이 fake self).

실사용 사고(선물 계좌를 주식 슬롯에 입력·2026-07-19) 재발 방지 배선: 계좌 종류 선택에
따라 save_kis / save_kis_futures / save_kis_overseas_futures 슬롯과 검증 TR이 갈린다.
저장 경로는 *진짜* secrets_store(인메모리 keyring)를 통과시켜 save→load 왕복까지 잠근다.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp import gui, secrets_store


class _MemKeyring:
    """keyring 대역 — Windows 자격증명 저장소 대신 인메모리 dict."""

    def __init__(self):
        self.store = {}

    def set_password(self, svc, name, val):
        self.store[(svc, name)] = val

    def get_password(self, svc, name):
        return self.store.get((svc, name))

    def delete_password(self, svc, name):
        self.store.pop((svc, name), None)


class _E:
    """ttk.Entry 대역 — get/delete/insert만."""

    def __init__(self, v=""):
        self.v = v

    def get(self):
        return self.v

    def delete(self, *_):
        self.v = ""

    def insert(self, _i, s):
        self.v += str(s)


class _Var:
    """tk.StringVar 대역."""

    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v

    def set(self, v):
        self.v = v


class _W:
    """configure 호출만 기록하는 위젯 대역 (버튼·라벨)."""

    def __init__(self):
        self.conf = {}

    def configure(self, **kw):
        self.conf.update(kw)


@pytest.fixture(autouse=True)
def mem_keyring(monkeypatch):
    kr = _MemKeyring()
    monkeypatch.setattr(secrets_store, "keyring", kr)
    return kr


@pytest.fixture
def mb(monkeypatch):
    """messagebox 대역 — 호출 기록 + askyesno 응답 제어."""
    calls = {"showinfo": [], "showerror": [], "askyesno": [], "answer": True}
    monkeypatch.setattr(gui, "messagebox", types.SimpleNamespace(
        showinfo=lambda *a, **k: calls["showinfo"].append(a),
        showerror=lambda *a, **k: calls["showerror"].append(a),
        askyesno=lambda *a, **k: (calls["askyesno"].append(a), calls["answer"])[1],
    ))
    return calls


def _fake_app(acct_type, virtual=True, key="AK", secret="SC", acct="12345678-03"):
    fs = types.SimpleNamespace(
        wizard_test_ok=True,
        wizard_virtual=virtual,
        kis_acct_type=_Var(acct_type),
        e_key=_E(key), e_secret=_E(secret), e_acct=_E(acct),
        e_hts=_E(""), e_quote_key=_E(""), e_quote_secret=_E(""),
        setup_collapsed=False,
        refresh_status=lambda: None,
        _push_state_async=lambda: None,
    )
    fs._wizard_selected_cred = (
        lambda: gui.SettingsApp._wizard_selected_cred(fs))
    return fs


# ── _wizard_save 슬롯 분기 ─────────────────────────────────────────────────────

def test_save_futures_slot_roundtrip(mb):
    """국내선물 선택 저장 → load_kis_futures 로드·주식 슬롯 무오염 (사고의 역방향 차단)."""
    fs = _fake_app(gui._ACCT_FUTURES, virtual=True)
    gui.SettingsApp._wizard_save(fs)
    cred = secrets_store.load_kis_futures()
    assert cred and cred["app_key"] == "AK" and cred["app_secret"] == "SC"
    assert cred["account_no"] == "12345678-03" and cred["virtual"] is True
    assert secrets_store.load_kis() is None            # 주식 슬롯은 그대로 비어있음
    assert secrets_store.load_kis_overseas_futures() is None
    assert fs.e_secret.get() == ""                     # secret은 저장 후 지움
    assert fs.setup_collapsed is True
    assert len(mb["showinfo"]) == 1


def test_save_futures_real_mode(mb):
    fs = _fake_app(gui._ACCT_FUTURES, virtual=False)
    gui.SettingsApp._wizard_save(fs)
    assert secrets_store.load_kis_futures()["virtual"] is False


def test_save_overseas_forced_real_with_confirm(mb):
    """해외선물은 모의 미지원 — Step 2가 모의여도 확인 후 virtual=False로 저장."""
    fs = _fake_app(gui._ACCT_OV_FUTURES, virtual=True, acct="12345678-08")
    gui.SettingsApp._wizard_save(fs)
    assert len(mb["askyesno"]) == 1                    # 실전 저장 명시 확인
    cred = secrets_store.load_kis_overseas_futures()
    assert cred and cred["virtual"] is False
    assert secrets_store.load_kis_futures() is None


def test_save_overseas_confirm_declined_no_save(mb):
    mb["answer"] = False
    fs = _fake_app(gui._ACCT_OV_FUTURES, virtual=True, acct="12345678-08")
    gui.SettingsApp._wizard_save(fs)
    assert secrets_store.load_kis_overseas_futures() is None
    assert fs.setup_collapsed is False                 # 저장 안 됨 — 화면 유지


def test_save_overseas_real_mode_skips_confirm(mb):
    """Step 2에서 이미 실전 선택(확인 완료) — 중복 확인 dialog 없이 저장."""
    fs = _fake_app(gui._ACCT_OV_FUTURES, virtual=False, acct="12345678-08")
    gui.SettingsApp._wizard_save(fs)
    assert mb["askyesno"] == []
    assert secrets_store.load_kis_overseas_futures()["virtual"] is False


def test_save_stock_slot_regression(mb):
    """주식(기본) 경로 회귀 — save_kis로 저장, 선물 슬롯 무오염."""
    fs = _fake_app(gui._ACCT_STOCK, virtual=False, acct="50001234-01")
    gui.SettingsApp._wizard_save(fs)
    cred = secrets_store.load_kis()
    assert cred and cred["account_no"] == "50001234-01" and cred["virtual"] is False
    assert secrets_store.load_kis_futures() is None


def test_save_stock_virtual_requires_quote_keys(mb):
    """주식 모의투자는 시세용 실전 앱키 필수 — 없으면 저장 차단 (기존 가드 회귀)."""
    fs = _fake_app(gui._ACCT_STOCK, virtual=True, acct="50001234-01")
    gui.SettingsApp._wizard_save(fs)
    assert len(mb["showerror"]) == 1
    assert secrets_store.load_kis() is None


# ── _wizard_test_connection 검증 TR 라우팅 ────────────────────────────────────

def _fake_test_app(acct_type, virtual=True):
    fs = _fake_app(acct_type, virtual=virtual)
    fs.wizard_test_ok = False
    fs._wizard_action_btn = _W()
    fs._wizard_test_status = _W()
    fs._wizard_save = lambda: None                       # done()이 버튼 command로 참조
    fs._run_bg = lambda work, done: done(work(), None)   # 동기 실행
    return fs


def test_test_connection_routes_futures(monkeypatch):
    calls = []
    from localapp import kis_futures_health
    monkeypatch.setattr(kis_futures_health, "test_credentials",
                        lambda *a, **k: (calls.append((a, k)),
                                         {"ok": True, "msg": "m"})[1])
    fs = _fake_test_app(gui._ACCT_FUTURES, virtual=True)
    gui.SettingsApp._wizard_test_connection(fs)
    assert len(calls) == 1
    assert calls[0][1].get("virtual") is True
    assert "account_kind" not in calls[0][1]             # 기본 국내선물
    assert fs.wizard_test_ok is True
    assert "저장" in fs._wizard_action_btn.conf.get("text", "")


def test_test_connection_routes_overseas(monkeypatch):
    calls = []
    from localapp import kis_futures_health
    monkeypatch.setattr(kis_futures_health, "test_credentials",
                        lambda *a, **k: (calls.append((a, k)),
                                         {"ok": True, "msg": "m"})[1])
    fs = _fake_test_app(gui._ACCT_OV_FUTURES, virtual=True)
    gui.SettingsApp._wizard_test_connection(fs)
    assert calls[0][1] == {"virtual": False, "account_kind": "overseas_futures"}


def test_test_connection_routes_stock(monkeypatch):
    calls = []
    monkeypatch.setattr(gui.kis_health, "test_credentials",
                        lambda *a, **k: (calls.append((a, k)),
                                         {"ok": True, "msg": "m"})[1])
    fs = _fake_test_app(gui._ACCT_STOCK, virtual=True)
    gui.SettingsApp._wizard_test_connection(fs)
    assert len(calls) == 1 and calls[0][1].get("virtual") is True


# ── 슬롯 전환 프리필·모드 채택 ─────────────────────────────────────────────────

def test_prefill_clears_then_fills_from_futures_slot():
    """계좌 종류 전환 시 이전 슬롯 잔존값 제거 후 새 슬롯 값 프리필(잘못 저장 방지)."""
    secrets_store.save_kis_futures("FK", "FS", "12345678-03", virtual=False)
    fs = _fake_app(gui._ACCT_FUTURES, key="STALE_STOCK_KEY", secret="S",
                   acct="50001234-01")
    fs.e_hts = _E("stale-hts")
    gui.SettingsApp._wizard_prefill_from_store(fs, clear=True)
    assert fs.e_key.get() == "FK"
    assert fs.e_acct.get() == "12345678-03"
    assert fs.e_secret.get() == ""                     # secret은 프리필 안 함
    assert fs.e_hts.get() == ""                        # 주식 전용 필드는 비움


def test_prefill_empty_slot_clears_stale_fields():
    fs = _fake_app(gui._ACCT_FUTURES, key="STALE", secret="S", acct="50001234-01")
    gui.SettingsApp._wizard_prefill_from_store(fs, clear=True)
    assert fs.e_key.get() == "" and fs.e_acct.get() == ""


def test_adopt_stored_mode_prevents_silent_virtual_flip():
    """실전으로 저장된 선물 슬롯 선택 시 wizard 모드가 실전으로 따라감(조용한 모의 재저장 방지)."""
    secrets_store.save_kis_futures("FK", "FS", "12345678-03", virtual=False)
    fs = _fake_app(gui._ACCT_FUTURES, virtual=True)
    fs._wizard_virtual_var = _Var("virtual")
    fs._refresh_step2_cards = lambda: None
    fs._wizard_on_mode_change = (
        lambda: gui.SettingsApp._wizard_on_mode_change(fs))
    gui.SettingsApp._wizard_adopt_stored_mode(fs)
    assert fs.wizard_virtual is False
    assert fs._wizard_virtual_var.get() == "real"

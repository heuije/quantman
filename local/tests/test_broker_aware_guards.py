"""브로커-aware 게이팅 회귀 — LS 활성 시 웹명령 거부 안 됨 + KIS 동작 불변.

B7 구현 검증:
1. active_cred_ok() — 활성 브로커의 자산군 슬롯 ≥1이면 True (P3 일반화 — 주식·선물·해외선물).
   주식 보유 사용자는 종전과 동일(주식 있으면 True). 선물 단독 검증은 test_broker_ready.py.
2. gui._handle_command 게이트: active_cred_ok() 호출 여부 소스 레벨 검증
3. runner._wait_for_order_ws: LS 활성 시 KIS WS 건드리지 않고 즉시 반환
"""
from __future__ import annotations
import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


# ── 1. active_cred_ok 단위 테스트 ─────────────────────────────────────────────


def test_active_cred_ok_kis_active_uses_kis_slots(monkeypatch):
    """KIS 활성 시 active_cred_ok()는 KIS 자산군 슬롯 기준(P3: 슬롯 ≥1). 주식 보유 시 True,
    모든 KIS 슬롯 부재면 False. (선물 단독 → True 는 test_broker_ready.py에서 검증.)"""
    from localapp import secrets_store as s
    monkeypatch.setattr(s, "get_active_broker", lambda: "kis")
    for n in ("load_kis_futures", "load_kis_overseas_futures",
              "load_ls", "load_ls_futures", "load_ls_overseas_futures"):
        monkeypatch.setattr(s, n, lambda: None)
    monkeypatch.setattr(s, "load_kis", lambda: {"app_key": "x"})
    assert s.active_cred_ok() is True

    monkeypatch.setattr(s, "load_kis", lambda: None)
    assert s.active_cred_ok() is False   # 모든 KIS 슬롯 부재 → False


def test_active_cred_ok_ls_uses_load_ls(monkeypatch):
    """LS 활성 시 KIS 자격증명 없어도 LS 자격증명 있으면 True."""
    from localapp import secrets_store as s
    monkeypatch.setattr(s, "get_active_broker", lambda: "ls")
    monkeypatch.setattr(s, "load_kis", lambda: None)        # KIS 없어도
    monkeypatch.setattr(s, "load_ls", lambda: {"app_key": "x"})
    assert s.active_cred_ok() is True                        # LS면 OK


def test_active_cred_ok_ls_no_ls_cred(monkeypatch):
    """LS 활성 시 LS 슬롯이 하나도 없으면 False (P3: 자산군 슬롯 집합 기준)."""
    from localapp import secrets_store as s
    monkeypatch.setattr(s, "get_active_broker", lambda: "ls")
    for n in ("load_ls", "load_ls_futures", "load_ls_overseas_futures"):
        monkeypatch.setattr(s, n, lambda: None)
    assert s.active_cred_ok() is False


# ── 2. gui 소스 레벨: _handle_command가 active_cred_ok 사용 확인 ───────────────


def test_gui_handle_command_uses_active_cred_ok():
    """gui.py _handle_command에서 load_kis() is None 패턴이 사라지고
    active_cred_ok()가 사용되는지 소스 텍스트 검증."""
    gui_path = _LOCAL / "localapp" / "gui.py"
    source = gui_path.read_text(encoding="utf-8")

    # _handle_command 함수 내부만 잘라서 검사
    start = source.find("def _handle_command(")
    end = source.find("\n    def ", start + 1)
    if end == -1:
        end = len(source)
    handler_src = source[start:end]

    # broker-gate 목적으로 load_kis() is None을 쓰는 패턴이 없어야 함
    assert "load_kis() is None" not in handler_src, (
        "_handle_command에 아직 load_kis() is None 가드가 남아 있습니다. "
        "active_cred_ok()로 교체하세요."
    )
    # active_cred_ok 또는 active_cred_ok()가 있어야 함
    assert "active_cred_ok" in handler_src, (
        "_handle_command에 active_cred_ok() 호출이 없습니다."
    )


def test_gui_cancel_order_uses_make_broker():
    """CANCEL_ORDER가 KisBroker()를 하드와이어하지 않고 make_broker()를 사용하는지 검증."""
    gui_path = _LOCAL / "localapp" / "gui.py"
    source = gui_path.read_text(encoding="utf-8")

    start = source.find("def _handle_command(")
    end = source.find("\n    def ", start + 1)
    if end == -1:
        end = len(source)
    handler_src = source[start:end]

    # CANCEL_ORDER 블록에서 KisBroker() 직접 인스턴스화 금지
    cancel_start = handler_src.find('"CANCEL_ORDER"')
    if cancel_start == -1:
        cancel_start = handler_src.find("'CANCEL_ORDER'")
    assert cancel_start != -1, "CANCEL_ORDER 블록을 찾을 수 없습니다"
    # 다음 if t == 블록까지 잘라서 검사
    next_block = handler_src.find('\n        if t ==', cancel_start + 1)
    cancel_block = handler_src[cancel_start: next_block if next_block != -1 else len(handler_src)]
    assert "KisBroker()" not in cancel_block, (
        "CANCEL_ORDER에 KisBroker() 하드와이어가 남아 있습니다. make_broker()로 교체하세요."
    )


def test_run_once_button_removed():
    """'지금 한 번 실행' 버튼 제거 잠금(2026-07-19 유저 결정 — 자동 따라잡기·재시도
    정비 후 실익 없음, 장중 오클릭 실발주 위험만 잔존). 재도입 시 리뷰 필요."""
    gui_path = _LOCAL / "localapp" / "gui.py"
    source = gui_path.read_text(encoding="utf-8")
    assert "def _run_once(" not in source
    assert 'trigger="manual"' not in source


# ── 3. runner._wait_for_order_ws: LS 활성 시 즉시 반환 ────────────────────────


def test_wait_for_order_ws_skips_for_ls(monkeypatch):
    """LS 활성 시 _wait_for_order_ws는 KIS WS를 건드리지 않고 즉시 반환한다."""
    from localapp import secrets_store as s, runner

    # LS 활성 + LS 자격증명 있음
    monkeypatch.setattr(s, "get_active_broker", lambda: "ls")

    # intraday_loop.status()가 호출되면 테스트 실패
    class FakeIntradayLoop:
        @staticmethod
        def status():
            raise AssertionError("LS 활성 시 intraday_loop.status()는 호출되면 안 됩니다")

    import types
    fake_loop = types.ModuleType("localapp.intraday_loop")
    fake_loop.status = FakeIntradayLoop.status
    monkeypatch.setitem(sys.modules, "localapp.intraday_loop", fake_loop)

    # 예외 없이 반환되어야 함
    runner._wait_for_order_ws()


def test_wait_for_order_ws_kis_no_hts_returns_early(monkeypatch):
    """KIS 활성이지만 hts_id 미설정 시 즉시 반환(기존 동작 불변)."""
    from localapp import secrets_store as s, runner

    monkeypatch.setattr(s, "get_active_broker", lambda: "kis")
    monkeypatch.setattr(s, "load_kis", lambda: {"app_key": "x", "hts_id": ""})

    # intraday_loop 호출 없이 반환
    import types
    fake_loop = types.ModuleType("localapp.intraday_loop")
    fake_loop.status = lambda: (_ for _ in ()).throw(
        AssertionError("hts_id 없으면 intraday_loop 호출 불필요"))
    monkeypatch.setitem(sys.modules, "localapp.intraday_loop", fake_loop)

    runner._wait_for_order_ws()   # 예외 없이 반환해야 함


def test_wait_for_order_ws_kis_with_hts_checks_ws(monkeypatch):
    """KIS 활성 + hts_id 설정 시 intraday_loop.status() 호출(기존 동작 불변).

    _wait_for_order_ws가 get_active_broker()=="kis"이고 hts_id가 있으면
    intraday_loop 경로에 진입함을 소스로 검증 (헤드리스 환경에서 intraday_loop
    자체의 WSS 연결 시도를 피하기 위해 소스-레벨 어설션 사용).
    """
    runner_path = _LOCAL / "localapp" / "runner.py"
    source = runner_path.read_text(encoding="utf-8")

    fn_start = source.find("def _wait_for_order_ws(")
    fn_end = source.find("\ndef ", fn_start + 1)
    fn_src = source[fn_start: fn_end if fn_end != -1 else len(source)]

    # KIS 게이트 이후에 intraday_loop 진입 경로가 있어야 함
    assert "intraday_loop" in fn_src, "_wait_for_order_ws에 intraday_loop 진입이 없습니다"
    # LS 게이트가 맨 앞에 있어야 함 (get_active_broker() != "kis" 이면 return)
    assert 'get_active_broker() != "kis"' in fn_src, (
        "_wait_for_order_ws에 LS 브로커 게이트(get_active_broker()!=\"kis\")가 없습니다"
    )


# ── 활성 브로커 저장≠전환 분리(2026-07-19 사고) ──────────────────────────────


def test_broker_activation_mode_policy():
    """등록만 하러 온 유저 보호 — 타 브로커 사용 가능 상태면 ask, 아니면 silent."""
    from localapp.gui import SettingsApp
    m = SettingsApp.broker_activation_mode
    assert m("ls", "ls", True) == "noop"
    assert m("ls", "kis", True) == "ask"       # LS 병행 → 명시 확인 후에만 전환
    assert m("kis", "ls", True) == "ask"
    assert m("kis", "ls", False) == "silent"   # 단일 브로커 온보딩 → 조용히 선언
    assert m("ls", "kis", False) == "silent"


def test_radio_click_does_not_flip_active_broker():
    """라디오=탐색 전용 — 클릭 즉시 set_active_broker 하던 사고 경로 제거 잠금.
    저장 경로 둘(_ls_save·_wizard_save)은 정책 헬퍼를 경유해야 한다."""
    source = (_LOCAL / "localapp" / "gui.py").read_text(encoding="utf-8")
    start = source.find("def _on_broker_choice_change(")
    body = source[start:source.find("\n    def ", start + 1)]
    assert "secrets_store.set_active_broker" not in body   # 실호출 금지(docstring 언급은 허용)
    for fn in ("def _ls_save(", "def _wizard_save("):
        s2 = source.find(fn)
        assert "_maybe_activate_broker(" in source[s2:source.find("\n    def ", s2 + 1)], fn


def test_runner_binds_secrets_via_module():
    """runner가 get_active_broker/load_kis를 모듈-레벨 from-import로 바인딩하면
    테스트 monkeypatch가 안 뚫려 실 keyring 의존 flaky(07-19 실측) — 모듈 경유 잠금."""
    src = (_LOCAL / "localapp" / "runner.py").read_text(encoding="utf-8")
    assert "from .secrets_store import load_kis, get_active_broker" not in src
    fn_start = src.find("def _wait_for_order_ws(")
    fn_src = src[fn_start: src.find("\ndef ", fn_start + 1)]
    assert "secrets_store.get_active_broker()" in fn_src

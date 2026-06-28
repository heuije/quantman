"""decide_setup_mode 순수함수 — 온보딩 모드 결정(위젯 무의존) 조합 매트릭스 (P3)."""
import pytest

from localapp.onboarding import decide_setup_mode


@pytest.mark.parametrize("broker,ready,dev_ok,collapsed,expected", [
    # 둘 다 완료 + 접힘 → normal
    ("kis", True,  True,  True,  "normal"),
    ("ls",  True,  True,  True,  "normal"),
    # 미등록 → 해당 브로커 wizard
    ("kis", False, False, True,  "wizard_kis"),
    ("ls",  False, False, True,  "wizard_ls"),
    ("kis", False, True,  True,  "wizard_kis"),
    ("ls",  False, True,  True,  "wizard_ls"),
    # 등록·페어링 미완 → wizard_pair
    ("kis", True,  False, True,  "wizard_pair"),
    ("ls",  True,  False, True,  "wizard_pair"),
    # 둘 다 완료지만 ⚙ 펼침 → 자격증명 변경(wizard)
    ("kis", True,  True,  False, "wizard_kis"),
    ("ls",  True,  True,  False, "wizard_ls"),
])
def test_matrix(broker, ready, dev_ok, collapsed, expected):
    assert decide_setup_mode(broker, ready, dev_ok, collapsed) == expected

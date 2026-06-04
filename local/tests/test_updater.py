"""updater.bat 생성 — 원자적 폴더 스왑 회귀 가드.

옛 `robocopy /E /XO` 머지 방식은 stale 파일·skip-older·부분 교체로 PyInstaller
빌드 짝(.exe ↔ python3xx.dll·base_library.zip)을 깨뜨려 'failed to start embedded
python interpreter'를 유발했다(v0.9.18 Python 3.12 ↔ v0.9.19 3.11 혼합 실측 사고).
이 테스트는 그 결함 방식(/XO 머지)이 재도입되지 않고, 폴더 스왑(clean replace +
롤백)이 유지됨을 단언한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

from localapp.updater import _write_updater_bat


def _gen(tmp_path: Path) -> str:
    bat = tmp_path / "updater.bat"
    src = tmp_path / "extracted" / "QuantPlatformLocal-v9.9.9-beta"
    dst = tmp_path / "install" / "QuantPlatformLocal-v9.9.9-beta"
    app_exe = dst / "QuantPlatformLocal.exe"
    _write_updater_bat(bat, src, dst, app_exe)
    return bat.read_bytes().decode("cp949", errors="replace")


def test_updater_bat_is_clean_swap_not_xo_merge(tmp_path):
    """결함 방식(/XO 머지) 재도입 차단 + 폴더 스왑(clean replace) 유지."""
    s = _gen(tmp_path)
    # /XO(skip-older) 절대 금지 — 빌드 짝 깨짐의 직접 원인.
    assert "/XO" not in s, "robocopy /XO 머지 재도입 — 빌드 짝 깨짐 위험"
    # robocopy는 빈(스왑된) 폴더에 통째 복사(/E).
    assert "robocopy" in s and "/E" in s
    # 원자적 스왑 — 설치 폴더 rename(.old) 잠금 게이트.
    assert "ren " in s and "QuantPlatformLocal-v9.9.9-beta.old" in s
    # 본체·좀비 일소.
    assert 'taskkill /F /IM "QuantPlatformLocal.exe"' in s
    # 복사 실패 시 롤백(불완전 폴더 제거 + 기존 복원) 분기.
    assert ":ROLLBACK" in s
    # 잠금 시 중단(기존 그대로) 분기.
    assert ":LOCKED" in s


def test_updater_bat_relaunches_app(tmp_path):
    """성공·롤백 양쪽에서 새/기존 앱을 재실행한다(사용자가 닫힌 채 방치되지 않게)."""
    s = _gen(tmp_path)
    assert s.count('start "" ') >= 2

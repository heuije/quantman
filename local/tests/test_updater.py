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
    src = tmp_path / "extracted" / "MyStock-v9.9.9-beta"
    dst = tmp_path / "install" / "MyStock-v9.9.9-beta"
    app_exe = dst / "MyStock.exe"
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
    assert "ren " in s and "MyStock-v9.9.9-beta.old" in s
    # 본체·좀비 일소.
    assert 'taskkill /F /IM "MyStock.exe"' in s
    # 복사 실패 시 롤백(불완전 폴더 제거 + 기존 복원) 분기.
    assert ":ROLLBACK" in s
    # 잠금 시 중단(기존 그대로) 분기.
    assert ":LOCKED" in s


def test_updater_bat_relaunches_app(tmp_path):
    """성공·롤백 양쪽에서 새/기존 앱을 재실행한다(사용자가 닫힌 채 방치되지 않게)."""
    s = _gen(tmp_path)
    assert s.count('start "" ') >= 2


def test_updater_bat_cds_out_of_install_before_ren(tmp_path):
    """🔴 cwd 자기잠금 회귀(v0.9.20→0.9.21 실측 사고): 앱이 설치 폴더에서 실행되면 updater
    cmd가 그 cwd를 상속하고, Windows는 프로세스 cwd인 폴더를 rename 못 해 ren 스왑이
    영영 실패(:LOCKED)했다. .bat은 ren 전에 설치 폴더 밖(%SystemRoot%)으로 cd해야 한다."""
    s = _gen(tmp_path)
    assert 'cd /d "%SystemRoot%"' in s, "설치 폴더 밖으로 cd 누락 — cwd 자기잠금 재발"
    assert s.index('cd /d "%SystemRoot%"') < s.index("ren "), "cd가 ren보다 뒤 — 잠금 안 풀림"


def test_updater_bat_swap_succeeds_when_cmd_cwd_is_install_dir(tmp_path):
    """행위 회귀(Windows): cmd cwd=설치폴더 조건에서 실제 .bat을 돌려 스왑 성공을 확인한다.
    기존 content-only 테스트가 못 잡던 *그 조건*을 재현 — 수정 전엔 ren이 'used by another
    process'로 실패해 새 빌드가 적용되지 않았다."""
    if sys.platform != "win32":
        import pytest
        pytest.skip("Windows 전용 (cmd.exe ren cwd-lock)")
    import os
    import shutil
    import subprocess
    staging = tmp_path / "stg"
    src = staging / "extracted" / "MyStock-v9.9.9-beta"
    (src / "_internal").mkdir(parents=True)
    (src / "marker_new.txt").write_text("NEW")
    (src / "_internal" / "python311.dll").write_text("dll")
    # app_exe는 *유효하고 즉시 종료하는* 실제 exe(hostname.exe)로 — bat 끝의 `start "" app_exe`가
    # 미존재 파일이면 "찾을 수 없음" modal 다이얼로그로 멈춘다(테스트 hang). 또 테스트 전용명이라
    # taskkill이 실사용자 MyStock.exe를 안 건드린다.
    exe_name = "MyStock_pytest_DONOTRUN.exe"
    shutil.copy(os.path.join(os.environ["SystemRoot"], "System32", "hostname.exe"), src / exe_name)
    install = tmp_path / "install" / "MyStock-v9.9.9-beta"
    install.mkdir(parents=True)
    (install / "marker_old.txt").write_text("OLD")
    app_exe = install / exe_name
    bat = staging / "updater.bat"
    _write_updater_bat(bat, src, install, app_exe)
    # cwd=install = 실제 사용자 조건(앱이 설치 폴더에서 실행 → 자식 cmd가 cwd 상속) 재현.
    subprocess.run(["cmd.exe", "/c", str(bat)], cwd=str(install), timeout=40,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    # 스왑 성공: install이 새 빌드로 통째 교체(옛 marker 사라지고 새 marker·_internal 존재).
    assert (install / "marker_new.txt").exists(), "스왑 실패 — 새 빌드 미적용(cwd 자기잠금?)"
    assert (install / "_internal" / "python311.dll").exists(), "_internal 미복사"
    assert not (install / "marker_old.txt").exists(), "옛 빌드 잔존 — clean replace 안 됨"

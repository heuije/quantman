"""로컬앱 자동 업데이트 — Windows·macOS Apple Silicon 양쪽.

흐름:
  1. 앱 시작 시 GitHub releases API로 최신 버전 조회 (background).
  2. 현재 버전과 비교 — 옛 버전이면 GUI 상단 배너 노출.
  3. 사용자 [지금 업데이트] 클릭 시:
     a. 플랫폼에 맞는 zip asset 다운로드 (~50~110MB, 진행률 표시).
     b. 임시 폴더 압축 해제.
     c. updater script (Windows: .bat, macOS: .sh) 작성 + detached 실행 → 앱 종료.
     d. updater script가 3초 대기 → 파일 교체 → 새 앱 실행 → 자체 정리.

Asset 선택 (v0.9.0-beta부터):
  - Windows: '...-windows.zip'
  - macOS arm64: '...-macos-arm64.zip'
  하위 호환 — v0.8.x는 suffix 없는 단일 zip (Windows 전용)으로 가정.

PyInstaller --onedir 가정.
  - Windows: 실행 중 exe는 lock 걸리지만 같은 폴더 .py·.dll은 교체 가능. exe 교체는
    별도 cmd 프로세스가 처리.
  - macOS: .app bundle 전체를 rsync로 교체. 실행 중 .app은 OS가 메모리에 매핑한
    binary만 보호 — bundle 폴더 교체는 가능 (앱 종료 후).
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, Optional

import requests

_log = logging.getLogger("updater")

# /releases/latest는 pre-release를 제외(GitHub 사양). 우리는 pre-release로 올린
# 빌드도 자동 업데이트 알림이 뜨길 원하므로 /releases 전체를 받아 SemVer 내림차순
# 정렬 후 첫 항목을 선택한다 — draft·zip-asset 없는 release는 건너뜀.
GITHUB_API = "https://api.github.com/repos/MercKR/quantman-releases/releases"
HTTP_TIMEOUT_S = 10
DOWNLOAD_TIMEOUT_S = 300


def _select_platform_asset(assets: list[dict]) -> Optional[dict]:
    """현재 플랫폼에 맞는 zip asset 선택.

    Convention (v0.9.0-beta+):
      - Windows: 이름에 '-windows' 포함
      - macOS arm64: 이름에 '-macos-arm64' 포함

    하위 호환 (v0.8.x):
      - suffix 없는 단일 zip은 Windows 전용으로 가정. macOS에선 매칭 거부 — mac
        사용자가 Windows binary를 받는 사고 방지.
    """
    zips = [a for a in assets
            if (a.get("name") or "").lower().endswith(".zip")]
    if not zips:
        return None

    plat_suffix = "-macos-arm64" if sys.platform == "darwin" else "-windows"
    matched = [a for a in zips
               if plat_suffix in (a.get("name") or "").lower()]
    if matched:
        return matched[0]

    # Suffix 없는 zip은 v0.8.x 레거시 — Windows만 fallback. macOS는 거부.
    if sys.platform != "darwin":
        legacy = [a for a in zips
                  if "-windows" not in (a.get("name") or "").lower()
                  and "-macos" not in (a.get("name") or "").lower()]
        if legacy:
            return legacy[0]
    return None


def check_latest_version() -> Optional[dict]:
    """모든 GitHub release 중 SemVer 기준 최신(비-draft·플랫폼 zip 있는) release 조회.

    Returns: {"tag": "v0.9.0-beta", "url": "https://.../*-platform.zip", "html_url": "..."}
    실패 또는 후보 없음 시 None.
    """
    try:
        r = requests.get(GITHUB_API, params={"per_page": 30}, timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        releases = r.json()
        if not isinstance(releases, list):
            return None
        candidates = []
        for rel in releases:
            if rel.get("draft"):
                continue
            tag = (rel.get("tag_name") or "").strip()
            if not tag:
                continue
            assets = rel.get("assets") or []
            zip_asset = _select_platform_asset(assets)
            if not zip_asset:
                continue
            candidates.append((_parse_version(tag), rel, zip_asset))
        if not candidates:
            _log.debug("플랫폼 zip asset 있는 release 후보 없음 (platform=%s)",
                       sys.platform)
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        _, rel, zip_asset = candidates[0]
        return {
            "tag": (rel.get("tag_name") or "").strip(),
            "url": zip_asset.get("browser_download_url"),
            "html_url": rel.get("html_url"),
        }
    except Exception as e:
        _log.debug("최신 버전 조회 실패: %s", e)
        return None


def _parse_version(s: str) -> tuple[int, ...]:
    """'v0.8.6-beta' → (0, 8, 6). 비교 가능한 tuple로.

    -beta, -rc 같은 suffix는 무시 (단순 numeric 비교). 같은 numeric이면 동등 처리.
    """
    s = s.lstrip("vV").split("-")[0].split("+")[0]
    parts = []
    for x in s.split("."):
        try:
            parts.append(int(x))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def is_newer(current: str, latest: str) -> bool:
    """latest > current 이면 True (업데이트 필요)."""
    return _parse_version(latest) > _parse_version(current)


def _app_root_and_exe() -> tuple[Path, Path]:
    """PyInstaller frozen 환경에서 앱 root 폴더·실행파일 경로 반환.

    "Root"는 업데이트 시 통째로 교체되는 폴더 단위.
      - Windows onedir: sys.executable='C:/.../QuantPlatformLocal/QuantPlatformLocal.exe'
        → root는 exe.parent (onedir 폴더 자체).
      - macOS .app bundle: sys.executable='.../QuantPlatformLocal-vX.Y.Z.app/Contents/MacOS/QuantPlatformLocal'
        → root는 .app bundle 자체 (3단계 위).

    개발 환경(python desktop.py)이면 호출자가 사전에 is_frozen()으로 차단.
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        if sys.platform == "darwin":
            # .app/Contents/MacOS/exe → .app
            return exe.parent.parent.parent, exe
        return exe.parent, exe
    # 개발 환경 — 실제 업데이트 불가, 호출자가 막아야.
    return Path(sys.argv[0]).resolve().parent, Path(sys.executable).resolve()


def is_frozen() -> bool:
    """PyInstaller 번들에서 실행 중이면 True."""
    return bool(getattr(sys, "frozen", False))


def _download_zip(url: str, dest: Path,
                   progress_cb: Optional[Callable[[int, int], None]] = None) -> None:
    """zip 다운로드 (스트리밍). progress_cb(downloaded, total) — total은 0 가능."""
    with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT_S) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb:
                    progress_cb(downloaded, total)


def _extract_zip(zip_path: Path, dest_dir: Path) -> Path:
    """zip 압축 해제. 안에 단일 폴더만 있으면 그 폴더 path 반환, 아니면 dest_dir."""
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest_dir)
    inner = [d for d in dest_dir.iterdir() if d.is_dir()]
    if len(inner) == 1:
        return inner[0]
    return dest_dir


def _write_updater_bat(bat_path: Path, src_dir: Path, dst_dir: Path,
                        app_exe: Path, parent_pid: int) -> None:
    """Windows updater.bat 작성.

    v0.9.3-beta 강화 — 이전(v0.9.2-) bat은 3초 sleep + robocopy 10초 retry +
    exit code 무시로 race condition 발생: 사용자가 본체 종료 직후 다시 띄우면
    `_internal/*.pyd` 잠금 회복 → robocopy 모든 파일 거부(exit 32) → bat은 무시
    하고 옛 exe 재실행. 사용자는 "재시작됐다"고 느끼지만 옛 버전 그대로.

    동작:
      1. taskkill /F /PID — 본체 강제 종료 (어림짐작 sleep 대신 명시적 kill).
      2. 잠금 해제 폴링 — exe 파일 rename 가능할 때까지 최대 30초 대기.
      3. robocopy /E /XO /R:30 /W:2 — 60초/파일 retry (사용자가 실수로 다시
         띄워도 회복 가능).
      4. exit code 검사 — 8+ 면 옛 exe 재실행 X + 사용자 메시지박스로 안내
         (release 페이지에서 수동 설치 유도). 4원칙 PR-1·PR-4 충족.
      5. 성공 시 새 exe 실행.
      6. 임시 폴더 + .bat 자체 삭제.

    robocopy는 onedir 폴더의 .exe lock에 걸리지 않음(앱 종료 후 실행이라).
    """
    # cmd.exe ANSI(cp949) — non-ASCII 경로/문자 위험. 우리 release는 영문 경로
    # ('QuantPlatformLocal-v0.9.3-beta/') 가정 — 사용자 디렉터리가 한글이어도
    # %APPDATA%·%TEMP%는 영문 short path로 cmd에서 동작한다.
    # PowerShell MessageBox로 실패 시 사용자 알림 (Windows 표준 UI).
    fail_msg = (
        "Quantman 업데이트 실패. 파일이 잠겨 있어 새 버전을 적용하지 못했습니다. "
        "MercKR/quantman-releases GitHub 페이지에서 최신 zip을 직접 다운로드해 "
        "기존 폴더에 덮어써주세요."
    )
    content = (
        "@echo off\r\n"
        "REM Quantman auto-updater v0.9.3 (auto-generated by app).\r\n"
        f"REM Parent PID = {parent_pid}, retry until lock release.\r\n"
        f'taskkill /F /PID {parent_pid} > nul 2>&1\r\n'
        # 잠금 해제 폴링 — exe 자체로 ren 시도. 성공하면 폴링 종료(파일 즉시 복귀).
        # 최대 30회 × 1초 = 30초. 그래도 못 풀리면 robocopy 자체 retry에 맡김.
        "set /a _N=0\r\n"
        ":WAITLOCK\r\n"
        f'ren "{app_exe}" "{app_exe.name}.lockprobe" 2>nul && '
        f'ren "{app_exe.parent / (app_exe.name + ".lockprobe")}" "{app_exe.name}" && '
        "goto :LOCKOK\r\n"
        "set /a _N+=1\r\n"
        "if %_N% GEQ 30 goto :LOCKOK\r\n"
        "ping -n 2 127.0.0.1 > nul\r\n"
        "goto :WAITLOCK\r\n"
        ":LOCKOK\r\n"
        # 본 copy — retry 30회 × 2초 = 최대 60초/파일.
        f'robocopy "{src_dir}" "{dst_dir}" /E /XO /R:30 /W:2 '
        f'> "%TEMP%\\quantman-update.log" 2>&1\r\n'
        "REM robocopy exit code 0-7 = success, 8+ = error.\r\n"
        "if %ERRORLEVEL% GEQ 8 goto :FAIL\r\n"
        # 성공 — 새 exe 실행 + 정리.
        f'start "" "{app_exe}"\r\n'
        f'rmdir /S /Q "{src_dir.parent}"\r\n'
        "(goto) 2>nul & del \"%~f0\"\r\n"
        # 실패 — 옛 exe 재실행 X (사용자가 옛 버전 그대로인지 확실히 알 수 있게).
        # PowerShell로 MessageBox 띄움 — Windows 표준.
        ":FAIL\r\n"
        "powershell -NoProfile -Command "
        '"Add-Type -AssemblyName PresentationFramework; '
        f'[System.Windows.MessageBox]::Show(\'{fail_msg}\','
        " '퀀트 플랫폼 업데이트 실패', 'OK', 'Warning')\"\r\n"
        # 옛 exe 재실행 X — 사용자가 수동 설치 후 직접 다시 띄우게.
        f'rmdir /S /Q "{src_dir.parent}"\r\n'
        "(goto) 2>nul & del \"%~f0\"\r\n"
    )
    bat_path.write_bytes(content.encode("cp949", errors="replace"))


def _write_updater_sh(sh_path: Path, src_app: Path, dst_app: Path) -> None:
    """macOS updater.sh 작성.

    동작:
      1. 3초 대기 (앱 종료 보장).
      2. rsync -a --delete로 새 .app 내용을 기존 .app으로 동기화 (삭제된 파일 반영).
      3. xattr -dr com.apple.quarantine으로 Gatekeeper quarantine 속성 자동 제거.
         (미서명 .app이라 macOS가 다운로드 직후 quarantine 부여 → 재경고 방지.)
      4. open으로 새 앱 실행.
      5. 임시 폴더 + .sh 자체 삭제.

    rsync는 실행 중 binary lock 회피 — 앱 종료 후이므로 안전.
    """
    # bash 스크립트 — POSIX, UTF-8 안전. 경로에 공백·한글 가능 → 항상 큰따옴표.
    content = (
        "#!/bin/bash\n"
        "# Quantman macOS auto-updater (auto-generated by app).\n"
        "set -e\n"
        "sleep 3\n"
        f'rsync -a --delete "{src_app}/" "{dst_app}/"\n'
        "# Gatekeeper quarantine 자동 제거 — 미서명 앱이라 두 번째 실행에서도\n"
        "# '확인되지 않은 개발자' 경고 안 뜨도록.\n"
        f'xattr -dr com.apple.quarantine "{dst_app}" 2>/dev/null || true\n'
        f'open "{dst_app}"\n'
        f'rm -rf "{src_app.parent}"\n'
        'rm -- "$0"\n'
    )
    sh_path.write_text(content, encoding="utf-8")
    sh_path.chmod(0o755)


def perform_update(zip_url: str,
                    progress_cb: Optional[Callable[[str, int, int], None]] = None
                    ) -> None:
    """업데이트 전체 flow. 성공 시 sys.exit(0) — 앱 종료 + updater 실행.

    progress_cb(stage, current, total): GUI 갱신용. stage는 "download"·"extract"·"install".
    예외 발생 시 임시 폴더 정리 + raise (UI가 캐치해 messagebox로 사용자에게 알림).

    개발 환경(non-frozen)에서 호출 시 RuntimeError — 호출자가 사전 차단.
    """
    if not is_frozen():
        raise RuntimeError("개발 환경 — 자동 업데이트는 PyInstaller 번들에서만 동작")

    app_root, app_exe = _app_root_and_exe()
    tmp_root = Path(tempfile.mkdtemp(prefix="quantman-update-"))

    try:
        # Step 1 — zip 다운로드
        zip_path = tmp_root / "update.zip"
        if progress_cb:
            progress_cb("download", 0, 100)
        _download_zip(
            zip_url, zip_path,
            progress_cb=lambda d, t: (progress_cb and progress_cb("download", d, t or 1)),
        )

        # Step 2 — 압축 해제
        extract_dir = tmp_root / "extracted"
        extract_dir.mkdir()
        if progress_cb:
            progress_cb("extract", 0, 100)
        src_dir = _extract_zip(zip_path, extract_dir)
        if progress_cb:
            progress_cb("extract", 100, 100)

        # Step 3 — updater script 작성 + detached 실행 (플랫폼 분기)
        if progress_cb:
            progress_cb("install", 0, 100)

        if sys.platform == "darwin":
            sh_path = tmp_root / "updater.sh"
            _write_updater_sh(sh_path, src_dir, app_root)
            # detached subprocess — 부모(우리 앱) 종료해도 sh 계속 동작. macOS는
            # start_new_session=True로 새 session group 만들면 부모 종료 시
            # SIGHUP 안 받음.
            subprocess.Popen(
                ["/bin/bash", str(sh_path)],
                start_new_session=True,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            bat_path = tmp_root / "updater.bat"
            # 본체 PID를 bat에 전달 — taskkill로 명시적 종료 보장.
            _write_updater_bat(bat_path, src_dir, app_root, app_exe,
                                parent_pid=os.getpid())
            # Windows는 부모 종료 시 자식 자동 kill 안 함. CREATE_NO_WINDOW로
            # console 창 안 뜸. DETACHED_PROCESS는 CREATE_NO_WINDOW와 상호배타라
            # 같이 쓰면 후자가 무시되어 cmd 창이 visible해진다 — 빼야 함.
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                ["cmd.exe", "/c", str(bat_path)],
                creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP),
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if progress_cb:
            progress_cb("install", 100, 100)

        _log.info("업데이트 시작 — app 종료 후 updater script가 파일 교체 + 재시작")
        # 메인 스레드 종료 — TrayApp.run의 tkinter mainloop 종료 필요.
        # 호출자(GUI)가 root.destroy()로 마무리한 뒤 이 함수 return하도록 함.
        # (sys.exit를 여기서 호출하면 tkinter cleanup이 막힐 수 있음.)
    except Exception:
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise

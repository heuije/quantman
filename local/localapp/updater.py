"""로컬앱 자동 업데이트.

흐름 (Phase 60):
  1. 앱 시작 시 GitHub releases API로 최신 버전 조회 (background).
  2. 현재 버전과 비교 — 옛 버전이면 GUI 상단 배너 노출.
  3. 사용자 [지금 업데이트] 클릭 시:
     a. zip 다운로드 (~50MB, 진행률 표시).
     b. 임시 폴더 압축 해제.
     c. updater.bat 작성 + detached 실행 → 앱 종료.
     d. updater.bat이 3초 대기 → robocopy로 새 파일 → 새 app.exe 실행 → 자체 정리.

PyInstaller --onedir 가정. exe 본인은 lock 걸리지만 같은 폴더 내 .py·dll 등은
실행 중 교체 가능. exe 교체는 별도 cmd 프로세스(앱 종료 후)가 처리.
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


def check_latest_version() -> Optional[dict]:
    """모든 GitHub release 중 SemVer 기준 최신(비-draft·zip 있는) release 조회.

    Returns: {"tag": "v0.8.7-beta", "url": "https://.../*.zip", "html_url": "..."}
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
            zip_asset = next((a for a in assets
                              if (a.get("name") or "").lower().endswith(".zip")), None)
            if not zip_asset:
                continue
            candidates.append((_parse_version(tag), rel, zip_asset))
        if not candidates:
            _log.debug("zip asset 있는 release 후보 없음")
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
    """PyInstaller frozen 환경에서 앱 폴더·실행파일 경로 반환.

    onedir: sys.executable이 'C:/.../QuantPlatformLocal/QuantPlatformLocal.exe'.
    개발 환경(python desktop.py)이면 None을 반환하지 않고 None signal로 호출자가
    "개발 모드" 처리.
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
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
                        app_exe: Path) -> None:
    """updater.bat 작성.

    동작:
      1. 3초 대기 (앱 종료 보장 — ping 1회 ~1초 × 3).
      2. robocopy /E (전체 트리) /XO (newer만) — src의 새 파일을 dst로 복사.
         /R:5 /W:2 (재시도 5회, 2초 wait). exit code 0~7은 정상(8+가 에러).
      3. 새 app.exe 실행 (start 명령으로 background).
      4. 임시 폴더 + .bat 자체 삭제.

    robocopy는 onedir 폴더의 .exe lock에 걸리지 않음(앱 종료 후 실행이라).
    """
    # cmd.exe ANSI(cp949) — non-ASCII 경로/문자 위험. 우리 release는 영문 경로
    # ('QuantPlatformLocal-v0.8.7-beta/') 가정 — 사용자 디렉터리가 한글이어도
    # %APPDATA%·%TEMP%는 영문 short path로 cmd에서 동작한다.
    content = (
        "@echo off\r\n"
        "REM Quantman auto-updater (auto-generated by app).\r\n"
        "REM 3-second wait to let the app process exit cleanly.\r\n"
        "ping -n 4 127.0.0.1 > nul\r\n"
        f'robocopy "{src_dir}" "{dst_dir}" /E /XO /R:5 /W:2 '
        f'> "%TEMP%\\quantman-update.log" 2>&1\r\n'
        "REM robocopy exit code 0-7 = success, 8+ = error. ignore here.\r\n"
        f'start "" "{app_exe}"\r\n'
        f'rmdir /S /Q "{src_dir.parent}"\r\n'
        "(goto) 2>nul & del \"%~f0\"\r\n"
    )
    bat_path.write_bytes(content.encode("cp949", errors="replace"))


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

        # Step 3 — updater.bat 작성 + detached 실행
        if progress_cb:
            progress_cb("install", 0, 100)
        bat_path = tmp_root / "updater.bat"
        _write_updater_bat(bat_path, src_dir, app_root, app_exe)

        # detached subprocess — 부모(우리 앱)가 종료해도 .bat은 계속 동작.
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            ["cmd.exe", "/c", str(bat_path)],
            creationflags=(DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                            | CREATE_NO_WINDOW),
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if progress_cb:
            progress_cb("install", 100, 100)

        _log.info("업데이트 시작 — app 종료 후 updater.bat이 파일 교체 + 재시작")
        # 메인 스레드 종료 — TrayApp.run의 tkinter mainloop 종료 필요.
        # 호출자(GUI)가 root.destroy()로 마무리한 뒤 이 함수 return하도록 함.
        # (sys.exit를 여기서 호출하면 tkinter cleanup이 막힐 수 있음.)
    except Exception:
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)
        raise

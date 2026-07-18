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


def is_safe_update_window(now_kst, *, intraday_running: bool,
                          pending_count: int,
                          cycle_in_flight: bool = False) -> tuple[bool, str]:
    """무인(자동) 업데이트 안전창 판정 — 주문·데이터·장중 활동과 겹치지 않는 시각.

    자동 업데이트는 앱을 재시작하므로, 사이클 발주·번들/preview 다운로드·장중 손절
    감시와 겹치면 그 작업을 유실한다. 유저의 [지금 업데이트] 수동 경로는 이 게이트를
    거치지 않는다(유저가 타이밍 판단). 자동 적용만 아래 **모두** 충족할 때 실행:

      ① 장중 세션(intraday_loop) 미가동 — WebSocket·손절 감시 회피(장중 전체).
      ② 미체결 주문 0 — 발주-체결 추적 중이면 재시작이 그 창을 끊는다.
      ③ KST 16:00~21:00 **또는 06:10~07:00**(로드맵 F) — 저녁창: KRX 정산(15:50) 후·
         US pre-warm(개장−40분, 야간 DST상 이르면 ~21:50) 전. 새벽창: 미국 정산
         (여름 05:05·겨울 06:05 — 겨울에도 06:05 이후) 뒤 ~ 아침 pre-warm(08:05) 전
         여유 포함 07:00 컷. 아침 pre-warm/개장·종가 사이클은 시각 게이트가 덮는다.

      ④ 사이클 미실행(cycle_in_flight=False) — R5: 시각창 안이라도 catchup·수동
         트리거 사이클이 도는 중이면 재시작이 발주·기장 도중을 끊는다(락 보유 확인).

    실제 상태(①②④) + 보수적 시각창(③) 이중 게이트. 하나라도 어긋나면 (False, 사유)."""
    if intraday_running:
        return False, "장중 세션 가동 중"
    if pending_count > 0:
        return False, f"미체결 주문 {pending_count}건 추적 중"
    if cycle_in_flight:
        return False, "매매 사이클 실행 중(락 보유) — 종료 후 재평가"
    in_evening = 16 <= now_kst.hour < 21
    in_dawn = now_kst.hour == 6 and now_kst.minute >= 10      # 06:10~06:59
    if not (in_evening or in_dawn):
        return False, f"안전창(16~21시·06:10~07:00 KST) 밖 — 현재 {now_kst:%H:%M}"
    return True, "안전창"


def _app_root_and_exe() -> tuple[Path, Path]:
    """PyInstaller frozen 환경에서 앱 root 폴더·실행파일 경로 반환.

    "Root"는 업데이트 시 통째로 교체되는 폴더 단위.
      - Windows onedir: sys.executable='C:/.../MyStock/MyStock.exe'
        → root는 exe.parent (onedir 폴더 자체).
      - macOS .app bundle: sys.executable='.../MyStock-vX.Y.Z.app/Contents/MacOS/MyStock'
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
                        app_exe: Path) -> None:
    """Windows updater.bat 작성 — 원자적 폴더 스왑(clean replace, 머지 아님).

    근본 결함 fix (v0.9.18→v0.9.19 실측 사고):
      옛 방식은 `robocopy /E /XO`로 새 번들을 기존 설치 폴더에 **머지**했다. 이는
        (a) 새 빌드에 없는 stale 파일을 안 지우고(예: 옛 python312.dll 잔존),
        (b) /XO로 대상보다 오래된(mtime) 새 파일을 스킵하며,
        (c) .exe가 잠겨 교체 실패해도 _internal은 계속 갱신해
      **PyInstaller 빌드 짝(.exe ↔ python3xx.dll·base_library.zip)을 깨뜨렸다.**
      실측: Python 3.12(.exe) + 3.11(base_library)가 뒤섞여 부트로더가
      "failed to start embedded python interpreter"로 죽음.

    새 방식 — 원자적 폴더 스왑(빌드 짝을 통째로만 교체):
      1. taskkill /F /IM — 본체·좀비(같은 이미지명) 인스턴스 일소.
      2. 설치 폴더 rename(.old) = **원자적 잠금 게이트**. 폴더 내 어떤 파일이라도
         잠겨 있으면(앱 미종료) rename 실패 → 재시도 → 한계 시 :LOCKED(중단,
         기존 설치 그대로 유지).
      3. 새 번들을 **빈(스왑으로 비워진) 폴더에 통째 복사** — /XO·머지·stale 없음.
      4. 성공 → 새 앱 실행 + .old·임시 정리.
      5. 복사 실패 → :ROLLBACK(불완전 폴더 제거 + .old 복원 + 기존 앱 재실행).
      부분 업데이트(.exe↔_internal 짝 불일치)를 구조적으로 절대 남기지 않는다.

    경로 가정: release는 영문 폴더('MyStock-v0.9.x-beta/'). 사용자 홈이
    한글이어도 %TEMP%·설치 경로의 ASCII 부분으로 cmd가 동작한다.
    """
    fail_msg = (
        "MyStock 업데이트를 적용하지 못했습니다. 기존 버전은 그대로 유지됩니다. "
        "앱을 완전히 종료한 뒤 잠시 후 다시 시도해 주세요."
    )
    dst_name = dst_dir.name
    dst_old = f"{dst_dir}.old"            # 스왑으로 비켜난 기존 폴더(전체 경로)
    dst_old_name = f"{dst_name}.old"      # ren의 새 이름(이름만)
    content = (
        "@echo off\r\n"
        "REM Quantman auto-updater (atomic folder-swap). 머지 아닌 통째 교체.\r\n"
        # 🔴 근본 fix (v0.9.20→0.9.21 실측 사고): 앱은 설치 폴더에서 실행되고, updater
        # cmd.exe는 그 cwd를 상속한다. Windows는 *프로세스의 cwd인 폴더*를 rename 못 한다
        # ("used by another process") → ren 스왑이 영영 실패(:LOCKED)해 업데이트가 안 됐다.
        # cmd를 설치 폴더 밖(SystemRoot)으로 cd시켜 잠금을 푼다. 이후 경로는 전부 절대경로.
        'cd /d "%SystemRoot%"\r\n'
        f'taskkill /F /IM "{app_exe.name}" > nul 2>&1\r\n'
        # 이전 실패의 잔여 .old 정리 — 스왑 차단 방지.
        f'rmdir /S /Q "{dst_old}" 2>nul\r\n'
        # 폴더 rename = 원자적 잠금 게이트. 앱 완전 종료(미잠금)돼야 성공.
        "set /a _N=0\r\n"
        ":SWAP\r\n"
        f'ren "{dst_dir}" "{dst_old_name}" 2>nul && goto :COPY\r\n'
        "set /a _N+=1\r\n"
        "if %_N% GEQ 15 goto :LOCKED\r\n"
        "ping -n 2 127.0.0.1 > nul\r\n"
        "goto :SWAP\r\n"
        ":COPY\r\n"
        # 빈(새) 폴더에 통째 복사 — /XO·머지·stale 없음. /E = 하위 폴더 포함.
        f'robocopy "{src_dir}" "{dst_dir}" /E /R:2 /W:1 '
        f'> "%TEMP%\\quantman-update.log" 2>&1\r\n'
        "REM robocopy exit code 0-7 = success, 8+ = error.\r\n"
        "if %ERRORLEVEL% GEQ 8 goto :ROLLBACK\r\n"
        # 성공 — 새 앱 실행 + .old·임시 정리.
        f'start "" "{app_exe}"\r\n'
        f'rmdir /S /Q "{dst_old}"\r\n'
        f'rmdir /S /Q "{src_dir.parent}"\r\n'
        "(goto) 2>nul & del \"%~f0\"\r\n"
        ":ROLLBACK\r\n"
        # 복사 실패 — 불완전 폴더 제거 후 기존 복원 + 재실행. 설치는 항상 정합 유지.
        f'rmdir /S /Q "{dst_dir}"\r\n'
        f'ren "{dst_old}" "{dst_name}"\r\n'
        f'start "" "{app_exe}"\r\n'
        "goto :NOTIFY\r\n"
        ":LOCKED\r\n"
        # 앱이 안 닫혀 폴더 잠금 — 스왑 자체가 안 일어나 기존 그대로.
        ":NOTIFY\r\n"
        "powershell -NoProfile -Command "
        '"Add-Type -AssemblyName PresentationFramework; '
        f'[System.Windows.MessageBox]::Show(\'{fail_msg}\','
        " 'MyStock 업데이트 실패', 'OK', 'Warning')\"\r\n"
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
            # v0.9.8-beta: PID 인자 제거 — bat이 image name + tree로 모든 인스턴스
            # (좀비 dialog 포함) 일소. parent_pid만 죽이던 v0.9.7-beta까지의 결함 fix.
            _write_updater_bat(bat_path, src_dir, app_root, app_exe)
            # Windows는 부모 종료 시 자식 자동 kill 안 함. CREATE_NO_WINDOW로
            # console 창 안 뜸. DETACHED_PROCESS는 CREATE_NO_WINDOW와 상호배타라
            # 같이 쓰면 후자가 무시되어 cmd 창이 visible해진다 — 빼야 함.
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            CREATE_NO_WINDOW = 0x08000000
            subprocess.Popen(
                ["cmd.exe", "/c", str(bat_path)],
                # cwd를 설치 폴더 밖(시스템 temp)으로 — cmd가 설치 폴더를 cwd로 상속하면
                # 그 폴더 ren(스왑)이 잠겨 실패한다(근본 fix, .bat의 cd와 이중 방어).
                cwd=tempfile.gettempdir(),
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

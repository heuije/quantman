"""파일 권한 제한 — 자격증명·민감 스냅샷에 owner-only ACL 적용.

KIS 토큰 캐시(`.kis_token.json`)와 전송 보류 스냅샷(`pending_snapshot.json`)은
keyring으로 옮기기엔 부적합한 파일 기반 저장이라, 최소한 OS 권한으로
같은 PC의 다른 사용자·프로세스 접근을 차단한다.

Windows: `icacls`로 상속 끊고 현재 사용자만 Full Control.
Unix: `os.chmod(0o600)`.

실패가 기능을 막지 않도록 예외를 삼키고 경고만 남긴다 — 권한 강화에 실패해도
원래 동작은 유지.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("localapp.file_security")


def restrict_to_owner(path: Path) -> None:
    """파일을 현재 사용자만 read/write 가능하도록 권한 제한.

    파일이 존재하지 않으면 무동작. write 직후 호출이 권장 패턴:

        path.write_text(data, encoding="utf-8")
        restrict_to_owner(path)
    """
    if not path.exists():
        return
    try:
        if sys.platform == "win32":
            _restrict_windows(path)
        else:
            os.chmod(path, 0o600)
    except Exception as e:
        log.warning("권한 제한 실패 [%s]: %s", path, e)


def _restrict_windows(path: Path) -> None:
    """Windows: icacls로 상속 해제 + 현재 사용자만 Full Control.

    `/inheritance:r`로 부모 디렉터리의 ACE 상속 제거,
    `/grant:r {user}:F`로 사용자 권한만 부여(기존 grant 교체).
    `/q /c`로 출력 억제·에러 계속.
    """
    user = os.getenv("USERNAME") or os.getenv("USER")
    if not user:
        return
    subprocess.run(
        ["icacls", str(path),
         "/inheritance:r",
         "/grant:r", f"{user}:F",
         "/q", "/c"],
        capture_output=True, timeout=5, check=False,
    )

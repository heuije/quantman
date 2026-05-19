"""단일 인스턴스 가드 — 로컬앱 이중 실행 시 이중 주문을 방지한다.

OS 파일 잠금을 사용하므로 프로세스가 비정상 종료해도 잠금이 자동 해제된다.
"""

from __future__ import annotations

import sys

from .config import APP_DIR

_LOCK_PATH = APP_DIR / "localapp.lock"
_handle = None


def acquire() -> bool:
    """잠금 획득 성공 시 True. 이미 다른 인스턴스가 실행 중이면 False."""
    global _handle
    _handle = open(_LOCK_PATH, "w")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        _handle.close()
        _handle = None
        return False


def release() -> None:
    global _handle
    if _handle is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt
            _handle.seek(0)
            msvcrt.locking(_handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(_handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        _handle.close()
        _handle = None

"""로컬앱 GUI 진입점 — 패키징(PyInstaller) 대상.

설정 창 + 트레이 상주. 단일 인스턴스만 허용한다.
개발 중에는 `python desktop.py`, 배포 시에는 빌드된 .exe로 실행된다.
"""

from __future__ import annotations

from localapp import single_instance
from localapp.logging_setup import setup_logging


def main():
    setup_logging(console=False)

    if not single_instance.acquire():
        import tkinter.messagebox as mb
        mb.showinfo("퀀트 플랫폼", "로컬앱이 이미 실행 중입니다.")
        return

    try:
        from localapp.tray import TrayApp
        TrayApp().run()
    finally:
        single_instance.release()


if __name__ == "__main__":
    main()

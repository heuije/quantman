"""로컬앱 GUI 진입점 — 패키징(PyInstaller) 대상.

설정 창 + 트레이 상주. 단일 인스턴스만 허용한다.
개발 중에는 `python desktop.py`, 배포 시에는 빌드된 .exe로 실행된다.
"""

from __future__ import annotations

from localapp import single_instance
from localapp.logging_setup import setup_logging


def _enable_dpi_awareness() -> None:
    """Windows 고DPI 모니터에서 Tkinter 텍스트 흐림 해결.

    기본 Tkinter는 96 DPI 가정 → 4K·125%+ scaling 모니터에서 OS가 앱을 저해상도로
    렌더 후 stretching → blurry. Tk root 생성 *이전*에 process DPI awareness를
    설정해야 sharp 렌더. shcore.SetProcessDpiAwareness(1)=SYSTEM_AWARE (적당히
    호환 + 선명), (2)=PER_MONITOR_AWARE (멀티모니터 다른 DPI 시 더 정확하지만
    Tk 자체가 fully 지원 안 해 부작용 가능). 1로 선택.

    fallback: shcore 없는 구버전 Windows → user32.SetProcessDPIAware() 옛 API.
    """
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        # non-Windows 또는 라이브러리 부재 — 무시 (Tkinter는 그래도 동작)
        pass


def _cleanup_stale_update_dirs() -> None:
    """%TEMP%/quantman-update-* 중 24h 이상 된 것 정리.

    v0.9.8-beta — updater :FAIL 또는 강제 종료 시 임시 폴더가 정리 안 된 채
    누적되는 케이스 대비 (이전에 16개 누적 사례). 디스크 누수 방지.
    """
    import glob
    import os
    import shutil
    import tempfile
    import time
    cutoff = time.time() - 86400  # 24h
    pattern = os.path.join(tempfile.gettempdir(), "quantman-update-*")
    for p in glob.glob(pattern):
        try:
            if os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            # mtime 조회 실패 또는 권한 — 다음 cleanup 기회로 미룸.
            pass


def _show_already_running_dialog() -> None:
    """5초 후 자동 종료되는 'already running' 알림.

    v0.9.8-beta — 옛 `mb.showinfo`는 blocking modal이라 사용자가 [확인] 안 누르면
    process가 영구 잔존. 이 좀비가 .exe·.dll을 메모리 매핑한 채로 살아있어
    updater의 robocopy가 잠금 풀지 못해 실패하는 결함을 v0.9.7-beta까지 노출.
    5초 timeout 자가 종료로 좀비 누적 차단.
    """
    import tkinter as tk
    root = tk.Tk()
    root.title("마이스톡")
    root.resizable(False, False)
    root.eval("tk::PlaceWindow . center")
    tk.Label(
        root,
        text="로컬앱이 이미 실행 중입니다.\n(5초 후 자동 종료)",
        padx=24,
        pady=20,
        font=("Segoe UI", 10),
    ).pack()
    root.after(5000, root.destroy)
    root.mainloop()


def main():
    _enable_dpi_awareness()             # tkinter import 전에 호출 필수
    setup_logging(console=False)
    _cleanup_stale_update_dirs()        # 누적된 옛 update temp 디렉터리 정리

    if not single_instance.acquire():
        # 좀비 process 누적 차단 (v0.9.8-beta) — 자세한 사유는 함수 docstring.
        _show_already_running_dialog()
        return

    try:
        from localapp.tray import TrayApp
        TrayApp().run()
    finally:
        single_instance.release()


if __name__ == "__main__":
    main()

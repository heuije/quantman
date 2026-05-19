"""트레이 상주 — 설정 창을 시스템 트레이로 감싼다.

창을 닫아도 종료되지 않고 트레이에 상주하며 스케줄러가 계속 돈다.
tkinter는 메인 스레드, 트레이 아이콘은 데몬 스레드에서 구동한다.
"""

from __future__ import annotations

import threading

import pystray
from PIL import Image, ImageDraw

from .gui import SettingsApp


def _icon_image() -> Image.Image:
    """단색 배경에 'Q'를 그린 간단한 트레이 아이콘."""
    img = Image.new("RGB", (64, 64), "#4f46e5")
    d = ImageDraw.Draw(img)
    d.ellipse((14, 14, 50, 50), outline="white", width=5)
    d.line((40, 40, 54, 54), fill="white", width=6)
    return img


class TrayApp:
    """SettingsApp + 시스템 트레이 아이콘."""

    def __init__(self):
        self.app = SettingsApp()
        self.app.on_close_to_tray = self._hide_window
        self.icon = pystray.Icon(
            "quant-platform-local", _icon_image(), "퀀트 플랫폼 로컬앱",
            menu=pystray.Menu(
                pystray.MenuItem("설정 열기", self._show_window, default=True),
                pystray.MenuItem("종료", self._quit),
            ),
        )

    def _hide_window(self):
        self.app.root.withdraw()

    def _show_window(self, _icon=None, _item=None):
        self.app.root.after(0, lambda: (self.app.root.deiconify(),
                                        self.app.root.lift()))

    def _quit(self, _icon=None, _item=None):
        self.icon.stop()
        self.app.root.after(0, self._destroy)

    def _destroy(self):
        if self.app.scheduler and self.app.scheduler.running:
            self.app.scheduler.shutdown(wait=False)
        self.app.root.destroy()

    def run(self):
        threading.Thread(target=self.icon.run, daemon=True).start()
        self.app.run()                      # tkinter mainloop (메인 스레드)


def main():
    TrayApp().run()


if __name__ == "__main__":
    main()

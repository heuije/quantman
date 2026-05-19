"""파일 로깅 설정 — 사이클·체결·오류를 APP_DIR/logs에 기록한다."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .config import APP_DIR

_configured = False


def setup_logging(console: bool = True) -> None:
    global _configured
    if _configured:
        return
    log_dir = APP_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    root = logging.getLogger("localapp")
    root.setLevel(logging.INFO)

    fh = RotatingFileHandler(log_dir / "localapp.log", maxBytes=2_000_000,
                             backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)

    _configured = True

"""로컬앱 설정."""

import os
from pathlib import Path

# 사용자 PC의 로컬앱 데이터 디렉터리 (원장·로그)
APP_DIR = Path(os.getenv("QP_LOCAL_DIR", Path.home() / ".quant-platform"))
APP_DIR.mkdir(parents=True, exist_ok=True)

# 연동할 플랫폼 서버
PLATFORM_URL = os.getenv("QP_PLATFORM_URL", "http://localhost:8000")

# keyring 서비스명 (OS 자격증명 저장소 키)
KEYRING_SERVICE = "quant-platform-local"

LEDGER_PATH = APP_DIR / "ledger.json"
EQUITY_PATH = APP_DIR / "equity.json"
TRADES_PATH = APP_DIR / "trades.jsonl"
PENDING_PATH = APP_DIR / "pending_snapshot.json"

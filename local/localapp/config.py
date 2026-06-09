"""로컬앱 설정."""

import os
from pathlib import Path

# 사용자 PC의 로컬앱 데이터 디렉터리 (원장·로그)
APP_DIR = Path(os.getenv("QP_LOCAL_DIR", Path.home() / ".quant-platform"))
APP_DIR.mkdir(parents=True, exist_ok=True)

# 연동할 플랫폼 서버 (배포 기본값 — 개발 시 QP_PLATFORM_URL 환경변수로 덮어쓰기)
PLATFORM_URL = os.getenv("QP_PLATFORM_URL",
                         "https://quantman-production.up.railway.app")

# keyring 서비스명 (OS 자격증명 저장소 키)
KEYRING_SERVICE = "quant-platform-local"

LEDGER_PATH = APP_DIR / "ledger.json"
EQUITY_PATH = APP_DIR / "equity.json"
TRADES_PATH = APP_DIR / "trades.jsonl"
PENDING_PATH = APP_DIR / "pending_snapshot.json"

# Phase 41 — preview pull 실패 시 단기 캐시(24h)로 fallback. 서버 일시 장애가
# "preview 없음 → 신규 진입 0 → 청산만" 발동시키지 않도록 안전망.
PREVIEW_CACHE_PATH = APP_DIR / "preview_cache.json"
PREVIEW_CACHE_TTL_SEC = 24 * 60 * 60

# Phase 9 추가
ORDERS_PATH = APP_DIR / "orders.jsonl"           # 주문 이벤트 로그 (제출/체결/취소/거부)
CYCLES_PATH = APP_DIR / "cycles.jsonl"           # 사이클별 의사결정 로그
KILLSWITCH_PATH = APP_DIR / "killswitch.json"    # kill switch 상태
AUTO_STATE_PATH = APP_DIR / "auto_state.json"    # 자동매매 스케줄러 상태(running/paused/stopped) — 웹 실시간 표시
PENDING_ORDERS_PATH = APP_DIR / "pending_orders.json"  # 미체결 추적
SLIPPAGE_PATH = APP_DIR / "slippage.json"        # 누적 슬리피지 통계
# L-01 — 발주 의도(intent) 저널 (append-only). 발주 직전 "submitting"으로 fsync,
# 직후 "submitted"/"failed"로 마감. 재기동 시 submitting으로 끝난 intent를 KIS
# 당일 주문 조회와 매칭해 중복 발주 방지. 자세한 설계는 intents.py.
INTENTS_PATH = APP_DIR / "intents.jsonl"

# 사용자 환경설정(평문 JSON) — 민감정보 아님. KIS API 키·계좌번호는 keyring 전용.
USER_SETTINGS_PATH = APP_DIR / "user_settings.json"

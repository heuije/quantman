"""서버 설정. 환경변수로 덮어쓸 수 있다."""

import os
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent


class Settings:
    # 인증
    SECRET_KEY: str = os.getenv("QP_SECRET_KEY", "dev-insecure-secret-change-me")
    JWT_ALGO: str = "HS256"
    ACCESS_TOKEN_HOURS: int = int(os.getenv("QP_ACCESS_TOKEN_HOURS", "168"))  # 7일

    # DB
    DB_URL: str = os.getenv("QP_DB_URL", f"sqlite:///{_BASE / 'data.db'}")

    # 기기 페어링
    PAIRING_TTL_MIN: int = 10            # 페어링 코드 유효시간(분)
    WEB_URL: str = os.getenv("QP_WEB_URL", "http://localhost:5173")

    # CORS 허용 오리진 (웹 SPA 개발 서버)
    CORS_ORIGINS: list[str] = os.getenv(
        "QP_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")


settings = Settings()

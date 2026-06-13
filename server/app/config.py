"""서버 설정. 환경변수로 덮어쓸 수 있다."""

import os
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent

_DEFAULT_SECRET = "dev-insecure-secret-change-me"

# 로컬 개발 편의 — server/.env가 있으면 KEY=VALUE를 환경변수로 로드(.env는 gitignored).
# .env는 **미설정·빈** 환경변수만 채운다(실제로 설정된 값은 보존). setdefault가 아니라
# 빈 값도 덮어쓰는 이유: 일부 환경이 ANTHROPIC_API_KEY=""처럼 빈 값을 export해두면
# setdefault가 .env를 무시하기 때문. 프로덕션(Railway)은 실제 env var가 그대로 우선.
_envfile = _BASE / ".env"
if _envfile.exists():
    for _line in _envfile.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            _v = _v.strip().strip('"').strip("'")
            if _v and not os.environ.get(_k):
                os.environ[_k] = _v


class Settings:
    # 실행 환경 — production 배포 시 QP_ENV=production 설정 (fail-fast 검증 활성).
    ENV: str = os.getenv("QP_ENV", "development")

    # 인증
    SECRET_KEY: str = os.getenv("QP_SECRET_KEY", _DEFAULT_SECRET)
    JWT_ALGO: str = "HS256"

    # /health/*/refresh 무인증 보호용 토큰 — production에서만 검증.
    # 미설정 시 production 부팅 차단(fail-fast).
    HEALTH_TOKEN: str = os.getenv("QP_HEALTH_TOKEN", "")
    ACCESS_TOKEN_HOURS: int = int(os.getenv("QP_ACCESS_TOKEN_HOURS", "168"))  # 7일

    # Google 소셜 로그인 — ID 토큰 검증 시 audience(aud) 확인용. 미설정 시 비활성.
    GOOGLE_CLIENT_ID: str = os.getenv("QP_GOOGLE_CLIENT_ID", "")

    # DB
    DB_URL: str = os.getenv("QP_DB_URL", f"sqlite:///{_BASE / 'data.db'}")

    # 기기 페어링
    PAIRING_TTL_MIN: int = 10            # 페어링 코드 유효시간(분)
    WEB_URL: str = os.getenv("QP_WEB_URL", "http://localhost:5173")

    # CORS 허용 오리진 (웹 SPA 개발 서버)
    CORS_ORIGINS: list[str] = os.getenv(
        "QP_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",")

    # NL→IR 컴파일러 (자연어 전략 설명 → StrategyIR). 키 미설정 시 /ir/compile가
    # 503을 명확히 반환(다른 기능엔 영향 없음). 모델은 env로 교체 가능.
    # 기본 Haiku 4.5 — archetype 평가 동급(12/12)이고 비용이 Sonnet의 ~1/3이라,
    # 일일 사용량 제한이 걸린 공개 베타 빌더의 기본으로 적합. 더 견고한 컴파일이
    # 필요하면 env로 상향: QP_NL_COMPILE_MODEL=claude-sonnet-4-6.
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    NL_COMPILE_MODEL: str = os.getenv("QP_NL_COMPILE_MODEL", "claude-haiku-4-5-20251001")

    # NL 컴파일 일일 사용량 제한 (인당·KST 기준). LLM 호출 비용·악용 통제.
    # admin 비밀번호(아래)로 상향 한도까지 해제. 둘 다 env로 조정 가능.
    NL_COMPILE_DAILY_LIMIT: int = int(os.getenv("QP_NL_COMPILE_DAILY_LIMIT", "10"))
    NL_COMPILE_ADMIN_LIMIT: int = int(os.getenv("QP_NL_COMPILE_ADMIN_LIMIT", "50"))
    # admin 언락 비밀번호 — 일치 시 일일 한도가 ADMIN_LIMIT로 상향(무제한 아님).
    # 단일 공유 시크릿(개인별 admin 계정 아님) — 베타 오버라이드용. 로그 미출력.
    NL_COMPILE_ADMIN_PASSWORD: str = os.getenv("QP_NL_COMPILE_ADMIN_PASSWORD", "ms2605")


settings = Settings()


# 프로덕션에서 기본(개발용) 시크릿을 그대로 쓰면 JWT 위조가 가능하다.
# QP_ENV=production인데 SECRET_KEY가 기본값이면 부팅을 막는다 (fail-fast).
if settings.ENV == "production" and settings.SECRET_KEY == _DEFAULT_SECRET:
    raise RuntimeError(
        "QP_SECRET_KEY가 설정되지 않았습니다 (기본 개발용 시크릿 사용 중). "
        "프로덕션 배포 시 QP_SECRET_KEY 환경변수를 반드시 설정하세요.")

# /health/*/refresh는 부작용이 작지만(공개데이터 재다운로드) 무인증이면 누구나
# 호출해 상류 API rate limit·비용을 소모시킬 수 있다. production에선 토큰 강제.
if settings.ENV == "production" and not settings.HEALTH_TOKEN:
    raise RuntimeError(
        "QP_HEALTH_TOKEN이 설정되지 않았습니다. "
        "프로덕션 배포 시 /health/*/refresh 보호 토큰을 반드시 설정하세요.")

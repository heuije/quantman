# Phase 7 — 보안 audit

## 🔴 Critical

### S-7-1 — `SECRET_KEY` fallback hardcoded `"dev-insecure-secret-change-me"`

**위치**: `server/app/config.py:11`
```python
SECRET_KEY: str = os.getenv("QP_SECRET_KEY", "dev-insecure-secret-change-me")
```

**영향**: 
- production env에 `QP_SECRET_KEY` 안 설정되면 hardcoded fallback 사용
- 공격자가 이 secret을 알면 **모든 사용자 JWT 위조 가능** → 인증 우회 → 어떤 계정도 접근
- repo public 시 즉시 노출 (지금 quantman은 private이지만 quantman-releases는 public)

**검증 필요**: Railway dashboard에서 `QP_SECRET_KEY` 환경변수 설정 여부 (이 리뷰 외부 확인 필수). 설정 안 됐으면 즉시 강제 회전 (+ 모든 user 재로그인 강제).

**권장 fix** (CLAUDE.md "Fallback 금지" 원칙 부합):
```python
SECRET_KEY: str = os.getenv("QP_SECRET_KEY", "")
if not SECRET_KEY:
    if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_DEPLOYMENT_ID"):
        raise RuntimeError(
            "QP_SECRET_KEY 환경변수 필수 — production에서는 강력한 random secret 설정"
        )
    # dev only — warn loudly
    import warnings
    warnings.warn("DEV: QP_SECRET_KEY 미설정, 약한 default 사용 — production 절대 금지")
    SECRET_KEY = "dev-insecure-secret-change-me"
```

자동 commit 안 함 — 사용자 결정 영역 (Railway env 먼저 확인 후 fix 적용 권장).

## 🟠 High

### S-7-2 — Webhook URL SSRF (Server-Side Request Forgery)

**위치**: `server/app/routers/sync.py:48~178` (6 호출 지점) + `preview_engine.py:415`
```python
def _post_webhook(url: str, text: str) -> bool:
    ...
    requests.post(url, json={...}, timeout=5)
```

**영향**:
- 사용자가 임의 URL 입력 가능 → 서버가 POST 요청
- 내부망 enumeration (Railway 내부 IP·메타데이터·다른 서비스)
- 예: `http://localhost:8000/admin`, `http://169.254.169.254/`, `http://10.0.0.1/`
- 인증된 사용자만 가능하지만 일반 사용자가 cloud 인프라 정보 탐색 가능

**권장 fix** (`routers/sync.py`에 helper 추가, `_post_webhook` 진입부에서 검증):
```python
from urllib.parse import urlparse

_ALLOWED_WEBHOOK_HOSTS = {
    "discord.com", "discordapp.com",
    "hooks.slack.com",
    # 사용자가 self-host 원하면 명시적 도메인 추가
}

def _validate_webhook_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        if parsed.hostname not in _ALLOWED_WEBHOOK_HOSTS:
            return False
        return True
    except Exception:
        return False
```

자동 commit 안 함 — 사용자 self-host webhook 사용 가능성 (정책 결정).

### S-7-3 — Login.tsx 패스워드 정책 `minLength=6` → **fix 적용**

**위치**: `web/src/pages/Login.tsx:92`

**Before**: `minLength={6}`
**After**: `minLength={8}` + 신규 가입 시 안내 텍스트

```tsx
<input type="password" required minLength={8} ... />
{mode === "signup" && (
  <p className="muted" style={{ fontSize: 12, marginTop: 4 }}>
    자동매매 자산 보호를 위해 8자 이상을 권장합니다. 영문·숫자·기호 혼용.
  </p>
)}
```

**한계**: 클라이언트 검증만 강화 — 서버에서도 `signup` 시 동일 검증 추가 필요 (P1, Phase 9 backlog).

## 🟡 Medium

### S-7-4 — pip-audit Windows cp949 인코딩 실패

**증상**: 
```
UnicodeDecodeError: 'cp949' codec can't decode byte 0xed in position 2
```

**원인**: `server/requirements.txt`에 한글 주석 포함 → pip-audit가 시스템 기본 인코딩 (Windows cp949)으로 읽으려다 실패.

**workaround**: 
- Linux/Mac 환경 또는 CI에서 audit 실행
- 또는 requirements.txt 한글 주석 제거 (메모리 ops_environment_pitfalls.md 함정)

**결과**: 이 환경에서 supply chain audit 미실행. **Github Dependabot 또는 CI pip-audit로 보완 권장**.

### S-7-5 — `requests` 라이브러리 stub 부재 (mypy)

`requests` import에 type stub 없어 mypy `import-untyped` 경고. 보안엔 영향 없지만 `types-requests` 설치 권장 (Phase 1 발견).

## 🟢 Low / 확인 완료

### S-7-6 — KIS 자격증명 server 미반입 ✅

`appkey|appsecret|account_number|kis_appkey` grep 결과 **0건**. CLAUDE.md 보안 원칙 부합.

### S-7-7 — 로컬앱 `.kis_token.json` 평문 저장 (acceptable)

**위치**: `local/localapp/kis_broker.py:76`

평문 JSON으로 KIS access_token 저장. 그러나:
- `file_security.py` (Phase 41-C-2/3)이 Windows ACL로 현재 사용자 전용 권한 부여
- 같은 PC의 다른 사용자/프로세스는 못 읽음
- 본인 PC 침해 시에만 노출 (그 경우 KIS 키 직접 입력도 노출)

**판정**: 데스크탑 앱 표준 (Chrome·Slack 등도 동일 방식). acceptable.

### S-7-8 — JWT 알고리즘 `HS256` 단일

`config.py:12` `JWT_ALGO: str = "HS256"`. HS256은 standard. token 만료 168h (7일) 합리적.

## 추세 비교

직전 리뷰 없음. 다음 리뷰부터 base.

## 다음

Phase 8 — quant 도메인 (체크리스트 + golden test).

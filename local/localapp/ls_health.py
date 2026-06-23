"""LS 자격증명 연결 테스트 — wizard 입력값으로 *저장 전* 검증.

`LsBroker`는 `secrets_store.load_ls()`로 저장된 값을 읽으므로 wizard처럼 *저장 전*
입력값을 검증할 때는 쓸 수 없다. 이 모듈은 입력값으로 직접 OAuth 토큰을 발급해
App Key·App Secret 유효성을 확인한다. (KIS의 `kis_health.test_credentials` 대칭.)

검증 깊이 = **토큰 발급만**. 이유: 잔고(t0424)·시세(t1102) 등 조회 TR은 응답 블록·
필드명·경로가 Phase C 실측 전까지 '초안'이라(잘못된 가정 경로로 *유효한* 키도 실패로
오판할 위험), 토큰 엔드포인트(`/oauth2/token`)만이 확정된 검증면이다. 잔고·체결까지의
깊은 검증은 `verify_ls.py`(Phase C raw 캡처)가 담당한다 — 필드 실측 후 이 모듈도 확장.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

# 모의·실전 단일 도메인(키가 환경 결정 — GOTCHAS G2). ls_broker._BASE와 동일 상수.
_BASE = "https://openapi.ls-sec.co.kr:8080"


def test_credentials(app_key: str, app_secret: str, virtual: bool) -> dict[str, Any]:
    """LS App Key/Secret을 *저장 없이* 검증 — OAuth 토큰 발급 성공 여부.

    LS 토큰은 계정 무관(appkey/appsecretkey만 사용)이라 계좌번호는 받지 않는다.
    계좌번호 형식·유효성은 Phase C(verify_ls.py 잔고/주문)에서 확정.

    Returns:
        {"ok": bool, "msg": str}  — msg는 사용자에게 보여줄 한 줄.
    """
    try:
        r = requests.post(
            f"{_BASE}/oauth2/token",
            headers={"content-type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "appkey": app_key, "appsecretkey": app_secret, "scope": "oob"},
            timeout=10)
    except requests.RequestException as e:
        return {"ok": False, "msg": f"네트워크 오류: {e}"}

    if r.status_code != 200:
        return {"ok": False,
                "msg": _format_error(r, f"토큰 발급 실패 (HTTP {r.status_code}) — App Key/Secret 확인")}

    try:
        body = r.json()
    except ValueError:
        return {"ok": False, "msg": "토큰 발급 응답을 해석할 수 없습니다"}

    if not body.get("access_token"):
        msg = (body.get("rsp_msg") or body.get("error_description")
               or body.get("error") or "App Key/Secret 무효 — 토큰이 발급되지 않았습니다")
        return {"ok": False, "msg": msg}

    mode = "모의투자" if virtual else "실전투자"
    return {"ok": True, "msg": f"App Key·Secret 유효 — {mode} 토큰 발급 성공"}


def _format_error(r: requests.Response, fallback: str) -> str:
    """LS 오류 응답(rsp_msg / error_description / error)에서 사람이 읽을 메시지 추출."""
    try:
        b = r.json()
    except ValueError:
        return fallback
    return b.get("rsp_msg") or b.get("error_description") or b.get("error") or fallback

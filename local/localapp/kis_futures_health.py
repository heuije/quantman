"""KIS 선물 자격증명 연결 테스트 — wizard/preflight에서 *저장 전* 검증 (kis_health 대칭).

`KisFuturesBroker`는 `secrets_store.load_kis_futures()`로 저장된 자격증명을 읽어 동작하므로,
*저장 전*에 입력값을 검증할 때는 쓸 수 없다. 이 모듈은 입력값을 인자로 받아 가장 가벼운
KIS 선물 호출 2개(토큰 발급 + 선물 잔고 조회)로 자격증명이 실제로 동작하는지 확인한다.

검증 깊이: 토큰 발급 + 선물 잔고 조회 (= app_key·secret + 선물 계좌번호까지 모두 검증).
주식 kis_health가 잔고조회(TTTC8434R)로 계좌를 검증하듯, 여기선 선물 잔고조회(VTFO6118R/
CTFO6118R)로 검증한다 — 토큰만 통과하고 틀린 계좌번호가 저장돼 첫 사이클에서 거부되는
이연 실패(V3)를 등록 시점에 차단한다. KIS 선물은 모의(VTS) 지원이라 결정적으로 검증 가능.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .kis_futures_broker import _BALANCE_PATH, build_balance_params

log = logging.getLogger(__name__)

# 도메인은 kis_broker.py·kis_health.py와 동일.
_REAL = "https://openapi.koreainvestment.com:9443"
_VTS = "https://openapivts.koreainvestment.com:29443"


def test_credentials(app_key: str, app_secret: str,
                     account_no: str, virtual: bool,
                     account_kind: str = "futures") -> dict[str, Any]:
    """KIS 선물 자격증명을 *저장 없이* 검증.

    Args:
        app_key, app_secret: KIS Open API 발급 키.
        account_no: "12345678-03" 또는 "1234567803" (선물 상품코드 03).
        virtual: True=모의(VTS 도메인+VTFO6118R), False=실전(실전 도메인+CTFO6118R).
        account_kind: "futures"(국내선물) | "overseas_futures"(해외선물).
            해외선물은 KIS가 모의 미지원(실전 전용)이라 실전 도메인 토큰 발급만
            검증하고 계좌 조회 검증은 후속(ls_health overseas_futures와 동일 한계).

    Returns:
        {
          "ok": bool,
          "msg": str,                # 사용자에게 보여줄 한 줄 메시지
          "rt_cd": str | None,       # KIS 응답 코드 (실패 진단용)
          "msg_cd": str | None,
        }
    """
    if account_kind == "overseas_futures":
        virtual = False              # KIS 해외선물 모의 미지원 — 항상 실전 도메인
    base = _VTS if virtual else _REAL

    # 1) 토큰 발급
    try:
        r = requests.post(
            f"{base}/oauth2/tokenP",
            json={"grant_type": "client_credentials",
                  "appkey": app_key, "appsecret": app_secret},
            timeout=10)
    except requests.RequestException as e:
        return {"ok": False, "msg": f"네트워크 오류: {e}",
                "rt_cd": None, "msg_cd": None}

    token_body = _safe_json(r)
    access_token = token_body.get("access_token")
    if r.status_code != 200 or not access_token:
        # KIS는 잘못된 키에 HTTP 403 또는 200+access_token 누락 둘 다 가능
        return {"ok": False,
                "msg": _format_kis_error(
                    token_body, fallback=f"토큰 발급 실패 (HTTP {r.status_code}) — App Key/Secret 확인"),
                "rt_cd": token_body.get("rt_cd"), "msg_cd": token_body.get("msg_cd")}

    if account_kind == "overseas_futures":
        # 해외선물 잔고 TR(OTFM1412R)은 실전 전용·미실측 — 등록 시점엔 토큰(App Key/
        # Secret)만 결정적으로 검증하고 계좌번호 검증은 후속으로 정직하게 한계 표기.
        return {"ok": True,
                "msg": "App Key·Secret 유효 (실전) — 해외선물 계좌 조회 검증은 후속(실전 전용)",
                "rt_cd": "0", "msg_cd": None}

    # 2) 계좌번호 파싱 — "12345678-03" 또는 "1234567803" (선물 상품코드 기본 03)
    norm = account_no.replace("-", "").strip()
    if len(norm) < 8:
        return {"ok": False, "msg": "계좌번호 형식이 올바르지 않습니다 (8자리 이상 필요)",
                "rt_cd": None, "msg_cd": None}
    cano = norm[:8]
    acnt_prdt_cd = norm[8:10] if len(norm) >= 10 else "03"

    # 3) 선물 잔고 조회 — 잔고 TR 선택은 KisFuturesBroker._balance_tr()와 동일(모의/실전)
    tr = "VTFO6118R" if virtual else "CTFO6118R"
    try:
        r2 = requests.get(
            f"{base}{_BALANCE_PATH}",
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {access_token}",
                     "appkey": app_key, "appsecret": app_secret,
                     "tr_id": tr, "custtype": "P"},
            params=build_balance_params(cano, acnt_prdt_cd),
            timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "msg": f"선물 잔고 조회 네트워크 오류: {e}",
                "rt_cd": None, "msg_cd": None}

    body2 = _safe_json(r2)
    if r2.status_code != 200 or body2.get("rt_cd") != "0":
        return {"ok": False,
                "msg": _format_kis_error(body2, fallback="선물 잔고 조회 실패 — 계좌번호 확인"),
                "rt_cd": body2.get("rt_cd"), "msg_cd": body2.get("msg_cd")}

    return {"ok": True, "msg": "연결 성공 · 선물 계좌 확인",
            "rt_cd": "0", "msg_cd": body2.get("msg_cd")}


def _safe_json(r: requests.Response) -> dict:
    try:
        return r.json() or {}
    except Exception:
        return {}


def _format_kis_error(body: dict, fallback: str) -> str:
    """KIS 응답 body에서 사용자 친화 에러 메시지 추출 (kis_health와 동일 형식)."""
    rt_cd = body.get("rt_cd")
    msg_cd = body.get("msg_cd")
    msg1 = (body.get("msg1") or body.get("error_description") or "").strip()
    if msg1:
        if msg_cd:
            return f"[{msg_cd}] {msg1}"
        return msg1
    if rt_cd and rt_cd != "0":
        return f"KIS 응답 코드 rt_cd={rt_cd}"
    return fallback

"""KIS 자격증명 연결 테스트 — wizard에서 입력값으로 직접 검증.

`KisBroker`는 `secrets_store.load_kis()`로 저장된 자격증명을 읽어 동작하므로,
wizard처럼 *저장 전*에 입력값을 검증할 때는 쓸 수 없다. 이 모듈은 입력값을
인자로 받아 가장 가벼운 KIS 호출 2개(토큰 발급 + 잔고 조회)로 자격증명이
실제로 동작하는지 확인한다.

검증 깊이: 토큰 발급 + 국내 잔고 조회 (= app_key·secret + 계좌번호까지 모두 검증).
해외 잔고·시세는 제외 — 모의계좌는 해외 미지원 케이스가 있어 false negative 위험.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

# 도메인은 kis_broker.py와 동일 (단일 출처는 미루지 않음 — 두 모듈 모두 const라
# 분기 적음. 향후 한 곳으로 합칠 가치 있으면 그때 통합)
_REAL = "https://openapi.koreainvestment.com:9443"
_VTS = "https://openapivts.koreainvestment.com:29443"


def test_credentials(app_key: str, app_secret: str,
                      account_no: str, virtual: bool) -> dict[str, Any]:
    """KIS 자격증명을 *저장 없이* 검증.

    Args:
        app_key, app_secret: KIS Open API 발급 키.
        account_no: "12345678-01" 또는 "1234567801" (10자리).
        virtual: True=모의(VTS 도메인+VTTC8434R), False=실전(실전 도메인+TTTC8434R).

    Returns:
        {
          "ok": bool,
          "msg": str,                      # 사용자에게 보여줄 한 줄 메시지
          "balance_krw": int | None,       # 성공 시 국내 예수금 (dnca_tot_amt)
          "total_eval_krw": int | None,    # 성공 시 국내 평가금액 (tot_evlu_amt)
          "rt_cd": str | None,             # KIS 응답 코드 (실패 진단용)
          "msg_cd": str | None,
        }
    """
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
                "balance_krw": None, "total_eval_krw": None,
                "rt_cd": None, "msg_cd": None}

    if r.status_code != 200:
        body = _safe_json(r)
        # KIS는 잘못된 키에 HTTP 403 또는 200+rt_cd!='0' 둘 다 가능
        return {"ok": False,
                "msg": _format_kis_error(body, fallback=f"토큰 발급 실패 (HTTP {r.status_code})"),
                "balance_krw": None, "total_eval_krw": None,
                "rt_cd": body.get("rt_cd"), "msg_cd": body.get("msg_cd")}

    token_body = _safe_json(r)
    access_token = token_body.get("access_token")
    if not access_token:
        return {"ok": False,
                "msg": _format_kis_error(token_body, fallback="토큰 발급 응답에 access_token 없음"),
                "balance_krw": None, "total_eval_krw": None,
                "rt_cd": token_body.get("rt_cd"), "msg_cd": token_body.get("msg_cd")}

    # 2) 계좌번호 파싱 — "12345678-01" 또는 "1234567801"
    norm = account_no.replace("-", "").strip()
    if len(norm) < 8:
        return {"ok": False, "msg": "계좌번호 형식이 올바르지 않습니다 (8자리 이상 필요)",
                "balance_krw": None, "total_eval_krw": None,
                "rt_cd": None, "msg_cd": None}
    cano = norm[:8]
    acnt_cd = norm[8:10] if len(norm) >= 10 else "01"

    # 3) 잔고 조회
    tr = "VTTC8434R" if virtual else "TTTC8434R"
    try:
        r2 = requests.get(
            f"{base}/uapi/domestic-stock/v1/trading/inquire-balance",
            headers={"content-type": "application/json",
                     "authorization": f"Bearer {access_token}",
                     "appkey": app_key, "appsecret": app_secret,
                     "tr_id": tr, "custtype": "P"},
            params={"CANO": cano, "ACNT_PRDT_CD": acnt_cd,
                    "AFHR_FLPR_YN": "N", "OFL_YN": "", "INQR_DVSN": "02",
                    "UNPR_DVSN": "01", "FUND_STTL_ICLD_YN": "N",
                    "FNCG_AMT_AUTO_RDPT_YN": "N", "PRCS_DVSN": "01",
                    "CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
            timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "msg": f"잔고 조회 네트워크 오류: {e}",
                "balance_krw": None, "total_eval_krw": None,
                "rt_cd": None, "msg_cd": None}

    body2 = _safe_json(r2)
    if r2.status_code != 200 or body2.get("rt_cd") != "0":
        return {"ok": False,
                "msg": _format_kis_error(body2, fallback="잔고 조회 실패 — 계좌번호 확인"),
                "balance_krw": None, "total_eval_krw": None,
                "rt_cd": body2.get("rt_cd"), "msg_cd": body2.get("msg_cd")}

    out = (body2.get("output2") or [{}])[0]
    try:
        cash = int(float(out.get("dnca_tot_amt", 0)))
        eval_amt = int(float(out.get("tot_evlu_amt", 0)))
    except (TypeError, ValueError):
        cash, eval_amt = 0, 0

    return {"ok": True,
            "msg": f"연결 성공 · 예수금 {cash:,}원 · 평가금액 {eval_amt:,}원",
            "balance_krw": cash, "total_eval_krw": eval_amt,
            "rt_cd": "0", "msg_cd": body2.get("msg_cd")}


def _safe_json(r: requests.Response) -> dict:
    try:
        return r.json() or {}
    except Exception:
        return {}


def _format_kis_error(body: dict, fallback: str) -> str:
    """KIS 응답 body에서 사용자 친화 에러 메시지 추출."""
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

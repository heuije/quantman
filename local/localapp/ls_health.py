"""LS 자격증명 연결 테스트 — wizard 입력값으로 *저장 전* 검증.

`LsBroker`는 `secrets_store.load_ls()`로 저장된 값을 읽으므로 wizard처럼 *저장 전*
입력값을 검증할 때는 쓸 수 없다. 이 모듈은 입력값으로 직접 OAuth 토큰을 발급하고
(App Key·App Secret 유효성) 이어서 read TR(잔고/포지션 조회)을 1회 호출해 appkey
계좌컨텍스트가 라이브임을 확인한다. (KIS의 `kis_health.test_credentials` 대칭.)

검증 깊이 = **토큰 발급 + read TR(rsp_cd 00000) 성공**.
  · token-only보다 강화: appkey가 가리키는 계좌가 실제로 조회 가능한지(라이브)까지 확인.
  · ⚠ 입력 *계좌번호 자체*는 read-only로 검증 불가 — LS는 appkey=계좌단위 발급이라
    read TR에 계좌번호를 보내지도 echo하지도 않는다(2026-06-29 모의 캡처: t0441/t0424
    응답 어디에도 계좌번호 미반영). 그래서 정직하게 한계를 메시지로 노출하고, 응답에
    계좌번호가 *우연히* echo되면(실전 가능) read-back으로 "확인됨"만 추가한다.
  · read TR은 모의·실전 공통 동작하는 t0441(선물)·t0424(주식) 사용 — CFOAQ50600은
    모의 미제공(rsp_cd 01900)이라 부적합. `_post`는 01900도 raise하지 않으므로
    rsp_cd를 명시 확인한다(예외만 보면 안 됨).
"""
from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger(__name__)

# 모의·실전 단일 도메인(키가 환경 결정 — GOTCHAS G2). ls_broker._BASE와 동일 상수.
_BASE = "https://openapi.ls-sec.co.kr:8080"


def test_credentials(app_key: str, app_secret: str, account_no: str,
                     virtual: bool, account_kind: str = "stock") -> dict[str, Any]:
    """LS 자격증명을 *저장 없이* 검증 — 토큰 발급 + read TR 조회 성공 여부.

    Args:
        app_key, app_secret: LS OpenAPI 키.
        account_no: 입력 계좌번호(검증·read-back 대조용. LS는 read TR에 안 쓰므로
            저장 전 형식 확인 + echo 시 read-back에만 사용).
        virtual: 모의(True)/실전(False).
        account_kind: "stock"(국내주식) | "futures"(국내선물). probe TR 선택.

    Returns:
        {"ok": bool, "msg": str} — msg는 사용자에게 보여줄 한 줄.
    """
    mode = "모의투자" if virtual else "실전투자"

    # 1) 토큰 발급 — App Key/Secret 유효성. 실패 시 기존 스타일 메시지로 반환.
    try:
        token = _issue_token(app_key, app_secret)
    except requests.RequestException as e:
        return {"ok": False, "msg": f"네트워크 오류: {e}"}
    except _TokenError as e:
        return {"ok": False, "msg": str(e)}
    if not token:
        return {"ok": False, "msg": "App Key/Secret 무효 — 토큰이 발급되지 않았습니다"}

    # 국내(stock/futures)만 read TR 검증. 해외선물은 별도 인증 컨텍스트(_ov)·실전 전용이라
    # 저장 전 read 검증을 후속으로 둔다 — 토큰 유효성만 확인(키는 검증, 계좌 조회는 라이브 후속).
    if account_kind not in ("stock", "futures"):
        return {"ok": True,
                "msg": f"App Key·Secret 유효 ({mode}) — '{account_kind}' 계좌 조회 검증은 후속(라이브 전용)"}

    # 2) read TR 1회 — appkey 계좌컨텍스트가 라이브인지 확인(검증된 production 경로 재사용).
    creds = {"app_key": app_key, "app_secret": app_secret,
             "account_no": account_no, "virtual": virtual}
    try:
        raw = _acct_probe(creds, account_kind)
    except Exception as e:  # noqa: BLE001 — 네트워크·5xx·인증 등 모든 조회 실패를 사용자에게 표면화
        return {"ok": False, "msg": f"LS 계좌 조회 실패: {e}"}

    rsp_cd = (raw or {}).get("rsp_cd")
    if rsp_cd == "00000":
        # read-back: 응답이 입력 계좌번호를 *우연히* echo하면 확인(실전 CFOAQ50600 등에서 가능).
        # 모의(t0441/t0424)는 echo 0이라 보통 아래 '한계 정직' 분기로 간다.
        if _account_echoed(raw, account_no):
            return {"ok": True, "msg": f"계좌 확인됨 · 조회 성공 ({mode})"}
        return {"ok": True,
                "msg": (f"App Key·Secret 유효 · 계좌 접근 확인 ({mode}) — "
                        "LS는 appkey가 계좌를 결정하므로 입력 계좌번호 자체는 "
                        "조회에 반영되지 않습니다")}

    # rsp_cd가 있고 00000이 아님 → 명시적 오류(권한·미제공 등). 코드·메시지 표면화.
    return {"ok": False,
            "msg": f"[{rsp_cd}] {(raw or {}).get('rsp_msg', '계좌 조회 실패')}"}


class _TokenError(RuntimeError):
    """토큰 발급 실패(키 무효·scope 오류 등) — 사용자 메시지를 담는다."""


def _issue_token(app_key: str, app_secret: str) -> str:
    """LS OAuth 토큰 발급 — App Key/Secret 유효성 검증면.

    grant_type=client_credentials(ls_broker._token과 동일 요청). 성공 시 access_token,
    실패 시 _TokenError(사용자 메시지) raise. 네트워크 오류는 requests.RequestException
    그대로 전파(호출자가 '네트워크 오류'로 표면화).
    """
    r = requests.post(
        f"{_BASE}/oauth2/token",
        headers={"content-type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "appkey": app_key, "appsecretkey": app_secret, "scope": "oob"},
        timeout=10)

    if r.status_code != 200:
        raise _TokenError(
            _format_error(r, f"토큰 발급 실패 (HTTP {r.status_code}) — App Key/Secret 확인"))

    try:
        body = r.json()
    except ValueError:
        raise _TokenError("토큰 발급 응답을 해석할 수 없습니다")

    token = body.get("access_token")
    if not token:
        msg = (body.get("rsp_msg") or body.get("error_description")
               or body.get("error") or "App Key/Secret 무효 — 토큰이 발급되지 않았습니다")
        raise _TokenError(msg)
    return token


def _acct_probe(creds: dict, account_kind: str) -> dict:
    """저장 전 read TR 1회로 appkey 계좌컨텍스트 확인 — 성공 응답(rsp_cd=='00000') 반환.

    검증된 production 경로(LsBroker/LsFuturesBroker)를 keyring 없이 재구성해 재사용 →
    응답 블록·필드 가정 위험 0. ⚠ LS는 계좌번호를 read TR에 안 보냄(appkey=계좌단위) →
    입력 계좌번호 자체는 검증 불가(echo 시 read-back만).

    read TR: 선물=t0441(`_positions_raw`), 주식=t0424(`_balance_raw`) — 모의·실전 공통
    동작(CFOAQ50600은 모의 미제공이라 부적합).
    """
    from .ls_broker import _LsAuth
    if account_kind == "futures":
        from .ls_futures_broker import LsFuturesBroker
        b = LsFuturesBroker.__new__(LsFuturesBroker)
        _LsAuth.__init__(b, creds)
        return b._positions_raw()              # t0441
    from .ls_broker import LsBroker
    b = LsBroker.__new__(LsBroker)
    _LsAuth.__init__(b, creds)
    b._overseas_unavailable = True             # read-only probe는 국내만 — 해외 호출 skip
    return b._balance_raw()                    # t0424


def _account_echoed(raw: Any, account_no: str) -> bool:
    """응답 어딘가에 입력 계좌번호(정규화)가 *값으로* 등장하는지 재귀 스캔 — boolean만 반환.

    **exact-equality**(부분일치 금지). 부분일치는 cano가 타임스탬프·다른 번호의 부분문자열로
    잡혀 *가짜* "계좌 확인됨"을 만들 수 있고, 그건 이 read-back의 정직성을 깬다 — 필드 값이
    정규화 후 계좌번호와 *정확히* 같을 때만 echo로 인정. (실전에서 계좌를 cano/prdt로 쪼개
    echo하면 그땐 실전 캡처로 매칭 규칙을 정밀화한다.)
    ⚠ 계좌번호 값 자체는 어디에도 출력·반환하지 않는다(INV-SEC) — 매칭 여부만.
    """
    norm = str(account_no).replace("-", "").strip()
    if len(norm) < 4:
        return False

    def _scan(node: Any) -> bool:
        if isinstance(node, dict):
            return any(_scan(v) for v in node.values())
        if isinstance(node, (list, tuple)):
            return any(_scan(v) for v in node)
        if isinstance(node, (str, int)):
            return str(node).replace("-", "").strip() == norm
        return False

    return _scan(raw)


def _format_error(r: requests.Response, fallback: str) -> str:
    """LS 오류 응답(rsp_msg / error_description / error)에서 사람이 읽을 메시지 추출."""
    try:
        b = r.json()
    except ValueError:
        return fallback
    return b.get("rsp_msg") or b.get("error_description") or b.get("error") or fallback

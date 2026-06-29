# P5.0 — 연결 테스트 계좌번호 검증 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended)
> 또는 superpowers:executing-plans로 task 단위 실행. 스텝은 `- [ ]` 체크박스로 추적.
> 이 plan은 [account-linked-strategy spec](../specs/2026-06-29-account-linked-strategy-and-fund-transparency-design.md)의 **P5.0(V3 전제)**.

**Goal:** 자격증명 연결 테스트가 **계좌번호 유효성까지** 검증하도록 확장 — 틀린 계좌번호가
"저장됨"으로 통과한 뒤 첫 사이클에서 거부되는 이연 실패(V3)를 등록 시점에 차단.

**Architecture:** KIS 주식은 이미 잔고조회(TTTC8434R)로 계좌 검증(`kis_health.py`). 이 패턴을 (1)
**KIS 선물**(KisFuturesBroker 잔고 TR)과 (2) **LS 전 슬롯**(LsBroker/LsFuturesBroker 잔고 TR)으로 확장한다.
LS는 **appkey=계좌단위 발급**이라 단순 "잘못된 번호→거부"가 성립하는지 불확실(잔고 InBlock에 AcntNo가
없을 수 있음) → **권위 계좌식별자 read-back**(응답에서 실제 계좌를 읽어 사용자 입력과 대조)이 더 견고.
그 정확한 동작·응답 필드는 **Task 1 라이브 캡처**(read-only)로 확정한 뒤 LS 구현을 마무리한다.

**Tech Stack:** Python(localapp), `requests`, `pytest`(`local/tests/`), KIS/LS REST. 기존 `kis_health.py`가
참조 구현. 표준: 토큰 발급(이미 검증) → 잔고/계좌 TR 1회 호출 → 성공이면 계좌 유효 + (가능시) read-back 대조.

**불변식:** INV-SEC — 계좌번호·키는 로컬 전용(검증은 로컬에서 브로커 직접 호출, 서버 미경유). 검증 호출은
**read-only**(잔고/계좌 조회만, 발주 없음).

---

## Task 1: LS 라이브 동작 캡처 (read-only 진단 — LS 구현의 결정 근거)

> **왜 먼저인가:** LS 잔고 TR 응답 필드(`⚠ 미검증`)와 *잘못된 계좌번호 거부 여부*가 KB에 없다. 추측 구현은
> 유효한 키를 실패로 오판(false negative)하거나 틀린 계좌를 통과(false positive)시킨다(원칙4 위반). 이 캡처가
> LS 검증 전략(거부형 vs read-back형)과 정확한 응답 필드를 확정한다. **사용자가 본인 LS 실전 국내선물 계좌로
> 1회 실행**(읽기 전용·발주 없음) — 본인 계좌도 동시에 검증된다.

**Files:**
- Create: `local/verify_ls_account.py` (read-only 진단 스크립트, `futures_preflight.py` 패턴)

- [ ] **Step 1: 캡처 스크립트 작성**

`local/verify_ls_account.py`:
```python
"""LS 계좌번호 검증 라이브 캡처 (read-only — 발주 없음).

P5.0: LS 잔고/계좌 TR이 (a) 성공/오류를 어떻게 신호하는지, (b) 잘못된 계좌번호를 거부하는지,
(c) 응답에 실제 계좌식별자가 echo되는지(read-back 가능 필드)를 실데이터로 확정한다.

실행(본인 LS 계좌로):
  python verify_ls_account.py <app_key> <app_secret> <account_no> [--real] [--futures]
  # 2회 권장: ① 올바른 계좌번호 ② 끝자리 바꾼 '틀린' 계좌번호 → 응답 차이를 비교
"""
import json, sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import localapp  # noqa: F401

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) < 3:
        print("usage: python verify_ls_account.py <app_key> <app_secret> <account_no> [--real] [--futures]")
        sys.exit(1)
    app_key, app_secret, account_no = args[0], args[1], args[2]
    virtual = "--real" not in sys.argv
    futures = "--futures" in sys.argv
    creds = {"app_key": app_key, "app_secret": app_secret,
             "account_no": account_no, "virtual": virtual}

    if futures:
        from localapp.ls_futures_broker import LsFuturesBroker
        b = LsFuturesBroker.__new__(LsFuturesBroker)
        from localapp.ls_broker import _LsAuth
        _LsAuth.__init__(b, creds)
        print(f"[LS 선물] virtual={virtual} account={account_no}")
        raw = b._acct_summary_raw()   # CFOAQ50600 — 선물 계좌요약
    else:
        from localapp.ls_broker import LsBroker
        b = LsBroker.__new__(LsBroker)
        from localapp.ls_broker import _LsAuth
        _LsAuth.__init__(b, creds)
        b._overseas_unavailable = True
        print(f"[LS 주식] virtual={virtual} account={account_no}")
        raw = b._balance_raw()        # t0424 — 주식 잔고

    print("\n--- RAW 응답 (전 필드) ---")
    print(json.dumps(raw, ensure_ascii=False, indent=2, default=str))
    print("\n점검: ① 위 호출이 예외 없이 반환됐는가(=성공 신호) "
          "② 응답에 입력 계좌번호와 매칭되는 필드가 있는가(read-back 후보) "
          "③ 틀린 계좌번호로 재실행 시 예외/오류 코드가 나는가")

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 사용자에게 실행 요청 (올바른 계좌 + 틀린 계좌 2회)**

실행 안내(읽기 전용·발주 없음): 본인 LS 국내선물 자격증명으로
`python local/verify_ls_account.py <key> <secret> <올바른계좌> --futures [--real]` 1회,
끝자리만 바꾼 *틀린* 계좌번호로 1회. 두 RAW 응답을 캡처.

- [ ] **Step 3: 캡처 결과를 plan에 기록 (LS 검증 전략 확정)**

캡처에서 다음을 확정해 Task 4·5의 구체 코드로 채운다:
- **성공/오류 신호:** `_post`가 성공 시 dict 반환·오류 시 raise임은 코드로 확정(`ls_broker.py:145-164`). 캡처로
  잘못된 계좌가 raise를 유발하는지 확인.
- **read-back 필드:** 응답에서 실제 계좌번호/식별자를 담는 키(예: 선물 `CFOAQ50600OutBlock2`의 계좌필드,
  주식 `t0424OutBlock`의 계좌필드)를 식별.
- **검증 전략 결정:** (A) 틀린 계좌가 오류를 내면 = *호출 성공 = 검증 통과*. (B) LS가 키 계좌로 무시하면 =
  *read-back한 실계좌를 정답으로 사용*(사용자 입력 불일치 시 경고·자동교정).

> 이 task는 코드 변경 없음(진단·결정). 결과가 Task 4의 검증 전략 분기를 확정한다.

---

## Task 2: KIS 선물 계좌 검증 함수 (저장 전 검증, TDD)

**Files:**
- Create: `local/localapp/kis_futures_health.py`
- Test: `local/tests/test_kis_futures_health.py`

KIS 선물은 **모의 지원**이라 결정적으로 구현·검증 가능. `kis_health.py`(주식)의 대칭. 잔고 TR·경로·파라미터는
기존 검증된 `kis_futures_broker`에서 재사용: `build_balance_params(cano, acnt_prdt_cd)`, `_BALANCE_PATH`,
그리고 모의=`VTFO6118R`/실전 TR(브로커 `_balance_tr()` 또는 모듈 상수 — 구현 시 `kis_futures_broker.py`에서 확인).

- [ ] **Step 1: 실패 테스트 작성**

`local/tests/test_kis_futures_health.py`:
```python
"""kis_futures_health.test_credentials — 저장 전 KIS 선물 계좌 검증 (모의 결정적)."""
from unittest.mock import patch, MagicMock
from localapp import kis_futures_health


def _resp(status, body):
    m = MagicMock(); m.status_code = status; m.json.return_value = body; return m


def test_token_fail_returns_not_ok():
    with patch("localapp.kis_futures_health.requests.post",
               return_value=_resp(403, {"error_description": "invalid appkey"})):
        out = kis_futures_health.test_credentials("k", "s", "12345678-03", virtual=True)
    assert out["ok"] is False
    assert "appkey" in out["msg"].lower() or "토큰" in out["msg"]


def test_balance_ok_returns_ok():
    token = _resp(200, {"access_token": "T"})
    # output2 = 계좌요약(증거금 등). rt_cd=="0" 성공.
    bal = _resp(200, {"rt_cd": "0", "output2": [{"dnca_tot_amt": "1000000"}]})
    with patch("localapp.kis_futures_health.requests.post", return_value=token), \
         patch("localapp.kis_futures_health.requests.get", return_value=bal):
        out = kis_futures_health.test_credentials("k", "s", "12345678-03", virtual=True)
    assert out["ok"] is True


def test_balance_reject_returns_not_ok():
    token = _resp(200, {"access_token": "T"})
    bad = _resp(200, {"rt_cd": "1", "msg_cd": "40570000", "msg1": "계좌번호 오류"})
    with patch("localapp.kis_futures_health.requests.post", return_value=token), \
         patch("localapp.kis_futures_health.requests.get", return_value=bad):
        out = kis_futures_health.test_credentials("k", "s", "99999999-03", virtual=True)
    assert out["ok"] is False
    assert "계좌" in out["msg"] or "40570000" in (out.get("msg_cd") or "")
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd local && python -m pytest tests/test_kis_futures_health.py -v`
Expected: FAIL (`ModuleNotFoundError: localapp.kis_futures_health`).

- [ ] **Step 3: 최소 구현**

`local/localapp/kis_futures_health.py` — `kis_health.py` 구조를 선물 잔고 TR로 미러링:
```python
"""KIS 선물 자격증명 연결 테스트 — wizard/preflight에서 저장 전 검증 (kis_health 대칭).

검증 깊이: 토큰 발급 + 선물 잔고조회 (= app_key·secret + 선물 계좌번호까지 검증).
KIS 선물은 모의 지원이라 결정적으로 검증 가능.
"""
from __future__ import annotations
from typing import Any
import requests
from .kis_futures_broker import build_balance_params, _BALANCE_PATH, _BALANCE_TR_VTS, _BALANCE_TR_REAL

_REAL = "https://openapi.koreainvestment.com:9443"
_VTS = "https://openapivts.koreainvestment.com:29443"


def test_credentials(app_key: str, app_secret: str,
                     account_no: str, virtual: bool) -> dict[str, Any]:
    base = _VTS if virtual else _REAL
    # 1) 토큰
    try:
        r = requests.post(f"{base}/oauth2/tokenP",
                          json={"grant_type": "client_credentials",
                                "appkey": app_key, "appsecret": app_secret}, timeout=10)
    except requests.RequestException as e:
        return {"ok": False, "msg": f"네트워크 오류: {e}", "rt_cd": None, "msg_cd": None}
    body = _safe_json(r)
    token = body.get("access_token")
    if r.status_code != 200 or not token:
        return {"ok": False, "msg": _fmt(body, f"토큰 발급 실패 (HTTP {r.status_code}) — App Key/Secret 확인"),
                "rt_cd": body.get("rt_cd"), "msg_cd": body.get("msg_cd")}
    # 2) 계좌번호 파싱 + 선물 잔고조회
    norm = account_no.replace("-", "").strip()
    if len(norm) < 8:
        return {"ok": False, "msg": "계좌번호 형식 오류 (8자리 이상)", "rt_cd": None, "msg_cd": None}
    cano, acnt = norm[:8], (norm[8:10] if len(norm) >= 10 else "03")
    tr = _BALANCE_TR_VTS if virtual else _BALANCE_TR_REAL
    try:
        r2 = requests.get(f"{base}{_BALANCE_PATH}",
                          headers={"content-type": "application/json",
                                   "authorization": f"Bearer {token}",
                                   "appkey": app_key, "appsecret": app_secret,
                                   "tr_id": tr, "custtype": "P"},
                          params=build_balance_params(cano, acnt), timeout=15)
    except requests.RequestException as e:
        return {"ok": False, "msg": f"선물 잔고 조회 네트워크 오류: {e}", "rt_cd": None, "msg_cd": None}
    b2 = _safe_json(r2)
    if r2.status_code != 200 or b2.get("rt_cd") != "0":
        return {"ok": False, "msg": _fmt(b2, "선물 잔고 조회 실패 — 계좌번호 확인"),
                "rt_cd": b2.get("rt_cd"), "msg_cd": b2.get("msg_cd")}
    return {"ok": True, "msg": "연결 성공 · 선물 계좌 확인", "rt_cd": "0", "msg_cd": b2.get("msg_cd")}


def _safe_json(r):
    try: return r.json() or {}
    except Exception: return {}


def _fmt(body: dict, fallback: str) -> str:
    msg1 = (body.get("msg1") or body.get("error_description") or "").strip()
    msg_cd = body.get("msg_cd")
    if msg1: return f"[{msg_cd}] {msg1}" if msg_cd else msg1
    return fallback
```

> 구현 시 `kis_futures_broker.py`에서 잔고 TR 상수/경로 실명을 확인해 import를 정합화한다(상수명이
> `_BALANCE_TR_VTS`/`_BALANCE_TR_REAL`가 아니면 실제 이름으로). `build_balance_params`·`_BALANCE_PATH`는
> `futures_preflight.py:37-44`에서 이미 import되는 검증된 공개 심볼.

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd local && python -m pytest tests/test_kis_futures_health.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: 커밋**

```bash
git add local/localapp/kis_futures_health.py local/tests/test_kis_futures_health.py
git commit -m "feat(local): KIS 선물 계좌번호 검증 함수 (kis_futures_health) — V3/P5.0"
```

---

## Task 3: LS health에 계좌번호 검증 추가 (TDD — 로직, Task 1이 필드 확정)

**Files:**
- Modify: `local/localapp/ls_health.py`
- Test: `local/tests/test_ls_health.py` (기존 — 케이스 추가)

LS 검증 = 토큰(기존) → **잔고 TR 1회 호출**(슬롯별: 주식=`LsBroker._balance_raw`, 선물=`LsFuturesBroker._acct_summary_raw`)
→ 성공(예외 없이 반환)이면 통과. Task 1 결과가 (A)거부형이면 호출 성공만으로 충분, (B)무시형이면 read-back 대조 추가.
아래 구현은 **호출-성공 검증**(두 경우 모두 필요한 최소). read-back 대조는 Task 1 확정 후 Step 3에 추가.

- [ ] **Step 1: 실패 테스트 작성 (기존 test_ls_health.py에 추가)**

```python
from unittest.mock import patch

def test_account_check_ok_when_balance_succeeds():
    # 토큰 OK + 잔고 호출이 정상 dict 반환 → ok=True
    with patch("localapp.ls_health._issue_token", return_value="T"), \
         patch("localapp.ls_health._balance_probe", return_value={"t0424OutBlock": {}}):
        out = ls_health.test_credentials("k", "s", "5544332211", virtual=True, account_kind="stock")
    assert out["ok"] is True

def test_account_check_fails_when_balance_raises():
    # 토큰 OK + 잔고 호출이 예외(LS 500 raise) → ok=False, 계좌 안내
    with patch("localapp.ls_health._issue_token", return_value="T"), \
         patch("localapp.ls_health._balance_probe", side_effect=RuntimeError("IGW...")):
        out = ls_health.test_credentials("k", "s", "9999999999", virtual=True, account_kind="stock")
    assert out["ok"] is False
    assert "계좌" in out["msg"]
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd local && python -m pytest tests/test_ls_health.py -v`
Expected: FAIL (`test_credentials() got unexpected keyword argument 'account_kind'` / `_balance_probe` 없음).

- [ ] **Step 3: 구현 — `ls_health.test_credentials` 확장**

`ls_health.py`의 `test_credentials` 시그니처에 `account_no`·`account_kind`("stock"|"futures") 추가.
토큰 발급 로직을 `_issue_token(app_key, app_secret)`로 추출(테스트 patch 지점). 토큰 성공 후
`_balance_probe(creds, account_kind)` 호출:
```python
def _balance_probe(creds: dict, account_kind: str) -> dict:
    """저장 전 잔고 1회 조회로 계좌번호 검증 — 성공 시 dict, 실패 시 예외(LS _post가 500을 raise).
    검증된 production 경로(LsBroker/LsFuturesBroker)를 그대로 재사용해 필드 가정 위험 0."""
    from .ls_broker import _LsAuth, LsBroker
    if account_kind == "futures":
        from .ls_futures_broker import LsFuturesBroker
        b = LsFuturesBroker.__new__(LsFuturesBroker); _LsAuth.__init__(b, creds)
        return b._acct_summary_raw()          # CFOAQ50600
    b = LsBroker.__new__(LsBroker); _LsAuth.__init__(b, creds)
    b._overseas_unavailable = True
    return b._balance_raw()                    # t0424
```
`test_credentials` 본문: 토큰 실패→기존 메시지. 토큰 성공 후 `_balance_probe` try/except — 성공이면
`{"ok": True, "msg": f"App Key·Secret·계좌 유효 — {mode}"}`; 예외면
`{"ok": False, "msg": "계좌 조회 실패 — 계좌번호를 확인하세요"}`.
**(Task 1 결과 (B)무시형이면**: `_balance_probe` 응답의 실계좌 필드를 입력 계좌와 대조해 불일치 시
`ok=True`로 두되 `msg`에 "입력 계좌가 키 계좌와 다름 — 실제 '<read-back>' 사용" 경고. 필드명은 Task 1 캡처값.)

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd local && python -m pytest tests/test_ls_health.py -v`
Expected: PASS.

- [ ] **Step 5: 커밋**

```bash
git add local/localapp/ls_health.py local/tests/test_ls_health.py
git commit -m "feat(local): LS 연결 테스트 계좌번호 검증 (잔고 probe) — V3/P5.0"
```

---

## Task 4: GUI wiring — LS·KIS선물 테스트에 계좌번호 전달

**Files:**
- Modify: `local/localapp/gui.py` (`_ls_test_connection` ~1519-1551; KIS 선물 입력 경로)

- [ ] **Step 1: `_ls_test_connection` 수정**

`ls_health.test_credentials(key, secret, virtual)` 호출을
`ls_health.test_credentials(key, secret, acct, virtual, account_kind=<슬롯>)` 로 변경.
`account_kind`는 현재 LS 폼의 계좌 종류 선택(`self.ls_acct_type`: `_LS_ACCT_FUTURES`→"futures", 그 외 "stock").
성공 메시지는 `result['msg']` 그대로. (이미 `acct = self.ls_e_acct.get().strip()`로 읽고 있음 — 미입력 가드도 존재.)

- [ ] **Step 2: KIS 선물 검증 경로 연결**

KIS 선물 자격증명 입력 경로(현재 GUI wizard는 주식만 저장 — 선물은 `futures_preflight.py` CLI). 선물 입력 UI가
있으면 그 테스트 버튼이 `kis_futures_health.test_credentials`를 호출하도록 연결. (선물 입력 UI가 아직 GUI에
없으면 본 task는 "kis_futures_health를 futures_preflight 토큰 단계 대체로 노출"로 한정하고, 선물 온보딩 UI는
P5 슬롯 모델에서 추가 — plan에 명시. 임의 UI 신설 금지.)

- [ ] **Step 3: 수동 확인**

`cd local && python -m localapp` → LS 폼에 올바른 계좌 입력 후 [연결 테스트] = 성공, 끝자리 틀린 계좌 = 실패
메시지(라이브 1회, 본인 계좌). KIS 주식 회귀: 기존대로 성공/실패.

- [ ] **Step 4: 커밋**

```bash
git add local/localapp/gui.py
git commit -m "feat(local): 연결 테스트가 계좌번호까지 검증 — LS/KIS선물 wiring (V3/P5.0)"
```

---

## Task 5: 전체 회귀 + 라이브 검증 게이트

- [ ] **Step 1: 로컬 전체 테스트**

Run: `cd local && python -m pytest -q`
Expected: 전부 pass (신규 3 + 기존 회귀 무영향).

- [ ] **Step 2: 라이브 검증 (사용자측, 본인 계좌)**

LS 실전 국내선물: 올바른 계좌 = 연결 성공·저장, 틀린 계좌 = 명확한 실패(저장 차단). KIS 모의 선물(있으면) 동일.
결과를 spec §8(미해결)에 "LS AcntNo 거동 = (A)거부 / (B)무시[read-back]" 중 확정된 것으로 기록.

- [ ] **Step 3: spec·brief 갱신 + 최종 커밋**

`spec §8`의 LS AcntNo 항목을 캡처 결과로 갱신. `brief.py done`으로 P5.0 완료 broadcast.

---

## Self-Review (작성자 점검)

- **Spec 커버리지:** P5.0 요구(LS·KIS선물 계좌번호 검증, read-back, 라이브 게이트) → Task 1~5로 커버. KIS 주식은
  기존 검증(작업 0). ✓
- **Placeholder:** Task 4 Step 2는 "선물 온보딩 UI 부재 시 P5로 이연"을 *명시적 조건*으로 기술(임의 UI 신설 금지) —
  미정 placeholder 아님. Task 3의 read-back 필드만 Task 1 캡처 의존(외부 KB 미검증이라 정당한 의존, 원칙4).
- **타입 일관성:** `test_credentials`는 KIS(주식·선물)·LS 모두 `{ok, msg, ...}` dict 반환으로 통일. `account_kind`
  ∈ {"stock","futures"} 일관. `_balance_probe`/`_issue_token`은 Task 3에서 정의·테스트 patch 지점으로 사용. ✓
- **알려진 한계(정직):** LS 검증의 최종 형태(거부형 vs read-back형)는 Task 1 라이브 캡처 전엔 확정 불가 —
  Task 순서가 이를 강제(캡처 → 구현). KIS 선물은 모의 지원이라 캡처 없이 결정적.

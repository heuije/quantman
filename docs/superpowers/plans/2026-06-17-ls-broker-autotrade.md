# LS증권 자동매매 브로커 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** KIS에 이은 2번째 REST 브로커 LS증권(구 이베스트투자증권)을 자동매매 실행 계층에 추가한다 — 이번 계획은 **국내주식**까지(Phase 2). 전략IR·백테스트·데이터셋은 브로커 무관이라 무변경.

**Architecture:** `Trader`는 `Broker` Protocol(11 메서드)에만 의존한다. KIS는 `KisBroker`가, LS는 새 `LsBroker`가 같은 Protocol을 구현한다. `make_broker()`가 사용자 선택(`active_broker` = "kis"|"ls")으로 둘 중 하나를 인스턴스화한다 — **단일 브로커 모델**(동시 멀티 아님). 국내선물/해외주식/해외선물은 별도 후속 plan에서 자산군 단계화로 추가.

**Tech Stack:** Python 3.11, `requests`(REST), `keyring`(자격증명), `pytest`(TDD), PyInstaller 번들. LS OpenAPI = `openapi.ls-sec.co.kr:8080`, OAuth2 client_credentials, api-id(`tr_cd`) 기반 블록 응답.

---

## 0. 맥락 — 왜·무엇·무엇이 아닌지 (zero-context 독자용)

이 저장소(MercKR/quantman)는 한국 주식 자동매매 SaaS다. 사용자는 웹에서 전략을 만들고, 본인 PC의 로컬앱(`local/localapp/`, Tkinter+PyInstaller)이 증권사 REST API로 자동 발주한다. 현재 유일한 브로커는 KIS(한국투자증권)이며 4자산군(국내/해외 주식·선물)을 이미 REST로 지원한다.

**왜 LS인가:** 사용자가 2번째 브로커로 LS증권을 도입하기로 확정(2026-06-17). LS는 키움(국내주식 REST만)과 달리 4자산군 전부 REST+WebSocket을 제공하고 OAuth 구조가 KIS와 거의 1:1이라 기존 시암에 그대로 끼울 수 있다. 거시 배경·증권사 비교는 `kiwoom-autotrade-kickoff.md`(레포 밖, Desktop/창업/퀀트/) 참조.

**이번 plan의 범위(WHAT):**
- 브로커 추상화 시암 일반화(KIS↔LS 선택).
- `LsBroker` — **국내주식만**: 인증/토큰/throttle, 잔고 스냅샷, 시세, 현금 매수/매도(시장가·지정가), 주문취소, 체결조회, 미체결.
- `docs/ls-api/` KB(검증된 TR 매핑).
- GUI 브로커 선택 UX.

**범위 밖(WHAT NOT — 명시적 비목표):**
- 전략 IR·백테스트 엔진·데이터셋·서버·웹 빌더 로직 → **무변경**. (브로커는 실행 계층 국소 추가.)
- 국내선물/해외주식/해외선물 LsBroker → **후속 plan**(자산군 단계화).
- LS WebSocket(실시간 시세·체결통보) → **후속 plan Phase 3**. 이번엔 REST 폴링만(KIS도 WS 없이 REST 폴링 fallback이 정상 동작).
- 멀티 브로커 동시 운용 → 후순위(단일 브로커 모델 확정).
- 동시에 KIS를 건드리는 변경 → 금지. KIS 경로는 **byte-identical** 보존(기본값 `active_broker="kis"`).

**계좌·키 없이 어디까지(현재 가능):** Task A2·A1·A5·A4 + B6의 코드/테스트까지 키 없이 작성·머지 가능. 실제 LS 응답 필드 검증·모의 E2E·라이브 게이트는 **키 도착 후**(§Phase C).

---

## 1. 환경 (Task 0 — 이미 완료)

- [x] **worktree 생성**: `_wt-ls` = `feat/ls-broker` 브랜치, **origin/main(`0158d66`) 기준**. (stale `platform/`·`_wt-theta` −38 금지.)
- [x] **hooksPath**: `git -C _wt-ls config core.hooksPath .githooks` (main 직접 push 차단 pre-push 훅).
- [x] **충돌 점검**: 열린 PR 중 `local/localapp/` 건드리는 것 없음 → 시암 작업 무충돌.
- [x] **브리핑**: `brief.py start` 기록 완료(다른 세션 align).

> 이후 모든 작업은 `_wt-ls/`에서. 테스트는 `cd _wt-ls/local && python -m pytest tests/ -q`.
> ⚠ 인코딩: Windows cp949 — 파이썬 stdout 한글 깨지면 `python -X utf8`. 파일은 UTF-8로 저장.

---

## 2. 파일 구조 (생성/수정 맵)

| 동작 | 경로 | 책임 |
|---|---|---|
| **생성** | `local/localapp/ls_broker.py` | `LsBroker` — Broker Protocol 구현(국내주식). 인증/토큰/throttle/HTTP + 조회·주문·체결. KIS의 `kis_broker.py`와 대칭. |
| 수정 | `local/localapp/secrets_store.py` | `_LS` 슬롯(save_ls/load_ls) + `active_broker` SSOT(set/get) + `clear()`에 추가. |
| 수정 | `local/localapp/runner.py` | `make_broker()` — `active_broker` 분기(ls→LsBroker, 기본 kis→무변경). |
| 수정 | `local/localapp/gui.py` | setup wizard에 브로커 선택(KIS/LS) + LS 자격증명 입력 폼. |
| **생성** | `docs/ls-api/{INDEX.md,GOTCHAS.md,README.md,CHANGELOG.md,endpoints/}` | LS API KB — `docs/kis-api/` 구조 그대로. |
| 수정 | `docs/api-index.md` | LS 행 등록(KB 접근법). |
| 수정 | `docs/modules/autotrade-engine.md` | 학습 원장 — LS 도입 entry(착수/완수). |
| **생성** | `local/tests/test_ls_token.py` | 토큰 발급·캐시(계정 귀속) 회귀. |
| **생성** | `local/tests/test_ls_broker_resp.py` | 주문/취소 응답 정규화 전수 + fetch_failed 마커 + status 어휘. |
| **생성** | `local/tests/test_secrets_active_broker.py` | active_broker SSOT·기본값·make_broker 분기. |

**설계 단위 경계:** `ls_broker.py`는 단일 파일(KIS도 단일 파일 1030줄 — 패턴 일치). 선물 추가 시점(후속 plan)에 `ls_futures_broker.py` 분리. 지금 분리하면 호출자 1곳뿐인 추상화(Over-engineering).

---

## 3. 설계 결정 (구현 전 합의 포인트)

### 3.1 브로커 선택 모델 — `active_broker` SSOT
현 `make_broker()`는 `KisBroker`를 하드와이어한다. 단일 브로커 모델에선 "지금 KIS냐 LS냐"를 알아야 한다.

- **결정:** `secrets_store`에 `active_broker`(="kis"|"ls") 단일 값 저장. `make_broker()`가 이걸 읽어 분기. **기본값 "kis"** → 기존 KIS 사용자는 이 값이 없으니 자동 "kis" → **완전 무변경**.
- **왜 명시 선택(presence-based 아님):** 사용자가 KIS 쓰다 LS로 전환하면 양쪽 자격증명이 동시에 존재할 수 있다. presence(어느 키가 있나)로 분기하면 모호 → 우선순위 가드(증상 봉합)가 필요해진다. 명시 선택은 모호성을 **근본 제거**(4원칙 #1). wizard가 자연히 이 값을 세팅.
- **저장 위치:** 비밀이 아니지만 브로커 정체성이라 자격증명과 같은 keyring에 co-locate(SSOT 한 곳). user_settings(JSON)에 흩지 않는다.

### 3.2 LsBroker 구조 — KisBroker 대칭, LS 차이 반영
| 항목 | KIS | LS (이번 구현) |
|---|---|---|
| base URL | 모의/실전 **도메인 분리**(_VTS/_REAL) | **단일 도메인** `openapi.ls-sec.co.kr:8080`, **키로 모의/실전 분기**(서버 라우팅). ⚠ 단일 도메인 가정 — A2에서 검증. |
| 토큰 | `/oauth2/tokenP`, expires_in(24h) | `/oauth2/token`, grant_type=client_credentials, **expires_in 그대로 존중**(LS는 익일 07:00 만료를 expires_in으로 인코딩). 하드코딩 금지. |
| 시세 키 | 모의 시 **별도 실전 앱키 필수**(EGW02004) | LS는 동일 키로 시세 제공 추정 → 별도 quote 키 **불필요**(가정 — A2 검증). 별도 키 없으면 KIS의 quote_app_key 복잡도 미도입(Over-engineering 회피). |
| 요청 식별 | header `tr_id` | header `tr_cd`(api-id) + 연속조회 `tr_cont`/`tr_cont_key`. |
| 응답 봉투 | `{rt_cd, msg_cd, msg1, output/output1/output2}` | `{rsp_cd, rsp_msg, <TRcd>OutBlock, <TRcd>OutBlock1[...]}`. ⚠ 성공코드·블록명·필드명 **전부 A2/키검증 대상**. |
| 주문 정규화 | `_submit_*`이 `{success,order_no,message,msg_cd}` 반환 | **동일 계약** — `normalize_ls_order_resp` 순수함수로 day-1부터 정규화(아래 레슨 #1). |

### 3.3 throttle·재시도
- proactive sliding-window throttle(`_Throttle`) — KIS와 동일 패턴 재사용. **LS TPS 미확인** → 보수적 기본 `max_calls=3, window_sec=1.0`로 시작, A2/키검증 후 조정(상수에 ⚠주석).
- read GET = 일시 5xx/rate-limit 재시도. **order POST = 멱등 아님** → rate-limit *접수 전 거부*에만 재시도, 그 외 즉시 raise(이중 발주 차단). KIS `_post_retry` 원칙 그대로.

---

## 4. KIS Carryover 레슨 → LS 적용 (구현 중 항상 점검)

KIS 라이브에서 *실제로 입증된* 결함의 근본 수정. LS 구현 시 **처음부터** 반영(같은 실수 반복 금지).

1. **주문 응답 정규화 day-1.** (`test_futures_broker_resp.py` 부류버그) 선물 브로커가 raw KIS json을 반환해 `Trader._after_submit`이 `r["success"]`를 falsy로 읽어 **모든 주문이 '거부' 처리**됐다. → LsBroker 모든 주문/취소 메서드는 `{success,order_no,message,msg_cd}`만 반환. `normalize_ls_order_resp` 순수함수 + **전 메서드 전수 parametrized 테스트**.
2. **부분 조회 실패 ≠ 0.** (−98% 킬스위치 오발동) 해외 잔고 조회 실패를 0으로 처리해 equity가 −98%로 보였고 킬스위치가 거짓 발동해 보유 전량 청산했다. → `account_snapshot`이 부분 실패 시 `balance["fetch_failed"]=[...]` 마커. 소비자(killswitch·equity)가 이 마커를 보고 위험 결정 보류.
3. **토큰 캐시는 계정 귀속.** (`test_kis_token_cache.py`) (도메인,appkey) 지문으로 분리 캐시 — 모의↔실전 전환 시 이전 계정 토큰 재사용 금지.
4. **체결 인지 = 폴링/조회 표준어휘.** `order_status`는 `{filled,partial,submitted,cancelled,unknown}` 표준 어휘 반환. 멱등 — 같은 체결 2번 기장 금지(canonical order-no 정규화).
5. **국내 시장가 단일.** 국내주식 시장가 주문은 단일 매매구분 코드. (KIS ORD_DVSN 01 ↔ LS 매매구분.)
6. **read=재시도, order POST=신중.** §3.3.
7. **추측 발주 금지.** 시세/계약 해석 실패 시 fallback 발주 대신 명시적 예외 → 호출자가 'error'로 보류.
8. **미검증 필드에 '완료' 선언 금지.** B6 필드명은 키 검증 전까지 "초안". 검증 가능 신호(모의 E2E) 없이 자율 완료 선언 안 함(4원칙 #4).

---

# Tasks

> A2→A1→A5→B6→A4 순. A2(KB)가 B6 필드의 1차 근거. A1(시암)이 A5/B6의 토대. 키 없이 전부 작성 가능하되 B6는 "초안".

---

### Task A2: `docs/ls-api/` KB + TR↔메서드 매핑

**Files:**
- Create: `docs/ls-api/README.md`, `docs/ls-api/INDEX.md`, `docs/ls-api/GOTCHAS.md`, `docs/ls-api/CHANGELOG.md`
- Create: `docs/ls-api/endpoints/` (TR별 .md — 국내주식 우선)
- Modify: `docs/api-index.md` (LS 행 등록)

**성격:** 리서치·문서화(TDD 아님). `docs/kis-api/` 구조를 그대로 미러. 목적 = B6 구현의 *검증된* 필드 근거 마련 + 미검증 표식.

- [ ] **Step 1: KB 골격 생성** — `docs/kis-api/README.md`·`INDEX.md`·`GOTCHAS.md` 형식을 미러해 빈 골격 작성. INDEX.md는 `| tr_cd | 이름 | 용도 | 우리 코드 위치 | 검증상태 |` 표.

- [ ] **Step 2: 공개 소스 조사** — 다음을 WebFetch/WebSearch로 수집(추측 금지, 출처 기록):
  - LS 공식 OpenAPI 가이드: `openapi.ls-sec.co.kr` → API 가이드/문서(주식 카테고리). OAuth `/oauth2/token` 스펙(grant_type, appkey/appsecret, expires_in, 익일 07:00).
  - 커뮤니티 래퍼(필드 구조 교차검증용): `teranum/ls-openapi-samples`, `callin2/ls-api`(또는 동등). xingAPI heritage TR 코드.
  - 국내주식 우선 TR 후보(⚠ A2에서 실제 확정 — 아래는 조사 시작점):
    - 현물주문(매수/매도 통합, 매매구분 필드): `CSPAT00601`
    - 정정/취소: `CSPAT00701`
    - 주식 잔고: `t0424`(주식잔고2) 또는 `CSPAQ12300`(계좌별잔고)
    - 체결/미체결: `t0425`(주식체결/미체결)
    - 현재가: `t1102`(주식현재가(시세))

- [ ] **Step 3: 국내주식 endpoint 문서 작성** — 각 확정 TR마다 `endpoints/{tr_cd}_<이름>.md`: request 헤더/body 필드, response 블록(OutBlock/OutBlock1)·필드, 모의/실전 차이, 한계, `우리 코드 위치`(LsBroker 메서드). **검증 안 된 필드는 `⚠ 미검증(키 발급 후 라이브 확정)` 명시.**

- [ ] **Step 4: GOTCHAS.md 초기 항목** — 알려진/예상 함정: 성공코드(rsp_cd) 정확값, 단일 도메인 키-라우팅, 토큰 익일 07:00 만료, TPS 한도, 연속조회(tr_cont). 검증 전 항목은 `⚠ 가정`.

- [ ] **Step 5: api-index.md 등록 + 커밋**
```bash
cd "C:/Users/USER/Desktop/창업/퀀트/_wt-ls"
git add docs/ls-api docs/api-index.md
git commit -m "docs(ls-api): LS OpenAPI KB 골격 + 국내주식 TR 매핑 초안"
```

---

### Task A1: 브로커 시암 일반화 — secrets_store + make_broker

**Files:**
- Modify: `local/localapp/secrets_store.py`
- Modify: `local/localapp/runner.py:37-65` (`make_broker`)
- Test: `local/tests/test_secrets_active_broker.py` (create)

- [ ] **Step 1: 실패 테스트 작성** — `test_secrets_active_broker.py`
```python
"""active_broker SSOT + make_broker 분기 회귀 — 기본 KIS 무변경 보장."""
from __future__ import annotations
import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


def test_active_broker_defaults_to_kis(monkeypatch):
    from localapp import secrets_store
    monkeypatch.setattr(secrets_store.keyring, "get_password", lambda *a, **k: None)
    assert secrets_store.get_active_broker() == "kis"   # 미설정 → kis (기존 사용자 무변경)


def test_set_get_active_broker_roundtrip(monkeypatch):
    from localapp import secrets_store
    store = {}
    monkeypatch.setattr(secrets_store.keyring, "set_password",
                        lambda svc, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(secrets_store.keyring, "get_password",
                        lambda svc, k: store.get(k))
    secrets_store.set_active_broker("ls")
    assert secrets_store.get_active_broker() == "ls"


def test_save_load_ls_credentials(monkeypatch):
    from localapp import secrets_store
    store = {}
    monkeypatch.setattr(secrets_store.keyring, "set_password",
                        lambda svc, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(secrets_store.keyring, "get_password",
                        lambda svc, k: store.get(k))
    secrets_store.save_ls("AK", "SK", "5550-1234", virtual=True)
    creds = secrets_store.load_ls()
    assert creds["app_key"] == "AK" and creds["account_no"] == "5550-1234"
    assert creds["virtual"] is True


def test_make_broker_routes_to_ls(monkeypatch):
    """active_broker=ls면 LsBroker, 기본이면 KisBroker 경로."""
    from localapp import runner, secrets_store
    monkeypatch.setattr(secrets_store, "get_active_broker", lambda: "ls")
    monkeypatch.setattr(secrets_store, "load_ls", lambda: {"app_key": "x"})
    import localapp.ls_broker as lb
    monkeypatch.setattr(lb, "LsBroker", lambda: "LS_INSTANCE")
    # runner가 secrets_store에서 import하므로 동일 객체 패치
    monkeypatch.setattr(runner, "load_kis", lambda: None)
    assert runner.make_broker() == "LS_INSTANCE"
```

- [ ] **Step 2: 테스트 실패 확인**
Run: `cd local && python -m pytest tests/test_secrets_active_broker.py -q`
Expected: FAIL (`get_active_broker`/`save_ls`/`load_ls` 미정의, `ls_broker` 모듈 없음)

- [ ] **Step 3: secrets_store.py 구현** — `_DEVICE` 정의 아래에 추가:
```python
_LS = "ls_credentials"
_BROKER_CHOICE = "active_broker"   # "kis" | "ls" — 단일 브로커 모델 SSOT


def save_ls(app_key: str, app_secret: str, account_no: str,
            virtual: bool = True) -> None:
    """LS증권 자격증명 저장. 국내주식 기준 — 시세도 동일 키로 조회(KIS와 달리 별도
    실전 앱키 불필요, A2 가정). 모의(virtual)는 별도 키로 LS 서버가 자동 라우팅.
    이 정보는 사용자 PC를 떠나지 않는다(서버·리포 전송 금지)."""
    keyring.set_password(KEYRING_SERVICE, _LS, json.dumps({
        "app_key": app_key, "app_secret": app_secret,
        "account_no": account_no, "virtual": virtual,
    }))


def load_ls() -> dict | None:
    raw = keyring.get_password(KEYRING_SERVICE, _LS)
    return json.loads(raw) if raw else None


def set_active_broker(name: str) -> None:
    if name not in ("kis", "ls"):
        raise ValueError(f"지원하지 않는 브로커: {name}")
    keyring.set_password(KEYRING_SERVICE, _BROKER_CHOICE, name)


def get_active_broker() -> str:
    """현재 활성 브로커. 미설정이면 'kis'(기존 사용자 무변경)."""
    return keyring.get_password(KEYRING_SERVICE, _BROKER_CHOICE) or "kis"
```
그리고 `clear()`의 키 루프를 `(_KIS, _KIS_FUT, _KIS_OVF, _LS, _BROKER_CHOICE, _DEVICE)`로 확장.

- [ ] **Step 4: runner.make_broker 분기** — 함수 맨 앞에 LS 분기 추가(KIS 경로는 그대로 아래 유지):
```python
def make_broker() -> Broker:
    from .secrets_store import get_active_broker, load_ls
    if get_active_broker() == "ls":
        if load_ls() is None:
            raise RuntimeError(
                "LS 자격증명이 등록되지 않았습니다. setup에서 LS appkey/secret/계좌를 "
                "등록하세요. (LS 모의투자는 별도 키로 발급됩니다.)")
        from .ls_broker import LsBroker   # 국내주식 단일. 선물 라우터는 후속 plan.
        return LsBroker()
    # ── 기존 KIS 경로 (완전 무변경) ──────────────────────────────────────────
    if load_kis() is None:
        raise RuntimeError(
            "KIS 자격증명이 등록되지 않았습니다. setup을 실행해 페어링·KIS 키를 "
            "먼저 등록하세요. (KIS 모의투자 가입은 무료이며 즉시 발급됩니다.)")
    from .kis_broker import KisBroker
    stock = KisBroker()
    from .secrets_store import load_kis_futures, load_kis_overseas_futures
    if not (load_kis_futures() or load_kis_overseas_futures()):
        return stock
    from .kis_futures_broker import KisFuturesBroker
    from .futures_contracts import ContractResolver
    from .broker_router import BrokerRouter
    cr = ContractResolver()
    return BrokerRouter(stock, KisFuturesBroker(),
                        resolve=cr.resolve, resolve_expiry=cr.resolve_expiry)
```
> Step 4의 `from .ls_broker import LsBroker`는 A5에서 모듈을 만들 때까지 import 에러. Step 1 테스트의 make_broker 케이스는 `ls_broker`를 monkeypatch하므로 A5 전엔 그 한 테스트만 skip/xfail 표시하고, A5 완료 후 해제. (나머지 3개는 통과.)

- [ ] **Step 5: 테스트 통과 확인**
Run: `cd local && python -m pytest tests/test_secrets_active_broker.py -q`
Expected: PASS (make_broker 케이스 제외 — A5 후 해제)

- [ ] **Step 6: 커밋**
```bash
git add local/localapp/secrets_store.py local/localapp/runner.py local/tests/test_secrets_active_broker.py
git commit -m "feat(local): 브로커 선택 시암 일반화 — active_broker SSOT + LS 슬롯 + make_broker 분기"
```

---

### Task A5: `LsBroker` 인증·토큰·throttle·HTTP 스캐폴딩

**Files:**
- Create: `local/localapp/ls_broker.py`
- Test: `local/tests/test_ls_token.py` (create)

이 Task는 **검증 가능**(우리 로직, 가짜 토큰 응답). 필드 의존 없음.

- [ ] **Step 1: 실패 테스트 작성** — `test_ls_token.py` (KIS 토큰 캐시 테스트 미러)
```python
"""LS access token 발급·캐시(계정 귀속) 회귀. 모의↔실전 전환 시 토큰 오재사용 금지."""
from __future__ import annotations
import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


def _fake_token_source(tokens):
    it = iter(tokens)

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"access_token": next(it), "expires_in": 86400,
                                 "token_type": "Bearer"}
    return lambda *a, **k: _Resp()


def _creds(app_key, virtual):
    return {"app_key": app_key, "app_secret": "sec",
            "account_no": "5550-1234", "virtual": virtual}


def test_ls_token_issued_and_cached(tmp_path, monkeypatch):
    from localapp import ls_broker
    monkeypatch.setattr(ls_broker, "_TOKEN_CACHE", tmp_path / ".ls_token.json")
    monkeypatch.setattr(ls_broker.requests, "post",
                        _fake_token_source(["TOK_1", "TOK_2"]))
    monkeypatch.setattr(ls_broker, "load_ls", lambda: _creds("AK", True))
    b = ls_broker.LsBroker()
    assert b._token() == "TOK_1"
    assert b._token() == "TOK_1"   # 캐시 적중 — 재발급 없음


def test_ls_token_not_shared_across_accounts(tmp_path, monkeypatch):
    from localapp import ls_broker
    monkeypatch.setattr(ls_broker, "_TOKEN_CACHE", tmp_path / ".ls_token.json")
    monkeypatch.setattr(ls_broker.requests, "post",
                        _fake_token_source(["TOK_PAPER", "TOK_REAL"]))
    monkeypatch.setattr(ls_broker, "load_ls", lambda: _creds("PAPER", True))
    assert ls_broker.LsBroker()._token() == "TOK_PAPER"
    monkeypatch.setattr(ls_broker, "load_ls", lambda: _creds("REAL", False))
    assert ls_broker.LsBroker()._token() == "TOK_REAL", \
        "실전 broker가 모의 토큰 재사용 — 캐시가 계정에 귀속되지 않음"
```

- [ ] **Step 2: 실패 확인**
Run: `cd local && python -m pytest tests/test_ls_token.py -q`
Expected: FAIL (`localapp.ls_broker` 없음)

- [ ] **Step 3: ls_broker.py 스캐폴딩 구현** (인증·토큰·throttle·HTTP까지만 — 조회·주문은 B6)
```python
"""LS증권(구 이베스트투자증권) REST 브로커 — 국내주식(Phase 2).

KIS의 kis_broker.py와 대칭. 자격증명은 keyring에서만 읽고, access token은 APP_DIR에
캐싱한다(계정 지문 귀속). LS는 단일 도메인에서 모의/실전을 키로 라우팅한다(KIS의 도메인
분리와 다름 — ⚠ docs/ls-api에서 검증).

⚠ 응답 필드명(블록명·rsp_cd 성공값·OutBlock 필드)은 키 발급 후 라이브 확정 전까지 '초안'.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta

import requests

from .config import APP_DIR
from .secrets_store import load_ls
from .state_store import save_json

log = logging.getLogger("localapp.ls_broker")

# LS OpenAPI — 단일 도메인, 키로 모의/실전 분기(⚠ A2 검증). KIS의 _VTS/_REAL 분리 불필요.
_BASE = "https://openapi.ls-sec.co.kr:8080"
_TOKEN_CACHE = APP_DIR / ".ls_token.json"

# LS 성공코드 — ⚠ A2/키검증 대상(현 가정 "00000"). 한 곳(SSOT)에서만 정의.
_RSP_OK = "00000"


class _Throttle:
    """sliding-window throttle. ⚠ LS TPS 미확인 → 보수적 3/s 시작, 검증 후 조정."""
    def __init__(self, max_calls: int = 3, window_sec: float = 1.0):
        self.max_calls, self.window_sec = max_calls, window_sec
        self._calls: list[float] = []
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < self.window_sec]
            if len(self._calls) >= self.max_calls:
                wait = self.window_sec - (now - self._calls[0]) + 0.01
                if wait > 0:
                    time.sleep(wait)
                    now = time.monotonic()
                    self._calls = [t for t in self._calls if now - t < self.window_sec]
            self._calls.append(now)


_GLOBAL_THROTTLE = _Throttle()   # 프로세스 전역 — 같은 LS 계정 부담 공유


class LsBroker:
    """LS증권 모의/실전 브로커. Broker Protocol 구현(국내주식 Phase 2)."""

    def __init__(self):
        creds = load_ls()
        if not creds:
            raise RuntimeError("LS 자격증명이 없습니다. 먼저 setup으로 등록하세요.")
        self.key = creds["app_key"]
        self.secret = creds["app_secret"]
        self.virtual = creds.get("virtual", True)
        self.base = _BASE
        # LS 계좌번호 포맷(⚠ A2 검증) — 보통 8자리 계좌. 하이픈 분해 보수적 처리.
        self.account_no = str(creds["account_no"]).replace("-", "")
        self._token_fp = hashlib.sha256(
            f"{self.base}:{self.key}:{int(self.virtual)}".encode()).hexdigest()[:16]

    # ── 토큰 ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _read_token_cache() -> dict:
        if not _TOKEN_CACHE.exists():
            return {}
        try:
            c = json.loads(_TOKEN_CACHE.read_text(encoding="utf-8"))
            return c if isinstance(c, dict) and "access_token" not in c else {}
        except Exception:
            return {}

    def _token(self) -> str:
        """access token — (도메인,appkey,virtual) 지문별 캐시. 만료 30분 마진 내 적중.

        LS는 grant_type=client_credentials. expires_in을 그대로 존중(LS 익일 07:00
        만료를 expires_in으로 인코딩 — 하드코딩 금지)."""
        cache = self._read_token_cache()
        ent = cache.get(self._token_fp)
        if ent and datetime.fromisoformat(ent["expires_at"]) > datetime.now() + timedelta(minutes=30):
            return ent["access_token"]
        r = requests.post(
            f"{self.base}/oauth2/token",
            headers={"content-type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials",
                  "appkey": self.key, "appsecretkey": self.secret,
                  "scope": "oob"},
            timeout=10)
        r.raise_for_status()
        d = r.json()
        cache[self._token_fp] = {
            "access_token": d["access_token"],
            "expires_at": (datetime.now()
                           + timedelta(seconds=int(d.get("expires_in", 86400)))).isoformat(),
        }
        save_json(_TOKEN_CACHE, cache)   # owner-only ACL + 원자적 저장
        return d["access_token"]

    def _headers(self, tr_cd: str, tr_cont: str = "N") -> dict:
        """LS REST 헤더 — api-id(tr_cd) + Bearer 토큰 + 연속조회 플래그."""
        return {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token()}",
            "tr_cd": tr_cd, "tr_cont": tr_cont, "tr_cont_key": "",
        }

    # ── HTTP (read=재시도, order POST=신중) ──────────────────────────────────
    def _post(self, path: str, tr_cd: str, body: dict, *,
              is_order: bool = False, timeout: int = 10, tries: int = 4) -> dict:
        """LS POST. read 조회는 일시 5xx/rate-limit 재시도, order는 멱등 아님 →
        rate-limit 접수전 거부에만 재시도(이중 발주 차단)."""
        last = None
        for i in range(tries):
            _GLOBAL_THROTTLE.acquire()
            r = requests.post(f"{self.base}{path}", headers=self._headers(tr_cd),
                              json=body, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            # ⚠ LS rate-limit 코드 미확인 — A2 검증 후 정확 코드로 가드. 현재는 429/5xx만.
            retryable = r.status_code in (429, 500, 502, 503)
            if retryable and (not is_order) and i < tries - 1:
                last = r
                time.sleep(0.3 * (i + 1))
                continue
            if retryable and is_order and r.status_code == 429 and i < tries - 1:
                last = r
                time.sleep(0.3 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        last.raise_for_status()
        return last.json()
```

- [ ] **Step 4: 테스트 통과 확인**
Run: `cd local && python -m pytest tests/test_ls_token.py -q`
Expected: PASS (2 tests)

- [ ] **Step 5: A1 make_broker 테스트 해제 + 통과** — A1 Step 4에서 보류한 `test_make_broker_routes_to_ls`의 skip/xfail 제거.
Run: `cd local && python -m pytest tests/test_secrets_active_broker.py tests/test_ls_token.py -q`
Expected: PASS (전부)

- [ ] **Step 6: 커밋**
```bash
git add local/localapp/ls_broker.py local/tests/test_ls_token.py local/tests/test_secrets_active_broker.py
git commit -m "feat(local): LsBroker 인증·토큰·throttle·HTTP 스캐폴딩 (국내주식 토대)"
```

---

### Task B6: `LsBroker` 국내주식 조회·주문 — ⚠ 응답 파싱 초안

> **이 Task는 '초안'이다.** 응답 블록명·필드명·성공코드는 **키 검증 전까지 미확정**(§Phase C에서 확정). 테스트는 *우리 계약*(Broker Protocol 출력 형태)을 잠그고, *LS 입력 형태*는 A2 조사 기반 가정 fixture로 둔다 — 키 도착 시 fixture만 교체하면 계약은 불변. **완료 선언은 모의 E2E(Phase C) 후.**

**Files:**
- Modify: `local/localapp/ls_broker.py` (조회·주문·체결·정규화 메서드 추가)
- Test: `local/tests/test_ls_broker_resp.py` (create)

- [ ] **Step 1: 정규화 순수함수 + 전수 테스트 작성** — `test_ls_broker_resp.py`
```python
"""LsBroker 주문/취소가 Broker 정규형 {success,order_no,message,msg_cd}를 반환하는지 전수.
또 account_snapshot이 부분 실패 시 fetch_failed 마커를 다는지(−98% 킬스위치 부류버그 방지).

⚠ LS 응답 fixture는 A2 조사 기반 '가정'. 키 검증(Phase C) 후 실측으로 교체."""
from __future__ import annotations
import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

import pytest
from localapp.ls_broker import normalize_ls_order_resp


def test_normalize_success_extracts_ordno():
    # ⚠ 가정 fixture: CSPAT00601 성공 응답
    raw = {"rsp_cd": "00000", "rsp_msg": "정상처리",
           "CSPAT00601OutBlock2": {"OrdNo": "12345"}}
    r = normalize_ls_order_resp(raw, ordno_field="OrdNo")
    assert r == {"success": True, "order_no": "12345",
                 "message": "정상처리", "msg_cd": "00000"}


def test_normalize_reject_is_not_success():
    raw = {"rsp_cd": "12345", "rsp_msg": "주문가능수량 초과"}
    r = normalize_ls_order_resp(raw, ordno_field="OrdNo")
    assert r["success"] is False and r["order_no"] == ""
    assert r["message"] == "주문가능수량 초과"


def test_normalize_never_leaks_raw_keys():
    raw = {"rsp_cd": "00000", "CSPAT00601OutBlock2": {"OrdNo": "7"}}
    r = normalize_ls_order_resp(raw, ordno_field="OrdNo")
    assert "rsp_cd" not in r and "CSPAT00601OutBlock2" not in r
```

- [ ] **Step 2: 실패 확인**
Run: `cd local && python -m pytest tests/test_ls_broker_resp.py -q`
Expected: FAIL (`normalize_ls_order_resp` 미정의)

- [ ] **Step 3: 정규화 함수 구현** — `ls_broker.py`에 모듈 함수 추가:
```python
def normalize_ls_order_resp(raw: dict, *, ordno_field: str) -> dict:
    """LS 주문/취소 응답 → Broker 정규형 {success,order_no,message,msg_cd}.

    raw KIS/LS json을 그대로 반환하면 Trader._after_submit이 success를 falsy로 읽어
    모든 주문이 '거부' 처리된다(선물 브로커 부류버그 재발 방지 — day-1 정규화).
    ⚠ rsp_cd 성공값·OutBlock 키·ordno_field는 키 검증 대상."""
    rsp_cd = str(raw.get("rsp_cd", ""))
    success = rsp_cd == _RSP_OK
    order_no = ""
    for k, v in raw.items():
        if k.endswith("OutBlock2") or k.endswith("OutBlock"):
            if isinstance(v, dict) and v.get(ordno_field) not in (None, ""):
                order_no = str(v[ordno_field])
                break
    return {"success": success, "order_no": order_no,
            "message": raw.get("rsp_msg", ""), "msg_cd": rsp_cd}
```

- [ ] **Step 4: 통과 확인**
Run: `cd local && python -m pytest tests/test_ls_broker_resp.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: 조회·주문·체결 메서드 구현(초안)** — Broker Protocol 11 메서드를 `ls_broker.py`에 추가. **각 메서드는 §Phase C에서 라이브 확정 표식.** 핵심 패턴(KIS 대칭):
  - `account_snapshot()` → `{"balance": {cash,total_eval,cash_usd:0,fx_usdkrw:0,foreign_eval_krw:0}, "positions": [...]}`. 국내만(해외 0). **부분 실패 시 `balance["fetch_failed"]=["..."]`**(레슨 #2).
  - `price(symbol)` → 현재가(t1102 류). `today_open(symbol)` → 당일 시가(catch-up용, 못 받으면 0.0).
  - `buy/sell(symbol, qty)` → 시장가(매매구분 코드). `buy_limit/sell_limit(symbol, qty, limit_price)` → 지정가. 전부 `normalize_ls_order_resp` 거쳐 정규형 반환.
  - `buy_resv_limit/sell_resv_limit` → **국내주식 미사용**: `raise NotImplementedError("LS 예약주문은 해외주식 단계(후속 plan)")` (조용한 무동작 금지 — 명시 예외).
  - `cancel(order_no, symbol, qty)` → 정정취소 TR, 정규형.
  - `order_status(order_no, symbol=None, hint=None)` → 표준어휘 `{order_no,status∈{filled,partial,submitted,cancelled,unknown},filled_qty,remain_qty,fill_price}`. canonical order-no 정규화.
  - `pending_orders()` → 미체결 리스트(`{order_no,symbol,name,side,qty,filled_qty,remain_qty,limit_price,...,market:"DOMESTIC",currency:"KRW"}`).
  - 모든 조회 GET 실패는 비치명적이되 **fetch_failed 표식**으로 표면화(0으로 위장 금지).

- [ ] **Step 6: account_snapshot fetch_failed 마커 테스트 추가** — `test_ls_broker_resp.py`에:
```python
def test_account_snapshot_tags_fetch_failed_on_error(monkeypatch):
    """잔고 조회 실패를 0으로 위장하지 않고 fetch_failed로 표면화(−98% 부류버그 방지)."""
    from localapp import ls_broker
    b = object.__new__(ls_broker.LsBroker)
    def _boom(*a, **k): raise RuntimeError("LS 게이트웨이 5xx")
    monkeypatch.setattr(b, "_balance_raw", _boom, raising=False)
    snap = b.account_snapshot()
    assert snap["balance"].get("fetch_failed"), "부분 실패가 fetch_failed로 표면화 안 됨"
```
(구현은 account_snapshot의 조회를 try/except로 감싸 실패 시 `balance["fetch_failed"]` 세팅.)

- [ ] **Step 7: 전체 테스트 + 커밋**
Run: `cd local && python -m pytest tests/test_ls_broker_resp.py tests/test_ls_token.py tests/test_secrets_active_broker.py -q`
Expected: PASS
```bash
git add local/localapp/ls_broker.py local/tests/test_ls_broker_resp.py
git commit -m "feat(local): LsBroker 국내주식 조회·주문·체결 (응답파싱 초안 — 키검증 대상)"
```

---

### Task A4: 브로커 선택 UX (GUI setup wizard)

**Files:**
- Modify: `local/localapp/gui.py` (setup wizard — KIS/LS 선택 + LS 입력 폼)

**성격:** GUI — TDD 대신 앱 실행 검증. 연결 테스트는 키 필요(Phase C).

- [ ] **Step 1: wizard에 브로커 선택** — `① KIS 자격증명` LabelFrame 위에 브로커 선택(라디오: KIS / LS). 선택 시 `secrets_store.set_active_broker(...)` + 해당 입력 폼 토글.
- [ ] **Step 2: LS 입력 폼** — appkey/secret/계좌/모의여부. 저장 시 `secrets_store.save_ls(...)`. (KIS 폼 재사용 패턴 — `_save_kis` 대칭 `_save_ls`.)
- [ ] **Step 3: 배지 갱신** — 상태 배지(gui.py:923 부근)에 활성 브로커 표시("LS · 국내주식").
- [ ] **Step 4: 앱 실행 검증** — `cd local && python -m localapp` → wizard에서 LS 선택·저장 → 재시작 후 `get_active_broker()=="ls"`·`load_ls()` 확인. (발주·연결은 키 도착 후.)
- [ ] **Step 5: 커밋**
```bash
git add local/localapp/gui.py
git commit -m "feat(local): setup wizard 브로커 선택(KIS/LS) + LS 자격증명 입력"
```

---

### Task B7: LS 사이클 경로 broker-aware 스윕 (구조적 — 부류 닫기)

> A1 코드리뷰에서 발견된 **부류(class) 결함**. `run_cycle`/settlement은 KIS 전용 헬퍼를 **활성 브로커와 무관하게** 호출한다 — LS가 실제로 사이클을 돌리면(B6 이후) 이 경로들이 KIS를 가정한다. 증상 1건씩 때우지 말고 **전수 스캔→한 기준으로 부류 닫기**(active_broker 게이팅 또는 Broker Protocol로 일반화).

**Files:** `local/localapp/runner.py`, `local/localapp/trader.py`, `local/localapp/intents.py`, **`local/localapp/gui.py`** (전수 스캔으로 확정)

> **부류 = `load_kis()`/`if kis:`로 브로커 동작을 게이트하는 모든 지점.** A4 코드리뷰에서 gui.py에 다수 사이트가 추가 발견됨(아래). 단건 수정 금지 — **공용 헬퍼 1개**(예: `secrets_store.active_cred_ok()` → 활성 브로커의 자격증명 존재 여부)를 만들고 전 사이트를 그것으로 치환해 부류를 한 번에 닫는다.

- [ ] **Step 1: 전수 스캔** — `local/localapp/`에서 `load_kis(`·`if kis:`·`reconcile_with_kis`·KIS 전용 메서드·`hts_id`·체결통보 WS 호출을 grep. 각각이 (a) LS에서 안전 no-op인지 (b) Broker Protocol만 쓰는지(이름만 KIS) (c) KIS 전용이라 게이팅 필요한지 분류. **알려진 후보(2026-06-17 grep):**
  - **gui.py `_handle_command` (실 기능 버그)**: `load_kis() is None` 가드가 `RUN_CYCLE_NOW`(~1223)·`LIQUIDATE_ALL`(~1251)·기타(~1271·~1286·~2108)에서 활성 브로커 무관 → **LS 활성+KIS 미등록 시 웹 명령이 "KIS 자격증명 없음"으로 거부**. 활성 브로커 자격증명(`active_cred_ok()`)으로 치환 + 라벨 동적화.
  - **gui.py `if kis:` 렌더 가드**(~760·~853·~876·~909, 잔고·타임라인·hero): LS 활성 시 LS 잔고 갱신 경로 누락. broker-aware 가드로(LS 잔고는 B6 account_snapshot 사용).
  - **gui.py `_toggle_setup_expanded`(~776)·`_wizard_jump_to_input`(~2025)**: `load_kis()`로 KIS wizard 진입 → 활성 브로커 게이트(LS면 LS 폼).
  - `runner.py:_wait_for_order_ws()` — `load_kis().hts_id` 의존. LS는 KIS 체결통보 WS 없음 → `get_active_broker()=="kis"`일 때만 수행하도록 게이트.
  - `_run_settlement_locked` → `trader.reconcile_with_kis()` — 이름은 KIS지만 Broker Protocol(account_snapshot/pending)만 쓰면 LS도 동작. 실제 본문 확인 후, KIS 전용 호출이 있으면 일반화 또는 게이트.
  - `run_cycle` → `intents.reconcile_submitting(broker, ...)` — broker 인자 기반이면 LS 안전. 확인.
  - **완료 기준: `git grep -n "load_kis()" local/localapp` 결과 중 브로커-게이트 목적 사이트가 0이거나 전부 `active_cred_ok()` 류로 치환됨**(전수 닫힘 검증).
- [ ] **Step 2: 회귀 테스트** — active_broker="ls"·LsBroker(mock)로 `_wait_for_order_ws`가 KIS WS를 건드리지 않고 즉시 반환하는지, 정산이 LsBroker로 동작하는지 테스트.
- [ ] **Step 3: 구현** — 분류에 따라 게이트(active_broker) 또는 Protocol 일반화. **KIS 경로 동작 무변경 보존**(KIS 회귀 green).
- [ ] **Step 4: 커밋** `fix(local): LS 활성 시 KIS 전용 사이클 경로 게이팅 (부류 닫기)`

> 위치 근거: B6(LsBroker 메서드)만으로는 LS가 사이클에서 *호출*되지만 KIS 전용 헬퍼가 남는다. B7이 그 배선 간극을 닫아야 Phase 2(국내주식 LS) end-to-end가 성립. A4(GUI) 전/후 무관하나 모의 E2E(Phase C) 전 필수.

## Phase C — 계좌·키 도착 후 (검증 체크리스트, 코드 아님)

키 없이는 **검증 불가**(4원칙 #4: 추측 완료 금지). 사용자가 LS 계좌개설→OpenAPI 신청→모의 키 수령 후:

- [ ] **미확인 5건 실측**: ①해외옵션 지원 ②정확 rate limit(TPS) ③모의 유효기간·대상자산군 ④토큰 1일 발급제한 ⑤IP 등록 요구. → `docs/ls-api/GOTCHAS.md` 확정.
- [ ] **응답 필드 라이브 확정**: B6의 모든 `⚠` 표식(rsp_cd 성공값·OutBlock 블록명·OrdNo/잔고/체결 필드명·계좌포맷·매매구분 코드)을 실측으로 확정 → fixture·상수 교체. throttle TPS 조정.
- [ ] **order_status 체결/취소 인식 수정 (GOTCHAS G10)**: B6 `order_status`는 t0425 `chegb="2"`(미체결만)라 전량체결·취소를 인지 못 하고 `unknown` 반환(체결은 정산 reconcile 백스톱). 모의 키로 체결·취소 1건씩 발생 → t0425 `chegb="0"`(전체) 응답에서 filled vs cancelled 구분 필드 확인 → `order_status`를 전체조회/일별체결 TR로 전환(`pending_orders`는 chegb="2" 유지). **구분 필드 확인 전엔 변경 금지**(취소→체결 오인 방지).
- [ ] **모의 E2E 라운드트립**: 모의 키로 매수→체결조회→잔고반영→매도→종가청산 1회. SimBroker 골든 회귀 byte-identical 보존.
- [ ] **라이브 게이트**: 국내주식 마이크로 실거래 1건으로 확정(KIS 패턴) → 점진 개방.
- [ ] **완료 선언**: 모의 E2E 통과 후에만 "국내주식 LS 지원 완료". 그 전까지 "초안".

---

## 후속 계획 (별도 plan — 이번 범위 밖)

- **Phase 3 — LS WebSocket**: 실시간 시세 + 체결통보(멱등 ingestion). KIS `kis_websocket.py`/`kis_order_websocket.py` 대칭.
- **Phase 4 — 자산군 확장**: 국내선물 → 해외주식 → 해외선물. 각자 별도 plan(TR 매핑·통화/FX·종가청산·`LsFuturesBroker`+`BrokerRouter` LS판·KIS 동일함정 점검 = LS 해외선물 모의 지원 여부). 해외주식 단계에서 `buy_resv_limit`의 NotImplementedError 해제.

각 후속 plan은 해당 단계 착수 시 writing-plans로 별도 작성(이 문서는 국내주식까지).

---

## Self-Review (작성 후 점검)

**1. 스펙 커버리지** — pre-account 범위(roadmap A1·A2·A4·A5·B6) 전부 Task로 매핑됨(A2·A1·A5·B6·A4). C(키 필요)는 체크리스트로 분리. ✅
**2. Placeholder 스캔** — "TBD/적절히 처리" 없음. 미검증 필드는 의도적 `⚠ 초안` 표식(placeholder 아님 — 검증 절차가 Phase C에 명시). ✅
**3. 타입 일관성** — Broker 정규형 `{success,order_no,message,msg_cd}`·snapshot `{balance:{...,fetch_failed},positions:[...]}`·status 어휘가 KisBroker와 동일(Trader 계약 일치). `normalize_ls_order_resp(raw,*,ordno_field)` 시그니처가 테스트·구현 일치. ✅
**4. KIS 무변경** — make_broker 기본 분기 "kis", 기존 본문 그대로. KIS 테스트·골든 영향 없음. ✅

---

## Execution Handoff

**계획 저장 완료: `docs/superpowers/plans/2026-06-17-ls-broker-autotrade.md`.**

> ⚠ 협업 규칙: 이 계획은 **설계안**이다. 규모 있는 작업이므로 코드 착수 전 사용자 승인 필요. 승인 후 A2부터 task-by-task.

# LS Phase D — 국내선물(KOSPI200) 자동매매 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** KOSPI200 국내선물을 LS 자동매매 실행계층에 추가한다 — `LsFuturesBroker`(국내선물) + LS 계약 resolver + `BrokerRouter` LS 배선. 전략IR·백테스트·데이터·KIS는 무변경(byte-identical).

**Architecture:** `BrokerRouter`(broker-agnostic, `local/localapp/broker_router.py`)를 **재사용**한다 — stock=`LsBroker`(기존), futures=`LsFuturesBroker`(신규), `resolve`=LS 계약 resolver. `LsFuturesBroker`는 `LsBroker`의 인증/토큰/throttle/HTTP를 **재사용**하고 LS 선물 TR(`docs/ls-api/domestic-futures-research.md`)만 매핑한다. 계약코드↔데이터셋 역매핑은 KIS 코드(`A01606`)와 LS 코드(`101V6000`)가 달라, `BrokerRouter`에 **`dataset_for_code` 콜백을 주입**(기본=`quant_core.dataset_for_contract` → KIS 무변경; LS는 LS-aware 역매핑 주입)해 **core 무변경**으로 처리한다.

**Tech Stack:** Python 3.11, `requests`(REST), `keyring`(자격증명), `pytest`(TDD). LS OpenAPI `/futureoption/{order,accno,market-data}`. KIS 레퍼런스 = `kis_futures_broker.py`·`broker_router.py`·`futures_contracts.py`.

---

## 0. 맥락 (zero-context 독자용)

이 저장소는 한국 주식 자동매매 SaaS. 로컬앱(`local/localapp/`)이 증권사 REST로 자동 발주한다. `Trader`는 `Broker` Protocol(`broker.py` 11메서드)에만 의존하고, 심볼이 선물이면 `BrokerRouter`가 선물 브로커로 라우팅한다. 현재 KIS는 4자산군 전부, LS는 **국내주식만**(`ls_broker.py`, 라이브 검증됨). 이번 plan은 LS에 **국내선물**을 추가한다(해외주식=Phase E, 해외선물=Phase F는 별도 plan).

**LS 선물 TR 정본 = [`docs/ls-api/domestic-futures-research.md`](../../ls-api/domestic-futures-research.md)** — 모든 필드/경로/코드값의 근거. 작업 중 이 표를 항상 대조한다.

**범위 밖(WHAT NOT):** 전략IR·백테스트·데이터·서버·웹 → 무변경. KIS 경로 → **byte-identical**(`kis_futures_broker.py`·`broker_router.py` 기본 동작 보존). core `quant_core/futures_contract.py` → **무변경**(LS 역매핑은 router 콜백 주입으로).

## 1. KIS Carryover 레슨 → LS 선물 적용 (구현 중 항상 점검)

KIS 라이브에서 *실제로 입증된* 결함의 근본 수정. 처음부터 반영:
1. **주문 응답 정규화 day-1.** 모든 주문/취소 메서드는 `{success, order_no, message, msg_cd}`만 반환. 성공판정 = **OrdNo 존재**(rsp_cd 고정값 아님 — 선물 rsp_cd는 "00040"매수/"00132"정정 등 가변; G17 패턴). raw json 누출 금지(선물 부류버그 재발 방지).
2. **부분 조회 실패 ≠ 0.** `account_snapshot`이 2-TR(CFOAQ50600+t0441) 중 하나라도 실패하면 `balance` 미위장 — `BrokerRouter`가 `fetch_failed=["futures"]` 표식(킬스위치 거짓 -98% 청산 차단). LsFuturesBroker는 실패를 raise(국내주식 `account_snapshot` try/except 패턴과 동형).
3. **체결 인지 = 전체상태 조회.** `order_status`는 t0434를 **`chegb="0"`(전체)**로 조회 → 체결·취소 행 포함, `status` 문자열로 filled/cancelled 구분(국내주식 G19 = chegb=0 전환과 동형). 미체결-only(chegb="2")로는 체결을 못 봐 정산 백스톱만 남는다.
4. **추측 발주 금지.** 계약코드 해석 실패(마스터 미수신) 시 resolve가 None/raise → 호출부(BrokerRouter `_code`)가 RuntimeError → Trader 발주 skip(fallback 발주 금지).
5. **read=재시도, order POST=신중.** GET 조회는 일시 5xx/429 재시도, 주문 POST는 멱등 아님(이중발주 차단) — `LsBroker._post(is_order=)` 패턴 재사용.
6. **롱/숏 = BnsTpCode net position.** 국내선물은 진입/청산 별도코드 없음(KIS 동일). 롱진입=매수("2")·롱청산=매도("1")·숏진입=매도("1")·숏청산=매수("2"). Trader가 side로 호출하면 broker가 BnsTpCode로 변환.
7. **취소 pending 오보고 차단.** t0434에서 `orgordno≠0`(정정/취소 주문행)은 resting 신규가 아님 → `pending_orders`에서 제외(KIS `orgn_odno` 필터 동형).

---

## 2. 파일 구조 (생성/수정 맵)

| 동작 | 경로 | 책임 |
|---|---|---|
| **생성** | `local/localapp/ls_futures_broker.py` | `LsFuturesBroker` — 국내선물 Broker 메서드. `LsBroker` 인증/HTTP 재사용. 선물 master(t8432) fetch 포함. |
| **생성** | `local/localapp/ls_futures_contracts.py` | `LsContractResolver` — 데이터셋 심볼↔LS 계약코드(101V6000). t8432 master 1일 캐시. |
| 수정 | `local/localapp/secrets_store.py` | `_LS_FUT` 슬롯 + `save_ls_futures`/`load_ls_futures` + `clear()` 확장. |
| 수정 | `local/localapp/runner.py` (`make_broker`) | LS 분기에 선물 자격증명 있으면 `BrokerRouter(LsBroker, LsFuturesBroker, resolve, dataset_for_code)` 반환. |
| 수정 | `local/localapp/broker_router.py` | `dataset_for_code` 콜백 주입(기본=quant_core, KIS 무변경). account_snapshot이 콜백 사용. |
| **생성** | `local/tests/test_ls_futures_resp.py` | 주문 정규화·account_snapshot 2-TR·order_status status·pending orgordno 필터 전수. |
| **생성** | `local/tests/test_ls_contract_resolver.py` | LS master 파싱→근월물 코드·역매핑 회귀. |
| 수정 | `local/tests/test_broker_router_dataset_cb.py`(생성) | dataset_for_code 콜백 주입 회귀(KIS 기본 보존 + LS 주입). |

**설계 경계:** `LsFuturesBroker`는 국내선물 메서드 + 해외선물 메서드(Phase F에서 추가)를 한 파일에 둔다(KIS `kis_futures_broker.py`가 국내+해외 단일 파일 — 패턴 일치). Phase D는 국내선물 메서드만. 해외선물 메서드는 Phase F에서 같은 파일에 추가.

---

## 3. 설계 결정

### 3.1 BrokerRouter 재사용 + dataset_for_code 콜백
`BrokerRouter`는 `resolve`(dataset→code)를 이미 콜백으로 받는다. 유일한 KIS-결합점은 `account_snapshot`의 `dataset_for_contract(code)`(code→dataset, KIS 코드 `^A\d`만 인식). LS 코드 `101V6000`은 미인식 → None → 포지션 심볼 미정규화 → ledger 미스매치.
- **결정:** `BrokerRouter.__init__`에 `dataset_for_code=None` 파라미터 추가. None이면 `quant_core.dataset_for_contract`(현 동작 = KIS 무변경). LS make_broker는 LS-aware 역매핑 주입.
- **왜 core 안 고치나:** scope = 실행계층만, core(engine) byte-identical. dataset_for_contract에 LS 코드 인식을 넣으면 engine이 LS 브로커 코드를 알게 됨(계층 침범). 콜백 주입이 근본적·계층 정합(4원칙 #1).

### 3.2 LsFuturesBroker = LsBroker 인증 재사용
선물 OAuth/토큰/throttle/HTTP는 국내주식과 **완전 동일**(같은 도메인·appkey·tr_cd·블록응답). 중복 구현 금지(4원칙 #3) → `LsFuturesBroker`가 `LsBroker`의 `_token`/`_post`/`_Throttle`을 **재사용**(상속 또는 합성). **결정: 합성** — `LsFuturesBroker.__init__`이 내부 `_http = _LsHttp(creds)`(LsBroker에서 추출한 인증/HTTP 믹스인)를 보유. 단, 선물 자격증명(`load_ls_futures`)으로 별도 계좌. (상속은 LsBroker의 국내주식 조회메서드까지 끌고와 혼란 — 합성이 명시적.)
- **간소화:** 별도 믹스인 추출이 과하면(Over-eng), `LsFuturesBroker`가 `LsBroker`의 `_token`/`_post`/`_headers`/`_Throttle`을 **모듈 함수/클래스로 재사용**(ls_broker.py의 `_Throttle`·`_GLOBAL_THROTTLE`는 이미 모듈 전역). `_token`/`_post`는 LsBroker 메서드라, **`ls_broker.py`에서 인증/HTTP를 `_LsAuth` 베이스 클래스로 추출**하고 LsBroker·LsFuturesBroker 둘 다 상속(D2 Step). KIS `KisBroker`/`KisFuturesBroker`가 토큰 로직을 각자 둔 것보다 DRY.

### 3.3 LS 선물 자격증명 = 별도 슬롯
LS OPEN API는 계좌 단위 신청(선물계좌 별도 가능). KIS `load_kis_futures` 패턴 미러 → `save_ls_futures(app_key, app_secret, account_no, virtual)`. make_broker가 LS 선물 슬롯 존재 시에만 라우터 구성(없으면 LsBroker 단독 = 국내주식만, 무변경).

---

# Tasks

> D1(시암·슬롯)→D2(인증추출+resolver)→D3(account)→D4(시세)→D5(주문)→D6(취소·체결)→D7(router 콜백+통합). 각 Task TDD.
> 테스트: `cd local && PYTHONUTF8=1 python -m pytest tests/<file> -q`. KIS 회귀: `pytest tests/test_kis_futures*.py tests/golden* -q` 항상 green 유지.

---

### Task D1: secrets_store LS 선물 슬롯 + make_broker LS 라우터 분기

**Files:**
- Modify: `local/localapp/secrets_store.py`
- Modify: `local/localapp/runner.py` (`make_broker`)
- Test: `local/tests/test_ls_futures_wiring.py` (create)

- [ ] **Step 1: 실패 테스트** — `test_ls_futures_wiring.py`

```python
"""LS 선물 자격증명 슬롯 + make_broker 라우터 분기 회귀."""
from __future__ import annotations
import sys
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))


def test_save_load_ls_futures(monkeypatch):
    from localapp import secrets_store
    store = {}
    monkeypatch.setattr(secrets_store.keyring, "set_password",
                        lambda svc, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(secrets_store.keyring, "get_password",
                        lambda svc, k: store.get(k))
    secrets_store.save_ls_futures("AK", "SK", "5550-9999", virtual=True)
    c = secrets_store.load_ls_futures()
    assert c["app_key"] == "AK" and c["account_no"] == "5550-9999" and c["virtual"] is True


def test_make_broker_ls_returns_router_when_futures(monkeypatch):
    """LS 활성 + 선물 자격증명 → BrokerRouter(LsBroker, LsFuturesBroker)."""
    from localapp import runner, secrets_store
    monkeypatch.setattr(secrets_store, "get_active_broker", lambda: "ls")
    monkeypatch.setattr(secrets_store, "load_ls", lambda: {"app_key": "x"})
    monkeypatch.setattr(secrets_store, "load_ls_futures", lambda: {"app_key": "f"})
    import localapp.ls_broker as lb
    import localapp.ls_futures_broker as lfb
    monkeypatch.setattr(lb, "LsBroker", lambda: "LS_STOCK")
    monkeypatch.setattr(lfb, "LsFuturesBroker", lambda: "LS_FUT")
    b = runner.make_broker()
    from localapp.broker_router import BrokerRouter
    assert isinstance(b, BrokerRouter)


def test_make_broker_ls_stock_only_when_no_futures(monkeypatch):
    """LS 활성 + 선물 자격증명 없음 → LsBroker 단독(국내주식, 무변경)."""
    from localapp import runner, secrets_store
    monkeypatch.setattr(secrets_store, "get_active_broker", lambda: "ls")
    monkeypatch.setattr(secrets_store, "load_ls", lambda: {"app_key": "x"})
    monkeypatch.setattr(secrets_store, "load_ls_futures", lambda: None)
    import localapp.ls_broker as lb
    monkeypatch.setattr(lb, "LsBroker", lambda: "LS_STOCK")
    assert runner.make_broker() == "LS_STOCK"
```

- [ ] **Step 2: 실패 확인** — Run: `cd local && python -m pytest tests/test_ls_futures_wiring.py -q` → FAIL(`save_ls_futures` 미정의·`ls_futures_broker` 없음).

- [ ] **Step 3: secrets_store 구현** — `_LS` 정의 아래 추가:

```python
_LS_FUT = "ls_futures_credentials"   # LS 선물계좌 (국내선물·해외선물 공통; 별도 계좌)


def save_ls_futures(app_key: str, app_secret: str, account_no: str,
                    virtual: bool = True) -> None:
    """LS 선물(국내·해외) 자격증명. 국내주식과 별도 계좌(LS OPEN API 계좌단위).
    이 정보는 사용자 PC를 떠나지 않는다(서버·리포 전송 금지)."""
    keyring.set_password(KEYRING_SERVICE, _LS_FUT, json.dumps({
        "app_key": app_key, "app_secret": app_secret,
        "account_no": account_no, "virtual": virtual,
    }))


def load_ls_futures() -> dict | None:
    raw = keyring.get_password(KEYRING_SERVICE, _LS_FUT)
    return json.loads(raw) if raw else None
```
그리고 `clear()`의 키 루프에 `_LS_FUT` 추가.

- [ ] **Step 4: make_broker LS 분기 확장** — `runner.make_broker`의 LS 분기를 (현재 `return LsBroker()`) 다음으로:

```python
    if get_active_broker() == "ls":
        if load_ls() is None:
            raise RuntimeError(
                "LS 자격증명이 등록되지 않았습니다. setup에서 LS appkey/secret/계좌를 등록하세요.")
        from .ls_broker import LsBroker
        stock = LsBroker()
        from .secrets_store import load_ls_futures
        if load_ls_futures() is None:
            return stock                         # 국내주식만 — 무변경
        from .ls_futures_broker import LsFuturesBroker
        from .ls_futures_contracts import LsContractResolver
        from .broker_router import BrokerRouter
        from quant_core.futures_contract import dataset_for_contract as _kis_d4c  # 기본(미사용 — LS 콜백 주입)
        r = LsContractResolver(LsFuturesBroker())   # resolver가 선물 토큰으로 master fetch
        return BrokerRouter(stock, r.broker,
                            resolve=r.resolve, resolve_expiry=r.resolve_expiry,
                            dataset_for_code=r.dataset_for_code)
```
> ⚠ D1에선 `ls_futures_broker`·`ls_futures_contracts`·`dataset_for_code`가 아직 없어 import 에러. **D1 Step 1 테스트의 router 케이스는 `LsFuturesBroker`/`LsContractResolver`를 monkeypatch**하므로, D2까지 그 케이스만 xfail 표시 후 D7에서 해제. (나머지 2 케이스는 통과.) — 실제 배선 코드는 D7에서 확정(여기선 슬롯·분기 골격만).

- [ ] **Step 5: 통과 확인** (router 케이스 xfail) — Run: `python -m pytest tests/test_ls_futures_wiring.py -q` → save/load + stock-only PASS.

- [ ] **Step 6: 커밋**
```bash
git add local/localapp/secrets_store.py local/localapp/runner.py local/tests/test_ls_futures_wiring.py
git commit -m "feat(local): LS 선물 자격증명 슬롯 + make_broker 라우터 분기 골격 (Phase D1)"
```

---

### Task D2: 인증/HTTP 베이스 추출 + LsFuturesBroker 스캐폴딩 + LS 계약 resolver

**Files:**
- Modify: `local/localapp/ls_broker.py` (인증/HTTP를 `_LsAuth` 베이스로 추출 — LsBroker 동작 무변경)
- Create: `local/localapp/ls_futures_broker.py` (scaffolding + master fetch)
- Create: `local/localapp/ls_futures_contracts.py` (resolver)
- Test: `local/tests/test_ls_contract_resolver.py` (create)

**근거:** `domestic-futures-research.md` — t8432(마스터: shcode `101V6000`·expcode ISIN·hname "F 2406"), t9943(경량 리스트). 인증은 국내주식과 동일.

- [ ] **Step 1: 인증/HTTP 베이스 추출** — `ls_broker.py`에서 `_token`/`_read_token_cache`/`_headers`/`_post`/`_token_fp`/`__init__`의 creds 적재를 `_LsAuth` 베이스 클래스로 이동. `LsBroker(_LsAuth)`로 상속, **메서드 본문·동작 완전 동일**(byte-identical 회귀 = 기존 LS 테스트 green). `_LsAuth.__init__(creds)`가 creds dict를 받아 key/secret/virtual/account_no/_token_fp 세팅(현 LsBroker.__init__과 동일 로직, `load_ls()` 대신 인자 creds).
  - `LsBroker.__init__`: `super().__init__(load_ls())` (creds None이면 베이스가 RuntimeError).
  - **검증:** `pytest tests/test_ls_token.py tests/test_ls_broker_resp.py -q` → 기존 LS 테스트 전부 green(추출이 동작 무변경 증명).

- [ ] **Step 2: 실패 테스트(resolver)** — `test_ls_contract_resolver.py`

```python
"""LS 선물 계약 resolver — t8432 마스터 파싱→근월물 코드·역매핑."""
from __future__ import annotations
import sys, datetime
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

# t8432 OutBlock 모의 — F 2406(근월) · F 2409 · 스프레드(제외 대상)
_MASTER = [
    {"shcode": "101V6000", "expcode": "KR4101V60002", "hname": "F 202406"},
    {"shcode": "101V9000", "expcode": "KR4101V90009", "hname": "F 202409"},
    {"shcode": "401V6V9SP", "expcode": "KR4401...", "hname": "F SP 06-09"},  # 스프레드 제외
]


def test_resolve_picks_front_month():
    from localapp.ls_futures_contracts import _pick_front_kospi200
    # 2024-05-01 기준 근월 = 202406
    code = _pick_front_kospi200(_MASTER, datetime.date(2024, 5, 1))
    assert code == "101V6000"


def test_resolve_skips_spread_and_expired():
    from localapp.ls_futures_contracts import _pick_front_kospi200
    # 2024-07-01 → 202406 만기경과 → 202409
    code = _pick_front_kospi200(_MASTER, datetime.date(2024, 7, 1))
    assert code == "101V9000"


def test_dataset_for_code_reverse():
    from localapp.ls_futures_contracts import LsContractResolver
    # LS 국내선물 코드(101…) → "코스피200선물"
    assert LsContractResolver.dataset_for_code_static("101V6000") == "코스피200선물"
    assert LsContractResolver.dataset_for_code_static("005930") is None  # 주식
```

- [ ] **Step 3: 실패 확인** — Run: `python -m pytest tests/test_ls_contract_resolver.py -q` → FAIL.

- [ ] **Step 4: LsFuturesBroker 스캐폴딩 + master fetch** — `ls_futures_broker.py`:

```python
"""LS증권 선물 브로커 — 국내선물(Phase D). 해외선물 메서드는 Phase F에서 추가.

LsBroker와 동일 인증/HTTP(_LsAuth 상속), 선물 TR(/futureoption/*)만 매핑.
자격증명은 load_ls_futures(별도 선물계좌). docs/ls-api/domestic-futures-research.md 정본.
"""
from __future__ import annotations
import logging
from .ls_broker import _LsAuth, normalize_ls_order_resp, canonical_odno
from .secrets_store import load_ls_futures

log = logging.getLogger("localapp.ls_futures_broker")


class LsFuturesBroker(_LsAuth):
    def __init__(self):
        creds = load_ls_futures()
        if not creds:
            raise RuntimeError("LS 선물 자격증명이 없습니다. setup에서 등록하세요.")
        super().__init__(creds)

    # 라우터가 미구성 컨텍스트 skip 판단 — 국내선물 자격증명 있으면 domestic 구성됨.
    @property
    def domestic_configured(self) -> bool:
        return True

    @property
    def overseas_configured(self) -> bool:
        return False   # Phase F에서 해외선물 자격증명 분기

    def index_futures_master(self) -> list[dict]:
        """t8432 지수선물 마스터 — shcode/expcode/hname. resolver가 1일 캐시.
        ⚠ research G-DF5: 단축코드 cipher 런타임 해석(t8432가 근본해결)."""
        body = self._post("/futureoption/market-data", "t8432", {"t8432InBlock": {"gubun": "0"}})
        return body.get("t8432OutBlock") or []
```

- [ ] **Step 5: LsContractResolver 구현** — `ls_futures_contracts.py`:

```python
"""데이터셋 심볼(한글)↔LS 선물 계약코드(101V6000). t8432 마스터 1일 캐시.

KIS ContractResolver 대칭이나 마스터 소스가 KIS 정적파일이 아니라 LS API(t8432)다.
근월물 선택은 hname의 YYYYMM 파싱(roll lead 적용). BrokerRouter에 resolve/dataset_for_code 주입.
"""
from __future__ import annotations
import datetime, re
import quant_core as qc
from quant_core.futures_contract import instrument_spec, roll_lead_days

_KOSPI200 = "코스피200선물"
_HNAME_YM = re.compile(r"(\d{4})(\d{2})")   # "F 202406" → (2024, 06)


def _pick_front_kospi200(master: list[dict], today: datetime.date) -> str | None:
    """t8432 마스터에서 KOSPI200 근월물 shcode. 스프레드(SP)·만기경과 제외, roll lead 반영."""
    lead = roll_lead_days(instrument_spec(_KOSPI200).default_roll)
    cands = []
    for row in master:
        h = str(row.get("hname") or "")
        sh = str(row.get("shcode") or "")
        if "SP" in h or not sh.startswith("101"):   # 스프레드·비KOSPI200 정규선물 제외
            continue
        m = _HNAME_YM.search(h)
        if not m:
            continue
        y, mo = int(m.group(1)), int(m.group(2))
        # 만기 ≈ 2번째 목요일. lead 이전이면 다음 월물로 롤.
        exp = _second_thursday(y, mo)
        if exp - datetime.timedelta(days=lead) >= today:
            cands.append((exp, sh))
    cands.sort()
    return cands[0][1] if cands else None


def _second_thursday(y: int, m: int) -> datetime.date:
    d = datetime.date(y, m, 1)
    first_thu = d + datetime.timedelta(days=(3 - d.weekday()) % 7)
    return first_thu + datetime.timedelta(days=7)


class LsContractResolver:
    """심볼→LS 계약코드(101V6000). 마스터 1일 캐시(선물 브로커 토큰으로 t8432 fetch)."""

    def __init__(self, futures_broker):
        self.broker = futures_broker
        self._master: list[dict] | None = None
        self._fetched: datetime.date | None = None

    def _ensure(self, today: datetime.date) -> None:
        if self._fetched == today:
            return
        try:
            self._master = self.broker.index_futures_master()
        except Exception:                     # 다운로드 실패는 None → resolve None → 발주 skip
            self._master = None
        self._fetched = today

    def resolve(self, symbol: str) -> str | None:
        if not qc.is_futures(symbol):
            return symbol                     # 주식은 심볼 그대로
        today = datetime.date.today()
        self._ensure(today)
        if not self._master:
            return None
        if symbol == _KOSPI200:
            return _pick_front_kospi200(self._master, today)
        return None                           # 국내선물=KOSPI200 only(Phase D)

    def resolve_expiry(self, symbol: str):
        """(계약코드, 만기일). 만기 자동청산 ledger 기록용. 미해석 → (None,None)."""
        code = self.resolve(symbol)
        if not code or symbol != _KOSPI200:
            return None, None
        m = _HNAME_YM.search(next((r["hname"] for r in (self._master or [])
                                   if r.get("shcode") == code), "") or "")
        if not m:
            return code, None
        return code, _second_thursday(int(m.group(1)), int(m.group(2)))

    @staticmethod
    def dataset_for_code_static(code: str) -> str | None:
        """LS 계약코드 → 데이터셋 심볼(역매핑). 국내선물 101… → 코스피200선물. 주식/미등록 → None."""
        if code and code.startswith("101"):
            return _KOSPI200
        return None

    def dataset_for_code(self, code: str) -> str | None:
        return self.dataset_for_code_static(code)
```
> `roll_lead_days`·`instrument_spec`이 quant_core에 있는지 확인(현 ContractResolver가 사용). 없으면 import 경로를 `futures_contract`에서 grep. `_second_thursday`는 core에도 있으나 import 대신 로컬 복제(작은 순수함수, 결합 회피).

- [ ] **Step 6: 통과 확인** — Run: `python -m pytest tests/test_ls_contract_resolver.py -q` → PASS(3).

- [ ] **Step 7: 커밋**
```bash
git add local/localapp/ls_broker.py local/localapp/ls_futures_broker.py local/localapp/ls_futures_contracts.py local/tests/test_ls_contract_resolver.py
git commit -m "feat(local): LS 인증 베이스 추출 + LsFuturesBroker 스캐폴딩 + LS 계약 resolver (Phase D2)"
```

---

### Task D3: account_snapshot (CFOAQ50600 + t0441)

**Files:** Modify `ls_futures_broker.py` · Test `local/tests/test_ls_futures_resp.py` (create)

**근거:** research §Broker 매핑 — account_snapshot = CFOAQ50600(account: equity=`EvalDpsamtTotamt`·order_cash=`MnyOrdAbleAmt`·margin=`CsgnMgnTotamt`·eval_pnl=`FutsEvalPnlAmt`) + t0441(positions: symbol=`expcode`·side=`medosu`·qty=`jqty`·avg=`pamt`·eval_pnl=`dtsunik1`). **반환 = `{"account": {...}, "positions": [...]}`** (KIS 선물 account_snapshot 동형 — BrokerRouter가 account.equity/order_cash 사용).

- [ ] **Step 1: 실패 테스트** — `test_ls_futures_resp.py`

```python
"""LsFuturesBroker 응답 정규화 전수 — account 2-TR·order normalize·t0434 status·pending 필터.
⚠ fixture는 research 기반. Phase D 모의 E2E 후 실측 교체."""
from __future__ import annotations
import sys
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))
from localapp import ls_futures_broker as lfb


def _broker():
    b = object.__new__(lfb.LsFuturesBroker)
    return b


def test_account_snapshot_merges_two_trs(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_acct_summary_raw", lambda: {
        "CFOAQ50600OutBlock2": {"EvalDpsamtTotamt": "50000000", "MnyOrdAbleAmt": "30000000",
                                "CsgnMgnTotamt": "8000000", "FutsEvalPnlAmt": "120000"}}, raising=False)
    monkeypatch.setattr(b, "_positions_raw", lambda: {
        "t0441OutBlock1": [{"expcode": "101V6000", "medosu": "매수", "jqty": "2",
                            "pamt": "342.25", "price": "343.10", "dtsunik1": "120000"}]}, raising=False)
    snap = b.account_snapshot()
    assert snap["account"]["equity"] == 50000000
    assert snap["account"]["order_cash"] == 30000000
    pos = snap["positions"][0]
    assert pos["symbol"] == "101V6000" and pos["side"] == "long" and pos["qty"] == 2


def test_account_snapshot_raises_on_partial_failure(monkeypatch):
    """2-TR 중 하나 실패 → raise(라우터가 fetch_failed 표식; 0 위장 금지)."""
    import pytest
    b = _broker()
    monkeypatch.setattr(b, "_acct_summary_raw", lambda: (_ for _ in ()).throw(RuntimeError("5xx")), raising=False)
    monkeypatch.setattr(b, "_positions_raw", lambda: {"t0441OutBlock1": []}, raising=False)
    with pytest.raises(Exception):
        b.account_snapshot()
```

- [ ] **Step 2: 실패 확인** — `python -m pytest tests/test_ls_futures_resp.py -q` → FAIL.

- [ ] **Step 3: 구현** — `ls_futures_broker.py`에 추가:

```python
    def _acct_summary_raw(self) -> dict:
        return self._post("/futureoption/accno", "CFOAQ50600",
                          {"CFOAQ50600InBlock1": {"RecCnt": 1, "BalEvalTp": "1",
                                                  "FutsPrcEvalTp": "1", "LqtQtyQryTp": "1"}})

    def _positions_raw(self) -> dict:
        return self._post("/futureoption/accno", "t0441",
                          {"t0441InBlock": {"cts_expcode": "", "cts_medocd": ""}})

    def account_snapshot(self) -> dict:
        """국내선물 잔고 — {account, positions}. 2-TR 중 실패는 raise(라우터가 fetch_failed)."""
        summary = (self._acct_summary_raw().get("CFOAQ50600OutBlock2") or {})
        account = {
            "equity": int(float(summary.get("EvalDpsamtTotamt") or 0)),       # 추정예탁자산(킬스위치)
            "order_cash": int(float(summary.get("MnyOrdAbleAmt") or 0)),      # 현금주문가능(사이징)
            "margin_total": int(float(summary.get("CsgnMgnTotamt") or 0)),
            "eval_pnl": int(float(summary.get("FutsEvalPnlAmt") or 0)),
            "currency": "KRW",
        }
        positions = []
        for it in (self._positions_raw().get("t0441OutBlock1") or []):
            qty = int(float(it.get("jqty") or 0))
            if qty == 0:
                continue
            positions.append({
                "symbol": str(it.get("expcode", "")).strip(),
                "side": "long" if str(it.get("medosu") or "") == "매수" else "short",
                "qty": qty,
                "avg_price": float(it.get("pamt") or 0),
                "eval_price": float(it.get("price") or 0),
                "eval_pnl": float(it.get("dtsunik1") or 0),
                "market": "DOMESTIC", "currency": "KRW", "asset_class": "futures",
            })
        return {"account": account, "positions": positions}
```

- [ ] **Step 4: 통과 + 커밋**
```bash
git add local/localapp/ls_futures_broker.py local/tests/test_ls_futures_resp.py
git commit -m "feat(local): LsFuturesBroker account_snapshot 2-TR (CFOAQ50600+t0441) (Phase D3)"
```

---

### Task D4: price / today_open (t2101)

**Files:** Modify `ls_futures_broker.py` · Test append `test_ls_futures_resp.py`

**근거:** research — t2101 `price`(현재가)·`open`(시가)·`jnilclose`(전일정산). InBlock 키 `focode`(8자).

- [ ] **Step 1: 테스트** (append)
```python
def test_price_and_open(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_quote_raw", lambda sym: {
        "t2101OutBlock": {"price": "343.10", "open": "342.00", "jnilclose": "341.50"}}, raising=False)
    assert b.price("101V6000") == 343.10
    assert b.today_open("101V6000") == 342.00
```
- [ ] **Step 2: 실패 확인.**
- [ ] **Step 3: 구현**
```python
    def _quote_raw(self, symbol: str) -> dict:
        return self._post("/futureoption/market-data", "t2101",
                          {"t2101InBlock": {"focode": symbol}})

    def price(self, symbol: str) -> float:
        return float((self._quote_raw(symbol).get("t2101OutBlock") or {}).get("price") or 0)

    def today_open(self, symbol: str) -> float:
        try:
            v = (self._quote_raw(symbol).get("t2101OutBlock") or {}).get("open")
            return float(v) if v not in (None, "", 0, "0") else 0.0
        except Exception:
            return 0.0
```
- [ ] **Step 4: 통과 + 커밋** `feat(local): LsFuturesBroker price/today_open (t2101) (Phase D4)`

---

### Task D5: buy/sell/limit (CFOAT00100, 롱숏 BnsTpCode) + normalize

**Files:** Modify `ls_futures_broker.py` · Test append

**근거:** research — CFOAT00100: `FnoIsuNo`·`BnsTpCode`(1매도/2매수)·`FnoOrdprcPtnCode`(00지정/03시장)·`FnoOrdPrc`(double 포인트)·`OrdQty`. 성공 = OutBlock2 `OrdNo` 존재. 레슨 #1·#6.

- [ ] **Step 1: 테스트** (append)
```python
def test_buy_market_normalizes_ordno(monkeypatch):
    b = _broker()
    captured = {}
    def _post(path, tr, body, **k):
        captured["body"] = body["CFOAT00601InBlock1"] if "CFOAT00601InBlock1" in body else body["CFOAT00100InBlock1"]
        return {"rsp_cd": "00040", "rsp_msg": "정상", "CFOAT00100OutBlock2": {"OrdNo": "777"}}
    monkeypatch.setattr(b, "_post", _post, raising=False)
    r = b.buy("101V6000", 1)
    assert r == {"success": True, "order_no": "777", "message": "정상", "msg_cd": "00040"}
    assert captured["body"]["BnsTpCode"] == "2" and captured["body"]["FnoOrdprcPtnCode"] == "03"

def test_sell_uses_bnstp_1(monkeypatch):
    b = _broker()
    captured = {}
    monkeypatch.setattr(b, "_post",
        lambda p, t, body, **k: (captured.update(b=body["CFOAT00100InBlock1"]),
                                 {"CFOAT00100OutBlock2": {"OrdNo": "8"}})[1], raising=False)
    b.sell("101V6000", 1)
    assert captured["b"]["BnsTpCode"] == "1"
```
- [ ] **Step 2: 실패 확인.**
- [ ] **Step 3: 구현**
```python
    def _submit(self, symbol, qty, side, ord_ptn, unit_price):
        bns = "2" if side == "buy" else "1"          # 레슨 #6: 롱숏 net via BnsTpCode
        prc = float(unit_price) if ord_ptn == "00" else 0
        resp = self._post("/futureoption/order", "CFOAT00100",
                          {"CFOAT00100InBlock1": {
                              "FnoIsuNo": symbol, "BnsTpCode": bns,
                              "FnoOrdprcPtnCode": ord_ptn, "FnoOrdPrc": prc, "OrdQty": qty}},
                          is_order=True)
        return normalize_ls_order_resp(resp, ordno_field="OrdNo")

    def buy(self, symbol, qty): return self._submit(symbol, qty, "buy", "03", 0)
    def sell(self, symbol, qty): return self._submit(symbol, qty, "sell", "03", 0)
    def buy_limit(self, symbol, qty, limit_price): return self._submit(symbol, qty, "buy", "00", float(limit_price))
    def sell_limit(self, symbol, qty, limit_price): return self._submit(symbol, qty, "sell", "00", float(limit_price))

    def buy_resv_limit(self, *a, **k):
        raise NotImplementedError("국내선물 예약주문 미지원")
    def sell_resv_limit(self, *a, **k):
        raise NotImplementedError("국내선물 예약주문 미지원")
```
> ⚠ `FnoOrdPrc`는 **double 포인트값**(342.25). int 절삭 금지(research). `normalize_ls_order_resp`는 ls_broker.py 재사용(OrdNo 기준 — 선물 rsp_cd 가변과 무관).
- [ ] **Step 4: 통과 + 커밋** `feat(local): LsFuturesBroker 주문 CFOAT00100 롱숏 + normalize (Phase D5)`

---

### Task D6: cancel (CFOAT00300) + order_status/pending_orders (t0434) + orderable_qty

**Files:** Modify `ls_futures_broker.py` · Test append

**근거:** research — cancel CFOAT00300(`OrgOrdNo`+`CancQty`, **원주문일자 불요**). t0434: `chegb="0"`(전체) order_status, `chegb="2"`(미체결) pending; `orgordno≠0` 제외(레슨 #7); fill_price=`cheprice`; status 문자열. orderable=CFOAQ10100 `NewOrdAbleQty`.

- [ ] **Step 1: 테스트** (append — order_status filled/pending 필터 + cancel)
```python
def test_order_status_filled_from_t0434(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_ccld_raw", lambda chegb: {"t0434OutBlock1": [
        {"ordno": "777", "orgordno": "0", "qty": "1", "cheqty": "1", "ordrem": "0",
         "cheprice": "343.10", "status": "완료"}]}, raising=False)
    st = b.order_status("777", symbol="101V6000")
    assert st["status"] == "filled" and st["filled_qty"] == 1 and st["fill_price"] == 343.10

def test_pending_excludes_modify_cancel_rows(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_ccld_raw", lambda chegb: {"t0434OutBlock1": [
        {"ordno": "10", "orgordno": "0", "qty": "2", "cheqty": "0", "ordrem": "2", "medosu": "매수", "price": "342.0"},
        {"ordno": "11", "orgordno": "10", "qty": "2", "cheqty": "0", "ordrem": "2", "medosu": "매수", "price": "342.0"}]}, raising=False)
    pend = b.pending_orders()
    assert len(pend) == 1 and pend[0]["order_no"] == "10"   # orgordno≠0(정정/취소) 제외

def test_cancel_normalizes(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_post",
        lambda p, t, body, **k: {"CFOAT00300OutBlock2": {"OrdNo": "99"}}, raising=False)
    r = b.cancel("10", "101V6000", 2)
    assert r["success"] is True
```
- [ ] **Step 2: 실패 확인.**
- [ ] **Step 3: 구현**
```python
    def cancel(self, order_no, symbol, qty):
        resp = self._post("/futureoption/order", "CFOAT00300",
                          {"CFOAT00300InBlock1": {"OrgOrdNo": int(order_no) if str(order_no).isdigit() else order_no,
                                                  "FnoIsuNo": symbol, "CancQty": qty}}, is_order=True)
        r = normalize_ls_order_resp(resp, ordno_field="OrdNo")
        return {"success": r["success"], "message": r["message"], "msg_cd": r["msg_cd"]}

    def _ccld_raw(self, chegb: str) -> dict:
        return self._post("/futureoption/accno", "t0434",
                          {"t0434InBlock": {"expcode": "", "chegb": chegb, "sortgb": "1", "cts_ordno": ""}})

    def order_status(self, order_no, symbol=None, hint=None):
        try:
            rows = self._ccld_raw("0").get("t0434OutBlock1") or []     # 레슨 #3: 전체조회
        except Exception as e:
            log.warning("LS선물 order_status 실패 [%s]: %s", order_no, e)
            return {"order_no": order_no, "status": "unknown", "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}
        for row in rows:
            if canonical_odno(row.get("ordno")) != canonical_odno(order_no):
                continue
            orig = int(float(row.get("qty") or 0)); che = int(float(row.get("cheqty") or 0))
            rem = int(float(row.get("ordrem") or 0)); status = str(row.get("status") or "")
            # ⚠ G-DF3: status 문자열 실측 전 — cheqty/ordrem 우선, status는 취소/거부 보조판정.
            if "취소" in status: st = "cancelled"
            elif rem == 0 and che > 0: st = "filled"
            elif che > 0: st = "partial"
            else: st = "submitted"
            return {"order_no": order_no, "status": st, "filled_qty": che, "remain_qty": rem,
                    "fill_price": float(row.get("cheprice") or row.get("price") or 0)}
        return {"order_no": order_no, "status": "unknown", "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}

    def pending_orders(self):
        try:
            rows = self._ccld_raw("2").get("t0434OutBlock1") or []
        except Exception as e:
            log.warning("LS선물 pending 실패: %s", e); return []
        out = []
        for row in rows:
            if str(row.get("orgordno") or "0") not in ("0", "", "00000000000"):   # 레슨 #7
                continue
            rem = int(float(row.get("ordrem") or 0))
            if rem <= 0: continue
            out.append({"order_no": str(row.get("ordno") or ""), "symbol": str(row.get("expcode") or "").strip(),
                        "side": "buy" if str(row.get("medosu") or "") == "매수" else "sell",
                        "qty": int(float(row.get("qty") or 0)), "remain_qty": rem,
                        "limit_price": float(row.get("price") or 0),
                        "market": "DOMESTIC", "currency": "KRW", "asset_class": "futures"})
        return out

    def orderable_qty(self, symbol, price, side="buy"):
        body = self._post("/futureoption/accno", "CFOAQ10100",
                          {"CFOAQ10100InBlock1": {"RecCnt": 1, "QryTp": "1", "FnoIsuNo": symbol,
                                                  "BnsTpCode": "2" if side == "buy" else "1",
                                                  "FnoOrdPrc": float(price), "FnoOrdprcPtnCode": "00",
                                                  "OrdAmt": 0, "RatVal": 0}})
        return int(float((body.get("CFOAQ10100OutBlock2") or {}).get("NewOrdAbleQty") or 0))
```
- [ ] **Step 4: 통과 + 커밋** `feat(local): LsFuturesBroker 취소·체결조회(t0434 status)·orderable (Phase D6)`

---

### Task D7: BrokerRouter dataset_for_code 콜백 + LS 통합 + KIS byte-identical 회귀

**Files:** Modify `broker_router.py` · Test `local/tests/test_broker_router_dataset_cb.py`(create) · D1 xfail 해제

- [ ] **Step 1: 실패 테스트** — `test_broker_router_dataset_cb.py`
```python
"""BrokerRouter dataset_for_code 콜백 — 기본=quant_core(KIS 무변경), 주입=LS-aware."""
from __future__ import annotations
import sys
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))
from localapp.broker_router import BrokerRouter


class _Stock:
    def account_snapshot(self, overseas=True):
        return {"balance": {"cash": 100}, "positions": []}
class _Fut:
    domestic_configured = True
    overseas_configured = False
    def account_snapshot(self):
        return {"account": {"equity": 5000000}, "positions": [{"symbol": "101V6000", "side": "long", "qty": 1}]}


def test_default_dataset_for_code_is_kis(monkeypatch):
    # 기본 콜백(quant_core) — LS 코드 101V6000은 KIS 매핑서 None → 심볼 미정규화(현 동작)
    r = BrokerRouter(_Stock(), _Fut(), resolve=lambda s: s)
    pos = r.account_snapshot()["positions"][0]
    assert pos["symbol"] == "101V6000"   # KIS 콜백은 LS코드 모름 → 원본 유지

def test_injected_dataset_for_code_normalizes(monkeypatch):
    r = BrokerRouter(_Stock(), _Fut(), resolve=lambda s: s,
                     dataset_for_code=lambda c: "코스피200선물" if c.startswith("101") else None)
    pos = r.account_snapshot()["positions"][0]
    assert pos["symbol"] == "코스피200선물" and pos["contract_code"] == "101V6000"
```
- [ ] **Step 2: 실패 확인** — FAIL(`dataset_for_code` 파라미터 없음).
- [ ] **Step 3: 구현** — `broker_router.py`:
  - `__init__` 시그니처에 `dataset_for_code=None` 추가. 본문: `from quant_core.futures_contract import dataset_for_contract; self._d4c = dataset_for_code or dataset_for_contract`.
  - `account_snapshot`의 `ds = dataset_for_contract(code)` → `ds = self._d4c(code)`.
  - (다른 모든 동작 무변경 — KIS는 dataset_for_code 미주입 → quant_core 기본 → byte-identical.)
- [ ] **Step 4: 통과 확인** — `python -m pytest tests/test_broker_router_dataset_cb.py -q` → PASS(2).
- [ ] **Step 5: D1 router 케이스 xfail 해제 + make_broker 실배선 확정** — D1 Step 4의 make_broker LS 라우터 분기에서 `dataset_for_code=r.dataset_for_code` 전달 확인. D1 `test_make_broker_ls_returns_router_when_futures` xfail 제거.
- [ ] **Step 6: KIS byte-identical 회귀 (필수)** — Run:
```bash
cd local && PYTHONUTF8=1 python -m pytest tests/test_kis_futures_resp.py tests/test_futures_equity.py tests/test_broker_parity.py -q
cd .. && PYTHONUTF8=1 python -m pytest tests/golden_backtest.py -q
```
Expected: 전부 PASS(KIS 선물·골든 무변경). `git diff origin/main -- local/localapp/kis_futures_broker.py local/localapp/kis_broker.py` = 비어있음 확인.
- [ ] **Step 7: 전체 LS 테스트 + 커밋**
```bash
cd local && PYTHONUTF8=1 python -m pytest tests/test_ls_futures_resp.py tests/test_ls_contract_resolver.py tests/test_ls_futures_wiring.py tests/test_broker_router_dataset_cb.py tests/test_ls_broker_resp.py tests/test_ls_token.py -q
git add local/localapp/broker_router.py local/tests/test_broker_router_dataset_cb.py local/tests/test_ls_futures_wiring.py
git commit -m "feat(local): BrokerRouter dataset_for_code 콜백 + LS 선물 통합 배선 (Phase D7, KIS byte-identical)"
```

---

## Phase D-C — 모의키 라이브 확정 (코드 아님, 일괄 모의 테스트 시)

키 발급 후 `verify_ls.py` 선물 확장으로 실측 확정(research G-DF1~8): t2101 시세 → CFOAQ50600/t0441 잔고 → CFOAT00100 1계약 매수 → t0434 status 실측(filled/cancelled 구분 문자열 G-DF3) → 청산. 단위테스트 fixture를 실측으로 교체. 모의 E2E(진입→체결→ledger→만기/청산) 통과 후 "국내선물 LS 지원 완료".

---

## Self-Review

**1. 스펙 커버리지:** research §Broker 매핑 8메서드(account_snapshot·price·today_open·buy/sell/limit·cancel·order_status·pending_orders·orderable_qty) 전부 Task로 매핑(D3~D6). 시암(D1)·resolver(D2)·router(D7) 포함. ✅
**2. Placeholder 스캔:** 미검증 필드는 research 기반 fixture + `⚠ G-DF` 표식(Phase D-C 확정 절차 명시 — placeholder 아님). ✅
**3. 타입 일관성:** account_snapshot `{account:{equity,order_cash,...}, positions:[{symbol,side,qty,...}]}` = KIS 선물·BrokerRouter 기대와 일치. `normalize_ls_order_resp(raw, ordno_field=)`·`canonical_odno` = ls_broker.py 재사용 시그니처 일치. `dataset_for_code(code)->str|None` = router 콜백 일치. ✅
**4. KIS/core 무변경:** BrokerRouter dataset_for_code 기본=quant_core(KIS 미주입→byte-identical), kis_futures_broker.py·core 무수정. D7 Step 6 회귀로 검증. ✅

## Execution Handoff
계획 저장: `docs/superpowers/plans/2026-06-22-ls-phase-d-domestic-futures.md`. subagent-driven-development로 D1→D7 task-by-task(각 2단계 리뷰·KIS 회귀 게이트). Phase E(해외주식)·F(해외선물)는 D 완료 후 별도 plan.

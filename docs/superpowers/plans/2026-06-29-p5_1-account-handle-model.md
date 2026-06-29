# P5-1 — 계좌 핸들 모델 + 보고 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (또는 executing-plans).
> 스텝은 `- [ ]` 체크박스. 상위 spec: [account-linked-strategy](../specs/2026-06-29-account-linked-strategy-and-fund-transparency-design.md) §3.1·§3.3·§4. P5의 foundation(P5-3 가드·P5-4 웹이 이 위에 선다).

**Goal:** 등록된 자격증명 슬롯마다 **비민감 계좌 핸들**(opaque account_id + 별명 + 브로커 + 자산군 + 모드)을
만들고, (broker, 계좌식별자, mode)가 바뀌면 account_id를 **자동 회전**시켜 snapshot으로 서버에 보고한다.

**Architecture:** 새 모듈 `account_handle.py`가 슬롯을 스캔해 핸들을 만든다. account_id는 **로컬 생성 랜덤
uuid**(서버엔 이것만 — 계좌번호·키 미노출). 슬롯의 **fingerprint**(KIS=`account_no+mode`, LS=`appkey+mode`)를
keyring에 함께 저장해, fingerprint가 바뀌면(모의→실전 재등록 등) account_id를 새로 발급 → 옛 핸들에 묶인
전략이 자동 무력화(C7 차단의 식별자 토대). `analytics.local_health()`가 핸들 목록 + 활성 핸들 집합을 보고.

**Tech Stack:** Python(localapp), `keyring`, `uuid`, `hashlib`, `pytest`. 기존 `secrets_store.py`·`coverage.py`
재사용. INV-SEC: 서버로는 uuid·별명·브로커·자산군·모드 불리언만(키·계좌번호 0).

**불변식:** INV-SEC(§4) — 핸들 출력 어디에도 app_key/secret/account_no **값** 미포함(uuid·fingerprint는 로컬
또는 단방향). INV-KIS — KIS 단일 슬롯 사용자는 핸들 1개·기존 동작 무변경(핸들은 *추가* 보고 표면).

---

## Task 1: account_handle 모듈 — fingerprint + account_id 회전 (순수·TDD)

**Files:**
- Create: `local/localapp/account_handle.py`
- Test: `local/tests/test_account_handle.py`

핵심: 슬롯 자격증명 dict → fingerprint(브로커별) → account_id(uuid, fingerprint별 안정·변경 시 회전).

- [ ] **Step 1: 실패 테스트 작성**

`local/tests/test_account_handle.py`:
```python
"""account_handle — fingerprint·account_id 회전 단위 검증 (keyring 무의존: 매핑 store를 주입)."""
from localapp import account_handle as ah


def test_fingerprint_kis_uses_account_and_mode():
    # KIS: 계좌번호+mode가 식별자 (appkey 무관)
    f1 = ah.fingerprint("kis", {"account_no": "12345678-01", "virtual": True})
    f2 = ah.fingerprint("kis", {"account_no": "1234567801", "virtual": True})  # 하이픈만 차이
    f3 = ah.fingerprint("kis", {"account_no": "12345678-01", "virtual": False})  # mode 차이
    assert f1 == f2          # 하이픈 정규화 → 동일
    assert f1 != f3          # 모의↔실전 = 다른 fingerprint


def test_fingerprint_ls_uses_appkey_and_mode():
    # LS: appkey=계좌단위 → appkey+mode가 식별자 (account_no cosmetic, 무시)
    f1 = ah.fingerprint("ls", {"app_key": "AK", "account_no": "11", "virtual": True})
    f2 = ah.fingerprint("ls", {"app_key": "AK", "account_no": "99", "virtual": True})  # 계좌만 차이
    f3 = ah.fingerprint("ls", {"app_key": "BK", "account_no": "11", "virtual": True})  # appkey 차이
    assert f1 == f2          # LS는 계좌번호 무시 → 동일
    assert f1 != f3          # appkey 다르면 다른 계좌


def test_account_id_stable_then_rotates_on_fingerprint_change():
    store = {}  # 주입형 매핑 store (keyring 대체)
    id1 = ah.resolve_account_id("kis_credentials", "FP_A", store)
    id2 = ah.resolve_account_id("kis_credentials", "FP_A", store)  # 동일 fingerprint
    id3 = ah.resolve_account_id("kis_credentials", "FP_B", store)  # 변경(모의→실전 등)
    assert id1 == id2        # 안정(재시작·재호출에도 동일)
    assert id3 != id1        # fingerprint 변경 → 새 uuid 회전
    assert len(id1) >= 16    # opaque uuid
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd local && python -m pytest tests/test_account_handle.py -v`
Expected: FAIL (`ModuleNotFoundError: localapp.account_handle`).

- [ ] **Step 3: 최소 구현**

`local/localapp/account_handle.py`:
```python
"""계좌 핸들 — 슬롯 자격증명을 비민감 핸들(opaque account_id + 메타)로.

account_id는 로컬 랜덤 uuid(서버엔 이것만). fingerprint(KIS=계좌번호+mode, LS=appkey+mode)별로
안정 — fingerprint가 바뀌면(모의→실전 재등록 등) 새 uuid로 회전해 옛 핸들 바인딩을 자동 무력화한다.
INV-SEC: app_key/secret/account_no 값은 핸들에 미포함(fingerprint는 단방향 해시, 로컬 store에만).
"""
from __future__ import annotations
import hashlib
import json
import uuid

import keyring

from .config import KEYRING_SERVICE

_HANDLE_MAP = "account_handles"   # keyring: {slot_key: {"account_id":..., "fingerprint":..., "nickname":...}}


def fingerprint(broker: str, creds: dict) -> str:
    """슬롯의 안정 식별자(단방향). KIS=계좌번호+mode, LS=appkey+mode(계좌번호 cosmetic)."""
    mode = "v" if creds.get("virtual", True) else "r"
    if broker == "ls":
        ident = str(creds.get("app_key", ""))        # appkey=계좌단위
    else:
        ident = str(creds.get("account_no", "")).replace("-", "").strip()  # KIS=계좌번호
    return hashlib.sha256(f"{broker}|{ident}|{mode}".encode()).hexdigest()[:24]


def resolve_account_id(slot_key: str, fp: str, store: dict) -> str:
    """slot_key의 account_id를 store에서 가져오되, fingerprint가 바뀌었으면 새 uuid 발급."""
    ent = store.get(slot_key)
    if ent and ent.get("fingerprint") == fp:
        return ent["account_id"]
    new_id = uuid.uuid4().hex
    store[slot_key] = {"account_id": new_id, "fingerprint": fp,
                       "nickname": (ent or {}).get("nickname", "")}
    return new_id


def _load_map() -> dict:
    raw = keyring.get_password(KEYRING_SERVICE, _HANDLE_MAP)
    return json.loads(raw) if raw else {}


def _save_map(m: dict) -> None:
    keyring.set_password(KEYRING_SERVICE, _HANDLE_MAP, json.dumps(m))
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd local && python -m pytest tests/test_account_handle.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: 커밋**

```bash
git add local/localapp/account_handle.py local/tests/test_account_handle.py
git commit -m "feat(local): account_handle — fingerprint + account_id 회전 (P5-1)"
```

---

## Task 2: current_handles / active_account_ids — 슬롯 스캔 → 핸들 목록 (TDD)

**Files:**
- Modify: `local/localapp/account_handle.py`
- Test: `local/tests/test_account_handle.py` (케이스 추가)

등록된 슬롯을 스캔해 핸들 목록을 만들고, 활성 브로커의 핸들 id 집합을 돌려준다. 슬롯→자산군 매핑은
기존 `coverage.covered_categories` 개념 재사용(중복 정의 금지).

- [ ] **Step 1: 실패 테스트 작성 (추가)**

```python
def test_current_handles_lists_registered_slots(monkeypatch):
    from localapp import account_handle as ah
    # 슬롯 로더 스텁: KIS 선물만 등록(모의)
    monkeypatch.setattr(ah, "_slot_creds", lambda: {
        "kis_futures_credentials": ("kis", "kr_futures",
                                    {"account_no": "12345678-03", "virtual": True}),
    })
    monkeypatch.setattr(ah, "_load_map", lambda: {})
    saved = {}
    monkeypatch.setattr(ah, "_save_map", lambda m: saved.update(m))
    hs = ah.current_handles()
    assert len(hs) == 1
    h = hs[0]
    assert h["broker"] == "kis" and h["mode"] == "paper"
    assert "kr_futures" in h["asset_classes"]
    assert h["account_id"] and "account_no" not in h and "app_key" not in h  # INV-SEC
    assert h["nickname"]                                                     # 자동 라벨 ≥1


def test_active_account_ids_for_active_broker(monkeypatch):
    from localapp import account_handle as ah
    monkeypatch.setattr(ah, "_slot_creds", lambda: {
        "kis_credentials": ("kis", "kr_equity", {"account_no": "11111111-01", "virtual": False}),
        "ls_futures_credentials": ("ls", "kr_futures", {"app_key": "AK", "account_no": "x", "virtual": True}),
    })
    monkeypatch.setattr(ah, "_load_map", lambda: {})
    monkeypatch.setattr(ah, "_save_map", lambda m: None)
    monkeypatch.setattr(ah, "get_active_broker", lambda: "kis")
    ids = ah.active_account_ids()
    handles = {h["account_id"] for h in ah.current_handles() if h["broker"] == "kis"}
    assert set(ids) == handles            # 활성 브로커(kis) 핸들만
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd local && python -m pytest tests/test_account_handle.py -v`
Expected: FAIL (`_slot_creds`/`current_handles`/`active_account_ids` 없음).

- [ ] **Step 3: 구현 (account_handle.py에 추가)**

```python
from .secrets_store import (get_active_broker, load_kis, load_kis_futures,
                            load_kis_overseas_futures, load_ls, load_ls_futures,
                            load_ls_overseas_futures)

# slot_key → (broker, asset_class, loader)
_SLOTS = [
    ("kis_credentials",                   "kis", "kr_equity",        load_kis),
    ("kis_futures_credentials",           "kis", "kr_futures",       load_kis_futures),
    ("kis_overseas_futures_credentials",  "kis", "us_futures",       load_kis_overseas_futures),
    ("ls_credentials",                    "ls",  "kr_equity",        load_ls),
    ("ls_futures_credentials",            "ls",  "kr_futures",       load_ls_futures),
    ("ls_overseas_futures_credentials",   "ls",  "us_futures",       load_ls_overseas_futures),
]

_LABEL = {"kr_equity": "국내주식", "kr_futures": "국내선물", "us_futures": "해외선물"}


def _slot_creds() -> dict:
    """등록된 슬롯만 {slot_key: (broker, asset_class, creds)} — 테스트 스텁 지점."""
    out = {}
    for key, broker, ac, loader in _SLOTS:
        c = loader()
        if c:
            out[key] = (broker, ac, c)
    return out


def current_handles() -> list[dict]:
    """등록 슬롯 → 핸들 목록(비민감). account_id 회전 매핑을 persist."""
    store = _load_map()
    handles = []
    for slot_key, (broker, ac, creds) in _slot_creds().items():
        fp = fingerprint(broker, creds)
        aid = resolve_account_id(slot_key, fp, store)
        mode = "paper" if creds.get("virtual", True) else "live"
        nick = store[slot_key].get("nickname") or \
            f"{broker.upper()} {'모의' if mode == 'paper' else '실전'} {_LABEL.get(ac, ac)}"
        handles.append({"account_id": aid, "broker": broker,
                        "asset_classes": [ac], "mode": mode, "nickname": nick})
    _save_map(store)
    return handles


def active_account_ids() -> list[str]:
    """활성 브로커의 핸들 account_id 집합 — 사이클 가드(P5-3)가 멤버십 검사."""
    ab = get_active_broker()
    return [h["account_id"] for h in current_handles() if h["broker"] == ab]
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd local && python -m pytest tests/test_account_handle.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: 커밋**

```bash
git add local/localapp/account_handle.py local/tests/test_account_handle.py
git commit -m "feat(local): current_handles/active_account_ids — 슬롯→핸들 (P5-1)"
```

---

## Task 3: analytics.local_health 보고 + INV-SEC 회귀 (TDD)

**Files:**
- Modify: `local/localapp/analytics.py` (`local_health`, line 289-341)
- Test: `local/tests/test_account_handle.py` (보고·INV-SEC 케이스 추가)

핸들을 `health["account_handles"]` + `health["active_account_ids"]`로 추가 → snapshot(payload.health)으로
서버 자동 노출(spec §3.4). 실패해도 기존 health 필드는 보존(best-effort).

- [ ] **Step 1: 실패 테스트 작성 (추가)**

```python
def test_local_health_reports_handles_no_secrets(monkeypatch):
    from localapp import analytics, account_handle as ah
    monkeypatch.setattr(ah, "current_handles", lambda: [
        {"account_id": "abc123", "broker": "ls", "asset_classes": ["kr_futures"],
         "mode": "paper", "nickname": "LS 모의 국내선물"}])
    monkeypatch.setattr(ah, "active_account_ids", lambda: ["abc123"])
    h = analytics.local_health()
    assert h["account_handles"][0]["account_id"] == "abc123"
    assert h["active_account_ids"] == ["abc123"]
    blob = str(h)
    assert "app_key" not in blob and "appkey" not in blob   # INV-SEC: 키 미포함
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd local && python -m pytest tests/test_account_handle.py::test_local_health_reports_handles_no_secrets -v`
Expected: FAIL (`KeyError: 'account_handles'`).

- [ ] **Step 3: 구현 — local_health에 추가**

`analytics.py`의 `local_health()` `return health` 직전에:
```python
    # P5-1 — 비민감 계좌 핸들 보고(서버/웹이 계좌 선택·가드 표면화에 사용). 실패해도 health 보존.
    try:
        from . import account_handle
        health["account_handles"] = account_handle.current_handles()
        health["active_account_ids"] = account_handle.active_account_ids()
    except Exception as e:
        log.warning("account_handles 보고 실패(무시): %s", e)
        health.setdefault("account_handles", [])
        health.setdefault("active_account_ids", [])
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd local && python -m pytest tests/test_account_handle.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: 전체 회귀 + 커밋**

Run: `cd local && python -m pytest -q` → 전부 pass.
```bash
git add local/localapp/analytics.py local/tests/test_account_handle.py
git commit -m "feat(local): local_health가 계좌 핸들 보고 (P5-1)"
```

---

## Self-Review

- **Spec 커버리지:** spec §3.1(핸들 슬롯맵·account_id 회전)·§3.4(local_health 보고)·§4(INV-SEC) → Task 1~3. ✓
  active_account_ids(집합)는 §3.3 가드(P5-3)가 소비 — 여기선 보고만.
- **Placeholder:** 없음. nickname은 P5-1에서 자동 라벨(사용자 편집은 P5-4 웹), store는 keyring `_HANDLE_MAP`.
- **타입 일관성:** `fingerprint(broker,creds)->str`, `resolve_account_id(slot_key,fp,store)->str`,
  `current_handles()->list[dict{account_id,broker,asset_classes,mode,nickname}]`, `active_account_ids()->list[str]`.
  Task 2가 Task 1의 fingerprint/resolve_account_id 사용, Task 3가 Task 2의 current/active 사용 — 일관. ✓
- **INV-SEC:** 핸들 dict에 키·계좌번호 값 없음(account_id=uuid, fingerprint=해시·로컬 store only). 테스트가 잠금. ✓
- **알려진 한계:** LS account_id가 appkey 기반이라 같은 appkey의 모의/실전은 mode로 구분(fingerprint에 mode 포함). KIS 단일 슬롯=핸들1=무변경(INV-KIS).

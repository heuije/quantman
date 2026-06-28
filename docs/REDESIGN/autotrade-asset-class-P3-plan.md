# P3 — 온보딩 슬롯 모델 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`.
> 상위 설계: [autotrade-asset-class-redesign.md](autotrade-asset-class-redesign.md) §3.1.
> **사장님 원증상(국내선물만 저장→화면이 다음 단계로 안 넘어감)을 직접 푸는 단계.**

**Goal.** 온보딩 "ready" 판정을 주식 슬롯 전용(`load_ls`/`load_kis`)에서 **등록된 자산군 슬롯 집합
(≥1)**으로 일반화하고, `_render_setup_area`의 모드 결정을 위젯에서 분리한 **순수함수**
`decide_setup_mode`로 추출해, 선물 단독 등록만으로도 온보딩이 페어링→자동매매로 진행되게 한다.

**Architecture.** (1) `secrets_store.broker_ready(broker)` — 그 브로커 자산군 슬롯이 ≥1이면 True
(active_cred_ok를 이걸로 일반화). (2) 새 `onboarding.py`의 `decide_setup_mode(broker, ready, dev_ok,
collapsed)` 순수함수(tkinter 무의존·테스트 가능). (3) `_render_setup_area`가 둘을 배선. (4) LS 저장이
어떤 자산군이든 active broker 일관 선언 + LS 배지/라벨이 선물 슬롯 반영.

**Tech Stack.** Python, pytest. P2(make_broker 선물 단독)가 이미 main에 있어 ready 통과 시 실행 동작함.

**불변식 INV-KIS.** 기존 KIS 사용자(주식 슬롯 보유)는 `broker_ready`/`active_cred_ok`/모드 결정이
이전과 동일(주식 있으면 ready=True 동일). 선물-단독 등 *기존에 도달 불가했던* 조합만 새로 진행.
조합 매트릭스 테스트(KIS 행 포함)로 잠근다.

---

## 현재 코드 (변경 기준점, f91a077)

- `secrets_store.active_cred_ok()` (≈161-166): `return bool(load_ls()) if get_active_broker()=="ls" else bool(load_kis())` — **주식 슬롯만**.
- `gui._render_setup_area(self, kis_ok, dev_ok)` (688-774): broker=ls면 `ls_ok=bool(load_ls())`(주식만)로 모드 결정(708-731), 이후 위젯 pack(732-757), normal 라벨(760-774).
- `refresh_status` (847): `self._render_setup_area(kis_ok=broker_cred_ok, dev_ok=bool(dev))`. (broker_cred_ok=`active_cred_ok()`, hero에도 사용.)
- `gui._ls_save` (1511-1540): 국내주식 분기만 `set_active_broker("ls")`+`broker_choice.set("ls")`. 선물/해외선물 분기는 미설정. messagebox에 "자동매매엔 '국내주식·해외주식' 계좌 등록이 기본으로 필요" (이제 거짓).
- `gui._refresh_active_accounts` (1004-1028): broker=ls면 `load_ls()` 배지 1개만. 선물 슬롯 미표시.
- `gui.py` import (21): `from . import (__version__, auto_state, killswitch, kis_health, order_log, pairing, secrets_store, sync_client, updater)`. `coverage`·`onboarding` 미import.

---

## File Structure

- **Modify** `local/localapp/secrets_store.py` — `broker_ready(broker)` 추가 + `active_cred_ok` 일반화.
- **Create** `local/localapp/onboarding.py` — `decide_setup_mode` 순수함수(tkinter 무의존).
- **Modify** `local/localapp/gui.py` — `_render_setup_area` 배선·`_ls_save` active broker 일관화·라벨·`_refresh_active_accounts` 배지.
- **Create** `local/tests/test_broker_ready.py`, `local/tests/test_decide_setup_mode.py`.

---

## Task 1: secrets_store.broker_ready + active_cred_ok 일반화

**Files:** Modify `local/localapp/secrets_store.py`; Create `local/tests/test_broker_ready.py`

- [ ] **Step 1: 실패 테스트** — `local/tests/test_broker_ready.py`
```python
"""broker_ready / active_cred_ok 일반화 — 자산군 슬롯 ≥1이면 ready (선물 단독 포함)."""
import pytest

from localapp import secrets_store


@pytest.fixture
def stub(monkeypatch):
    state = {"broker": "kis", "kis": None, "kis_fut": None, "kis_ovf": None,
             "ls": None, "ls_fut": None, "ls_ovf": None}
    monkeypatch.setattr(secrets_store, "get_active_broker", lambda: state["broker"])
    monkeypatch.setattr(secrets_store, "load_kis", lambda: state["kis"])
    monkeypatch.setattr(secrets_store, "load_kis_futures", lambda: state["kis_fut"])
    monkeypatch.setattr(secrets_store, "load_kis_overseas_futures", lambda: state["kis_ovf"])
    monkeypatch.setattr(secrets_store, "load_ls", lambda: state["ls"])
    monkeypatch.setattr(secrets_store, "load_ls_futures", lambda: state["ls_fut"])
    monkeypatch.setattr(secrets_store, "load_ls_overseas_futures", lambda: state["ls_ovf"])
    return state


def test_kis_stock_ready(stub):
    stub["kis"] = {"app_key": "x"}
    assert secrets_store.broker_ready("kis") is True
    assert secrets_store.active_cred_ok() is True


def test_kis_futures_only_ready(stub):
    """주식 없이 KIS 국내선물만 — ready (P3 핵심)."""
    stub["kis_fut"] = {"app_key": "y"}
    assert secrets_store.broker_ready("kis") is True


def test_kis_empty_not_ready(stub):
    assert secrets_store.broker_ready("kis") is False
    assert secrets_store.active_cred_ok() is False


def test_ls_futures_only_ready(stub):
    """주식 없이 LS 국내선물만 — ready (사장님 원증상의 핵심)."""
    stub["broker"] = "ls"
    stub["ls_fut"] = {"app_key": "y"}
    assert secrets_store.broker_ready("ls") is True
    assert secrets_store.active_cred_ok() is True


def test_ls_overseas_futures_only_ready(stub):
    stub["broker"] = "ls"
    stub["ls_ovf"] = {"app_key": "z"}
    assert secrets_store.broker_ready("ls") is True


def test_ls_empty_not_ready(stub):
    stub["broker"] = "ls"
    assert secrets_store.broker_ready("ls") is False


def test_active_cred_ok_uses_active_broker(stub):
    """active_cred_ok = broker_ready(get_active_broker())."""
    stub["broker"] = "ls"
    stub["ls_fut"] = {"app_key": "y"}    # LS 선물만
    stub["kis"] = None
    assert secrets_store.active_cred_ok() is True   # 활성=ls라 LS 슬롯 기준
```

- [ ] **Step 2: 실패 확인**
Run: `cd local && python -m pytest tests/test_broker_ready.py -q`
Expected: FAIL — `broker_ready` 미존재 + `active_cred_ok`가 선물단독 False.

- [ ] **Step 3: 구현** — `local/localapp/secrets_store.py`. 기존 `active_cred_ok` (≈161-166)을 교체:
```python
def broker_ready(broker: str) -> bool:
    """주어진 브로커의 자격증명이 하나라도 등록됐는지(자산군 슬롯 ≥1) — 온보딩 ready SSOT.

    기존 active_cred_ok(주식 슬롯만)을 자산군 슬롯 집합으로 일반화한다. 주식 없이 선물만
    등록해도 ready=True(선물 단독 온보딩). 주식 보유 사용자는 종전과 동일(주식 있으면 True)."""
    if broker == "ls":
        return bool(load_ls() or load_ls_futures() or load_ls_overseas_futures())
    return bool(load_kis() or load_kis_futures() or load_kis_overseas_futures())


def active_cred_ok() -> bool:
    """현재 활성 브로커의 자격증명이 하나라도 등록됐는지 — 단일 브로커 'broker ready' SSOT.

    기존 KIS 사용자(주식 보유)는 종전과 동일(불변식). 선물 단독 등 신규 조합만 새로 ready."""
    return broker_ready(get_active_broker())
```
(`active_cred_label`은 그대로 둔다.)

- [ ] **Step 4: 통과**
Run: `cd local && python -m pytest tests/test_broker_ready.py -q`
Expected: PASS (7).

- [ ] **Step 5: 커밋**
```bash
git add local/localapp/secrets_store.py local/tests/test_broker_ready.py
git commit -m "feat(autotrade): broker_ready — 온보딩 ready를 자산군 슬롯 집합으로 일반화 (P3)"
```

---

## Task 2: onboarding.decide_setup_mode 순수함수

**Files:** Create `local/localapp/onboarding.py`, `local/tests/test_decide_setup_mode.py`

- [ ] **Step 1: 실패 테스트** — `local/tests/test_decide_setup_mode.py`
```python
"""decide_setup_mode 순수함수 — 온보딩 모드 결정(위젯 무의존) 조합 매트릭스 (P3)."""
import pytest

from localapp.onboarding import decide_setup_mode


@pytest.mark.parametrize("broker,ready,dev_ok,collapsed,expected", [
    # 둘 다 완료 + 접힘 → normal
    ("kis", True,  True,  True,  "normal"),
    ("ls",  True,  True,  True,  "normal"),
    # 미등록 → 해당 브로커 wizard
    ("kis", False, False, True,  "wizard_kis"),
    ("ls",  False, False, True,  "wizard_ls"),
    ("kis", False, True,  True,  "wizard_kis"),
    ("ls",  False, True,  True,  "wizard_ls"),
    # 등록·페어링 미완 → wizard_pair
    ("kis", True,  False, True,  "wizard_pair"),
    ("ls",  True,  False, True,  "wizard_pair"),
    # 둘 다 완료지만 ⚙ 펼침 → 자격증명 변경(wizard)
    ("kis", True,  True,  False, "wizard_kis"),
    ("ls",  True,  True,  False, "wizard_ls"),
])
def test_matrix(broker, ready, dev_ok, collapsed, expected):
    assert decide_setup_mode(broker, ready, dev_ok, collapsed) == expected
```

- [ ] **Step 2: 실패 확인**
Run: `cd local && python -m pytest tests/test_decide_setup_mode.py -q`
Expected: FAIL — `localapp.onboarding` 미존재.

- [ ] **Step 3: 구현** — `local/localapp/onboarding.py`
```python
"""온보딩 상태 결정 — 위젯 무의존 순수 로직(테스트 가능).

GUI(_render_setup_area)는 이 결정의 렌더러일 뿐. 분리로 조합 매트릭스를 전수 테스트한다
(기존엔 위젯과 결합돼 무검증이라 '선물 단독 stuck' 버그가 ship됨)."""
from __future__ import annotations


def decide_setup_mode(broker: str, ready: bool, dev_ok: bool, collapsed: bool) -> str:
    """온보딩 화면 모드 결정. 반환: normal | wizard_kis | wizard_ls | wizard_pair.

    ready = 그 브로커 자산군 슬롯 ≥1(secrets_store.broker_ready). 기존 _render_setup_area
    로직과 동치이되 ready를 '주식 슬롯'이 아니라 '자산군 슬롯 집합'으로 받는다."""
    wizard = "wizard_ls" if broker == "ls" else "wizard_kis"
    if not ready:
        return wizard               # 자격증명 미등록 → 해당 브로커 입력 폼
    if not dev_ok:
        return "wizard_pair"        # 자격증명 OK, 페어링 필요
    if collapsed:
        return "normal"             # 둘 다 완료 + 접힘 → 정상 운영
    return wizard                   # 둘 다 OK인데 ⚙ 펼침 → 자격증명 변경
```

- [ ] **Step 4: 통과**
Run: `cd local && python -m pytest tests/test_decide_setup_mode.py -q`
Expected: PASS (10).

- [ ] **Step 5: 커밋**
```bash
git add local/localapp/onboarding.py local/tests/test_decide_setup_mode.py
git commit -m "feat(autotrade): decide_setup_mode 순수함수 — 온보딩 모드 결정 위젯 분리 (P3)"
```

---

## Task 3: _render_setup_area 배선 + _ls_save active broker 일관화 (사장님 원증상 수정)

**Files:** Modify `local/localapp/gui.py`

- [ ] **Step 1: import 추가** — `gui.py` 상단 `from . import (...)`에 `coverage` 불필요(미사용),
  `onboarding` 추가:
```python
from . import (__version__, auto_state, killswitch, kis_health, onboarding,
                order_log, pairing, secrets_store, sync_client, updater)
```

- [ ] **Step 2: `_render_setup_area` 모드 결정부 교체** — 현재 708-731(broker/ls_ok/kis_ok/dev_ok →
  new_mode 블록)을 아래로 교체. `kis_ok` 파라미터는 더는 안 쓰므로 시그니처에서 제거:
  - 시그니처 `def _render_setup_area(self, kis_ok: bool, dev_ok: bool):` → `def _render_setup_area(self, dev_ok: bool):`
  - 본문 시작(708)부터 731까지를:
```python
        broker = self.broker_choice.get()
        ready = secrets_store.broker_ready(broker)
        new_mode = onboarding.decide_setup_mode(broker, ready, dev_ok, self.setup_collapsed)
```
  (732 이하 `if getattr(self, "_setup_mode", None) != new_mode:` 위젯 pack 로직은 **그대로**.)

- [ ] **Step 3: normal 라벨(760-774) 선물 단독 견고화** — LS 분기에서 `load_ls()`가 None이어도
  (선물 단독) 적절히 표시. 현재:
```python
            if broker == "ls":
                ls = secrets_store.load_ls()
                mode = "모의" if (ls or {}).get("virtual", True) else "실전"
                parts = [f"✓ LS증권 자격증명 ({mode})", "✓ 플랫폼 계정 연결됨"]
```
  를:
```python
            if broker == "ls":
                ls = secrets_store.load_ls()
                if ls:
                    mode = "모의" if ls.get("virtual", True) else "실전"
                    parts = [f"✓ LS증권 자격증명 ({mode})", "✓ 플랫폼 계정 연결됨"]
                else:
                    parts = ["✓ LS증권 자격증명 등록됨", "✓ 플랫폼 계정 연결됨"]
```

- [ ] **Step 4: refresh_status 호출부(847) 수정** — `self._render_setup_area(kis_ok=broker_cred_ok, dev_ok=bool(dev))` 를:
```python
        self._render_setup_area(dev_ok=bool(dev))
```
  (`broker_cred_ok`는 hero에서 계속 사용하므로 그 변수 자체는 유지.)

- [ ] **Step 5: `_ls_save` active broker 일관화 + messagebox 수정** — 현재 국내주식 분기만
  set_active_broker하는 것을, **모든 LS 저장이 LS를 active broker로** 선언하게(사용자가 LS 폼에서
  저장 = LS 사용 의도; P2가 선물 단독을 지원하므로 안전). 현재 1523-1530:
```python
        if acct_type == _LS_ACCT_FUTURES:
            secrets_store.save_ls_futures(key, secret, acct, virtual=virtual)
        elif acct_type == _LS_ACCT_OV_FUTURES:
            secrets_store.save_ls_overseas_futures(key, secret, acct, virtual=virtual)
        else:   # 국내주식·해외주식 — LsBroker 기반 브로커 + LS 활성화
            secrets_store.save_ls(key, secret, acct, virtual=virtual)
            secrets_store.set_active_broker("ls")
            self.broker_choice.set("ls")
```
  를:
```python
        if acct_type == _LS_ACCT_FUTURES:
            secrets_store.save_ls_futures(key, secret, acct, virtual=virtual)
        elif acct_type == _LS_ACCT_OV_FUTURES:
            secrets_store.save_ls_overseas_futures(key, secret, acct, virtual=virtual)
        else:   # 국내주식·해외주식
            secrets_store.save_ls(key, secret, acct, virtual=virtual)
        # 어떤 자산군이든 LS 폼에서 저장 = LS 사용 의도 → active broker 일관 선언
        # (P2 make_broker가 선물 단독 구성을 지원 — 주식 없이 선물만으로도 동작).
        secrets_store.set_active_broker("ls")
        self.broker_choice.set("ls")
```
  그리고 messagebox 문구(1538-1539)에서 거짓이 된 "(자동매매엔 '국내주식·해외주식' 계좌 등록이
  기본으로 필요합니다.)" 줄을 제거하고 다음으로:
```python
        messagebox.showinfo("저장 완료",
                            f"LS증권 {acct_type} 자격증명을 저장했습니다 ({mode}).\n\n"
                            "다른 자산군(선물·해외선물)은 [계좌 종류]를 바꿔 추가 등록할 수 있습니다.")
```

- [ ] **Step 6: 통과 + 회귀** — 헤드리스 GUI 인스턴스화 없이 import만 확인 + 전체 회귀.
Run: `cd local && python -c "import localapp.gui" && python -m pytest tests/ -q`
Expected: import OK; 전체 PASS(기존 + P3 신규). 기존 시나리오 무영향.

- [ ] **Step 7: 커밋**
```bash
git add local/localapp/gui.py
git commit -m "fix(autotrade): 온보딩이 자산군 슬롯 ready로 진행 — 선물 단독 등록 stuck 해소 (P3)"
```

---

## Task 4: _refresh_active_accounts LS 선물 배지

**Files:** Modify `local/localapp/gui.py`

- [ ] **Step 1: 구현** — `_refresh_active_accounts`(1004-1028) LS 분기(1013-1017)를 KIS처럼 3슬롯으로:
```python
            if broker == "ls":
                parts = [
                    badge(secrets_store.load_ls(), "LS·국내주식"),
                    badge(secrets_store.load_ls_futures(), "LS·국내선물"),
                    badge(secrets_store.load_ls_overseas_futures(), "LS·해외선물"),
                ]
                broker_tag = "활성 브로커: LS증권"
```

- [ ] **Step 2: 확인** — import + 전체 회귀.
Run: `cd local && python -c "import localapp.gui" && python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 3: 커밋**
```bash
git add local/localapp/gui.py
git commit -m "feat(autotrade): LS 활성계좌 배지에 국내선물·해외선물 슬롯 표시 (P3)"
```

---

## Self-Review

**1. Spec coverage:** §3.1 = ready를 슬롯 집합으로(Task1)·decide_setup_mode 순수함수(Task2)·배선+저장
일관화(Task3)·badge(Task4). 검증공백 V1(온보딩 테스트 0)은 Task1·2 매트릭스로 해소.
**2. Placeholder scan:** 없음. 모든 스텝 실제 코드.
**3. Type consistency:** `broker_ready(broker)->bool`·`decide_setup_mode(broker,ready,dev_ok,collapsed)->str`
일관. `_render_setup_area`에서 ready=broker_ready(broker_choice), decide_setup_mode로 모드.
**INV-KIS:** decide_setup_mode 매트릭스의 KIS 행이 기존 동작과 동치(주식 보유 시 ready=True 동일).
`active_cred_ok` 일반화는 주식 보유 사용자에게 무변경(additive). **검증 한계:** GUI 위젯 pack은
헤드리스로 단위검증 불가(import + 순수함수 매트릭스로 대체) — 실제 화면 진행은 사장님 모의 1회
(선물 단독 저장→페어링 단계 진행 확인)로 P2+P3 릴리스 후 검증.

# P5-3 — 실행 계좌 가드 (C7 차단) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. 상위 spec:
> [account-linked-strategy](../specs/2026-06-29-account-linked-strategy-and-fund-transparency-design.md) §3.3.
> 의존: P5-1(`account_handle.active_account_ids`)·P5-2(`/sync/strategies`의 account_ref) — **둘 다 완료**.

**Goal:** 사이클 진입에서 전략의 `account_ref`가 **현재 활성 계좌 핸들이 아니면 그 전략을 통째 skip** +
`skip_wrong_account` 표면화. 모의로 검증한 전략이 실전 계좌로 무경고 실거래되는 것(C7)을 식별자 차원에서 차단.

**Architecture:** P1 커버리지 게이트와 **같은 결정 지점**(`trader._enter_from_preview` 진입 루프, coverage
게이트 직전)에 가드 1개 추가. `account_ref=None`(레거시·미바인딩)은 통과(기존 거동 무변경). 활성 핸들
집합은 `account_handle.active_account_ids()`(P5-1)에서 사이클당 1회 조회(로컬 keyring, 네트워크 0).

**Tech Stack:** Python(localapp trader). 기존 `coverage` 게이트(skip_uncovered)가 동형 참조.

**불변식:** 레거시 호환 — account_ref 없는 전략은 활성 계좌에서 그대로 실행. INV-KIS — KIS 단일계좌
사용자는 핸들 1개라 account_ref 바인딩 시에도 항상 매칭(미바인딩이면 통과) → 기존 거동.

---

## Task 1: _enter_from_preview에 account 가드 + 요약 카운트 (TDD)

**Files:**
- Modify: `local/localapp/trader.py` (import line 32; `_enter_from_preview` line 1229-1267; cycle_summary line ~1929)
- Test: `local/tests/test_account_guard.py` (신규 — `test_coverage.py` 패턴 재사용)

- [ ] **Step 1: 실패 테스트 작성**

`test_coverage.py`의 SimBroker·전략·preview(by_strategy) 구성 패턴을 그대로 가져와(새 패턴 금지),
`account_handle.active_account_ids`를 monkeypatch로 제어. 케이스:
```python
"""P5-3 — 실행 계좌 가드(account_ref↔활성 핸들). test_coverage.py 패턴 재사용."""
# (fixture·SimBroker·_run_entry 헬퍼는 test_coverage.py에서 복사)

def test_strategy_bound_to_other_account_is_skipped(monkeypatch):
    monkeypatch.setattr("localapp.account_handle.active_account_ids", lambda: ["acc_ACTIVE"])
    # 전략 account_ref="acc_OTHER" → skip_wrong_account, 발주 0
    decisions = _run_entry(strategies=[{"id": "1", "name": "s", "definition": _DEF,
                                        "account_ref": "acc_OTHER"}],
                           by_strategy=[{"strategy_id": "1", "candidates": [{"symbol": "005930"}]}])
    actions = [d["action"] for d in decisions]
    assert "skip_wrong_account" in actions
    assert "bought" not in actions

def test_strategy_bound_to_active_account_runs(monkeypatch):
    monkeypatch.setattr("localapp.account_handle.active_account_ids", lambda: ["acc_ACTIVE"])
    decisions = _run_entry(strategies=[{"id": "1", "name": "s", "definition": _DEF,
                                        "account_ref": "acc_ACTIVE"}],
                           by_strategy=[{"strategy_id": "1", "candidates": [{"symbol": "005930"}]}])
    assert "skip_wrong_account" not in [d["action"] for d in decisions]

def test_legacy_null_account_ref_runs(monkeypatch):
    monkeypatch.setattr("localapp.account_handle.active_account_ids", lambda: ["acc_ACTIVE"])
    decisions = _run_entry(strategies=[{"id": "1", "name": "s", "definition": _DEF}],  # account_ref 없음
                           by_strategy=[{"strategy_id": "1", "candidates": [{"symbol": "005930"}]}])
    assert "skip_wrong_account" not in [d["action"] for d in decisions]

def test_cycle_summary_counts_skip_wrong_account(monkeypatch):
    monkeypatch.setattr("localapp.account_handle.active_account_ids", lambda: ["acc_ACTIVE"])
    # cycle() 전체를 돌려 cycle_summary["n_skip_wrong_account"] >= 1 확인 (test_coverage.py의 cycle 호출 패턴 재사용)
    ...
```
> `_DEF`(최소 유효 IR)·SimBroker·`_run_entry`/cycle 호출은 **test_coverage.py에서 복사**. SimBroker는 전
> 자산군 커버라 coverage 게이트는 통과 → account 가드만 분리 검증.

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd local && python -m pytest tests/test_account_guard.py -v`
Expected: FAIL (skip_wrong_account 미발생 — 가드 없음).

- [ ] **Step 3: 구현**

`trader.py` import(line 32) — `account_handle` 추가:
```python
from . import account_handle, analytics, coverage, intents, killswitch, order_log, state_store
```

`_enter_from_preview`(line 1247) — `strat_def_by_id`를 account_ref 포함 3-tuple로 + 활성 핸들 1회 조회:
```python
        strat_def_by_id = {str(s["id"]): (s.get("name", ""), s.get("definition", {}),
                                          s.get("account_ref"))
                           for s in strategies}
        # P5-3 — 활성 계좌 핸들 집합(사이클당 1회·로컬 keyring). 조회 실패 시 보수적 빈 집합
        # (account_ref 바인딩 전략은 skip, 레거시 None은 통과) — 불확실하면 실거래 안 함.
        try:
            active_ids = set(account_handle.active_account_ids())
        except Exception as e:
            log.warning("활성 계좌 핸들 조회 실패 — account_ref 전략 보수적 skip: %s", e)
            active_ids = set()
```
unpack(line 1259):
```python
            strat_name, strat_def, acct_ref = name_def
            # P5-3 (계좌-전략 연동) — 전략이 특정 계좌(account_ref)에 묶였는데 그 계좌가 현재
            # 활성 계좌가 아니면 전략 통째 skip. 모의 검증 전략이 실전 계좌로 무경고 실거래되는
            # 것(C7)을 식별자 차원에서 차단. account_ref=None(레거시·미바인딩)은 통과(기존 거동).
            if acct_ref and acct_ref not in active_ids:
                decisions.append(order_log.decision(
                    "skip_wrong_account", sid, strat_name, "",
                    "이 전략은 다른 계좌에 묶여 있어 현재 활성 계좌에서 실행되지 않습니다"))
                continue
```
(이 블록은 기존 coverage 게이트[line 1262] **직전**에 위치 — 바인딩이 자산군보다 선행.)

cycle_summary(line ~1929, `n_skip_uncovered` 옆) — 카운트 추가:
```python
            "n_skip_wrong_account": sum(
                1 for d in decisions if d["action"] == "skip_wrong_account"),
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd local && python -m pytest tests/test_account_guard.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: 전체 회귀 + 커밋**

Run: `cd local && python -m pytest -q` → 전부 pass(account_ref 없는 기존 전략 무영향 = 레거시 호환).
```bash
git add local/localapp/trader.py local/tests/test_account_guard.py
git commit -m "feat(local): 실행 계좌 가드 — account_ref↔활성 핸들 (P5-3, C7 차단)"
```

---

## 비범위
- **청산/포지션 경로의 account 가드:** 보유 포지션은 이미 coverage 게이트(orphan_uncovered, line 1770)가
  브로커 가시성 부재를 표면화 — 활성 계좌 변경 시 옛 포지션은 coverage가 잡는다. account_ref 가드는 *진입*에
  집중(C7 핵심). 별도 필요 측정 시 후속.
- **웹 표면화(skip_wrong_account를 사용자에게):** 이미 snapshot decisions에 실려 서버로 감 — 웹 표시는 P5-4.

## Self-Review
- **Spec 커버리지:** §3.3(진입 가드·전략단위 skip·표면화·같은 결정지점) → Task 1. ✓
- **Placeholder:** 테스트는 test_coverage.py 패턴 복사를 *명시 지시*(새 패턴 금지) — 정당한 참조.
- **타입 일관성:** `acct_ref`(str|None) ← `s.get("account_ref")`(P5-2 serve), `active_ids: set[str]` ←
  `account_handle.active_account_ids()->list[str]`(P5-1). 3-tuple unpack 1곳(line 1259)만 변경. ✓
- **레거시 호환:** account_ref None → 가드 통과. 전체 회귀(Step 5)가 잠금. INV-KIS 보존.
- **자금안전 기본값:** 활성 핸들 조회 실패 시 바인딩 전략 보수적 skip(실거래 안 함)·레거시는 통과·로그 표면화.

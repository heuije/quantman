# P5-2 — Strategy.account_ref (서버) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. 상위 spec:
> [account-linked-strategy](../specs/2026-06-29-account-linked-strategy-and-fund-transparency-design.md) §3.1·§5(P5).
> P5-3(로컬 실행 가드)이 `/sync/strategies`의 account_ref를 소비한다.

**Goal:** 전략을 계좌 핸들에 묶을 수 있도록 `Strategy.account_ref`(nullable, 핸들 account_id) 컬럼을 추가하고,
생성·수정 시 받아 저장하며, `/sync/strategies`·전략 API 응답에 실어 준다.

**Architecture:** 이 프로젝트는 **Alembic 없음** — `SQLModel.metadata.create_all`(신규 테이블만) +
`db.py`의 `_NEW_COLS` 리스트 + 부팅 시 `_migrate()`의 멱등 `_ensure_column`(PG/SQLite 통합)으로 기존
테이블에 컬럼을 추가한다. account_ref는 **nullable VARCHAR** → 레거시 전략은 NULL(=미바인딩, P5-3 가드가
레거시 통과 처리). account_id는 opaque uuid(로컬 생성, INV-SEC) — 서버는 검증 없이 그대로 저장·서빙.

**Tech Stack:** FastAPI·SQLModel·Pydantic. 기존 Phase 59 strategy 컬럼(paper_started_at 등)이 동일 패턴의 참조.

**불변식:** 레거시 호환 — account_ref 없는 기존 전략/요청은 NULL로 동작(기존 거동 무변경). 서버는 account_ref를
opaque로 저장(키·계좌번호 아님 — INV-SEC 무관).

---

## Task 1: 데이터 계층 — model 필드 + 마이그레이션 컬럼 + 스키마 (TDD)

**Files:**
- Modify: `server/app/models.py` (Strategy, line 49 직후)
- Modify: `server/app/db.py` (`_NEW_COLS`, line 83 직전)
- Modify: `server/app/schemas.py` (StrategyIn line 96-100, StrategyOut line 102+)
- Test: `server/tests/test_strategy_account_ref.py` (신규)

- [ ] **Step 1: 실패 테스트 작성**

`server/tests/test_strategy_account_ref.py`:
```python
"""P5-2 — Strategy.account_ref 데이터 계층 회귀."""
from app import db as appdb
from app.models import Strategy
from app.schemas import StrategyIn, StrategyOut


def test_strategy_model_has_account_ref():
    s = Strategy(user_id=1, name="t", account_ref="acc_abc")
    assert s.account_ref == "acc_abc"
    assert Strategy(user_id=1, name="t2").account_ref is None  # nullable·레거시 기본


def test_schemas_have_account_ref():
    assert StrategyIn(definition={}).account_ref is None       # 입력 optional
    out = StrategyOut(id=1, name="t", run_mode="draft", engine="ir",
                      definition={}, account_ref="acc_abc",
                      created_at=__import__("datetime").datetime.now(),
                      updated_at=__import__("datetime").datetime.now())
    assert out.account_ref == "acc_abc"


def test_new_cols_registers_account_ref():
    assert ("strategy", "account_ref", "VARCHAR") in appdb._NEW_COLS
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd server && python -m pytest tests/test_strategy_account_ref.py -v`
Expected: FAIL (account_ref 미존재 / _NEW_COLS 누락).

- [ ] **Step 3: 구현**

`models.py` Strategy, `live_basket` 필드(line 49) 직후 추가:
```python
    # P5-2 (계좌-전략 연동) — 이 전략을 실행할 계좌 핸들(account_handle.account_id, opaque uuid).
    # NULL=미바인딩(레거시) → 로컬 실행 가드(P5-3)가 활성 계좌에서 통과 처리. INV-SEC: 계좌번호 아님.
    account_ref: Optional[str] = None
```

`db.py` `_NEW_COLS` 리스트에 strategy 그룹과 함께 추가(line 74 `live_basket` 다음 줄):
```python
    ("strategy",     "account_ref",                      "VARCHAR"),
```

`schemas.py` `StrategyIn`(line 96)에 추가:
```python
    account_ref: Optional[str] = None   # 바인딩할 계좌 핸들 id (없으면 미바인딩)
```
`schemas.py` `StrategyOut`(line 102)에 추가:
```python
    account_ref: Optional[str] = None
```
(`Optional` import 확인 — schemas.py 상단에 이미 있을 것. 없으면 `from typing import Optional` 추가.)

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd server && python -m pytest tests/test_strategy_account_ref.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: 마이그레이션 멱등 회귀**

Run: `cd server && python -m pytest tests/test_migrate_indexes.py -v`
Expected: PASS (account_ref 추가가 멱등 _migrate 2회 실행에 에러 0 — 기존 idempotency 테스트가 잡음).

- [ ] **Step 6: 커밋**

```bash
git add server/app/models.py server/app/db.py server/app/schemas.py server/tests/test_strategy_account_ref.py
git commit -m "feat(server): Strategy.account_ref 컬럼 + 스키마 (P5-2 데이터계층)"
```

---

## Task 2: 라우터 — account_ref 저장·서빙 (create/update/_out/sync) (TDD)

**Files:**
- Modify: `server/app/routers/strategies.py` (create line 265, update line 322-329, `_out` 헬퍼)
- Modify: `server/app/routers/sync.py` (pull_strategies, line 248-250)
- Test: `server/tests/test_strategy_account_ref.py` (라운드트립 케이스 추가)

- [ ] **Step 1: 실패 테스트 작성 (추가)**

라운드트립 — 기존 strategies/sync 테스트의 app fixture 패턴 재사용(`tests/test_strategies_ir.py`·`test_sync_*` 참조: TestClient + create_all + 인증 의존성 override). 케이스:
```python
def test_create_persists_and_serves_account_ref(client_and_user):
    client, _ = client_and_user
    r = client.post("/strategies", json={"definition": {...최소 유효 IR...},
                                         "run_mode": "draft", "account_ref": "acc_xyz"})
    assert r.status_code == 201
    sid = r.json()["id"]
    assert r.json()["account_ref"] == "acc_xyz"
    assert client.get(f"/strategies/{sid}").json()["account_ref"] == "acc_xyz"  # _out 경로

def test_update_changes_account_ref(client_and_user):
    # 생성(account_ref=None) → PUT account_ref="acc_2" → GET 반영(재바인딩)
    ...

def test_pull_strategies_includes_account_ref(client_and_user):
    # paper 전략 account_ref="acc_p" 생성 → GET /sync/strategies(device-authed)에 account_ref 포함
    ...

def test_legacy_strategy_account_ref_null(client_and_user):
    # account_ref 없이 생성 → None(레거시 호환)
    ...
```
> 정확한 fixture·최소 유효 IR·device 인증은 기존 `tests/test_strategies_ir.py`/`test_sync_preview_auth.py`에서 그대로 가져온다(새 패턴 만들지 말 것).

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd server && python -m pytest tests/test_strategy_account_ref.py -v`
Expected: FAIL (account_ref 미저장/미서빙).

- [ ] **Step 3: 구현**

`strategies.py`:
- `create_strategy`: `Strategy(...)` 생성자에 `account_ref=body.account_ref` 추가(line 265-270 블록).
- `update_strategy`: 다른 필드 set 옆에 `row.account_ref = body.account_ref` 추가(line 322-329 블록).
- `_out(row)` 헬퍼: 반환 StrategyOut에 `account_ref=row.account_ref` 추가(헬퍼 위치는 파일에서 `def _out` grep).

`sync.py` `pull_strategies`(line 248-250)의 StrategyOut 생성에 추가:
```python
        return [StrategyOut(id=s.id, name=s.name, run_mode=s.run_mode, engine=s.engine,
                            definition={**(s.definition or {}), "engine": s.engine},
                            account_ref=s.account_ref,
                            created_at=s.created_at, updated_at=s.updated_at) for s in rows]
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd server && python -m pytest tests/test_strategy_account_ref.py -v`
Expected: PASS (전 케이스).

- [ ] **Step 5: 서버 전체 회귀 + 커밋**

Run: `cd server && python -m pytest -q` → 전부 pass(기존 strategies/sync 테스트 무영향 = 레거시 호환 확인).
```bash
git add server/app/routers/strategies.py server/app/routers/sync.py server/tests/test_strategy_account_ref.py
git commit -m "feat(server): account_ref 저장·서빙 (create/update/_out/sync) (P5-2 라우터)"
```

---

## 비범위 (이 plan 밖)
- `_assert_live_tradable`를 계좌 핸들 자산군과 대조하는 **advisory 확장** → P5-4(웹) 또는 후속(서버는 로컬
  핸들 커버리지를 snapshot으로만 알아 권위가 약함 — spec §3.4). P5-2는 account_ref 저장·서빙까지만.
- 마이그레이션 **배포**(origin/main 머지 → Railway 자동) — 사용자 명시 허락 시에만.

## Self-Review
- **Spec 커버리지:** §3.1(account_ref 컬럼)·§5 P5(서버) → Task 1(데이터)·Task 2(서빙). ✓
- **Placeholder:** Task 2 테스트는 "기존 fixture 재사용"을 *명시 지시*(새 패턴 금지) — 구현자가 실제 fixture를 가져옴. 최소 유효 IR은 기존 test_strategies_ir.py에서 복사. 정당한 참조(중복 정의 회피).
- **타입 일관성:** `account_ref: Optional[str]` — model·StrategyIn·StrategyOut 동일. create=`body.account_ref`, update=`row.account_ref=body.account_ref`, serve=`account_ref=s.account_ref`/`row.account_ref`. ✓
- **레거시 호환:** nullable·기본 None — 기존 전략/요청 무변경. 서버 전체 회귀(Step 5)가 잠금.

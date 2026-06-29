# P5-4 — 웹 계좌 선택 바인딩 UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. 상위 spec:
> [account-linked-strategy](../specs/2026-06-29-account-linked-strategy-and-fund-transparency-design.md) §3.2.
> 의존: P5-1(snapshot.health.account_handles/active_account_ids)·P5-2(account_ref accept/serve) — **둘 다 main 머지됨**.
> ⚠ UI 작업 — `DESIGN.md` 토큰 준수(인라인 hex 금지). web 빌더 공통=조대표 도메인(§3).

**Goal:** 전략 "적용" 시 **실행할 계좌를 명시적으로 고르게** 하고(선택한 핸들의 mode가 run_mode), `account_ref`를
세팅한다. 전략 카드·상세에 바인딩된 계좌를 표시하고, "전환(rebind)"으로 다른 계좌(예: 실전)로 재바인딩한다.
→ **C7을 사용자 차원에서 실활성화**(account_ref가 채워져야 로컬 P5-3 가드가 동작).

**Architecture:** `AccountPicker`(모달, 핸들 목록 선택) 신설. IrBuilder "적용" → 핸들 있으면 picker → 선택 시
`account_ref=handle.account_id`·`run_mode=handle.mode`로 save. 핸들 없으면 "로컬앱에서 계좌 등록·페어링" 안내.
계좌 정보는 기존 `api.snapshot().payload.health.account_handles`에서 읽음(P5-1). StrategyDetail에서 재바인딩.

**Tech Stack:** React+TS+Vite. `web/src/{types.ts,api.ts,pages/IrBuilder.tsx,pages/Strategies.tsx,pages/StrategyDetail.tsx}` + `web/src/components/AccountPicker.tsx`(신규) + `index.css`. DESIGN.md 토큰.

**불변식:** 핸들 없는 사용자/레거시 전략 = 기존 거동(account_ref 없이도 적용 가능 — 단 "계좌 미바인딩" 표시).
INV-SEC: 웹은 비민감 핸들(account_id·별명·mode)만 다룸.

---

## Task 1: 타입 + api — account_ref·AccountHandle 플러밍 (tsc)

**Files:**
- Modify: `web/src/types.ts` (StrategyRow ~151, + AccountHandle 신규, SyncSnapshot health)
- Modify: `web/src/api.ts` (createStrategy ~176, updateStrategy ~181)

- [ ] **Step 1: 타입 추가**

`types.ts`:
```typescript
export interface AccountHandle {
  account_id: string;
  broker: "kis" | "ls";
  asset_classes: string[];
  mode: "paper" | "live";
  nickname: string;
}
```
`StrategyRow`에 `account_ref?: string | null;` 추가.
`SyncSnapshot`의 `payload.health` 타입에 `account_handles?: AccountHandle[]`·`active_account_ids?: string[]` 추가
(health 타입 위치는 types.ts에서 `health` grep — 없으면 payload 타입에 추가).

- [ ] **Step 2: api 시그니처에 account_ref**

`api.ts` createStrategy/updateStrategy에 `account_ref?: string | null` 파라미터 추가, body에 포함:
```typescript
createStrategy: (definition, run_mode, engine = "ir", account_ref?: string | null) =>
  req<StrategyRow>("/strategies", { method: "POST",
    body: JSON.stringify({ definition, run_mode, engine, account_ref }) }),
updateStrategy: (id, definition, run_mode, engine = "ir", account_ref?: string | null) =>
  req<StrategyRow>(`/strategies/${id}`, { method: "PUT",
    body: JSON.stringify({ definition, run_mode, engine, account_ref }) }),
```
(파라미터 타입은 기존 시그니처 형식에 맞춤.)

- [ ] **Step 3: 타입체크**

Run: `cd web && bunx tsc --noEmit` (또는 `npx tsc --noEmit`)
Expected: 에러 0 (account_ref optional이라 기존 호출부 무변경 통과).

- [ ] **Step 4: 커밋**

```bash
git add web/src/types.ts web/src/api.ts
git commit -m "feat(web): account_ref·AccountHandle 타입 + api (P5-4 플러밍)"
```

---

## Task 2: AccountPicker 컴포넌트 (모달 — 핸들 선택)

**Files:**
- Create: `web/src/components/AccountPicker.tsx`
- Modify: `web/src/index.css` (필요 시 — 기존 `.account-menu`/`.seg`/badge 재사용 우선)

- [ ] **Step 1: 컴포넌트 작성**

`AccountPicker.tsx` — props `{ handles: AccountHandle[]; activeIds: string[]; currentRef?: string | null; onSelect: (h: AccountHandle) => void; onClose: () => void; }`.
- 핸들 목록을 선택 리스트로(각 행: 별명 + 브로커 + 자산군 + mode 배지 `.sc-badge.paper/.live`). 행은 `.account-menu button` 패턴 재사용.
- 활성 계좌(activeIds 포함)는 "● 현재 활성" 표시, 비활성은 "○"(선택은 가능 — 나중에 전환 의도).
- currentRef와 같은 핸들은 "현재 바인딩" 강조.
- 핸들 0개면 안내: "로컬앱에서 계좌를 등록·페어링하면 여기 표시됩니다."
- 모든 색·간격은 DESIGN.md 토큰(`var(--*)`)만. 인라인 hex 금지.
- mode=live 선택 시 onSelect 전 confirm: "실전 계좌 '{별명}'로 적용합니다 — 다음 사이클부터 실제 자금으로 거래됩니다. 계속할까요?"

- [ ] **Step 2: 타입체크 + 커밋**

Run: `cd web && bunx tsc --noEmit` → 에러 0.
```bash
git add web/src/components/AccountPicker.tsx web/src/index.css
git commit -m "feat(web): AccountPicker — 계좌 핸들 선택 모달 (P5-4)"
```

---

## Task 3: IrBuilder 적용 → 계좌 선택

**Files:**
- Modify: `web/src/pages/IrBuilder.tsx` (save ~641, apply 버튼 ~1295, snapshot fetch 추가)

- [ ] **Step 1: 구현**

- IrBuilder가 `api.snapshot()`로 health.account_handles·active_account_ids를 로드(Strategies.tsx 패턴 재사용).
- "모의 적용" 버튼 → "**계좌에 적용**"으로 라벨 변경, onClick → AccountPicker 열기(핸들 있으면). 핸들 0개면
  `alert("로컬앱에서 계좌를 등록·페어링한 뒤 적용할 수 있습니다.")`.
- AccountPicker onSelect(h) → `save(h.mode, h.account_id)`로 호출(save 시그니처에 account_ref 추가).
- `save(runMode, accountRef?)`: 정적 바스켓 confirm은 유지. `api.createStrategy(def, runMode, "ir", accountRef)` /
  `api.updateStrategy(id, def, mode, "ir", accountRef)`로 account_ref 전달.
- (정적 universe confirm은 mode 결정 후이므로 picker 다음에 위치.)

- [ ] **Step 2: 타입체크 + 빌드**

Run: `cd web && bunx tsc --noEmit && bunx vite build` → 에러 0.

- [ ] **Step 3: 커밋**

```bash
git add web/src/pages/IrBuilder.tsx
git commit -m "feat(web): 전략 적용 시 계좌 선택(모드=계좌) (P5-4)"
```

---

## Task 4: 바인딩 표시 + 재바인딩 (Strategies 카드 · StrategyDetail)

**Files:**
- Modify: `web/src/pages/Strategies.tsx` (카드 ~206-240, snapshot에서 핸들 맵)
- Modify: `web/src/pages/StrategyDetail.tsx` (헤더 ~120, Stats 탭 ~434)

- [ ] **Step 1: 구현**

- 공용 헬퍼: `account_ref` → health.account_handles에서 `{nickname, mode}` 룩업(없으면 "미바인딩"/"옛 계좌").
- Strategies 카드: `.sc-meta` 아래에 계좌 한 줄("계좌: {별명}" + mode 배지, 미바인딩이면 회색 "계좌 미선택").
- StrategyDetail: Stats 탭 끝에 "실행 계좌" 섹션 — 바인딩 계좌(별명·mode 배지) + **"전환"** 버튼 → AccountPicker →
  `api.updateStrategy(id, def, h.mode, "ir", h.account_id)`로 재바인딩(승격=실전 계좌 선택, confirm은 picker가 처리).
- 모든 스타일 DESIGN.md 토큰.

- [ ] **Step 2: 타입체크 + 빌드 + 커밋**

Run: `cd web && bunx tsc --noEmit && bunx vite build` → 에러 0.
```bash
git add web/src/pages/Strategies.tsx web/src/pages/StrategyDetail.tsx
git commit -m "feat(web): 전략 바인딩 계좌 표시 + 전환(rebind) (P5-4)"
```

---

## Task 5: 검증 (브라우저 — 가능 범위)

- [ ] **Step 1: 빌드·타입 그린**

Run: `cd web && bunx tsc --noEmit && bunx vite build` → 0 에러.

- [ ] **Step 2: 브라우저 렌더 확인 (preview_tools)**

dev 서버 기동 후: ① /strategies·/builder 정상 렌더(콘솔 에러 0) ② "계좌에 적용" 클릭 → 핸들 0개 환경에서
"계좌 등록·페어링" 안내 표면(핸들 주입 불가 환경 — 안내 경로·미바인딩 표시까지 검증) ③ 카드/상세에 "계좌 미선택"
표시. **한계 명시:** 실제 핸들 선택→account_ref 세팅 풀플로우는 페어링된 로컬앱이 핸들을 보고해야 가능 —
dev에선 미바인딩/안내 경로만 검증(풀플로우는 라이브 E2E·사용자측).

- [ ] **Step 3: DESIGN 일관성**

추가 UI가 DESIGN.md 토큰만 쓰는지 점검(인라인 hex 0). 기존 `.sc-badge`/`.account-menu`/`.seg` 재사용 확인.

---

## 비범위 / 후속
- **자산군 커버리지 disable**(핸들이 전략 요구 자산군 미커버 시 선택불가): 클라이언트 instrument_category 필요 —
  P5-4는 전 핸들 표시(로컬 P1 coverage 게이트·P3 가드가 실행시 차단). 필요 측정 시 후속.
- **skip_wrong_account 타임라인 표시**: 로컬이 이미 decision으로 push — 웹 타임라인/모니터 표시는 후속.

## Self-Review
- **Spec §3.2 커버리지:** 계좌 선택=적용(Task 3)·모드=계좌(handle.mode)·승격=재바인딩(Task 4)·배지(Task 4)·핸들 0개 안내(Task 3). ✓
- **Placeholder:** Task 2/3/4는 컴포넌트·통합이라 코드 골격+정확한 props/호출 명시. 정적 universe confirm·snapshot fetch는 기존 패턴 재사용 *명시 지시*. UI 세부는 DESIGN 토큰+브라우저 반복(UI 특성).
- **타입 일관성:** `AccountHandle`·`account_ref?: string|null` — types·api·picker·builder·display 동일. save(runMode, accountRef?) 시그니처 일관.
- **DESIGN 준수:** 토큰만·인라인 hex 금지 명시. 기존 badge/menu/seg 재사용.
- **검증 한계 정직:** dev에 핸들 주입 불가 → 미바인딩·안내 경로만 브라우저 검증, 풀플로우는 라이브 E2E.

# Phase 4 — 코드 변경 리뷰

이번이 첫 정식 review라 main 기준 diff가 의미 없음 → 최근 commit 10개 + Phase 1·2에서 식별된 코드 결함 deep dive.

## 최근 commit 10개

| commit | 분류 | 평가 |
|---|---|---|
| 8b7739c | review infra | ✅ 본 리뷰의 산출물 — self-evaluated |
| 21f1ad4 | fix | ✅ Phase 42-3 페어 stale 차단, Dashboard `effectiveSnap` + Monitor `paired` gate 정확 |
| d0b2da1 | hotfix | ✅ schemas.py에 alert_on_reconcile_drift 추가, c373686 누락 보강 |
| c373686 | feat | ✅ Phase 42-2 위험 한도 UI — backend·frontend·types 모두 일관 |
| db4ad32 | fix | ✅ Postgres pool_pre_ping + pool_recycle + TCP keepalives, 결정적 패턴 |
| 3e3fea2 | fix | ✅ ConditionBuilder dead code 제거 (Vercel TS6133 해소) |
| 8d5fa0d | chore | ✅ Vercel rebuild trigger |
| e27081a | chore | ✅ gitignore + QA 산출물 |
| 476db30 | feat | (Phase 2에서 검증) Dashboard·Monitor·Backtest·Strategies UI |
| 537027d | feat | core 엔진 확장 — Phase 8 quant 도메인 검증으로 회귀 검증 |

## 즉시 적용된 fix

### F-4-1 — `MonitorCards.tsx` `HealthCard` `Date.now()` in render

**Before** (line 256):
```tsx
export function HealthCard(...) {
  const items: ... = [];
  const now = Date.now();  // ← render마다 다른 값, React purity 위반
  ...
}
```

**After**:
```tsx
import { useEffect, useState } from "react";
...
export function HealthCard(...) {
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);
  ...
}
```

ESLint `react-hooks/impure-call` 해소 + "방금 전 / N분 전 / N시간 전" 라벨이 30s 주기로 갱신됨 (이전엔 parent re-render 시점에만).

### F-4-2 — `Backtest.tsx:111` function hoisting lint 위반

**Before**:
```tsx
useEffect(() => {
  if (tab === "history" && !historyLoaded) loadHistory();  // ← 111: function 미선언
}, [tab]);

function loadHistory() { ... }  // 114
```

**After**: 함수 정의를 useEffect 위로 이동. JS function declarations은 hoist되니 동작 변화 없음, lint만 해소.

### F-4-3 — `routers/backtest.py:6` `select` import

(Phase 1 P0에서 이미 적용 — 이번 phase 산출물 commit에 포함)

## ESLint 결과

- 직전: 12 errors
- F-4-1·F-4-2 적용 후: **10 errors**
- 남은 10건 — case-by-case backlog (Phase 9)

| 잔여 결함 | 위치 | 의도성 평가 |
|---|---|---|
| setState in effect | `auth.tsx:20` | 인증 상태 부트스트랩 — 정당 |
| setState in effect | `Monitor.tsx:54` | initial load + interval — 정당, useCallback 래핑으로 lint 회피 가능 |
| setState in effect | `Strategies.tsx:52` | 헤더 모드 → 필터 동기화 — 정당 |
| setState in effect | `Strategies.tsx:65` | `useEffect(load, [])` — load() 내부 setState OK 패턴 |
| setState in effect | `Backtest.tsx:964` | CapitalInput 외부 prop 동기화 — controlled component 정당 패턴 |
| only-export-components | `auth.tsx:57`, `mode.tsx:45`, `SymbolPicker.tsx:7,19` | HMR 영향만, 런타임 무관. context exports + components 분리하면 해소 |
| explicit-any | `Login.tsx:37` | Google `window.google` 타입 unknown, 안전 |

## `kis_master_cache.py` `_state` mypy 노이즈 — runtime 안전 확인

`_state` 변수는 의도된 heterogeneous dict — symbols/by_symbol/fetched_at/n_*가 모두 다른 타입. mypy가 union으로 추론해서 15+ errors 발생하지만 runtime 안전. 모든 접근이 `_lock`으로 보호됨 (concurrency safe).

권장 (Phase 9 backlog):
```python
from typing import TypedDict

class _StateDict(TypedDict):
    symbols: set[str]
    by_symbol: dict[str, dict]
    fetched_at: Optional[datetime]
    n_kospi: int
    n_kosdaq: int
    n_nas: int; n_nys: int; n_ams: int; n_tse: int; n_hks: int

_state: _StateDict = {...}
```

→ mypy 15건 해소, 새 필드 추가 시 타입 강제.

## 추가 발견

### 발견 없음 카테고리

- SQL 안전성: SQLModel + 파라미터 바인딩, raw SQL 흔적 없음 ✅
- LLM trust boundary: LLM 호출 없음 (해당사항 없음)
- 조건부 부작용: useEffect 안 동기 setState는 위 setState-in-effect로 잡힘
- race condition: `_lock` 사용, 명시적 동기화 패턴 ✅
- error handling 누락: 다수 try/except + setErr 패턴 일관
- naming consistency: 한글 docstring 일관, 함수명 snake_case (Python) / camelCase (TS) 일관

## 다음

Phase 5 — Codex 독립 시선 (skip — Phase 0에서 codex 미설치 확인).

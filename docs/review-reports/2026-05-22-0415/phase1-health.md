# Phase 1 — Health 점수

## 종합

| 영역 | 점수 | 비고 |
|---|---:|---|
| Server lint (ruff) | 7/10 | 24 errors → 자동수정 3 + 즉시 fix 1 → 20 cosmetic 남음 |
| Server type (mypy) | 6/10 | import-untyped 노이즈 + `kis_master_cache._state` 진짜 의심 |
| Web type (tsc) | 10/10 | 0 errors |
| Web lint (eslint) | 6/10 | 12 errors (React 19 hooks 규칙 미준수 위주) |
| **평균** | **7.3/10** | |

## P0 Critical — 즉시 수정 완료

### 1. `routers/backtest.py:137` — `select` import 누락 (런타임 NameError)

- 영향: `GET /backtest/runs` (Backtest 페이지 history 탭) 호출 시 500 에러
- 원인: `from sqlmodel import Session`만 import, `select` 누락
- 수정: `from sqlmodel import Session, select`
- 상태: ✅ 적용 완료. ruff F821 해소 확인.

## P1 High — 즉시 수정 권장 (Phase 2/4에서)

### 2. `components/MonitorCards.tsx:256` — `Date.now()` in render body

- 영향: 매 render 시 다른 값 → React memoization 무효화, 일관성 깨짐
- 원인: `HealthCard` 컴포넌트가 render 중 `const now = Date.now()` 호출
- 권장: `useState(() => Date.now())` 또는 useMemo + 30초 refresh interval
- 처리: Phase 2 design-review에서 cosmetic·구조 같이 정리

### 3. `pages/Backtest.tsx:111` — function hoisting (lint 위반)

- 영향: 동작은 OK (JS function declarations are hoisted). React 19 strict mode 잠재 위험.
- 원인: `useEffect` 안에서 `loadHistory()` 호출, 함수 정의는 line 114
- 권장: 함수를 effect 위로 이동 또는 `useCallback`으로 wrap
- 처리: Phase 4 코드 리뷰

### 4. `server/app/kis_master_cache.py` — `_state` 변수 union 타입 (15+ mypy errors)

- 영향: mypy가 `int | dict | set | None` 4종 union으로 추론. 진짜 런타임 type confusion 가능.
- 원인: `_state` 변수가 함수마다 다른 값 할당
- 처리: Phase 4 코드 리뷰에서 깊이 분석

## P2 Medium — Phase 9 backlog

### 5. setState in useEffect (6건)

React 19의 새 규칙 `react-hooks/set-state-in-effect`. cascading render 성능 저하 가능.
위치:
- `src/auth.tsx:20`
- `src/pages/Monitor.tsx:54`
- `src/pages/Strategies.tsx:52, 65`
- `src/pages/Backtest.tsx:964`
- (인라인 일부 — 일부는 의도된 동기화 패턴일 수 있음, case-by-case 검토 필요)

### 6. Server ruff E701/E702 (20건)

`if cond: continue` 같은 한 줄 스타일. 코드 안전성 영향 없음.
주로 `app/technical_cache.py` (다수) + `app/preview_engine.py`.
일괄 정리 한 번에 fix 가능 (10분).

### 7. Server mypy db.py:21-24 dict-item (4건)

`connect_args` dict가 sqlalchemy stub의 `dict[str, bool]` 시그니처와 충돌
(Phase 42-1에서 추가한 TCP keepalives int 값). 동작은 정상.
`connect_args: dict = {...}` 명시적 annotation으로 해결 가능.

## P3 Low — 무시 가능

- `react-refresh/only-export-components` (4건): HMR만 영향, 런타임 무관
- `pages/Login.tsx:37` `any` (1건): catch (e: any) 패턴
- mypy `import-untyped` (다수): pandas·requests·FinanceDataReader stub 부재 → `types-requests` 등 설치만으로 해소

## ruff 자동수정 적용 결과

3건 자동수정:
- `server/app/preview_engine.py`
- `server/app/routers/dataset.py`
- `server/app/routers/market.py`

`F821 select import 누락 수동 fix`와 함께 phase 끝에 별도 commit 예정.

## 추세 비교

직전 리뷰 폴더 없음 (이번이 첫 정식 리뷰). 다음 리뷰부터 비교 base.

## 다음

Phase 2 — design-review (7개 페이지).

# Phase 2 — Design & UX

브라우저 검증: http://localhost:5173/ (Vite preview). 기존 세션 토큰으로 로그인 상태 진입.

## 페이지별 점수 (0-10)

| 페이지 | 점수 | 주요 평가 |
|---|---:|---|
| Login | 8 | 폼 깔끔, Google OAuth 분기, error 표시 ok. minLength=6 약한 정책 (Phase 7에서 검토) |
| Pair | 6 → **8** | 빈 상태 회색 한 줄 → `.empty-state` 카드화 fix 적용 |
| Dashboard | 9 | Phase 42-3 페어링 차단 동작 확인. "오늘의 액션 아이템" + 자산곡선 + 시스템 상태 잘 정돈 |
| Monitor | 9 | 페어링 차단 + 액션 4개 buttons disabled(opacity 0.5 + cursor not-allowed) 확인 |
| Strategies | 9 | 빈 상태 카드 패턴 모범 (제목 + 안내 + CTA 버튼) |
| Backtest | 8 | ConditionBuilder 문장형 빈칸 차별점 확인. "마켓플레이스V2" 라벨 의문 |
| Settings | 7 → **9** | Phase 42-2 위험 한도 UI ok. 빈 상태 회색 한 줄 → `.empty-state` 카드화 fix |
| **평균** | **8.6** | 즉시 fix 2건 적용 후 8.6/10 |

## DESIGN.md 일관성 검증

| 토큰 | 값 (DESIGN.md) | 실측 | 결과 |
|---|---|---|---|
| `--bg` | `#f4f5f7` | `rgb(244, 245, 247)` | ✅ |
| `.panel` bg | `#ffffff` | `rgb(255, 255, 255)` | ✅ |
| `.panel` margin-bottom | `18px` | `18px` | ✅ |
| `.empty-title` font-size/weight | `15px / 700` (정의됨) | `15px / 700 / center` | ✅ |

## 즉시 적용된 cosmetic fix (commit 예정)

### Fix #1 — Settings `연결된 기기 (0)` 빈 상태 카드화

`src/pages/Settings.tsx:101-103` 회색 한 줄(`<p className="muted">아직 연결된 기기가 없습니다.</p>`)을 `.empty-state` 패턴으로 교체.

**Before:**
```tsx
<p className="muted">아직 연결된 기기가 없습니다.</p>
```

**After:**
```tsx
<div className="empty-state">
  <p className="empty-title">아직 연결된 기기가 없습니다</p>
  <p>위 칸에 로컬앱이 표시한 8자리 페어링 코드를 입력하면 기기가 등록됩니다.</p>
</div>
```

DESIGN.md "빈 상태(`.empty-state`): 회색 한 줄 금지. 제목 + 안내 문단 + CTA 버튼 카드." 원칙 부합. 브라우저 검증 완료 (15px/700/center align).

### Fix #2 — Pair `연결된 기기` 동일 패턴

`src/pages/Pair.tsx:93-95`도 동일 회색 한 줄. 일관성 위해 같은 fix 적용. Settings에서 페어링 가능하니 Pair는 deep-link 경유만 도달하지만, 도달 시 같은 경험이어야 함.

## 발견된 결함 (deferred to Phase 9)

### High

#### H-2-1 — `MonitorCards.tsx:256` `Date.now()` in render body

(Phase 1 H-2와 동일 항목) render 중 `const now = Date.now()` 호출 → React purity 위반.
권장: `useState(() => Date.now())` + 30초 refresh interval, 또는 `useMemo` + 시간 dependency.
이건 구조적 변경이라 Phase 4 코드 리뷰에서 종합 fix.

### Medium

#### M-2-1 — Backtest "마켓플레이스V2" 라벨

`pages/Backtest.tsx` 탭 라벨 중 "마켓플레이스V2". 사용자에게 "V2"가 의미 없음 — 내부 prototype 표식인 듯.
권장: "마켓플레이스" (V1 제거 시) 또는 "마켓플레이스 (베타)" / 일반 사용자 숨김.
Phase 4 코드 리뷰에서 의도 확인 후 결정.

#### M-2-2 — Login `minLength={6}` 약한 패스워드 정책

`pages/Login.tsx:92` `<input type="password" required minLength={6}>`. NIST 권장 12자, OWASP 권장 최소 10자.
사용자 패스워드는 자동매매 자산 보호에 직결. **Phase 7 cso에서 critical로 분류 권장**.

### Low

#### L-2-1 — Phase 1의 setState in effect 6건 — 페이지별 영향
- `Strategies.tsx:52`: 헤더 모드 변경 시 필터 자동 동기화 — 의도된 effect-sync 패턴
- `Monitor.tsx:54`: 초기 load + interval 시작 — 의도됨
- `Backtest.tsx:964`: history 탭 lazy load — 의도됨
- 모두 React 19 새 규칙이 너무 strict함. effect 안에서 직접 setState 호출하더라도 useCallback wrap 또는 separate function으로 lint warn 회피 가능.
Phase 9 backlog로.

## 모바일 반응형

브라우저 viewport 변경 검증 미실행 (시간 예산). DESIGN.md에 본문 최대폭 1100px + 좌측 220px 사이드바 명시. 모바일 별도 처리 없음 → 차후 별도 phase (Phase 43 후보).

## 접근성 (간이 평가)

- 키보드 nav: 사이드바·메인·tablist에 정상 tab order 추정 (정밀 평가 미실행)
- 색 대조: 텍스트 `#1a1d23` on `#ffffff` → AAA 대비. accent `#4f46e5` on `#ffffff` → AA 대비.
- focus indicator: 명시적 outline 미검증 (별도 패스 필요)

→ a11y 정식 audit은 별도 phase 권장 (axe-core 자동 + 수동 키보드 테스트).

## 다음

Phase 3 — qa (17개 사용자 시나리오).

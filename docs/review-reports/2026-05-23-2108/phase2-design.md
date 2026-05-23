# Phase 2 — Visual & UX Design (코드 inspection)

dev 서버: `mcp__Claude_Preview` 통해 백그라운드 기동 (port 5173).
대상: 7개 페이지 (Login, Pair, Dashboard, Monitor, Strategies, Backtest, Settings).
검증 깊이: 토큰 위반·빈 상태 정의 누락 grep + Dashboard 로드 확인. Visual diff는 Phase B UI 작업 시 보강.

## 페이지별 점수 (0-10)

| 페이지 | 점수 | 근거 |
|---|---|---|
| Login | 8 | 빈상태/loading/error 3건. 입력 보더 토큰 적용 (직전 cycle) |
| Pair | 8 | 디바이스 페어 흐름. 4상태 정의 |
| Dashboard | 7 | **빈상태/loading/error 텍스트 grep 0건** — but Promise.all + setLoaded(true) 패턴으로 빈상태는 component 단위로 처리됨(EquityChart, 활성전략 listitem). 부분적 OK |
| Monitor | 7 | **cancelled chip만 hardcoded #f3f4f6 — fix됨** (D-02). 나머지 토큰 일관 |
| Strategies | 8 | 4상태 정의 |
| Backtest | 7 | 4상태 텍스트 12건. `set-state-in-effect` 1건 (CapitalInput) — PR-3 |
| Settings | 8 | 4상태 텍스트 3건 |
| **종합 UX/Visual** | **7.5** | 직전 cycle의 token 정렬은 대부분 적용, 누락 2건 발견·fix |

## 발견 결함

| ID | 위치 | 카테고리 | 처리 |
|---|---|---|---|
| **D-01** (High) | `web/src/components/EquityChart.tsx:39,44,47,49` | DESIGN.md 토큰 위반 — 인디고 `#4f46e5`(직전 액센트), `#eef0f3`/`#6b7280`/`#9ca3af` 회색 hardcoded | **closed** — 토큰값 직접 인라인(recharts SVG 한계 주석 명시) |
| **D-02** (Low) | `web/src/pages/Monitor.tsx:556` | EventBadge `cancelled` chip만 `#f3f4f6` hardcoded(나머지는 토큰) | **closed** — `var(--border)` |
| D-03 (Medium) | `web/src/pages/Dashboard.tsx` | 페이지 자체에 빈상태/error/loading 텍스트 grep 0건 (component 단위 처리에 의존) | Phase 9 surface (구조적 — deferred) |
| D-04 (Medium) | `web/src/pages/Backtest.tsx:1046` (CapitalInput) | `useEffect(() => { if (!focused) setDraft(...) })` → react-hooks/set-state-in-effect | Phase 9 surface (controlled input 정당화 여부 — 결정 필요) |
| D-05 (Low) | 4 파일 react-refresh/only-export-components | fast refresh 영향. dev only | Phase 9 backlog |

## 즉시 수정 commit hash

- `<TBD after commit>` — `style(web): DESIGN.md 토큰 누락 정렬 (D-01,D-02)`

## 4원칙 자기검토 (Phase 2)

| 원칙 | 자기검토 |
|---|---|
| 근본원인 | D-01 fix는 단순 색 교체가 아니라 토큰 일관성 회복 ✅ |
| Over-eng | recharts CSS var 미수신 한계는 주석으로 명시 — 추상화 추가 없음 ✅ |
| Over-think | 4 + 1 값 교체. 새 토큰 도입 검토는 차단(기존 var(--border) 재사용) ✅ |
| 검증된 해결책 | preview_snapshot으로 Dashboard 로드 확인 + tsc/lint pass 유지 ✅ |

## Deferred to Phase 9

- D-03 Dashboard 통합 빈상태 정의 (구조 변경 — 사용자 결정)
- D-04 set-state-in-effect — controlled input 패턴 vs effect rewrite
- D-05 react-refresh 위반 (4 파일) — 코드 분리 vs 무시

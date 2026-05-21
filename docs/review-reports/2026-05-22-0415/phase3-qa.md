# Phase 3 — QA 사용자 시나리오

dev 환경: web=http://localhost:5173 (Vite preview), api=http://localhost:8000 (사용자 사전 실행).
인증: 기존 세션 토큰(qp_token, exp=2026-05-26) 활용.

## 시나리오 결과 표

✅ = 브라우저/API 실행 검증 / 🔵 = 코드+UI 검증 (실행 미시도) / ⚠️ = 결함 발견 / ⚪ = 미시도

| # | 시나리오 | 결과 | 비고 |
|---:|---|---|---|
| 1 | 신규 가입 → 로그인 → Google OAuth | 🔵 | Login.tsx 코드 분석. minLength=6 약함 → Phase 7 cso로 |
| 2 | 디바이스 페어링 (코드 입력 + 승인) | 🔵 | Settings·Pair UI 검증. 빈 상태 fix 적용 |
| 3 | 첫 전략 생성 (ConditionBuilder) | 🔵 | Backtest 페이지 ConditionBuilder UI 검증 |
| 4 | 백테스트 실행 → 결과 확인 → 저장 | ⚠️ | 백테스트 history API 500 — **C-3-1 참조** |
| 5 | 모의투자 모드 전환 → 자동매매 시작 | 🔵 | 모의↔실전 tablist 검증 |
| 6 | 실시간 모니터링 (Dashboard·Monitor) | ✅ | Phase 42-3 페어 차단 동작 확인 |
| 7 | 위험 한도 설정 (Settings) | ✅ | Phase 42-2 UI 검증, 입력 가능 |
| 8 | 페어링 해제 → snapshot stale 차단 | ✅ | Dashboard 총평가/킬스위치 "-"/"—" 표시 |
| 9 | 로그아웃 → 재로그인 → 상태 복원 | 🔵 | useAuth 코드: localStorage qp_token + qp_mode 보존 |
| 10 | dark mode 토글 | ⚪ | DESIGN.md 미정의, dark mode 미지원 |
| 11 | 모바일 반응형 | ⚪ | DESIGN.md 미정의, 차후 phase |
| 12 | 키보드 nav (tab order) | ⚪ | a11y audit 별도 |
| 13 | 에러 토스트 표시 | 🔵 | Login·Settings는 setErr → 인라인 표시. global toast 미존재 |
| 14 | 로딩 상태 표시 | ✅ | "데이터 불러오는 중…" 등 명시 |
| 15 | 빈 상태 UX | ✅ | Strategies·Settings(fix후)·Pair(fix후) `.empty-state` 카드 |
| 16 | 외부 webhook 알림 설정 검증 | ⚪ | UI만 있고 실제 webhook trigger는 백엔드 cron 필요 |
| 17 | 페어링 후 모니터 데이터 표시 | ⚪ | 실제 로컬앱 미실행 — 시뮬 시나리오 |

## 발견 결함

### 🔴 Critical

#### C-3-1 — `GET /backtest/runs` 500 Internal Server Error

- **재현**:
  ```bash
  curl -i -H "Authorization: Bearer <valid_jwt>" http://localhost:8000/backtest/runs
  → HTTP 500
  ```
- **원인**: Phase 1 P0에서 발견한 `select` import 누락 (`routers/backtest.py`). 코드 fix는 적용됐으나 dev server reload 안 됨 (manual 재시작 필요).
- **production 영향**: Railway 배포 서버의 코드도 동일하면 production에서 Backtest 페이지 "결과 리포트" 탭 열 때 500 발생. **사용자가 실제로 본 적 있는지 확인 필요.**
- **수정 상태**: code 수정 완료 (`from sqlmodel import Session, select`). server restart 후 / 다음 Railway deploy 후 자동 해소.
- **검증 명령** (server restart 후):
  ```bash
  curl -i -H "Authorization: Bearer <token>" http://localhost:8000/backtest/runs
  → HTTP 200 + JSON array
  ```

### 🟠 High

(없음 — 다른 critical UX 결함 발견 안 됨)

### 🟡 Medium

#### M-3-1 — global toast/notification 시스템 부재

(Phase 9 backlog로) Login·Settings 등은 component-local `setErr`로 인라인 표시. 비동기 액션 (자동매매 가동·페어링 성공) 결과를 전역 알림으로 보여주는 패턴 없음. backlog candidate.

#### M-3-2 — 모바일 반응형 미지원

DESIGN.md 본문 폭 1100px + 좌측 220px sidebar. 모바일 viewport에서 가로 스크롤 또는 깨짐 예상. 별도 phase 권장.

### 🟢 Low

#### L-3-1 — dark mode 미지원

DESIGN.md 명시 안 함. 사용자 요청 없으면 우선순위 낮음.

## 다음

Phase 4 — 코드 변경 리뷰 (diff 기반).

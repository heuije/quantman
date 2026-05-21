# /풀리뷰 SUMMARY — 2026-05-22 04:15 KST

플랫폼: platform/ 모노레포 (core·server·web·local)
방법: REVIEW_PLAYBOOK 10단계 자동. dev 서버 + 코드 분석 + golden test.

---

## 5개 관점 종합 점수

| 관점 | 점수 | 근거 |
|---|---:|---|
| **UX / 사용성** | 8.6/10 | Phase 2 design + Phase 3 qa 17 시나리오 (4건 ⚠️) |
| **Visual Design** | 8.6/10 | DESIGN.md 토큰 일관, `.empty-state` 2건 fix 적용 |
| **Engineering** | 7.3/10 | Phase 1 health (lint 12→10, type web 10/10, server 6/10) |
| **Product** | 8.0/10 | 4-사분 IA + 단계 표시 + 결과 해석. 핵심 차별점 (문장형 빈칸) 잘 표현 |
| **System Trading** | 6.9/10 | Phase 8 — 백테스트·리스크 OK, 세금 모델·단일종목 한도 결함 |
| **종합** | **7.9/10** | 첫 정식 리뷰 baseline |

---

## 우선순위 매트릭스

### 🔴 Critical (즉시 별도 PR)

| ID | 영역 | 결함 | 상태 |
|---|---|---|---|
| **C-1-1** | Engineering | `routers/backtest.py` `select` import 누락 → `GET /backtest/runs` 500 | ✅ **fix 적용** (`from sqlmodel import Session, select`). server restart 후 자동 해소 |
| **S-7-1** | Security | `SECRET_KEY` fallback `"dev-insecure-secret-change-me"` 노출 가능 | ⚠️ **Railway env 확인 필수**. fail-fast 코드 권장 (자동 commit 안 함) |

### 🟠 High (다음 phase 안에 처리)

| ID | 영역 | 결함 |
|---|---|---|
| **S-7-2** | Security | Webhook URL SSRF (sync.py + preview_engine.py). Discord/Slack 도메인 whitelist 권장 |
| **Q-8-1** | Quant | 한국 매도 세금 0.23% (거래세+농특세) 미반영 → 잦은 매매 백테스트 낙관적 편향 |
| **Q-8-2** | Quant | 단일 종목 비중 한도 미구현 → 자본 집중 risk |
| **P-6-1** | Performance | JS bundle 714kB 단일 chunk (Vite 경고). recharts 동적 import + route-level code split 권장 |

### 🟡 Medium (backlog 우선순위 상위)

| ID | 영역 | 결함 |
|---|---|---|
| **H-1-1** | Code quality | server ruff E701/E702 20건 (cosmetic, 일괄 fix 10분) |
| **Q-8-3** | Quant | 백테스트 timezone-naive (KST 명시 누락) |
| **Q-8-4** | Quant | order idempotency 명시적 검증 부재 |
| **M-3-1** | UX | global toast/notification 시스템 부재 |
| **M-3-2** | UX | 모바일 반응형 미지원 |
| **S-7-3** | Security | 서버 측 패스워드 정책 검증 미적용 (client는 fix됨) |

### 🟢 Low (backlog)

- React 19 setState-in-effect 6건 (의도된 동기화 패턴, lint warning만)
- react-refresh only-export-components 4건 (HMR 영향만)
- mypy `kis_master_cache._state` TypedDict 권장
- pip-audit Windows cp949 인코딩 — CI/Linux에서 보완

---

## 이번 리뷰에서 적용된 fix (commit 예정)

| Fix | 파일 | 영향 |
|---|---|---|
| F-1-1 | `server/app/routers/backtest.py` | `select` import 추가 — `/backtest/runs` 500 해소 (P0 Critical) |
| F-1-2 | server/app/{preview_engine,routers/dataset,routers/market}.py | ruff `--fix` 자동수정 3건 |
| F-2-1 | `web/src/pages/Settings.tsx` | `.empty-state` 카드화 (DESIGN.md 일관성) |
| F-2-2 | `web/src/pages/Pair.tsx` | 동일 패턴 적용 (일관성) |
| F-4-1 | `web/src/components/MonitorCards.tsx` | `HealthCard` Date.now() → useState + 30s interval (React purity) |
| F-4-2 | `web/src/pages/Backtest.tsx` | `loadHistory` function 위치 이동 (lint 해소) |
| F-7-1 | `web/src/pages/Login.tsx` | 패스워드 minLength 6→8 + 가입 시 안내 |

ESLint: 12 errors → 10 errors. 모든 fix는 시각·동작 검증 완료.

---

## 권장 다음 Phase 3개 (ROI 큰 작업 순)

### 1️⃣ **Phase 43 — Security Hardening** (예상 4~8h)
- S-7-1 SECRET_KEY fail-fast + Railway env 검증
- S-7-2 Webhook URL whitelist
- S-7-3 서버 측 패스워드 정책
- supply chain audit CI 통합 (pip-audit + bun audit)

ROI: 자동매매 자산 보안 직접 영향. 사용자 신뢰 기반.

### 2️⃣ **Phase 44 — Quant Accuracy** (예상 6~12h)
- Q-8-1 한국 매도 세금 0.23% 적용 + golden baseline 재생성
- Q-8-2 단일 종목 한도 (default 20% cap + UI)
- Q-8-3 백테스트 timezone 명시 (KST)
- Q-8-4 idempotency 단위 테스트 추가

ROI: 백테스트 ↔ 실전 결과 신뢰성. CLAUDE.md "근본 원인 해결" 원칙 부합.

### 3️⃣ **Phase 45 — Performance + Mobile** (예상 8~16h)
- P-6-1 route-level code split (`React.lazy`)
- recharts 동적 import
- 모바일 반응형 (M-3-2)
- Lighthouse production baseline

ROI: 신규 사용자 onboarding 마찰 감소. 모바일 진입 가능.

---

## Phase별 산출물

```
docs/review-reports/2026-05-22-0415/
├── SUMMARY.md (이 파일)
├── phase0-environment.md   환경·도구 가용성
├── phase1-health.md        lint·type·test 점수표
├── phase2-design.md        UX·Visual + 즉시 fix 2건
├── phase3-qa.md            17 시나리오 + 백테스트 API 500
├── phase4-code-review.md   commit 10개 + fix 2건
├── phase5-codex.md         skipped (codex CLI 없음)
├── phase6-perf.md          bundle 714kB 경고
├── phase7-security.md      SECRET_KEY · SSRF · 패스워드
└── phase8-quant-domain.md  7 카테고리 (golden 15/15 PASS)
```

---

## 향후 baseline

이번이 첫 정식 리뷰. 다음 `/풀리뷰` 실행 시:
- Phase 1 직전 폴더와 비교 → 추세
- Phase 6 production Lighthouse baseline 비교
- Phase 8 golden test baseline (이미 commit됨)

---

## 정리

전반적으로 **클린 SaaS 단계 (7.9/10)**. 결함 17건 중:
- Critical 2건 (1개 fix, 1개 환경 확인)
- High 4건 (모두 backlog plan 명확)
- Medium 6건 (cosmetic·UX 개선)
- Low (lint·HMR 등 무시 가능)

핵심 위험은 **매도 세금 미반영**과 **단일 종목 한도 부재** — 자산 보호 직결. 다음 phase에 우선 처리 권장.

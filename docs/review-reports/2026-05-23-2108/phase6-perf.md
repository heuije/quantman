# Phase 6 — 성능 baseline (web build)

대상: production build. Production URL 측정은 별도 cycle(Vercel + benchmark CLI 필요).

## Build 결과

| 항목 | 0252 | 2108 | 변화 |
|---|---|---|---|
| 빌드 시간 | ~1.5s | **1.36s** | -0.14s ✅ |
| index.html | 1.39 kB | 1.39 kB | = |
| CSS bundle (gzip) | ~7.5 kB | **7.54 kB** | = |
| **JS bundle (gzip)** | **216 kB** | **216.35 kB** | = (변화 없음) |
| **JS bundle (raw)** | **735 kB** | **735.10 kB** | = (변화 없음) |
| chunk warning (>500 kB) | 발생 | 발생 | 미해결 |

## 발견 결함

**P-01 (Medium, 직전 cycle 미해결 backlog)** — 단일 chunk 735 kB.

- 위치: `dist/assets/index-CfaHrNJE.js`
- 원인: recharts(차트) + 모든 페이지 컴포넌트가 단일 entry. dynamic import 미사용.
- 영향: cold load 첫 페인트가 느려짐 (특히 mobile 3G).
- **근본 해결:** `React.lazy` + `Suspense`로 페이지별 chunk split. recharts는 EquityChart 하위 페이지(Dashboard/Backtest)에서만 import.
- 게이트: Phase 9 권장. Phase B 후보로 surface.

## Production 측정 누락

- Vercel production URL의 Core Web Vitals (LCP/CLS/INP) 측정 없음.
- 직전 cycle에서도 미측정 — `/benchmark` skill·benchmark CLI 환경 부재.
- **검증 불가** — 사람이 별도 cycle에서 lighthouse 또는 webpagetest 실행 필요.

## 4원칙 자기검토 (Phase 6)

| 원칙 | 자기검토 |
|---|---|
| 근본원인 | bundle size 회귀 없음 ✅. P-01 deferred 정당 (구조 변경 → 사용자 결정) |
| Over-eng | benchmark CLI 강제 도입 안 함. dev 서버 측정으로 갈음 ✅ |
| Over-think | bundle size 1줄 비교 ✅ |
| 검증된 해결책 | build 출력 마지막 줄로 회귀 없음 ✅ |

# Phase 6 — Performance / Web Vitals

dev (5173) baseline + `vite build` 결과 측정. production Lighthouse는 별도 권장.

## Bundle Size

```
dist/index.html                   0.68 kB │ gzip:   0.43 kB
dist/assets/index-1rumF1l7.css   34.06 kB │ gzip:   6.49 kB
dist/assets/index-CZVQ4GKV.js   714.48 kB │ gzip: 210.73 kB  ← Vite 경고: >500 kB
```

빌드 시간: 1.33s (빠름).

### 🟠 High — JS 단일 chunk 714 kB

Vite 빌드 경고:
> Some chunks are larger than 500 kB after minification. Consider:
> - Using dynamic import() to code-split the application
> - Use build.rolldownOptions.output.codeSplitting to improve chunking

**영향**:
- 모바일 3G/4G 환경에서 initial JS download 210 kB gzip = 1~2초 (사용자 첫 진입 시 blank screen)
- LCP 직접적 악화
- Vercel CDN은 빠르지만 한 청크라 cache 무효화 시 전체 재다운

**대응 권장** (P1, Phase 9):
1. `recharts` (가장 큰 dep) → 동적 import (Backtest 페이지에서만 필요)
2. Route-level code split — `React.lazy(() => import('./pages/Backtest'))` 적용 (현재 모든 페이지가 한 번에 번들)
3. `react-router-dom` v7 + lazy route

예상 효과: initial bundle 250~350 kB (gzip 80~120 kB) — 50% 감소.

## Navigation timing (dev 5173)

| metric | ms |
|---|---:|
| DOM interactive | 120 |
| DOM complete | 1526 |
| Load event | 1526 |
| paint events | (빈 배열, React SPA) |

dev는 HMR·sourcemap 영향 → production보다 느림. production 정확 측정 필요.

## Core Web Vitals 미측정

`web-vitals` 라이브러리 또는 Lighthouse 필요. 현재 dev 도구로 LCP·CLS·INP 직접 측정 불가.

**권장** (P2):
1. production URL 확보 후:
   ```powershell
   npx lighthouse https://<prod-url>/ --output=json --output-path=./lighthouse.json --chrome-flags="--headless"
   ```
2. 또는 `web-vitals` 패키지 추가 + Vercel Analytics 통합
3. 베이스라인 측정 후 매 review마다 회귀 비교

## Recharts 사이즈 확인

`web/package.json`에 `recharts: ^3.8.1` 의존. recharts v3 = ~150 kB minified. 단일 chunk 714 kB의 ~20% 차지.

## 다음

Phase 7 — cso (보안 audit).

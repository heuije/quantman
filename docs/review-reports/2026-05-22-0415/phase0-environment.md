# Phase 0 — 환경 점검

- 시각: 2026-05-22 04:15 KST
- 작업 디렉토리: `platform/`
- git HEAD: `8b7739c` chore(review): /풀리뷰 트리거 인프라 + 골든 백테스트 baseline
- git 변경사항: `M web/.gitignore` (이 리뷰와 무관, 이전 작업 흔적)

## 도구 가용성

| 도구 | 상태 | 비고 |
|---|---|---|
| Python | ✅ 3.12.7 | |
| pytest | ✅ 9.0.3 | |
| node_modules vite | ✅ 8.0.13 | bun PATH 없어 `node_modules/.bin/vite` 직접 호출 |
| npm | ✅ | |
| pnpm | ✅ | |
| git | ✅ | |
| codex CLI | ❌ | **Phase 5 skip** |

## Dev 서버

- 명령: `cd platform/web && ./node_modules/.bin/vite`
- URL: http://localhost:5175/ (5173·5174 점유 중이라 5175로)
- background job ID: `bfr1lpyka`

## 결과 폴더

`platform/docs/review-reports/2026-05-22-0415/`

## Baseline

- `tests/golden_baseline.json` ✅ 존재 (방금 commit 8b7739c에서 생성)
- 5개 전략 baseline 보유, Phase 8에서 회귀 검증 모드로 사용

## 다음

Phase 1 (health) 진입.

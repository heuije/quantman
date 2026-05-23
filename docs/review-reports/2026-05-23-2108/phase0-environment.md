# Phase 0 — 환경·신호 9종 baseline

작업 시각: 2026-05-23 21:08 KST
CWD: `platform/`
HEAD: `766d51f docs(claude): §4 코딩·협업 규칙을 핵심 4원칙으로 확장`
작업트리: clean (uncommitted 없음)
직전 baseline: `2026-05-23-0252` (16시간 전, FINDINGS_REPORT.md REPORT-ONLY)

## 환경

- Python 3.11+, Node + npm (web), bun 미사용 — 본 사이클은 `npm` 사용
- codex CLI: **미설치** → Phase 5 skipped
- dev 서버: **이번 사이클은 미기동** (Phase 2·3에서 필요 시 백그라운드 기동)

## 신호 9종 — 마지막 줄 증명 (직전 baseline 대비)

| # | 신호 | 0252 (직전) | **2108 (지금)** | 변화 |
|---|---|---|---|---|
| 1 | `pytest tests/golden_backtest.py -v` | `15 passed, 1 skipped, 30 warnings in 254.78s` | **`15 passed, 1 skipped, 2 warnings in 213.84s`** | 동일 pass·skip, warning −28, 시간 −41s ✅ |
| 2 | `pytest tests/ -q` | (미실행) | **`1 failed, 39 passed, 2 warnings in 6.54s`** | 새 baseline. **1 fail (test_unsupported_market)** 발견 |
| 3 | server `ruff check .` | `Found 42 errors. [*] 3 fixable` | **`Found 17 errors. [*] 2 fixable`** | −25 ✅ |
| 4 | server `mypy app` | `Found 82 errors in 22 files (checked 31)` | **`Found 86 errors in 24 files (checked 33 source files)`** | +4 ⚠️ 회귀 |
| 5 | web `npx tsc --noEmit` | (0줄) | **(0줄)** | 통과 유지 ✅ |
| 6 | web `npm run lint` | `✖ 10 problems (10 errors, 0 warnings)` | **`✖ 6 problems (6 errors, 0 warnings)`** | −4 ✅ |
| 7 | web `npm run build` | `built` + chunk warning (735KB) | **`built in 1.36s`, dist/assets/index-CfaHrNJE.js 735.10 kB │ gzip: 216.35 kB**, 동 warning | bundle 동일, P-02 backlog 미해결 |
| 8 | web `npm audit` | `found 0 vulnerabilities` | **`found 0 vulnerabilities`** | 유지 ✅ |
| 9a | server `pip-audit -r requirements.txt` | `decoding with 'cp949' codec failed` (exit 1) | **`No known vulnerabilities found`** | **X-01 fix 효과 확인** ✅ |
| 9b | local `pip-audit -r requirements.txt` | `decoding with 'cp949' codec failed` (exit 1) | **`No known vulnerabilities found`** | **X-01 fix 효과 확인** ✅ |

## 즉시 처리 후보

- **C-01 (High, PR-4 검증 누락)** — `tests/test_market_calendar.py::test_unsupported_market` 는 "KR"을 unsupported 시장으로 가정하지만 commit `993e819` (Q2+Q8 KR/US 캘린더 자동 갱신)에서 `_BUNDLE_FILES`에 KR 추가됨. 테스트 stale. **즉시 fix 가능** (가짜 마켓 코드로 변경).

- **C-02 (Medium, mypy 회귀)** — +4 errors / +2 files. 직전 cycle commits에 mypy ignore 처리 누락 흔적. Phase 1에서 분류.

## 4원칙 자기검토 (Phase 0)

| 원칙 | 자기검토 |
|---|---|
| 근본원인 | C-01은 stale 테스트 — 표면 픽스(KR→XX) 대신 실제 unsupported 마켓 코드를 쓰는 게 근본 ✅ |
| Over-eng | 신규 추상화 없음 ✅ |
| Over-think | 검사용 grep 외 추가 단계 없음 ✅ |
| 검증된 해결책 | 신호 9종으로 baseline 확립 ✅ |

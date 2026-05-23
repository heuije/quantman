# 개발 로그 — 플랫폼

각 cycle의 Phase B(개발) 결과를 기록한다. Phase A(리뷰)는 별도 `docs/review-reports/`.

## 2026-05-23-2108 (이번 cycle)

### 후보·선택

Phase A SUMMARY §9에서 5+ 게이트 batched 결정. AskUserQuestion 결과:
- ✅ 자동가능 batch (cosmetic 정리) — 선택
- 보류: Q-02 섹터 한도, Q-03 비용 비교 UI, P-01 chunk split

### 구현 commit 0c23f82 — `chore(quality): cosmetic batch`

| 영역 | 처리 |
|---|---|
| PR-1 (3건) | `analytics.py` 묵음 `except Exception: pass` 3건 → narrow exception + `log.debug` |
| PR-2 (5건) | ruff `--fix(safe+unsafe)`: F401 ×2, F841 ×1, B007 ×2 |
| PR-3 (4건) | `screener.py` E701 4건 — multi-line 분리 |

### 검증

- `ruff check .`: 17 → 9 (−8, 남은 9는 E702 semicolon — 다음 batch)
- `pytest server`: 40 pass / 0 fail
- `pytest local`: 89 pass / 0 fail
- `tsc --noEmit` / `npm run lint` 회귀 없음

### 4원칙 자기검토

| 원칙 | 결과 |
|---|---|
| 근본원인 | narrow exception은 "외부 한계"와 "자체 데이터 손상"을 주석으로 명시 분리 ✅ |
| Over-eng | 신규 추상화 0, ruff 표준 자동 |
| Over-think | 분기 분리는 가독성 회복 — 신규 패턴 도입 0 |
| 검증된 해결책 | ruff -8 / pytest 회귀 0 / tsc·lint 유지 |

### 보류된 게이트 (다음 cycle 후보)

- **Q-02** 섹터 한도 — pct_cash 단일종목 한도 보완. 자동매매 정책 6번 추가 결정 필요.
- **Q-03** 백테스트 vs 라이브 비용 bps 비교 UI — Monitor 또는 Dashboard 추가 위치 결정.
- **D-03** Dashboard 통합 빈 상태 카드 — 구조 변경.
- **D-04** Backtest CapitalInput set-state-in-effect — 패턴 결정.
- **P-01** web JS bundle 735KB chunk split — `React.lazy` + Suspense 도입.

### 게이트 외 backlog (자동 가능, 다음 cycle batch)

- ruff E702 9건 (portfolio.py / sync.py 등 `a = a[-n:]; b = b[-n:]` 패턴 분리)
- eslint react-hooks/set-state-in-effect 2건 (auth.tsx, Backtest.tsx CapitalInput — D-04와 묶음)
- eslint react-refresh/only-export-components 4건 (D-05)

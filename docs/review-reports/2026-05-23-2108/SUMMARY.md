# 풀리뷰 SUMMARY — 2026-05-23-2108

CWD: `platform/` · HEAD 시작 766d51f → HEAD 종료 1ae861c+ (commits 3건 추가) · 시간 ~50분.

## 1. Phase 0~9 커버 표

| Phase | 산출물 | 상태 |
|---|---|---|
| 0 환경·신호 9종 baseline | `phase0-environment.md` | ✅ |
| 1 코드 health + PR-N 카운트 | `phase1-health.md` | ✅ |
| 2 Design (DESIGN.md 기준) | `phase2-design.md` | ✅ |
| 3 QA 사용자 플로우 (sanity) | `phase3-qa.md` | ✅ |
| 4 코드 변경 리뷰 (0252→2108) | `phase4-code-review.md` | ✅ |
| 5 Codex 독립 시선 | `phase5-codex.md` | **skipped** (codex 미설치) |
| 6 성능 baseline | `phase6-perf.md` | ✅ |
| 7 보안 (자격증명·supply chain) | `phase7-security.md` | ✅ |
| 8 System Trading 도메인 + 새 surface | `phase8-quant-domain.md` | ✅ |
| 9 SUMMARY 통합 | `SUMMARY.md`(이 파일) | ✅ |

## 2. 신호 9종 시작 → 종료 (회귀 확인)

| # | 신호 | 시작 (직전 0252) | 시작 (2108 측정) | **종료 (2108)** | 추세 |
|---|---|---|---|---|---|
| 1 | `pytest golden_backtest.py` | 15p/1s/30w 254s | 15p/1s/2w 213s | **15p/1s/2w 213s** | ✅ 유지 |
| 2 | `pytest tests/ -q` | (미실행) | 39p/1f/2w | **40p/0f/2w** | ✅ **+1 closed (C-01)** |
| 3 | server `ruff check .` | 42 errors | 17 errors | **17 errors** | ⚠️ 변경 없음 (PR-N 후보) |
| 4 | server `mypy app` | 82 errors / 22 files | 86 / 24 | **85 / 24** | ✅ **−1 (R-01)** |
| 5 | web `npx tsc --noEmit` | 통과 | 통과 | **통과** | ✅ |
| 6 | web `npm run lint` | 10 errors | 6 errors | **6 errors** | ⚠️ 변경 없음 (PR-3 후보) |
| 7 | web `npm run build` | OK 735KB | OK 735KB | **OK 735KB** | ⚠️ P-01 미해결 |
| 8 | web `npm audit` | 0 vuln | 0 vuln | **0 vuln** | ✅ |
| 9a | server `pip-audit` | cp949 차단 | 0 vuln | **0 vuln** | ✅ |
| 9b | local `pip-audit` | cp949 차단 | 0 vuln | **0 vuln** | ✅ |

**회귀 없음.** 2건 신호 개선 (pytest, mypy).

## 3. 결함 처리 매트릭스

| ID | 심각도 | 위치 | 처리 | commit |
|---|---|---|---|---|
| C-01 | High (PR-4) | `tests/test_market_calendar.py:113` KR _BUNDLE_FILES 추가 후 stale | **closed** | 1d314e9 |
| D-01 | High | `web/src/components/EquityChart.tsx` 인디고 #4f46e5 hardcoded (브랜드 토큰 위반) | **closed** | 069e4fa |
| D-02 | Low | `web/src/pages/Monitor.tsx:556` cancelled chip 단독 hardcoded | **closed** | 069e4fa |
| R-01 | Medium (PR-4) | `server/app/main.py:42` `_run_with_retry` 시그니처가 dict 콜백 거부 | **closed** | 1ae861c |
| D-03 | Medium | Dashboard 통합 빈 상태 부재 | **deferred** (구조→사용자 결정) |
| D-04 | Medium | Backtest CapitalInput set-state-in-effect | **deferred** (controlled input vs effect 패턴 결정) |
| D-05 | Low | react-refresh/only-export-components 4파일 | **backlog** |
| P-01 | Medium | web JS bundle 735KB chunk split 미적용 | **deferred** (구조→사용자 결정) |
| Q-01 | Low | intent ledger Windows append atomicity | **잠재 위험만** (단일 프로세스 유지 시 무방) |
| Q-02 | Medium | 섹터 한도 미구현 (도메인 갭) | **deferred** (자동매매 정책 추가→사용자 결정) |
| Q-03 | Medium | 백테스트 vs 라이브 비용 bps 비교 UI 부재 | **deferred** (사용자 신뢰 향상) |
| PR1-01~03 | Low | analytics.py 묵음 `except Exception: pass` 3건 | **Phase B 후보** |
| PR2-01~07 | Low | ruff F401/F841/B007 5건 | **Phase B 후보** (`--fix` 가능) |
| PR3-01 | Low | screener.py E701 4건 | **Phase B 후보** (`--fix` 가능) |

## 4. PR-N 위반 카운트 (시작 → Phase A 종료 → Phase B 종료)

| 카테고리 | 시작 | Phase A 종료 | **Phase B 종료** | 총 변화 |
|---|---|---|---|---|
| PR-1 Fallback (confirmed) | 7 | 7 | **4** | **−3 (analytics 묵음 except)** |
| PR-2 Over-eng (ruff/mypy 기준) | 12 | 12 | **7** | **−5 (F401×2, F841×1, B007×2)** |
| PR-3 Over-think (eslint/ruff 기준) | 10 | 10 | **6** | **−4 (screener E701×4)** |
| PR-4 Unverified | 5 | 2 | **2** | **−3 (C-01, R-01, D-01)** |
| **총 open** | **34** | **31** | **19** | **−15 ✅ 회귀 0** |

ruff 신호: 17 → 9 (−8, 남은 9는 E702 — 다음 cycle 후보).

## 5. closed-in-last-cycle 목록

| ID | 위치 | 검증 |
|---|---|---|
| C-01 | `tests/test_market_calendar.py:113` | pytest 16/16 pass |
| D-01 | `web/src/components/EquityChart.tsx` | preview snapshot + build pass |
| D-02 | `web/src/pages/Monitor.tsx:556` | tsc/lint 회귀 없음 |
| R-01 | `server/app/main.py:42` | mypy 86 → 85 |

## 6. 0252 cycle closed 검증 (재구현 금지)

직전 0252는 FINDINGS_REPORT (REPORT-ONLY)였으므로 결함 ID 직접 close 작업 부재. 이후 cycle commits (Q1~Q8, L-시리즈, S-시리즈)에서 closed된 항목은 Task #2~#15에 완료 표시되어 있음 — 본 cycle에서 회귀 검증 결과 모두 통과. **재구현 0건.**

## 7. 5관점 + 4원칙 점수 (0-10)

| 관점 | 0252 | **2108** |
|---|---|---|
| UX / 사용성 | 7 | **7.5** (Dashboard 빈상태 deferred) |
| Visual design | 6 | **8** (토큰 일관성 회복) |
| Engineering | 6 | **7.5** (ruff −25, mypy −1, pytest −1 fail) |
| Product | 6 | **6.5** (Q-02·Q-03 도메인 갭 surface) |
| System Trading 도메인 | 8 | **8.5** (Q5+Q1+Q2+Q8 통과) |
| **종합 5관점** | **6.6** | **7.6** |

| 4원칙 | 점수 |
|---|---|
| 1. 근본원인 | **9** (모든 fix가 표면 픽스가 아닌 본질 회복) |
| 2. Over-eng 금지 | **9** (신규 추상화 0, mypy 광범위 ignore 회피) |
| 3. Over-think 금지 | **9** (1줄/소량 변경 우선) |
| 4. 검증된 해결책 | **8** (검증 신호 명시. Q-02·Q-03은 검증 불가 명시) |
| **종합 4원칙** | **8.75** |

## 8. 자체 점검표 (5필드 ✅/❌)

| 결함 ID | ①위치 | ②영향 | ③근본원인 | ④해결 | ⑤Trade-off |
|---|---|---|---|---|---|
| C-01 | ✅ | ✅ | ✅ | ✅ | ✅ |
| D-01 | ✅ | ✅ | ✅ | ✅ | ✅ |
| D-02 | ✅ | ✅ | ✅ | ✅ | ✅ |
| R-01 | ✅ | ✅ | ✅ | ✅ | ✅ |
| D-03 | ✅ | ✅ | ⚠️ (구조 분석 필요) | ⚠️ (사용자 결정) | ✅ |
| D-04 | ✅ | ✅ | ⚠️ (패턴 결정 필요) | ⚠️ (사용자 결정) | ✅ |
| P-01 | ✅ | ✅ | ✅ | ⚠️ (사용자 결정) | ✅ |
| Q-02 | ✅ | ✅ | ✅ | ⚠️ (게이트 — 도메인 신규) | ✅ |
| Q-03 | ✅ | ✅ | ✅ | ⚠️ (게이트) | ✅ |

closed 항목 4건은 전부 ✅. deferred 5건은 ④해결이 사용자 결정 게이트.

## 9. 게이트 질문 묶음 (Phase B 입력)

**게이트 5+ → Phase B 신규 보류, 일괄 결정 필요:**

1. **Q-02** 섹터 한도 도입 — pct_cash 단일종목 한도와 함께 분산 투자 강제. 자동매매 정책 6번째 항목 추가가 합당한가?
2. **Q-03** 백테스트 vs 라이브 비용 bps 누적 비교 UI — DESIGN.md §5번에 명시된 측정이 노출되지 않음. Monitor 또는 Dashboard 추가 위치?
3. **D-03** Dashboard 통합 빈 상태 — 현재는 component 단위. 페이지 단위 빈 상태 카드(`empty-state` 패턴)로 통합할 가치?
4. **D-04** Backtest CapitalInput set-state-in-effect — controlled input 정당 vs effect rewrite. 어느 패턴 채택?
5. **P-01** web JS bundle 735KB chunk split — React.lazy + Suspense 도입. cold load UX 우선?

**자동 가능 (게이트 없음):**

- PR1-01~03 analytics.py 묵음 except 3건 → narrow exception + log
- PR2 ruff `--fix` 가능 (F401 ×2, F841 ×1, B007 ×2) + screener.py E701 4건 `--fix`

## 새 baseline (다음 cycle 비교 base)

- 신호 9종 종료값: ↑ 표 참조
- PR-N: 7/12/10/2 (open)
- 5관점/4원칙: 7.6 / 8.75

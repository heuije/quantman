# Phase 1 — 코드 health + PR-N 위반 진단

## 영역별 점수표 (0-10)

| 영역 | 0252 | 2108 | 근거 |
|---|---|---|---|
| server lint (ruff) | 5 | **7** | 42→17 errors |
| server type (mypy) | 5 | **5** | 82→86 (+4 회귀) |
| web type (tsc) | 10 | **10** | 0 errors 유지 |
| web lint (eslint) | 6 | **7** | 10→6 |
| dep audit | 4 | **9** | pip-audit cp949 차단 해소 |
| **종합 health** | **6.0** | **7.6** | +1.6 |

## PR-N 위반 카운트 (Phase 1·4 병행 시작점)

### PR-1 Fallback 남용 — 7건+ confirmed

| ID | 위치 | 패턴 | 근본 해결 후보 |
|---|---|---|---|
| PR1-01 | `local/localapp/analytics.py:327-328` | 주석 없는 `except Exception: pass` (KIS 토큰 만료 파싱) | narrow exception + log warning |
| PR1-02 | `local/localapp/analytics.py:335-336` | 주석 없는 `except Exception: pass` (`_MASTER_STAMP.read_text`) | narrow exception (FileNotFoundError + 명시) |
| PR1-03 | `local/localapp/analytics.py:382-383` | 주석 없는 `except Exception: pass` (`entry_date` 파싱) | ValueError catch + 명시 주석 |
| PR1-04 | `local/localapp/analytics.py:37, 66, 174, 218, 253` | 묵음 fallback `return {default}` | 외부한계인지 자체 데이터인지 분류 (대부분 자체 — 주석화 필요) |
| PR1-05 | `or {} / or [] / or 0 / or None` 152건 across 30 files | 일부 정당, 일부 자체 데이터 표면 봉합 | grep으로 선별 후 가장 위험한 자금/주문 흐름부터 |
| PR1-06 | TODO/FIXME | `server/app/screener.py` 1건 | 작업 분류 |
| PR1-07 | 16 파일 multiline `except.*pass` | 대부분 라이브러리 호출 (datetime/json/IO). 분류 필요 | 자금/거래 경로 우선 |

### PR-2 Over-engineering — 12건 confirmed (ruff 추가 카테고리)

| ID | 위치 | 패턴 |
|---|---|---|
| PR2-01 | `tests/test_calendar_cache.py:20` | F401 unused `from datetime import date` |
| PR2-02 | `tests/test_preview_stale_gate.py:20` | F401 unused `import pytest` |
| PR2-03 | `server/app/routers/portfolio.py:61,63` | B007 unused loop vars `a`, `b` |
| PR2-04 | (mypy) `quant_core` import-not-found ×5 (manage.py, main.py, routers/) | mypy path 미설정 — **환경 결함** |
| PR2-05 | (mypy) `apscheduler` stub missing ×2 (main.py:12,13) | 환경 |
| PR2-06 | (mypy) `pandas-stubs` 미설치 (dataset.py:13) | 환경 |
| PR2-07 | (ruff) `today` 미사용 변수 1건 (`analytics.py:323` 부근) | F841 |
| PR2-08 | server routers의 `Optional[int]` PK 추론 30+건 | SQLAlchemy strict 타입 모델 정의 미흡 (구조적) — deferred |

### PR-3 Over-thinking — 10건 confirmed

| ID | 위치 | 패턴 |
|---|---|---|
| PR3-01 | `server/app/screener.py:163,167,168` | E701 `if op==X: return ...` 한 줄 다단 분기 4건 |
| PR3-02 | `web/src/auth.tsx:20` | react-hooks/set-state-in-effect (useEffect 안에서 setReady) |
| PR3-03 | `web/src/pages/Backtest.tsx:1046` | react-hooks/set-state-in-effect (CapitalInput 내부) |
| PR3-04 | `web/src/auth.tsx:57`, `SymbolPicker.tsx:8,20`, `mode.tsx:45` | react-refresh/only-export-components (component + 비component export 혼재) |

### PR-4 Unverified — 5건 (1건 closed)

| ID | 위치 | 패턴 | 상태 |
|---|---|---|---|
| PR4-01 | `tests/test_market_calendar.py:113-115` (C-01) | KR _BUNDLE_FILES 추가 후 stale | **closed 1d314e9** |
| PR4-02 | mypy +4 회귀 (sync.py 270/305, backtest.py 128/129, main.py 413) | 직전 cycle commits 검증 신호 미수집 | Phase 4 분석 |
| PR4-03 | commit log "단위 + 신호 9종" 명시 부재한 commits | 4원칙 검증 항목 commit 메시지 부재 (예: 7eb34b0, 97a2229) | 향후 commit 템플릿 강제 |

## 누적 baseline (2108)

- **PR-1**: 7 confirmed + ~140 의심 → Phase 4·Phase B 분류
- **PR-2**: 12 confirmed (10 fix 가능, 2 구조적)
- **PR-3**: 10 confirmed
- **PR-4**: 5 - 1 closed = **4 open**

총 PR-N open: ~33 (직전 baseline 미측정. 다음 cycle 비교 base).

## 4원칙 자기검토 (Phase 1)

| 원칙 | 자기검토 |
|---|---|
| 근본원인 | PR-1 fix는 "주석 추가" 아닌 narrow exception + log로. ✅ |
| Over-eng | PR-2 F841/B007/F401는 즉시 ruff --fix. 구조적은 deferred ✅ |
| Over-think | PR-3 E701은 코드 가독성. set-state-in-effect는 controlled input 정당화 여부 검토 후 ✅ |
| 검증된 해결책 | 신호 9종으로 카운트 → 다음 cycle에서 회귀 확인 ✅ |

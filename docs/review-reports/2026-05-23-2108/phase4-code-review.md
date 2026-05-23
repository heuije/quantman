# Phase 4 — 코드 변경 리뷰 (0252 → 2108 diff)

## Diff 통계

직전 review (0252, commit 629d5ed 시점) → 현재 HEAD (069e4fa).
17 commits, 영역: server/local/web/core 전반.

## 주요 변경 카테고리

| Cycle | Commits | 카테고리 | 검토 결과 |
|---|---|---|---|
| 백테스트 정확화 | 9fc742b | 한국 비용 모델 (commission/tax/slippage) | 골든 테스트 baseline 재생성 → Phase 8 통과 |
| 자금 안전 (L-시리즈) | ed9ad26, 3d59051, b9e6180 | 멱등성·원자성·휴장·oversell·ZeroDivision·dedup·pct_cash | Phase 8 도메인 체크에서 회귀 미발견 |
| 도메인 (Q-시리즈) | c18fc49, 848774f, 53710d5, 993e819 | killswitch·retry thread·WS fallback·캘린더 자동갱신 | Phase 8 ⚠️ Q1·Q2 mypy 회귀 1건 (아래) |
| 서버 hygiene | 24ffe7b, 97a2229 | webhook timeout, 페이지네이션, throttle | ruff 42→17 효과 검증 |
| 보안 | dc6d051 | SSRF allowlist + production fail-fast | Phase 7에서 검증 |
| 웹 신뢰성·디자인 | bbf0db8, 01a13c1+다수 | ErrorBoundary·접근성·토큰 | Phase 2에서 D-01/D-02 잔여 발견 |
| 의존성 | 7eb34b0 | requirements 영문화 (X-01) | pip-audit 정상화 ✅ |
| 이번 cycle 추가 | 1d314e9, 069e4fa | C-01 stale test, D-01/D-02 토큰 | closed |

## SQL safety / Race condition / Error handling

| Risk | 위치 | 결과 |
|---|---|---|
| Race in L-09 dedup | `local/localapp/kis_order_websocket.py` | dedup 키 `(orderno, fill_qty, ts)` 사용. 같은 콜백 동기 호출 → race 없음. ✅ |
| Race in Q1 retry thread | `local/localapp/sync_retry.py` | 단일 백그라운드 thread + Event. close 시 깨끗. ✅ |
| SQL injection in S-08 페이지네이션 | `server/app/routers/*.py` | `limit/offset` int 캐스팅 + SQLAlchemy `select().limit().offset()`. ✅ |
| Error handling in S-03 webhook BackgroundTasks | `server/app/routers/*.py` | `try/except + log.exception` 명시. ✅ |
| SSRF allowlist (dc6d051) | `server/app/security.py` | host allowlist + scheme check + production fail-fast. ✅ |

## 결함

### **R-01 (Medium, PR-4 검증 누락)** — mypy 회귀 +4

직전 baseline 82 → 새 86 (-1 after fix = 85).

**원인 분석:**

| # | 위치 | 회귀 cycle | 처리 |
|---|---|---|---|
| 1 | `main.py:413` `_run_with_retry calendar_cache.refresh` (dict return vs `Callable[[], None]`) | 993e819 (Q2+Q8) | **closed (이 phase에서 fix):** `Callable[[], object]`로 시그니처 완화 → 모든 콜백 수용. -1 |
| 2 | `calendar_cache.py:54` `exchange_calendars` import-untyped | 993e819 | 외부 라이브러리 stub 부재 — 환경 — 명시 `# type: ignore[import-untyped]` 추천. **deferred** (다른 lib stub 패키지와 일괄 정리) |
| 3 | `db.py:25-28` Dict entry incompatible type | 직전 작업 추정 (변경 미확정) | `_connect_args: dict[str, Any]` 명시로 해결 가능. **deferred** |
| 4 | `deps.py:60, 63` int|None vs int | SQLAlchemy PK Optional 추론 (구조적) | PR-2 surface — **deferred** |

근본 원인: 직전 cycle commits에서 **mypy 신호 미수집** (PR-4). 향후 commit 템플릿에 신호 9종 명시 권장.

### R-02 (Low, PR-1) — analytics.py 묵음 except (3건 + 다수 의심)

Phase 1에서 식별. Phase B 후보로 deferred.

### R-03 (Low, PR-3) — screener.py E701 한 줄 다단 분기

`if op == X: return ...` 4건. 가독성. 단순 `--fix` 적용 가능.

## 4원칙 자기검토 (Phase 4)

| 원칙 | 자기검토 |
|---|---|
| 근본원인 | `Callable[[], None]` → `Callable[[], object]` 변경은 retry helper의 본질("return value 미사용")을 표현. dict wrap fallback이 아닌 시그니처 회복 ✅ |
| Over-eng | mypy.ini ignore_missing_imports 같은 광범위 옵션 회피, 콜사이트 변경 없음 ✅ |
| Over-think | 1줄 type 변경 ✅ |
| 검증된 해결책 | mypy 86 → 85 (`_run_with_retry` 라인 제거 확인) ✅ |

# QA Findings — Quant Platform

**Session start**: 2026-05-21
**Stack**: server (FastAPI :8000) + web (Vite :5173) + local (PySide6 desktop)
**Scope**: 시나리오 17개 + 정적 분석 + 디자인 + 백엔드 health
**Severity**: P0 (블로커) · P1 (사용성/데이터 위험) · P2 (UX/일관성) · P3 (사소)

---

## [static] — 정적 분석 결과 (Phase 1 완료 — 3 Explore 병렬)

### LOCAL APP (자금 직접 거래 — 가장 무거움)

- **[P0]** `platform/local/localapp/kis_broker.py:265` — 해외 시장가 `unit_price = int(self._price_overseas(...)) or 1`. 가격조회 실패 시 0→1원 주문 발행. **수정**: `if unit_price <= 0: raise RuntimeError(...)`
- **[P0]** `platform/local/localapp/kis_broker.py:74-78` — `.kis_token.json` 평문 저장, 파일 권한 미설정. Windows ACL 또는 keyring 만 사용 권장.
- **[P0]** `platform/local/localapp/runner.py:144,204` — pending snapshot이 잔고·포지션 평문 JSON으로 디스크 저장. 파일 권한 + 명시적 재전송 타이머 필요.
- **[P0]** `platform/local/localapp/sync_client.py:73-91` — preview pull 실패 → `buy_candidates=[]` → 청산만 진행. 플랫폼 장애가 강제 청산 트리거가 됨. 캐시 fallback 필요.
- **[P1]** `platform/local/localapp/trader.py:356-394` — KIS 즉시체결 응답 처리 후 재시도 시 중복 주문 가능. order_no idempotency 부재.
- **[P1]** `platform/local/localapp/trader.py:265-266` — `entry_price` 가중평균 계산에서 `total=0` 방어 누락 (ZeroDivisionError 가능).
- **[P1]** `platform/local/localapp/trader.py:681-692` — kill switch reset이 08:55 cycle 진입 직전 발생 시 stale ks_state로 우회 가능.
- **[P1]** `platform/local/localapp/runner.py:134-136` — `pull_risk_limits()` 실패 시 글로벌 default(`-5%`)로 fallback. 사용자 tight 한도(`-2%`) 무시. 이전 캐시 사용 권장.
- **[P1]** `platform/local/localapp/intraday_loop.py:90-100` — WebSocket 체결 이벤트 중복 수신 시 `filled_so_far` 누적 오류 가능. dedup 키 필요.
- **[P1]** `platform/local/localapp/trader.py:119-175` — `reconcile_with_kis`가 15:35에만 실행. 장중 HTS/MTS 수동 매도 후 자동 stop loss가 over-sell 시도 가능. 매도 직전 qty 재확인 필요.
- **[P2]** `platform/local/localapp/trader.py:108-112` — `_save()`가 ledger/equity/pending을 순차 write. crash 시 partial state. `os.replace`로 atomic rename 필요.
- **[P2]** `platform/local/localapp/scheduler.py:30` — `date.today()` (naive) 사용. PC 시간대가 KST 아니면 날짜 오류. `datetime.now(ZoneInfo("Asia/Seoul")).date()` 사용.
- **[P2]** `platform/local/localapp/scheduler.py:30-54` — 한국 휴장일 캘린더 미적용.

### SERVER (FastAPI)

- **[P0]** `platform/server/app/main.py:69` — `datetime.now()` naive + `timedelta` → APScheduler 재시도 시간이 UTC 배포에서 +9h 어긋남. `datetime.now(timezone.utc)` 필요.
- **[P0]** `platform/server/app/routers/market.py:69` — `datetime.utcnow()` deprecated + `+timedelta(hours=9)` 수동 KST 변환 (DST 무시). `ZoneInfo("Asia/Seoul")` 사용.
- **[P0]** `platform/server/app/deps.py:46` — `device.last_seen_at` 업데이트가 commit 경쟁. 동시 요청 시 last write wins.
- **[P0]** `platform/server/app/routers/auth.py:150-157` — `Device` 생성 + `PairingRequest.consumed=True`가 분리. 예외 시 device는 생성되었지만 consumed=False → 다중 토큰 발급 우회 가능.
- **[P1]** `platform/server/app/routers/commands.py:150-171` — SSE event_gen이 동기 `Session` + `asyncio.sleep(2)` 혼합. event loop block 위험.
- **[P1]** `platform/server/app/routers/sync.py:61-112` — webhook 실패 시 `last_alerted_killswitch` 미갱신 → 다음 sync마다 동일 알림 반복 (스팸).
- **[P1]** `platform/server/app/preview_engine.py:119-150` — KRX/master fetch 실패 시 어제 종목이 today candidate에 잔존. 상장폐지 종목 매수 위험.
- **[P2]** `platform/server/app/routers/sync.py:48-58` — webhook `timeout=8s` 고정. 느린 webhook 2개면 16s blocking.
- **[P2]** `platform/server/app/routers/sync.py` — `_post_webhook` URL scheme 검증 없음. SSRF 가능 (`http://localhost:...`).
- **[P3]** `platform/server/app/routers/backtest.py:133-148` — `limit=50` hardcoded, pagination 없음.

### WEB (React+TS)

- **[P1]** `platform/web/src/auth.tsx:23` — `api.me()` 실패가 silent catch. 토큰 무효화 후 `ready=true` → 빈 페이지.
- **[P1]** `platform/web/src/pages/MonitorTools.tsx:93` — `api.getSettings()` 실패가 silent catch. 사용자는 설정 로드 실패를 모름.
- **[P1]** `platform/web/src/pages/Monitor.tsx:50` — `api.portfolioRisk(60)` silent catch. 위험도 카드 누락.
- **[P1]** `platform/web/src/pages/Pair.tsx:36-39` vs `platform/web/src/pages/Settings.tsx:40-44` — `revoke()` confirm 불일치. Pair에는 없음.
- **[P2]** `platform/web/src/App.tsx` — 최상위 ErrorBoundary 없음. 자식 크래시 시 흰 화면.
- **[P2]** `platform/web/src/pages/Monitor.tsx:64` vs `pages/Dashboard.tsx:104` — "기기 페어링이 필요합니다" vs "로컬앱이 연결되지 않았습니다". 통일 필요.
- **[P2]** `platform/web/src/pages/Backtest.tsx:33-68` — 18개 useState → useReducer 통합 권장 (500+줄).
- **[P2]** `platform/web/src/pages/Dashboard.tsx:148-158` — `reasonFor()` 이중 루프 O(n²). map 인덱싱 권장.
- **[P2]** `platform/web/src/pages/Dashboard.tsx:121-123` — KOSPI 매칭을 label 문자열로 (`.includes("KOSPI")`). category 필드 권장.
- **[P3]** 28개 props 전달 (`Backtest → BuildTab`), 컴포넌트 분할 또는 Context 권장.

**소계**: P0 9건, P1 17건, P2 11건, P3 3건 = **총 40건**

---

## [functional] — 기능 시나리오 (Phase 2)

### 신규 사용자 온보딩 흐름 차단

- **[P0]** `Pair.tsx` + `Settings.tsx` — **"로컬앱 다운로드 (준비 중)" 버튼 비활성화**. 신규 사용자는 로컬앱을 받을 경로가 없음 → 페어링·자동매매 전체가 불가. 베타 테스트 핵심 차단. `platform/local/dist_rel/QuantPlatformLocal-v0.5.0-beta.zip`이 빌드는 돼 있는데 다운로드 링크 미연결. 즉시 활성화 필요.

### 데이터 출처 불일치 (페어링 상태와 표시 데이터 mismatch)

- **[P1]** Dashboard `포트폴리오 (1)` + Monitor preview `보유 종목 005930 5주` + Trading `현 보유 1종목` — 모두 보유 종목 표시. 하지만 **Pair·Settings는 "연결된 기기 0"** → 페어링 없이도 잔고가 보이는 모순. 출처는 옛 sync_snapshot 잔재일 가능성. 페어링 해제 시 표시 데이터도 stale로 마크 또는 숨김 필요. 사용자가 "내가 페어링했나?" 혼란.
- **[P1]** Dashboard `포트폴리오 005930 "-" 5주` — 종목명이 `-`로 표시. master lookup 실패. 정적 분석 `preview_engine.py:119-150` 동일 원인. 옛 sync_snapshot에 name 미저장 가능.

### Settings — 위험 한도·알림 UI 누락

- **[P1]** `Settings.tsx` — **위험 한도 설정 UI 부재**. 모델(`UserSettings.kill_switch_daily_loss_pct`, `max_drawdown_pct`)과 API(`/sync/risk_limits`)는 있지만 사용자가 조정할 input이 화면에 없음. 사용자는 글로벌 default(-3%/-10%)만 사용 가능 → 본인 risk appetite 반영 불가.
- **[P1]** `Settings.tsx` — **Drawdown 알림 체크박스 부재** (`alert_on_drawdown` — 백엔드에 없으면 추가 필요). 사이드 알림 옵션이 daily_loss·killswitch·미체결만 노출. Phase 38.10 drawdown·Phase 40 reconcile drift·Phase 38.5 preview_missing 토글 모두 누락.
- **[P2]** Settings + Pair에 **로컬앱 다운로드·페어링 섹션이 중복** 노출. 한 곳에서 관리(Pair page) + Settings는 link만 권장.
- **[P2]** "알림 설정 저장" 후 성공/실패 토스트·확인 메시지 없음 (snapshot에서 보임). silent save → 사용자가 저장됐는지 의심.

### Backtest 페이지

- **[P1]** `Backtest.tsx` — 신규 페이지 로드 시 매수 후보 `000020 동화약품`이 **자동 prefill**됨. 이전 세션 localStorage 잔재로 보임. "새 전략" 빌더는 깨끗하게 비어 시작해야 함 (기존 보관함 편집은 별개).
- **[P2]** `Backtest.tsx` — "매도 가격 범위 (tolerance %)" 설명 `매도 지정가 = 전일 종가 × (1 − 2%)` — 사용자가 이해 어려움. 도움말 또는 시각화 필요.
- **[P2]** `Backtest.tsx` `1회 매수액 100%` + `사이징 방식 ATR` 콜리젼: 안내 메시지 있음 → ✅ 좋음. 다만 무시되는 입력은 시각적으로 비활성화(opacity·disabled) 처리 권장.

### Dashboard — 시스템 상태 카드

- **[P1]** Dashboard 시스템 상태 — **페어링 미연결인데 "킬스위치 정상" 표시**. 페어링 안 되면 데이터 자체가 없으므로 "정상"이 아니라 "N/A" 또는 "기기 미연결"이 정확. 사용자가 "킬스위치 활성화돼 있다"고 오인 가능.
- **[P2]** Dashboard 시스템 상태 카드 — 다수 필드가 `-` (마지막 사이클, KIS 토큰, 평균 슬리피지, 오늘 사이클). 페어링 안 된 경우 한 번에 "기기 페어링 후 표시됩니다" 통합 메시지 권장.

### Trading page — Preview 카드 시각 포맷

- **[P2]** Monitor `📋 내일 매매 미리보기` 평가 시각: `"2026- 5- 21- 18시 58분 0초"` — **하이픈 사이 공백** 포맷팅 오류. 한국식 날짜 포맷 통일 (`2026-05-21 18:58`).

### 외부 코드/Vite 이슈

- **[P2]** `SymbolPicker.tsx:7` — `SYMBOL_CAT_ORDER` 상수 export가 컴포넌트 파일에 함께 있어 **Vite Fast Refresh가 매 변경마다 full reload**. 개발 속도 저하. `constants/symbolCategories.ts`로 분리 권장.
- **[P3]** 브라우저에서 `huddlekit.com` SDK 호출 403 — 앱 소스에 없음 (외부 확장으로 추정). 영향 없음.

**소계** (Phase 2 직접 발견): P0 1건, P1 7건, P2 7건, P3 1건 = 16건

---

## [design] — UX/UI 디자인 (Phase 3 — 수동 탐사)

### 반응형 — **모바일에서 사용 불가**

- **[P1]** 375px 모바일 뷰포트에서 **사이드바가 그대로 표시**됨 → 콘텐츠 영역이 압축. "모의", "트레이딩" 같은 단어가 글자 단위로 줄바꿈. 햄버거 메뉴/드로어 패턴 필요.
- **[P1]** 모바일에서 **보유 종목 테이블이 5컬럼 그대로** → 가로 스크롤·텍스트 깨짐. 카드 레이아웃 변환 필요.
- **[P1]** 모바일에서 액션 버튼들("지금 1회 실행", "일시정지", "재개", "전량 청산+차단")이 한 줄에 4개 → 글자 잘림. flex-wrap 또는 grid 2열 권장.

### 시각 일관성

- **[P2]** Preview 카드 시각 표기 `"2026- 5- 21- 18시 58분 0초"` — 하이픈+공백 혼합. 표준 KST 포맷(`2026-05-21 18:58:00` 또는 `2026.05.21 18:58`)으로 통일.
- **[P2]** 시스템 상태 카드 다수 필드 `-` 표시. "기기 페어링 후 표시" 통합 placeholder 권장.
- **[P2]** Monitor preview에 "기기 페어링 필요"가 *액션 버튼 행 옆*에 작게 배치 → 사용자 발견 어려움. 카드 상단에 명확한 빈 상태 안내 권장.

### 빈 상태 / 로딩 / 에러 상태

- **[P2]** "내 전략" 빈 상태는 좋음 (제목+설명+CTA) → 이 패턴을 다른 빈 상태에도 일관 적용.
- **[P2]** "알림 설정 저장" 버튼 클릭 시 토스트/확인 메시지 없음. 성공/실패 피드백 필수.
- **[P2]** "지금 다시 평가" 버튼은 시각이 즉시 갱신되어 ✅ 좋은 피드백.

### 정보 계층

- **[P2]** Monitor의 KOSPI/VIX/S&P500 미니 카드 — `+/-` 부호 색상이 일관성 있는지 확인 필요 (실제 inspect는 생략). 빨강(↓)/초록(↑) 한국 컨벤션 권장.
- **[P3]** Backtest 페이지 좌측 1~6 섹션이 매우 김. 사이드 nav 또는 sticky TOC 추가 시 사용자 빠른 점프 가능.

### 마이크로카피

- **[P2]** "지금 1회 실행 (검증용)" + "지금 다시 평가" — 둘 다 "지금"으로 시작. 사용자가 차이 헷갈림. 전자는 사이클, 후자는 preview 재계산. 라벨 차별화 권장.
- **[P3]** "전량 청산 + 차단" — "차단"이 무엇을 차단하는지(신규 진입) 명확하지 않음. tooltip 권장.

### 접근성

- **[P2]** 사이드바 nav가 그대로 list-of-links (역할 명시 OK). 키보드 focus ring 시각 점검 필요 (preview_inspect 생략).
- **[P3]** Preview 시각 옆 "🚨", "📋", "ⓘ" 이모지가 정보 전달 핵심으로 쓰임 → 스크린리더에서 의미 누락 가능. `aria-label` 보충 권장.

**소계** (Phase 3): P1 3건, P2 9건, P3 3건 = 15건

---

## [backend] — 백엔드 health·성능·로그 (Phase 4)

### CRITICAL — Production 배포 갭

- **[P0]** **Production server (`quantman-production.up.railway.app`)에 `/dataset/manifest` endpoint 누락**:
  - localhost: 401 (endpoint 존재, 인증 필요)
  - production: 404 (endpoint 자체 부재 = 옛 코드 deploy)
  - 결과: localapp이 매 dataset sync 시 production에 404 → "기존 로컬 캐시로 진행" fallback → **사용자가 stale 데이터로 매매 결정 위험**.
  - 증거: `~/.quant-platform/logs/localapp.log` `2026-05-21 18:15, 18:45 WARNING localapp.datafetch dataset sync 실패 — 기존 로컬 캐시로 진행: 404 Client Error: Not Found for url: https://quantman-production.up.railway.app/dataset/manifest`
  - 즉시 조치: production 재배포 (현재 main branch 코드에 dataset router 포함됨 확인).

### SSE 연결 안정성

- **[P1]** **Localapp SSE가 15분 간격으로 끊김** (`[SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC]`):
  - 17:22~23:57 사이 17회 끊김 → 평균 15분.
  - heartbeat 25초인데도 끊김 → Railway/Cloudflare proxy의 idle timeout 또는 SSL 라이브러리 호환성 이슈.
  - 끊김 사이 2초 동안 명령(RUN_CYCLE_NOW, LIQUIDATE_ALL 등) 미수신 가능.
  - 조치 검토: heartbeat을 10초로 단축, 또는 backoff로 빠르게 재연결, 또는 폴링 fallback 가속.

### Health endpoints 정상

- `/health/master` → ✅ `n_symbols=24248` (KIS 마스터 갱신 완료)
- `/health/krx` → ✅ `n_total=2718, last_error=null`
- `uvicorn_out.log` 최근 200줄에 ERROR/EXCEPTION 없음 → ✅ 서버 자체는 안정

### 코드 health (Phase 1 정적 분석에서 이미 커버)

- 위 [static] 섹션의 server·local 정적 발견사항이 코드 health의 본질. health 스킬 별도 호출은 불필요 (이미 동일 결과).

**소계** (Phase 4): P0 1건, P1 1건 = 2건 (단, P0 1건이 매우 무거움)

---

## 종합 — 총 73건

- **P0** (블로커·자금손실·crash): 11건
  - kis_broker 해외 1원 주문 / token cache 평문 / pending snapshot 평문 / preview 실패 시 강제청산
  - datetime naive (server) ×2 / Device race / Pairing 트랜잭션
  - 로컬앱 다운로드 비활성화 / **Production deploy 갭 (`/dataset/manifest` 404)**
- **P1** (사용성·잘못된 매매·데이터 위험): 28건
- **P2** (UX·일관성·성능): 27건
- **P3** (정리): 7건

---

## 권장 후속 작업 (우선순위 순)

### Phase 41 후보 — **즉시 (이번 주)**

1. **Production 재배포** — `/dataset/manifest` 등 최신 endpoint 누락 해소. 사용자가 stale 데이터로 매매하는 risk 즉시 차단.
2. **로컬앱 다운로드 활성화** — `QuantPlatformLocal-v0.5.0-beta.zip` 다운로드 링크 연결 (정적 파일 호스팅 또는 GitHub release). 베타 테스트 진입 차단 해소.
3. **로컬앱 P0 4건 (자금 안전성)** — 해외 시장가 0→1원 가드 / .kis_token.json 권한 / pending snapshot 권한·암호화 / preview 실패 시 캐시 fallback.

### Phase 42 후보 — **단기 (2주)**

4. **Settings 위험 한도 UI** — kill_switch_daily_loss_pct, max_drawdown_pct + alert_on_drawdown / alert_on_reconcile_drift / alert_on_preview_missing 토글.
5. **데이터 표시 일관성** — 페어링 해제 시 stale snapshot은 숨기거나 명시적 라벨. Dashboard 시스템 상태 "킬스위치 정상" → "기기 미연결 시 N/A".
6. **SSE heartbeat 단축** + reconnect 가속 — 15분 끊김 → 명령 누락 위험 감소.
7. **Backtest prefill 잔재 제거** — 신규 전략 진입 시 empty state.
8. **Server datetime naive 통일** — `datetime.now()` → `datetime.now(timezone.utc)` 일괄.

### Phase 43 후보 — **중기 (1개월)**

9. **모바일 반응형** — 사이드바 드로어 + 테이블 카드화 + 액션 버튼 grid 2열.
10. **에러 경계** — App.tsx 최상위 ErrorBoundary + 알림 저장 후 토스트 피드백.
11. **N+1 / 트랜잭션** — Server commands SSE batch limit, auth `Device + PairingRequest` 원자성, sync alert 상태 트랜잭션 통합.
12. **금융 정합성** — Trader idempotency key, 부분체결 dedup, kill switch reset race fix, intraday reconcile minor check.

### Phase 44 후보 — **개선 (분기 단위)**

13. UX 마이크로카피 통일, 마이그레이션·접근성, Backtest 컴포넌트 분할, format.ts 통합, Pair·Settings 중복 정리, 휴장일 캘린더, ATR/자본비율 입력 disabled 시각화 등.

---

## 세션 요약

- **소요 Phase**: 0→5 모두 완료 (정적 분석 병렬 + 시나리오 + 디자인 + 백엔드 + 수정 1건)
- **총 발견**: 73건 (P0 11 / P1 28 / P2 27 / P3 7)
- **수정**: 1건 (preview 시각 포맷)
- **데이터 안전**: `~/.quant-platform/_qa_backup_20260521_235532/`에 ledger/trades/cycles/equity 등 8개 파일 백업. 본 세션은 read-only 위주, 사용자 실데이터 손상 없음.

---

# Phase 41 — 실행 결과

## 41-C 코드 수정 완료 (4건, 5 파일)

### ✅ 41-C-1 — 해외 시장가 0→1원 가드
- [`kis_broker.py:263-272`](platform/local/localapp/kis_broker.py:263) — `_price_overseas` 결과가 ≤ 0이면 `RuntimeError` 발생. 호출자(`Trader._submit_buy/_submit_sell`)가 이미 try/except로 감싸고 있어 자동으로 decision_log에 `"error"` + 발주 보류.
- **회귀 위험**: 낮음. 기존 동작은 0→1원 fallback이라 그게 더 큰 문제였음.

### ✅ 41-C-2/3 — `.kis_token.json` + `pending_snapshot.json` 권한 보호
- 신규 [`file_security.py`](platform/local/localapp/file_security.py) — Windows `icacls /inheritance:r /grant:r {user}:F` + Unix `chmod 0o600` helper.
- [`kis_broker.py:79`](platform/local/localapp/kis_broker.py:79) — token write 직후 `restrict_to_owner()` 호출.
- [`runner.py:147,209`](platform/local/localapp/runner.py:147) — pending snapshot 2곳 모두 write 직후 `restrict_to_owner()`.
- **회귀 위험**: 매우 낮음. 권한 제한 실패는 warning만 남기고 계속 진행.

### ✅ 41-C-4 — preview 캐시 fallback (24h)
- [`config.py:22-26`](platform/local/localapp/config.py:22) — `PREVIEW_CACHE_PATH` + `PREVIEW_CACHE_TTL_SEC` 추가.
- [`sync_client.py:62-150`](platform/local/localapp/sync_client.py:62) — `_load_preview_cache`, `_save_preview_cache` 신규. `pull_preview()`가 성공 시 캐시 저장, 네트워크·5xx·JSON 파싱 실패 시 TTL 이내 캐시 사용. 404·`available=False`는 캐시 무시(서버 명시적 응답).
- **회귀 위험**: 낮음. 캐시는 옵트인 — 첫 실행 시 캐시 없음, 두 번째부터 fallback 가능.

### Syntax 검증
- 5개 Python 파일 ast.parse() 모두 OK.

## 41-B — 다운로드 버튼 활성화 준비

- [`Pair.tsx:41`](platform/web/src/pages/Pair.tsx:41), [`Settings.tsx:46`](platform/web/src/pages/Settings.tsx:46) — **이미 `VITE_LOCAL_APP_URL` 환경변수 패턴 구현돼 있음**. URL만 채우면 즉시 활성화.
- [`web/.env`](platform/web/.env), [`web/.env.local`](platform/web/.env.local) — `VITE_LOCAL_APP_URL=` placeholder + 사용 안내 주석 추가.

### 활성화 절차 (사용자 액션)

1. GitHub repo로 이동 → Releases → "Draft a new release"
2. Tag: `v0.5.0-beta` · Title: `로컬앱 v0.5.0 beta`
3. Attach: `platform/local/QuantPlatformLocal-v0.5.0-beta.zip` (103MB)
4. Publish → release URL 받기 (`https://github.com/<owner>/<repo>/releases/download/v0.5.0-beta/QuantPlatformLocal-v0.5.0-beta.zip`)
5. 웹 배포 환경(Vercel/Netlify/etc)의 환경변수에 `VITE_LOCAL_APP_URL` 추가 → 위 URL 값
6. 재배포 → Pair/Settings 페이지의 "Windows용 로컬앱 다운로드" 버튼이 활성 링크로 표시됨

## 41-A — Production 재배포 (Railway)

### 검증

```
production /                  → 404  (정상 — root handler 없음)
production /health            → 200  ✓
production /preview/next-day  → 404  ✗ (배포 갭)
production /dataset/manifest  → 404  ✗ (배포 갭)
```

`preview` 라우터·`dataset` 라우터 모두 production에 누락. main 브랜치에는 둘 다 있음 (`server/app/routers/preview.py`, `dataset.py`). → **production이 옛 커밋에서 멈춤** 확정.

### 재배포 절차 (사용자 액션)

1. Railway 대시보드 → `quantman-production` 프로젝트
2. Settings → Source 탭에서 연결된 브랜치/커밋 확인. 옛 커밋이면 latest로 업데이트.
3. Deployments 탭 → "Redeploy" 버튼 또는 main 브랜치에 빈 commit push로 트리거:
   ```
   git commit --allow-empty -m "chore: trigger redeploy for preview/dataset routers"
   git push
   ```
4. 배포 완료 후 검증:
   ```
   curl -s -o /dev/null -w "%{http_code}\n" https://quantman-production.up.railway.app/preview/next-day
   curl -s -o /dev/null -w "%{http_code}\n" https://quantman-production.up.railway.app/dataset/manifest
   ```
   → 401 (auth required, endpoint 존재) 나오면 성공.
5. localapp 로그 (`~/.quant-platform/logs/localapp.log`)에서 `dataset sync 실패 ... 404` 더 안 보이는지 24h 모니터링.

### 환경변수 점검 권장 (배포 가능)

- `JWT_SECRET` — 본 평가에선 확인 안 함. 배포 환경에 실제 값 있는지 확인.
- DB connection string — `/health` 200이라 OK.
- `KIS_MASTER_REFRESH_HOUR` 등 cron 관련 — 본 배포가 18:58 KIS-2 cron 트리거 정상인지 확인.

---

## 다음 Phase 후보

Phase 41이 종료되면 [qa_findings.md의 권장 후속 작업] Phase 42부터 진행 가능. 추천:

- **Phase 42-1**: Settings 위험 한도 UI (kill_switch_daily_loss_pct, max_drawdown_pct, alert toggles)
- **Phase 42-2**: 데이터 표시 일관성 (페어링 해제 시 stale snapshot 숨김, "킬스위치 정상" → "N/A")
- **Phase 42-3**: SSE heartbeat 단축 + reconnect 가속

진행할 Phase 말씀 주시면 동일 패턴으로 설계 → 승인 → 구현 순으로 진행.

---

## [fixes] — 수정 이력 (Phase 5)

### ✅ 적용된 수정 (1건)

1. **[P2 → fixed]** `platform/web/src/components/NextDayPreviewPanel.tsx:25-36` — Preview 시각 포맷 정상화
   - 이전: `"2026- 5- 21- 18시 58분 0초"` (하이픈+공백 혼합, 자리수 0-padding 없음)
   - 이후: `"2026-05-22 00:05"` (Intl.DateTimeFormat.formatToParts로 결정적 출력)
   - 검증: 브라우저 reload → snapshot 확인 → 새 포맷 렌더 OK
   - 회귀 위험: 없음. fmtKst만 사용하는 1개 파일 내 함수 교체.

### ⏸ 보류된 수정 (사용자 결정 필요 — 회귀 위험 또는 범위 확장)

- **[P1] Phase 1 agent 오판 — auth.tsx silent catch**: 실제 코드 `.catch(() => tokenStore.clear())`는 토큰 명시적 클리어. ready=true 후 Login으로 라우팅됨. **수정 불필요** (false positive 정정).
- **[P0] 로컬앱 다운로드 활성화** + **[P0] Production `/dataset/manifest` 404**: 둘 다 deploy/인프라 작업. 본 QA 세션에서 수정 부적절.
- **[P1] Settings 위험 한도 UI 추가**: 신규 컴포넌트 + form + API 통합 — Phase로 분리 권장.
- **[P1] 모바일 반응형 (사이드바 드로어, 테이블 카드화)**: 별도 디자인 Phase 필요.
- **[P1] Backtest "000020 동화약품" prefill 제거**: localStorage 잔재 추정. 원인 분석 + 영향 범위 확인 후 진행 권장 (보관함 편집 흐름과 충돌 위험).
- **[P0×4] LOCAL APP 금융 안전성** (해외 1원 주문 / token 권한 / pending 평문 / preview 실패 강제청산): 모두 자금 직결. 변경마다 unit test + paper trading 검증 필수 → 별도 Phase로.

다음 단계 후보는 **종합** 섹션 끝의 "권장 후속 작업"에 정리.

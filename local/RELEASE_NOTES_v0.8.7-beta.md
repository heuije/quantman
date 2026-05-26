## 주요 변경 — GUI 모드 미국 자동매매 활성화 + 인앱 자동 업데이트

### 🚨 CRITICAL — GUI 모드 미국 cycle 영구 누락 fix

**증상:** v0.8.6 이하 사용자가 GUI에서 자동매매 시작해도 KST 22:25
(미국 정규장 -5분)에 cycle 미발동. 22:30 정규장 개장해도 미국 매수 안 됨.
한국(KRX) 08:55 cycle만 자동 실행됐음.

**원인:** `gui.py:_toggle_auto`가 `BackgroundScheduler`에 KRX 08:55 한 개만
`add_job`. `_plan_us_session`·`intraday_loop`·`heartbeat`·`dataset sync`·
`calendar sync` 등 다른 모든 job은 headless 모드(`python -m localapp.scheduler`)
의 `BlockingScheduler`에서만 등록되어 GUI 모드 사용자에겐 영구 누락.

**Fix:** `scheduler.py`에 `register_jobs(sched)` 헬퍼 추출. KRX cron + US
야간 플래너 + 캘린더 sync + heartbeat + dataset sync를 한 곳에서 등록.
`gui.py`도 동일 helper 호출 → GUI/headless 동일 job set.

**영향:**
- KST 12:00 매일 + 기동 즉시 1회 `_plan_us_session` 실행
- 미국 정규장 open−10분 `intraday_loop.start` (실시간 매도 WS)
- 미국 정규장 open−5분 `run_cycle(market="US")` (시초가 매수)
- 미국 장 마감 후 `intraday_loop.stop` + `run_post_close_settlement`
- DST·휴장 자동 반영 (캘린더 기반)

**v0.8.7 첫 실행 후 자동매매 시작:**
1. KIS 자격증명 + 페어링 완료 상태에서 "자동매매 시작" 클릭
2. log에 `미국 세션 스케줄 — loop HH:MM · 사이클 HH:MM · 정산 ...` 출력
3. 그 시각에 자동 cycle 발동

### 🆕 Phase 60 — 인앱 자동 업데이트

이번 release부터 새 버전 출시 시 **앱 안에서 한 번 클릭으로 업데이트**.
zip 다운로드·압축 해제·재실행 모두 자동.

**흐름:**
1. 앱 시작 시 GitHub releases API로 최신 버전 자동 조회 (background)
2. 새 버전 있으면 GUI 상단에 amber 배너 노출
3. [지금 업데이트] 클릭 → zip 다운로드 + 압축 해제 + `updater.bat` 실행
4. 앱 자체 종료 → updater가 파일 교체 후 새 버전 자동 시작

**참고:** v0.8.6 → v0.8.7로 가는 이번 한 번은 *수동 다운로드*. v0.8.7부터
모든 이후 업데이트는 자동.

### 🐛 미국 종목 preview 사이징 통화 불일치 fix

**증상:** 트레이딩 페이지 "매매 예정"에서 AMD 2,138주·약 100만"원" 같은
비정상 표시.

**원인:** server `preview_engine`이 `cash(KRW)` ÷ `prev_close(USD)` = 통화
불일치 qty 계산. server는 보안 원칙상 사용자 USD 주문가능액·실시간 환율
정확히 모름 (KIS 자격증명은 로컬앱 전용).

**Fix:** 미국 종목은 preview에서 `qty/est_total = null`로 표시. trader가
발주 시점에 USD 주문가능액으로 실제 사이징. UI는 "(USD)" 라벨 + "발주 시
결정" 안내.

### 기타

**리스크 한도 토글:** `max_position_pct` / `max_drawdown_pct`가 *실제 OFF*
default. 이전엔 UI에서 OFF 설정해도 backend에서 default 10%/20% 클램프
적용됐음. v0.8.7부터 None이면 클램프 skip — UI 의도와 일치.

**보유종목 컬럼 fallback:** KIS 잔고에 있지만 ledger에 없는 종목 (사용자
수동 매수) — strategy_name `"(수동 매수)"`, 평단가 KIS 응답 그대로 표시.
이전엔 `—` 공란이었음.

**일일 손실 한도 제거:** ExecutionPolicy.daily_loss_limit_pct 필드 제거.
종목 단위 실시간 현재가 매도(익절·손절·트레일링)로 위험 처리. user
모니터링 설정의 kill switch는 별도 유지.

### 검증

- `register_jobs(sched)` → 10 jobs 정상 등록
- `tsc --noEmit` 통과
- `vite build` 799ms 성공
- `pytest server/preview` 13 passed
- `pytest local/killswitch_intraday` 14 passed
- `pytest core/golden_backtest + core/tests` 41 passed

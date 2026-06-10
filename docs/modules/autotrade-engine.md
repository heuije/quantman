# 자동매매 엔진 〔담당: 조대표〕

> 학습 원장. 작업계획 착수 시 로그 entry(의도·계획)를 추가하고, 완수 시 시행착오·인사이트·결과구현을 채우고 전이 가능한 교훈을 맨 위 §교훈으로 distill한다.

## 📌 교훈·함정 (작업 전 먼저 읽기)

- 🔒 **KIS 자격증명·계좌·원시주문은 로컬 PC 전용·서버엔 안전정보만 — 위반 금지.** KIS 자격증명·계좌번호·원시 주문은 사용자 로컬 PC 전용. 서버 스키마·payload·로그 어디에도 들어가지 않는다. 서버엔 **안전정보만**(전략 정의·체결 요약·잔고 스냅샷·dataset).
- **kill switch·backtest=live parity 깨지면 자금 위험 → 변경 시 모의 1회 검증 필수.** kill switch(일일 손실 한도)·backtest=live parity는 자금 안전의 근간.
- **해외선물은 KIS 모의 미지원 → 실전+SimBroker로만 검증.** 국내선물은 라이브 검증 완료.
- **폴링 endpoint: ETag tag-first**(scalar 먼저 계산, 304면 큰 payload SELECT 안 함)**+필드 projection.** ETag는 scalar로 먼저 계산(tag-first)해 304면 큰 컬럼(payload)을 아예 SELECT하지 않게 하고, window 조회는 필요한 JSON 필드만 projection한다(Neon egress 인시던트 재발 방지 — `docs/incidents/2026-06-10-neon-data-transfer-quota.md`). 〔작성: 조대표〕
- **KIS endpoint 작업 전 API knowledge base 필수 참조**(`docs/kis-api/`).

## 현재 구조 (안정)

**기능.** 웹에서 만든 전략을 **사용자 PC의 로컬앱**이 KIS API로 모의/실전 자동 실행. 국내주식·국내선물·해외선물 지원. 백테스트와 **동일 IR**로 돌아 backtest=live 일치 보장.

**폴더.**
- `local/localapp/` — 로컬 실행 본체:
  - `trader.py`(매매 로직: 시장가/지정가·가격필터·ATR 사이징·슬리피지 측정)
  - `runner.py`(사이클 오케스트레이터)
  - `scheduler.py`(KST cron: 국내 08:55 메인·15:35 정산, 미국 동적 플래너)
  - `intraday_loop.py`(장중 틱 익절/손절/트레일링)
  - `killswitch.py`(일일 손실 한도 → 자동 청산+진입 차단)
  - `broker.py` + `kis_broker.py`(국내주식)
  - `kis_futures_broker.py`(국내선물)
  - `kis_overseas_futures.py`(해외선물)
  - `kis_websocket.py`·`kis_order_websocket.py`(시세·체결통보)
  - `sync_client.py`(서버 동기화)
  - `secrets_store.py`·`file_security.py`(자격증명 보관)
- `server/app/routers/commands.py` — **서버→로컬 명령 버스**(SSE): RUN_CYCLE_NOW·PAUSE/RESUME_AUTO·LIQUIDATE_ALL·CANCEL_ORDER·RESET_KILL_SWITCH·RECONCILE_NOW
- `server/app/routers/trading.py`(자동매매 타임라인·heartbeat) · `server/app/routers/sync.py`(동기화)
- `core/quant_core/ir_engine/live.py` — 라이브 신호 평가(백테스트와 같은 청산 우선순위)
- 테스트베드: core의 SimBroker(증거금·롱숏·정산손익, 선물 포함) — 자금 안전 경로 모의 검증

**구동 워크플로.** `scheduler` cron 또는 서버 `commands`(SSE)가 사이클 트리거 → `runner`가 `core/ir_engine/live.py`로 신호 평가 → `trader`가 사이징 후 KIS broker로 발주 → `killswitch`가 손실 한도 감시 → `sync_client`가 **안전정보만** 서버로 업로드.

**현황.**
- **국내선물:** 라이브 검증 완료.
- **해외선물:** KIS 모의 미지원 — 실전+SimBroker로만 검증.

## 작업계획 로그 (누적·최신 우선)

### [진행중] 자동매매 신뢰성 ultra 캠페인 — 다중일 무발주 근본 바로잡기 (2026-06-11 착수)

**의도.** 자동매매가 며칠 연속 발주를 못 하고(락 컨보이·거짓 킬스위치·체결 미기록·거짓 stale)
단건 핫픽스로는 실패가 반복돼, 6차원 심층 코드 리뷰로 결함 46건을 부류화하고 구조 수준에서
일괄 바로잡는다. 범위는 국내/해외 × 주식/선물 자동매매 4경로 전체이며, 신규 기능은 없다.

**참고자료(필독).** 사고 원장: `docs/incidents/2026-06-10-autotrading-week-retrospective.md` ·
결함 대장+패키지 설계: `docs/review-reports/2026-06-11-autotrading-ultra/SUMMARY.md`.

**계획.** 직렬 6 PR(머지 건별 승인): α 서버 지혈(KR preview 신선도 판정·bundle 이벤트 빌드·
timeline 정합) → β 정지점 제거(manifest 폴백 제거·락 timeout·사이클 저널) → γ 발주 시간창·
거부 처리·숏 패리티 → δ 체결 감지(현지날짜 구간 조회·fill 멱등·pending GC·reconcile 안전화·
예약주문 매칭) → ε equity 단일 산출기+신뢰성 게이트 → η 명령 버스 at-least-once.
결정 확정: US 예약주문은 유지+개장후 ccnl 매칭, Railway 영속 볼륨 채택(ops).
각 PR은 결함 재현 테스트 선작성 후 구현, 골든 byte-identical 보존.

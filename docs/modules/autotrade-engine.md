# 자동매매 엔진 〔담당: 조대표〕

> 학습 원장. 작업계획 착수 시 로그 entry(의도·계획)를 추가하고, 완수 시 시행착오·인사이트·결과구현을 채우고 전이 가능한 교훈을 맨 위 §교훈으로 distill한다.

## 📌 교훈·함정 (작업 전 먼저 읽기)

- 🔒 **KIS 자격증명·계좌·원시주문은 로컬 PC 전용·서버엔 안전정보만 — 위반 금지.** KIS 자격증명·계좌번호·원시 주문은 사용자 로컬 PC 전용. 서버 스키마·payload·로그 어디에도 들어가지 않는다. 서버엔 **안전정보만**(전략 정의·체결 요약·잔고 스냅샷·dataset).
- **kill switch·backtest=live parity 깨지면 자금 위험 → 변경 시 모의 1회 검증 필수.** kill switch(일일 손실 한도)·backtest=live parity는 자금 안전의 근간.
- **해외선물은 KIS 모의 미지원 → 실전+SimBroker로만 검증.** 국내선물은 라이브 검증 완료.
- **폴링 endpoint: ETag tag-first**(scalar 먼저 계산, 304면 큰 payload SELECT 안 함)**+필드 projection.** ETag는 scalar로 먼저 계산(tag-first)해 304면 큰 컬럼(payload)을 아예 SELECT하지 않게 하고, window 조회는 필요한 JSON 필드만 projection한다(Neon egress 인시던트 재발 방지 — `docs/incidents/2026-06-10-neon-data-transfer-quota.md`). 〔작성: 조대표〕
- **KIS endpoint 작업 전 API knowledge base 필수 참조**(`docs/kis-api/`).
- **발주 방식은 시장이 결정 — 국내(주식·선물)=시장가 단일, 미국주식=지정가(예약).** 국내는
  동시호가 단일가(08:55 시초가·종가)에 체결돼 시장가가 지정가 대비 슬리피지 손해가 없다(경매
  단일가). IR빌더가 지정가 *가격*을 표현할 수단이 없어 지정가 옵션은 dead+청산 footgun(종가 미체결
  →오버나이트 carry)이라 제거(v0.9.34). **시장가는 가격 미지정이라 코스피200선물 실시간가격제한
  (직전가 ±1%) 같은 "모의투자 상/하한가 오류" 거부가 원천적으로 불가** — 06-11 만기일 거부 사가의
  부류 종결. 미국주식만 KIS가 연속장 시장가 매수 미지원이라 지정가(예약, 직전가±tol). 거부
  (rejected)는 주문 미생성 → intent를 failed로 마감해 당일 재시도 허용(D5-5). catch-up(미스 사이클)은
  시초가 limit 변환 유지(백테스트 정렬). ⚠ 이전 `_live_limit`·`broker.quote_band`·±1% 클램프
  (`_KRX_FUT_RT_BAND_PCT`)는 국내 지정가 제거로 dead라 함께 삭제 — 부활 금지. 〔작성: 조대표〕
- **미국주식 지정가는 *신선한 현재가*×(1±tol)로 — 전일종가는 갭에 미체결(v0.9.35).** US 진입가가
  전일종가(`prev_close`)기반이라 애프터마켓·프리마켓 갭에 예약 limit이 미달→미체결이던 결함을, `_us_limit`이
  `_safe_price`(HHDFS00000300 실시간/프리마켓)×(1±tol)로 발주(전일종가는 조회실패 fallback)해 닫음 —
  **사이징은 prev_close 유지(백테스트 패리티)**, limit만 fresh. 예약매도는 MOO(31, 모의 미검증)→**00 지정가**
  (`sell_resv_limit`)로 모의=실전 통일. **종가청산 사이클**(`run_close_cycle market=US`·스케줄러 폐장−5분·
  신규 Trader라 `_reserved_us`=False→라이브 `sell_limit`)을 신설 — 종전엔 미국 당일매매가 다음 개장 MOO로
  하루 늦게 청산됐다. tolerance는 **미국 전용 라이브 버퍼**(국내 시장가는 무시)·default ±3%·유저 override.
  ⚠ MOC는 모의 미지원이라 폐장−5분 연속장 지정가가 최선의 종가 근사(백테스트 종가와 미세 발산은 불가피). 〔작성: 조대표〕

## 현재 구조 (안정)

**기능.** 웹에서 만든 전략을 **사용자 PC의 로컬앱**이 KIS API로 모의/실전 자동 실행. 국내주식·국내선물·해외선물 지원. 백테스트와 **동일 IR**로 돌아 backtest=live 일치 보장.

**폴더.**
- `local/localapp/` — 로컬 실행 본체:
  - `trader.py`(매매 로직: 시장가/지정가·가격필터·ATR 사이징·슬리피지 측정)
  - `runner.py`(사이클 오케스트레이터)
  - `scheduler.py`(KST cron: 국내 08:55 메인·15:25/15:40 종가청산·15:50 정산, 미국 동적 플래너)
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

## 발주 기준 매트릭스 (시장별 — 단일 출처) 〔작성: 조대표〕

**주문 유형은 유저가 고르지 않는다 — 거래 *시장*이 자동 결정한다(`exec_defaults`엔
주문유형 토글 없음).** 시가=진입, 종가=청산(당일매매 hold_days=0). 모의·실전 동일.

| 시장 | 주문유형 | 시가(진입) 발주 | 종가(청산) 발주 | 비고 |
|---|---|---|---|---|
| 국내주식 | **시장가** | 08:55(장시작 동시호가) → 09:00 시초가 단일가 | 15:25(장마감 동시호가 15:20~15:30) → 15:30 종가 단일가 | ✅ 구현 |
| 국내선물 | **시장가** | 08:55(동시호가) → 09:00 시초가 단일가 | **15:40**(동시호가 15:35~15:45) → 15:45 종가 단일가 | ✅ 구현(15:43→15:40, 여유 5분) |
| 미국주식 | **지정가(00)** | 개장−20분(22:10±DST) **예약 지정가**, 신선한가(`_safe_price`)×(1+tol) → 개장 체결 | **폐장−5분 라이브 지정가**, 신선한가×(1−tol) → 연속장 막판 체결 | ✅ 구현(v0.9.35). 예약 매수·매도 모두 00 지정가(MOO 모의 미검증→통일). tol default ±3%·유저 override |
| 해외선물(CME) | (보류) | 24h 연속·개폐장 경매 없음 | (보류) | KIS 모의 미지원 |

**왜 시장가(국내):** 동시호가 단일가 체결이라 시장가가 지정가 대비 슬리피지 손해
없음. 가격 미지정이라 코스피200선물 실시간가격제한(±1%) "상/하한가 오류" 거부도
원천 불가. IR빌더가 지정가 *가격*을 표현 못해 지정가는 dead+청산 footgun이었음(v0.9.34 제거).
**왜 지정가(미국):** KIS가 미국 연속장 시장가를 아예 미지원 — 지정가/LOO만 존재, 그나마
모의는 00 지정가만이라 모의=실전 통일 위해 **진입·청산·예약 매수/매도 전부 00 지정가**로 고정.
지정가가 시장가 손해 없이 체결되도록 **신선한 현재가(`_safe_price`=HHDFS00000300 실시간/프리마켓)
×(1±tol)** 로 발주(market-proxy) — 전일종가는 미국 갭을 못 담아 미체결을 유발하므로 fallback으로만.
tolerance는 **미국 전용 라이브 버퍼**(국내 무시)·default ±3%·전략 execution으로 유저 override.
종가청산은 MOC가 모의 미지원이라 폐장−5분 연속장 지정가가 최선의 종가 근사(백테스트 종가와
미세 발산 — 연속장 막판가 vs 종가 단일가). catch-up(미스 사이클)은 시초가 limit 변환 유지.
⚠ 해외선물(CME)은 모의 미지원이라 미배선(별개). ⚠ 라이브 실측(예약 모의 접수·프리마켓 시세·
종가청산 라운드트립)은 다음 미국 세션 게이트.

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

**진행 (2026-06-12).** 라이브 실증(GOOG 모의 라운드트립·국장 선물 시장가 발주 PASS)으로
잔여 결함을 구조 단위로 재편 — 증상 단위 δ·ε를 **WS-1 포지션 SSOT**(`fix/position-ssot`)
하나로 구현: ① RC1 해외 체결조회 날짜창(미국 현지 D-1~KST 오늘, `_overseas_query_window`)
② RC2 예약주문 번호공간 — 종목+사이드+수량 매칭 + `claimed_fills.json` 청구 dedup
③ pending 7일 GC ④ ★킬스위치/day_start/drawdown/equity의 **부분 잔고(fetch_failed) 평가
보류**(06-09 거짓 −98% 청산 재발 차단) ⑤ equity 시계열 통합자산화(D3-3) ⑥ **disk-SSOT
invariant(M9)** — 장수명 loop Trader의 stale 저장이 매도 포지션을 부활시키던 부류를
변경 즉시 영속+진입부 reload로 종결(체결 이벤트 3중 중복 기록의 뿌리). 재현 테스트 18건
red→green, local 364 전부 green. 병행 브랜치: `fix/preview-slot-freshness`(타임라인 슬롯
판정 — "오늘 신선분=done/갱신중/데드라인후 missed", server 197 green) ·
`fix/legacy-ir-migration`(K — 동결 ledger의 sweep/period_split을 파싱 경계에서 query/study로
의미보존 마이그레이션, 보유 구주식 3종 파싱 복원, core 245+루트 390+local 346 green) ·
`fix/server-infra-pruning-indexes`(#2 인덱스→#1 pruning cron→#3 스케줄러 가드, server 198
green) · `fix/parity-oracle-fx`(WS-2: 사이징 FX+백테스트=라이브 경제결과 오라클, 진행 중).
**머지는 전부 무거래창 + 건별 승인 대기.**

**진행 (2026-06-13).** 라이브 검증(국장 선물 종가청산 실패→수동복구·미장 GOOG 261주
방치·preview 거짓누락·equity 거짓폭락)으로 진단 모델 100% 입증, **새 미지 결함 0** →
통합 수정계획 확정(`docs/REDESIGN/autotrade-reliability-roadmap.md` = 단일 로드맵).
마지막 미구현 워크스트림 **θ**를 `fix/close-cycle-reliability`(#118 위 스택)로 구현:
① θ — `liquidate_day_trades`에 `_wait_pending`(일반 cycle 동일) + 정산 cron
15:35→**15:50** 재배치("발주창 이후 반드시 resolve 패스" 불변식, catch-up 임계 정합)
② N1 — 종가청산 시작 시 `_resolve_pending` 선실행으로 미기록 진입 체결(δ류)을 ledger
복원 후 순회, 체결확인 불능+계좌>원장이면 추측 발주 없이 "당일청산 불능" 표면화(병1
불변식 — 외부 보유 오인 매도 금지) ③ N2 — 타임라인 종가청산 마일스톤 3종(주식 15:25·
선물 15:40·미장 close−5분)+kind-aware 매칭(state_sync/정산/종가청산 push 교차 가장
차단)+`n_pending_unresolved>0`이면 ⚠ warning(거짓 녹색 "✓ 0건" 제거). 재현 테스트
15건 red→green, local 370·server 196·루트 골든 390·web build 전부 green. 인시던트:
`docs/incidents/2026-06-12-futures-close-fill-unrecorded.md`. ⚠ 배포 순서: 서버
타임라인 15:50은 로컬앱 릴리즈와 동시 웨이브로(구버전 정산 push는 same-day fallback
호환). 잔여=머지 웨이브(Phase 2)·라이브 게이트(Phase 3)·고아 정리(Phase 4).

### [진행중] 해외(미국) 선물 자동매매 하자보수 — 4계층 진단 + P1~P4 (2026-06-13)

**의도.** 해외선물(CME 6종: 원유·천연가스·금·은·나스닥·비트코인)이 국내주식/국내선물 대비
미성숙한 부분을 진단·수정한다. 근본원인=**KIS 모의 미지원→라이브 검증 불가→가정기반 단위
테스트만→통화축·하드닝·청산·equity가 국내 대비 미배선/버그**. 4계층 병렬 감사 + 고위험 주장
현재코드 직접 대조(에이전트의 "CME 백테스트 데이터 전무"는 오류 정정 — `data_fetcher.YFINANCE_SYMBOLS`에
6종 전부 CL=F 등 존재, 백테스트 계층 완전).

**P1~P4 (자율 검증가능, TDD, `fix/overseas-futures-autotrading` 브랜치):**
- **US-F1 통화 사이징**(core): 해외선물은 라이브 선물계좌 현금이 KRW인데 가격·승수가 USD
  (`_currency_of`가 US주식마스터 부재로 KRW 분류→KRX 사이징 분기). `fixed_amount`는 F-01에서
  FX 환산받았으나 `%`(futures_margin_pct) 경로 누락→~1,370배 과대 잠재. `event_buy_qty` %
  경로에 `spec.currency!=KRW`면 fx 환산 추가(미가용=0 보류)·`needed_symbols`에 USD선물% FX
  포함. **백테스트 `_budget`은 단일통화(cash=종목통화) 가정이라 무변경**(골든·선물엔진 보존,
  ⚠에이전트 "백테스트+라이브 둘다 수정" 프레이밍대로 했으면 골든 깨졌을 것 — 그라운딩이 결정적).
- **US-F2 equity 사본 drift**(local): `intraday_stop._ks_unified_equity_krw`가 "trader와 동일"
  주석인데 `futures_eval_krw` 누락→장중 kill-switch가 선물 손익 무시(**국내선물도 영향**).
  사본을 trader._unified_equity_krw 위임으로(lazy import). 중복 제거가 근본.
- **US-F4 종가청산 배선**(local): US 종가청산이 stock만 스케줄→해외선물 day-trade 오버나이트
  방치. 라우팅 메커니즘(liquidate_day_trades futures/US)은 이미 존재, 스케줄러 잡
  `us_close_cycle_futures`(폐장−5분) 추가. CME 정산시각 정밀정렬은 라이브 정밀화(문서화).
- **US-F5 브로커 하드닝**(local): 해외 전 메서드·국내 POST가 벌거숭이 requests. `_order_post`
  (EGW00201만 재시도·5xx 비재시도=중복발주 방지) 신설+POST 라우팅, 해외 read→기존 `_read_get`
  (5xx 재시도) 경유.

**검증.** TDD 재현 테스트 신규(P1 3·P2 1·P3 2·P4 5)+기존 갱신, **core 259·local 381·루트골든 390·
server 233 green**(골든 byte-identical 보존). 라이브 게이트는 코스피200만 개방(해외 차단 유지)이라
**전부 휴면**(프로덕션 무영향). **잔여(라이브 의존, 모의 미지원)**: 해외 잔고 populated 필드·취소
ORD_DT 추적·시세 sCalcDesz·해외 equity USD환산·라이브 게이트 개방 — 첫 실거래 캡처로 확정.
**미배포**(머지·로컬앱 릴리즈는 사용자 승인 대기).

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

- **2번째 브로커 = Broker Protocol 구현체 1개 + `make_broker` 분기로 끝 — 전략/백테스트/데이터 무변경.** LS증권을 KIS에 이어 추가(`ls_broker.py`). 선택=`secrets_store.active_broker` SSOT("kis"|"ls", 기본 kis→KIS byte-identical). **신규 브로커는 KIS 라이브 레슨을 day-1 이식**(주문응답 정규화·부분조회 실패 `fetch_failed` 마커·토큰 계정지문 캐시). **미검증 외부필드는 ⚠ 초안 + 테스트는 우리 계약(출력형)만 잠그고 입력 fixture는 가정** → 키 도착 시 fixture 교체(완료선언은 모의 E2E 후). ⚠ **`load_kis()`/`if kis:`로 브로커 동작을 게이트하는 사이트는 부류** — 신규 브로커 추가 시 `active_cred_ok()`류로 전수 치환(cycle·web명령·잔고·wizard·하드와이어 `KisBroker()`). 〔작성: 조대표〕

- **브로커 자산군 확장 = 자산군별 *단일 파일* 스코프 → KIS/공유파일 무변경이면 byte-identical이 자동(diff 노력 불요).** LS 4자산군 확장(국내주식·국내선물·해외주식·해외선물)에서 각 자산군은 **하나의 LS 파일만** 건드린다: 해외주식=`ls_broker.py`에 시장판정 내부분기(`_detect_market`로 국내/미국), 선물=`ls_futures_broker.py`. **시장판정은 브로커 무관 `market_index`(심볼→거래소) 재사용** — LS 전용 거래소코드(82/81)만 LS 파일에 둔다. `broker_router.py`는 이미 선물 CME→`overseas_*` 라우팅·CME price=0.0(dataset fallback)·CME cancel=NotImplemented를 갖춰 **무변경**. 〔작성: 조대표〕
- **해외(USD) 자산군은 브로커가 *KRW 환산* equity를 제공해야 — killswitch는 통화무인지(`_unified_equity_krw`가 단일 합산점).** 해외주식=`foreign_eval_krw=(usd_cash+Σqty·eval)×fx` 직접계산·해외선물=`equity=EvalAssetAmt(USD)×Xchrat`. **환율 미수신(fx/Xchrat≤0) 시 raise → 라우터가 `fetch_failed` 표식 → killswitch 보류**; *절대 0 반환 금지*(보유분을 평가금0으로 읽어 −98% 거짓청산 재발하는 부류). KIS 해외선물은 `CRCY_CD=TKR`로 서버가 KRW 직접 환산하지만 LS는 USD라 클라이언트 환산이 정답. 〔작성: 조대표〕
- **선물 듀얼 인증: 해외선물 계좌는 별도 토큰 컨텍스트.** `KisFuturesBroker`가 도메스틱(`load_kis_futures`)+해외(`load_kis_overseas_futures`) 자격증명을 둘 다 읽고 `_ov_token` 별도 보유하듯, `LsFuturesBroker`도 `self._post`=도메스틱(_LsAuth 상속) + `self._ov=_LsAuth(해외자격증명)` 별도 컨텍스트. 같은 appkey면 토큰캐시 공유·다른 appkey면 별도 — 계좌단위 API(AcntNo 토큰귀속 vs body)의 불확실성에 robust. 〔작성: 조대표〕
- **라우터가 호출 안 하는 브로커 메서드는 추가 금지(4원칙#2).** 해외선물 `overseas_price`/`overseas_pending_orders`/`overseas_orderable_qty`는 라우터가 CME에 0.0/stock위임/미사용이라 LS 미구현(추가 시 dead surface). 같은 이유로 안 쓰는 함수 인자(예: `_ov_ccld_raw(only_unfilled)` — pending 미포팅이라 무호출)는 제거. 최종 종합리뷰가 이런 dead surface를 포착하는 마지막 게이트. 〔작성: 조대표〕
- **미검증 외부필드 부류는 ⚠+gap-id+*안전 실패*로 봉인 → 모의 E2E서 일괄 실측.** 신규 증권사 응답필드·심볼 마스터는 키 없이 100% 확정 불가. 코드는 안전쪽으로: 심볼 resolve 실패→None→발주 skip(오발주 0)·조회 실패→raise→fetch_failed(보류). 단위테스트는 *출력 계약*만 잠그고 입력 fixture는 research 가정. 완료선언은 모의 E2E 후(4원칙#4). 〔작성: 조대표〕
- **비KIS 실시간 WS는 KisWebSocket 게이트가 아니라 브로커별 팩토리(`make_quote_ws`/`make_order_ws`)로 — REST 폴백은 *보유 stop-loss만* 평가하고 pending 체결 인지는 *사이클 경계에서만* 한다(06-24 주식 시가매수 체결 6.5h 지연 부류).** `_should_start_kis_ws()` 게이트는 LS=WS 미작동을 전제해 KIS 체결통보·시세 WS를 skip→REST 폴백만 쓰게 했다. 이를 active broker별 팩토리로 일반화(KIS→Kis*WebSocket 동일 객체·동일 start라 byte-identical, LS→Ls*WebSocket). LS WS는 KIS와 달리 **단일 bare `/websocket`(모의 29443·실전 9443)·OAuth 토큰 헤더 재사용·평문 JSON**(approval_key·AES·pipe 불필요 — `docs/ls-api/GOTCHAS` G-WS1~3). 체결통보(주식 `SC1`·국내선물 `C01`, tr_key 공란=내 주문 전체)·시세(`S3_`/`K3_` 종목당 둘 다 구독·`FC9` 선물, body 공통 `price`)를 **KIS H0STCNI0 evt 키로 정규화**(`ODER_NO`·`CNTG_QTY`·`CNTG_UNPR`·`CNTG_YN=2`)해 `_on_exec_event`·`on_tick`을 무수정 공유. 가이드의 `/websocket/stock`·`/futureoption` 경로는 모의서 timeout(bare만 동작)·**데이터(틱·체결) 실제 전달은 장중 live 검증**(connect+구독 ack은 모의 프로브로 확정). 시세 WS는 국내주식 한정(선물·미국 stop-loss는 양 브로커 모두 REST 폴링=parity). 〔작성: 조대표〕

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

### [완료-draft] 코스닥150선물(KQ150) 라이브 계약 배선 (2026-07-12, `feat/kq150-futures-autotrade`)
- **의도**: 데이터 계층 완료(PR#387 수급·#389 가격·exec_defaults KQ150 등록)된 코스닥150선물을 라이브 발주에 배선 — 심볼 "코스닥150선물"→브로커 계약코드(KIS·LS) 해석+잔고 역매핑+만기. 기존 코스피200선물 라이브 유저(LS) 무영향 절대 보장.
- **실측 확정(KIS 공개 마스터 fo_idx_code.mst 다운로드)**: KQ150 = root_char `3`·단축 prefix `A06`·라인 키워드 `KSQ150`. ⚠핸드오프 추정 "KOSDAQ150"은 오답(마스터엔 `KSQ150`·`코스닥150`) — 추측 배선 시 0건 매칭·조용한 거래불가였을 것(실측의 가치). KRX 잔고형 `106`(A0N→10N 패턴·모의 왕복 최종확인 대상).
- **구현(core 2파일)**: `_DOMESTIC_SPEC` 3-튜플화(+라인키워드)+KQ150 · `_DOMESTIC_KRX_PREFIX` +`106` · `_front_domestic` `"KOSPI200"` 하드코딩→`line_keyword` 파라미터화(기본값 보존=byte-identical) · `futures_expiry` `kosdaq150_2nd_thu` 독립분기. LS `_pick_front_kospi200`은 symbol-param+core파생이라 로직 무변경(자동전파). KIS 리졸버·broker_router·trader·server(region/category) 전부 위임이라 무변경.
- **byte-identical 보장**: 기존 코스피200/미니 경로 = 딕셔너리 새 키 추가·기본값 `KOSPI200` 유지·prefix 101/105/106 distinct·8자 가드. 골든+전 스위트 증명(core+root 1326·local 773·server 594 green).
- **부수 발견·수정**: PR#389가 KOSPI200 증거금 0.195→0.198(KRX 2026-07-06 정기변경) 반영하며 기대값 테스트 3건 갱신 누락 → origin/main 로컬 스위트 red였음. 카탈로그값으로 보정(test_sim_futures 2·test_fund_transparency 1). 다른 세션 인지 필요.
- **📌 교훈**: 새 선물 배선 전 브로커 마스터를 **실제 다운로드해 실측**하라 — 핸드오프 추정 키워드가 실제와 달랐다. 형식 불일치 실패는 조용함(resolve None→발주 보류·오발주 0이지만 "거래 안 됨"). 공유 함수 파라미터화는 기존 상품 키워드를 **기본값으로 보존**해 byte-identical 유지(회귀 테스트로 잠금).
- **LS 실측·배선 완료(07-13)**: LS 코스닥150은 지수선물마스터(t8467)가 아니라 **파생종목마스터 t8435 gubun="SF"** 제공(모의 실측: shcode `A0669000`·hname `KQF 2609` — prefix A06는 KIS와 동일). `index_futures_master`가 t8467+t8435(SF) 병합(additive·non-fatal — 코스피200 정규 보존). 부수 발견: 미니 A05도 t8467 부재·t8435 `MF` 필요(별도 작업 spawn). 교훈="코스닥150도 지수선물이니 지수선물마스터에 있겠지"가 오진 뿌리 — 실측이 문서추론·리서치를 둘 다 정정.
- **잔여**: KIS·LS **모의 1계약 왕복 ×2브로커**(미니 K200 전철 방지). **머지·릴리스는 사용자 승인**. draft PR#391.

### [진행중] 장중 신호 템플릿 P1 — 급등/상한가 마감형 종가창 스캔 진입 (2026-07-12 착수, `feat/intraday-template`)

**의도.** "실시간급 신호로 거래를 생성"하는 전략 부류의 자동매매 지원 첫 단계. 임의 장중 IR을 라이브로 번역하지 않고 **사전 검증된 템플릿 화이트리스트**(1호 `limit_up_close_v1`: 15:25 마감 동시호가 스캔 → 상한가 잠김 종목 종가 매수 → 익일 시가 매도)만 연동 허용 — 보장을 런타임 검증이 아니라 설계 상수로 만든다. 근거 연구=상한가 오버나이트(승률 74%·연 344~699건). 설계 SSOT=`docs/REDESIGN/intraday-template-redesign.md`(Phase 1→최종 통합).

**구현(P1).** 브로커 seam `scan_close_surge` **KIS·LS 동시 배선**(패리티 — KIS `FHPST01820000` 장마감예상→`FHKST01010100` 상한가 대조 / LS `t1488`→`t8407` 배치 uplmtprice) · `template_scan` 합성 후보(전략 수 무관 스캔 1회·전략별 재필터 잠김/임계/시장/max_daily_entries) → `run_close_cycle` 합류(`run_close_netting` 무변경 — 킬스위치·손실한도·커버리지·멱등 전수 상속) · trader `skip_unknown_template` 이중 안전망 · `push_snapshot` app_version 주입(서버 앱버전 게이트) · 서버 승격 게이트 템플릿 분기(kind=all 일반 차단과의 구조 충돌 해소) · preview "장중 스캔 대기".

**구현(P2 — 07-12 착수, `feat/watchlist-trigger` · "구현 선행/실측하며 디버깅" 사용자 결정).** 워치리스트 장중 돌파(`watchlist_trigger_v1`): ① 엔진 `fill="trigger"` — 정규형 신호(신규 지표 `high_change_1d`=당일 High/전일 Close−1 %)가 참인 바에서 max(시가, 전일종가×(1+임계%)) 보수 체결. 경계가의 단일 출처=신호 const(`trigger_threshold_of` — 엔진·검증기 공유), S-trigger가 on_signal·롱·정규형 강제(scheduled 경로의 close 오독 divergence 차단) ② local `EntryTriggerManager`(신규 entry_trigger.py) — 매도 전용 intraday_loop에 진입 감시 합류: tick 현재가/전일종가 임계 판정 → `_close_entry_blocked` 게이트 → `_enter_from_preview(entry_window="intraday")` 재사용(종목 게이트 전수 상속). **디스크 발화 기록 = 전략×종목×일 1회·저장이 발주보다 먼저(실패 시 발주 금지 — 중복 방지 우선)·M9 재기동 SSOT.** WS 예산 41 = 보유(청산 감시) 우선 + 잔여에 워치(초과분은 폴링 폴백 감시 — degrade 순위 구현), 폴링 루프 tick_fn/extra_symbols 확장 ③ server `_watch_used` 합산 admission control(watch_budget=30 = 41−보유여유 11·update 자기 제외) + preview "장중 트리거 대기". 검증: core 44·local 진입매니저 7·server 게이트 13 green(전 스위트는 PR 게이트).

**남은 것.** P1: ~~draft PR·머지~~(PR#371→38f3db6·Railway 배포 검증 07-12 完) → 로컬앱 릴리스 v0.9.72(월 아침 게이트 후) → 실측 ⓐ KIS/LS 스캔 TR 가용성(⚠07-12 주말 선행 시도 = dev 파이썬 keyring 자격증명 로드 불가 확정 — 릴리스된 앱에서 ⓑ와 함께) ⓑ 15:25 드라이런 ⓒ 소액 라이브+체결률 계측. P2: 전 스위트→draft PR(머지 승인 대기)→백테스트 실데이터 스모크→릴리스 v0.9.73→장중 페이퍼 관찰 1주→노출 PR(P1·P2 함께·별도 승인) → [완료] 전환·교훈 distill.

### [진행중] 자동매매 전 사이클 크래시 — Close-only 시리즈 × ATR 무가드 (2026-07-10 착수, `fix/indicator-ohlc-guard`)

**의도.** 07-07 22:13~ 로컬앱 전 사이클(아침/종가/미장/장중 loop)이 `KeyError: 'High'`로 크래시해 자동매매 전면 중단(`docs/incidents/2026-07-07-close-only-series-cycle-crash.md`). 근본=PR#325가 발행한 **국채 수익률 37종이 Close-only**인데 dataset_scope **ALL_SYMBOLS 안전망으로 전 사이클에 자동 유입**되고, `compute_all→add_atr`가 `df["High"]` **무가드 접근** → 시리즈 하나가 dataset 로드·사이클 전체를 죽임. 부류="High/Low/Volume 요구 지표의 무가드 접근"(Volume 2곳은 기존에 올바른 관용구 보유).

**구현.** `add_atr`(High/Low)·`add_high_deviation`(High·동일 부류 잠복)에 기존 Volume 관용구 적용 — 컬럼 부재 시 해당 지표만 **NaN 컬럼**(스키마 유지·Close-only에 ATR은 수학적 미정의라 NaN이 올바른 값). 회귀 7종(`test_indicators_ohlc_guard.py`) + **실데이터 재현 검증**(이 PC 실제 dataset·인시던트 동일 호출 130종/국채 37종 → 크래시 소멸·국채 pct 정상·주식/선물 ATR 정상). core만 변경(서버/웹/로컬 배선 무변경) — 서버 백테스트·챗 경로도 동일 가드 보호.

**교훈(distill 대기).** ① 새 매크로 피드는 ALL_SYMBOLS 등록 순간 **모든 로컬 사이클 dataset에 유입** — 데이터 형상(Close-only)이 소비자 암묵 전제(OHLCV)와 어긋나면 전 유저 동시 다운. ② 지표의 컬럼 요구는 가드가 불변식(요구 부재=NaN 컬럼·배치 계속) — "시리즈 하나가 사이클을 죽일 수 없다".

**남은 것.** 전체 스위트 확인 → PR·머지 → 로컬앱 릴리스 v0.9.69(코어가 exe에 번들이라 릴리스 필수) → 사용자 설치 후 다음 사이클 정상 확인 → [완료] 전환·§교훈 distill.

### [진행중] 비상청산 sid-미스매치 고아 (R6/D6) (2026-07-06 착수, `fix/emergency-liquidation-orphan`)

**의도.** 07-06 모의(LS 국내선물): 사용자가 계좌 킬스위치(LIQUIDATE_ALL)로 청산했는데 **원장에 반대방향 유령 롱4**가 새로 생김(`docs/incidents/2026-07-06-emergency-liquidation-sid-orphan.md`). 근본=`_apply_fill`이 체결 open/close를 **전략 id(sid)로 판정**하는데, 비상청산(`liquidate_all_held`)은 브로커를 **종목 단위**로 청산하며 매칭 안 되는 **합성 sid `liquidate:{symbol}`**로 주문 → BUY가 신규 롱으로 오기록. v0.9.65/66(R1 reconcile·R2 정규화)이 못 덮은 같은 부류 다른 진입점(=R6). 자금경로라 설계서 선제출→승인→구현.

**구현(설계서 §6 R6/D6).** `liquidation=True` 플래그를 발주→booking 전파(`_submit_sell`/`_submit_close_short`→`_after_submit`→`p["liquidation"]`→`_apply_fill`). 신규 `_book_liquidation_fill`: sid 무시하고 **(종목, 반대 side)** 매칭 차감(commingle 결정적 순서)·매칭 없으면 `external_liquidated` 기록만·**신규 포지션 절대 생성 금지(I7)**. `liquidate_all_held`이 `liquidation=True` 전달. 기존 `netted` 플래그 선례(기본 False=byte-identical). **경로 구분 확정:** 인트라데이 daily-loss 킬스위치(`_on_ks_trigger`)는 `trader.cycle` 청산 패스로 **원장 실 sid 청산** → R6 무관(D6은 웹 LIQUIDATE_ALL 경로만 수정). 서버/웹 무변경. 07-06 재현 회귀 포함 신규 5종 + local+core **1437 passed·회귀 0**.

**교훈(distill 대기).** 종목 단위 브로커 청산 ↔ 전략 단위 원장 판정의 seam — 비상/reconcile 등 **sid-무관 경로가 sid-키 booking을 재사용**할 때 조용한 전제 붕괴(2026-07-03 R2와 동형). 비상 booking은 "닫기만·절대 열지 않기" 불변식으로 강제.

**남은 것.** push·PR·머지(허락 게이트) → 로컬앱 릴리스 → 모의 재검증(비상청산 후 원장 유령 없음·브로커 정합) 후 [완료] 전환·§교훈 distill. 별도(R6 밖): 비상청산 BUY가 브로커 숏 실청산인지(norm_side 오판 시 2배 확대) 모의 HTS 실측.

### [진행중] 자동매매 명령 투명성 UX (2026-07-05 착수, `feat/autotrade-transparency-ux`)

**의도.** 유저가 비상청산(LIQUIDATE_ALL)을 눌렀는데 "모의투자 영업일이 아닙니다"로 거부됐지만 실패 신호가 안 가고 킬스위치만 ON이던 문제. 근본=데이터(n_rejected·사유)는 로컬→서버까지 오는데 웹 `send()`가 명령 ack(result)를 버리고 스냅샷만 리로드, 거부지표는 접힌 감사로그에만. 부류(T1~T6)로 웹·로컬 양쪽 표면화.

**구현(3커밋).** ①로컬: `analytics.emergency_liquidation_summary`(순수·거부사유 요약, ok/message) + gui LIQUIDATE_ALL ack 강화 + 데스크탑 결과배너(green/red). ②웹: `send()`가 `listCommands` 폴링→결과배너(T1), 확정 다이얼로그에 시장 phase 노출(T4), 감사로그 "최근 명령" 표(T5), StatusStrip에 `reconcile_blocked`·`n_daytrade_unclosed` 경보(T6·v0.9.65 신호), 킬스위치 "미청산 잔존" 표면화(T2b). **T2b 무조건 자동 재청산은 자금경로 위험이라 미채택**(기존 인트라데이 `_on_ks_trigger`가 실행+장중 케이스 담당).

**남은 것.** 커밋 완료(4f0f904·5088402·230c3cd)·미push. PR/머지/릴리스는 사용자 허락 대기. 검증=local 745 green·web tsc/eslint/build green(라이브 렌더는 프로덕션 로그인+페어링 필요).

### [진행중] 포지션 정합성 구조 재설계 — 원장↔브로커 분기 인시던트 (2026-07-04 착수, `fix/autotrade-position-integrity`)

**의도.** 라이브(모의·LS 국내선물)에서 원장↔브로커가 완전 분기한 인시던트(06-30~07-03, `docs/incidents/2026-07-03-futures-ledger-divergence.md`)의 구조 뿌리 4개를 부류 단위로 닫는다: R1 reconcile 파괴적 자동삭제(매칭 실패를 "외부 매도"로 단정) · R2 LS 잔고 KRX형 계약코드(101T9000) 정규화 조용한 실패 · R3 정합 불변식 부재 · R4 종가창 미실행 무감지. 서버/웹 무변경, R5 commingle·결함C(오버나이트 롱 hold0 서버측)는 후속.

**계획·구현(설계서 `docs/REDESIGN/autotrade-position-integrity-redesign.md`).** D1 core `dataset_for_contract`에 KRX 숫자형 프리픽스(101/105·8자 가드) 역매핑 → D2 LS 역매퍼 core 위임 + 라우터 정규화 실패 fail-loud(`symbol_unmapped`) → D3 reconcile fail-safe(`fetch_failed`/`symbol_unmapped` 시 선물 orphan 파괴 차단·`reconcile_blocked` 표면화·주식 차감 유지) → D4 정산 불변식 I5(당일매매 잔존 감지 `n_daytrade_unclosed`). 인시던트 재현 회귀 포함 테스트 5파일, local+core 1404 green(1 fail=선재 캘린더 테스트 격리 플레이크·별도 태스크).

**남은 것.** PR·머지(허락 게이트) → 로컬앱 릴리스 → 모의 재검증(reconcile in_sync·n_daytrade_unclosed=0·주식 수동매도 차감 유지) 후 [완료] 전환·교훈 distill.

### [완료-draft] LS증권 2번째 REST 브로커 — 국내주식 토대 (2026-06-17)

**의도.** KIS에 이어 LS증권(구 이베스트)을 자동매매 실행계층 2번째 REST 브로커로 추가. 국내주식 범위, 계좌·키 발급 전이라 draft(필드 미검증). 전략IR·백테스트·데이터 무변경(실행계층 국소 추가).

**구현(`feat/ls-broker`).** ① 시암 일반화: `secrets_store.active_broker` SSOT + `save_ls/load_ls`, `runner.make_broker` KIS|LS 분기(기본 kis 무변경). ② `ls_broker.py` LsBroker: OAuth `/oauth2/token`(expires_in 존중·계정지문 캐시)·throttle·`_post`(read재시도·order신중)·Broker 11메서드 국내주식(`normalize_ls_order_resp`·`account_snapshot` fetch_failed·`order_status` 표준어휘·resv는 명시적 NotImplementedError). ③ GUI 브로커 선택 라디오+LS 폼(KIS wizard byte-identical). ④ `docs/ls-api/` KB(공개소스 TR 매핑·⚠ 미검증). **subagent-driven**(구현→spec리뷰→품질리뷰) 5태스크.

**결과.** 머지 대기(draft 토대). local **438 green**·LS 신규 50테스트·KIS/골든 무변경. **B7 완료**=`load_kis()`/`if kis:` 게이팅 **부류**를 `active_cred_ok()`+`active_cred_label()` 헬퍼로 전수 닫음(gui web명령·`CANCEL_ORDER` make_broker·runner/intraday WS 게이트·잔고; **KIS byte-identical**·git grep 부류 0). **남은 것(키 필요):** **Phase C**(키 후)=응답필드 실측·**G10**(`order_status` t0425 chegb=2 미체결-only라 체결/취소 unknown→chegb=0 전환)·모의 E2E·라이브게이트·미확인5. **Phase 3 WS**=LsBroker `get_approval_key`/`ws_url` 미구현→LS 시세·체결 WS 둘 다 폴링-only. 상세=`docs/superpowers/plans/2026-06-17-ls-broker-autotrade.md`.

**교훈.** 위 §교훈 distill. 추가: order_status가 미체결-only TR이면 체결/취소를 못 봐 unknown→정산 reconcile 백스톱 의존(GOTCHAS G10) — 신규 브로커 체결인지는 전체-state 조회 TR 필요.

### [진행중] LS Phase C 라이브 테스트 — ⚠초안 필드 실측 (2026-06-20 착수, `feat/ls-broker-phase-c`)

**의도.** 사용자가 LS 모의계좌를 개설해 단위테스트(mock 경계)가 못 잡은 ⚠초안 필드(t0424/t1102/t0425 블록·필드명, 주문 성공 rsp_cd, **당일매매 핵심 t0425 체결인지 `status`**)를 실측 확정하고 fill→ledger→종가청산 통합 흐름을 검증한다. 범위=실행계층 LS 국소 교정만(전략/백테스트/데이터·KIS 무변경).

**준비물(이번 세션).** ① `local/verify_ls.py` — verify_kis 대칭 raw-캡처 프로브. `_post` 캡처 래퍼로 본문 중복 없이 각 TR 요청/응답 덤프(자격증명·계좌 자동 마스킹), `--kosdaq`(exchgubun)·`--order`(모의 1주 라운드트립 + **t0425 chegb=0 status 실측**). LS 키는 GUI wizard로 등록(`run.py setup`은 KIS 전용). ② `docs/ls-api/PHASE-C-LIVE-TEST.md` — 단계 런북(C-0 읽기→C-1 주문→C-2 풀사이클 E2E→C-3 실전 마이크로) + 실측 체크리스트(G11~G21·미확인5를 TR·필드·캡처단계·교정액션으로 매핑). 안전 게이트=모의먼저·1주·읽기→주문·킬스위치 수동감시.

**잔여(키 대기).** 사용자 LS 모의키 발급 후 C-0부터 raw 회수→내가 ls_broker 교정→C-1 status 확정 시 `order_status`를 chegb=0 전환(당일매매 잠금해제)→C-2 E2E. 완료선언은 모의 E2E 통과 후.

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
**전부 휴면**(프로덕션 무영향). **✅ P1~P4 머지·릴리즈**(독립리뷰 F-1 종가청산 동시성·F-2 EGW00201
HTTP500 반영, PR#129·v0.9.38-beta).

### [진행중] 해외선물 게이트 개방 블로커 G1~G3 닫기 (2026-06-13, 검토 후속)
end-to-end 준비도 독립 검토에서 게이트 개방 블로커 3건 식별 후 자율 수정:
- **G1 사이징 예산 base + G3 kill-switch equity**(같은 뿌리): 해외선물 주문 예산·equity가 해외선물
  *계좌*가 아니라 국내선물/주식 KRW 현금 기준이던 결함. 근본=잔고 OTFM1412R이 positions만 줌.
  **KIS 공식 스펙 OTFM1411R(예수금현황 inquire-deposit, CRCY_CD=TKR로 KRW환산)** 신규 배선 —
  `fm_ord_psbl_amt`(주문가능→예산)·`fm_tot_asst_evlu_amt`(총자산→equity). `parse_overseas_deposit`+
  `overseas_deposit`+`overseas_account_snapshot`이 positions+account 결합 → broker_router 병합이
  futures_order_cash/futures_eval_krw 채움(둘 다 KRW=FX 추측 없음). **추측 아닌 스펙 필드**(모의
  미지원이라 값 대조는 첫 실거래).
- **G2 시세**: CME 실시간시세 유료구독(EGW00553)+미스케일 raw라 broker_router.price(CME)가 손절에
  거짓 트리거하던 것 → **0 반환**(호출자 dataset/skip). 유료피드+scalc_desz는 라이브 단계.
- 검증: 신규 테스트(parse_deposit·account 결합·router 병합·graceful·price 0) **local 387·루트골든 390
  green**. KB: `docs/kis-api/GOTCHAS.md`(OTFM1411R). ⚠ **잔여 라이브 의존**: G4 체결조회 필드·G6 ODNO·
  종가청산 라운드트립·게이트 개방 = 첫 실거래. 미배포(머지·릴리즈 승인 대기).

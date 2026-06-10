# Claude 작업 가이드 — 퀀트 자동매매 플랫폼

이 파일은 Claude Code가 이 저장소에서 작업할 때 따라야 할 규칙과 트리거를 모은다.

---

## 1. 프로젝트 개요

- 한국 주식 자동매매 SaaS. 초중급 퀀트 트레이더 대상.
- 핵심 차별점: **문장형 빈칸 조건 설정** — 코드 없이 자연어 흐름으로 전략을 만든다.
- 사용자 흐름: 웹앱에서 전략 수립 → 모의/실전 모드 선택 → 사용자 PC의 로컬앱이 KIS API로 자동 실행.

## 2. 모노레포 구조 + 3대 엔진 (핸드오프)

> **경로는 모두 레포 루트 기준**(`core/`, `server/`, `web/`, `local/`). 아래 일부 구
> 섹션(§7~9)에 남아 있는 `platform/` 접두사는 레거시 — 루트 기준으로 읽을 것.

### 🤝 모듈 담당 맵 + 표기 규칙 (먼저 읽기)

이 문서는 **조대표·희제 공동 관리**다. 누가 무엇을 맡고 누가 썼는지 아래로 구분한다.

- **〔담당: 이름〕** — 그 모듈을 유지보수하는 사람.
- **〔작성: 이름〕** — 그 문단 내용을 직접 쓴 사람.
- 분담은 **유동적·유연** — 경계가 겹치면 PR/이슈에서 협의. 아래 맵은 현재 기준.
- **현재 §2 전체 작성 = 조대표.** 희제 담당 모듈(§2.6·2.7)도 지금은 조대표가
  아키텍처만 기술해 둔 상태다 — 희제가 자기 영역을 보강/재작성하면 그 부분 〔작성〕을 희제로 갱신할 것.

| 모듈 | 담당 | 핵심 위치 |
|---|---|---|
| 데이터 엔진 | **조대표** | `core/quant_core/data/` · `server`(캐시·cron) — §2.2 |
| 인사이트 엔진 (코어·스크리닝·회귀·백테스트) | **조대표** | `core/quant_core/ir_engine/` · `routers/ir*.py` — §2.3 |
| 자동매매 엔진 | **조대표** | `local/localapp/` · `routers/{commands,trading,sync}.py` — §2.4 |
| **개별종목분석** (단일종목 360·뉴스) | **희제** | `ir_engine`(describe 단일)·`routers/ir.py`·web 리포트 — §2.6 |
| **포트폴리오** (진단·관리) | **희제** | `routers/portfolio.py`·`ir_engine`(진단)·web 포트폴리오 — §2.7 |
| 웹 빌더·시각화(공통) | 조대표 (개별종목·포트폴리오 화면은 희제) | `web/src/` |

### 2.1 디렉터리 한눈에

```
(repo root = MercKR/quantman)
├── core/    quant_core — pure Python 엔진 (pip install -e). 데이터 정의·수집·백테스트·인사이트 전부 여기
├── server/  FastAPI — Railway 호스팅, Neon Postgres. 서빙·cron·동기화·preview·NL 컴파일
├── web/     React+TS+Vite — Vercel 호스팅. 노코드 빌더 + 결과 시각화
├── local/   Python Tkinter 데스크탑 — 사용자 PC. KIS REST+WS, PyInstaller 번들. 자동매매 실행
├── docs/    설계·계획·KIS API knowledge base (docs/kis-api/, docs/REDESIGN/, docs/superpowers/)
└── tests/   루트 통합/골든 테스트
```

**핵심:** 세 엔진은 폴더가 1:1로 떨어지지 않고 **layer로 나뉜다** — 로직은 `core/`에
정의되고, `server/`(서빙)·`web/`(UI)·`local/`(실행)에 배선된다. 아래는 엔진별로
**기능 / 폴더 / 구동 워크플로 / 유의사항**을 정리한 핸드오프다.

---

### 2.2 🔧 데이터 엔진 — "무엇을 수집해 어디에 뿌리나" 〔담당: 조대표〕

**기능.** 백테스트·인사이트가 쓰는 모든 데이터를 한 곳에서 정의·수집·검증해 공급.
출처는 데이터포인트당 **1개 원칙**(no-backup). 지원 현황(present/partial/absent)의
**진실원천 = `core/quant_core/data/spec.py`** — 새 데이터 추가/상태 확인은 여기부터.

**폴더.**
- `core/quant_core/data/spec.py` — 데이터 종류 등록부(진실원천). 무엇을 지원/미지원하는지
- `core/quant_core/data/feeds/` — **소스별 수집 모듈**(새 데이터 = 여기에 모듈 1개 추가):
  `fundamental_kr.py`(OpenDART)·`fundamental_us.py`(SEC)·`classification.py`(섹터/FDR)·
  `listing.py`(상장폐지)·`news_kr.py`(뉴스/네이버 검색 API)
- `core/quant_core/data_fetcher.py`(가격 OHLCV: FDR·yfinance)·`indicators.py`(기술지표 24종 자체산출)·`dataset.py`(조립)
- `core/quant_core/data/gate.py`·`deps.py`·`manifest.py` — 무결성 게이트(PIT·생존편향)·의존성 도출·수집 기록
- **서빙(server/app)**: `data_cache.py` = **Store A**(canonical parquet 시계열 — 백테스트·360리포트·SELECT 공급) ·
  `krx_cache.py` = **Store B**(스크리너 인메모리 스냅샷, 빠른 조회 전용) ·
  `naver_fundamentals.py`·`technical_cache.py`·`us_metrics_cache.py`(보조 캐시) ·
  `main.py`(cron 오케스트레이션 — KR 펀더멘털 매일 17:30 KST 등)

**구동 워크플로.** `server/app/main.py`의 cron이 시점마다 `feeds/` 수집 →
`data_cache`(Store A)에 적재 → 엔진/리포트가 소비. 스크리너는 별도로 가벼운
`krx_cache`(Store B) 스냅샷을 읽음. 뉴스만 예외 — 사전수집 없이 360 리포트 요청
시점에 `server/app/routers/ir.py`에서 **서버 엣지 on-demand 호출**(저장 안 함).

**유의사항.**
- **Store A ↔ Store B 분리는 의도적**(속도 최적화). 스크리너를 무거운 canonical로
  라우팅하지 말 것 — 회귀다. 일원화는 *출처(sourcing)*만, *서빙(serving)*은 분리 유지.
- **뉴스는 결정적 엔진 밖(서버 엣지)에서 붙인다** — 라이브 네트워크·env키 의존을
  core에 넣으면 골든 테스트 불변식이 깨진다. 같은 이유로 모든 라이브 데이터는 엣지에서.
- **partial(되지만 위험):** 한국 시세 분할조정 표기 미검증 / 시총·종목마스터가
  백테스트 store 미부착(스크리너 메모리에만) / 소스별 조정정책 혼재.
- **absent(못 가져옴):** 애널 추정치(무료 불가)·수급(외국인/기관, 공식 무료 API 없음·
  네이버 일별 순매매는 스크랩 가능하나 엔진 소비경로 미배선)·공매도·KR 지수 멤버십 이력.
- **미배포 대기:** 스크리너 PBR/PER을 360과 같은 OpenDART로 통일하는 작업이
  브랜치 `feat/data-engine-a1-valuation`에 구현·로컬검증 완료, **백필 커버리지 ~90% 도달 시 배포**(현재 ~60%).

---

### 2.3 🧠 인사이트 엔진 — "질문 → 분석 → 답변" 〔담당: 조대표 · 개별종목분석·포트폴리오 갈래는 희제 §2.6–2.7〕

**기능.** 백테스터를 "자연어 질문이 되는 기계"로 확장. 하나의 질문을
**동사 × 대상**으로 쪼갠다: `query ∈ {select, describe, relate, simulate}` × `study(axis×reduction)`.
- **select**(스크리닝, 예 "저평가 반도체주 3개") · **describe**(단일종목 360리포트·포트폴리오 진단) ·
  **relate**(다중팩터 횡단 회귀=Fama-MacBeth) · **simulate**(백테스트·스윕·기간분할)

> 〔담당 경계〕 엔진 코어 + `select`·`relate`·`simulate` = **조대표**. `describe` 갈래 중
> **개별종목분석(단일 360)·포트폴리오 진단**은 **희제** 담당 → §2.6·2.7 참조.

**폴더.**
- `core/quant_core/ir_engine/` — 엔진 본체:
  `run.py`(**`run_query` 디스패치** = 동사 라우팅) · `spec.py`(`StrategyIR` 스키마) ·
  `capabilities.py`(능력 노출) · `engine.py`·`backtest.py`(실행) · `live.py`(backtest=live 일치) ·
  `explain.py`·`metrics.py`·`sweep.py`
- `server/app/routers/ir.py` — 질문 실행 라우터 + 360 뉴스 facet enrichment
- `server/app/routers/ir_compile.py` — **자연어 → StrategyIR 컴파일러**(기본 Haiku 4.5, 롤백 env `QP_NL_COMPILE_MODEL`)
- `web/src/components/ResultCharts.tsx` 외 시각화 컴포넌트 — 동사별 결과 카드/차트(랭킹·360·진단·회귀·최적해)
- 설계 스펙: `docs/REDESIGN/question_layer_spec.md`

**구동 워크플로.** 웹/자연어 질문 → `ir_compile.py`(NL→StrategyIR 번역) →
`ir.py`가 `run_query` 호출 → `core/ir_engine`이 동사·study에 따라 계산 →
구조화 결과 반환 → 웹이 결과 종류별 컴포넌트로 시각화.

**유의사항.**
- **결정적 core 불변식.** 엔진은 네트워크·env·시계 의존 0 — 그래야 골든 테스트가
  고정된다. 라이브 데이터(뉴스 등)는 **서버 엣지에서 결과에 덧붙인다**(엔진 안에 넣지 말 것).
- `signal`은 describe에도 **필수**(스키마 계약).
- **이벤트(on_signal) 단일종목 숏 백테스트(신규).** `engine._run_on_signal`(이벤트 경로)이
  `direction="short"`를 무시(롱으로 계산)하던 갭 수정 — `_open`/`_close`/NAV를 `Position.sign`
  인지로(숏=sell-to-open·open에 거래세·buy-to-cover, NAV=`sign*shares*close*mult`). **롱(sign=+1)
  byte-identical(골든 보존)**. ⚠`_open`/`_close`/NAV/weight·margin-call NAV recompute 어디든 부호를
  빠뜨리면 숏 손익이 틀어진다(전부 `pos.sign` 경유). long_short 이벤트는 범위 외(롱 유지). 라이브
  숏(M5c 진입·Stage B 청산)과 backtest=live 패리티 회복.
- **당일매매(hold_days=0, 신규).** `Exit.hold_days=0` = 진입한 바의 종가에 청산
  (`fill=next_open`과 결합 시 시가→종가 당일 O→C, 분봉 불필요). 엔진은 **hold_days==0일 때만**
  next_open defer 청산을 같은 바 종가로 즉시 실행(engine.py 청산 디스패치) — hold_days≥1은
  byte-identical(골든 보존). ⚠이 게이트(`== 0`)를 일반화하면 진입 바 가격청산이 익일시가→당일종가로
  바뀌어 골든이 깨진다. **자동매매 종가청산 사이클(Stage B) 배선 완료** — 라이브는 사이클이
  아침·종가로 나뉘므로 `live.cycle_exit_reason(is_close=)`로 어느 사이클인지 구분(종가에만 당일청산,
  아침은 보유 유지). `trader.liquidate_day_trades(dataset, instrument_class)` + `runner.run_close_cycle`
  + 스케줄러 종가 cron(주식 15:25·선물 15:43, 단일가 발주창 내). 게이트 ⑤ 제거 → paper/live 허용.
  ⚠라이브 E2E(실 KIS 단일가 체결) 검증은 사용자 업데이트 후 대기(SimBroker·단위까지만 검증).
- **부호방향 long_short 라이브 양방향(M5d, 신규).** 조건별 롱/숏(예: S&P 부호→코스피200선물
  시가매수 or 시가매도) = `direction="long_short"` + score 부호방향. 핵심: **방향정책의 단일 출처
  `engine._direction_for(buy_bool, score_vals, base_sign, threshold)`** — condition은 base_sign×bool
  (단방향 보존), score는 부호방향(>thr 롱·<thr 숏·사이 0). `run_unified`(on_signal)가 `long_short`+score를
  허용(spec.py 예외)하고 `_open(sign)`·`dir_arrs`로 바별 방향 체결. **랭킹 long_short(top_n/top_pct·
  scheduled)와 구분** — 랭킹은 라이브 미지원 유지. ⚠`_direction_for`는 향후 scheduled 경로도 호출해
  방향정책 일원화할 **엔진 직교화의 1번 축**(현재는 on_signal만). 4계층 배선: spec 예외(on_signal+
  long_short+score) → 게이트(`_assert_live_tradable`: on_signal+전종목 선물 directional만 라이브 허용·
  랭킹/비선물 차단) → preview(`_evaluate_ir_strategy`가 shorts도 emit + 후보에 `direction` 부착) →
  executor(`_try_buy_one_symbol(cand_direction=)`로 `_submit_buy`/`_submit_open_short` 분기; 방향
  없는 long_short 후보는 무음 롱전환 방지 skip). 골든 14 byte-identical 보존. **단일/선물 한정**(다종목
  per-symbol 방향은 사이징 정규화 패리티 협의 후 — 현재 범위 외). 라이브 E2E는 사용자 모의 대기.
  **NL 컴파일러 라우팅(M5d):** `ir_compiler._route_directional`이 결정적으로 부호방향 long_short
  당일매매(비랭킹·hold_days=0)를 `on_signal`로 강제(LLM이 옛 쿡북대로 scheduled 내도 수렴) +
  idiom 1·6이 의도(이벤트/당일→on_signal·정기→scheduled)로 안내. scheduled+long_short+hold_days=0
  부정합을 닫음(scheduled는 당일청산 불가 → backtest≠live였음).
- **완성도 ~70~80%.** 보류: P3 데이터 4종, P4 2차최적화(QP), 일부 P6 결과형태 브라우저 미확인.
  NL·웹 라이브 검증 일부는 로그인 자격 경계로 자동검증 불가(사용자 세션 필요).
- 프로덕션 검증 채널 = 로그인된 `quantman.vercel.app` 페이지 컨텍스트에서
  `localStorage.qp_token` Bearer로 `quantman-production.up.railway.app` 호출.

---

### 2.4 🤝 자동매매 엔진 — "전략 → 모의/실전 발주" 〔담당: 조대표〕

**기능.** 웹에서 만든 전략을 **사용자 PC의 로컬앱**이 KIS API로 모의/실전 자동 실행.
국내주식·국내선물·해외선물 지원. 백테스트와 **동일 IR**로 돌아 backtest=live 일치 보장.

**폴더.**
- `local/localapp/` — 로컬 실행 본체:
  `trader.py`(매매 로직: 시장가/지정가·가격필터·ATR 사이징·슬리피지 측정) ·
  `runner.py`(사이클 오케스트레이터) · `scheduler.py`(KST cron: 국내 08:55 메인·15:35 정산, 미국 동적 플래너) ·
  `intraday_loop.py`(장중 틱 익절/손절/트레일링) · `killswitch.py`(일일 손실 한도 → 자동 청산+진입 차단) ·
  `broker.py` + `kis_broker.py`(국내주식) · `kis_futures_broker.py`(국내선물) · `kis_overseas_futures.py`(해외선물) ·
  `kis_websocket.py`·`kis_order_websocket.py`(시세·체결통보) · `sync_client.py`(서버 동기화) · `secrets_store.py`·`file_security.py`(자격증명 보관)
- `server/app/routers/commands.py` — **서버→로컬 명령 버스**(SSE): RUN_CYCLE_NOW·PAUSE/RESUME_AUTO·LIQUIDATE_ALL·CANCEL_ORDER·RESET_KILL_SWITCH·RECONCILE_NOW
- `server/app/routers/trading.py`(자동매매 타임라인·heartbeat) · `server/app/routers/sync.py`(동기화)
- `core/quant_core/ir_engine/live.py` — 라이브 신호 평가(백테스트와 같은 청산 우선순위)
- 테스트베드: core의 SimBroker(증거금·롱숏·정산손익, 선물 포함) — 자금 안전 경로 모의 검증

**구동 워크플로.** `scheduler` cron 또는 서버 `commands`(SSE)가 사이클 트리거 →
`runner`가 `core/ir_engine/live.py`로 신호 평가 → `trader`가 사이징 후 KIS broker로
발주 → `killswitch`가 손실 한도 감시 → `sync_client`가 **안전정보만** 서버로 업로드.

**유의사항 (보안 — 위반 금지).**
- **KIS 자격증명·계좌번호·원시 주문은 사용자 로컬 PC 전용.** 서버 스키마·payload·로그
  어디에도 들어가지 않는다. 서버엔 **안전정보만**(전략 정의·체결 요약·잔고 스냅샷·dataset).
- **국내선물은 라이브 검증 완료, 해외선물은 KIS 모의 미지원**(실전+SimBroker로만 검증).
- kill switch(일일 손실 한도)·backtest=live parity는 깨지면 자금 위험 — 변경 시 모의 1회 검증 필수.
- **폴링 endpoint 설계 원칙 〔작성: 조대표〕**: ETag는 scalar로 먼저 계산(tag-first)해
  304면 큰 컬럼(payload)을 아예 SELECT하지 않게 하고, window 조회는 필요한 JSON 필드만
  projection한다(Neon egress 인시던트 재발 방지 — `docs/incidents/2026-06-10-neon-data-transfer-quota.md`).
- KIS endpoint 작업 전 **§8 API knowledge base 필수 참조**.

---

### 2.5 배포 토폴로지

- **웹앱:** Vercel (production + preview) — `origin/main` push → 자동 deploy
- **서버:** Railway (Neon Postgres) — `origin/main` push → 자동 deploy
- **로컬앱:** PyInstaller zip → `MercKR/quantman-releases` (public repo) GitHub Release

> ⚠ 열려 있는 브라우저 탭은 reload 전까지 **이전 JS 번들**을 서빙한다(정상 SPA 동작).
> 배포 후 웹 확인은 `location.reload()` 먼저.

---

### 2.6 🔍 개별종목분석 〔담당: 희제 · 작성: 조대표(희제 보강 영역)〕

**기능.** 종목 하나를 평문으로 풀어 보여주는 단일종목 분석 — 인사이트 엔진
`describe`(단일) 갈래 + "왜 움직였나" 뉴스. 소매 사용자 최대 수요 지점.

**관련 위치(현재 파악 기준 — 희제 확인·보강).**
- `core/quant_core/ir_engine/run.py` — `run_describe_report`(단일종목 360 지표)
- `server/app/routers/ir.py` — `_attach_symbol_news`(서버 엣지 뉴스 facet)
- `web/src/components/ResultCharts.tsx` — `ReportCards`(360 카드 + 뉴스)

**유의사항.** 뉴스는 저장 안 하는 on-demand. 360은 인사이트 엔진 + 데이터 엔진
(펀더멘털·밸류)에 의존 → 데이터 엔진 변경 시 영향. 상세 설계·로드맵은 희제가 보강.

### 2.7 📁 포트폴리오 〔담당: 희제 · 작성: 조대표(희제 보강 영역)〕

**기능.** 보유/가상 포트폴리오의 진단(연변동성·집중도 HHI·섹터 쏠림·가중 밸류)과
관리 — 인사이트 엔진 `describe`(포트폴리오) 갈래.

**관련 위치(현재 파악 기준 — 희제 확인·보강).**
- `server/app/routers/portfolio.py` — 포트폴리오 API
- `core/quant_core/ir_engine/run.py` — `run_portfolio_diagnosis`(진단)
- `web/src/` — 포트폴리오 화면·도넛(섹터) 시각화

**유의사항.** 섹터 분류(데이터 엔진 `static.classification`)·밸류 컬럼에 의존.
상세 설계·로드맵은 희제가 보강.

## 3. 보안 원칙 (위반 금지)

- **KIS 자격증명·계좌번호·원시 주문은 사용자 로컬 PC 전용.**
  서버 스키마·payload·로그 어디에도 들어가지 않는다.
- **서버에는 안전정보만** — 전략 정의, 체결 로그 요약, 잔고 스냅샷.
- **Git push는 사용자 명시 허락 시에만.** 자동 push 금지.
- **로컬앱 토큰 파일은 Windows ACL로 사용자 전용** (Phase 41-C-2/3).

## 4. 코딩·협업 규칙 — 핵심 4원칙

모든 작업에 적용. 위반 의심 시 즉시 멈추고 사용자와 합의한다.

- **근본 원인 해결.** 본질적 해결이 가능한 상황에서 임시방편 fallback·예외
  무시·`except: pass`·`or default`·증상 봉합용 가드 금지. 증상이 아니라 원인을
  고친다. fallback이 정당한 경우는 외부 시스템(브로커·OS·네트워크)의 진짜
  한계뿐 — 그때도 *왜 필요한지* 명시 주석을 단다.
- **Over-engineering 금지.** 서비스 핵심 가치 구현에 필수적이지 않은 부차
  기능·옵션·추상화 추가 금지. "혹시 모르니"로 옵션·계층·플래그를 늘리지
  않는다. 호출자가 1곳뿐인 추상화, 사용처 없는 옵션·환경변수, dead config,
  미사용 분기·파라미터를 의심한다. 업계 표준이 단일 동작이면 단일,
  다중이면 합의된 다중만. 유저와 개발자를 혼란스럽게 만드는 부차 표면은
  제거가 추가보다 우선.
- **Overthinking 금지.** 같은 결과를 더 단순·효율적으로 낼 방법이 있는데
  복잡한 workflow나 불필요한 코드를 만들지 않는다. 단순·명시·직관 우선.
  다단 캐시·중복 가드·과도한 계층화·"만약을 위한" 추가 단계는 그 복잡도가
  *실제로 측정된* 문제를 해결할 때만 정당하다. 두 안이 결과가 같으면
  코드가 짧고 추론이 쉬운 쪽을 선택한다.
- **검증된 해결책만.** 변경은 실제 동작·테스트·신호로 검증한 뒤에만
  "완료"라 선언한다. 추측("should work", "아마 동작할 것")으로 품질을
  저하시키지 않는다. UI 변경 = 브라우저(Claude in Chrome)로 동작·포커스·
  에러 상태 확인, 자금 안전 경로 = paper/MockBroker 시나리오 1회, 코드
  품질 = lint·type·test·golden 신호. 검증이 불가능하면 "검증 불가" 사실을
  명시 보고하고 자율 완료 선언하지 않는다.

운영 규칙:
- **CLAUDE.md를 살아있는 핸드오프로 유지(필수).** 이 저장소는 조대표·희제가 모듈을
  나눠 공동개발한다 — 한쪽의 변경이 다른 쪽 컨텍스트에서 빠지면 협업이 깨진다. 작업을
  진행할 때 다음이 생기면 **같은 PR 안에서** 그 **작업 계획·의도·유의사항을 CLAUDE.md
  (특히 §2 핸드오프·모듈 담당 맵)에 반영**한다:
  - ① 새 모듈·기능 추가, ② 아키텍처·워크플로·데이터 흐름 변경, ③ 담당/소유 모듈 경계
    변동, ④ 공동개발자가 모르면 깨질 **비자명 유의사항**(가정·엣지·미배포·미배선·보안 경계·
    교차 모듈 영향).
  - 표기는 §2 규칙을 따른다: 본인이 쓴 내용은 〔작성: 이름〕, 모듈 유지보수자는 〔담당: 이름〕.
    남의 담당 모듈을 대신 기술하면 작성자를 거짓 귀속하지 말고 "작성=본인(보강 영역)"으로 정직히.
  - **단, 4원칙(특히 Over-/Overthinking 금지) 적용:** 사소한 변경·자명한 내용까지 적지
    않는다. **공동개발자의 행동이 달라지는 정보만** 남긴다 — 문서도 핵심만, 군더더기 금지.
- **Git 협업 워크플로(필수) — 충돌·중복·유실 방지.** 2명이 여러 세션·worktree로 병렬
  작업하므로 절차를 강제한다:
  - **작업 전:** `git fetch` 후 **열린 PR 확인**(SessionStart 훅이 자동 출력). 내가 만질
    파일·담당 경계(§2 맵)가 다른 PR과 겹치면 **시작 전** PR/이슈에서 협의한다.
  - **시작 시 draft PR:** 의도를 *끝*이 아니라 *시작*에 broadcast한다(유실·중복 방지).
    역할 구분 — **시시각각 바뀌는 in-flight 진척은 draft PR 본문에**, **구조적 핸드오프
    (새 모듈·아키텍처·담당 경계)는 위 규칙대로 CLAUDE.md §2에.** 진행상황을 CLAUDE.md에
    적지 않는다(거기서 또 충돌난다).
  - **브랜치·push:** `feat/`·`fix/` 작업 단위 브랜치로만. **main 직접 push 금지**(pre-push
    훅이 차단). clone·worktree마다 1회: `git config core.hooksPath .githooks`.
  - **짧게·자주 머지:** 브랜치는 오래 두면 발산한다(과거 main 작업트리 −212커밋 drift 사례).
    매일 main에 머지, merge 후 알림, 다음 작업은 **최신 main에서 pull 후** 시작한다.
- **규모 있는 작업: 설계안 제시 → 질문 → 승인 → 구현.** 곧장 코드부터 쓰지 않는다.
- **공백·인코딩.** Windows cp949 환경. UTF-8 명시 필요한 경우 `-Encoding utf8` 지정.

## 5. 디자인

`DESIGN.md` 참조. 색상·타이포·간격·컴포넌트 패턴 모두 거기 정의.
새 컴포넌트는 이 시스템 안에서 만들고, 벗어나기 전에 합의한다.

## 6. 전체 리뷰 트리거

사용자가 다음 표현 중 하나를 쓰면 즉시 `REVIEW_PLAYBOOK.md`를 읽고 거기 정의된
10단계 (Phase 0~9)를 순차 실행한다.

**트리거 phrase:**
- `/풀리뷰`
- `/full-review`
- `풀리뷰 실행`
- `full review run`

산출물은 `docs/review-reports/YYYY-MM-DD-HHMM/` 폴더에 phase별로 저장,
최종 `SUMMARY.md`로 통합.

총 예산 ~2.5~3시간. 중간 STOP/PAUSE 시 진행 상태 저장 후 멈춤.

## 7. 진단 — 로그 채널을 직접 CLI로 조회

추측 금지. 사용자 신고 또는 cycle·preview 이상 의심 시 다음 채널을
**직접 CLI로 호출**해 실제 로그를 확보한 뒤 진단한다. 로컬 추측만으로
근거 없는 가설을 내지 않는다 (4원칙 "검증된 해결책만"의 진단 단계 적용).

**인시던트 기록(항상).** 프로덕션/인프라 장애는 해소 직후 `docs/incidents/`에 파일 1개로
**발생·대응·결과**를 남긴다(차후 참고). 형식·인덱스는 `docs/incidents/README.md` 참조.

**채널별 명령** (모두 user 머신에 인증·설치 완료):

```bash
# Railway — 서버 stdout/stderr (cron·예외·HTTP·DB 에러)
railway logs --since 5h --lines 1000 --filter "@level:error"
railway logs --since 2026-05-27T22:20:00Z --until 2026-05-27T23:00:00Z
railway logs --http --status ">=500" --lines 50

# Vercel — 웹앱 build·deploy·serverless runtime
npx vercel ls quantman                                          # 최근 deploy 목록
npx vercel inspect <deployment-url> --logs                       # 특정 deploy build log
npx vercel logs <production-url>                                 # runtime log

# GitHub — release·workflow·PR·issue 등 모든 API
gh release list --repo MercKR/quantman-releases
gh release view <tag> --repo MercKR/quantman-releases --json assets
gh api repos/MercKR/quantman-releases/releases/latest
gh run list --workflow=<name> --limit 10                          # actions 워크플로
gh pr view <number>                                               # PR 상세

# 로컬앱 진단 (사용자 PC)
tail -100 ~/.quant-platform/logs/localapp.log
cat ~/.quant-platform/preview_cache.json     # 마지막 server preview 응답 캐시
tail -10 ~/.quant-platform/cycles.jsonl       # 최근 사이클 history
tail -10 ~/.quant-platform/orders.jsonl       # 발주 이벤트
ls -la ~/.quant-platform/*.json               # 모든 state 파일 mtime
```

**권한 없으면 사용자에게 요청.** Railway·Vercel·GitHub 모두 user
자격증명으로 동작 — 별도 환경에서 인증 못 받았으면 즉시 알리고
대안(스크린샷 요청·웹 UI 가이드) 안내.

**모니터링 background 가동 패턴** (장시간 사용자 부재 시):

```bash
# /tmp/quantman-monitor.sh 같은 스크립트로 tail -F + 5min 주기 server health
# bash 스크립트를 run_in_background:true로 spawn → /tmp/quantman-monitor.log에 누적
# 사용자 ping 시 즉시 cat·grep으로 진단
```

## 8. 외부 API knowledge base — 작업 전 필수 참조

외부 API 호출·결함 진단·새 endpoint 사용 시 **추측 금지** — 먼저 **`docs/api-index.md`(레지스트리)**에서
그 API의 *검증된 문서 접근법*을 찾아 확인한다(🟢 WebFetch / 🟠 WebSearch(봇차단 API) / 🟡 패키지소스 /
🔵 로컬 / 🟣 스킬). OpenDART status를 안 보고 추측해 사고 난 부류의 재발 방지.

**KIS**는 로컬 knowledge base가 가장 충실 — 작업 전 다음 순서:

1. **`docs/kis-api/INDEX.md`** grep으로 endpoint 후보 찾기
   ```bash
   grep -i "시초가\|open\|시가" docs/kis-api/INDEX.md
   ```
2. **`docs/kis-api/endpoints/{TR_ID}_*.md`** 읽기 — request/response/모의실전/한계
3. **`docs/kis-api/GOTCHAS.md`** 한 번 훑기 (1~2분) — 실측 발견사항
4. raw docs (`docs/kis-api/raw/*.xlsx`) 더 깊은 정보 필요 시 직접 열어 확인

작업 중 발견·새 endpoint 사용·결함 진단 시 **즉시 기록** (자가발전):

| 발견 종류 | 기록 위치 |
|---|---|
| 새 endpoint 사용 | `endpoints/{TR_ID}_*.md` 작성 (없으면 신규) |
| 공식 doc과 실측 다름 | `GOTCHAS.md` 상단에 entry 추가 (날짜·증상·원인·해결·우리 코드) |
| 릴리즈 fix | `CHANGELOG.md`에 entry |
| 우리 코드에서 사용 위치 | endpoint .md의 `우리 코드 위치` 섹션에 file:line 추가 |

**doc 부족 시 사용자에게 명시 요청**: "KIS docs '해외주식 주문' sheet xlsx
필요합니다. 받아서 `docs/kis-api/raw/`에 추가해주세요" 형식.

**KIS 외 API의 접근법·gotcha는 `docs/api-index.md` 참조.** 신규 API knowledge base(로컬 `docs/{api-name}/`)
구축은 사용자가 docs 제공 시점에 KIS와 같은 구조로 추가하고 `docs/api-index.md`에 행을 등록한다.

## 9. 자주 쓰는 명령

```powershell
# 웹 dev 서버
cd web; bun run dev

# 서버 로컬 실행 (core는 먼저 pip install -e core/)
cd server; uvicorn app.main:app --reload

# 로컬앱 실행
cd local; python -m localapp

# 백테스트 골든 테스트
pytest tests/golden_backtest.py -v

# API knowledge base 검색
grep -i <키워드> docs/kis-api/INDEX.md
cat docs/kis-api/endpoints/HHDFS76200200_*.md
cat docs/kis-api/GOTCHAS.md
```

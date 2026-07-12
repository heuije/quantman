# 데이터 완결성·커버리지 강화 (Data Completeness & Coverage)

**상태:** Phase 0 착수 (2026-06-29). 담당=조대표. 사장님 승인.
**브랜치:** `feat/data-coverage-manifest` (worktree `_wt-data-coverage`).
**선행 분석:** 코드 감사 + 디스크 실측 + 프로덕션 env/로그 + 라이브 프로브(아래 검증 사실).

---

## 1. 목표 & 원칙

챗봇이 유저 질문에 **편향 없이 정확히** 답하도록, 데이터 수집을 *완결적·일관적*으로 강화한다:
- KR/US 각 유니버스 내에서 **동일 기간·동일 필드·무결손** 데이터셋을 지향.
- 편향의 진짜 원인은 "데이터가 얕은 것"이 아니라 **"없는 걸 0/있는 것처럼 취급"**하는 것 → **null ≠ 0** 보장이 1순위.
- 4원칙 준수: 기존 자산 위에 얹는다(중복 구축 금지). 측정→노출을 먼저, 깊이 백필은 그 다음.

## 2. 승인된 결정 (2026-06-29)

1. **US 유니버스 포함규칙** = 보통주 + ETF만 (워런트/유닛/우선주 제외).
2. **깊이 통일** = 가격·수급·컨센서스 모두 **2010** (단편 조각 배제 — 컨센서스는 2006까지 가능하나 2010로 통일).
3. **착수 순서** = **Phase 0(커버리지 측정+노출) 먼저**, 그 다음 깊이 백필.

## 3. 가용 범위 — 검증된 사실

| 데이터 | KR 가능 깊이 | US 가능 깊이 | 비고 |
|---|---|---|---|
| OHLCV | 2010~ (FDR ~2000; 현 `start="2015-01-01"`는 설정) | 2010~(이미) | 설정 변경 + 1회 소급 백필 |
| 지수·선물·원자재 | 2010~(이미) | 2010~(이미) | 코인만 BTC 2017·FNG 2018 (소스한계) |
| 분기재무 | **2015~** (OpenDART 바닥 — 소스한계) | 2009~(이미, SEC) | 2010~14 KR 재무 무료 불가 |
| 컨센서스 | **2006~** (한경, 프로브 검증) | ❌ 없음 | 시장구조 |
| 수급(flow) | **2010~** (pykrx) | ❌ 직접등가 없음 (대안 13F) | KRX 로그인·봇차단 리스크 |
| 섹터·업종 | 전종목 가능 (KSIC/OpenDART) | 전종목 가능 (SIC/SEC) | 현재 KR 3,269·US 503(S&P500)만 |
| 배당수익률 | 2015~ (DPS) | 깊게 가능 | |
| 외인소진/보유 | 2010~ (KRX) | ❌ 개념 미적용 | |

**프로브 결과(2026-06-29):**
- 한경 컨센서스: 2006/2008/2010/2012 전부 140~160건 반환 → **2006까지 제공**.
- US 유니버스 12,153 vs NASDAQ Trader 공식(보통주 7,462·ETF 5,419): 공식 보통주의 **87.1%(6,503) 커버**, 누락 959는 대부분 워런트/우선주/유닛, 미매칭 231=ADR/stale.

**핵심:** "2015부터"의 거의 전부가 소스한계 아닌 **설정 파라미터**. 진짜 바닥은 KR 분기재무(2015)·US 수급(없음)·코인(2017)뿐.

## 4. 기존 자산 (재사용 — 표면 맵 검증)

| 컴포넌트 | 역할 | file |
|---|---|---|
| `spec.py` REGISTRY | 피드 요구 SSOT — 각 피드 `provides`(필드)·`xs_completeness`·`point_in_time`·`current_status` | `core/quant_core/data/spec.py` |
| `DataManifest`/`SymbolManifest`/`FeedManifest` | 실측 메타 스키마 (per-symbol 기간·feed status·PIT) | `core/quant_core/data/manifest.py` |
| `build_dataset_manifest` | dataset→manifest 빌더. **펀더 컬럼 유무로 feed status 실측(:72-86)** = 필드 커버리지 패턴 존재 | `server/app/data_manifest.py` |
| `evaluate_data_soundness` | 4액션 무결손 게이트 (가용성·결손·조정·PIT·생존편향·워밍업) — 완비 | `core/quant_core/data/gate.py:45` |
| `assess_data_quality` | Phase 0.5 실행 전 품질(missing/stale/gap, 교차심볼) | `core/quant_core/ir_engine/data_quality.py` |
| `classify_status` | 결과 품질 계약 — `diagnostics`에 `coverage`·`stale_symbols` 적재, 모델·웹 소비 | `core/quant_core/ir_engine/result_status.py:31-65` |
| `provenance.py` | 출처 큐레이션(챗 프롬프트·UI) | `core/quant_core/data/provenance.py` |

## 5. 진짜 갭 2개 (Phase 0가 닫는 것)

1. **필드×종목×기간 커버리지 행렬 부재** — `SymbolManifest`는 가격 행수만. 펀더/플로우/컨센서스의 종목별 first/last/밀도 없음.
2. **챗 경로 게이트 미배선** — `chat/tools.py:300,356,479`의 `strategy_from_spec(ir, dataset)`가 `manifest=` 미전달 → 게이트·커버리지가 챗 결과에 안 흐름(IR 라우터 `ir.py:254-256`은 전달 — 비대칭).

---

## 6. Phase 0 — 커버리지 측정 + 노출 (상세)

> 원칙: 측정→노출. 프로덕션 데이터 *수집*은 안 바꾼다(자금/외부상태 영향 0). 순수 메타·결과계약만.

### 0a. 필드 커버리지 (manifest 확장)
- `SymbolManifest`에 필드별 커버리지 추가: `field_coverage: dict[field -> {first, last, n}]`.
  관측 가능한 필드(예: `pb_ratio`,`trailing_pe`,`shares_outstanding`,`inst_net_buy`,`foreign_net_buy`,`consensus_target` …)에 대해 dataset의 각 심볼 DataFrame에서 비결측 구간 first/last/count 산출.
- `build_dataset_manifest`(`data_manifest.py`)에서 산출 — 이미 펀더 컬럼 유무를 보는 :72-86 패턴을 *기간*까지 확장.
- 필드↔피드 매핑은 `spec.py`의 `provides`를 권위로 사용(신규 매핑 만들지 않음).

### 0b. 챗 게이트 배선 (비대칭 해소)
- `chat/tools.py:300,356,479`의 `strategy_from_spec(ir, dataset)` → `strategy_from_spec(ir, dataset, manifest=build_dataset_manifest(dataset))`.
- 그러면 `evaluate_data_soundness` 이슈가 `service.py:172` 경로로 `warnings`에 병합되고, `serialize.py`가 보존, `classify_status`가 진단 승격 — IR 라우터와 동일 계약.

### 0c. 결과계약 노출 (null ≠ 0)
- `classify_status`(`result_status.py:59`)의 `diagnostics`에 `field_coverage` 요약 추가:
  질의가 사용한 필드별로 `{covered_symbols, total_symbols, period}` → 모델 식단(`_status_header`)·웹 `ChatResultView`가 단일 계약으로 자동 노출.
- 목적: 챗봇이 "이 필드는 N/M 종목만·기간 X~Y" 를 *알고* 답 → 결손을 0으로 오해 불가.

### 0d. 전역 커버리지 리포트 (백필 기준선)
- 유니버스 전체의 (필드 × 커버리지%·기간) 요약을 산출하는 함수/CLI — Phase 2 백필 진행률 추적·Phase 3 무결손 SLA의 측정 기반.
- 무겁지 않게: per-symbol field_coverage 집계. (영속화는 `save_manifest` 기존 함수 재사용, 필요 시.)

### 검증 게이트 (Phase 0 완료 기준)
- core 단위테스트: field_coverage 산출 정확성(합성 dataset으로 first/last/n), spec.provides 매핑 일치.
- server 테스트: 챗 경로가 게이트 issue를 결과에 싣는지(전/후), `diagnostics.field_coverage` 존재.
- 회귀 0: 기존 core/server/golden 스위트.
- 라이브(로컬 $0): describe/select 1회 → `field_coverage`가 실제 결손(예 flow=null) 표면화 확인.

---

## 7. Phase 1~4 (로드맵)

- **P1 유니버스 고정**: KR=KRX 전종목(유지). US=NASDAQ Trader 디렉터리 권위 채택→보통주+ETF 규칙→12,153 reconcile(stale 231 제거·진짜 보통주 편입).
- **P2 깊이 백필**(`*/N분` 청크+예산+마커, 비차단): KR OHLCV/수급/컨센서스 2010 소급, KR 외인보유 신규, KR 재무 deepv 완주(2015~).
- **P3 무결손 검증**: 코어 패널(KR 2015 all-fields / US 2010) gap 탐지→자동 재수집·표면화 (P0 커버리지 게이트 위에서).
- **P4 갭 필드**: US 시총 이력화(shares×price), 섹터 전종목(KSIC/SIC), US 13F 수급 대안.

## 8. 충돌/협업 노트
- `#243 financials-fill`(dart-fss 5개년) = server HOME 재무(`financials.py`·`dart_fss_fetch.py`)만 건드림 → **P2-A 엔진 재무 백필(main.py cron·fundamental_kr)과 파일 무겹침** 확인(2026-06-30). "엔진 피드 vs 서버 HOME" 두 평행 계층이라 안전.
- `_wt-data-engine`(detached, stale)이 `spec.py`·`classification.py` diff 보유 — P0에서 `spec.py` 편집 시 재확인.
- 데이터엔진=조대표 단독 담당이라 희제 충돌 없음. push·머지·프로덕션 배포는 사장님 명시 허락 후.

---

## 9. P2 실행 — feasibility verdict · 결정 · 구현 상태 (2026-06-30)

**P0(#253 `51eb5aa`)·P1(#258 `3f49e97`) 머지·배포·prod LIVE** (P1 rebuild 로그 `NASDAQ Trader 11566 + KIS 12498` 확인).

### 9.1 스코프 확장 (사장님 승인) — 인스트루먼트 타입별 완결 정의
"완결 일관"="모든 필드"가 아니라 **인스트루먼트 타입별 적용 필드를 같은 기간·무결손**으로:
- **KR 주식** {OHLCV·재무·컨센서스·flow·배당·섹터}
- **KR 선물** {OHLCV·투자자 수급(flow)·미결제약정(OI)}  ← 원래 누락분 편입
- **US 주식** {OHLCV·재무·배당·섹터·시총}  (flow·컨센서스는 미국 시장구조상 미해당)
→ KR/US는 **필드셋 자체가 다름**. 완결은 유니버스별로 정의.

### 9.2 Feasibility verdict (2026-06-30, 병렬 조사 2건·증거 기반) — **원래 "KRX MDC 스크래퍼 하나로 다 풀림" 가정이 무너짐**
- **KRX MDC 익명접근 = 사망.** `data.krx.co.kr/comm/bldAttendant/getJsonData.cmd` 익명 POST → HTTP 400(쿠키 심어도). pykrx [#244](https://github.com/sharebook-kr/pykrx/issues/244): KRX 로그인 의무 멤버십 전환. **단 prod엔 `KRX_ID/PW`가 있어 주식 flow(flow_kr/pykrx)는 인증경로로 작동 중** — 즉 익명만 막혔지 *인증 스크래퍼*는 prod에서 가능.
- **KIS = 선물 투자자수급 TR 없음**; OI는 현재 스냅샷 필드뿐(일별이력 컬럼 없음) → 선물 둘 다 불가.
- **LS OpenAPI = 선물 수급/OI 둘 다 깨끗이 가능** ✅: `t8462`(선물 투자자 수급·일별·from/to 페이징·investor 코드 sv_00…sv_18), `t8466`(선물 일봉 OHLCV+`openyak`=미결제약정·sdate/edate/cts_date 페이징). **단 LS appkey는 계정단위**(서버 전용 데이터계정 필요)·**이력깊이 미확인**(2010 못 미칠 수 있음 → prod 프로브 필요).
- **Tier1**: V-KOSPI(FDR-Yahoo 실패·KS11 sanity는 2010 정상→KRX 소스 추정), 옵션 P/C·프로그램매매·ETF flow = **KRX MDC 게이트**(인증 스크래퍼 자작 필요·pykrx는 파생 함수 없음·봇취약). 실적캘린더 KR=KIND·US=Finnhub 무료키 = **비게이트 feasible**. CBOE US P/C = 무료 연속이력 없음(equity archive 2016·index 2012·FRED 2019 종료) → **보류**.

### 9.3 결정 (사장님, 2026-06-30)
- **선물 수급/OI 소스 = LS 전용 데이터계정** (provision 필요 + prod 깊이/TR 프로브 선결). KIS·KRX 불채택.
- **Tier1 = 보류** (대부분 KRX 게이트/소스 미해결 — 코어 집중·over-engineering 회피).

### 9.4 재설계한 P2 3갈래
- **P2-A KR 코어패널 2010 깊이 백필** = 확실·신규크레덴셜 0·고가치 코어. **→ 구현·검증 완료(아래 9.5).**
- **P2-B KR 선물 수급/OI (LS)** = `t8462`+`t8466`로 `flow_kr.py` 모양 피드 신설. **블로커: ① 서버 전용 LS 데이터계정 provision(Railway env) ② prod 이력깊이·TR shape 프로브**(t8462 sv_00…18 코드 legend·미니K200 underlying id·`openyak` 깊이). 착수 전 prod 프로브 먼저(검증된 해결책만).
  - **→ 프로브 완료(2026-07-12, 모의투자 키 실측).** ✅작동: 토큰·t8467(마스터 13종)·t8462 주간(tm_rng=D — 야간 N과 값 상이·주야 구분 확인)·t8466 일봉+`openyak` 전부 정상 응답. **⚠결정적 한계 2건**: ① **t8462 이력 = 2025-06-10부터만**(2025-01·2023·2020·2010 전부 0행, 롱윈도우 최소일=2025-06-16 — 가이드 예시 날짜 20250609와 일치, TR 신설 개시일로 판단. 2010 floor 불가) ② **t8466은 만기 지난 종목 0행**(현존 상장물만 — 과거 만기물 패널 소급 불가). **판정(1차): LS는 forward 전용.** 모의투자 키로 전 TR 작동은 확인(서버 provision은 모의 키로 충분).
  - **→ 최종 판정(같은 날, KRX MDC 재프로브로 LS 기각·소스 재확정).** 사용자 결정("forward 축적은 무의미") 후 KRX 정보데이터시스템을 pykrx 인증 세션+getJsonData로 재실측: ① **선물 투자자별 수급 = MDCSTAT13101(기간합계)/13102(일별추이·inqTpCd=2)** — 핵심 파라미터는 `isuCd="KR___FUK2I"`(KRDRV→KR___ 변형·코스닥150=KR___FUKQI 동작 확인), **이력 2005-01까지 nonzero 실측**(2010 floor 충족). bld·파라미터는 사용자 로그인 브라우저의 실화면 요청 관찰로 확정(prodId 추측 스윕은 전부 0이었음 — 폼 실관찰이 정본). ② **ETF 투자자별 수급 = pykrx 내장 MDCSTAT04801/04802, 2005-01까지 실측** — 신규 스크래퍼 불요. ③ 선물 만기물 일별 패널(시세·베이시스)=MDCSTAT13401, 2010-01 만기물 80행 실측(D-2 백필에도 사용 가능). ④ OI는 기존 krx_openapi(공식 API) 2010~ 수집 중이라 애초 갭 아님. **결론: D-3 소스 = KRX MDC(선물 13101/13102·pykrx 세션 재사용, 일별 1콜 저부하 — flow_kr과 동일 로그인·차단 리스크 프로파일) + pykrx(ETF). LS·전용 데이터계정 불채택**(2026-06-30 결정을 실측으로 대체). 잔여 구현 시 확인: 13102 응답 A07/A08/A09/A12 컬럼→투자자 그룹 매핑(웹 표 헤더 대조).
  - **→ 구현 완료(2026-07-12, `feat/flow-deriv-kr` — 피드 `flow_deriv_kr.py`·매크로 심볼 6종 {코스피200선물|코스닥150선물|KRETF}{외국인|기관}순매수).** 잔여 매핑을 웹 표 대조 대신 **기간합계 라벨 합산 산술 대조(재현 가능)**로 확정: 13102 `A07=기관합계·A08=기타법인·A09=개인·A12=외국인` / 04802 `VAL21=기관합계·22=기타법인·23=개인·24=외국인(+기타외국인 정확합)·25=전체(순매수 항등 0)` — 10거래일 합산 ±2원 일치, 값은 money 파라미터 무관 **원**. 신규 함정: 13102 서버 소요 거래일당 ~0.1s 선형(600일 창=read timeout) → 백필 청크 240일·timeout 90s. 챗 표면(카탈로그 '수급' 그룹·별칭·provenance·필드가이드) 동시 배선 — 상세는 data-engine.md [2026-07-12] entry.
- **P2-C Tier1 = 보류.** (재개 시: 비게이트 승리 실적캘린더부터, KRX 파생통계는 인증 스크래퍼 결정 후.)

### 9.5 P2-A 구현 (branch `feat/data-depth-2010`·worktree `_wt-data-p2a`·미push)
| 항목 | 변경 | 파일 |
|---|---|---|
| KR OHLCV → 2010 | 신규 `backfill_korean_stocks_depth(codes, floor, budget_symbols)` — 기존 종목 `min>floor`면 `floor~(min-1)` fetch→prepend. depth-done 마커(`_kr_ohlcv_depth_done.json`)로 완료 종목 영구 skip(완주=0비용). 신규함수 필요한 이유=일일 `fetch_korean_stocks`는 *앞으로만* 증분 append라 과거 소급 불가. + 신규종목 기본 `start` 2015→2010 | `core/quant_core/data_fetcher.py` |
| 백필 cron 배선 | `_backfill_kr_ohlcv_chunk`(10분 `minute="7-59/10"` 스태거)+`_initial_kr_ohlcv_backfill`(부팅 +100s) | `server/app/main.py` |
| KR flow → 2010 | `_backfill_flow_chunk` start `20140101`→`20100101` | `server/app/main.py` |
| KR 컨센서스 → 2010 | `_CONSENSUS_BACKFILL_START` `20150101`→`20100101` (한경 2006까지 제공·프로브) | `server/app/main.py` |
| KR 재무 → 2015 | `range(yr-10,yr+1)`→`range(yr-11,yr+1)` (OpenDART 바닥 2015) | `server/app/main.py` |

**검증:** core 581 passed(+신규 `test_kr_ohlcv_depth_backfill.py` 4: deepen/young/idempotent/budget)·golden green·server 448 passed(펀더 contract 테스트 2015 floor로 갱신)·ruff main.py 0(기존 10 vestigial 정리)·data_fetcher 신규에러 0. **데이터 parquet 미삭제**(명단·깊이만 추가). 배포 후 며칠 점진 백필→`coverage_report()`로 진행률 추적.

### 9.6 다음 (P2-B 착수 순서)
1. **LS 서버 데이터계정 provision**(사장님) → Railway env(예: `QP_LS_DATA_APPKEY`/`SECRET`). 모의/데이터전용 계정이 t8462/t8466 서빙하는지 확인.
2. **prod 프로브**: t8466 `openyak` 이력깊이, t8462 sv 코드 legend·미니K200 id, 정규 K200(`K2I`) 동작. → 깊이 확정.
3. 확정 후 `feeds/flow_futures_kr.py`(가칭) 신설 — `flow_kr.py` 모양(per-product parquet·raw investor 컬럼+OI·merge 백필+증분·creds 가드 no-op)·OAuth2 토큰·~1req/s throttle.

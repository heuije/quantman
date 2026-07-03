# 데이터 수집 설계안 — 2010 Core Floor + PIT 단일저장

> 상태: 설계(구현 전). 이 세션에서 코드·프로덕션 로그·외부 소스로 검증한 사실 기반.
> 담당: 데이터 엔진(조대표). 챗 표면화 배선은 챗 세션(§7 핸드오프).

---

## 0. 목표

1. **깊이 일관성** — 백테스트가 여러 데이터 종류를 결합할 때 사용가능창은 *가장 얕은 데이터*가
   결정한다(교집합). 그래서 Core 데이터는 **전 자산군 2010 floor**로 통일한다.
2. **최대수집** — Enrichment(수급·컨센서스·13F·공매도·COT 등)는 무료 소스가 허용하는 최대 깊이까지.
3. **PIT 무결성** — 후공개 데이터는 발표시점(`as_of`)부터만 노출(look-ahead 0). 백테스트·챗봇이
   같은 진실원천을 공유하되 서빙 뷰만 다르다(§4).

---

## 1. 수집 철학 (전 항목 공통)

- **데이터포인트당 소스 1개** (백업 없음 — 복잡도 회피).
- **진실원천 = 원시 as-reported 저장**, 서빙 = 파생 뷰.
- 후공개 = `as_of`(발표/거래일) 인덱스 + `reindex-ffill` 병합 → 그 시점 이후에만 노출.
- **얕음을 위장하지 않는다** — 소스가 2010을 못 주면 실제 floor를 `field_coverage`로 정직히 노출.

---

## 2. 2계층 깊이 정책

| 계층 | 대상 | 깊이 정책 |
|---|---|---|
| **Core** | 가격 OHLCV · 펀더멘털 · 시총·거래대금 · 매크로 · 선물 | **통일 2010 floor** (KR·US) |
| **Enrichment** | 수급 · 컨센서스 · 13F · 공매도 거래량 · COT | **소스별 최대 깊이**(2010 목표, 불가 시 자연 floor 정직 노출) |

- "일관된 깊이"의 올바른 형태 = 단일 global floor 강제(truncate)가 아니라
  **Core 2010 통일 + Enrichment 투명 노출 + 엔진이 사용 필드 교집합으로 사용가능창 동적 산출**
  (`assess_data_quality` Phase 0.5에 이미 존재).

---

## 3. 자산군별 수집 매트릭스

범례: ✅ 완료/충족 · 🔄 진행형(백필) · 🟡 갭(작업 필요) · 🔵 신규 · ⛔ 제외

### 3.1 국내주식 (KR)

| 데이터 | 계층 | 소스·방식 | 목표 깊이 | 상태 |
|---|---|---|---|---|
| OHLCV | Core | FDR(KRX) 일별 | 2010 | 🔄 인프라 존재(`backfill_korean_stocks_depth` floor 2010·`kr_ohlcv_depth` cron */10) → **완료 검증**(P0) |
| 펀더멘털 13종 | Core | OpenDART 분기·PIT(`as_of`=접수일) | 2010 | 🟡 현재 2016~ → OpenDART 소급 가능범위 조사(될 만큼·나머지 정직 노출) |
| 시총·거래대금·상장주식수 | Core | KRX Open API `sto/stk_bydd_trd`·`ksq_bydd_trd` (basDd 날짜별 전종목) | 2010 | 🔵 신규(⚠`sto` 서비스 별도 신청) |
| 수급(기관·외국인 순매수) | Enrich | pykrx(KRX 로그인) | 2010 | 🟡 현재 백필 start 2014 → **2010으로 변경**(pykrx ~2010 제공) |
| 컨센서스(목표가·투자의견) | Enrich | 한경 무로그인 이벤트·PIT | 2015(소스 floor) | ✅ 2015~ 수집 완료 |
| forward 이익추정 | Enrich(스냅샷) | FnGuide highlight | 현재 스냅샷 only | ✅ describe 전용(§4 스냅샷-only) |
| 실적 캘린더(과거) | Enrich | fundamental `as_of` + OpenDART 공시검색(공정공시) | 소급 | 🔵 대부분 보유·표면화(§6.5) |

### 3.2 국내선물 (KR)

| 데이터 | 계층 | 소스·방식 | 목표 깊이 | 상태 |
|---|---|---|---|---|
| 코스피200 실선물 OHLCV | Core | KRX Open API 만기물 패널→연속물(롤=백테 파라미터) | 2010 | 🔄 백필형 |
| 미니 코스피200선물 | Core | 정규 가격 공유(승수만 상이) | 2010 | ✅ |
| 선물 참조 매크로(선물OI·V-KOSPI·풋콜) | Core/매크로 | KRX Open API | 2010 | 🔄 백필형 |

### 3.3 해외주식 (US)

| 데이터 | 계층 | 소스·방식 | 목표 깊이 | 상태 |
|---|---|---|---|---|
| OHLCV | Core | yfinance(배당조정) | 2010 | ✅ 2010-01-04 floor 확인 |
| 펀더멘털 13종 | Core | SEC Company Facts·PIT(`as_of`=filed) | 2010 | ✅ ~2009~2010(개별 예외 GOOGL 2015) |
| **13F 기관보유** | Enrich | SEC Form 13F Data Sets(분기)·CUSIP별 집계 | **2010** | 🔵 신규(구조화 2013Q2~ + **원시파싱 2010~2013Q1**) |
| **공매도 거래량**(옵션) | Enrich | FINRA Reg SHO daily | **2010** | 🔵 신규(2009~ 소스, 2018 이전 구포맷 파싱) |
| 실적 캘린더(과거) | Enrich | SEC filed date + yfinance earnings_dates | 소급 | 🔵 대부분 보유·표면화 |
| ~~공매도 잔고(short interest)~~ | — | — | — | ⛔ **제외**(FINRA 아카이브 2014·상장주 2021-06 → 2010 불가) |

### 3.4 해외선물 (US)

| 데이터 | 계층 | 소스·방식 | 목표 깊이 | 상태 |
|---|---|---|---|---|
| OHLCV | Core | yfinance 연속 프록시(ES/CL/GC/NQ/SI…) | 2010 | ✅ |
| 선물 참조 매크로(VIX군·달러·금리) | Core/매크로 | yfinance/FRED | 백테기간 | ✅ |
| **COT 포지셔닝 + 선물 OI** | Enrich | CFTC bulk CSV + Socrata API(주간)·우리 선물→CFTC market code | **2010**(소스 1986~) | 🔵 신규 |

---

## 4. PIT 단일저장 → 2 서빙뷰 아키텍처 (핵심)

**백테스트용과 챗봇용으로 수집·보관을 나누지 않는다.** 하나의 PIT-correct 진실원천에서 두 뷰를 파생한다.

### 수집·보관 (단일)
- **`as_of` 스탬프 · append-only · 수정본 보존.**
  13F 정정(13F-HR/A)·재무 restatement·COT 개정은 **덮어쓰지 말고 새 `as_of` 행으로** 추가 →
  그날 알려진 값 = 미래참조 0.

### 서빙 (2 뷰, 파생)
- **PIT 백테스트 뷰** = `as_of ≤ T`의 최신값 → 기존 `add_flow`/`add_consensus`의 reindex-ffill이 소비.
- **챗봇/현시점 뷰** = `as_of ≤ 오늘`의 최신값(= tail) + 신선도. 풍부한 현재 상세는 raw 아카이브에서 렌더.

### 이미 있는 패턴 (일반화)
`consensus_kr`: `{code}_raw.parquet`(원시 전건 = 진실) + `{code}.parquet`(PIT 변경점 패널 = 백테).
US enrichment(13F·COT 등)도 **동일 구조**로: raw as-reported 아카이브 → PIT 패널 파생.

### 유일 예외 — 스냅샷-only 소스
과거 vintage가 없는 소스(FnGuide forward 이익추정·미래 실적 예정일)는 PIT 이력을 소급 재구성 불가.
→ `fetched_at`으로 **스냅샷을 전진 누적**(오늘부터 vintage 축적). 챗봇엔 지금 사용, **PIT 백테스트는
미래분만 유효**(과거 불가). `estimate_kr`이 이미 이 방식(describe 전용) — 원칙으로 명문화.

| | 수집 | 보관 | 서빙 |
|---|---|---|---|
| 백테스트 | 동일(PIT: as_of·append-only·수정본보존) | 동일(raw 진실원천 1개) | PIT `as_of≤T` 뷰 |
| 챗봇 | 〃 | 〃 | 최신 tail 뷰 |
| 스냅샷-only 소스 | fetched_at 전진 누적 | 〃 | 챗봇 지금 / 백테 미래분만 |

→ **PIT-correct로 한 번 모으면 챗봇 뷰는 그 부분집합.** 챗봇의 "과거엔 어땠나"도 PIT 뷰가 정확(정정 전 값).

---

## 5. 작업 항목 (우선순위)

**P0 — 검증 (거의 무비용)**
- KR OHLCV 깊이백필이 prod dataset에서 실제 2010 도달하는지 확인(매니페스트 `first_date` ≤ 2010,
  `done_total` vs 전종목). 번들 2014는 로컬앱용 캡/진행중 스냅샷 가능성 — **서빙/백테스트 경로가
  2010 보존**하는지가 관건.

**P1 — Core 2010 정렬 + clean win**
- KR 수급 백필 start `20140101`→`20100101`(pykrx 2010 깊이 1종목 실측 후).
- **Q4 시총·거래대금 신규**: KRX `sto/*` floor 2010, 기존 `_krx_backfill` cursor 재사용.

**P2 — US Enrichment 2010 (신규 워크스트림, 우선순위 순)**
1. **COT** — 가장 깨끗(무료·1986~·OI 포함·PIT 명확)·우리 선물 직결. CFTC bulk/Socrata.
2. **13F** — CUSIP↔ticker 매핑 선행(friction). 구조화 2013Q2 + 원시파싱 2010~2013Q1.
3. **공매도 거래량(옵션)** — 일별 무거움, 수요 확인 후.

**P2 — KR 펀더멘털 소급**
- OpenDART 2010~2015 재무 소급 가능성 조사. 가능한 만큼 채우고 불가 구간 floor 정직 노출.

**P3 — 실적 캘린더 (on-demand facet)**
- 과거(fundamental as_of + OpenDART 공시검색) 표면화 + 미래 예정일(FnGuide/KIND·yfinance, '예정·변경가능' 명시).

**상시 — 거버넌스**
- 드리프트 가드 **필드형 확장**(새 필드 미배선 시 CI 실패).
- `field_coverage`가 Core floor(2010) 달성/미달을 매니페스트에 노출.

---

## 6. 소스별 상세 스펙

### 6.1 KR 시총·거래대금 (Q4)
- 소스: KRX Open API `sto/stk_bydd_trd`(KOSPI)·`ksq_bydd_trd`(KOSDAQ). `basDd` 하루 1콜 = 그날 전종목.
- 필드: `MKTCAP`·`ACC_TRDVAL`·`LIST_SHRS`(+OHLCV 교차검증용).
- 깊이: 2010~. 호출 ~3,800/서비스 ≪ 10k/day/key(병목 아님). ⚠`sto` 서비스 KRX 포털 별도 신청.
- PIT: 각 basDd = 그날 실제값(PIT-safe).

### 6.2 US 13F 기관보유
- 소스: SEC Form 13F Data Sets(분기 ZIP/TSV) + 원시 13F 파일(EDGAR, 2001~).
- 산출: 전 신고자 INFOTABLE을 **CUSIP별 합산** → 종목별 기관보유(가치·주식수)·**분기 순증감**·보유기관수.
- 깊이: 구조화 2013Q2~ + **원시파싱 2010~2013Q1**로 2010 floor.
- 주기·PIT: 분기·45일 lag. `as_of` = 실제 filing accepted date(EDGAR 타임스탬프).
- ⚠ **CUSIP↔ticker 매핑 필요**(13F=CUSIP, 우리=ticker) — 구현 최대 friction.

### 6.3 US COT 포지셔닝(+선물 OI)
- 소스: CFTC Historical Compressed(연도 ZIP) + Socrata API(publicreporting.cftc.gov).
- 산출: 선물시장별 trader 카테고리 포지션(상업/비상업·Disaggregated·TFF) + **미결제약정(OI)**.
- 깊이: Legacy 1986~ / Disagg·TFF 2006~ (2010 floor 여유 충족).
- 주기·PIT: 주간(화 마감 → 금 15:30 ET 공개, 3일 lag). `as_of` = 금요일 공개일.
- 매핑: 우리 선물(ES/CL/GC/NQ/SI…) → CFTC market code.

### 6.4 US 공매도 거래량 (옵션)
- 소스: FINRA Reg SHO daily short sale volume files(regsho.finra.org).
- 필드: 일별 공매도 거래량/총거래량(off-exchange 한정 — 총량 아님, 명시).
- 깊이: 2009~(통합 NMS 포맷 2018-08~; 2009~2018 구포맷 TRF별 파싱).
- 주기·PIT: 일별·당일 게시(지연 낮음). `as_of` = 거래일.

### 6.5 실적 캘린더 (on-demand)
- 과거(확정·PIT): fundamental `as_of`(제출일) 보유 + OpenDART 공시검색(주요사항/공정공시=잠정실적일)·US SEC filed/yfinance earnings_dates.
- 미래(예정·변경가능): KR=KIND IR일정·FnGuide 실적 캘린더(gicode) / US=yfinance `.calendar`·Yahoo. 종목당 1콜(news_kr 패턴).
- PIT: 과거=확정(백테 이벤트 가능) / 미래=현시점 예정(describe 전용, 스냅샷-only).

---

## 7. item C — 데이터→챗봇 동기화

새 데이터가 챗봇 멘탈모델(무엇·기간·유형)에 자동 반영되도록 3-tier로 분류·처리한다.

| Tier | 이 설계의 예 | 챗봇 인지 | 조치 |
|---|---|---|---|
| **심볼/기간** | KR OHLCV·수급·펀더 깊이 2010 확장 | ✅ 자동 | 드리프트 가드 강제(이미) |
| **필드** | Q4 시총·거래대금 | ⚠️ 반자동 | 매니페스트 `track_fields`+`_FIELD_GROUPS` 배선·**드리프트 가드 필드형 확장** |
| **모달리티** | 13F·COT·공매도량·실적캘린더 | ❌ 수동 | **챗 세션 4계층 배선**(엔진 emit·컴파일러 idiom·프롬프트 메뉴·렌더러) |

- 모달리티 신규 수급 완료 시 **데이터 세션 → 챗 세션 명시 핸드오프**(브리핑 + 통지).
- Enrichment는 2010 Core가 아닌 각자 floor를 `field_coverage`로 정직 노출.

---

## 8. 검증 정책 (검증된 해결책만)

- "완료" = 매니페스트 실측 `first_date`로 확인(하드코딩 아님).
- `assess_data_quality`(Phase 0.5)가 Core 필드 결합 시 **2010 교집합**을 리포트 → 얕은 종목/필드 경고.
- 신규 피드는 순수함수 추출기 단위테스트(krx_openapi 패턴) + 라이브 1일 실측(필드·깊이 확정).
- PIT: 미래참조 0 회귀 테스트(as_of 병합이 T 이후 값 미노출).

---

## 9. 리스크·한계 (정직)

- **KR 펀더멘털 2010 불가 가능**(OpenDART 구조화 재무 ~2015) → 펀더멘털만 얕은 floor 정직 노출.
- **13F CUSIP↔ticker 매핑** = 구현 최대 friction(무료 매핑 소스 확보 필요).
- **공매도 잔고 제외** = FINRA 2014/상장주 2021-06 → 2010 불가로 이번 스코프 제외(추후 별건).
- **US 일별 선물 OI 장기** = 유료(CME DataMine) → 무료는 주간 COT OI로 대체.
- **번들(로컬앱)은 깊이 캡 무방**(자동매매는 최근만 필요) — 2010 보장은 서빙/백테스트 경로만.

---

## 10. 실행 순서 (요약)

```
P0  KR OHLCV 2010 도달 검증(무비용)
P1  KR 수급 floor 2014→2010 · Q4 시총·거래대금 신규(2010)
P2  US Enrichment 2010: COT → 13F(매핑 선행) → 공매도량(옵션) · KR 펀더 소급 조사
P3  실적 캘린더 on-demand facet
상시 드리프트 가드 필드형 확장 · field_coverage floor 노출
```

전 항목 공통: **PIT 단일저장 → 2 서빙뷰**(§4) · **모달리티는 챗 세션 핸드오프**(§7).

---

## 부록 A. 정밀 진단 → 대원칙 → 구조 리팩토링 (2026-07-02 구현)

### A.1 진단 (3축 감사 — data_fetcher 모놀리스 · feeds 모듈러 · main.py 오케스트레이션)

| # | 결함(검증됨) | 증거 |
|---|---|---|
| D1 | **깊이 비일관**: US 신규 floor 2015 하드코딩 + US 깊이백필 부재 → **US 2010 도달 3%·2015 캡 36%**(N=300 실측). floor 리터럴 7곳 산재 | data_fetcher:707 |
| D2 | **백필 패러다임 4종 혼용**(날짜커서·종목budget·depth마커·deepv) + 커서/마커 경로·포맷 제각각 | main.py 복붙 5곳 |
| D3 | **세대 신호 비일관**: fetch_yfinance/fdr/bitcoin/fred/fng가 mark_data_dirty 미호출(단독 호출 시 stale 서빙) | 감사 Top7-1 |
| D4 | **메타 드리프트**: spec `current_status` 실측 불일치(flow/consensus absent인데 prod 수집중)·드리프트 가드 심볼형만 | spec.py |
| D5 | 당일봉 판단 4종(타임존별)·overseas 공휴일 미보정 — *비효율(오염 아님)·스코프 제외* | 감사 §2-B |
| D6 | manifest가 "시도-무데이터 vs 미시도" 미구분 — *소비처 미확정·스코프 제외* | 감사 §3 |

보존 강점(사실상의 계약): atomic write+safe read·순수함수 추출기·as_of PIT·raw+panel 분리·빈결과≠실패 구분.

### A.2 대원칙 7

1. **SSOT** — 소스 1/포인트·원시 as-reported가 진실·파생 멱등. 정책 상수(floor)도 코드 SSOT.
2. **PIT 무결성** — as_of·append-only·수정본 보존. 스냅샷-only는 vintage 전진 축적.
3. **일관 깊이** — Core는 `CORE_FLOOR`(data/policy.py) 단일 상수. Enrichment는 자연 floor 정직 노출.
4. **피드 계약** — is_active 게이팅·빈결과≠실패·atomic write·순수 추출기·as_of.
5. **단일 백필 문법** — 날짜축 백필은 `DateCursorBackfill`(data/backfill.py) 하나.
6. **실측 주장** — 커버리지·상태는 매니페스트 실측만. 메타는 CI 가드로 실측과 결속.
7. **세대 일관성** — 데이터 변경 = 세대 신호 → 캐시·manifest·bundle 전파.

### A.3 구현 (W1~W6)

| W | 내용 | 파일 |
|---|---|---|
| W1 | `CORE_FLOOR`/`CORE_FLOOR_COMPACT` SSOT + 리터럴 7곳 치환(US 2015→2010 포함) | core/quant_core/data/policy.py·data_fetcher.py·server/app/main.py |
| W2 | **US 깊이백필 신설** `backfill_overseas_depth`(min_date 그룹배치·depth마커·KR 규약 미러) + cron `us_ohlcv_depth`(`:03` 스태거)+부팅 스레드(130s) | data_fetcher.py·main.py |
| W3 | `DateCursorBackfill`로 컨센서스+KRX×4 커서 백필 공통화(기존 커서파일 100% 호환·손상커서=fresh 리셋으로 wedging 근절) | data/backfill.py·main.py |
| W4 | spec: flow/consensus `absent→present`(prod 로그 검증)·`floor` 필드 신설·Core 4유형+flow/consensus에 floor 선언. 인벤토리 `target_floor` 노출("백필 진행중" — 챗이 깊이 과신 방지) | data/spec.py·server/app/data_manifest.py |
| W5 | 드리프트 가드 확장: **필드형**(P3/P7 spec ⊆ `_FIELD_GROUPS`=TRACK_FIELDS 단일출처)·Core floor 정책·fetch 기본값 시그니처 | test_data_coverage_surface.py·test_coverage_inventory.py |
| W6 | 세대신호 부류수정 — 개별 fetch 5곳 `mark_data_dirty()` | data_fetcher.py |

신규 테스트: `test_backfill_cursor.py`(커서 규약 5)·`test_us_ohlcv_depth_backfill.py`(US 미러 6)·가드 6종.

설계 결정 기록: US 깊이백필의 빈 응답 신뢰 규약 = KR(FDR)과 동일(**예외=실패·재시도 / 무예외 빈결과=young**). 빈 응답을 불신하면 young 그룹이 영원히 재시도돼 백필이 수렴하지 않음 — false-young(글리치 오판)의 결과는 기존 깊이 유지(오염 아님)라 수렴을 택함.

---

## 부록 B. Enrichment 신규 피드 착지 (2026-07-03 구현)

### B.1 착지 3종 + 이월 1종

| 피드 | 소스(검증) | 깊이 | tier | 상태 |
|---|---|---|---|---|
| **COT 포지셔닝+주간 OI** | CFTC Socrata Legacy Futures-Only(계약코드 8종 라이브 검증) | **1986~** | 심볼(매크로 16종) — 챗 자동 배선 | ✅ 수집+실데이터 e2e(금 1,922행) |
| **KR 시총·거래대금·상장주식수** | KRX `sto/stk·ksq_bydd_trd` | 2010~ | 필드 | ⚠ 코드 완비·**포털 `sto` 신청 대기**(fail-safe: 에러/휴장 구분 전용 fetcher — 커서 침묵 전진 차단) |
| **US 공매도 거래량** | FINRA Reg SHO consolidated(실파일 검증) | **2018-08~**(구포맷 병합 시 소급) | 필드 | ✅ 수집+실데이터 e2e(3일·12,552종목) |
| 13F 기관보유 | SEC 구조화 ZIP(53개·최근 ~86-100MB/분기 실측) | 2013Q2~ | 필드 | 📋 설계 확정(B.2)·구현 이월 |

**필드형 2종(sto·shortvol)의 엔진 소비 배선은 후속 PR** — indicators attach·compute 배관·컴파일러 노출을 라이브 데이터 검증+golden 영향 확인과 함께. 원칙: *엔진이 계산 못 하는 컬럼을 컴파일러에 노출하지 않는다.* 수집은 먼저 가동해 이력을 축적.

### B.2 13F 확정 설계 (구현 이월 — 별도 세션급)

- **소스**: `sec.gov/files/structureddata/data/form-13f-data-sets/` 분기 ZIP 53개(2013Q2~). 구명명 `YYYYqQ_form13f.zip`·신명명 `01mar2026-31may2026_form13f.zip`(인덱스 페이지 스크랩으로 목록 취득). 최근 분기 ~86-100MB(압축).
- **PIT**: INFOTABLE엔 제출일 없음 → **SUBMISSION.tsv join으로 filing accepted date** = as_of. 45일 lag 실측 반영.
- **CUSIP→ticker 매핑**: SEC FTD(fails-to-deliver) 파일(반월 ~1.2MB·CUSIP+SYMBOL 공식 병기) 누적으로 매핑 테이블 구축 — openfigi 등 외부 키 불요.
- **집계**: 분기 ZIP 스트리밍 파싱 → CUSIP별 (총보유가치·주식수·기관수) 합산 → 매핑 → 종목별 분기 시계열(+전분기 차분=순증감).
- **볼륨·주의**: ZIP 1개씩 내려 처리 후 삭제(Railway 디스크)·분기축 커서 청크. 롱온리 편향·수정신고(13F-HR/A)·2013Q2 이전은 원시 파싱(후순위) 문서화.

### B.3 신규 cron 슬롯 맵

| 슬롯 | 잡 |
|---|---|
| 토 09:00 | `cot_weekly`(주 8콜·전체이력 멱등) |
| :01,:31 | `marketcap_chunk`(60일 창·~86콜/청크 — KRX 10k/일 quota 관리) |
| :16,:46 | `shortvol_chunk`(90일 창) |
| 16:40 / 09:40 | `marketcap_daily` / `shortvol_daily` |

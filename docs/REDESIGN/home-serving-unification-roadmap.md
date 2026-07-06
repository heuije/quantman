# HOME 서빙 일원화 로드맵 (개별종목분석 성능·데이터 SSOT)

> 작성: 조대표 세션 2026-07-06. 상태: **제안(승인·희제 협의 대기)**. 모듈 경계: HOME 엔드포인트/웹=**희제**, 데이터엔진 피드=**조대표**.
> 근거 문서: 이 세션의 진단·타당성 실측 + 기존 [data-collection-plan.md](data-collection-plan.md)·[data-engine-unification-redesign.md](data-engine-unification-redesign.md).

---

## 1. 배경 — 문제와 핵심 발견

**증상**: HOME(개별종목분석) 탭 로딩이 평상시 대형주 ~4초·비주력 ~10초, 재배포 직후 30~120초. (프로덕션 실측)

**근본 진단** (2회 반전 검증):
1. **온디맨드 라이브 크롤** — 종목 클릭 시 서버가 외부 6소스(네이버·DART·FnGuide·KRX)를 *그 순간* 병렬 크롤. 벽시계 = 가장 느린 소스(**reports 6.4초**).
2. **에페메랄 캐시** — 캐시가 인메모리(lru)·앱폴더 디스크라 **재배포마다 증발** → 배포 직후 첫 방문자가 전부 재크롤(warmup 30~120초).

**핵심 발견 (반전)**: HOME이 라이브로 긁는 데이터의 **대부분은 데이터엔진이 이미 전종목 수집해 볼륨(`/srv/data`)에 저장**하고 있다. HOME이 그걸 안 읽고 재크롤할 뿐. **성능 병목 reports의 데이터조차 이미 수집돼 있다**(consensus_kr 원시 아카이브). → **진짜 과제 = 신규 수집이 아니라 "서빙 일원화".**

---

## 2. 목표 · 비목표

**목표**
- HOME(및 웹 전반)이 **데이터엔진 볼륨 SSOT에서 서빙** → 로딩↓·중복 수집 제거·데이터 정합성·SaaS 동시부하 확장성.
- 실시간이 본질인 데이터(장중 현재가)만 온디맨드 유지.
- 재배포 warmup 근본 제거(캐시 볼륨 영속).

**비목표 (스코프 밖)**
- 장중 실시간 현재가 센터화(별도 KIS 시세 경로 유지).
- 데이터엔진 피드 **대량 신설**(대부분 이미 존재 — 서빙 배선이 핵심).
- 실수요 없는 고비용 전종목 수집(재무 상세 등 — 게이트 뒤).

---

## 3. 설계 원칙

1. **저렴형 vs 고비용형 구분** — 전종목 1콜 API(flow·컨센서스·시총·공시?)는 *커버리지 한계비용 0* → 전종목 정당. 종목당 크롤(재무 DART)은 *커버리지가 곧 예산·OOM* → **실수요 게이트**로만 정당화.
2. **성능 ≠ hygiene** — 센터화 대부분은 SSOT·중복제거지 **로딩 개선이 아니다**(벌크 쉬운 것들은 이미 빠름). 로딩 실이득은 reports 재배선 1건에 집중. 각 작업에 목적 라벨 명시.
3. **온디맨드 폴백 유지** — 현재가·신규상장·롱테일·우선주(FDR 소스부재 739코드)는 스케줄로 못 덮음 → 폴백 제거 불가.
4. **캐시 볼륨 영속** — HTTP 서빙 캐시는 `serving_cache/`(볼륨) — 재배포 생존.
5. **검증된 해결책만·over-engineering 금지** — 각 Phase 착수 전 실수요·트레이드오프 실측.
6. **데이터엔진 우선 서빙 (웹 전역 원칙).** 웹 서빙은 데이터엔진 볼륨 SSOT를 primary로 읽고, 종목당
   라이브 크롤은 실시간(현재가)·폴백(롱테일·신규상장) 전용. 이 원칙은 `CLAUDE.md` §2에 명문화(희제 협의 완료)
   — HOME뿐 아니라 앞으로 웹 전 화면이 따른다. 서빙 재배선이 끝난 라이브 경로는 §5.5 잔재 정리로 제거한다.

---

## 4. 현황 매트릭스 (수집 / 서빙)

| 데이터 | 데이터엔진 수집 | HOME 서빙 현재 | 목표 서빙 | 성격 |
|---|---|---|---|---|
| OHLCV 주가 | ✅ 전종목(3566·오늘) | ✅ **볼륨 우선+FDR 폴백** | 볼륨 parquet(EOD 일봉·오버레이 불요) | **perf ✅ (summary 4s→수십ms·아래 6e)** |
| flow 수급 | ✅ 전종목(3636) | 라이브 네이버 | flow_kr parquet | hygiene |
| 컨센서스 | ✅ 전종목(4657) | 라이브 네이버 | consensus_kr 패널 | hygiene |
| **리포트목록** | ✅ **reports_kr 피드 신설(네이버 전종목)** | **라이브 네이버(6.4초·병목)** | **reports_kr 파켓** | **perf ⭐ PR#320** |
| 기업개요 | ✅ 전종목(company_profiles) | 저장본(이미 서버) | 유지(feeds/ 이관 비권장) | done |
| 추정실적 | ⚠️ 피드 URL 죽음(0건) | 라이브 wcomp | URL 복구 후 저장본 | 결함복구 |
| 재무제표 상세 | ⚠️ ~402만 | 라이브+XBRL | (게이트) 경량 JSON 전종목 | 조건부 |
| 공시 | ❓ 미확인 | 라이브 DART | (확인) DART list 전종목? | 확인 |
| 공매도잔고 | ❓ 미확인 | 라이브 KRX | (확인) KRX 전종목? | 확인 |
| 현재가(장중) | — | 온디맨드 | **온디맨드 유지** | 센터화 제외 |

---

## 5. 로드맵 (Phase별)

### Phase 0 — 캐시 볼륨 영속화 ✅ **draft PR #319 오픈**
- **내용**: financials·earnings·company_profiles 디스크 캐시를 에페메랄 앱폴더 → 영속 볼륨 `serving_cache/`로 이동(`serving_cache.py` 헬퍼). **캐시 로직 무변경, 위치만.**
- **효과**: 재배포 warmup 근본 제거. **재무제표 탭 30~120초 → 종목당 1회 크롤 후 재배포에도 생존.**
- **모듈**: 서버(희제 인접). **검증**: 서버 499 passed·1 skipped(회귀0) + 신규 테스트 3종.
- **상태**: 브랜치 `feat/home-cache-volume-persist` → **draft [PR #319](https://github.com/MercKR/quantman/pull/319)** (원칙 명문화 §2 CLAUDE.md 포함). 머지는 승인 대기.

### Phase 1 — 리포트목록 네이버 전종목 피드 신설 ⭐ **[성능 최우선] · draft PR #320**
> ⚠ **원안(한경 consensus_kr raw 파생뷰) 실측으로 폐기.** 착수 전 커버리지 델타 실측 결과 한경은
> 리포트 목록용으론 커버리지 절반(최근 21일 대형주: 삼성전자 5 vs 네이버 11·삼성/미래에셋/신한 등 대형
> 증권사 누락). 한경은 컨센 *지표*(인라인 목표가)엔 옳지만 리포트 *목록*엔 부적합 → **네이버 별도 피드 신설**.
- **내용**: `core/data/feeds/reports_kr.py` 신설 — 네이버 크로스종목 목록(전종목 1콜형·page newest-first) 크롤·종목명→코드(ticker_db 실측 100%)·nid dedup·`{code}.parquet`. `krdata.reports`를 **피드 우선 + 라이브 폴백**(원칙6)으로, `_reports`(6.4초 라이브)는 폴백 전용 강등. `main.py` cron(19:30 증분 + startup 1회 백필·볼륨 플래그).
- **효과**: **HOME 최대 병목 reports 6.4초 소거**(피드 파켓 서빙) + **커버리지 2배**(대형 증권사 포함) + 재시작 재크롤 제거.
- **트레이드오프(수용됨)**: 당일 신규 리포트 최대 반나절~하루 lag(19:30 cron·사용자 승인). 목표가는 목록 소스에 없어 생략(컨센 목표가는 한경 담당·web `target=null→"—"` 비파괴).
- **모듈**: 조대표(피드+서버 서빙). **`market.py`·web 무변경**(krdata.reports 스왑 투명). 검증: core 4 + server 4 신규·서버 500 green·실데이터 E2E. 잔여: railway 최초 백필·배포 후 브라우저 커버리지 검증.

### Phase 2 — flow(수급)·컨센서스 카드 HOME 서빙 일원화 **[hygiene/SSOT · draft PR]**
- **내용**: `krdata.investor`(네이버 frgn)→`flow_kr` 볼륨(거래대금·원), `krdata.consensus`(네이버 wisereport)→`reports_kr` raw 증권사별 standing. 둘 다 데이터엔진 우선(원칙6).
- **효과**: 이중 크롤 제거·SSOT·유저간 캐시부재 degrade 해소. **로딩 개선 아님**(이미 0.5~2초).
- **결정(사용자 승인)**: flow **단위=거래대금(원)** — 웹 수급 차트 "단위 주"→"단위 억원" 전환(주식수 근사변환은 PIT 오염·4원칙 위반). ⚠ 그 결과 **수급 라이브 폴백 없음**(네이버=주식수라 단위 불일치) — flow_kr 미커버(신규상장 ≤1일)는 빈 수급. consensus는 네이버 동일 단위라 라이브 폴백 유지.
- **모듈**: 조대표(krdata 서빙) + 희제 웹(StockDashboard 수급 차트 단위 label/format). market.py 무변경(krdata 스왑 투명). 검증: server 4 신규·507 green·web tsc 0.
- **잔여**: 배포 후 브라우저 검증(수급 억원 표시·컨센 standing). flow_kr는 KRX_ID/PW 필요(prod 가동중 실증).

### Phase 3 — estimate_kr URL 복구 + krdata SSOT 단일화 ✅ **draft PR (구현·검증 완료)**
- **내용**: `estimate_kr.py` 죽은 소스(`comp.fnguide SVD_Main` 302→wcomp 루트 리다이렉트·`highlight_D_Y` 소멸) →
  신 API `wcomp.fnguide.com/CompanyInfo/getSnpFinancial`(JSON·`cmp_cd`/`consol_typ=C→I 폴백`/`freq_typ=Y`)로 전환.
  HTML 스크래핑→JSON 파싱 교체하되 다운스트림 뷰 계약(`{years,is_estimate,metrics}`→frame→forward/annual)은 보존.
- **⚠ 실측 재진단(로드맵 원안 정정)**: 로드맵은 "HOME이 신 API를 이미 쓴다"고 봤으나 그건 stale 체크아웃 착시 — origin/main의
  `krdata._earnings`는 **이미 wcomp JSON으로 이전**돼 있었다(희제, 2026-06말). 진짜 결함은 ①`estimate_kr`(챗 describe/compare
  소스)가 여전히 죽은 URL(→0건) ②`krdata._earnings`↔`estimate_kr`가 **같은 wcomp JSON을 이중 파싱**(§5.5 잔재). 하나의 죽은-URL
  부류가 둘을 갈라놓음 → **구조적 근본수정 = estimate_kr 복구 + krdata를 그 피드로 SSOT 단일화**(estimate_kr가 krdata의 검증
  로직=C→I·발표기준 max-non-null·rev대비 마진 계승).
- **효과**: (1) 챗 describe/compare 추정실적 실데이터 회복(현 0건). (2) 중복 wcomp 파서·별도 json 캐시 제거 → 단일 소스(챗·웹·엑셀 공유).
- **검증**: core estimate_kr 12 + krdata 어댑터 2 + serving_cache 회귀 갱신. 전체 core 695·server 505 green. **라이브 스모크 5종목**
  (현대차·카카오·셀트리온 forward 정상·삼성/SK하이닉스 메모리 대형주 FnGuide 컨센 이상은 출처표기로 대응·과거확정 정확).
  **출력동일 검증 10종목**(옛 krdata._earnings vs 새 어댑터 byte-identical → 웹 Estimates 탭 무드리프트).
- **⚠ over-engineering 가드**: **bulk cron 부착 보류** — on-demand 7일 parquet 캐시(estimate_kr.get)로 충분·대부분 미열람. 전종목 screen 실수요 시만.
- **모듈**: 조대표(estimate_kr 코어 + krdata 서빙). 웹 KrEarnings 계약(`{years(E), 한글 rows}`) **무변경**(어댑터가 동일 형태 산출)
  → 희제 웹 코드 무변경(Estimates 탭=IndustryAnalysis CompanyReport). 잔여: 배포 후 브라우저 Estimates 탭 시각검증.

### Phase 4 — 공시·공매도 전종목 API 실측 → 편입 **[확인]**
- **내용**: `railway run`으로 ① DART `list.json` 날짜별 전종목 공시 1콜·quota ② KRX/pykrx 공매도잔고 전종목 엔드포인트 실존 확인.
- **판정**: 저렴형(전종목 1콜)이면 Phase 2급 즉시 편입. 공시는 **당일 발간=신선도 본질**이라 스케줄+온디맨드 병용. 공매도는 T+2라 일 1회 충분.
- **모듈**: 조대표(신규 피드, 존재 시).

### Phase 5 — 재무제표 상세 전종목화 **[조건부 · 실수요 게이트]**
- **기본 판정 = 하지 않음.** HOME은 이미 industry ~402 + 온디맨드로 충분. "4300 전종목 상세를 실제 누가 여는가"를 실측 제시 전 착수 금지.
- **착수 시 제약**: 경량 `dart.py fnlttSinglAcntAll.json`만·**dart_fss(XBRL) 전종목 절대 배제**(2026-05 Railway OOM). 별도 OPENDART 키 or fundamental_kr 예산 분할(20k/일 경합). 예산 4.3일(별키)~8.6일+(경합). fundamental_kr 증분 마커 패턴 재사용.
- **모듈**: 조대표. (⚠ HOME Financials 탭의 *표시용* 원본 재무제표는 Phase 6b가 **on-demand→볼륨**으로 흡수 — 이 Phase의 *전종목 bulk*와는 별개 경로.)

### Phase 6 — GlobalMarket·산업·재무 서빙 일원화 (웹 전역 확장) **[감사 도출 · 조대표]**
> **2026-07-07 서빙 bypass 전수 감사**(9-에이전트 워크플로·적대적 재검증) 도출. HOME 밖에서도 데이터엔진을
> 우회해 라이브 크롤하는 경로 다수 확인 — **대부분 데이터엔진이 이미 중앙 수집 중인데 웹이 안 읽고 재크롤**
> (estimate_kr↔krdata와 동일 부류). 신규 수집이 아니라 **서빙 재배선**이 과제. 원칙6을 HOME→웹 전역으로 확장.
> 감사 원문: 세션 산출물(bypass 인벤토리). legitimate-live(현재가·공시·공매도·경제캘린더·KOMIS)는 유지.

- **6a — GlobalMarket 탭(국채금리·글로벌지수·원자재) → dataset 서빙 ✅ 구현·검증 완료.**
  - **국채(bonds)**: `feeds/bonds.py` 신설(FRED/MOF/ECB 수집 단독소유·볼륨). 서버 `bonds.py`는 볼륨 서빙(get·self-heal·
    lru 제거→warmup 근절). **+전만기 매크로화(단일 SSOT)**: US11·JP15·EU10·CN1=37 만기물을 매크로 심볼(`macro.bonds`)로
    발행→챗/백테스트 참조. 겹치던 DGS2/30/3M/5·^TNX(미국채10년)를 FRED/YF에서 제거해 국채 피드로 이관. KR은 KRX 국고채가 SSOT라 표시전용.
  - **지수·원자재**: 세계지수 8·원자재 8을 `YFINANCE_SYMBOLS`로 추가(dataset_global cron 수집)→`globalmarket.py`
    dataset-first 서빙 + 미수집 라이브 FDR 폴백(원칙6). 16심볼 `ohlcv.futures`→챗 커버리지 인벤토리.
  - **결정(사용자)**: 표시=챗 사용가능(1 자산계열 통일·2 국채 단일SSOT). 경제캘린더(TradingView)·KOMIS는 legitimate-live 유지.
  - **검증**: 출력동일 국채 5개국·globalmarket 지수9+원자재13 실측·라이브 스모크(bond tenor load_dataset_for)·드리프트 가드·core702/server511 green.
  - **모듈/PR**: 조대표(feeds+서버). 커밋 3(bonds서빙·지수/원자재·bonds매크로). 잔여: 배포 후 GlobalMarket 브라우저 검증.
- **6b — 재무제표 상세(HOME Financials) 데이터엔진 흡수 [사용자 지정 방향].** `financials.py`가 FnGuide
  SVD_Finance를 요청당 라이브 크롤(+DART 폴백)하는 것을, **희제 의도(DART 재무제표 금액·계정 양식을 최대한 원본
  그대로)를 보존**하며 데이터엔진 피드로 흡수. 데이터엔진 `fundamental_kr`은 *계산 지표*(마진·ROIC 등)만 저장하고
  *원본 재무제표 계정·금액·양식*은 미보관 → **원본 재무제표(raw statement) 수집을 데이터엔진이 추가**(on-demand→
  볼륨, estimate_kr 패턴: 열람된 종목만 lazy 수집·볼륨 캐시·SSOT). financials.py는 볼륨 서빙으로 전환하되 **웹 표시
  형식·계정 순서 무변경**(FnGuide 중복 크롤·재배포 warmup 제거).
- **6c — 산업분석(시총 treemap·EBITDA/D&A) 서빙 일원화.** `industry.py` 시총·등락(네이버 realtime+FDR)→
  `marketcap_krx`, EBITDA/D&A(FnGuide 8병렬 크롤)→`fundamental_kr` 서빙. 중복 크롤 제거.
- **6d — dead 수집 코드 정리.** `naver_fundamentals.py`(매일 2,700종목 긁어 krx_cache 메모리에만·웹 엔드포인트
  없음·재시작 증발)·`hankyung.py`(수집하나 라우터 미노출) — 소비처 없는 수집 제거(4원칙 over-engineering).
- **6e — 종목상세 OHLCV 볼륨 서빙 ✅ 구현·검증 완료 [HOME summary 로딩 최우선·사용자 재우선순위].**
  `market.py:symbol_detail`의 `_raw_ohlcv`(종목)·`_raw_bench`(벤치마크)가 요청당 라이브 `fdr.DataReader`
  (직렬 ~4s·range별 재fetch·lru만→재배포 warmup)로 HOME summary 지배 병목. 데이터엔진이 이미 KR 전종목
  OHLCV(`{code}.parquet`·FDR 백필)·지수를 볼륨에 수집 중 → **볼륨 우선+FDR 폴백**으로 교체(수십ms). 백필과
  동일 FDR 소스라 **출력동일**(symbol_detail 두 경로 series 380바 byte-대조 FULLY identical 실증). 신선도
  무회귀(FDR·볼륨 둘 다 EOD 일봉·현재가=마지막 일봉종가·오버레이 불요). **부수=버그복구**: FDR ^KS11/^KQ11
  (캐럿형)이 NaN이라 깨졌던 국내 벤치마크·베타를 볼륨(코스피/코스닥지수) 실값으로 복구. **코스닥지수(^KQ11)
  데이터엔진 매크로 정식 편입**. core702/server520 green. 잔여: 배포 후 브라우저(summary 로딩·국내 벤치마크
  오버레이). ⚠ 산업분석·재무제표(6c·6b) 탭은 사용자 지시로 미터치.
- **모듈**: 조대표(데이터엔진 피드+서버 서빙). 웹 표시 계약 보존(희제 코드 무변경 목표) — 불가피한 웹 변경은 협의.
  검증: 각 재배선 전후 **출력동일 대조**(Phase 3 방식) + 배포 후 브라우저. 순서: 6a→6e(summary perf)✅. (6c·6b 보류·사용자 재우선순위)

---

## 5.5 잔재 정리 워크스트림 (HOME 라이브 크롤 → 데이터엔진 서빙 · 희제 협의 완료)

**성격**: 별도 Phase가 아니라 **각 서빙 재배선 Phase에 부수**하는 정리 작업 + 마지막 sweep. 원칙:
서빙을 볼륨으로 옮긴 뒤 **더는 참조되지 않는 라이브 크롤 코드는 제거**하고, 정당한 폴백만 남기되
**"폴백 전용"으로 명시**한다(§3 원칙 3·6). 삭제가 추가보다 우선(4원칙 over-engineering 금지).

| 대상 잔재 | 위치 | 처리 | 트리거 Phase |
|---|---|---|---|
| `_reports`(네이버 목록8p+상세15콜) | `krdata.py:130` | reports_kr 피드로 교체 후 **폴백 전용 강등**(주석 완료·PR#320) | Phase 1 ✅ |
| `_investor`(네이버 frgn 수급) | `krdata.py` | flow_kr 서빙으로 교체 후 **제거** | Phase 2 |
| `_consensus`(네이버 wisereport) | `krdata.py` | consensus_kr 패널 서빙으로 교체 후 **제거** | Phase 2 |
| estimate 라이브 경로 중복 | `krdata._earnings`↔`estimate_kr` | estimate_kr URL 복구 후 `krdata.earnings`가 피드로 위임·중복 wcomp 파서/json 캐시 **제거**(출력동일 검증) | Phase 3 ✅ |
| `market.py` `/kr` ThreadPoolExecutor 6소스 팬아웃 | `market.py:461` | 소스별 교체에 맞춰 워커 목록 축소, 볼륨 리드로 대체 | Phase 1·2 |

**규칙**: ① 재배선과 잔재 제거는 **같은 PR**에 묶어 dead code가 유예 없이 사라지게 한다(반쪽 상태 방지).
② 폴백으로 남기는 크롤은 반드시 주석으로 *왜 남기는지*(롱테일·신규상장) 명시 — "혹시 몰라서" 잔존 금지.
③ Phase 1·2 완료 후 **최종 dead-code sweep**: HOME 라이브 크롤 함수·import·미사용 캐시 디렉터리 잔재를
grep으로 전수 확인해 마무리. ④ 모두 희제 웹 모듈 → PR 협의(협의 자체는 완료, 코드리뷰는 PR에서).

---

## 6. 트레이드오프 · 리스크 (통합)

1. **성능 payoff는 Phase 1(reports) 1건에 집중.** Phase 2~5는 SSOT·정합성·확장성이지 로딩 개선 아님 — 기대 오독 주의.
2. **Phase 1도 순수 이득 아님** — 커버리지·신선도 트레이드오프(§Phase1). 실측·하이브리드 완화 전제.
3. **온디맨드 폴백 제거 불가** — 현재가·신규상장·롱테일·우선주 739(FDR 소스부재). 스케줄+폴백 병용이 정답.
4. **고비용형 커버리지=예산** — 재무 전종목화는 OOM·DART 한도 리스크. 실수요 게이트 필수.
5. **모듈 경계 충돌** — 서빙 재배선은 전부 희제 웹(market.py·Home.tsx) 관여. 최신 main 기준 새 브랜치·협의 후.

---

## 7. 미해결 · 실측 필요 (railway run)

| 항목 | 확인 방법 | Phase |
|---|---|---|
| DART 공시목록 전종목 1콜·quota | `railway run` list.json | 4 |
| KRX 공매도 전종목 엔드포인트 | pykrx/KRX Open API 카탈로그 | 4 |
| estimate wcomp 4300 병렬 지속(봇차단) | 4300 배치 실측 | 3 |
| 재무 전종목 저장 볼륨 여유 | 백필 시 Railway 볼륨 모니터 | 5 |
| Phase 1 파생뷰 vs 네이버 커버리지 델타 | railway run 종목·증권사 표본 비교 | 1 |
| flow 단위계약 웹 수용 | 희제 협의 | 2 |

---

## 8. 우선순위 요약

| # | 작업 | 목적 | 모듈 | 상태 |
|---|---|---|---|---|
| 0 | 캐시 볼륨 영속화 + 원칙 명문화 | 성능(warmup) | 서버 | ✅ draft PR #319 |
| 1 | 리포트목록 네이버 피드 신설 | **성능(6.4s)+커버리지2배** | 조대표 | ✅ draft PR #320 |
| 2 | flow·컨센 서빙 일원화 | hygiene/SSOT | 희제+조대표 | ✅ draft PR #322 |
| 3 | estimate_kr URL 복구 + krdata SSOT 단일화 | 결함복구+SSOT | 조대표 | ✅ draft PR (검증완료) |
| 4 | 공시·공매도 실측→편입 | 확인 | 조대표 | 실측 대기 |
| 5 | 재무 상세 전종목화(bulk) | 조건부 | 조대표 | 게이트 뒤 |
| 6 | GlobalMarket·산업·재무 서빙 일원화(웹 전역) | 감사도출 SSOT | 조대표 | 6a✅(국채·지수·원자재)→6c→6b→6d |

---

## 부록 — 관련 파일·문서
- 진단·설계: `퀀트/home_perf_redesign.md`, `퀀트/data_engine_universe_feasibility.md`(워크스페이스)
- HOME 엔드포인트: `server/app/routers/market.py`(kr_extras:461·symbol_detail:229·profile:590), `server/app/krdata.py`(reports:130)
- 서빙 캐시: `server/app/serving_cache.py`(Phase 0 신설), `financials.py`·`company_profiles.py`
- 데이터엔진 피드: `core/quant_core/data/feeds/{consensus_kr,flow_kr,estimate_kr,fundamental_kr}.py`, 리더 `core/quant_core/data_fetcher.py`(load_stock_flow·load_stock_consensus)
- 정합 설계: `docs/REDESIGN/{data-collection-plan.md,data-engine-unification-redesign.md}`

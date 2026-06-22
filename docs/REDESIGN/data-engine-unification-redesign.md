# 데이터엔진 단일화 — 중복 소싱 정리 + forward 추정치 확장 (뿌리①)

> 상태: 감사·설계 승인 → spec 검토 대기 · 작성 2026-06-22 · 브랜치 `feat/data-engine-unify`(origin/main 8c90325 기반)
> 배경: 희제 웹앱 테스트 12증상의 4구조뿌리 중 **뿌리①(데이터 소스 분열)**. 뿌리②③은 [PR#198](https://github.com/MercKR/quantman/pull/198)(머지대기), ④는 후속.
> 선행 설계: `docs/REDESIGN/data_engine_design.md`(2026-06-08, 수집 일원화→3서빙뷰). 이 문서는 그 연장 — **중복 정리 + 검증된 갭(forward) 확장**에 집중.

## 0. 사용자 지시 (governing principle)

> "다중 소스에서 중복 수집하는 workflow가 있다면 정리. 중복 데이터는 가장 효과적인(많은 데이터·안정적) 소스로 단일화. 엔진이 못 긷는 데이터는 확장하되, **확장 소스가 최선인지 + 기존 소스로 정말 불가한지 검토**."

= [[feedback_no_data_backup]](데이터포인트당 소스 1개)의 전면 적용. 북극성: **마이스톡 전 데이터를 데이터엔진이 수집·배포 → 모든 소비자(챗·HOME·엑셀·백테스트)가 동일 소스를 읽는다.**

## 1. 감사 결론 — 두 개의 평행 데이터 계층 (증거 기반)

마이스톡엔 같은 데이터를 두 번 긷는 평행 계층이 있다: **엔진(core/quant_core/data/feeds/)** vs **서버 HOME(server/app/krdata.py·financials.py·naver_fundamentals.py)**.

| 데이터포인트 | 엔진(core) | HOME(server) | 판정 |
|---|---|---|---|
| 수급(기관/외인) | `flow_kr` KRX공식·pykrx·2010+·영구parquet·PIT | `krdata.investor` NAVER frgn·120일·메모리캐시 | **엔진 승**(공식·영구·이력)¹ |
| 컨센서스 목표가/의견 | `consensus_kr` 한경·PIT·분산/리비전·영구 | `krdata.consensus` wisereport·현재값·메모리 | **엔진 승**(PIT·풍부·영구) |
| 밸류 PER/PBR | OpenDART 계산 pb_ratio/trailing_pe·일별PIT | `krdata.earnings`(FnGuide 연간)·`naver_fundamentals`(A1서 PER/PBR 제거됨) | **엔진 승**(A1서 스크리너 이미 일원화·PR#103) |
| **forward 추정치** | ❌ **없음** | `krdata.earnings` FnGuide highlight 3년추정(E) | **갭→확장**(§3) |
| historical 밸류 추이 | ✅ 일별 pb_ratio/trailing_pe(OpenDART) 보유 | `krdata.earnings` 5년 PER/PBR | **소싱 갭 아님·표면화만**(§3.4) |
| 재무제표(PL/BS/CF) | `fundamental_kr` OpenDART(계산용 ~14필드) | `financials.py` FnGuide(표시용 전체 계정) | **희제 #199가 DART로 이관 중**(§5) |
| 뉴스 | `news_kr`(NAVER·키)+`news_gdelt`(글로벌) | `krdata.news` Google RSS·키리스 | Phase 3 연기(둘 다 작동·비급) |

¹ **수급 단서(검증 필수)**: `flow_kr`은 `KRX_ID`/`KRX_PW` 필요·미설정 시 **비활성**·pykrx 봇차단 취약(feed docstring 자인). 단일화 전 **prod KRX 자격+적재 커버리지 확인** — 미적재면 NAVER가 유일 작동 소스가 되어 섣부른 제거는 회귀.

**비중복(HOME 고유·제거 대상 아님)**: `naver_fundamentals`의 DPS·배당수익률·외국인소진율·52주(A1서 PER/PBR 제거 후 고유필드만) · `krdata`의 기업개요·공시·공매도·애널리포트목록. 엔진 등가물 없음 → 유지(추후 symbol뷰로 흡수 가능).

## 2. 검증 기록 (추측 아님)

- **forward 추정치 = 엔진 구조적 불가 확인**: OpenDART=과거 공시만(미래 없음), `consensus_kr` 한경=목표**주가**·투자의견만(추정 이익 아님). 두 기존 소스 어느 것도 추정 매출/영업이익/EPS를 못 만듦.
- **FnGuide = 최선 무료 forward 소스 확인**: FnGuide Consensus는 증권사 추정(매출·영업이익·순이익) 3개월 평균을 **2003년부터** 무료 제공(상세 Daily만 유료). `krdata.earnings`가 이미 **키리스 스크래핑(SVD_Main highlight_D_Y)으로 5년확정+3년추정 작동 중** = 프로덕션 입증. NAVER 증권도 같은 컨센서스 계열이라 우위 없음. → 데이터엔진 메모리의 "추정치 유료독점→보류"는 *구조화 Daily API* 한정이었고, **단일종목 on-demand 무료 경로는 가용**.
- **historical 밸류 = 엔진 이미 가능 확인**: `fundamental_kr`+`indicators`가 일별 pb_ratio/trailing_pe 시계열 생성(2015+). 1-2는 수집 갭 아닌 **표면화 갭**.

## 3. Phase 1 — forward 추정치 feed + 챗 표면화 (이번 PR·순수 추가·무회귀)

목표: 증상 **1-1(추정실적)**·**1-2(historical 밸류 추이)**를 챗(describe/inspect)에서 닫는다. **제거 0 → 회귀 0.** 전부 `core/*` + 챗 경계 → 희제 #199(server/web)와 파일 무충돌.

### 3.1 신규 feed `core/quant_core/data/feeds/estimate_kr.py`
- 소스: FnGuide `SVD_Main` `highlight_D_Y`(키리스 스크래핑) — `server/app/krdata.py`의 `_earnings` 로직을 **core로 이전·정제**(영문 키·`is_estimate` 플래그).
- 파싱: 연도별(`2024/12`·`2026/12(E)`) × {rev, op, ni, controlling_ni, ebitda, eps, per, pbr, roe} + `op_margin`·`net_margin` 파생. `(E)` 접미사 → `is_estimate=True`.
- 저장: 종목별 parquet `data/estimates/{code}.parquet`(인덱스=회계연도, 컬럼=지표+`is_estimate`, 메타 `fetched_at`). **현재 스냅샷 1개만**(FnGuide highlight는 과거 추정 아카이브 없음 → 덮어쓰기·7일 신선도). as_of=fetch일.
- 신뢰성: 키리스, 실패→빈결과(가짜0 금지). consensus_kr·flow_kr와 동일 마커/신선도 패턴.
- **명명 주의**: `estimate_kr`=forward **이익 추정**(FnGuide) vs 기존 `consensus_kr`=목표**주가** 컨센서스(한경). 별개 데이터·상보적.

### 3.2 수집 = on-demand 심볼뷰 (`estimate_kr.get`, **bulk cron 불필요**)
- `get(code)` = load-or-fetch(7일 신선도) — data_engine 설계의 `symbol` 뷰("없으면 즉시 수집→적재→서빙") 패턴. describe가 **본 종목만 lazy 수집**(2700종목 bulk cron은 대부분 미열람이라 Over-engineering·원칙2 — Phase 2 screen이 bulk 추정을 필요로 하면 그때 cron 추가).
- 결과: **`main.py` 미수정** = 희제 #199와 충돌면이 더 줄어듦. fetch 실패면 stale 저장본 graceful(외부 FnGuide 일시장애).

### 3.3 describe 표면화 — 서버 엣지 enrich (골든 불변·희제 함수 미수정)
- `server/app/routers/ir.py`에 `_attach_symbol_estimates(result, symbol)` 추가 — **`_attach_symbol_news`와 동일 패턴**(엔진 결정성 밖, 라이브/단일종목 데이터를 엣지서 보강).
- describe 결과(`query=="describe"`)에 `estimates` 블록 부착: 다음 회계연도 **추정 매출·영업이익·순이익 성장률 + forward PER(=현재가/추정EPS) + PEG**. 원천=estimate feed parquet.
- **희제 소유 `run_describe_report`(core describe-단일)는 건드리지 않음** — 엣지 enrich라 보고 함수 무변경(협업 경계 존중).

### 3.4 historical 밸류 추이 표면화 (수집 0)
- 엔진이 이미 일별 pb_ratio/trailing_pe 보유 → **inspect로 조회 가능**(`inspect symbol=… columns=[pb_ratio,trailing_pe]`). capability_spec·NL 라우팅에 "PER/PBR 추이·밸류 히스토리→inspect" 명시(이미 inspect가 지원하나 챗이 안내 못 하던 표면화 갭).
- describe `estimates` 카드에 최근 밸류 추세 요약(현 trailing_pe vs forward_pe 비교)으로 보강.

### 3.5 소비 (챗 viz — 조대표 공통 viz)
- `web` 챗 describe 렌더러(`ChatResultView`→`ReportCards`)에 **추정실적 카드**(forward 성장·forward PER·PEG) 추가. (HOME `Home.tsx`=희제 소유라 미터치 — 챗 렌더러만.)

### 3.6 검증 (4원칙 — 검증된 해결책만)
- core: estimate feed 파싱 테스트(연도/E플래그/필드)·골든 백테스트 무변경.
- server: `_attach_symbol_estimates` 단위(피드 있음/없음/부분)·describe 직렬화.
- 챗 평가 하니스(#197 머지됨): describe 시나리오에 "삼성전자 추정실적/전망" 추가 → estimates 블록·forward PER 표면화 확인. "PER 추이" → inspect 라우팅 확인.
- web `tsc -b && vite build`.

## 4. Phase 1 4계층 매트릭스

| 계층 | 변경 | 소유/충돌 |
|---|---|---|
| 엔진(데이터) | `estimate_kr` feed 신규 + 저장 | 조대표 데이터엔진·신규파일 무충돌 |
| 엔진(인사이트) | inspect 컬럼/capability에 밸류추이·estimate 노출 | 조대표 |
| 수집 | `estimate_kr.get` on-demand 심볼뷰(cron 없음) | 조대표·`main.py` 미수정 |
| serialize/계약 | `ir.py` `_attach_symbol_estimates`(엣지 enrich) | 조대표(챗 라우터)·`run_describe_report` 미수정 |
| NL | 라우팅 idiom(추정실적→describe·밸류추이→inspect) | 조대표 |
| 웹/엑셀 | 챗 describe 렌더러 추정실적 카드 | 조대표 공통 viz(HOME 미터치) |

## 5. Phase 2·3 (후속 — 본 PR 아님)

- **Phase 2** 〔내 모듈 + 희제 협의·#199 머지 후〕: **symbol 서빙뷰**(data_engine 설계 A3 완성) + HOME `/market/kr/{symbol}` 중복(krdata.investor/consensus/earnings)을 엔진 canonical로 이관 → **증상 2-5b(값 불일치) 소멸 + 중복 workflow 제거**. ⚠️ `market.py`·`Home.tsx`가 희제 #199와 직접 충돌 → **#199 머지 후 최신 main에서 새 브랜치 + 희제 협의**.
- **Phase 3** 〔대부분 희제가 수행 중〕: **재무제표 FnGuide→DART 수렴 = 희제 #199가 이미 진행**(financials.py 5개년 DART). 내 스코프에서 제외. 뉴스 수렴(krdata.news Google RSS vs 엔진 news_kr/gdelt)만 잔여 — 비급(둘 다 작동) → 추후.

## 6. 스코프 경계 / 리스크

- **본 PR(Phase 1)은 순수 추가** — 어떤 기존 소스도 제거 안 함(중복 제거는 Phase 2서 소비자 이관과 함께). 1-1·1-2를 무회귀로 먼저 닫는다.
- **희제 #199 협업**: 재무제표 DART 이관은 희제 담당(중복 안 만듦). Phase 2는 #199 머지 대기. 본 PR은 #199와 파일 무충돌이라 병렬 진행.
- forward 추정치는 **현재 스냅샷 only**(FnGuide highlight=과거 추정 아카이브 없음) → describe/screen(현시점) 사용 가능, **PIT 백테스트 부적합**(명시·lookahead 금지). 백테스트용 추정 아카이브는 향후 누적 적재 시 가능(별도).
- `krdata.earnings`는 Phase 1서 **유지**(HOME이 아직 씀) — Phase 2 HOME 이관 시 제거. 즉 일시적으로 core estimate_kr + server krdata.earnings 공존하나, **둘 다 같은 FnGuide highlight** = 값 분열 없음(가격 같은 FDR 무해 사례와 동형).

## 7. 구현 단계 (Phase 1)
- **P1a** `estimate_kr` feed(파싱·저장·테스트).
- **P1b** on-demand 심볼뷰 `get`(load-or-fetch·bulk cron 없음·main.py 미수정).
- **P1c** `ir.py` `_attach_symbol_estimates`(describe estimates 블록)+단위테스트.
- **P1d** capability/NL(추정실적→describe·밸류추이→inspect)+하니스 시나리오.
- **P1e** 웹 챗 describe 추정실적 카드.
- **P1f** 풀 회귀(골든·core·server·하니스·web build).

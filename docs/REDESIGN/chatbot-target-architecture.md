# 챗봇 목표 아키텍처 — "백테스트·스크리닝 비서" → "전문 분석·추천·내러티브 자문봇"

> 상태: **Phase 1·2 구현완료 (draft PR#188) · Phase 3~5 미착수** · 정본 브랜치 `feat/chat-capability-redesign`
> 목적: 후속 기능을 무작정 덧대 구조를 over-complicate하지 않도록 **최종 형상(완성본)을 먼저 고정**하고,
> 모든 향후 증분이 직교적으로 붙는 **계약(seam)** 을 정의한다.
> 진행 현황·다음 세션 인계는 **§9 (맨 아래)** 를 먼저 읽어라 — 무엇이 끝났고 어디서 이어가는지.

---

## 0. TL;DR

- **rewrite 아님.** 코드 감사 결과 현 엔진의 뼈대는 직교적(verb × study × universe, 등급 B+). 옳은 설계를
  다시 만드는 것은 over-engineering(원칙 2)·overthinking(원칙 3)·검증불가(원칙 4)·동시작업 충돌.
- **대신: 목표 아키텍처를 지금 고정 + 약한 seam 3개를 1회 정비 + 검증된 핵심 유지 + 프런티어를 클린 add.**
- 약한 seam 3개 = ① 결과 계약(타입 없는 dict) ② shape→render 매핑(순서의존 switch) ③ capability 선언(수기 다중출처·드리프트).
- 데이터 수집층(컨센서스·수급·거시)은 **이미 main #149로 라이브** — 본 작업은 수집이 아니라 **표면화 + 엔진 아키텍처 + 챗 + 웹**.

---

## 1. 배경 — 왜 이 작업인가

현 챗봇은 **서술·검증 엔진**이다: describe(단일 서술)·screen(현재 순위)·simulate(과거 검증)·diagnose(입력 진단).
강력하지만, 사용자 프런티어 질문들은 엔진에 없는 **두 방향**을 요구한다.

- **처방축 (prescriptive):** "무엇을 담아라 / 어떻게 보여라" — 상관행렬 → 포트폴리오 최적화 → 추천 + 히트맵·트리맵.
- **내러티브·시장축 (explanatory):** "무슨 일이 왜" — 거시 맥락 + 뉴스 종합 + 시장 breadth/지수 + (실시간=보안경계).

그리고 이를 가로막는 **두 구조 결함**:

1. **표면화 단절** — 라이브 데이터(컨센서스·수급·거시)가 `describe`·`summarize`·`reference_data`·시스템 프롬프트
   4계층에서 차단된다. 특히 `prompt.py`가 모델에게 *"수급·컨센서스는 미수급"* 이라 **거짓 고지**(환각·자기검열 유발).
2. **선언 드리프트** — "내가 뭘 할 수 있나 / 어떤 데이터가 있나"의 진실이 `capability_spec`·`get_all_indicator_columns`·
   `prompt`·NL idioms 여러 곳에 수기 복제되어 실제 상태와 어긋난다(위 거짓고지가 대표 증상; `data_spec
   current_status="absent"`도 같은 부류였다 — *실제로는 라이브*).

---

## 2. 현 아키텍처 스냅샷 (정본 앵커)

| 계층 | 현 구조 | 평가 | 앵커 |
|---|---|---|---|
| verb 디스패치 | `run_query`의 if/elif (verb별 격리) | 🟢 clean | `core/quant_core/ir_engine/run.py` run_query |
| study 디스패치 | axis/reduction/relation_kind 중첩 | 🟡 확장시 조합복잡 | run.py (study 분기) |
| **결과 계약** | **타입 없는 dict** + 런타임 `result_shape()` 판별 | 🔴 seam #1 | `ir_engine/summarize.py` result_shape |
| **shape→render** | **순서의존 if/elif switch** (equity 분기 반드시 마지막) | 🔴 seam #2 | `web/src/components/ChatResultView.tsx` |
| **capability 선언** | `capability_spec()` + `get_all_indicator_columns()`(BASE+FUND만) + prompt + idioms | 🔴 seam #3 (드리프트) | `ir_engine/capabilities.py`·`indicators.py:538`·`server/app/chat/prompt.py` |
| context seam | `_attach_symbol_news` (post-hoc·엔진 밖·단일종목·뉴스만) | 🟢 좋은 패턴, 일반화 여지 | `server/app/routers/ir.py` |
| 데이터 | 컨센서스·수급·거시 **LIVE** (main #149) / 뉴스=스텁 | — | `data/feeds/*`·`main.py` 크론 |
| 검증 자산 | 골든 백테스트 불변식 · LLM-free 하니스(analysis_corpus·$0) | 🟢 강점 | `core/tests/analysis_corpus.py` |

**검증된 클린 확장 패턴 (사내 정본):** 블록 카탈로그 `blocks.catalog.get(op.name)` — 새 연산 = 파일 1개·자동 디스패치.
seam 정비는 이 레지스트리 패턴을 verb/result/render에 복제한다.

---

## 3. 목표 아키텍처 (완성본 형상)

6개 요소. 각: **계약 / 왜 / 무엇이 붙나**.

### 3.1 verb 축 확장 — `+PRESCRIBE` (처방)
- **계약:** `query="prescribe"`. 입력 = universe + `objective`(max_sharpe·min_var·risk_parity·equal_risk) + `constraints`
  (max_weight·sector_cap·long_only…). 출력 = 추천 비중 벡터 + (선택) 효율적 프런티어 + 진단.
- **왜:** 현 엔진은 *입력받은* 포트폴리오를 진단만(describe portfolio). 처방(구성·추천)은 새 verb로 직교 추가.
- **붙는 것:** Q4 "포트폴리오 비중 추천". 디스패치는 verb→handler 레지스트리로 정리(블록 카탈로그 패턴).

### 3.2 결과 계약 — 타입 판별 유니온 (seam #1, 최대 부채)
- **계약:** 각 결과형상 = Pydantic 모델, 공통 `shape: Literal[...]` discriminator 필드를 *스스로 선언*.
  `EngineResult = Annotated[Union[SimulateResult, SelectResult, DescribeSingleResult, …], Field(discriminator="shape")]`.
- **왜:** 현 `result_shape()`는 타입 없는 dict를 런타임 추론(`r.axis`·`r.report`…) → 순서버그·무성 누락. 형상이
  *결과 안에* 인코딩되면 추론·순서의존이 사라진다.
- **붙는 것:** 새 형상(상관행렬·추천·breadth)이 판별자만 추가하면 끝. summarize/serialize/web 모두 discriminator로 분기.
- **마이그레이션 안전:** 기존 dict 출력과 1:1 호환되는 어댑터를 두고 점진 전환(행동보존).

### 3.3 shape→render 레지스트리 (seam #2)
- **계약:** `RENDERERS: Record<shape, Component>`. `ChatResultView`는 `RENDERERS[result.shape]` 단일 조회.
  순서 무관·타입 안전. 노코드 빌더 `ResultPanel`과 동일 레지스트리 공유.
- **왜:** 현 순서의존 switch(equity 분기가 반드시 마지막)는 새 형상마다 수술·무성 파손 위험.
- **붙는 것:** 히트맵(matrix)·트리맵(hierarchy)·차트종류 선택이 레지스트리 등록 1건으로.

### 3.4 capability SSOT (seam #3, 드리프트 차단)
- **계약:** 능력·데이터 가용성을 **단일 파생 출처**로 — 엔진에 *등록된* verb/study/block + *실제 존재하는* 데이터
  컬럼(컨센서스·수급·거시 포함)에서 `capability_spec`·`reference_data`를 **생성**해 프롬프트·UI에 주입.
  `data_spec`를 진실원천으로 삼되 **실제 데이터 존재로 검증**(`assess_data_quality` 재사용, `current_status`
  하드코딩 라벨 신뢰 금지).
- **왜:** 수기 다중 선언 → 데이터 추가시 갱신 누락 → 드리프트(프롬프트 "미수급" 거짓고지).
- **붙는 것:** 새 데이터·verb가 *자동으로* 모델 식단·UI에 노출. 거짓고지 부류 영구 소멸.

### 3.5 context / narrative 사이드카
- **계약:** 일반화된 context provider가 **결정적 엔진 밖**에서 결과를 거시 레짐·뉴스·종합으로 enrich,
  해석단계(모델 식단)에 공급. `_attach_symbol_news`를 단일종목·뉴스 → 다(多)대상·다(多)모달리티로 일반화.
- **왜:** 텍스트/인과/거시 종합은 수치 IR이 아니다. 골든 불변식을 지키려면 엔진에 *넣지 말고* 옆에 둔다.
- **붙는 것:** Q2 "최근 이슈" 내러티브 · Q3 인과 맥락 · **준실시간 시세 스냅샷**(현재가·등락률, `_naver_quotes`
  일반화 — 골든 dataset엔 미누출, 표시·내러티브 전용). 종합은 LLM이 수행(엔진 무변) — 병목은 *입력 공급*뿐.

### 3.6 경계 명시 (boundary, not bolt)
- **실시간/장중 (정정 2026-06-21):** *준실시간 가격 스냅샷*(현재가·등락률, ~90초)은 **사이드카로 feasible** —
  기존 `_naver_quotes`(`server/app/industry.py:219`, 네이버 폴링·키 불필요·비공식 스크래핑) 재사용. 보안경계는
  **KIS 계좌·주문·자격증명·true-tick·트레이딩**에 한정(로컬 전용)이지 *공개 시세 조회*는 아니다.
  비범위로 남는 것: 임의 과거 *장중 분봉 이력*(무거운 피드)·true-tick 호가/체결(KIS)·매매신호용 정확도.
- **교차종목/헤지/페어:** 포지션이 종목별(Stage 1). Stage 2 포지션 모델은 명시적 후속 경계.
- **미래 예측(ML):** "백테스트=검증이지 예측 아님" 원칙 유지. 별도 트랙(합의 후).
- **뉴스 심층 인과:** 헤드라인+거시 종합은 사이드카로 즉시 / *깊은* 인과는 뉴스 벌크 수집(큰 투자).

---

## 4. 무엇이 가능해지나 (Q1~Q4 + 확장)

| 질문/작업 | 지금 | 목표 후 | 주 의존 요소 |
|---|:---:|:---:|---|
| Q1 히트맵/트리맵 시각화 | ❌ | ✅ | 3.3 레지스트리 + 3.2 새 형상 |
| Q2 최근 이슈(거시·뉴스 종합) | ⚠️ | ✅\* | 3.5 사이드카 + 3.4 거시 표면화 |
| Q3 "코스피 왜 빠져"(실시간) | ❌ | ✅(what)/⚠️(why) | *준실시간 시세*=사이드카 즉시(`_naver_quotes`) / *왜*=Phase 5 breadth·거시·뉴스 |
| Q4 상관 + 포트 추천 | ❌ | ✅ | 3.1 PRESCRIBE + 상관 relation_kind |
| 단일종목 전문 해석(컨센서스·수급) | ❌ | ✅ | 3.4 + describe 표면화 |
| 후속 기능 추가비용 | 높음(cruft) | 낮음(1~수 파일) | seam 3 정비의 복리효과 |

(\* 뉴스 *심층 인과*는 뉴스 벌크 투자까지 가야 완전.)

---

## 5. 단계별 마이그레이션 (검증 게이트)

순서는 **의존성·리스크·동시작업 충돌**로 결정. 각 단계 = 독립 PR, 골든 불변 + 하니스 $0 검증.

### Phase 1 — 설계서 (이 문서) · 검증=리뷰 ✅ **완료** (커밋 01bfebf)

### Phase 2 — capability SSOT + describe 표면화 ✅ **완료** 〔foundation·additive·저위험·최고레버〕
- **2a.** ✅ `get_all_indicator_columns`에 컨센서스·수급 컬럼 포함(`FLOW_INDICATOR_COLS`+`CONSENSUS_INDICATOR_COLS`) ·
  `prompt.py`의 "수급·컨센서스 미수급" 거짓고지 정정(라이브로). (커밋 3ad1331)
- **2b.** ✅ `run_describe_report`에 컨센서스·수급 블록(per-symbol 컬럼·PIT `df.loc[idx<=asof]`) · `summarize`
  describe compact 확장(`target_upside`·`analyst_count`·`consensus_opinion`·수급 20일 누적) · `ReportCards`에
  컨센서스/수급 카드. **+부류수정**: describe 요약 수익률·연변동성·MDD가 분수를 `%`로 표기(100× 축소)하던 버그를
  `_f`→`_pct` 일괄 교정. (커밋 b9e57c9)
  ⚠ **거시(macro)는 단일종목 df에 없는 교차-엔티티라 엔진 아닌 Phase 4 사이드카로 이동**(엔진-거시 결합 회피).
- **검증:** ✅ 전체 **805 pass**(신규 회귀 `test_indicator_surfacing`·`test_describe_surfacing`) · 골든 백테스트
  byte-identical · web build(tsc+vite)+eslint · ruff. **잔여 = 로그인 브라우저 E2E**(describe 카드 실렌더, 사용자측).

### Phase 3 — 결과 타입 유니온 + render 레지스트리 〔seam #1·#2〕
- 결과 Pydantic 판별 유니온(기존 dict 호환 어댑터) · `result_shape()` → discriminator · `ChatResultView` 레지스트리화.
- **점진:** 기존 11형상부터 1:1 행동보존 전환 → 순서버그 회귀 제거.
- **검증:** 하니스 형상별 출력 동등 · web 스냅샷 · 순서의존 회귀 테스트.

### Phase 4 — context 사이드카 일반화
- `_attach_*` → 일반 context provider(거시·뉴스·종합·**준실시간 시세 스냅샷**) · 다대상. `_naver_quotes` 재사용.
- **검증:** 골든 불변(엔진 무변) · 결정성 · 사이드카 실패시 graceful · 실시간 가격 dataset 미누출.

### Phase 5 — 프런티어 클린 add (독립·병렬 가능)
- 5a. 상관/공분산 `relation_kind="correlation"` → 히트맵.
- 5b. `PRESCRIBE` verb(최적화) → 트리맵 + 프런티어.
- 5c. breadth/지수 엔티티 → "코스피 왜/어때".
- **검증:** 각 하니스 케이스 + 골든 + 1e-N 수치 대조.

**충돌 회피:** `excel_export.py`·`core/tests`는 타 세션 in-flight → 회피/조율. `spec.py`·`run.py` 편집 전 brief 확인.

---

## 6. 검증 전략 (원칙 4)

- **골든 백테스트 불변식** — 모든 단계 byte-identical(거래내역·equity). 회귀 0 보장.
- **LLM-free 하니스** (`analysis_corpus`) — 결정적 코어 $0 무제한 회귀. 단계별 신규 케이스 추가.
- **웹** — build + eslint + preview 스냅샷(렌더 검증).
- **단계별 PR** — core/server/web 신호 첨부. 추측 완료 금지.

---

## 7. 비범위 · 리스크 · 롤백

- **비범위:** 실시간 틱 · 교차종목/헤지 · ML 예측 · 뉴스 심층 인과(별 트랙).
- **리스크:** Phase 3(결과 유니온)이 광범위 touch → *점진·행동보존·어댑터*로 완화. Phase 5 독립이라 격리.
- **롤백:** 각 phase 독립 PR · revert 가능 · 골든이 회귀 즉시 검출.

---

## 8. 4계층 매트릭스 (프런티어별 — 데이터·엔진·챗도구·웹)

| 기능 | 데이터 | 엔진 | 챗 도구 | 웹 |
|---|---|---|---|---|
| describe 컨센서스/수급/거시 표면화 | ✅ 라이브 | run_describe_report 확장 | summarize 확장·prompt SSOT | ReportCards 카드 |
| 상관행렬 | ✅(가격) | relation_kind=correlation | (simulate nl) | 히트맵 렌더러 |
| PRESCRIBE 추천 | ✅ | run_prescribe(optimizer) | 신규 verb·idiom | 트리맵+프런티어 |
| breadth/지수 | ✅(가격·섹터) | 신규 study·지수 엔티티 | describe 라우팅 | breadth 렌더러 |
| 내러티브(거시·뉴스) | 거시✅·뉴스=스텁 | (엔진 밖) | 사이드카·해석 | news/context 카드 |

> 빈 칸 = "미배선" 명시. 새 기능은 이 매트릭스 4계층을 모두 채워야 완성([[arch_four_layer_contract]]).

---

## 9. 구현 현황 & 다음 세션 인계 (hand-off) — 2026-06-21

이 절은 **"어디까지 했고, 다음에 무엇을 어디서 이어가나"** 단일 출처다. 다음 세션은 여기부터 읽어라.

### 9.1 의도(재확인) — 한 문단
챗봇이 주식투자자의 다양한 궁금증에 **①적절한 데이터 → ②정확한 연산 → ③직관적 시각화 → ④전문적 해석·조언**
4박자로 답하게 만든다. 현 엔진은 **서술·검증**(describe/screen/simulate/diagnose)엔 강하나, 프런티어 질문은
**처방축**(상관→포트폴리오 추천→히트맵/트리맵)과 **내러티브축**(거시·뉴스 종합·준실시간 "왜")을 요구한다.
방침 = **rewrite 아님**(뼈대 B+ 직교). 약한 seam 3개(결과계약·render매핑·capability선언)를 1회 정비하고,
프런티어를 클린 add. 데이터 수집은 이미 라이브(#149) — 본 작업은 **표면화 + 아키텍처 seam + 챗 + 웹**.

### 9.2 완료된 것 (3개 독립 draft PR — 미머지)
| PR | 범위 | 커밋/브랜치 | 검증 |
|---|---|---|---|
| **[#188](https://github.com/MercKR/quantman/pull/188)** | Phase 1 설계서 + Phase 2(capability SSOT·describe 컨센서스/수급 표면화·요약 100× 매그니튜드 부류수정) | `feat/chat-capability-redesign` 01bfebf·3ad1331·b9e57c9 | 805 pass·웹빌드·ruff·골든불변 |
| **[#189](https://github.com/MercKR/quantman/pull/189)** | 대화 세션 멀티대화(좌측 사이드바·rename/delete endpoint·자동제목, **DB 스키마 변경 0**) | `feat/chat-sessions` | chat API 26pass·웹빌드 |
| **[#190](https://github.com/MercKR/quantman/pull/190)** | 답변 쉬운말 executive summary 시작(프롬프트 `<answer_format>`) | `feat/chat-exec-summary` | 렌더검증·ruff |

- 세 PR은 **파일 분리·독립 머지 가능**. #188은 `core`(indicators·run·summarize)를 만지니 **희제(선물 등 core 작업)와 머지 타이밍 조율** 권장(surgical/additive라 충돌 시 해소 쉬움).
- **#189·#190은 아키텍처 로드맵과 직교**한 부가 개선(세션 UX·프롬프트). Phase 3~5와 독립.
- 머지·push·배포는 **사용자 명시 허락 시에만**(자동 금지). 머지 시 Railway(server)+Vercel(web) 자동배포. 머지 후 희제 알림.

### 9.3 다음에 이어갈 것 — Phase 3 → 4 → 5 (의존순)

**먼저 Phase 3** (seam #1·#2). Phase 5의 새 형상(히트맵·트리맵·breadth)이 render 레지스트리에 깨끗이 등록되려면
Phase 3가 선행이어야 한다. Phase 4(사이드카)는 엔진 무변이라 Phase 3와 **병렬 가능**.

- **Phase 3 — 결과 타입 유니온 + render 레지스트리.** 진입점:
  - 엔진: `core/quant_core/ir_engine/summarize.py`의 `result_shape()`(타입없는 dict 런타임 추론·순서의존).
    **첫 수**: 각 결과 dict에 `shape` 판별자 필드를 **결과 생성 시점에 직접 emit**(additive·행동보존) → `result_shape()`는
    그 필드를 읽기만. 그 다음 Pydantic 판별 유니온으로 점진 전환(기존 dict 호환 어댑터 유지).
  - 웹: `web/src/components/ChatResultView.tsx`의 순서의존 if/elif switch(equity 분기가 반드시 마지막) →
    `RENDERERS: Record<shape, Component>` 단일 조회로. 노코드 빌더 `ResultPanel`과 **동일 레지스트리 공유**.
  - 리스크: 광범위 touch → **점진·행동보존·어댑터**로 완화. 기존 11형상부터 1:1 전환.

- **Phase 4 — context 사이드카 일반화.** 진입점:
  - `server/app/routers/ir.py`의 `_attach_symbol_news`(post-hoc·엔진 밖·단일종목·뉴스만) → 일반 context provider로.
    **첫 수**: 준실시간 시세 스냅샷(현재가·등락률)을 `server/app/industry.py:219` `_naver_quotes` 재사용으로 enrich
    (네이버 폴링·키 불필요·~90초). 그 다음 거시 레짐·뉴스를 다대상으로 일반화. **모델 식단(해석)에만 공급, 골든 dataset엔 미누출.**
  - 보안경계: KIS 계좌·주문·자격증명·true-tick은 로컬 전용. *공개 시세 조회*(네이버)는 경계 밖 — 사이드카 OK.

- **Phase 5 — 프런티어 클린 add (독립·병렬).** 각 기능은 §8 4계층 매트릭스를 모두 채워야 완성:
  - 5a. 상관/공분산 `relation_kind="correlation"`(run.py) → 새 matrix 형상 → **히트맵** 렌더러. (Q4 전반부)
  - 5b. `PRESCRIBE` verb(`run_prescribe` 최적화기: max_sharpe·min_var·risk_parity + constraints) → **트리맵 + 효율적 프런티어**. (Q4 후반부) ⚠ 최적화 추정방법은 착수 전 사용자 협의(scipy 가능하나 방법 합의 필요).
  - 5c. breadth/지수 엔티티(신규 study) → "코스피 왜 빠져/어때"(Q3 *why*). 준실시간 *what*은 Phase 4 사이드카가 이미 커버.

### 9.4 검증 레시피 (착수 전 baseline·완료 전 게이트)
```
# LLM-free 결정적 코어 회귀($0·무제한) — analysis_diag 하니스
PYTHONPATH=core python scripts/analysis_diag.py          # 합성 코퍼스
PYTHONPATH=core python scripts/analysis_diag.py --real   # frozen 실데이터
# 전체 테스트
cd core && python -m pytest -q          # 골든 백테스트 불변식 포함
cd server && python -m pytest -q
# 웹
cd web && npm run build && npx eslint .  # tsc+vite+eslint
ruff check core server                   # 린트
```
**원칙 4**: 골든 byte-identical + 하니스 통과 + 웹빌드 없이 "완료" 선언 금지. UI는 로그인 브라우저 E2E(사용자측).

### 9.5 함정·주의 (반복 실수 방지)
- **데이터 진실**: 컨센서스·수급은 **라이브**(main #149·크론 `main.py:862-879`·볼륨 2.3GB). `data/spec.py`의
  `current_status="absent"`는 **stale 문서**(읽는 코드 0) — 믿지 말 것. 뉴스만 스텁(미배선).
- **매그니튜드 규약**: `summarize.py`에서 `_pct(v)`=분수×100(표시%), `_f(v)`=숫자그대로. 수익률·변동성·MDD는
  엔진서 **분수** → `%`로 표면화할 땐 반드시 `_pct`(Phase 2b에서 이 부류 100× 버그를 고쳤으니 답습 금지).
- **선존 ruff E702**(세미콜론): `run.py:215/231/306/335/771`·`chat.py` create_conversation — **내 코드 아님, 건드리지 말 것**(범위 규율).
- **체크아웃**: 작업 정본은 `_wt-diag`(워킹 체크아웃). `platform/`는 stale. 규칙·디자인 정본은 **origin/main**.
- **충돌 회피**: `core/*`는 희제와 겹칠 수 있음(선물). `excel_export.py`·`core/tests`도 타 세션 in-flight 가능 → 편집 전 brief 확인·조율.
- **커밋 메시지(한글)**: PowerShell `-m` 말고 **Bash(Git Bash UTF-8)** 로. main 직접 push 금지·작업단위 브랜치+PR.
- **시각화는 도구가 아니라 결과 형상(shape) 1:1** — 유저가 차트종류 못 고름. 새 차트 = 엔진 새 형상 emit + render 등록(Phase 3 후 1건).

### 9.6 관련 메모리·문서
메모리 `project-chat-capability-baseline`(워크플로·데이터 8모달리티·갭지도 SSOT) · 본 설계서(아키텍처 SSOT) ·
관련 [[project_chat_lab]]·[[project_question_engine]]·[[project_data_engine]]·[[arch_four_layer_contract]]·[[reference_retail_ai_demand]].

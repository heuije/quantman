# 챗봇 구조 재설계 — 파이프라인 6뿌리 (프로덕션 실측 기반)

> 상태: **승인됨 · P0·P1·P2·P3(부분) 구현·검증(브랜치 feat/chat-structural-p0)**. 2026-07-07.
> 근거: 프로덕션 실측 진단(floo.korea@gmail.com, 3대화·19턴 — `railway run python -m app.chat_analytics`).
> 진행: **P0 ✅**(별칭 D·게이트 F·예외계약 C) · **P1 ✅ 백엔드**(코호트 프리미티브·LLM 라우팅 e2e 검증;
> 웹 tsc 미검증) · **P2-a ✅**(이벤트스터디 adjustable/D5) · **P2-b ✅**(코호트 종목명 식별/新#5) ·
> **P2-c ✅**(무응답 드롭 관측성·GeneratorExit 백스톱/D3) · **P3-a ✅**(백테스트 함정 자동 verdict —
> 이벤트 연도 편중·`_year_concentration`·실 dev-data e2e 80% 발화/무발화 검증) · **P3-b ✅**(raw 증빙
> 엑셀 3갭 — 코호트 전용빌더·이벤트 전체시계열+신호수식·연도편중 블록·simulate 구조 byte-동일) ·
> **P3-c ✅**(정량 grounding — 의견은 수치와 함께·ok caveat surface 배선). **→ P3(E 뿌리) 완결.**
> **다음**: P4(카탈로그 노출·신규수집 실측) 또는 머지 준비. ⚠ 브랜치가 origin/main보다 크게 뒤
> (데이터 서빙 PR 다수 머지) — **머지 전 최신화 필수**. 웹 CohortComparison tsc/브라우저 미검증.
> 관련 선행: [chat-latency-context-redesign.md](chat-latency-context-redesign.md)(Phase2 복합비교)·[chat-reliability-redesign.md](chat-reliability-redesign.md)(결과계약)·[excel-export-redesign.md](excel-export-redesign.md).

---

## 0. 진단 요약 (실측)

19턴 지표: latency **p50 58.7s · p90 117.8s · max 334s(5.5분)**, bad_result 15.8%(infeasible 3, 전부 conv#41).

11개 증상(원 6 + 사용자 추가 5)을 **파이프라인 스테이지별 구조 뿌리**로 압축한다. 각 뿌리는
자의적 분류가 아니라 "질문→답변" 파이프라인의 한 스테이지에서 구조가 새는 지점이다.

| 스테이지 | 뿌리 | 핵심 증상(실측) |
|---|---|---|
| ① 대화제어 | **F** align 없이 실행 | 선택지 제안 후 답 안 기다리고 같은 턴에 바로 실행 |
| ② 계획/실행 | **A** 집합을 순차 팬아웃 | 종목당 simulate 1콜 → N종목=N라운드 → 334s·6라운드 |
| ③ 컴파일 | **B** NL→IR 비결정·자기치유 취약 | 같은 NL이 종목·run마다 실패(삼성 5·애플 2·월마트 1회) |
| ④ 결과계약 | **C** 정직·완결·조정·식별 미보증 | 예외 오귀인·adjustable []·조용한 드롭·코드만 표시 |
| ⑤ 증빙·인사이트 | **E** 증명·통찰 부재 | 정량근거 없이 분위기·raw 증빙 부재·함정 수동감지 |
| ⑥ 데이터(직교→흡수) | **D** 커버리지 오해 | 원/달러 "없음"—사실은 수집됨(이름 불일치) |

**메타 뿌리:** 챗봇이 *단일 엔티티 × 자연어 프리미티브 × 리액티브 LLM 루프*에 전적으로 의존 →
어려운 질문(다종목·다조건·최적화·정성판단)을 전부 "개별 NL 컴파일된 단일 엔티티 콜의 시퀀스"로
표현 → 비용(지연)·취약성(컴파일 실패)·불투명성(나쁜 복구)이 **곱셈으로 증폭**. `compare`(이번
세션)가 표형 지표비교를 1콜로 접은 첫 사례이고, 나머지 동사는 아직 안 접힘.

**D 재분류:** 데이터는 대부분 이미 수집됨(원달러환율=FRED DEXKOUS, COT/미결제약정, 실적캘린더,
컨센서스, 시총, 공매도, 13F). D는 "신규 수집"이 아니라 "심볼 해상도·카탈로그 노출·엔진 소비 배선"
문제 = **B/C/E 뿌리에 흡수**. (신규 수집 필요 소수: KR 개별 미결제약정·매크로 prod 백필 실측 — 별도.)

---

## 1. 실측 seam 지도 (코드 근거)

재설계는 추측이 아니라 아래 실제 코드 지점에 착지한다.

### A — 집합-값 실행 프리미티브
- **현 상태:** `run_sweep`([run.py:285](../../core/quant_core/ir_engine/run.py))의 `axis=="entity"`(:317)·
  `"label"`(:332)·`run_period_split`(기간)가 **집합을 1콜로 실행하는 경로로 이미 존재**. 그러나
  entity 축은 각 자산을 `d["query"]="simulate"; study.axis="none"`으로 **강제 재실행**(:324) —
  즉 **이벤트스터디(relate)·describe를 종목 집합에 팬아웃하는 경로가 없다.**
- **결과:** LLM은 "다종목 이벤트스터디"를 표현할 집합 경로가 없어 **단일종목 relate를 N번 반복** → 팬아웃 폭발.
- **재사용 자산:** entity/label 스윕 골격 + 이번 세션 `_run_compare`(select mode=compare, tools/run) 패턴.

### B — NL→IR 컴파일 비결정·자기치유
- **현 상태:** `compile_nl`([ir_compiler.py:506](../../server/app/ir_compiler.py))에 **이미 validate→repair
  루프**(`max_repairs=2`, :539)가 있다. 실패 시 검증 오류를 LLM에 되먹여 재프롬프트. 3회 다 실패 →
  `"검증을 통과하는 IR을 생성하지 못했습니다"`(:583, **Type A**).
- **비결정 근본:** Sonnet5가 **temperature 미지원**(:543 주석) → 온도=0 불가 → 같은 NL이 run마다 다른 IR.
  repair도 **LLM 기반**(같은 취약 LLM 재프롬프트)이라 param_grid·4조건 같은 어려운 타깃은 3회 다 실패.
- **결정적 정규화기 이미 존재:** `_route_directional`(:460)·`_force_attribute_filter_contains`(:486)·
  `_resolve_symbols`(:448) — **LLM 아닌 코드로 IR을 결정적 수리**하는 패턴. 이걸 확장한다.

### C — 결과·실패 자기서술 계약
- **현 상태:** `classify_status`([result_status.py:49](../../core/quant_core/ir_engine/result_status.py))가
  *반환된 결과 dict*에 status/diagnostics/verdict를 스탬프(계약 존재).
- **구멍 1 (예외 미포함):** 엔진 실행 예외는 `run_tool`이 안 감싼다(try는 `assemble_ir`만, [tools.py:403](../../server/app/chat/tools.py)).
  엔진 raise는 agent.py 캐치올(`_tool_failure_result`, [agent.py:159](../../server/app/chat/agent.py))로 전파돼
  **모든 예외가 동일한 `"조건을 단순하게…"` 일반 문구로 오귀인**(Type B, D2 뿌리). 즉 계약이 *반환결과*만 덮고 *예외*는 안 덮음.
- **구멍 2 (adjustable):** `res["adjustable"]=param_manifest(ir)`(tools.py:411)가 이벤트스터디 IR에서 `[]` 반환
  → "값만 조정(재컴파일 회피)" 경로 무력(D5).
- **구멍 3 (식별):** 결과 내 종목이 코드로만(composition.by_symbol·breadth top_gainers) — 이름 해상도 미배선.
- **구멍 4 (완결):** conv#43 대형 다축 쿼리가 assistant·메트릭 0으로 조용히 증발(경계된 완료 미보증).

### D — 심볼 해상도·카탈로그 (흡수)
- **현 상태:** 원/달러는 `원달러환율`(FRED DEXKOUS)로 **수집됨**([data_fetcher.py:115](../../core/quant_core/data_fetcher.py)),
  `ALL_SYMBOLS=ASSET+MACRO`(:154-157)라 엔진이 읽음. 봇은 `USDKRW`로 조회 실패.
- **근본:** `_resolve_symbols`의 `name_map`(ir_compiler:448)·`run_inspect`(tools.py:393, 별도 경로)에
  **티커→수집명 별칭(USDKRW→원달러환율, DXY→달러지수)이 없음**. 카탈로그도 매크로 미노출 → LLM이 존재를 모름.

### E — 증빙·인사이트 지능 (차별화)
- **현 상태:** 엑셀(`excel_export.py`, 이번 세션 EXTERNAL_FEED_COLS)·`composition.by_year`(결과에 이미 존재)·
  verdict(result_status.py에서 세팅).
- **갭:** ① 엑셀이 신호값만·전체 시계열+수식 없음 ② ~~by_year를 봇이 수동으로 눈치챌 뿐 자동 pitfall verdict 없음~~
  **✅ P3-a 해소**(`_year_concentration`이 event_study verdict로 자동 경고) ③ 정성 답변이 정량 근거로 grounding 안 됨.

### F — 대화제어 게이트
- **현 상태:** 프롬프트에 게이트 **이미 존재**([prompt.py:81-94](../../server/app/chat/prompt.py)): 고위험 모호성
  (ETF vs 선물=5배 레버리지)은 STOP, **"약한 모호성(파라미터·유니버스·기간·비용)은 그냥 진행"(:94)**.
- **근본:** 게이트가 **"자금 손해 위험"에만** 걸려 있고 **"사용자가 선택하려는 연구 설계 의도"에는 안 걸림** →
  사용자가 고르려던 선택지를 봇이 제안하자마자 기본값으로 스스로 골라 실행.

---

## 2. 통합 설계 — 심장 A+B, 계약 C, 차별화 E, 게이트 F

### A+B (심장) — 집합-값 코호트 프리미티브 + 결정적 assemble
`compare`의 일반화. **임의 동사(relate/event-study·simulate·describe)를 {종목×조건×기간} 집합에 1콜로.**

1. **엔진(A):** `run_sweep`의 entity/label 경로를 확장해 `query=simulate` 강제를 제거 — 집합의 각 원소에
   대해 **원래 query(relate 이벤트스터디 포함)를 실행**하고 per-entity 결과를 collated로 반환. 이벤트스터디
   코호트면 각 종목의 n_events·mean·p_value·composition을 한 표로.
2. **컴파일(B):** 코호트 요청을 **타입드 단일 spec**(symbols[]·조건 노드·windows·param_grid)로 받아
   `_run_compare`류 **결정적 assemble**로 IR화 → N개 취약 NL 컴파일을 1개 결정적 조립으로 붕괴(비결정·재시도 제거).
3. **도구 표면:** LLM이 "여러 종목/조건에 같은 분석"을 감지하면 단일 simulate 반복 대신 **코호트 도구**로 라우팅.
   → N종목=1라운드. 지연 top3 턴(334/170/118s)의 구조 원인 제거.

### B (컴파일 신뢰성) — 결정적 수리 확장
- 취약 클래스(param_grid 경로 합성·다조건 AND·기간 한정)를 **결정적 정규화기로 수리**(`_route_directional`
  패턴 확장) → LLM repair 라운드 소모 없이 자가치유.
- **코호트 assemble(위)**가 가장 취약한 다종목·최적화를 NL 컴파일에서 아예 빼내 근본 제거.

### C (결과계약) — 예외까지 덮는 자기서술 + 완결·조정·식별
- **예외 계약화:** 엔진 실행 예외를 엔진/도구 경계에서 잡아 **status=infeasible + 진실한 diagnostics
  (어느 종목·어느 스테이지·무슨 값)** 로 반환 — agent.py 일반 오귀인 문구 제거(D2). run_tool이 엔진 exec를 감싸도록.
- **adjustable 배선:** `param_manifest`가 이벤트스터디/relate IR에서도 조정 노브(임계·windows·기간)를 산출.
- **식별 배선:** 코드→종목명 해상도를 **결과계약 레벨**(serialize_ir_result)에서 균일 적용 — 렌더러별 아님.
- **완결 보증:** 경계된 부분결과(latency Phase1 재사용) 확장 — 대형 다축 쿼리가 조용히 증발하지 않게.

### E (차별화) — 증빙·함정·정량
- **raw 증빙 엑셀 ✅ P3-b:** 이벤트스터디 계열 3갭 종결 — GAP-2 코호트 오라우팅(빈 표)→전용 `_build_cohort`,
  GAP-1 전체 일별 시계열+신호 수식(`_raw_data_sheet` 추출·`_signal_panel` index 일반화·SIMULATE/EVENT 공용·
  단일 대상 '신호·발생' =IF(조건,1,0)), GAP-3 연도편중 블록(by_year→'연도별 이벤트 분포'). 검증=simulate 구조
  byte-동일·실 dev-data e2e·결정적 3종. 잔여=대량 이벤트 절단(GAP-4)·다중 pool 전체시계열.
- **함정 자동 verdict ✅ P3-a:** `_year_concentration`이 `composition.by_year`를 소비해 최다 연도가 이벤트의
  60%↑면 **레짐/기간 편중·상승장 전용 과대추정을 자동 경고**(result_status.py event_study 브랜치·status는
  ok caveat·top_year_share를 diagnostics에 정량 스탬프). 소표본(<5)은 편중 노이즈라 억제. 사람이 눈치채는
  대신 구조가 경고. 검증=결정적 5종+실 dev-data e2e(삼성 다년도 11.6% 무발화·에코 2023랠리 80% 발화).
  잔여=simulate(백테스트) 연도별 수익분해·regime-direction(by_regime) 직접 검출.
- **정량 grounding ✅ P3-c:** ①평가·의견은 도구결과 수치와 함께(평가형 질문은 정량 도구 먼저·prior 답 금지)
  ②status=ok여도 verdict의 정량 caveat(P3-a 레짐 편중·저신뢰 표본)를 `[참고: …]`로 노출→답에 녹여 경고
  ('정상'이라 드롭 방지). `_status_header` ok+verdict `[참고]` 배달 경로 결정적 잠금+프롬프트 불변식 2종.
  P3-a 함정 verdict가 status=ok인 이상 이 배선이 없으면 경고가 사용자에 미도달했다.

### F (게이트) — 위험 기준 → 사용자-선택 의도 기준
- 게이트 조건에 **"모델이 실제 선택지(2~3 연구방향)를 제시하는 순간"** 추가 → 제안 후 **STOP**,
  자기 질문에 스스로 답하지 않음. 명백한 연속만 ACT. (prompt.py:81-94 게이트 규칙 확장.)

### D 흡수
- **B:** `name_map`·`run_inspect`에 티커→수집명 별칭(USDKRW→원달러환율, DXY→달러지수).
- **C/E:** 매크로·COT·실적캘린더 시계열을 챗 카탈로그(prompt.py `_symbols_text`/analysis_menu)에 노출 → LLM 라우팅.

---

## 3. 구현 순서 (의존성)

의존 사슬: **B(assemble·결정적 수리) → A(코호트 엔진) → C(계약) → E(증빙·함정) → F(게이트).**
단, F·D-별칭은 저비용·독립이라 조기 착수 가능(빠른 신뢰 회복).

| Phase | 범위 | 뿌리 | 4계층 | 담당 경계 |
|---|---|---|---|---|
| P0 ✅ | 심볼 별칭(D) + F 게이트 프롬프트 + C 예외 계약화 | D·F·C | 서버(chat) | 조대표(챗) — **완료(507 green)** |
| P1 ✅(백엔드) | 코호트 프리미티브: 엔진 집합실행 + axis=entity 라우팅 | A·B | 코어·서버·웹 | 조대표 — **백엔드 완료(core698/server508·LLM라우팅 검증)·웹 tsc대기** |
| P2 ✅ | 결과계약 완성: adjustable(P2-a)·식별(P2-b)·완결 관측성(P2-c) | C | 코어·서버·웹 | 조대표 — **완료(server509)·웹 tsc대기** |
| P3 ✅ | 증빙·인사이트: **함정 verdict(P3-a)·raw 엑셀(P3-b)·정량 grounding(P3-c)** | E | 코어·서버·웹 | 조대표 — **완결(core708/server511)·웹 tsc대기** |
| P4 | 카탈로그 노출(매크로/COT/캘린더) + 신규수집 실측 | D | 코어·서버 | 조대표+데이터세션 |

각 Phase는 독립 PR + 실데이터/로컬 $0 하니스([[project_local_visual_testenv]]) 검증 게이트.

## 4. 4계층·협업 파급
- **엔진(A/C/E)** = 조대표. **웹 렌더(C 식별·E 증빙 표시)** = 희제 경계 — P2/P3에서 협의.
- **⚠ 데이터 세션 충돌:** 진행 중 PR#322(서빙 일원화·krdata.py·flow_kr·consensus). P4 카탈로그 노출이
  겹칠 수 있음 — 착수 전 협의. P0~P3은 챗/엔진이라 무충돌.

## 5. 검증 전략
- 로컬 $0 하니스로 각 Phase 실데이터 재현(라이브 프로덕션 진단 셋을 회귀 코퍼스로).
- **회귀 코퍼스:** 이번 진단의 실패 쿼리(삼성 다종목 이벤트스터디·환전·대형 다축·2020~2025 한정)를 고정 케이스로.
- 지연: 코호트 라우팅 후 다종목 턴 라운드수 N→2 확인. 신뢰: infeasible→정직 diagnostics 확인.

## 6. 미해결 / 재현 필요
- **Type B(런타임 infeasible) 정확 원인:** 컴파일 비결정이 만든 나쁜 IR vs 엔진 데이터 엣지케이스
  (삼성 2018 액면분할 불연속 가설) 미확정. 서버 로그 롤오프로 트레이스백 미확보 → **로컬에서 삼성 이벤트스터디
  IR 직접 실행 재현으로 P0/P1 착수 시 확정**.
- KR 개별 미결제약정·매크로 prod 백필 실측(P4).

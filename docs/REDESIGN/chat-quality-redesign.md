# 챗봇 품질 통합 재설계 (Wave 2) — 자기서술 계약 + 방법 지능

> 상태: **설계 승인 완료 · 구현 대기** (사장님 승인 2026-06-30). 접근 = **계약-우선 점진**(Wave 1과 동일, rewrite 아님).
> 출처: 사장님이 프로덕션 웹앱 챗봇을 직접 테스트하며 보고한 **10개 실측 증상**(2026-06-30) → §0.5 규율로
> *컴파일된 IR·실행결과·실데이터*에서 진단 → 7개 구조 뿌리(A~G) 합성. 진단 포렌식 전문은 작업자 원장 참조.
>
> **선행 문서와의 관계 (중복 아님):**
> - [`chat-reliability-redesign.md`](chat-reliability-redesign.md) (Wave 1, PR#236) — 프로덕션 25대화 진단 →
>   **결과 품질 계약**(status/diagnostics/verdict) 키스톤을 ④결과평가 단계에 깔았다. P1·P3·P5·P4 머지.
>   **본 문서(Wave 2)는 그 계약을 *5단계 파이프라인 전체*로 확장** — 10개 신규 증상이 "단방향 낙관" 병리가
>   ④뿐 아니라 **①방법선택·②컴파일·⑤표현** 단계에서도 살아있음을 증명했다.
> - [`chatbot-target-architecture.md`](chatbot-target-architecture.md) — 약한 seam①(결과 계약). 본 문서는 이를
>   *자기서술(provenance)* 까지 확장(메서드·IR·데이터·품질·구성을 모델·사용자·엑셀 3표면에 실음).
> - [`question_layer_spec.md`](question_layer_spec.md)·[`excel-export-redesign.md`](excel-export-redesign.md) — 본 문서가
>   소비/확장하는 분석 동사·증빙 엑셀의 정의.

---

## 0. TL;DR

- **하나의 병리(Wave 1과 동일 뿌리, 더 넓게 확인):** 챗봇은 *각 단계가 "앞 단계가 옳았다"고 가정하고 "성공"을
  자기서술·자기검증 없이 다음으로 흘려보내는* 단방향 낙관 파이프라인이다. Wave 1은 ④만 닫았고, 신규 10증상은
  ①②⑤가 여전히 열려 있음을 보여준다.
- **키스톤 = 분석 자기서술 계약(Analysis Provenance Contract):** 모든 분석이 `{method, ir_summary, data, quality,
  composition}`을 들고 다니며 **모델·사용자·엑셀 3표면에 일관 표면화**. 이 하나가 ⑤E를 닫고 ④C를 강화하고
  ②F를 노출하고 ①G를 가능케 한다.
- **착수 순서 = 의존성·레버리지:** Phase 1(자기서술 spine + fail-soft) → Phase 2(방법 지능) → Phase 3(엔진
  substrate) → Phase 4(학습 hook). Phase 3은 의도적으로 뒤(타 세션 선물 작업과 조율).
- **회귀 게이트:** 10개 증상 = 골든 코퍼스. 각 Phase 후 재실행해 해당 뿌리 닫힘 확인.

---

## 1. 진단 — 5단계 분석 파이프라인의 단계별 실패

```
질문 ─①방법선택─②NL→IR컴파일─③엔진+데이터실행─④결과평가─⑤표현/답변─→ 사용자
       │G            │F            │A·B·D            │C          │E
```

| 단계 | 뿌리 | 한 줄 정의 |
|---|---|---|
| ① 방법선택 | **G** 분석 깊이·결단력·method-fit | 얕은 라우팅·가설형상에 안 맞는 방법 선택·method-fit 점검 부재 |
| ② 컴파일 | **F** NL→IR 충실성 | 의도를 드롭/오매핑(성장률·비교·필드) |
| ③ 엔진+데이터 | **A·B·D** substrate | 교차캘린더 정렬·데이터 정제/커버리지·선물 사이징 |
| ④ 결과평가 | **C** 결과계약 | 퇴화/빈/크래시를 정직 분류 안 함 |
| ⑤ 표현 | **E** 자기서술 | 방법론·스케일·기준·구성을 서술 안 함 (최빈·7증상) |

### 1.1 10개 증상 → 뿌리 (실측 근거 요약)

| # | 쿼리(요지) | 증상 | 근거(아티팩트) | 뿌리 |
|---|---|---|---|---|
| 1 | S&P500 신호 코스피선물 일중 롱숏 | S&P500 종가 결손 多 | 175/4030행 결손=미국휴장+주말/placeholder행(소스CSV). 교차자산 ffill은 있음(context.py:74) | A·B·E |
| 2 | (동) | 2020-09말부터 수익률 평탄 | 자산<1계약값→정수 0계약 무언중단. budget=현금×20%·denom=가×25만×0.10(engine.py:391). 데이터 멀쩡 | D·C |
| 3 | KOSPI+NASDAQ 저PER×영업이익성장 분기 | 1억 기준 미명시·원자료 NASDAQ "없음"·9개월 무거래·PIT경고 유저 미가시 | 거래종목 1301중 US 447 실재(원자료 40캡 절단 excel_export.py:42)·펀더 얕음(삼성 2021+)·PIT는 as_of인덱스로 준수 | E·B·C·F |
| 4 | 오늘 코스피·SK하이닉스 무빙 | 크래시·이벤트엑셀 요약만·섹터 무차별 pooling | agent.py:301 crash-only·_build_event 요약표만(excel_export.py:149)·n=12195 단일풀 | C·E |
| 5 | 오늘 나스닥 롱/숏? | 얕은 답(RSI/MA/뉴스)·정량확률 없음 | 라우팅 '전망→describe'(prompt.py:43)·예측금지 가드 부작용(prompt.py:91)·이벤트스터디 미동원 | G |
| 6 | (나스닥선물 차트) | RSI/MA 안 보임 | 단일 YAxis에 Close(~3만)+RSI(0~100)+vol(<1) 동축(ChatResultView:82) | E |
| 7 | S&P500 30/20/10일 -1σ 매수 | 백테스트 로직(기간·체결·신호기준) 답변 누락 | summarize_result가 결과만·방법론 누락(summarize.py:170~). IR엔 있으나 compact_summary 미추출 | E |
| 8 | (동, sweep) | window 2개 의미 모호·엑셀 요약만 | z-score window 2역할(룩백+롤링std)을 둘 다 스윕→3×3 라벨 | F·E |
| 9 | 삼성전자 매수추천? | 좋은 가설(외국인 수급)→IC 검증 실패·빈차트 | 단일종목 시계열 가설을 횡단 IC(rank axis=1, run.py:526)로→표본0. 이벤트스터디가 정답인데 미선택 | G·C·E |
| 10 | (#8 세션) | "3비교 요청→기본조건만" 시행착오 | 비교=study.axis=parameter 필요한데 기본 axis=none 폴백(ir_compiler.py:207)→self-correct 재시도 | F |

### 1.2 인과 사슬 (왜 한 증상이 다른 증상을 부르나)

> **G**(잘못된 방법) → 실패 시 **C**(빈 결과를 정직 고지 못 해 빈 차트) / 성공 시 **E**(자기서술 부재→사용자
> 검증 불가) → 그 자기서술 부재가 **F**(오매핑)까지 숨긴다.

→ **레버리지 중심: ⑤E(자기서술)를 닫으면 F가 드러나고(예: #7에서 방법론 밝혔으면 "성장률 vs ts_delta" 즉시 검출),
C가 강화되고, G가 검증가능해진다.** 그래서 Phase 1을 자기서술 spine으로 시작한다.

### 1.3 보존해야 할 강점 (퇴행 금지)

#9에서 챗봇이 **"외국인 수급이 삼성 반등 핵심 트리거"라는 합리·창의적 가설**을 생성했다. 이는 챗봇의 진짜
차별가치다. 본 재설계는 *가설 생성을 죽이지 않고, 그 가설을 검증하는 방법선택(G)·정직성(C)·자기서술(E)을
강화*한다. 즉 "좋은 아이디어가 빈 차트로 죽는" 경로만 닫는다.

---

## 2. 키스톤 — 분석 자기서술 계약 (Analysis Provenance Contract)

모든 분석이 다음을 **들고 다니며 모델·사용자·엑셀 3표면에 동일 출처로 표면화**:

```
provenance = {
  method:      { chosen, rationale, fit_check },                      # 어떤 방법·왜·이 가설형상에 유효한가  (G)
  ir_summary:  { period, fill, signal_basis, sizing, universe, rebalance },  # 무엇을 어떻게 돌렸나           (E·F노출)
  data:        { coverage, gaps, cleaning_flags },                    # 무엇이 비었나·정제이슈               (B노출)
  quality:     { status, significance, sample, verdict },             # 유의·표본·퇴화                      (C강화)
  composition: { segments }                                          # 섹터·시기·세그먼트 분해              (#4c)
}
```

- **생산:** 각 단계가 자기 몫을 스탬프(method=①, ir_summary=②, data=③, quality=④, composition=③/④).
- **재사용(이미 머지 — 새로 안 만듦):**
  - `execution_summary.py`(#252) — 4분류 실행명세 = `ir_summary`. 전략상세에만 배선·**챗 미배선**.
  - data-coverage manifest(#253, `diagnostics.field_coverage`) = `data.coverage`.
  - `result_status.py`(#236) = `quality`. excel 자기서술(7132392).
- **소비(3표면 동일 출처):** ① `compact_summary`(모델 식단) ② `ChatResultView`(사용자) ③ `excel_export`(증빙).

---

## 3. 단계 계획

### Phase 1 — 자기서술 Spine + Fail-soft (E + C) · 토대 + 체감치명
닫는 증상: #1·#3·#4a·#4b·#4c·#6·#7·#9b·#9c (10중 9)

| T | 작업 | 파일 | 닫음 | 재사용/검증 |
|---|---|---|---|---|
| T1 | execution_summary → **compact_summary**(모델이 방법론 봄) | `chat/tools.py` | #7·#3·#1 | execution_summary.py · 단위 |
| T2 | provenance → **ChatResultView**(사용자가 봄) | `web ChatResultView.tsx`·api·types | #7·#1 | web build |
| T3 | **크래시 fail-soft**(막다른길→부분결과+실패부류+복구제안) | `chat/agent.py` | #4a | 단위:tool raise→구조화 |
| T4 | **신규 퇴화케이스**(0표본·자본부족·장기무거래→honest status) | `ir_engine/result_status.py` | #9b·#2 | classify_status 확장 |
| T5 | **스케일인지 차트**(이질스케일 패널분리) | `ChatResultView.tsx`·DESIGN.md | #6·#9c | preview |
| T6 | **%정규화 곡선 + 기준명시** | `EquityChart`·`ChatResultView` | #1 | preview |
| T7 | **구성 분해**(pooled study 세그먼트) | `ir_engine/summarize.py`·`ChatResultView` | #4c | 단위 |
| T8 | **이벤트스터디 엑셀 증빙 패리티**(raw+수식) | `ir_engine/excel_export.py`(_build_event) | #4b | openpyxl-lazy 보존·골든 |

착수 순서: **T3(독립·체감치명) → T1·T2(spine) → T4(퇴화) → T5~T8(표현, 병렬 가능).**

### Phase 2 — 방법 지능 (G + F) · 분석 깊이
닫는 증상: #5·#8·#9·#10·#3(필드)

- **2a. 방법선택 plan + method-fit gate** — 의도→후보(데이터×방법) + "이 방법이 이 가설형상(단일종목/횡단/시계열)에
  유효한가" 점검. 방향/전망→국면-조건부 이벤트스터디(#5)·단일종목 예측→시계열 이벤트스터디(IC 금지, #9).
- **2b. 분석 레시피 라이브러리(전문가 prior seed)** — plan이 검색하는 카탈로그. NL→IR 쿡북 패턴을 *방법선택*으로 확장.
- **2c. F 충실성 레시피** — 성장률 vs ts_delta(펀더, #3)·비교→sweep 안정화 + 이중window 명확화(#8·#10)·필드선택
  (ttm_ebit vs op_margin, #3). **Phase 1 provenance가 탐지기**(IR 표면화→드리프트 검출).
- ⚠ 과대약속 금지: 방향성 질문의 "정량 확률"은 *기저율 근처·넓은 CI·약한 유의성*으로 정직히. 막연 vibe→유의성검정된
  확률+단서로 교체가 목표(예측 단정 아님). prompt.py:91 가드와 양립.

### Phase 3 — Substrate 정확성 (A + D) · 엔진 (B는 데이터엔진 세션 조율)
닫는 증상: #1(캘린더)·#2(사이징)

- **3a. A 교차캘린더 정책** — 미국휴장/한국개장일 갭 명시처리(교차자산 ffill은 존재) + 주말/placeholder 행 정제는
  데이터엔진 세션과 조율. 정책을 provenance에 표면화.
- **3b. D 선물 사이징 현실화** — 20%증거금캡+정수내림 무언freeze → 목표레버리지 모델(거래소증거금 되면 ≥1계약,
  또는 미니선물 단위) + "자본부족·0계약" degenerate status(Phase 1 T4와 연결) + 헤드라인지표=실거래기간 기준.
  ⚠ **futures-orderable-clamp 세션의 "Phase 1.5"(카탈로그 0.10→실증거금 교정)와 동일영역** — 그 작업 머지 후 그 위에
  honest 레이어를 얹는다(Phase 3 후순위 = 자연 조율).

### Phase 4 — 학습 hook (flywheel substrate only)
- {질문의도 지문·선택방법·provenance/품질·사용자후속신호}를 `ChatTurnMetric`(존재) 확장 적재 + 레시피
  라이브러리 prior seed. **본격 검색+큐레이션 루프 = 별건 Phase**(효과적 신호 신뢰성·과적합/노이즈 난제 선결 후).

---

## 4. 경계·조율 (타 세션 · origin/main 검증 2026-06-30 ee511cf)

- **내 Phase 1 대상 파일은 #236(d5aaf99) 이후 origin/main 무변경** (agent.py·tools.py·result_status.py·
  prompt.py·ChatResultView.tsx) → Phase 1 충돌 없음.
- **재사용(머지·충돌 아님):** execution_summary(#252)·coverage manifest(#253)·excel 자기서술(7132392).
- **base 필수:** openpyxl lazy(#257) — excel 작업이 laziness 보존(eager 재도입 금지).
- **Phase 3b ↔ futures-orderable-clamp "Phase 1.5"** — 백테스트 선물 사이징 동일영역. Phase 3 후순위로 자연 조율.
- 데이터 정제/커버리지(B) = 데이터엔진 세션(조대표) 소관. 본 설계는 provenance에 커버리지 표면화 + 정제 필요 플래그.

## 5. 검증 전략 (4원칙: 검증된 해결책만)
- **10증상 = 골든 회귀 코퍼스.** 각 Phase 후 10쿼리 재실행 → 해당 뿌리 닫힘 확인(증상 재현 불가 = 닫힘).
- $0 로컬: `analysis_diag`(결정적 코어)·Sonnet 4.6 compile 하니스(F)·result-contract 단위테스트·web preview(E).

## 6. 비-목표 (over-engineering 경계)
- 모델 파인튜닝 X(flywheel hook만). 레시피로 되는 곳에 새 엔진 프리미티브 X(atomic 원칙).
- 기존 자산(result_status·execution_summary·coverage manifest·excel 자기서술) 재사용 — 중복 구축 금지.
- 가설 생성(챗봇 강점) 약화 X — 검증·정직성·자기서술만 강화.

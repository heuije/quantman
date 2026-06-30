# 챗봇 품질 Wave 2 — Phase 2 실행계획 (방법 지능 G+F)

> 상태: **계획 확정 · 구현 대기** (사장님 승인 2026-06-30). 상위 설계 = [`chat-quality-redesign.md`](chat-quality-redesign.md) §3 Phase 2.
> Phase 1(자기서술 spine + fail-soft)은 **머지·배포 완료**([PR#262](https://github.com/MercKR/quantman/pull/262)→main `ecf59a6`).
> 이 문서는 Phase 2의 *상세 task 분해*(M1~M4) — 다음 세션이 바로 실행하도록 검증된 file:line 근거를 싣는다.

---

## 0. 핵심 진단 (실제 코드로 검증 — §0.5)

**병리:** 방향성/단일종목-예측 질문이 (G) 얕은 답(`describe`)·잘못된 method(단일종목에 횡단 IC)로 가고,
(F) 컴파일러 쿡북이 이벤트스터디·필드·비교·window를 안내하지 않아 오매핑된다. **엔진은 이미 다 지원** —
prompt/컴파일러 라우팅이 능력을 노출하지 않는 것이 근본. Phase 1(자기서술)이 이 드리프트를 *드러냈고*, Phase 2가 *교정*한다.

**검증된 사실 (2026-06-30, worktree `_wt-chat-p2` 기준):**

| 사실 | 위치 | 함의 |
|---|---|---|
| `study.event`·`study.label`·`windows`가 IR 스키마에 존재 | `server/app/ir_compiler.py:204` | 국면-조건부 이벤트스터디는 **표현 가능** |
| 엔진: `query=relate` + `study.event` → `_run_event_study`; `has_label`이면 by_regime 분리 | `core/quant_core/ir_engine/run.py:159`, `_run_event_study` | 방향성·국면 분석 **엔진 준비됨** |
| 엔진: `relate` + `event` 없으면 IC(`_run_ic_study`), 단일종목이면 `_empty`("종목 2개 이상") | `run.py:_dispatch_query`, `run.py:514` | 단일종목 IC = 빈 결과(infeasible) |
| **쿡북에 이벤트스터디 레시피가 0개** (14개 중 없음) | `ir_compiler.py:235~310` `<idioms>` | #5·#9 근본 — 모델이 이벤트스터디를 거의 안 씀 |
| 레시피 11이 "단일팩터 예측력=relation_kind='ic'"만 언급(횡단≥2종목 조건·단일종목 대안 누락) | `ir_compiler.py:300` | #9 — "삼성전자 예측력"→IC 오매핑의 정확한 출처 |
| `ts_delta` %수익률 경고는 **이미 있음**(레시피 2·#246) | `ir_compiler.py:252~256` | 중복 추가 금지 |
| repair 루프 `validate_fn(strat)->(issues, ok)`·intent 앵커 | `ir_compiler.py:434,481,493` | **method-fit 피드백 plug-in 지점** |
| 라우팅 "전망→describe" | `server/app/chat/prompt.py:39,43` | #5 방향성→describe 폴백의 출처 |
| 예측 금지 가드 | `prompt.py:90~91` | 정직성 불변식(M3과 양립) |

---

## 1. Task 분해 (M1~M4)

착수 순서: **M1(구조 안전망) → M2(쿡북) → M3(라우팅) → M4(F)**. 새 엔진 프리미티브 0(atomic 원칙 — 전부 쿡북/검증/프롬프트).

### M1 — method-fit gate (키스톤 · G 구조적 안전망) [#9, #5-partial] ✅ 구현 완료
컴파일된 IR의 method↔가설형상 부정합을 **결정적으로** 잡아 repair 루프로 되돌린다(프롬프트/쿡북이 놓친 것의 안전망).
⚠ NL 키워드 매칭(band-aid) 아님 — **IR 형상 기반** 근본 검증.

**§0.5 정정 (구현 중 실제 코드 검증):** 원안은 "단일종목 IC 미탐지 → 신규 `method_fit_issues` 함수"였으나,
**게이트는 이미 존재**한다 — `validate_strategy`(spec.py:684 IC·694 회귀·651 상관)와 엔진(`run.py:515` IC·`610` 회귀)
*양 surface 모두* 단일종목 횡단 method를 SEV_ERROR로 거부 중. 신규 함수는 **중복(원칙2 위반)**. 진짜 #9 뿌리는
*메시지가 "종목 2개 이상"이라는 막다른 제약*일 뿐 **올바른 대안(이벤트스터디)을 안내하지 않아** repair가 종목
추가(의도 왜곡)로 잘못 수렴하던 것. → **기존 게이트의 막다른 메시지를 이벤트스터디로 리다이렉트**(더 단순·더 근본).

- **변경:** `validate_strategy` IC·회귀 단일종목 에러 메시지(모델 대면·repair 피드백 경로)에 "단일종목의 예측력은
  이벤트 스터디(query=relate + study.event 조건 + windows)로 분석하세요" 추가. 엔진 `_empty` IC·회귀 메시지
  (사용자 대면 verdict 경로)에도 평이체 리다이렉트 추가 → **부류를 양 surface에서 닫음**.
  - 상관(correlation)·처방·breadth 단일종목 가드는 *예측 부류 아님*(동조성/비중/시장폭) → 무변경(scope 규율).
- **검증($0·완료):** `test_question_validate.py` 4종(단일 IC/회귀→이벤트 안내·횡단 IC 2종목 클린·단일 이벤트스터디 클린),
  `test_result_status.py`(verdict 리다이렉트 잠금). 전체 코어 **579 passed**. *end-to-end repair 루프 재컴파일*(NL→이벤트
  스터디 전환)은 LLM 필요 → M2 컴파일 하니스에서 검증(M1·M2 상보).

### M2 — 이벤트스터디 쿡북 레시피 신설 (F) [#5, #9] ✅ 구현·검증 완료
현재 쿡북에 이벤트스터디 레시피가 **없었다**(레시피 0개). 추가:

- **신규 레시피 15** (`ir_compiler.py` `<idioms>`): "[이벤트 스터디 — 신호 후 수익·방향성·단일종목 예측]
  → `query=relate` + `study.event`(condition·발생 여부; '돌파한 날'=cross) + `study.windows` + (국면 있으면)
  `study.label`(bucket). universe=single 가능. 결과=forward 수익 분포·승률·유의성·MAE/MFE." + 과대약속 금지 주석.
- **레시피 11(IC) 보강:** "IC·회귀는 횡단(종목 2+). 단일종목 예측력은 IC 아님 → 이벤트 스터디(레시피 15)."
- **few-shot 신설:** 단일종목 방향성 이벤트스터디(국면별) — "삼성전자 골든크로스 후 5/20일 수익, 상승/하락장 비교"
  (#5/#9 앵커). cross(돌파) + bucket(120일 추세부호) 국면 라벨. **사전에 validate_strategy·엔진 실행으로 패턴 검증.**
- **검증:** ① 결정적($0): `test_event_study_single_regime.py` — 단일종목 이벤트스터디 실행 + by_regime 2국면 분리 잠금.
  ② **LLM e2e($0·ClaudeCodeBackend 구독)**: #9·#5·#9b 3질문 모두 `query=relate + study.event`(국면질문은 +label)·
  universe=single로 컴파일(**repair=0** — 레시피+few-shot이 직접 앵커, IC 오매핑 0). M1 안전망은 백스톱 확인.

### M3 — prompt 라우팅 교정 (G) [#5]
- **`prompt.py:39,43` 라우팅 교정:** "방향성·롱숏·이벤트후수익 판단 → simulate(이벤트스터디)" — 방향성 질문을 describe 폴백 대신 분석 엔진으로. (describe는 *현황 요약*에만.)
- **정직성:** 방향성 답변은 *forward 수익 분포 + 유의성*으로(예측 단정 아님). `prompt.py:90~91` 예측가드와 양립.
- **검증:** chat_eval 라우팅 하니스(claude -p $0) — 방향성 질문 → simulate(event study) 라우팅.

### M4 — F 충실성 레시피 (컴파일) [#10, #8, #3-field]
- **비교 명확화** (#10): "여러 조건 *비교/나열*"=`study.axis=parameter`+`reduction=enumerate`(모두 보기) vs "*최적 1개*"=`reduction=extremize`. 레시피 10(`ir_compiler.py:292~296`)에 enumerate↔extremize 구분 명시.
- **z-score window 2역할** (#8): `<reference_data>` 또는 레시피에 "z-score window=롤링 std 계산기간(거래일). 룩백 기간과 혼동 금지" 명확화. (실제 z-score 블록 구현 `core/quant_core/.../blocks/` 먼저 확인 — agent 미조회 항목.)
- **필드 가이드** (#3-field): "영업이익 **절대**=ttm_ebit(규모) vs **률**=op_margin(효율). '성장'=%수익률 또는 부호(ts_delta는 절대차→부호만)." `<reference_data>`(`ir_compiler.py:220~225`)에 추가.
- **검증($0):** 컴파일 하니스 재컴파일(#10 axis=parameter·#3 올바른 필드).

---

## 2. 검증 전략 ($0 우선 · 검증된 해결책만)
- **F(컴파일러):** Sonnet 4.6 NL→IR 컴파일 하니스로 #5/#8/#9/#10/#3 재컴파일 → 올바른 IR 형상 단언. (하니스 위치=`scripts/chat_eval.py` 계열 / project_nl_compiler_reliability `compile-stats`.)
- **M1(method-fit):** validate_fn 순수함수 단위테스트 + analysis_diag(`scripts/analysis_diag.py`·결정적 IR 코퍼스) 케이스 추가.
- **G(라우팅):** chat_eval(LLM tool-selection) — 방향성→simulate.
- **회귀 게이트:** **10증상 골든 코퍼스** 재현(#5·#8·#9·#10·#3 닫힘 확인).

## 3. 불변식 (over-engineering·over-promising 경계)
- **새 엔진 프리미티브 0** — 전부 쿡북·검증·프롬프트(atomic 원칙). 엔진은 이미 이벤트·국면·비교 지원.
- **과대약속 금지** — 방향성 "확률"은 기저율 근처·넓은 CI·약한 유의로 정직히. Phase 1 result_status(이벤트 n<5 저신뢰·prob_positive)가 이미 honest 강제 + 예측가드 양립.
- **중복 금지** — ts_delta% 경고(레시피 2)는 이미 존재. 보강만.

## 4. 경계·조율
- Phase 2 대상 = `ir_compiler.py`·`compile_service.py`(validate_fn)·`chat/prompt.py` + core method_fit(신규). **인사이트 엔진·컴파일러 담당=조대표** — 충돌 영역 아님.
- Phase 3(엔진 substrate A·D, #2 자본부족 포함)·Phase 4(학습 hook)는 후속.

# 챗 분석 루프 근본 재설계 — 설계안 (MECE 근본수정 + 검증)

> 상태: **P1·P2·P3 구현·검증 완료**(branch `feat/chat-loop-redesign`, 로컬 커밋 — push/merge는 사용자 허락 대기).
> 진단 원장: 본 세션 트랜스크립트 분석(conv#1~8, prod Neon). 정본 기준: `origin/main`.
> 검증: core 358 pass · server 346 pass · 골든 무변경 · web tsc+vite build·eslint 0. 별개 A(PR-D 컴파일러 의미가드)는 후속 트랙.

## 0. 문제 — 한 줄

챗봇은 결정적 엔진을 **자연어 병목**으로 구동하고 결과를 **하드코딩 요약**으로 관측한다.
모든 결함은 이 **분석 왕복(round-trip)**의 네 변(邊) 중 하나다. 단건 땜질(예: "연도별 숫자만 요약에
넣기")은 한 변의 한 귀퉁이만 막는다.

## 1. MECE 프레이밍 — 왕복의 4변 + 별개 트랙

| 범주 | 인터페이스 | 현재 결함(실측) | 근원 |
|---|---|---|---|
| **① 명세 Specify** | 모델→엔진 (의도→IR) | 호출마다 `nl` 재컴파일·핸들 재사용 불가 → 같은 의도가 +64/−77/−6.5%로 발산 | `tools.py:run_simulate`가 매번 `compile_strategy(nl)` |
| **② 관측 Observe** | 엔진→모델 (결과→인지) | `compact_summary`가 **shape 맹목** 4스칼라, buckets 폐기 → 모델이 연도별을 못 봄 | `tools.py:334` 도구이름 기반 하드코딩 |
| **③ 제어 Control** | 루프 정책 | 수렴기준 없음 → 8회 헛돌이·동일 IR 중복·임의 최종선택 | `agent.py:176` 루프에 정지/중복 가드 부재 |
| **④ 표현 Present** | 엔진→유저 | 모델요약 ≠ 유저차트(desync)·도구결과마다 풀차트 난립 | `ChatLab.tsx:94,117` 파트별 풀렌더 |
| **별개 A** | nl→IR 컴파일 충실도 | 1회 컴파일도 의미 오류(단위·접두) | 검증기가 구조만 보고 의미 안 봄(Phase-0) |

비범위: 별개 B(데이터 커버리지·품질), 별개 C(대화 영속/탭복원 — ④의 인접 잔여, 후속).

## 2. 핵심 구조 원리 — "단일 분석 객체 + shape 파생 투영"

**한 분석 = 하나의 구조화 객체 `{ir, result, manifest}`.** 그 둘레의 모든 관찰자는 그 객체의
**투영(projection)**이고, 각자 임시로 만든 게 아니다.

### 현재의 병 — "result shape"가 암묵적·4중 중복

같은 result를 4곳이 **각자 다른 판별식**으로 분기한다:

| 소비자 | 위치 | 분기 기준 | 상태 |
|---|---|---|---|
| 엑셀 증빙 | `excel_export.py:build_strategy_excel` | equity/axis/query/report/reduction | OK(P2) |
| 챗 차트 | `ChatResultView.tsx:ChatResultBody` | query/report/reduction/axis/buckets/equity | OK |
| 빌더 차트 | `IrBuilder.tsx:ResultPanel` | (위와 동일 — 챗이 이걸 미러) | OK·중복원본 |
| **모델 요약** | `tools.py:compact_summary` | **도구이름** | ❌ shape 맹목 |

→ 같은 개념(result shape)이 4벌 → 드리프트, 그리고 1벌(모델 요약)은 깨졌다.

### 처방 — 엔진이 shape를 정본으로 찍고, 모두가 읽는다

`strategy_from_spec` 결과에 **`result["shape"]`를 1회 스탬프**(core 단일 분류기). 이후:
- 모델 요약 = `summarize_result(result)` — shape 파생 투영(P1).
- 엑셀·웹 = `result.shape` 읽어 분기(P3에서 인라인 판별식 제거).

이로써 ②(모델 shape 인지)와 ④(3투영 정합)가 **같은 분류기**에서 닫힌다.

## 3. 범주별 근본수정

### ② 관측 — P1 (먼저·가장 작고 효과 큼)

**신규 `core/quant_core/ir_engine/summarize.py`:**

```python
def result_shape(result: dict) -> str:
    """result → canonical shape 태그. 엑셀·웹·요약 공용 단일 분류기.
    (현재 excel_export·ChatResultBody의 판별식과 동일 집합을 한 곳으로.)"""
    if result.get("query") == "select":        return "select"
    if result.get("report") == "single":       return "describe_single"
    if result.get("report") == "portfolio":    return "describe_portfolio"
    if result.get("reduction") == "extremize": return "extremize"
    ax = result.get("axis")
    if ax == "relation":
        return "relate_regression" if result.get("relation") == "regression" else "relate_ic"
    if ax == "time":                           return "event_study"
    if ax == "signal":                         return "signal_dist"
    if ax and result.get("buckets"):           return "sweep"     # parameter/asset/condition/period
    if result.get("query") == "inspect":       return "inspect"
    if result.get("equity"):                   return "simulate"
    return "unknown"

def summarize_result(result: dict, *, max_buckets: int = 40) -> str:
    """shape별로 '모델이 답하기에 충분한 핵심 구조'를 텍스트로 직렬화.
    숫자는 결과에서만(지어내기 금지). 토큰 가드: 버킷 상한·소수 자릿수."""
    # simulate: cagr/sharpe/mdd/total_return (+bench)
    # sweep   : 버킷 key→cum_return/sharpe 전부(연/분기/월/파라미터/국면) — max_buckets 초과 시 상·하위 K + "외 N개"
    # extremize: argmax 셀 + 목적값 + oos_guard
    # relate  : 팩터별 coef/t/p (또는 horizon별 IC/p)
    # describe: 360 핵심(가격·52주·수익·밸류) / 포트 진단(HHI·집중)
    # select  : 상위 top_n 종목·점수
```

- `compact_summary(tool_name, result)` → 도구이름 분기 폐기, **`summarize_result(result)`로 일원화**(save_strategy 카드만 별도). simulate의 전 분석형상·screen·describe·inspect 모두 result_shape로 투영.
- ⚠ **P1은 `result_shape` 순수함수만**(엔진 결과계약 무변경 → 골든 절대안전). 엔진이 `result["shape"]`를 스탬프하고 excel·web이 그 키를 읽도록 3투영을 수렴시키는 건 **P3**(④ 표현 정합)로 이관 — P1을 가산적·저위험으로 유지.

**검증(②):**
- 단위 `core/tests/test_summarize.py` — 각 shape fixture(P2 evidence-excel 테스트가 이미 13형상 fixture 보유 → 재사용) → `summarize_result`가 그 shape의 핵심 숫자 포함: 연도별이면 `"2015"`·`"2020"`·`"2024"` 키와 값, sweep이면 파라미터별 값, extremize면 argmax.
- 골든: 엔진 결과 동일성(shape 키 가산만) — 기존 골든 14 무변경.
- 라이브: 같은 S&P 쿼리 1회 → transcript의 모델 요약에 연도값 등장(현재는 4스칼라뿐).

### ① 명세 — P2

simulate 결과는 이미 `ir`+`adjustable`(manifest) 동봉(P3). **조정 전용 도구**를 추가해 nl 재컴파일을 끊는다:

```python
ADJUST_TOOL = {  # tools.py
  "name": "adjust_analysis",
  "description": "직전 분석(simulate)의 '변수 값만' 바꿔 재실행. 비용·기간·top_n·보유기간 등. "
                 "새 전략이 아니라 마지막 분석의 파라미터 조정일 때만(재컴파일·발산 방지).",
  "input_schema": {"changes": [{"path": "manifest 경로", "value": "새 값"}]},
}

def run_adjust(session, conversation_id, changes) -> dict:
    ir = _last_simulate_ir(session, conversation_id)      # 이미 존재(tools.py:252)
    if ir is None: return {"success": False, "error": "조정할 직전 분석이 없습니다."}
    valid = {p["path"] for p in param_manifest(ir)}        # P3 manifest가 허용 경로 정의
    for ch in changes:
        if ch["path"] not in valid: return {"success": False, "error": f"조정 불가 경로: {ch['path']}"}
        _set_path(ir, ch["path"], ch["value"])             # P3 web setPath의 py 포팅(소)
    res = strategy_from_spec(ir, _load_dataset(ir))
    if res.get("success"): res.update(ir=ir, adjustable=param_manifest(ir))
    return res                                             # LLM 0회 — /ir/strategy와 동일 결정적 경로
```

- 새 의도 = `simulate(nl)` 1회 컴파일. 값 조정 = `adjust_analysis(handle)`. **명확 분리.**
- 재사용: `_last_simulate_ir`(존재)·`param_manifest`(P3)·`strategy_from_spec`·`_load_dataset`.

**검증(①):**
- 단위 `test_chat_tools.py` 확장 — `run_adjust(path="simulation.commission", value=0)`가 IR의 그 필드만 바꾸고 나머지 **byte 동일**. 잘못된 경로 → error.
- 라이브: "비용 빼고 다시" → `adjust_analysis` 호출(`simulate` 재호출 아님), 결과 IR이 직전과 **1필드만 차이**.

### ③ 제어 — P2 (①과 동반)

1. **프롬프트 계약**(`prompt.py`): "유효한 결과를 한 번 받으면 그것으로 답하라. 같은 질문을 재컴파일·재실행하지 말 것. 값만 바꾸려면 `adjust_analysis`. simulate는 '새 분석'에만."
2. **루프 중복 가드**(`agent.py`): 이번 턴에 실행한 simulate 결과의 IR 해시를 추적. 모델이 같은(또는 결과 IR이 동일한) 분석을 재호출하면 **캐시된 결과 반환 + "동일 분석" 표식**(엔진 재실행·재렌더 0). 상한 `MAX_TOOL_ROUNDS=8` 유지(방어선).

**검증(③):**
- 단위: 같은 tool_input 2회 → 2번째는 캐시 경로(엔진 호출 0) 단위테스트.
- 라이브: 같은 질문에 simulate **1회만** 호출 — `ChatTurnMetric.n_tool_calls`·`n_rounds` 분포 개선(현재 conv#8=4 → 목표 1). `chat_analytics stats`로 전후 대조.

### ④ 표현 — P3

1. **3투영 정합**: 엑셀·웹·모델요약이 `result.shape`(②에서 스탬프)를 읽도록 — `excel_export`·`ChatResultBody`의 인라인 판별식을 `result.shape` 분기로 교체(중복 제거). 모델이 보는 것 = 유저가 보는 것.
2. **중간결과 접기**(`ChatLab.tsx`): 한 턴의 tool_result들을 묶어 **마지막(유효) 하나만 펼치고**, 이전은 "분석 N회 중 1" 접힌 칩. 재실행이 ③에서 줄지만, 남는 다중호출도 시각적으로 1개.
3. (인접·별개 C) 대화 복원: `listConversations`/`getConversation`(이미 존재, `api.ts:386`) 마운트 배선 — 같은 PR 또는 후속 결정.

**검증(④):** 웹 `npm run build`+eslint(신규 0), 브라우저 — N회 호출 턴에서 차트 1개만 펼침, 모델 요약 숫자 = 차트 숫자 일치.

### 별개 A — 컴파일러 의미 충실도 (독립 트랙·독립 PR)

범위: nl→IR **1회** 컴파일의 의미 정확도. #168/#171이 *개별 예시*를 few-shot으로 박음 → **부류 가드**로 승격.

- **의미 가드**(`blocks/validate.py` 또는 컴파일 후처리):
  - 단위: `COMPARE_GROUP=="pct"` 지표 비교 상수의 크기 sanity — ±0.001류(전형적 100× 축소) 경고.
  - 크로스에셋: signal이 외부 심볼 `SYM.ref`인데 universe에 SYM 없으면(자기참조 둔갑 위험) 경고/확인.
- **골든 idiom 스냅샷**: `server/evals/compile_archetypes.py`(존재) 확장 — 이 S&P 쿼리 등 표준 관용구 → 기대 IR 고정. 회귀 방지.

**검증(A):** 컴파일 골든 스냅샷(idiom→IR), 의미가드 단위테스트. ※ 별도 진단·별도 PR — 본 설계는 방향만.

## 4. 단계 · PR 경계 · 게이트

| 단계 | 범주 | 산출 | PR | 머지 게이트 |
|---|---|---|---|---|
| **P1** | ② 관측 | `summarize.py`(result_shape 분류기 + summarize_result 투영기)·`compact_summary` 일원화·단위테스트 | PR-A | core green·골든 무변경·연도 buckets 요약 표면화 |
| **P2** | ①+③ | `adjust_analysis` 도구·setPath(py)·루프 중복가드·프롬프트 계약·테스트 | PR-B | 단위(1필드 diff·캐시)·라이브 rounds 1회 |
| **P3** | ④ | 3투영 `result.shape` 일원화·중간결과 접기·(대화복원?) | PR-C | 웹 빌드/eslint·브라우저 정합 |
| **A** | 별개 A | 의미 가드·골든 idiom | PR-D(독립) | 컴파일 골든·가드 단위 |

- PR-A→B→C 순서 의존(②의 shape가 ④ 전제). A는 병렬 독립.
- 각 PR: `feat/` 브랜치·**시작 시 draft PR**·머지/배포는 **사용자 명시 허락 시만**·희제 영역(server chat·web) 겹침 broadcast.

## 5. MECE 검증 매트릭스

| 범주 | 단위(pytest/vitest) | 골든·정합 | 라이브 신호(prod) |
|---|---|---|---|
| ② 관측 | summarize가 shape별 핵심값 포함 | 골든 14 무변경(shape 가산만) | transcript 모델요약에 연도/버킷값 |
| ① 명세 | adjust 1필드 diff·잘못된 경로 거부 | — | "비용빼고" → adjust 호출·IR 1필드차 |
| ③ 제어 | 동일 input 2회 → 캐시(엔진0) | — | n_tool_calls/n_rounds 하락(stats 전후) |
| ④ 표현 | — | 3투영이 같은 shape 분기 | 차트 1개 펼침·요약숫자=차트숫자 |
| 별개 A | 의미가드 경고·golden idiom→IR | 컴파일 스냅샷 고정 | 같은 쿼리 IR 안정(재발산 0) |

## 6. 왜 over-engineering이 아닌가 (4원칙)

- **신규 표면 최소**: `summarize.py`(1) + `adjust_analysis`(도구1) + 가드 몇 줄. 새 레이어·추상 0.
- **제거가 핵심**: shape-맹목 `compact_summary` 하드코딩, nl-재컴파일-재시도, 4중 판별식 중복을 **줄인다**.
- **기존 원시 재사용**: IR SSOT·`compile_strategy`·`strategy_from_spec`·`param_manifest`(P3)·`/ir/strategy`(P3)·excel dispatcher(P2)·`_last_simulate_ir`·`compile_archetypes`(eval).

## 7. 리스크·미해결

- 토큰: 투영기 버킷 상한(연도≈15 OK, 대형 스윕 상·하위 K). 측정해 조정.
- shape 스탬프가 result 직렬화·기존 소비자에 영향 없는지 P1에서 골든·웹 타입 확인.
- 대화 복원(별개 C) 스코프 — P3 동반 vs 후속, 승인 시 결정.
- 별개 A·B는 본 설계 밖(각자 트랙).

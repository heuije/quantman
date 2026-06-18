# 챗봇 IR 위임 재설계 — 설계 스펙

- **날짜:** 2026-06-18
- **상태:** 스펙 리뷰 대기
- **범위 단위:** 단일 응집 재설계 (원칙 1개 · 단계 구현)
- **관련:** 첫 챗봇 진단(simulate 8라운드 비수렴 prod 실패) · NL 컴파일러(`compile_nl`·#127) · 측정환경(`chat_analytics`, 재진단 검증) · 4계층 호환 계약

---

## 1. 목표·맥락

측정환경 첫 진단에서 **구조적 결함 부류**가 드러났다: 챗봇이 LLM에게 엔진 payload(StrategyIR 등)를 **맨손으로 짓게** 시키는데, 정작 검증된 NL→IR 컴파일러가 가진 스키마·관용구·교정피드백을 하나도 안 준다. 부류:

- 🔴 **simulate** — `signal` Node 트리 추측 → 8라운드 비수렴 → 36초 무응답 (prod 2건 0% 성공)
- 🟠 **save_strategy** — 동일 IR 구성
- 🟠 **screen** — 섹터필터 부재 → universe 추측 → "반도체" 오답
- 🟡 **inspect** — 컬럼 추측, 교정 없음
- 🔴 **에이전트 루프** — 라운드 소진 시 무응답

이 부류를 **한 원칙으로 구조적으로 닫는다**(단건 패치 금지).

## 2. 원칙

**챗봇 모델은 의도(자연어)를 선언하고, 공유 compile 레이어가 검증된 엔진 payload로 변환한다.** 전략 IR을 받던 도구가 NL을 받으므로 *모델이 IR을 추측하는 surface 자체가 제거*된다. IR 구성은 이미 검증된(12/12 archetype) `compile_nl`이 전담 — 재발명 0, DRY.

**토큰 분업:** 기계적·카탈로그-heavy IR 구성을 싼 Haiku(컴파일러)로 이전하고, 비싼 Sonnet(챗)은 대화에만 쓴다. 챗 프롬프트에 카탈로그·관용구를 *안* 넣어 경량 유지 → 위임은 효과성·토큰 둘 다 우위(현 상태=8 실패 Sonnet 라운드가 가장 비쌈).

## 3. 범위

**In:**
1. 공유 compile 진입점
2. simulate: IR → NL
3. save_strategy: IR → NL (+ 마지막 IR 재사용)
4. screen: `sector` 파라미터 → `universe.screener`
5. inspect: 교정 피드백
6. 에이전트 루프: graceful 종료
7. 시스템 프롬프트 갱신

**Out (YAGNI):**
- 챗 프롬프트에 카탈로그/관용구 임베드 (위임이라 불필요)
- describe NL 라우팅 (현재 안전 — symbol만 받음)
- 풀 NL screen 라우팅 (`sector` param으로 충분, 매 screen에 LLM콜 회피)
- block-builder 수준의 수동 IR 미세조정 (별도 UI 담당)

## 4. 아키텍처 — 데이터 흐름

```
유저: "S&P500 조건부 코스피200선물 당일매매 백테스트"
  → 챗봇 협의 (NL, Sonnet)
  → simulate(nl="...전략 완결 서술...")
  → [서버] compile_strategy(nl):
        compile_nl(nl, catalog, capabilities, ...)   # Haiku · repair 루프 · 관용구 · field_contract
        → { ir(검증됨), assumptions(explain_ir) }
  → [서버] strategy_from_spec(ir, dataset) → 백테스트
  → 반환 { success, results, ir, assumptions }
  → 챗봇(Sonnet): "이렇게 해석했어요: [assumptions]" + 결과·차트
  → 유저 정제(NL) → 재컴파일  /  "저장" → 마지막 ir 재사용(재컴파일 0)
```

핵심: 챗봇은 **기존 NL→IR→백테스트 파이프라인의 대화형 래퍼**. simulate 8-루프 소멸(컴파일러가 관용구#1 signed-score `select` 중첩으로 한 발에).

## 5. 컴포넌트

### 5.1 공유 compile 진입점 — `compile_strategy(nl)` (server/app/ir_compiler.py에 추가)
`compile_strategy(nl: str) -> {success, ir, assumptions, error?}`. `compile_nl` 옆(같은 모듈)에 두어 **router·chat 양쪽이 import**. 내부에서 `catalog_spec()`·`capability_spec()`·`get_all_indicator_columns()`·`valid_keys`·`name_map`·`_validate` 클로저를 배선해 `compile_nl(...)` 호출 + `explain_ir`로 assumptions 생성. **현재 routers/ir_compile.py에 흩어진 이 배선을 이 단일 함수로 추출**(DRY — router도 이 헬퍼를 쓰도록 리팩터). 검증: 추출 후 기존 `/ir/compile` 골든·테스트 무변경.

### 5.2 simulate: IR → NL (chat/tools.py)
- `SIMULATE_TOOL` 입력 스키마: `strategy`(IR object) **제거** → `nl`(string: "백테스트할 전략을 자연어로 완결 서술") 추가.
- run path: `compile_strategy(nl)` → 실패 시 명확한 error 반환(graceful), 성공 시 `strategy_from_spec(ir)` 백테스트.
- 반환: `{success, ...results, ir, assumptions}`. `ir`·`assumptions`를 tool_result에 포함(저장 재사용 + 유저 표시).
- `compact_summary`: 결과 요약 + assumptions 한 줄(컨텍스트 절약 — 기존 D3 compact 유지).

### 5.3 save_strategy: IR → NL + 마지막 IR 재사용 (chat/tools.py)
- 입력: `name` + (선택)`nl`.
- **우선순위:** 대화의 **마지막 성공 simulate tool_result의 `ir`를 재사용**(영속된 Message.parts에서 조회 → 재컴파일 0, 토큰 절감). 없으면 `nl`로 `compile_strategy`.
- 저장: 기존 `save_ir_draft`(검증된 IR).

### 5.4 screen: `sector` 파라미터 (chat/tools.py)
- `SCREEN_TOOL`에 `sector`(string, 선택) 추가 — 도구설명에 "섹터/업종명(예: 반도체)".
- `assemble_ir` screen 분기: `sector` 있으면 `universe.screener`(condition = `is_in(attribute("Sector"), [sector], match="contains")`)로 빌드. 엔진 기존 screener + contains-match 재사용. **추가 LLM콜 0.** 모델의 종목 universe 추측 제거.
- `sector` 없으면 기존 동작(symbols 또는 all).

### 5.5 inspect: 교정 피드백 (chat/tools.py `run_inspect`)
- 미존재 컬럼 시 error에 **유효 컬럼 목록(또는 근접 매치)** 포함 → 모델 자가수정. 소스: 해당 symbol df 컬럼 ∪ `qc.get_all_indicator_columns()`.

### 5.6 에이전트 루프: graceful 종료 (chat/agent.py)
- `for _ in range(MAX_TOOL_ROUNDS)` 소진 시 마지막 `stop_reason`이 여전히 `tool_use`면: **fallback 텍스트 1개 append**("요청을 완료하지 못했어요. 조금 더 구체적으로 말씀해 주시겠어요?") → 무응답 종료 방지(추가 LLM콜 0). 위임 후 이 경로는 드묾(simulate가 1라운드로 성공) — 방어선.

### 5.7 시스템 프롬프트 (chat/prompt.py)
- `tools_guidance`: simulate/save = "전략을 **자연어로 완결 서술**(IR JSON 짓지 말 것). 서버가 컴파일·검증한다." screen에 `sector` 안내.
- 카탈로그·관용구는 **미추가**(경량 유지). 협의 시 합의된 전략을 *완결된 NL 한 문단*으로 서술하도록 가이드(다음 simulate가 그대로 쓰도록).
- **전체 라우팅 보존**(simulate NL화가 챗봇을 백테스트 전용으로 좁히지 않도록 명시): 주가·데이터→`inspect` · 종목분석→`describe`(단일종목 360) · 스크리닝→`screen` · 백테스트→`simulate`(NL) · **일반 대화·투자 원론→도구 없이 직접 답변**. 개인 맞춤 투자자문은 범위 밖(교육적 일반론까지). 데이터 미수급 영역(뉴스·광범위 추정치·수급)은 hallucinate 금지·graceful decline.

## 6. 토큰 정제
- 전략 안 바뀐 턴 **재컴파일 0**(save/re-run = 마지막 ir 재사용).
- 컴파일은 싼 Haiku(캐시된 system prompt). Sonnet 챗 프롬프트 비대화 회피.
- `compact_summary`가 도구결과를 요약 — 8라운드 full payload 누적(관측 60K cache_read) 방지.

## 7. 에러 처리
- `compile_strategy` 실패(repair 후 `expressible=false`): `{success:false, error:"이 전략은 현재 표현할 수 없어요: <이유>"}` → 챗봇이 graceful 전달(8 무성실패 대체).
- screen `sector` 매치 0: 빈 결과 + 안내(섹터 분류 한계 — KSIC, contains-match).

## 8. 검증 — 측정환경으로 루프를 닫음
- **단위**(로컬 SQLite·mock): `simulate(nl)`→mock `compile_strategy`→백테스트 배선 · save 마지막 IR 재사용 · `screen(sector)`→screener IR · inspect 피드백 · 루프 fallback.
- **통합:** 실패했던 **S&P→코스피200 NL → `compile_strategy` → 유효 IR → 백테스트 성공.** `evals/compile_archetypes.py`(M5d 등 10종)로 컴파일러 커버리지 회귀.
- **재진단(배포 후):** `chat_analytics stats/transcripts` → simulate 성공률·라운드·지연·토큰 **델타 실측**(8라운드·36초 소멸 확인). 측정환경이 이 수정의 검증 도구.

## 9. 단계
- **P1 (핵심):** 공유 `compile_strategy` + simulate NL화 + 프롬프트. (최악 실패 닫음.)
- **P2:** save NL화+IR재사용 + screen `sector`.
- **P3:** inspect 피드백 + 루프 graceful.
한 스펙·한 브랜치, 단계별 커밋.

## 10. 파일 매니페스트
| 파일 | 변경 |
|---|---|
| `server/app/ir_compiler.py` | `compile_strategy` 공유 헬퍼 추가(compile_nl 배선 통합) |
| `server/app/routers/ir_compile.py` | 공유 헬퍼 사용하도록 리팩터(배선 중복 제거) |
| `server/app/chat/tools.py` | simulate/save/screen/inspect 스키마·run path |
| `server/app/chat/agent.py` | 루프 graceful 종료 |
| `server/app/chat/prompt.py` | tools_guidance 갱신 |
| `server/tests/test_chat_*.py` | 신규 테스트 |
| `web/src/components/ChatResultView.tsx`(+타입) | simulate 결과에 assumptions 표시 (선택·P2/P3) |

## 11. 리스크·확인사항
- `compile_nl`은 챗 레이어에서 호출 가능(audit 확인). `_validate` 클로저 배선을 공유 헬퍼로 **정확히 추출** 필요 — 추출 후 `/ir/compile` 회귀 테스트로 검증.
- 마지막 IR 재사용은 대화 `Message.parts`의 마지막 simulate tool_result.`ir` 조회(영속 데이터 기반 → 멀티워커 안전, 인메모리 캐시 아님).
- `compile_nl`은 LLM콜(Haiku) — 지연 ~3-9s. 8라운드 36s 실패를 대체하므로 순이득.
- web(assumptions 표시)은 선택 — 서버만으로 기능 동작(P2/P3로 미룸 가능).

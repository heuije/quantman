# 전략연구소 챗봇 전면 장애 — Sonnet 5 상향의 thinking·temperature 계약 변경

**일자:** 2026-07-03 · **심각도:** Critical · **상태:** ✅ 해소

## 요약
[PR#288](https://github.com/MercKR/quantman/pull/288)이 챗 LLM(`CHAT_MODEL`·`NL_COMPILE_MODEL`)을 Sonnet 4.6→**Sonnet 5**로 상향한 뒤, 프로덕션 챗봇이 **도구를 쓰는 거의 모든 질문**에서 `anthropic.BadRequestError(400)`로 실패. 사용자에겐 "분석 도중 일시적인 연결 문제로 멈췄어요… 잠시 후 다시 시도해 주세요"로 표시돼 **일시적 장애처럼 보였으나 실제로는 지속성**(재시도해도 항상 실패)이었다.

## 발견
사장님이 프로덕션 웹앱에서 반복 실패를 신고(describe·research_news 스크린샷). Railway 서버측 필터 로그(`railway logs --since 12h --filter "turn failed" / "anthropic"`)에서 실제 예외 포착:
```
anthropic.BadRequestError: 400 - invalid_request_error:
  'messages.1.content.0.thinking.thinking: Field required'
```

## 영향
- **1라운드(도구 호출)는 성공** → 중간결과(리포트·뉴스 카드) 표시. **2라운드부터 100% 실패** → 도구를 쓰는 모든 분석 질문(describe·simulate·screen·research_news 등 사실상 전부) 무응답.
- `simulate`(백테스트)는 별개 원인(아래 ②)으로 **1라운드부터 실패**.
- 오분류로 "일시적 연결 문제"라 표시돼 지표(bad_result_rate)에서도 은폐 → PR#288 배포 시점부터 미탐지.

## 근본 원인 (실측·§0.5)
Sonnet 5의 두 가지 계약 변경이 우리 코드와 충돌 — **부류**(서버 LLM 호출부 전수 3곳 중 2곳):

**① thinking 기본 ON (agent 오케스트레이터 루프)**
- Sonnet 4.6은 `thinking` 파라미터 생략 시 thinking OFF였으나 **Sonnet 5는 생략 시 adaptive thinking 기본 ON** → 응답에 `thinking` 블록 포함.
- `agent.py`의 `_block_to_wire`가 `text`·`tool_use`만 처리하고 그 외는 `{"type": t}`로 떨궈, **thinking 블록이 텍스트·signature 없이 `{"type":"thinking"}`으로 변형**돼 다음 라운드 메시지 히스토리에 실림.
- 레퍼런스: 멀티턴에선 thinking 블록을 **변경 없이 그대로** 돌려줘야 함 → 변형된 블록 = 400 `thinking.thinking: Field required`.

**② non-default temperature 거부 (NL→IR 컴파일러)**
- `ir_compiler.py`가 결정성 목적으로 `temperature=0`을 넘겼으나 **Sonnet 5는 non-default 샘플링 파라미터를 400으로 거부**(`temperature is deprecated for this model`). → 컴파일 매 호출 실패.

**③ 오분류(장애 은폐)**
- `agent.py::_classify_failure`가 `anthropic.APIError` 전부를 "transient(일시적)"로 분류 → **지속성 400(BadRequest)을 "잠시 후 다시"로 오안내**하고 메트릭도 error로 안 잡아 탐지를 지연.

## 대응 (근본 수정 — 부류 전수 차단)
서버 LLM 호출부 3곳 전수 확인 후:
1. `agent.py` 스트림 호출: `thinking={"type":"disabled"}`. 오케스트레이터는 thinking 불필요(4.6 검증동작)이라 끔 → thinking 블록 미생성 → 라운드마다 thinking 토큰 낭비도 제거.
2. `ir_compiler.py`: `temperature=0` 제거 + `thinking={"type":"disabled"}`. 결정성은 forced `tool_choice`+검증/repair 루프가 담당(temperature 미지원 대체).
3. `news_research.py`(단발·미노출이나 일관성): `thinking={"type":"disabled"}`.
4. `_classify_failure`: 재시도 유효(연결·타임아웃·429·5xx)만 transient, 4xx는 analysis로 **표면화**.

## 결과 (해소 검증)
- 단위: `test_chat_agent`·`test_ir_compile_rate_limit` **36 pass**(classify 재분류·thinking=disabled 가드 신규)·ruff clean.
- **실 Sonnet 5 스모크(프로덕션 키·`railway run`) — before/after 대조**:
  - AGENT: before(thinking ON)=`400 thinking.thinking: Field required`(**프로덕션 시그니처 정확 재현**) → after(disabled)=`round1 types=['text','tool_use'] → round2 OK`.
  - COMPILER: before(temperature=0)=`400 temperature is deprecated` → after=`OK`.
- 배포: PR#___ 머지 → Railway 재배포·health 200.

## 재발 방지
- **교훈: 모델 상향은 순수 스왑이 아니다** — Sonnet 5는 thinking 기본ON·temperature 거부 등 요청 계약이 바뀐다. 모델 변경 PR엔 **실 API 스모크(멀티턴 도구루프 1건)** 를 게이트로. ($0 구독 shim은 Opus라 Sonnet5 계약 위반을 못 잡는다 — 그래서 #288이 통과됐다.)
- **오류 분류: 지속성(4xx)을 transient로 묶지 말 것** — 은폐돼 탐지가 늦는다. 이 수정으로 향후 API 계약 위반은 error로 표면화.
- **멀티턴 루프는 어시스턴트 블록을 그대로 round-trip** — `_block_to_wire`의 `{"type": t}` fallback은 미지 블록형에 취약(현재는 thinking OFF라 무해하나 잠재 지뢰).

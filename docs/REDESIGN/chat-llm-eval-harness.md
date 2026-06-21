# 챗봇 LLM 무료 테스트·평가 하니스 (Claude Code 구독 백엔드)

> 상태: 설계 승인 대기 · 작성 2026-06-22 · 브랜치 `feat/chat-llm-eval`
> 관련: [chatbot-target-architecture.md](chatbot-target-architecture.md) · 결정적 코어 무료진단은 `scripts/analysis_diag.py`·`test_capability_contract.py`(별개·이미 존재)

## 1. 목표·배경

챗봇의 **결정적 코어**(IR→엔진→결과→요약/엑셀)는 이미 `scripts/analysis_diag.py`로 $0·무제한 테스트된다. 그러나 **LLM이 개입하는 부분**은 미검증으로 남아 있다:

- **오케스트레이터**(Sonnet 4.6): 도구 라우팅·멀티턴·최종 답변
- **NL→IR 컴파일러**(Haiku 4.5): 자연어 전략 → 검증 IR
- **뉴스 다이제스트**(Haiku 4.5): 기사 본문 → 증거 요약

이 세 지점을 **Anthropic API 비용 0**으로, 개발자 본인의 **Claude Pro/Max 구독**(Claude Code 헤드리스)을 백엔드로 써서 전수 테스트하고 결과를 자동 평가하는 오프라인 하니스를 만든다.

**비목표(YAGNI)**: 프로덕션 코드 변경, 웹 프론트 신규 렌더러, CI 통합(후속), 실 API 호출(출시 직전 스모크만 — §11).

## 2. 핵심 통찰 — 왜 $0가 가능한가

세 가지가 맞물린다(전부 코드/프로브로 검증됨):

1. **프로덕션 루프에 주입구가 이미 있다.** `stream_chat_turn(session, conversation_id, user_text, *, client=None, model=None)` ([server/app/chat/agent.py](../../server/app/chat/agent.py))은 LLM 클라이언트를 인자로 받는다 → **LLM 백엔드만 교체**하면 실제 프로덕션 루프가 그대로 돈다.
2. **`claude -p`(헤드리스)는 구독으로 인증·$0 API.** `claude setup-token`이 발급한 OAuth 토큰(`sk-ant-oat01…`)을 `CLAUDE_CODE_OAUTH_TOKEN`으로 주면 nested `claude -p`가 Pro/Max로 인증한다. `--model sonnet`/`--model haiku`로 **프로덕션과 같은 티어** 매칭. (프로브 결과: `삼성전자 어때?` → `{"tool":"describe","input":{"symbol":"005930"}}` 정확 라우팅.)
3. **DB가 기본 SQLite.** `QP_DB_URL` 기본값 `sqlite:///…/data.db` ([server/app/config.py:43](../../server/app/config.py:43)) — Postgres 불필요. 하니스가 로컬 SQLite에 영속하면 로컬 web+server가 같은 파일을 읽어 차트를 렌더한다(§8 viewing).

> ⚠️ **검증된 제약**: nested `claude -p`는 이 "Claude Agent SDK" 호스트 세션의 부모 인증을 **물려받지 못한다**(401). 반드시 `setup-token` 토큰이 필요하다. 또 호출당 **~43k 토큰 오버헤드**(CC가 글로벌 CLAUDE.md·스킬·MCP 로드)가 구독 쿼터에 잡힌다(API는 $0) → §10.

## 3. 아키텍처 — "뇌만 교체, 몸은 프로덕션"

```
시나리오(코퍼스)
  → run_chat_turn(session=로컬SQLite, client=ClaudeCodeBackend)
      ├─ .messages.stream(system, tools=TOOL_SCHEMAS, messages)   ← 오케스트레이터
      │     claude -p --model sonnet → JSON 결정 → tool_use/text 블록 변환
      │       └─ 실제 도구 실행(run_simulate/run_tool/run_adjust…) [결정적·$0]
      │             ├─ simulate → compile_nl: .messages.create(tools=[emit_strategy], tool_choice)
      │             │     claude -p --model haiku → {strategy,assumptions,expressible} → tool_use 블록
      │             └─ research_news → _digest: .messages.create(no tools)
      │                   claude -p --model haiku → 산문 → text 블록
      └─ 실제 summarize_result · attach_context · _persist(SQLite) 전부 경유
```

핵심: **실제 루프 코드**(compact 요약·멀티턴 와이어 복원·중복 IR 가드·`attach_context`·persist)가 그대로 실행된다. 과거 버그가 났던 바로 그 seam들을 진짜 코드로 검증한다.

## 4. 컴포넌트 (격리된 4파일, 프로덕션 무변경)

| 파일 | 책임 |
|---|---|
| `scripts/chat_eval/backend.py` | **`ClaudeCodeBackend`** — Anthropic SDK 부분 흉내. `.messages.stream()`·`.messages.create()`만 구현 |
| `scripts/chat_eval/corpus.py` | 시나리오 코퍼스(§7) |
| `scripts/chat_eval/run.py` | 러너 — 몽키패치 + `run_chat_turn` 호출 + 트랜스크립트 저장 |
| `scripts/chat_eval/grade.py` | 평가 — 결정적 assert + LLM-심판 → `REPORT.md` |
| `scripts/chat_eval.py` | CLI 진입점(run/grade 래퍼) |

### 4.1 `ClaudeCodeBackend` — shim의 3가지 모드

내부 `_call_claude(system, messages, model, tools, tool_choice) -> dict`가 공통:
- 토큰: `CLAUDE_CODE_OAUTH_TOKEN` env에서 읽음(없으면 명확한 에러).
- `system`(블록 리스트→텍스트 병합) + `messages`(와이어 배열→읽기쉬운 트랜스크립트) + `tools`(있으면 JSON 스키마) 를 하나의 프롬프트로 직렬화.
- 지시: tools 있으면 **"호출할 도구 하나를 `<decision>{\"name\":…,\"input\":{…}}</decision>`로만 출력, 다른 도구·설명 금지"**; `tool_choice`로 강제된 도구명이 있으면 그 도구만.
- 실행: `claude -p '<프롬프트>' --model {sonnet|haiku} --system-prompt '<우리 system>' --max-turns 1 --output-format json < /dev/null` (stdin 비움 필수).
- 파싱: 출력 JSON 봉투의 `result`에서 `<decision>…</decision>`/코드펜스 제거 → dict. 비-JSON·실패는 명시 에러(graceful, 시나리오 fail로 기록).

세 모드 분기:
1. **오케스트레이터** — `.messages.stream(model, max_tokens, system, tools, messages)` → 컨텍스트매니저 반환. `.text_stream`=빈 이터레이터(또는 1회 yield), `.get_final_message()`→`tools`가 있으니 결정이 도구면 `stop_reason="tool_use"` + `content=[ToolUseBlock(id, name, input)]`, 텍스트면 `stop_reason="end_turn"` + `content=[TextBlock(text)]`. `.usage`=봉투의 usage.
2. **NL→IR** — `.messages.create(model, system, tools=[emit_strategy], tool_choice={...})` → `content=[ToolUseBlock(id, name="emit_strategy", input={strategy, assumptions, expressible})]`. `compile_nl`의 수리 루프가 재호출하면 각 호출은 독립(현재 `messages`에 검증오류 tool_result가 들어오므로 claude -p가 자가수정).
3. **뉴스 다이제스트** — `.messages.create(model, system, messages)` (tools 없음) → `content=[TextBlock(text)]`.

> 블록 객체는 `.type`·`.text`·`.id`·`.name`·`.input`·`.content`·`.stop_reason`·`.usage` 속성을 갖는 가벼운 dataclass로 충분(프로덕션 코드가 접근하는 속성만).

### 4.2 러너 `run.py`

```
1. QP_DB_URL = 로컬 eval SQLite (기본 data.db 또는 chat_eval.db)
2. SQLModel.metadata.create_all(engine); 테스트 유저·대화 보장
3. monkeypatch: ir_compiler.anthropic.Anthropic = lambda **_: ClaudeCodeBackend()
                news_research.anthropic.Anthropic = (동일)   ← compile_nl·뉴스 커버
4. 각 시나리오:
     conv = 새 Conversation(test_user)
     for turn in scenario.turns:
         run_chat_turn(session, conv.id, turn, client=ClaudeCodeBackend())   ← 오케스트레이터 커버
     트랜스크립트(parts: text/tool_use/tool_result full) → chat_eval_out/<name>.json
```

`run_chat_turn`이 이미 `_persist`로 DB에 쓰므로, eval DB를 로컬 서버 DB로 두면 §8 viewing이 공짜.

## 5. 데이터 흐름 (한 시나리오: "삼성전자 저PBR 동종목 비교 백테스트")

1. 러너 → `run_chat_turn(client=shim)`.
2. 오케스트레이터 `.messages.stream` → shim → `claude -p --model sonnet` → `{"name":"simulate","input":{"nl":"…"}}`.
3. 실제 `run_simulate` → `compile_strategy` → `compile_nl` → `.messages.create(tool_choice=emit_strategy)` → shim → `claude -p --model haiku` → `{strategy:{…},expressible:true}`.
4. 검증 통과 → `strategy_from_spec`(엔진·$0) → 결과.
5. `attach_context`(시세·$0 best-effort) → `summarize_result`(요약·$0) → `_persist`(SQLite).
6. 오케스트레이터 2라운드 `.messages.stream` → 결과 요약 읽고 최종 답변 → `end_turn`.
7. 트랜스크립트 저장 → 채점.

## 6. 평가 기준 — "챗봇이 자기 계약을 지켰는가"

기준은 챗봇 시스템 프롬프트의 계약([prompt.py](../../server/app/chat/prompt.py) `<rules>·<reruns>·<consult>·<reading_results>`)에서 도출한다.

### Tier 1 — 결정적(코드 채점·pass/fail)
| # | 기준 | 채점 방법 |
|---|---|---|
| D1 | 라우팅 정확성 | 호출 `tool_use.name` == 기대 도구 |
| D2 | NL→IR 구조 | simulate/screen 동봉 `ir`이 기대 구조 부분일치(예: 저PBR→`query=select`·`select.descending=false`) |
| D3 | 숫자 무환각 | 최종 답변에서 추출한 숫자 ⊆ 도구결과 수치집합(허용오차 내) |
| D4 | 중복 재실행 없음 | 한 턴 내 동일 IR 서명 재등장 0(dup-guard 작동) |
| D5 | 수렴 | `stop_reason=end_turn` · 라운드<8 · 도구 success |
| D6 | 거부 정확성 | 개인 투자자문 시나리오: 도구호출 0 + 한계 명시 문구 |
| D7 | 시각화 중복(휴리스틱) | 리치 렌더러 shape(select·correlation_matrix·breadth·prescribe·describe_*)인데 답변에 마크다운 표(`|…|`) 존재 → 플래그(D-flag, 심판이 확정) |

### Tier 2 — LLM-심판(claude -p·정성·0~5점+근거)
심판에게 {시나리오 의도, 트랜스크립트, **결과 shape의 렌더러 설명("이 데이터는 <히트맵/트리맵/표>로 차트가 이미 뜬다, 사용자가 봄")**, 루브릭}을 주고 채점:
| # | 기준 | 질문 |
|---|---|---|
| J1 | 해석 충실성 | 도구결과 숫자를 왜곡·오독 없이 해석했나 |
| J2 | 응답성 | 질문에 실제로 답했나(동문서답 아님) |
| J3 | 무환각(정성) | 도구가 안 준 사실을 지어냈나 |
| J4 | 협의 적절성 | 모호한 질문에 빈 되묻기 대신 기본값+선택지 제안했나 |
| J5 | 쉬운말 요약 | 평이한 executive summary로 시작했나 |
| J6 | 정직성 | 백테스트를 "예측"이라 안 했나·못하는 걸 정직히 밝혔나 |
| J7 | **시각화 중복/가독성** | 차트로 이미 뜨는 데이터를 텍스트 표로 불필요하게 반복하지 않고, 해석·시사점 중심으로 간결한가 |

**통과 = Tier1 전부 pass + Tier2 평균 ≥ 임계(기본 4.0).** REPORT 하단에 출시 적합도 집계.

## 7. 코퍼스 — "기대 기능" 1:1 (~20 시나리오)

각 항목: `{name, turns:[…], expect:{tools, ir_asserts, answer_asserts, judge_focus}}`.

describe(단일) · screen(섹터필터) · simulate-백테스트 · simulate-연도별 · simulate-sweep · simulate-extremize · simulate-국면 · simulate-회귀/IC · simulate-이벤트 · simulate-포트진단 · simulate-종목비교 · inspect(시계열) · correlation · prescribe · breadth · research_news(recent) · research_news(range) · adjust_analysis(직전 변수조정) · save_strategy · 멀티턴 협의(모호→기본값제안→확정) · 거부(개인 투자자문) · 무환각(없는 데이터 요구).

## 8. 시각화 확인 — 3층

프로덕션(웹앱+API·과금)과 **별개의 오프라인 리그**임을 전제.

| 보는 곳 | 차트 렌더 | 용도 |
|---|---|---|
| Claude Code 터미널 | ✗ (JSON·텍스트) | 디버깅 |
| 자동 채점 REPORT.md | ✗ — "어떤 렌더러가·무슨 데이터로 뜨는지"를 **데이터로** 검증 + 텍스트 중복 채점 | 헤드리스·반복 |
| **로컬 웹앱** | ✅ 실제 렌더 차트 | 사람 눈 검수(가독성·중복) |

로컬 웹앱 경로: 하니스가 로컬 SQLite(=로컬 서버 DB)에 대화를 영속 → `cd server; uvicorn app.main:app` + `cd web; bun run dev` → 전략연구소 채팅에서 테스트 유저로 그 대화들이 사이드바에 뜨고 `ChatResultView`가 실제 차트로 렌더. **새 렌더링 코드 0**(기존 persist + 기존 렌더러 재사용).

## 9. 인증·이식성 (희제도 자기 계정으로)

- 각 개발자 1회: `claude setup-token`(자기 Pro/Max 구독) → `CLAUDE_CODE_OAUTH_TOKEN` 환경변수 설정 → 같은 `python scripts/chat_eval.py`. 사용량은 **본인 구독 쿼터**(각자 $0 API).
- **공유=코드(코퍼스·러너·채점기), 토큰=각자 로컬**. 하니스는 `CLAUDE_CODE_OAUTH_TOKEN` **env에서만** 읽는다(커밋되는 코드에 파일경로 하드코딩 없음).
- ⚠️ 조건: 희제도 **Pro/Max 구독 필요**(`setup-token`은 구독 전용). 토큰 **절대 커밋 금지** — `oauth_token.txt` 등 로컬 토큰 파일은 `.gitignore`에 추가.

## 10. 쿼터 예산·오버헤드 완화

호출당 ~43k 토큰 × 시나리오당 ~3콜 × 20 ≈ **~2.6M 토큰/풀런**(구독 쿼터·**API $0**). 완화:
- **run/grade 분리**: LLM 호출은 run 1회, 루브릭 튜닝은 `--regrade`로 트랜스크립트 무료 재채점.
- `--only <name>` 부분 코퍼스, `--no-judge` 결정적만.
- **1순위 구현 스파이크**: 오버헤드 최소화 측정 — 클린 cwd(글로벌 CLAUDE.md 회피)·`--strict-mcp-config`(빈 MCP)·스킬 비활성으로 43k가 얼마나 줄지 실측 후 호출 래퍼에 반영.

## 11. 충실도 한계·출시 게이트

`claude -p`는 **프롬프트형 결정**(우리가 system+tools+messages를 텍스트로 직렬화 → JSON 결정)이라 raw API의 native tool-use와 미세 차이가 있고, CC 자체 래퍼가 낀다. → **프롬프트·도구설명·NL→IR·라우팅 로직 버그엔 탁월**하나 프로덕션 모델 거동과 byte-identical은 아니다. **출시 직전엔 실 API 스모크 5~10건**(Haiku 몇 센트+Sonnet 소액)으로 최종 확인.

## 12. 범위 경계

- **test-only**: `scripts/chat_eval/` + `docs/` 만 추가. 프로덕션 `server/`·`core/`·`web/` 코드 **무변경**(주입구 `client=`·`anthropic.Anthropic` 몽키패치만 사용 — 둘 다 기존 seam).
- **골든 무변경**: 엔진 결정성 미접촉.
- KIS 자격증명·계좌 등 보안경계 무관(엔진/LLM 경로만).

## 13. 구현 단계 (writing-plans에서 상세화)

- **P0 스파이크**: claude -p 오버헤드 최소화 호출 래퍼 확정(§10) + 블록 dataclass.
- **P1 NL→IR**: `.messages.create(tool_choice=emit_strategy)` 모드 + compile_nl 단독 테스트(IR 구조 채점). 가장 단순·고가치.
- **P2 오케스트레이터**: `.messages.stream` 모드 + `run_chat_turn` 전체 루프 통과(라우팅·멀티턴).
- **P3 뉴스**: `.messages.create`(평문) 모드.
- **P4 채점·REPORT**: Tier1 결정적 + Tier2 심판 + 집계.
- **P5 viewing**: 로컬 SQLite 영속 확인 + 로컬 웹앱 렌더 1회 사람 검수.

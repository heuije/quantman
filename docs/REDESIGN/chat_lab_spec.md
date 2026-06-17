# 전략 연구소 대화형 챗봇 명세 (Conversational Strategy Lab)

> **문서 위상.** [`question_layer_spec.md`](question_layer_spec.md)·[`visualization_spec.md`](visualization_spec.md)의
> **상위 통합 명세**. 질문 큐브(엔진 능력)와 시각화(답의 마지막 층)가 *무엇을 계산·렌더하나*를
> 정의했다면, 본 문서는 그것을 **멀티턴 대화로 묶는 오케스트레이션 층**을 정의한다.
> NL 컴파일러(`routers/ir_compile.py`)의 one-shot 변환을 **tool-use agent 루프**로 확장한다.
>
> **제품 한 줄.** 전략 연구소 = **대화하며 실시간으로 분석·시각화·논의하고, 합의된 전략을 저장하는
> 챗봇.** 노코드 문장형 빌더를 대체한다. AI는 *무엇을 분석할지*만 정하고, **숫자는 엔진이 계산한
> 사실(tool_result)에서만** 나온다(4원칙 §검증·신뢰성 차별점 계승).
>
> **전제.** 전원 알파 테스터 → **하위호환 어댑터 없이 재설계**(IR 수렴 때 operand 제거 패턴 답습).
> 노코드 블록(SentenceTree)은 챗봇이 기능을 완전 흡수한 뒤 제거한다.
>
> **상태.** Draft (2026-06-17). 설계 합의용 — 구현 미착수.

---

## 0. 관련 문서·메모리

- 엔진 능력: [`question_layer_spec.md`](question_layer_spec.md) (4동사 큐브) · [`block_ir_spec.md`](block_ir_spec.md) (블록 IR)
- 시각화: [`visualization_spec.md`](visualization_spec.md) (결과형태→컴포넌트 계약)
- NL 신뢰성: `routers/ir_compile.py`·`app/ir_compiler.py` (idiom 쿡북·검증 repair)
- 4계층 호환: `arch_four_layer_contract` — 엔진·데이터·NL·웹이 *같은 의미로* 호환돼야 기능 완성
- 담당 경계: platform CLAUDE.md §2.3(인사이트 엔진=조대표)·§2.6·2.7(개별종목분석·포트폴리오=희제)

---

## 1. 동기 (Why) — 현재의 한계

현재 전략 연구소(`/lab` = `IrBuilder.tsx`, ~1557줄)는 **거대한 노코드 폼 + one-shot NL 변환**이다:

1. 자연어 입력 → `/ir/compile`(강제 `emit_strategy` 1툴, 1~3 LLM콜) → StrategyIR 반환
2. 그 IR이 **문장형 폼을 통째로 덮어씀**(`hydrate()` — "변환은 아래 폼을 덮어씁니다")
3. 사용자가 폼 편집 → *별도* `/ir/strategy` 요청으로 실행 → 결과는 페이지 하단 한 곳에 렌더

**근본 한계 3가지:**
- **모델이 실행 결과를 한 번도 보지 못한다.** compile과 run이 분리돼 있어 해석·후속제안·논의가 불가능.
- **1회성.** 대화 맥락이 없어 "아까 추천한 종목" 같은 참조가 불가능(thread/message 상태 0).
- **추상 의도를 조용히 단순화.** "유망 종목 사고 싶어"를 협의 없이 아무 전략으로 축소(NL 컴파일러의 알려진 실패).

본질 가치: **(a)** 추상 의도→구체 전략(협의), **(b)** 정밀 통계 조회, **(c)** 결과 해석·논의·후속제안,
**(d)** 합의된 전략을 draft로 저장(→ 자동매매가 인계).

---

## 2. 스코프 — B(완전 대체) + "저장만"

**IN (챗봇이 제공):**
- 분석 대화: `screen`(스크리닝)·`describe`(360/진단)·`relate`(관계·회귀)·`inspect`(원시 시계열 조회)
- 전략 구성·백테스트: `simulate`
- 합의된 simulate 전략을 **draft로 저장**(`save_strategy`)

**OUT (챗봇이 하지 않음):**
- **모의/라이브 실행** → 기존 자동매매 엔진이 담당(이미 `_assert_live_tradable` 게이팅 존재).
  챗봇은 draft까지만 — 머니패스 검증을 챗 안에 재현하지 않는다.
- 알림/모니터링(범위 밖, question_layer_spec 결정 계승), 임의 커스텀 시각화 생성.

**유지 (교체 안 함, 사용자 결정 2026-06-17).** 챗봇이 교체하는 것은 **`/lab`(전략 연구소)뿐**.
**`포트폴리오`·`개별종목분석`(StockDashboard) 전용 메뉴는 그대로 둔다** — describe 분석은 챗에서도
가능하지만, 희제의 전용 페이지는 제거·수정하지 않고 유지(엔진 러너·렌더 컴포넌트만 읽기전용 재사용).

**보안 경계(불변).** KIS 자격증명·계좌·원시 주문은 로컬 PC 전용(챗봇 무관). 챗봇 산출물=draft 전략 정의뿐.
고위험 경로(자금)는 전부 자동매매 쪽에 격리된 채 유지.

---

## 3. 핵심 아키텍처 결정 (Decisions)

| ID | 결정 | 근거(4원칙) |
|---|---|---|
| **D1** | **tool-use agent 루프** (손유지 "질문→분석 매핑표" 만들지 않음) | 매핑표 = `capability_spec`이 `spec.py`에서 드리프트한 그 부류의 재발. 모델이 자기기술 표면을 읽고 선택 → over-eng·드리프트 회피 |
| **D2** | **동사별 도구 5개** + `save_strategy` (범용 단일도구 아님) | 엔진의 기존 4동사 구조 반영 → 모델 선택 쉽고 스키마 단순. 단 `simulate`는 full StrategyIR(저장 대상) |
| **D3** | **컨텍스트 이중 표현** (full↔compact) | tool_result가 큼(수천 점 equity). 모델엔 compact 요약, 프론트엔 full payload → 컨텍스트 폭발 방지 |
| **D4** | **타입화된 도구결과 → 프리빌트 컴포넌트** (생성 코드 아님) | Claude artifacts와 UX 동일하나 깨진 viz·숫자 조작 위험 0. 모델은 *무엇을* 보여줄지만 결정 |
| **D5** | **숫자 규율** — LLM=서술자, 수치는 tool_result에서만 | question_layer_spec 계승. 신뢰성 해자 |
| **D6** | **엔진 확장 = `inspect` 동사 신설** + 컨센서스 히스토리 노출 | 현 4동사는 전부 집계/스냅샷 — "단일종목 원시 시계열"(목표주가 흐름 등) 동사 부재. 소매 최대수요 |
| **D7** | **모델 = Sonnet 기본**, 세션 예산 가드 | agentic 추론·논의엔 Haiku 약함. 멀티턴 비용 무한대 방지 |

---

## 4. 도구 정의 (Tool Specs)

각 도구는 LLM tool-use 스키마. 서버 핸들러가 입력을 StrategyIR로 조립(또는 신규 러너 호출) →
엔진 실행 → full 결과 저장·스트리밍 + compact 요약을 모델에 반환.

| 도구 | 입력(요약) | 엔진 경로 | full 결과 | 렌더(visualization_spec) | compact 요약 |
|---|---|---|---|---|---|
| `screen` | universe·screener·score signal·top_n/pct·as_of | `run_select` (`run.py`) | 랭킹 리스트 | `RankedListChart` | 상위 N 심볼+스코어, 개수 |
| `describe` | symbol \| portfolio(holdings·weights) | `run_describe_report` / `run_portfolio_diagnosis` 〔희제 갈래〕 | 360 / 진단 | `ReportCards` / `DiagnosisPanel` | 핵심 팩트(수익·밸류·집중) |
| `relate` | universe·factors·target·windows·kind(ic/regression/event) | `_run_regression_study` / `_run_ic_study` / `_run_event_study` | 관계+유의성 | `RegressionChart` 등 | 계수·t·유의성 헤드라인 |
| `simulate` | **full StrategyIR**(entry/exit/sizing/signal/universe + study) | `run_query`(simulate 계열: backtest/sweep/extremize/period_split) | 자산곡선·성과 | `EquityChart`/`SweepChart`/`ExtremizeChart` | CAGR·Sharpe·MDD·기간 |
| `inspect` 〔신규〕 | symbols·columns·window | **신규 러너**(§8) | 컬럼 시계열 | `LineChart`〔신규〕 | 최근값·변화·min/max |
| `save_strategy` | ir(simulate StrategyIR)·name | `createStrategy`(draft, engine=ir) | 저장 결과 | 저장 확인 카드+링크 | strategy_id·이름 |

**도구 스키마 = StrategyIR의 동사별 큐레이션 부분집합.** 서버가 `query` + 기본값을 스탬프해 full IR 조립
(최소 어댑터 — 모델 정확도를 위해 정당). `simulate`만 full IR을 그대로 받음(저장 산출물이므로).

**도구 호출 전 검증 재사용.** 모델이 emit한 IR은 실행 전 `validate_strategy`(query-aware) 통과 →
실패 시 issue 목록을 tool_result로 되돌려 모델이 교정(기존 repair 피드백 재사용).

---

## 5. 컨텍스트 관리 (D3)

### 5.1 이중 표현

한 번의 도구 실행 결과를 **두 소비자에게 다르게** 보낸다:

```
도구 실행 → full 결과
   ├── 프론트엔드 / DB : full payload      (차트 렌더 · 영속 · 감사)
   └── 모델 컨텍스트   : compact summary   (추론 · 회상 · 논의)
```

- `compact_summary(tool, result)`는 **서버 측 함수**(core 아님 — LLM 컨텍스트는 server 관심사).
- **단일 진실원천 = full 결과**(DB `Message.parts`에 저장). compact는 컨텍스트 빌드 시 **파생**(둘 다 저장 안 함).
- "아까 추천한 종목"은 `screen`의 compact(상위 N 심볼)가 컨텍스트에 남아 회상 가능 = 순수 협의 턴.
- 잘려나간 디테일이 필요하면 모델이 **결정적 재실행**(같은 as_of→같은 결과). 절대 지어내지 않음(D5).

### 5.2 토큰 예산

- 슬라이딩 윈도우 + (아주 길어지면) 옛 턴 러닝 요약. **P0은 compact만으로 충분.**
- 다단 압축·RAG-over-history는 *측정된 필요* 시만(overthinking 금지).
- 자주 참조되는 아티팩트(추천 종목·저장한 전략)는 항상-인윈도우 **pin**(가벼운 옵션, 필수 아님).

### 5.3 데이터 모델

```
Conversation { id, user_id, title, created_at, updated_at }
Message       { id, conversation_id, role(user|assistant), parts: JSON, created_at }
  parts[] = { type: "text",        text }
          | { type: "tool_use",    id, name, input }          # 모델이 emit한 IR(작음)
          | { type: "tool_result", tool_use_id, name, result } # full 엔진 결과
```

**DB는 논리적 턴**(user 메시지 + assistant 응답 = text+tool_use+tool_result)을 저장. **Anthropic 와이어
포맷**(tool_result는 user-role 블록)으로의 변환은 컨텍스트 빌더가 담당 — DB 모델 ≠ 와이어 포맷.

---

## 6. API & 요청 흐름

### 6.1 엔드포인트

```
POST /chat/message            { conversation_id, message }   # 메인 (스트리밍 SSE)
POST /chat/conversations      { }                            # 새 대화
GET  /chat/conversations      → 목록 (영속 스레드)
GET  /chat/conversations/{id} → 메시지 히스토리 (재렌더용 full payload)
```

기존 `/ir/strategy`·`/ir/validate`·`/ir/catalog`은 유지(엔진 실행·검증은 챗봇 내부에서도 재사용).

### 6.2 Agent 루프 (서버, 스트리밍)

```python
history = load_messages(conversation_id)
msgs    = to_wire(compact(history)) + [user_message]
loop:
  resp = anthropic.create(
           model = settings.CHAT_MODEL,                       # Sonnet 기본 (D7)
           system = SYSTEM(capabilities + catalog + idioms),  # ← 기존 컴파일러 프롬프트 재사용, 캐시
           tools  = [screen, describe, relate, simulate, inspect, save_strategy],
           messages = msgs, stream = True)
  if resp.stop_reason == "tool_use":
     for tu in resp.tool_uses:                                # 한 턴에 N개(병렬/순차) — 비교분석
        ir = assemble_ir(tu.name, tu.input)                   # 동사별 어댑터
        ok, issues = validate_strategy(ir)                    # ← 기존 검증+repair
        result = {error: issues} if not ok else run_query(ir, dataset)  # ← 기존 엔진
        store_full(result); stream_part(tu, result)           # → 프론트 즉시 차트
        msgs.append(tool_result(tu.id, compact(tu.name, result)))  # → 모델엔 compact
     continue
  else:                                                       # end_turn
     persist(new turns); break
```

### 6.3 스트리밍(SSE) 이벤트

`text-delta`(서술 토큰) · `tool-start`(예: "screen 실행 중…") · `tool-result`(프론트가 즉시 차트 mount) ·
`done`. → §Q2 "시나리오 A·B가 순차적으로 나타나는" UX 실현.

### 6.4 재사용 vs net-new

- **재사용**: 시스템 프롬프트 조립(capabilities/catalog/idioms)·프롬프트 캐싱·`validate_strategy`+repair·
  `run_query`·viz 컴포넌트·StrategyIR 스키마·`createStrategy`(저장).
- **net-new**: `Conversation`/`Message` 테이블 · agent 루프 · `compact_summary`/`assemble_ir` 어댑터 ·
  스트리밍(SSE) · 챗 UI · 세션 예산 가드 · `inspect` 엔진 확장(§8).

---

## 7. 프론트엔드

### 7.1 챗 UI (net-new)

- 스레드(메시지 리스트) + 입력창(현 단일 `nlText` textarea 대체) + 영속 대화 목록 사이드.
- **메시지-파츠 렌더러**: `assistant` 메시지의 `parts[]`를 차례로 렌더 — text 파츠는 산문,
  `tool_result` 파츠는 **인라인 차트**(transcript 안에 mount).
- 스트리밍 소비(SSE) → 점진 렌더.

### 7.2 시각화 재사용 (D4)

`ResultCharts.tsx`의 10종 컴포넌트는 **폼 의존 0**이라 그대로 재사용. 현재 `IrBuilder.tsx`의
`ResultPanel`(결과종류→컴포넌트 라우팅 switch)을 **공유 모듈로 들어올려** 메시지 파츠 렌더러가 사용.
신규 `LineChart`(inspect용)만 추가.

### 7.3 노코드 제거 대상 (P4)

- `SentenceTree.tsx`(264줄, 문장형 블록) — 컨트롤드 컴포넌트, 소비처 **2곳**(`IrBuilder`·`MultiSymbolPicker`).
- `IrBuilder.tsx`의 `buildStrategy()`/`hydrate()` 폼-덮어쓰기 기계(~60 setter·~70 state)와 폼 JSX.
- `MultiSymbolPicker`의 screener 문장 에디터 처리(SentenceTree 2번째 소비처).
- **유지**: `IrNode`/catalog 타입(IR 계약), api 레이어(`compileIr`→agent로 대체되나 `runIrStrategy`/저장은 유지).

### 7.4 라우팅·인바운드 링크 정리

`/lab` = 챗봇으로 교체. 인바운드 링크 4곳(`Dashboard`·`Strategies` 빈상태·`StrategyDetail`의
`?edit=<id>` 딥링크·CTA) 점검. **`?edit` 딥링크**(저장 전략을 폼에 로드)는 챗봇에서 "이 전략 불러와
이어서 논의" 진입점으로 재해석 필요. **`포트폴리오`·`개별종목분석`(StockDashboard) nav 항목은 유지**
(§2 유지 결정) — `/lab` 항목만 챗봇으로 바뀐다.

### 7.5 담당 경계 (협업 — CLAUDE.md §2.6·2.7)

`describe`(단일 360·포트폴리오 진단) 엔진 갈래와 렌더 컴포넌트(`ReportCards`·`DiagnosisPanel`)는
**희제 담당**. 챗봇은 이를 **읽기전용으로 재사용**(러너 호출 + 컴포넌트 mount)할 뿐, **희제의 전용
메뉴(포트폴리오·개별종목분석)는 제거·수정하지 않고 유지**한다(사용자 결정 2026-06-17). 교체 대상은
`/lab`뿐이라 겹침은 사실상 없음 — 단, 재사용 인터페이스(러너 시그니처·컴포넌트 props)를 *바꿔야*
하게 되면 그때 협의.

---

## 8. 엔진 확장 — `inspect` 동사 (D6)

현 `query: Literal["select","describe","relate","simulate"]`(`ir_engine/spec.py:219`)에 **`"inspect"` 추가.**

- **러너 `run_inspect(strategy, dataset)`**: `inspect` 스펙(symbols·columns·window)으로 `dataset[sym][col]`
  슬라이스 → 시계열 반환. 데이터 배관은 이미 존재(컬럼이 dataset에 있음) → 구현 거의 trivial.
- **목표주가 흐름** = `inspect(symbol, ["consensus_target"], window)`. 컨센서스는 PR#149로 prod에 PIT
  컬럼 적재됨(`dataset[종목]["consensus_target"]`이 일별 시계열).
- **결정적 core 불변식 유지**: 네트워크·env·시계 의존 0. dataset 슬라이스만(라이브 데이터 아님).
- **가드(question_layer_spec "금지 enum" 규율)**: `inspect`를 `capability_spec()`에 노출 +
  `test_capability_coverage` 통과 + 골든은 additive(기존 4동사 byte-identical 보존).
- **컨센서스 facet**: `describe` 단일리포트에 컨센서스 스냅샷(목표가·괴리율·수정폭) 추가도 병행 가능
  (희제 갈래 — 협의). "흐름"(시계열)은 `inspect`, "현재값"(스냅샷)은 `describe`.

---

## 9. 비용·안전 가드

- **세션 예산**: 멀티턴은 턴당 LLM 라운드 다수 → 인당/세션 토큰·호출 상한(기존 `CompileLog` 카운팅
  패턴 확장). env 조정(`QP_CHAT_*`).
- **숫자 규율(D5)**: 서술 산문은 생성형이나 수치·심볼은 tool_result에서만. 메타인지가 정확해야
  못 하는 건 모델이 "아직 못 함"이라 정직히(지어내기 금지).
- **프리빌트 viz(D4)**: 임의 코드 생성 없음.
- **draft-only**: 머니패스(모의/라이브)는 자동매매로 격리. 저장은 무자금 draft.
- **프롬프트 캐싱**: 큰 시스템 프롬프트(capabilities+catalog+idioms) 캐시 유지(기존 패턴).

---

## 10. 단계 분해 (P0~P4)

> IR 수렴 선례 답습: **새 경로 구축·검증 → 옛 노코드 제거(마지막).** 각 단계 독립 검증 가능.
> 아래는 *의존성* 기준 분해 — **실제 구현 순서는 효율·효과 최적으로 재배열**(사용자 결정 2026-06-17,
> writing-plans에서 확정). 불변 제약: 노코드 제거(P4)는 챗봇이 기능을 완전 흡수해 검증된 *뒤*에만.

| 단계 | 범위 | 검증 신호 |
|---|---|---|
| **P0 골격** | agent 루프 + 4 분석도구(screen/describe/relate/simulate) + 대화 영속 + **텍스트 전용** 챗(compact를 텍스트로) | "질문→실행→해석" 1턴 왕복(텍스트). 루프·persistence 단위테스트. golden byte-identical |
| **P1 인라인 viz + 스트리밍** | 메시지-파츠 렌더러 + ResultPanel 공유모듈화 + SSE | 결과가 transcript 안 차트로 렌더. 스트리밍 점진 표시 |
| **P2 다중도구·비교·저장** | 한 턴 N도구(병렬/순차) + 비교 서술 + `save_strategy` | 시나리오 A·B 순차 렌더+비교. draft 저장→자동매매에서 보임 |
| **P3 inspect + 컨센 facet** | `inspect` 동사(엔진) + `LineChart` + 컨센서스 노출 | "목표주가 흐름" 라인차트. capability_coverage·골든 보존 |
| **P4 노코드 제거** | SentenceTree·폼 기계 제거, `/lab` 완전 챗봇화, 인바운드 링크 정리 | 폼 경로 제거 후 회귀 0. `?edit` 재해석 동작 |

각 단계는 **4계층 호환 점검**(엔진·데이터·NL·웹): 빈 계층은 "미배선" 명시.

---

## 11. 비목표 (Non-goals / YAGNI)

- 임의 커스텀 시각화 *생성*(프리빌트로 충분 — D4).
- 챗 내 모의/라이브 *실행*(자동매매가 담당 — §2).
- 알림/모니터링(범위 밖).
- 다단 컨텍스트 압축·RAG-over-history(측정된 필요 전엔 금지 — §5.2).
- 손유지 "질문→분석" 매핑표(D1).

---

## 12. 미해결·리스크

- **모델 비용/지연**: Sonnet 멀티턴 + 무거운 백테스트 N회 → 세션 예산·캐싱으로 완화하나 실측 필요.
- **스트리밍 인프라**: 현 스택 스트리밍 0 → SSE 신규(P1의 실질 엔지니어링 비용).
- **긴 대화 컨텍스트**: P0 compact로 충분 가정 — 실사용 길이 측정 후 윈도우/요약 정책 확정.
- **담당 경계(희제)**: 해소됨 — 챗봇은 describe 러너·컴포넌트만 읽기전용 재사용, 희제 전용 메뉴 유지(§7.5). 재사용 인터페이스 변경 시에만 협의.
- **stale 작업트리**: 구현은 *최신 origin/main* 기준 `feat/chat-lab` 브랜치에서(platform/는 stale).
- **`?edit` 딥링크 대체**: 저장 전략을 챗으로 불러오는 UX 미확정(P4).

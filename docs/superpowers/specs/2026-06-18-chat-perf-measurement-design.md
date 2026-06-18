# 챗봇 성능 측정·개선 환경 — 설계 스펙

- **날짜:** 2026-06-18
- **상태:** 스펙 리뷰 대기
- **범위 단위:** 단일 구현 플랜 (측정+진단 MVP)
- **관련:** token-opt ②(`_log_usage`, PR#159) · NL컴파일러 measurement flywheel(`CompileLog` + `/admin/compile-stats`) · 4계층 호환 계약 · 데이터갭 분석

---

## 1. 목표·맥락

전략 연구소 챗봇(멀티턴 tool-use agent)의 성능을 **지속적으로 측정·진단·개선**할 수 있는 환경을 만든다. 측정 대상:

- **토큰 소모량** — turn별 in/out/cache
- **답변 소요시간** — turn 전체 latency + TTFT(첫 토큰까지)
- **답변 정확도** — Claude Code가 대화 히스토리를 읽고 4축 루브릭으로 채점
- **커버리지·준비도** — 봇이 *못 하는* 질문 = 수요/로드맵 신호

근본 원인을 분석하고 적확한 수정을 적용·검증하는 루프까지 포함한다.

**핵심 제약:** 진단·채점은 **별도 LLM API를 쓰지 않는다.** Claude Code(개발자)가 `railway run` CLI로 프로덕션 데이터를 끌어와 직접 분석·채점한다. 인프로덕트 LLM 심판/분석 API를 만들지 않는다(비용·복잡도 회피).

---

## 2. 범위

**In (v1 MVP):**
1. turn별 지표 적재 — `ChatTurnMetric` 테이블
2. agent 루프 계측 — 타이밍 + usage 누적
3. `python -m app.chat_analytics` CLI — `stats`, `transcripts [--suspect]`
4. 정확도 루브릭 doc — 4축 + 계층형 역량 인벤토리 + 커버리지 갭 2-facet 태깅
5. 진단→수정→검증 런북 doc — 품질 루프 + 로드맵 루프

**Out (v1 제외, YAGNI):**
- 과거 질문 리플레이/A-B (토큰 비용·범위 큼)
- 골든 대화셋 (별도 이니셔티브)
- 웹 운영진 대시보드 (CLI로 충분)
- 인프로덕트 LLM 심판/분석 (제약 위반)

---

## 3. 아키텍처 개요

```
[유저 대화] → stream_chat_turn (계측: 타이밍 + usage)
                 │
                 ├─→ Message          (기존: 내용 full payload)     ← 정확도 채점 원천
                 └─→ ChatTurnMetric   (신규: turn별 숫자 지표)       ← 정량 진단 원천
                          │
   railway run python -m app.chat_analytics
                          │
            ┌─────────────┴──────────────┐
       stats (집계 정량)         transcripts (가독 트랜스크립트)
            │                            │
            └──────→ Claude Code 진단 ←───┘
                          │
              루브릭(채점) + 런북(수정→검증)
```

지표(숫자)와 내용(텍스트)은 분리한다: `ChatTurnMetric`=숫자, `Message`=내용. 채점은 둘을 조인한 트랜스크립트로 한다.

---

## 4. 컴포넌트

### 4.1 데이터 모델 — `ChatTurnMetric` (server/app/models.py)

한 user 턴(질문→최종 답변)당 1행. `CompileLog` 패턴 미러. additive 테이블(startup `create_all` 자동 생성).

| 필드 | 타입 | 의미 |
|---|---|---|
| `id` | int PK | |
| `conversation_id` | int FK | 대화 |
| `user_id` | int FK (비정규화) | per-user 집계 |
| `created_at` | datetime(UTC) | KST 일경계 집계용 |
| `latency_ms` | int | 턴 전체 wall-clock |
| `ttft_ms` | int \| null | 첫 델타까지(스트리밍 체감); 도구-only 턴은 null 가능 |
| `input_tokens` | int | 턴 내 라운드 합 |
| `output_tokens` | int | 〃 |
| `cache_read_tokens` | int | 〃 |
| `cache_write_tokens` | int | 〃 |
| `n_rounds` | int | 도구 라운드 수 |
| `n_tool_calls` | int | 총 도구 호출 수 |
| `tool_names` | list (JSON) | 호출된 도구명 |
| `model` | str | 사용 모델 |
| `stop_reason` | str \| null | 마지막 라운드 stop_reason |
| `ok` | bool | 턴 정상 종료 여부(에러=false) |

내용(질문·답변 텍스트)은 **미저장** — Message가 단일 진실원천(중복 회피).

**근거:** 토큰은 현재 휘발성 Railway 로그에만 있고 지연은 아예 측정 안 됨. 지속·집계·추세를 위해 적재가 필요하다. 로그-온리 대안은 휘발·지연 부재·집계 난해로 기각.

### 4.2 캡처 — `stream_chat_turn` 계측 (server/app/chat/agent.py)

- 턴 시작 시 `t0 = perf_counter()`. 첫 `delta` yield 시 `ttft` 기록.
- 라운드마다 usage 누적(in/out/cache_read/cache_write 합), `n_rounds`·`n_tool_calls`·`tool_names` 수집, `model`·`stop_reason` 기록.
- 턴 종료(정상 또는 except) 시 `ChatTurnMetric` 1행 commit. **에러 턴도 `ok=false`로 기록**(고아 없음).
- 기존 `_log_usage`(라운드별 로그)는 저비용이라 유지하고, 이와 별개로 턴별 `ChatTurnMetric`을 적재한다(다른 granularity: 라운드 로그 vs 턴 집계). 변경 표면 = agent.py + models.py.
- **격리:** 지표 적재는 영속과 같은 session. 단, 지표 write 실패가 대화 응답을 깨지 않도록 가드(DB 일시오류 시 지표 누락은 허용, 대화는 보존 — 이는 외부 시스템 한계라 fallback 정당, 주석 명시).

### 4.3 내보내기 — `python -m app.chat_analytics` (server/app/chat_analytics.py, 신규)

`app.manage` 패턴(argparse + `__main__`). `railway run`으로 prod Neon 조회. `from app.db import engine; Session(engine)`.

**`stats [--days N]`** — 정량 집계(stdout 표; `--json` 옵션):
- 턴 수, 유저 수
- 토큰 분위수(p50/p90/max): input·output·cache_read; 캐시 적중률 = cache_read / (input + cache_read)
- 지연 분위수: latency_ms · ttft_ms
- 도구 사용 히스토그램(tool_names), 라운드 분포(n_rounds)
- 에러율(ok=false 비율)
- 일자 추세(--days 윈도)

**`transcripts [--days N] [--limit M] [--conv ID] [--suspect]`** — 정확도 채점용 가독 트랜스크립트(stdout/파일):
- 대화별·턴별: `[유저] 질문` → `[도구] name(input)` → `[결과] full payload` → `[봇] 답변`, 각 턴 지표(토큰·지연·라운드) 인라인.
- **full 도구결과 포함** → "답변 수치가 실제 데이터에 근거했나"를 직접 대조 가능(parts에 full payload 존재).
- `--suspect`: 미답변 후보 **무비용 휴리스틱 트리아지**(별도 API 0) — 다음 표층 신호 중 하나라도 플래그: ① `n_tool_calls==0`(도구 없이 답함) ② 답변에 회피표현("할 수 없"·"지원하지 않"·"확인이 어렵") ③ 직후 유저 부정/재질문("아니"·"그게 아니라"). *의미 분류가 아닌 표층 신호 — 판정이 아니라 needle 우선순위만; 최종 채점은 Claude Code.*

### 4.4 정확도 루브릭 (docs/chat-perf/accuracy-rubric.md)

Claude Code가 트랜스크립트를 읽을 때 적용하는 채점 기준.

**계층형 역량 인벤토리(기준틀)** — 갭 계층 태깅의 근거(4계층 계약과 정렬):
- **① 노출 도구:** screen · simulate · save_strategy · describe · inspect (각 커버 범위)
- **② 가용 데이터:** KR/US OHLCV · 펀더멘털 · 섹터 분류 등 (데이터갭 분석 참조; 미수급: 뉴스·추정치·플로우·인트라데이 등)
- **③ 엔진 분석로직:** IR verbs (select / describe / relate / simulate / extremize …)

**4축 채점** (축별 pass / partial / fail + 한 줄 사유):
1. **의도 이해·가이드** (최우선): 모호해도 진짜 의도 파악? 생산적 안내(협의 = 선택지·추천, 더 나은 프레이밍 제안)? 단순 직역 실행이 아닌가?
2. **도구·근거 정확성:** 의도에 맞는 도구 선택? 답변 수치가 도구결과에 근거(날조 없음)?
3. **질문 완결성:** 끝까지 답했나 / 적절히 되물었나? 빠뜨린 맥락은?
4. **준비도·커버리지:** 역량 밖 질문인가? — (a) **처리:** graceful(정직한 한계 고지 + 우회 제안) vs bad(환각·자신있게 틀림·무시); (b) **2-facet 태깅** ↓

**커버리지 갭 2-facet 태깅:**

(a) **증상 태그** (질문에서 추출): `history-reference` · `data-metadata` · `sector/qualitative-filter` · `analysis-type-X` …

(b) **근본원인 계층 태그** (수정이 어느 계층으로 가는지 라우팅):

| 태그 | 의미 | 수정 트랙 |
|---|---|---|
| `missing-tool` | 엔진·데이터엔 있으나 챗봇 도구로 미노출 | 도구 배선 (최저비용) |
| `missing-data` | 기반 데이터 미수급 | 데이터엔진 수급 (외부·고비용) |
| `missing-logic` | IR/엔진 분석 프리미티브 부재 | 엔진 신설 |
| `missing-metadata-access` | 데이터는 있으나 메타(as-of·유니버스·출처·커버리지) 질의 수단 없음 | 도구/프롬프트 |
| `history-context` | 과거 대화·결과 참조 실패(컴팩트·리텐션) | 아키텍처 |
| `out-of-scope` | 설계상 미지원(개인자문·실행) | 올바른 거절이면 OK |

태그 집계 = 개선·로드맵 우선순위(compile-stats의 `top_fail_rules`와 동형).

### 4.5 진단→수정→검증 런북 (docs/chat-perf/diagnosis-runbook.md)

**입력:** `stats`(정량) + `transcripts` 채점(정성·태그 집계).

**두 루프:**
- **품질 루프** (축 1~3 + 토큰/지연): 증상 → 근본원인(프롬프트 갭 · 도구 스키마 · 컨텍스트 비대 · 모델 티어) → 타깃 수정 → **검증**(unit test + `stats` 토큰/지연 델타 + 샘플 재채점; 정확도 회귀는 unit test로 고정).
- **로드맵 루프** (축 4): 미충족 의도의 계층 태그 집계 → 계층별 라우팅(missing-tool=빠른 배선 / missing-data=수급 / missing-logic=엔진 / history-context=아키텍처) → 우선순위 = 빈도 × 가치.

**예시 진단:**
- p90 latency 높음 + n_rounds 많음 → 도구 과호출/프롬프트 비효율.
- cache_read 0 지속 → 히스토리 캐싱 미작동(PR#159 검증과 직접 연결).
- "저평가 반도체주" 반복 실패 + tag=`missing-tool`(섹터필터) → 섹터필터 배선(기지 갭).
- "아까 그 종목 다시" 실패 + tag=`history-context` → 컴팩트(full→compact) 설계 재검토.

---

## 5. 환경 자체 검증

- **캡처:** 로컬 SQLite 시드 대화로 `ChatTurnMetric` 적재 단위 테스트(타이밍·usage 누적·에러 턴 ok=false).
- **CLI:** 시드 데이터로 `stats`·`transcripts` 출력 단위 테스트(분위수·트랜스크립트 포맷·--suspect 휴리스틱).
- **통합:** `railway run python -m app.chat_analytics stats` 1회 prod 스모크.
- **방식:** TDD(RED→GREEN).

---

## 6. 파일 매니페스트

| 파일 | 변경 |
|---|---|
| `server/app/models.py` | `ChatTurnMetric` 추가 |
| `server/app/chat/agent.py` | `stream_chat_turn` 계측 + 적재 |
| `server/app/chat_analytics.py` | CLI (신규) |
| `server/tests/test_chat_analytics.py` | 캡처·CLI 테스트 (신규) |
| `docs/chat-perf/accuracy-rubric.md` | 루브릭 (신규) |
| `docs/chat-perf/diagnosis-runbook.md` | 런북 (신규) |
| `docs/superpowers/specs/2026-06-18-chat-perf-measurement-design.md` | 이 스펙 |

---

## 7. 리스크·확인사항

- **create_all:** startup에서 신규 테이블을 생성하는지 구현 시 확인(Alembic 부재 → 기존 Conversation/Message/CompileLog도 create_all로 생성됨을 근거로 가정).
- **railway run:** QP_DB_URL(Neon)을 주입해 로컬 스크립트가 prod를 조회하는지 1회 확인.
- **지표 격리:** write 실패가 대화 응답을 깨지 않도록 가드(지표 누락 허용, 대화 보존).
- **프라이버시:** 트랜스크립트에 유저 전략 텍스트 포함 — 서버는 안전정보만 보관(자격증명 없음, 보안모델상 OK).
- **적재 볼륨:** 일일 사용량 캡(5/20) × 베타 유저 → 미미(Neon egress 무관).

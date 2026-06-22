# 챗봇 LLM 무료 평가 하니스

전략 연구소 챗봇에서 **LLM이 개입하는 부분**(오케스트레이터 라우팅·NL→IR 컴파일·멀티턴·뉴스
다이제스트·답변 품질)을 **Anthropic API 비용 0**으로 전수 테스트·평가한다. LLM 백엔드를
Claude Code 헤드리스(`claude -p`, 본인 Pro/Max 구독)로 갈아끼워 *실제* `run_chat_turn` 루프를
그대로 돌린다.

> 결정적 코어(IR→엔진→결과→요약/엑셀)의 $0 테스트는 별개로 `scripts/analysis_diag.py`·
> `core/tests/test_capability_contract.py`가 담당한다. 이 하니스는 **LLM 부분 전용**이다.
> 설계 상세: [docs/REDESIGN/chat-llm-eval-harness.md](../../docs/REDESIGN/chat-llm-eval-harness.md)

## 1. 1회 셋업 (각 개발자)

```bash
claude setup-token                 # 본인 Pro/Max 구독으로 장기 토큰 발급(브라우저 OAuth)
# 출력된 토큰을 환경변수 또는 파일에 둔다(둘 중 하나):
setx CLAUDE_CODE_OAUTH_TOKEN "<토큰>"          # 환경변수(권장·세션 재시작 후 적용)
#   또는
notepad %USERPROFILE%\.claude\oauth_token.txt  # 파일(하니스가 폴백으로 읽음)
```

- **Pro/Max 구독 필수**(`setup-token`은 구독 전용). 사용량은 본인 구독 쿼터에 잡힘(**API $0**).
- ⚠️ **토큰은 절대 커밋 금지** — env 변수 또는 `~/.claude/oauth_token.txt`(repo 밖)에만. `.gitignore`가 `*oauth_token*` 차단.

## 2. 실행

```bash
python scripts/chat_eval.py                  # 전체: run + grade → chat_eval_out/REPORT.md
python scripts/chat_eval.py run   --only correlation   # 부분 코퍼스만 실행(쿼터 절약)
python scripts/chat_eval.py grade --no-judge           # 결정적 채점만(LLM-심판 생략)
python scripts/chat_eval.py grade            # 트랜스크립트 재채점(재실행 0·루브릭 튜닝)
python scripts/chat_eval.py --smoke "삼성전자 어때?"    # 단일 쿼리 스모크(코퍼스 우회)
```

run(LLM 호출)과 grade(채점)는 분리 — 트랜스크립트는 `chat_eval_out/<name>.json`에 캐시되어
`grade`는 재실행 없이 무한정 재채점 가능(루브릭/기대를 코퍼스에서 다시 읽음).

## 3. 무엇을 평가하나 — "챗봇이 자기 계약을 지켰는가"

기준은 챗봇 시스템 프롬프트의 계약([prompt.py](../../server/app/chat/prompt.py))에서 도출.

**Tier 1 — 결정적(pass/fail)**: D1 라우팅(기대 도구 호출) · D2 NL→IR 구조 · D5 수렴 · D6 거부(no_tools).
보조 플래그: D3 숫자 무환각(휴리스틱) · D4 중복 재실행 · **D7 시각화 중복**(리치 렌더러 shape인데
답변에 마크다운 표).

**Tier 2 — claude -p 심판(0~5)**: J1 해석충실 · J2 응답성 · J3 무환각 · J4 협의 · J5 쉬운말요약 ·
J6 정직 · **J7 시각화 중복/가독성**(차트로 이미 뜬 데이터를 텍스트 표로 반복하지 않는가).

코퍼스: [corpus.py](corpus.py) — describe·screen·simulate(백테/연도별/sweep/extremize/국면/회귀/이벤트/
포트진단) · inspect · correlation · prescribe · breadth · research_news · adjust · save · 협의 · 거부 · 무환각.

## 4. 시각화 확인 (3층)

| 보는 곳 | 차트 | 용도 |
|---|---|---|
| Claude Code 터미널 | ✗ | 디버깅 |
| `REPORT.md` 자동 채점 | ✗(데이터 검증) | 헤드리스·반복 |
| **로컬 웹앱** | ✅ 실제 렌더 | 사람 눈 검수 |

로컬 웹앱: 하니스가 대화를 로컬 SQLite(`server/data.db`)에 영속 → `cd server; uvicorn app.main:app`
+ `cd web; bun run dev` → 전략연구소 채팅에서 테스트 유저(`chat-eval@local`)로 그 대화들을 실제
차트(`ChatResultView`)로 본다. 새 렌더링 코드 0(기존 영속+렌더러 재사용).

## 5. 한계

- **합성 데이터**: 엔진이 결과를 내도록 합성 OHLCV를 `_load_dataset`에 주입한다(LLM 거동 평가엔
  데이터 내용 realism 무관). 실데이터 충실도는 프로덕션에서.
- **호출당 ~40~50k 토큰** 오버헤드(`claude -p`가 CC 시스템·CLAUDE.md·스킬 로드) — 구독 쿼터 소모(API $0).
  풀런(~20 시나리오) ≈ 수 M 토큰. 개발 중엔 `--only`/`--no-judge`.
- **프롬프트형 결정**: `claude -p`는 native tool-use 대신 구조화 출력으로 결정을 흉내 → 프롬프트·
  도구설명·NL→IR·라우팅 버그엔 탁월하나 프로덕션 모델과 byte-identical은 아니다. 출시 직전엔
  실 API 스모크 5~10건으로 최종 확인 권장.

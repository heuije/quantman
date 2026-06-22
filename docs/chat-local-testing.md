# 챗봇을 로컬에서 API 비용 0으로 테스트·사용하기 (조대표·희제 참고)

전략 연구소 챗봇의 **LLM 부분**(오케스트레이터 라우팅·NL→IR 컴파일·멀티턴·뉴스·답변)을
**각자 본인 Claude Pro/Max 구독**으로 돌려 **Anthropic API 비용 0**으로 쓰는 두 방법. 실제
`run_chat_turn` 루프를 그대로 실행하고 LLM 백엔드만 `claude -p`(헤드리스)로 교체한다.

> 결정적 코어(IR→엔진→결과/엑셀)의 $0 테스트는 별개로 `scripts/analysis_diag.py` ·
> `core/tests/test_capability_contract.py`가 담당한다. 이 문서는 **LLM 부분** 전용.

## 0. 전제 (각 개발자 1회)

- 본인 **Claude Pro/Max 구독** + **Claude Code** 설치
- `claude setup-token` → 토큰을 `CLAUDE_CODE_OAUTH_TOKEN` 환경변수 **또는**
  `~/.claude/oauth_token.txt`(repo 밖). `.gitignore`가 `*oauth_token*`를 차단 — **커밋 금지**.
- 사용량은 **본인 구독 쿼터**에 잡힘(API $0). 인터랙티브 Claude 사용과 쿼터 공유(5h·주간 캡).

## 모드 A — 평가 하니스 (자동 평가·배치·회귀)

고정 코퍼스나 임의 쿼리를 구독으로 돌려 **채점·트랜스크립트**를 남긴다. 차트는 로컬 웹에서 검수.

```bash
python scripts/chat_eval.py                          # 전체 run+grade → chat_eval_out/REPORT.md
python scripts/chat_eval.py --smoke "삼성전자 어때?"    # 임의 단일 쿼리($0·로컬 DB 저장)
python scripts/chat_eval.py run --only correlation       # 일부 코퍼스만(쿼터 절약)
python scripts/chat_eval.py grade --no-judge             # 결정적 채점만(LLM 심판 생략)
```

차트 눈검수: 하니스가 대화를 로컬 SQLite에 저장 → 로컬 웹앱(아래 모드 B의 2·3단계)에서 테스트
유저 `chat-eval@local` 대화를 실제 차트로 본다.
상세: [scripts/chat_eval/README.md](../scripts/chat_eval/README.md) ·
설계: [docs/REDESIGN/chat-llm-eval-harness.md](REDESIGN/chat-llm-eval-harness.md)

## 모드 B — 라이브 로컬 챗 (웹 채팅창에 직접)

서버를 **로컬 구독 모드**(env `QP_CHAT_LOCAL_SUBSCRIPTION=1`)로 띄우면, 웹 채팅창에 직접 친
질문이 본인 구독으로 답변되고 차트가 라이브로 렌더된다($0).

```powershell
# 1) 서버 (PowerShell) — Claude Code CLI 설치 필요
cd server
$env:QP_CHAT_LOCAL_SUBSCRIPTION = "1"
$env:CLAUDE_CODE_OAUTH_TOKEN    = "<발급 토큰>"     # 또는 ~/.claude/oauth_token.txt
uvicorn app.main:app                                # localhost:8000

# 2) 웹 (기본 API base가 localhost:8000 → 추가 설정 불요)
cd web; bun run dev                                 # localhost:5173

# 3) 브라우저 5173 → 구글 로그인 → 전략연구소 채팅 → 직접 질문
#    → 본인 구독으로 답변($0) + 라이브 차트 렌더
```

- **API 키 불필요** — 서버 가드(`_require_chat_llm`)가 구독 모드를 "LLM 구성됨"으로 인정.
- 단일 seam: `server/app/chat/agent.py`의 `_default_chat_client` — 플래그 set이면 오케스트레이터는
  `ClaudeCodeBackend()`, NL→IR 컴파일·뉴스 다이제스트는 `anthropic.Anthropic` 몽키패치로 같은 구독 경유.
- 셸별 env 설정: CMD `set QP_CHAT_LOCAL_SUBSCRIPTION=1` · bash `export QP_CHAT_LOCAL_SUBSCRIPTION=1`.

## 어느 걸 언제

| | 모드 A (하니스) | 모드 B (라이브) |
|---|---|---|
| 용도 | 자동 평가·회귀·배치·채점 | 인터랙티브 손맛·UX/차트 눈검수 |
| 입력 | 코퍼스 / `--smoke "쿼리"` | 웹 채팅창에 직접 타이핑 |
| 출력 | `REPORT.md` + 로컬 웹 뷰 | 웹에서 즉시 답변 + 차트 |

## 한계·주의

- ⚠️ **로컬 개발 전용** — `QP_CHAT_LOCAL_SUBSCRIPTION`을 **배포 환경(Railway)에 설정 금지**.
  기본 off라 프로덕션은 항상 API 키 경로(동작 byte-identical).
- `claude -p` 서브프로세스라 **API보다 느림**(호출당 ~40–50k 토큰 오버헤드). 프롬프트형 결정이라
  **프로덕션 모델과 byte-identical 아님** → 출시 직전엔 실 API 스모크 5~10건으로 최종 확인.
- ToS: 본인 구독으로 *자기* 개발/테스트 = Claude Code 허용 범위(회색지대·메터링 정책 변동 가능).
  토큰 공유·타 제품에 구독 로그인 끼워팔기는 금지 — 각자 본인 구독 + 본인 `setup-token`.

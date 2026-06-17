# 협업 환경 핸드오프 — 세션간 · 공동작업자간 자동 협업

이 문서는 **두 종류의 자동 협업**을 설정·운영하는 단일 안내서다. 신규 작업자는 이 문서만
따라오면 된다.

- **Layer 1 — 공동작업자간** (조대표 ↔ 희제, 서로 다른 PC): **draft PR + git 훅**
- **Layer 2 — 세션간** (한 사람의 같은 PC 여러 Claude 세션): **공유 브리핑 로그 + 훅**

| | Layer 1 (공동작업자간) | Layer 2 (세션간) |
|---|---|---|
| 범위 | 두 개발자, **서로 다른 PC** | 한 사람, **같은 PC**의 여러 세션 |
| 채널 | GitHub draft PR | 로컬 브리핑 로그 파일 |
| 성격 | 지속·조회·리뷰 | 지속·작업 시작 시 pull |
| 진실원천 | PR 본문 + 프로젝트 `CLAUDE.md §2` | `~/.claude/session-briefings.jsonl` |
| 상태 | **이미 repo에 구현됨** (활성화만) | **템플릿 복사로 설치** (`docs/collab/`) |

---

## 배경 — 왜 필요한가

2명이 각자 여러 Claude 세션·worktree로 병렬 작업하면 **충돌·중복·유실**이 반복된다
(실제 사례: 유실 커밋 일주일 방치, main 작업트리 −212커밋 drift, 이미 머지된 변경의 로컬 중복).

핵심 원리: **git은 이미 쓰여진 코드만 본다.** 충돌·중복은 코드가 생기기 *전*, "무엇을 하기로
했다"와 "푸시했다" 사이의 공백에서 터진다. 그 공백을 **의도를 일찍 broadcast**하고 **절차를
훅으로 강제**해서 닫는다.

---

## Layer 1 — 공동작업자간 자동 협업 (이미 구현됨)

### 원리
**작업을 시작할 때 draft PR을 연다.** PR이 곧 "내가 무엇을 왜 하는가"의 broadcast이고,
git이 건드릴 파일을 자동 계산해주며, 지속·조회·리뷰가 된다. 다른 개발자는 세션을 열 때
열린 PR 목록을 자동으로 본다.

### 구성 요소 (모두 repo에 있음)
- **`.githooks/pre-push`** — `main` 직접 push 차단. 작업은 브랜치 + PR로만.
- **`.claude/settings.json` 의 SessionStart 훅** — 세션 열 때 `git fetch` + `gh pr list`로
  **열린 PR을 자동 표시**.
- **`CLAUDE.md §4` 의 "Git 협업 워크플로"** — 작업 절차 규칙.

### 1회 설정 (각 개발자, 각 PC에서 한 번)
1. 최신 main 받기: `git checkout main && git pull`
2. 훅 활성화: `git config core.hooksPath .githooks`
   - clone당 1회면 됨(`.git/config`에 저장 → 같은 clone의 새 worktree는 자동 상속).
3. GitHub 로그인: `gh auth login` (머신당 1회, `gh`가 없으면 먼저 설치)
4. Claude Code로 프로젝트를 열면 `.claude/settings.json` 훅 실행 **승인** (머신당 1회)

검증: `git config core.hooksPath` → `.githooks` / `gh auth status` → Logged in /
Claude 세션을 새로 열면 시작 시 열린 PR 목록이 뜸.

### 작업 리듬 (설정 후 영구 자동)
1. **작업 전:** 세션 열면 SessionStart 훅이 fetch + 열린 PR 표시. 내 작업과 겹치는 PR이
   있으면 시작 전 PR 댓글로 협의.
2. **시작 시 draft PR:** `git switch -c feat/내작업` 후 바로 draft PR을 연다(유실·중복 방지).
3. **작업 단위 브랜치 + main 직접 push 금지**(훅이 차단).
4. **짧게·자주 머지:** 브랜치는 오래 두면 발산한다. 매일 main 머지, merge 후 알림.
5. **희제 PR은 merge-commit으로 머지(squash 금지).** Vercel 웹 배포는 main 커밋의 *author*로 동작하는데,
   squash 머지는 커밋 author를 PR 저자(희제)로 찍어 → 희제가 Vercel 팀 미소속이라 **웹 자동배포가 거부**된다
   (Railway 서버는 author 무관·정상). GitHub "Create a merge commit" 또는 `gh pr merge <n> --merge`로 머지하면
   author=머지한 사람(조대표)이라 자동배포된다. 조대표 본인 PR은 squash여도 무방. (이유·수동 재배포 대안은
   프로젝트 `CLAUDE.md §2.5 배포 토폴로지`.)
6. **머지된 브랜치 재사용 금지 (충돌 재발 근본원인).** 머지 끝난 브랜치에 새 작업을 얹지 말 것 —
   새 작업 = 최신 main에서 **새 브랜치**: `git checkout main && git pull && git switch -c feat/<새이름>`.
   강제 장치: **pre-push 훅**이 머지된 브랜치 재push를 차단, **SessionStart 훅**(`.claude/branch_check.py`)이
   stale·머지된 브랜치에 경고, **GitHub**가 머지된 remote 브랜치를 자동삭제(재사용 미끼 제거).
   ⚠ 훅은 `git config core.hooksPath .githooks`가 돼 있어야 작동 — 각 clone에서 1회 확인.

### 경계 — CLAUDE.md §2 vs draft PR
- **구조적 핸드오프**(새 모듈·아키텍처·담당 경계) → 프로젝트 `CLAUDE.md §2` (오래 가는 사실)
- **진행 중 작업상태**(시시각각 바뀜) → **draft PR 본문**
- 진행상황을 `CLAUDE.md`에 적지 않는다(거기서 또 충돌난다).

---

## Layer 2 — 세션간 자동 협업 (한 PC, 본인 세션끼리)

> 한 사람이 같은 PC에서 여러 Claude 세션을 동시에 굴릴 때, 세션들이 서로 **무엇을 하는지/
> 어떻게 끝냈는지** 자동으로 알게 한다. **공동작업자간 협업이 아니다**(그건 Layer 1).

### 원리
inter-session 실시간 broadcast는 **휘발성**(그 순간 연결된 세션만 받음)이라 align엔 부족하다.
대신 **지속되는 브리핑 로그**에 각 세션이 기록하고, **`UserPromptSubmit` 훅**이 매 작업
시작(유저 쿼리) 시점에 **안 본 것만 catch-up해 context에 주입**한다. 각 세션(session_id)마다
'마지막으로 본 시각' 커서를 두고 그 이후 새 브리핑만 보여준다 → **같은 항목 반복 없음 +
나이 무관 절대 안 놓침**. 새 세션은 전체를 catch-up(브랜치별 최신 1건으로 자연 상한).
→ 늦게 연 세션도, 이미 닫힌 세션의 결과도 빠짐없이 catch-up 된다.

### 구성 요소 (`docs/collab/` 의 템플릿을 개인 `~/.claude/` 로 복사)
- **`brief.py`** — 쓰기. 작업 `start`/`done`을 로그에 한 줄 append.
- **`read_briefings.py`** — 읽기. 세션별 커서로 **안 본 브리핑만** catch-up 출력(`[진행중]`/`[완료]`).
  커서는 `~/.claude/briefing-cursors/<session_id>` 에 저장된다.
- **`settings.snippet.json`** — `UserPromptSubmit` 훅(개인 settings에 병합).
- **`personal-CLAUDE.snippet.md`** — 브리핑 프로토콜(개인 `~/.claude/CLAUDE.md`에 추가).
- 로그 파일: `~/.claude/session-briefings.jsonl` (자동 생성, machine-local, git 아님).

### 1회 설정 (각 개발자, 각 PC에서 한 번)
1. 스크립트 복사:
   - `docs/collab/brief.py` → `~/.claude/hooks/brief.py`
   - `docs/collab/read_briefings.py` → `~/.claude/hooks/read_briefings.py`
2. `docs/collab/settings.snippet.json` 의 `hooks` 블록을 개인 `~/.claude/settings.json` 에
   **병합**(덮어쓰기 아님). `command` 의 경로를 자기 절대경로로 교체.
3. `docs/collab/personal-CLAUDE.snippet.md` 의 프로토콜 블록을 개인 `~/.claude/CLAUDE.md` 에 추가.
4. Claude Code가 훅 실행 **승인**을 물으면 승인.

검증: 아래 두 줄로 더미 기록 후 새 세션을 열어 주입되는지 확인.
```
python ~/.claude/hooks/brief.py start --intent "테스트" --plan "확인" --files "x"
python ~/.claude/hooks/read_briefings.py     # 출력에 안 뜨면(자기 브랜치 제외 규칙) 다른 브랜치에서 확인
```

### 브리핑 프로토콜 (Claude가 따름)
- **작업 시작 시:** `brief.py start --intent --plan --files`
- **작업 완료 시:** `brief.py done --intent --plan --files --impl --outcome`
  (`--impl`=구현/결정 요지, `--outcome`=어떻게 끝났나: 머지/PR/보류/중단)
- **`--intent` 는 self-contained 핸드오프로 2~3문장.** 다른 세션이 사전 맥락 없이 읽고
  이해하도록: 무엇을·왜 + 배경(어느 시스템/계층·어떤 문제·어디로) + 범위 경계. 한 구절 금지.
  나머지 필드는 한 줄로 충분. (렌더링이 intent를 `의도:` 자체 줄에 wrap해 보여준다.)
- 매 프롬프트 시작 시 다른 세션 현황이 자동 주입됨(안 본 것만) → 겹치면 시작 전 고려.
- **상태 공유만 — 다른 세션에 명령하지 않는다.**

### 동작 예시
```
09:00  세션 A (feat/futures-sizing) 시작
  → brief.py start: 의도=선물 사이징 근본수정 / 파일=trader.py, live.py

09:30  세션 B (fix/preview-label) 첫 쿼리 → UserPromptSubmit 훅이 주입:
  === 다른 세션 작업 현황 (전체 catch-up) ===
    [진행중] [06-10 09:00] feat/futures-sizing
        의도: 선물 자동매매 사이징이 주식계좌 현금으로 계산돼 0계약이 나오는 버그를 고친다.
            선물계좌 가용증거금현금으로 사이징하고 유저 상한(margin_pct)을 두는 게 목표.
        파일: trader.py, live.py
  → B: "A가 trader.py·선물 사이징 수정 중. 내 preview_engine.py와 안 겹침 → 진행."
  (B의 다음 쿼리부터는 새 소식이 없으면 무출력 — 반복 안 함)

09:55  세션 A 완료
  → brief.py done: 구현=margin_pct 20% 기본·선물계좌 현금 / outcome=PR #59 머지

14:00  세션 C 새로 열림(A는 이미 닫힘) → 훅 주입(C는 처음이라 전체 catch-up):
  === 다른 세션 작업 현황 (전체 catch-up) ===
    [완료] [06-10 09:55] feat/futures-sizing: 선물 사이징 근본수정 -> PR #59 머지
           구현: margin_pct 20% 기본·선물계좌 가용현금
  → C: "선물 사이징은 #59에서 구현됨. 중복 대신 그 위에 얹겠습니다."
```
A가 닫혔어도, **나이 무관**(24h 넘어도) C가 안 본 것이면 catch-up되어 **"무엇이·어떻게
끝났는지"를 이어받아** 중복을 피한다(broadcast로는 불가능).

### 한계 (솔직히)
- **읽기는 결정론적(훅), 쓰기(start/done)는 Claude가 프로토콜대로** 호출(model-driven).
  Claude가 `done`을 빠뜨리면 그 작업은 `[진행중]`으로 남는다 — 치명적이진 않지만 완벽 보장은 아님.
  (Claude Code엔 "작업 단위 완료"를 잡는 결정론적 이벤트가 없어 `done`은 지시 기반이 최선.)
- **세션 커서는 session_id 기반.** 훅 stdin에 session_id가 없으면(드문 경우) `_default` 공유
  커서로 폴백한다 — 여러 세션이 한 커서를 공유해 catch-up이 거칠어질 수 있으나 동작은 한다.

---

## 두 층의 경계 / 승격 규칙

- **Layer 2(세션간)** = 같은 PC, 본인 세션끼리의 실시간 align. 휘발적 편의.
- **Layer 1(공동작업자간)** = 두 개발자 사이의 지속 기록. **진실원천.**
- **승격 규칙:** 다른 *개발자*가 알아야 할 내용은 Layer 2(브리핑 로그)에만 두지 말고
  **반드시 draft PR 본문 / `CLAUDE.md §2`로 올린다.** 브리핑 로그는 그 PC에만 있다.

---

## FAQ

- **매 세션·매 작업마다 명령을 실행해야 하나?** 아니다. 1회 설정 후 fetch·PR 표시·브리핑
  주입은 전부 **세션 열 때/쿼리 입력 시 자동**. 사람이 손으로 칠 명령은 없다(brief 기록은
  Claude가 프로토콜대로 함).
- **닫힌 세션에도 공유되나?** Layer 2 로그는 **지속**되므로 닫힌 세션의 결과도 **나이 무관**
  catch-up된다(세션이 아직 안 봤다면). inter-session 실시간 broadcast는 연결된 세션만 받아
  불가 — 그래서 지속 로그 + 세션 커서 방식을 택했다.
- **inter-session 플러그인이 필요한가?** 이 방식(로그+훅)은 **불필요**. 더 단순하고 지속된다.
- **`core.hooksPath` 는 worktree마다?** 아니다. `.git/config` 공유 → clone당 1회면 모든
  worktree에 적용.

---

## 신규 작업자 온보딩 체크리스트

초대하는 쪽:
- [ ] GitHub `MercKR/quantman` 협업자로 추가(Write). 릴리즈 맡으면 `quantman-releases`도.

새 작업자(각 PC에서):
- [ ] 도구 설치: git, `gh`, Claude Code, Python(+web면 bun)
- [ ] `gh repo clone MercKR/quantman && cd quantman`
- [ ] **Layer 1**: `git config core.hooksPath .githooks` / `gh auth login` / 훅 승인
- [ ] **Layer 2**: `brief.py`·`read_briefings.py` → `~/.claude/hooks/` 복사 /
      `settings.snippet.json` 병합(경로 교체) / `personal-CLAUDE.snippet.md` 를 `~/.claude/CLAUDE.md`에
- [ ] 검증: 새 세션 열어 열린 PR + (있으면) 다른 세션 브리핑이 뜨는지 확인
- [ ] 작업 시작 = `feat/`·`fix/` 브랜치 + draft PR. main 직접 push 금지.

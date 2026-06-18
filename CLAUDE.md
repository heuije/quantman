# Claude 작업 가이드 — 퀀트 자동매매 플랫폼

이 파일은 Claude Code가 이 저장소에서 작업할 때 따라야 할 **공유 맥락·규칙**을 모은다.
공동작업자(조대표·희제) align 장치이므로 **개괄만** 둔다 — 모듈 세부·진척은 `docs/modules/`,
지금 누가 뭐하나는 draft PR·브리핑이 담당한다(§6 편집 기준).

---

## 1. 프로젝트 개요

- 한국 주식 자동매매 SaaS. 초중급 퀀트 트레이더 대상.
- 핵심 차별점: **문장형 빈칸 조건 설정** — 코드 없이 자연어 흐름으로 전략을 만든다.
- 사용자 흐름: 웹앱에서 전략 수립 → 모의/실전 모드 선택 → 사용자 PC의 로컬앱이 KIS API로 자동 실행.

## 2. 아키텍처 한눈에

```
(repo root = MercKR/quantman)
├── core/    quant_core — pure Python 엔진 (pip install -e). 데이터 정의·수집·백테스트·인사이트 전부 여기
├── server/  FastAPI — Railway 호스팅, Neon Postgres. 서빙·cron·동기화·preview·NL 컴파일
├── web/     React+TS+Vite — Vercel 호스팅. 노코드 빌더 + 결과 시각화
├── local/   Python Tkinter 데스크탑 — 사용자 PC. KIS REST+WS, PyInstaller 번들. 자동매매 실행
├── docs/    설계·계획·모듈 학습원장(docs/modules/)·KIS API KB(docs/kis-api/)·인시던트(docs/incidents/)
└── tests/   루트 통합/골든 테스트
```

**핵심 원리:** 엔진은 폴더와 1:1이 아니라 **layer로 나뉜다** — 로직은 `core/`에 정의되고
`server/`(서빙)·`web/`(UI)·`local/`(실행)에 배선된다. 모듈별 기능·구조·작업이력은 §3의 문서에.

**배포 토폴로지:**
- **웹앱:** Vercel (production + preview) — `origin/main` push → 자동 deploy
- **서버:** Railway (Neon Postgres) — `origin/main` push → 자동 deploy
- **로컬앱:** PyInstaller zip → `MercKR/quantman-releases` (public repo) GitHub Release
- **⚠ Vercel 웹 배포는 main 커밋의 *author* 기준.** author가 Vercel 팀(FLOO's projects) 미소속이면 거부됨 →
  **희제 PR을 squash 머지하면 커밋 author=희제(팀 미소속)라 웹 배포가 거부된다**(Railway 서버는 author 무관·정상).
  대응: 희제 PR은 **merge-commit**으로 머지(§4 규칙). 막혔을 때 수동 재배포(merckr 권한, **반드시 레포 루트에서**):
  `VERCEL_ORG_ID=team_3rW5zrcgysV62xsllpfmKKtE VERCEL_PROJECT_ID=prj_oaSfa9IftnZzKtk6j0xbJPlZQOPs vercel --prod --yes`
  (quantman 프로젝트 rootDirectory=web — web/ 안에서 실행하면 web/web 에러). **Vercel 프로젝트는 공개 웹 `quantman`만 사용** —
  옛 `platform` 프로젝트(루트 링크·커스텀 도메인 없음·401 잠금)는 미사용이라 삭제 권장.

> ⚠ 열려 있는 브라우저 탭은 reload 전까지 **이전 JS 번들**을 서빙한다. 배포 후 확인은 `location.reload()` 먼저.

## 3. 모듈 담당 맵 + 세부 문서

이 저장소는 **조대표·희제 공동 관리**다. 모듈 세부(기능·폴더·구동 워크플로)·작업계획 이력·교훈은
**각 모듈 문서(학습 원장)**에 있다 — **그 모듈을 작업하기 전 해당 문서의 `📌 교훈·함정`을 먼저 읽는다.**

| 모듈 | 담당 | 핵심 위치 | 세부 문서 (학습 원장) |
|---|---|---|---|
| 데이터 엔진 | **조대표** | `core/quant_core/data/` · server 캐시·cron | [docs/modules/data-engine.md](docs/modules/data-engine.md) |
| 인사이트 엔진 (코어·스크리닝·회귀·백테스트) | **조대표** | `core/quant_core/ir_engine/` · `routers/ir*.py` | [docs/modules/insight-engine.md](docs/modules/insight-engine.md) |
| 자동매매 엔진 | **조대표** | `local/localapp/` · `routers/{commands,trading,sync}.py` | [docs/modules/autotrade-engine.md](docs/modules/autotrade-engine.md) |
| 개별종목분석 (단일종목 360·뉴스) | **희제** | `ir_engine`(describe 단일) · web 리포트 | [docs/modules/stock-analysis.md](docs/modules/stock-analysis.md) |
| 포트폴리오 (진단·관리) | **희제** | `routers/portfolio.py` · web 포트폴리오 | [docs/modules/portfolio.md](docs/modules/portfolio.md) |
| 웹 빌더·시각화 (공통) | 조대표 (개별종목·포트폴리오 화면은 희제) | `web/src/` | 해당 모듈 문서 참조 |

**표기 규칙:** **〔담당: 이름〕** = 유지보수자, **〔작성: 이름〕** = 그 문단을 직접 쓴 사람.
분담은 유동적 — 경계가 겹치면 PR/이슈에서 협의. 남의 담당 모듈을 대신 기술하면 "작성=본인(보강 영역)"으로 정직히.

## 4. 보안 원칙 (위반 금지)

- **KIS 자격증명·계좌번호·원시 주문은 사용자 로컬 PC 전용.** 서버 스키마·payload·로그 어디에도 들어가지 않는다.
- **서버에는 안전정보만** — 전략 정의, 체결 로그 요약, 잔고 스냅샷.
- **Git push는 사용자 명시 허락 시에만.** 자동 push 금지.
- **로컬앱 토큰 파일은 Windows ACL로 사용자 전용** (Phase 41-C-2/3).

## 5. 코딩·협업 규칙

모든 작업에 적용. 위반 의심 시 즉시 멈추고 사용자와 합의한다.

### 핵심 4원칙
- **근본 원인 해결.** 본질적 해결이 가능한 상황에서 임시방편 fallback·예외 무시·`except: pass`·`or default`·
  증상 봉합용 가드 금지. 증상이 아니라 원인을 고친다. fallback이 정당한 경우는 외부 시스템(브로커·OS·네트워크)의
  진짜 한계뿐 — 그때도 *왜 필요한지* 명시 주석을 단다.
- **Over-engineering 금지.** 핵심 가치에 필수적이지 않은 부차 기능·옵션·추상화 추가 금지. "혹시 모르니"로 옵션·
  계층·플래그를 늘리지 않는다. 호출자 1곳뿐인 추상화, 사용처 없는 옵션·env, dead config, 미사용 분기를 의심한다.
  부차 표면은 제거가 추가보다 우선.
- **Overthinking 금지.** 같은 결과를 더 단순·효율적으로 낼 방법이 있으면 복잡한 workflow를 만들지 않는다.
  단순·명시·직관 우선. 다단 캐시·중복 가드·과도한 계층화는 *실제로 측정된* 문제를 풀 때만 정당하다.
  두 안이 결과가 같으면 짧고 추론이 쉬운 쪽.
- **검증된 해결책만.** 변경은 실제 동작·테스트·신호로 검증한 뒤에만 "완료"라 선언한다. 추측("아마 동작")으로
  품질을 떨구지 않는다. UI = 브라우저로 동작·에러 확인, 자금 안전 경로 = paper/MockBroker 1회, 코드 품질 =
  lint·type·test·golden. 검증 불가면 "검증 불가" 명시 보고하고 자율 완료 선언하지 않는다.

### 운영 규칙
- **학습 원장 유지 (모든 작업 적용).** 거시적 작업계획에 착수/완수할 때 해당 모듈 문서(`docs/modules/<m>.md`)를 갱신한다:
  - **착수:** 작업계획 로그에 entry — 의도(self-contained 2~3문장)·계획. `[진행중]`.
  - **완수:** 시행착오·인사이트·결과 구현 채움 + `[완료]` + **전이 가능한 교훈을 맨 위 `📌 교훈·함정`으로 distill**(같은 실수 반복 방지).
  - **단위 = 거시 작업계획**(≈기능/이니셔티브, 여러 PR·세션 걸침). 사소한 task는 기록 안 함(브리핑/커밋이 담당).
  - **CLAUDE.md엔 진척을 적지 않는다.** 모듈 무관 구조 변경·담당 경계 변동만 §3에 반영. 휘발 진척은 draft PR/브리핑.
- **Git 협업 워크플로(필수) — 충돌·중복·유실 방지.** 2명이 여러 세션·worktree로 병렬 작업하므로 절차를 강제한다:
  - **작업 전:** `git fetch` 후 **열린 PR 확인**(SessionStart 훅이 자동 출력). 내가 만질 파일·담당 경계(§3)가
    다른 PR과 겹치면 **시작 전** PR/이슈에서 협의한다.
  - **시작 시 draft PR:** 의도를 *끝*이 아니라 *시작*에 broadcast(유실·중복 방지). in-flight 진척은 draft PR 본문에,
    구조적 핸드오프는 `docs/modules/`에. 진행상황을 CLAUDE.md에 적지 않는다.
  - **브랜치·push:** `feat/`·`fix/` 작업 단위 브랜치로만. **main 직접 push 금지**(pre-push 훅 차단).
    clone·worktree마다 1회: `git config core.hooksPath .githooks`.
  - **짧게·자주 머지:** 브랜치는 오래 두면 발산한다(과거 −212커밋 drift 사례). 매일 main 머지, merge 후 알림,
    다음 작업은 **최신 main에서 pull 후** 시작.
  - **머지된 브랜치 재사용 금지 (충돌 재발 근본원인 — 가드 적용됨).** 머지 끝난 브랜치에 새 작업을 얹지 말 것.
    새 작업 = 최신 main에서 **새 브랜치**: `git checkout main && git pull && git switch -c feat/<새이름>`.
    pre-push 훅이 머지된 브랜치 재push를 **차단**하고, SessionStart 훅이 stale/merged 브랜치에 **경고**한다.
  - **희제 PR은 merge-commit으로 머지(squash 금지).** squash는 커밋 author를 PR 저자(희제)로 찍어 Vercel
    웹 자동배포가 거부된다(이유·수동대안 §2.5). GitHub "Create a merge commit" 또는 `gh pr merge <n> --merge`.
    (조대표 본인 PR은 author=조대표라 squash 무방.)
  - 협업 환경 전체 안내: [docs/COLLABORATION.md](docs/COLLABORATION.md).
- **규모 있는 작업: 설계안 제시 → 질문 → 승인 → 구현.** 곧장 코드부터 쓰지 않는다.
- **공백·인코딩.** Windows cp949 환경. UTF-8 명시 필요 시 `-Encoding utf8`(PowerShell)·`reconfigure(encoding="utf-8")`(Python).

## 6. CLAUDE.md 편집 기준 (이 파일을 lean하게 유지)

CLAUDE.md는 **공동작업자 align 장치**다. 한 줄을 넣기 전 3-test — **셋 다 통과**해야 수록:
1. **Universal** — 어떤 모듈을 작업하든 알아야 하나? (특정 모듈 안에서만 의미 → `docs/modules/`)
2. **Stable** — 매 작업/PR마다 바뀌지 않나? (진척·상태는 휘발 → draft PR/브리핑)
3. **Align-critical** — 모르면 공동작업자와 충돌/불변식 위반/중복하나?

**요지: 누가·무엇을·어디서·규칙(WHO·WHAT·WHERE·RULES) → 여기 / 어떻게(모듈 내부)·현재 상태(HOW·STATUS) → 밖.**

## 7. 디자인

**`DESIGN.md` = 디자인 단일 기준(SSOT). UI 작업 전 필독.** 색·타이포(모듈 22/16/12pt)·간격·
컴포넌트·**차트 규칙(네이비 막대+골드 선, 회색 금지, 마진/%-선, FY·nQ 라벨)** 모두 거기 정의.
토큰의 실제 값은 `web/src/index.css` `:root` — 토큰 변경 시 `:root`와 `DESIGN.md`를 **함께**
고친다(하나만 고치면 가이드가 낡아 통일성이 깨짐). 임의 색/폰트/간격 도입 금지, 벗어나기 전 합의.

## 8. 전체 리뷰 트리거

사용자가 `/풀리뷰`·`/full-review`·`풀리뷰 실행`·`full review run` 중 하나를 쓰면 즉시 `REVIEW_PLAYBOOK.md`를
읽고 10단계(Phase 0~9)를 순차 실행한다. 산출물은 `docs/review-reports/YYYY-MM-DD-HHMM/`에 phase별 저장,
최종 `SUMMARY.md`로 통합. 총 예산 ~2.5~3시간. 중간 STOP/PAUSE 시 진행 상태 저장 후 멈춤.

## 9. 진단 — 로그 채널을 직접 CLI로 조회

추측 금지. 사용자 신고 또는 cycle·preview 이상 의심 시 다음 채널을 **직접 CLI로 호출**해 실제 로그를 확보한 뒤
진단한다(4원칙 "검증된 해결책만"의 진단 단계).

**인시던트 기록(항상).** 프로덕션/인프라 장애는 해소 직후 `docs/incidents/`에 파일 1개로 **발생·대응·결과**를 남긴다.
형식·인덱스는 `docs/incidents/README.md` 참조.

```bash
# Railway — 서버 stdout/stderr (cron·예외·HTTP·DB 에러)
railway logs --since 5h --lines 1000 --filter "@level:error"
railway logs --http --status ">=500" --lines 50
# Vercel — 웹앱 build·deploy·serverless runtime
npx vercel ls quantman                 # 최근 deploy 목록
npx vercel inspect <deployment-url> --logs
npx vercel logs <production-url>        # runtime log
# GitHub — release·workflow·PR·issue
gh release list --repo MercKR/quantman-releases
gh run list --workflow=<name> --limit 10
gh pr view <number>
# 로컬앱 진단 (사용자 PC)
tail -100 ~/.quant-platform/logs/localapp.log
tail -10 ~/.quant-platform/cycles.jsonl    # 최근 사이클 / orders.jsonl 발주 이벤트
cat ~/.quant-platform/preview_cache.json   # 마지막 server preview 응답 캐시
```
**권한 없으면 사용자에게 요청.** Railway·Vercel·GitHub 모두 user 자격증명으로 동작 — 인증 못 받았으면 즉시 알리고 대안 안내.

## 10. 외부 API knowledge base — 작업 전 필수 참조

외부 API 호출·결함 진단·새 endpoint 사용 시 **추측 금지** — 먼저 **`docs/api-index.md`(레지스트리)**에서 그 API의
*검증된 문서 접근법*을 찾는다(🟢 WebFetch / 🟠 WebSearch / 🟡 패키지소스 / 🔵 로컬 / 🟣 스킬).

**KIS**는 로컬 KB가 가장 충실 — 작업 전: ① `docs/kis-api/INDEX.md` grep으로 endpoint 후보 →
② `docs/kis-api/endpoints/{TR_ID}_*.md`(request/response/모의실전/한계) → ③ `docs/kis-api/GOTCHAS.md` 훑기 →
④ 필요 시 `docs/kis-api/raw/*.xlsx`.

작업 중 발견·새 endpoint·결함 진단 시 **즉시 기록**(자가발전): 새 endpoint→`endpoints/{TR_ID}_*.md` / 실측 차이→
`GOTCHAS.md` 상단 / 릴리즈 fix→`CHANGELOG.md` / 사용 위치→endpoint .md의 `우리 코드 위치`.
신규 API KB는 KIS와 같은 구조로 `docs/{api-name}/`에 만들고 `docs/api-index.md`에 행 등록.

## 11. 자주 쓰는 명령

```powershell
cd web; bun run dev                          # 웹 dev 서버
cd server; uvicorn app.main:app --reload     # 서버 (core는 먼저 pip install -e core/)
cd local; python -m localapp                 # 로컬앱
pytest tests/golden_backtest.py -v           # 백테스트 골든 테스트
grep -i <키워드> docs/kis-api/INDEX.md        # API KB 검색
```

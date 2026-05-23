# Claude 작업 가이드 — 퀀트 자동매매 플랫폼

이 파일은 Claude Code가 이 저장소에서 작업할 때 따라야 할 규칙과 트리거를 모은다.

---

## 1. 프로젝트 개요

- 한국 주식 자동매매 SaaS. 초중급 퀀트 트레이더 대상.
- 핵심 차별점: **문장형 빈칸 조건 설정** — 코드 없이 자연어 흐름으로 전략을 만든다.
- 사용자 흐름: 웹앱에서 전략 수립 → 모의/실전 모드 선택 → 사용자 PC의 로컬앱이 KIS API로 자동 실행.

## 2. 모노레포 구조

```
platform/
├── core/         quant_core (전략 정의·백테스트·분석 엔진, pure Python)
├── server/       FastAPI (사용자·전략·동기화·preview, Railway 호스팅, Postgres)
├── web/          React+TypeScript+Vite (Vercel 호스팅)
├── local/        Python Tkinter 데스크탑 (KIS REST+WS, PyInstaller 번들)
├── DESIGN.md     디자인 시스템 — 모든 UI 작업 기준
├── REVIEW_PLAYBOOK.md   /풀리뷰 트리거 시 따를 10단계 스크립트
├── docs/
│   └── QUANT_DOMAIN_CHECKLIST.md   시스템 트레이딩 도메인 체크리스트
└── tests/
    └── golden_backtest.py          백테스트 엔진 회귀 테스트 (Phase 8에서 실행)
```

배포:
- 웹앱: Vercel (production + preview), GitHub push → 자동 deploy
- 서버: Railway, GitHub push → 자동 deploy
- 로컬앱: PyInstaller zip → `MercKR/quantman-releases` (public repo) GitHub Release

## 3. 보안 원칙 (위반 금지)

- **KIS 자격증명·계좌번호·원시 주문은 사용자 로컬 PC 전용.**
  서버 스키마·payload·로그 어디에도 들어가지 않는다.
- **서버에는 안전정보만** — 전략 정의, 체결 로그 요약, 잔고 스냅샷.
- **Git push는 사용자 명시 허락 시에만.** 자동 push 금지.
- **로컬앱 토큰 파일은 Windows ACL로 사용자 전용** (Phase 41-C-2/3).

## 4. 코딩·협업 규칙 — 핵심 4원칙

모든 작업에 적용. 위반 의심 시 즉시 멈추고 사용자와 합의한다.

- **근본 원인 해결.** 본질적 해결이 가능한 상황에서 임시방편 fallback·예외
  무시·`except: pass`·`or default`·증상 봉합용 가드 금지. 증상이 아니라 원인을
  고친다. fallback이 정당한 경우는 외부 시스템(브로커·OS·네트워크)의 진짜
  한계뿐 — 그때도 *왜 필요한지* 명시 주석을 단다.
- **Over-engineering 금지.** 서비스 핵심 가치 구현에 필수적이지 않은 부차
  기능·옵션·추상화 추가 금지. "혹시 모르니"로 옵션·계층·플래그를 늘리지
  않는다. 호출자가 1곳뿐인 추상화, 사용처 없는 옵션·환경변수, dead config,
  미사용 분기·파라미터를 의심한다. 업계 표준이 단일 동작이면 단일,
  다중이면 합의된 다중만. 유저와 개발자를 혼란스럽게 만드는 부차 표면은
  제거가 추가보다 우선.
- **Overthinking 금지.** 같은 결과를 더 단순·효율적으로 낼 방법이 있는데
  복잡한 workflow나 불필요한 코드를 만들지 않는다. 단순·명시·직관 우선.
  다단 캐시·중복 가드·과도한 계층화·"만약을 위한" 추가 단계는 그 복잡도가
  *실제로 측정된* 문제를 해결할 때만 정당하다. 두 안이 결과가 같으면
  코드가 짧고 추론이 쉬운 쪽을 선택한다.
- **검증된 해결책만.** 변경은 실제 동작·테스트·신호로 검증한 뒤에만
  "완료"라 선언한다. 추측("should work", "아마 동작할 것")으로 품질을
  저하시키지 않는다. UI 변경 = 브라우저(Claude in Chrome)로 동작·포커스·
  에러 상태 확인, 자금 안전 경로 = paper/MockBroker 시나리오 1회, 코드
  품질 = lint·type·test·golden 신호. 검증이 불가능하면 "검증 불가" 사실을
  명시 보고하고 자율 완료 선언하지 않는다.

운영 규칙:
- **규모 있는 작업: 설계안 제시 → 질문 → 승인 → 구현.** 곧장 코드부터 쓰지 않는다.
- **공백·인코딩.** Windows cp949 환경. UTF-8 명시 필요한 경우 `-Encoding utf8` 지정.

## 5. 디자인

`DESIGN.md` 참조. 색상·타이포·간격·컴포넌트 패턴 모두 거기 정의.
새 컴포넌트는 이 시스템 안에서 만들고, 벗어나기 전에 합의한다.

## 6. 전체 리뷰 트리거

사용자가 다음 표현 중 하나를 쓰면 즉시 `REVIEW_PLAYBOOK.md`를 읽고 거기 정의된
10단계 (Phase 0~9)를 순차 실행한다.

**트리거 phrase:**
- `/풀리뷰`
- `/full-review`
- `풀리뷰 실행`
- `full review run`

산출물은 `platform/docs/review-reports/YYYY-MM-DD-HHMM/` 폴더에 phase별로 저장,
최종 `SUMMARY.md`로 통합.

총 예산 ~2.5~3시간. 중간 STOP/PAUSE 시 진행 상태 저장 후 멈춤.

## 7. 자주 쓰는 명령

```powershell
# 웹 dev 서버
cd platform/web; bun run dev

# 서버 로컬 실행
cd platform/server; uvicorn app.main:app --reload

# 로컬앱 실행
cd platform/local; python -m localapp

# 백테스트 골든 테스트
cd platform; pytest tests/golden_backtest.py -v
```

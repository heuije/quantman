# 챗봇 뉴스 리서치 재설계 — 능동 `research_news` 도구 (Approach C)

> 상태: **구현 완료** (feat/chat-news-research · 미push). 계획=`news-research-plan.md` 6 TDD 태스크 완료.
> 검증: core 438 pass(골든 불변)·server 379·하니스 18/18·웹빌드·ruff 0신규. trafilatura는 이미 설치돼 있어
> 본문 추출에 사용(§8 의존성 결정 해소). 잔여=사용자 push/머지 허락 + 키(NAVER) 있는 환경 라이브 E2E.

## 0. 배경·목표

현재 챗봇 뉴스 = `describe` 사이드카가 붙이는 **최근 헤드라인 5건**(네이버 검색·on-demand·본문 없음·과거 없음).
데이터엔진은 뉴스를 **수집·아카이브하지 않음**(유일한 스텁 모달리티 — 크론·parquet·DB 0). 즉 "엔진이 모아둔
뉴스 코퍼스"는 없고, 챗 사이드카와 노코드(`ir.py`)가 같은 `news_kr.fetch_news` 하나를 on-demand로 부를 뿐.

**목표**: 유저 질문에 답하기 위해 모델이 *필요한 뉴스 종류(엔티티+관련 매크로)·기간*을 **판단** → on-demand
**수집** → **본문까지** 읽고 → 답변. 과거·실시간 모두. 단순 헤드라인을 넘어 인과·맥락 해석.

## 1. 결정 요약 (사용자 협의 완료)

| 결정 | 값 | 근거 |
|---|---|---|
| 통합 형태 | **Approach C 하이브리드** | 가벼운 describe 헤드라인 사이드카 **유지**(즉답·저비용) + 능동 도구 `research_news` 신설(심층). 가벼운 건 가볍게, 깊은 건 도구로 |
| 기간 | **하이브리드·모델 판단** | 최근=네이버, 과거=GDELT. 모델이 기간(최근/과거/범위) 판단 |
| 본문/비용 | **2단계 다이제스트** | 본문 수집 → **Haiku 압축**(증거 다이제스트) → 오케스트레이터. 토큰 통제+깊이 보존. LLM=Haiku 1콜(NL컴파일 동형) |
| 검색 | **자유 키워드** | `queries:[str]` 자유 텍스트. 엔티티만 심볼→정식명 grounding, 매크로/테마/기간은 모델 자유. 티커 고정 ❌(매크로 뉴스 위해) |
| 소스 | 네이버(최근 KR)+GDELT(과거/글로벌)+본문추출 | US 최근=Yahoo 옵션·후속, KR 과거정밀=BigKinds 후속 |

## 2. 모듈 경계 (아키텍처 최적 배치)

| 모듈 | 위치 | 역할 | LLM |
|---|---|---|---|
| `news_kr.py` (기존) | `core/quant_core/data/feeds/` | 최근 KR(네이버 검색)·헤드라인+링크+스니펫 | 0 |
| `news_gdelt.py` **(신규)** | `core/…/data/feeds/` | 과거/글로벌(GDELT doc API·쿼리+날짜범위·throttle)·기사 메타 | 0 |
| `news_body.py` **(신규)** | `core/…/data/feeds/` | 기사 URL→본문 추출(trafilatura/폴백)·URL캐시 | 0 |
| `news_research.py` **(신규)** | `server/app/chat/` | 오케스트레이션: 라우팅→수집→본문→**Haiku 다이제스트**→반환 | Haiku 1콜 |
| `tools.py`(+`agent.py`) | `server/app/chat/` | `research_news` 도구 스키마·디스패치(기존 tool-use 루프) | — |
| `NewsDigest` 렌더러 | `web/…/ResultCharts`·`ChatResultView` | shape `news_research`→내러티브+인용 링크(Phase3 RENDERERS) | — |
| `capabilities.py`·`prompt.py`·`ir_compiler` | core·server | SSOT — 오케스트레이터가 "뉴스 리서치 가능" 인지 | — |

**원칙 적합**: 결정적 수집(naver/gdelt/body)은 **0토큰**(기존 news_kr와 동일 계층). LLM은 **Haiku 다이제스트
1콜뿐**. 뉴스는 엔진/dataset/백테스트에 **미진입(골든 무누출)** — `inspect`처럼 도구결과로만.

## 3. 데이터 흐름

```
유저 "엔비디아 지난달 왜 급락?"
 → [오케스트레이터 판단] research_news(
       queries=["엔비디아","NVIDIA","반도체 수요","AI 캐펙스"],   # 엔티티+관련매크로(자유, 엔티티는 grounding)
       period={kind:"range", start:"2026-05-01", end:"2026-05-31"},  # 기간(모델 판단)
       depth="full")
 → [news_research / 서버]
      ① 라우팅: recent→네이버 · range/과거→GDELT(throttle 1/5s + 캐시)
      ② 후보 기사 수집 · URL 중복제거 · 상한 N(≤8)
      ③ news_body로 본문 추출(캐시·타임아웃·길이상한)
      ④ Haiku 다이제스트: 본문들 → {핵심드라이버·타임라인·기사별1줄[n]·종합내러티브} (기사 [n] 참조)
      ⑤ 반환 {digest(모델용), citations(결정적·웹링크), period, n, sources}
 → 오케스트레이터가 digest로 **답변**(인용 포함)
 → 웹: NewsDigest 카드(내러티브 + 출처 링크)
```

## 4. 도구 스키마 `research_news`

```
research_news(
  queries:      [str],     # 엔티티 + 관련 매크로/섹터 (모델 판단; 엔티티는 심볼→정식명 grounding)
  period:       {kind:"recent", days:N} | {kind:"range", start:"YYYY-MM-DD", end:"YYYY-MM-DD"},
  max_articles: int = 8,
  depth:        "headlines" | "full" = "full"   # headlines=빠름(본문·다이제스트 생략) / full=본문+Haiku
)
```
- 매크로 뉴스 = 모델이 `queries`에 관련 매크로/섹터어를 직접 채움(예 삼성전자 → +"반도체 업황·D램 가격·환율").
- 시장 질문("코스피 왜")이면 `queries=["코스피","증시","외국인 순매도","미 금리"]` 전부 자유 키워드.
- 기존 `describe` 헤드라인 사이드카 유지 — 즉답 facet(빠름·Haiku 없음). 심층/기간/매크로/본문 필요 시에만 도구 호출.

## 5. 다이제스트 + 토큰 통제

- 입력: N(≤8)기사 (제목 + 본문 절단 ~1,500자/기사) + 쿼리 맥락.
- Haiku 출력(≤~600토큰): **{핵심 드라이버 3~5 · 타임라인(날짜→사건) · 기사별 1줄[n] · 종합 내러티브 2~3문장}**.
- ⚠ **인용은 결정적** — fetch한 실제 기사 메타 {제목·URL·날짜·매체}를 그대로 전달(LLM 미생성 → URL 환각 차단).
  다이제스트의 `[n]` ↔ 인용 리스트 매핑. 숫자·사실은 기사에서만.

## 6. 에러·레이트리밋·키 (전부 best-effort)

- **키**: NAVER(최근)=서버 env, 미설정 시 GDELT 폴백 또는 빈 결과+정직고지. GDELT·본문=무키.
- **GDELT 1/5s**: 프로세스 throttle(min-interval) + (query,period) 캐시 · `queries` 상한 ≤4 · 초과 시 부분+고지.
- **본문**: URL캐시(TTL)·타임아웃 8s·실패 스킵·소수 동시 풀 · ToS/robots 주의(공개기사).
- **Haiku 실패** → 헤드라인+스니펫 폴백(정직). 전부 best-effort·챗 턴 미파괴.
- **지연**: full ≈ 수초(명시적 "왜/분석"에만 호출)·headlines는 빠름.

## 7. 테스트 (원칙 4)

- 결정적 fetcher(news_gdelt·news_body): HTTP mock → 파싱·중복제거. **$0**.
- news_research: fetcher+Haiku mock → 라우팅(recent→naver, range→gdelt)·dedup·상한·**인용 결정적 조립**·graceful 실패(소스 down·본문 실패·다이제스트 실패).
- 도구 등록: capability coverage·dispatch·NL idiom 존재.
- 웹: NewsDigest 렌더(shape `news_research`) 빌드.
- **골든 byte-identical** — 뉴스가 엔진/dataset에 미진입함을 보장(결정적 엔진 테스트 불변).

## 8. 열린 하위결정 (권장 포함 — spec 리뷰에서 확정)

1. **본문 추출기**: `trafilatura`(품질↑·신규 의존성) vs 경량 휴리스틱(`<article>/<p>`·무dep·품질↓).
   **권장 = trafilatura + 무dep 폴백**(의존성 원치 않으면 폴백 단독).
2. **GDELT 한국어 커버리지**: 엔티티 grounding이 **한글명+영문명 둘 다** 공급(KR 종목 글로벌 커버리지↑).
3. **다이제스트 티어**: 기존 cheap-tier(`QP_NL_COMPILE_MODEL`) 재사용(일관).

## 9. 비범위·후속 트랙

- BigKinds(KR 과거 정밀 아카이브·등록 필요) · 뉴스 센티먼트 백테스트(아카이브 적재·PIT) · 뉴스 본문 영구저장.
- US 종목 최근 뉴스 Yahoo(`yfinance .news`) 일원화 · breadth("코스피 왜")에 시장 뉴스 자동 부착.

---
*(Approach C: 가벼운 describe 헤드라인 사이드카 유지 + 능동 `research_news` 도구. 자유 키워드·엔티티 grounding,
네이버[최근]+GDELT[과거]+본문추출, 2단계 Haiku 다이제스트, 골든 무누출, 인용 결정적.)*

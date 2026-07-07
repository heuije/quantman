# 개별종목분석 〔담당: 희제 · 작성: 조대표(희제 보강 영역)〕

> 학습 원장. 작업계획 착수 시 로그 entry(의도·계획)를 추가하고, 완수 시 시행착오·인사이트·결과구현을 채우고 전이 가능한 교훈을 맨 위 §교훈으로 distill한다. 상세 설계·로드맵은 희제가 보강.

## 📌 교훈·함정 (작업 전 먼저 읽기)
- **Financials 빈 값·빠진 그래프 = 대부분 계정 '매칭' 문제지 데이터 부재가 아니다.** DART 값은 있는데 안 보이는 4대 근본원인(2026-07 노바렉스 당기순이익 디버깅에서 전부 발현):
  1. **`account_id='-표준계정코드 미사용-'` placeholder.** 공시자가 표준코드 미태깅 시 DART가 넣는 placeholder. 같은 계정이 보고서마다 한 해는 `ifrs-full_ProfitLoss`, 다른 해는 이 placeholder로 와서, **account_id로 병합하면 슬롯이 갈려 과거연도가 통째로 유실**된다. → 병합키는 canon(`dart._merge_key`)으로. placeholder를 유효 id로 쓰면 안 됨.
  2. **반기·분기·옛 comparatives는 포괄손익계산서에 연결 당기순이익 '합계'를 빼고 귀속 분해(지배+비지배)만 준다**(합계는 자본변동표=SCE에만 있어 필터됨). → **당기순이익 = 지배기업소유주지분 + 비지배지분** 회계 항등식으로 채운다(`dart._fill_ni_from_attribution`, 연간·분기 store 공통). 귀속 명칭은 `지배회사지분순이익`·`...귀속되는 분기/반기순이익` 등 변형 다수 → `_CANON`에 등록해야 인식.
  3. **canon 정규화가 계층마다 다르면 그래프가 조용히 빠진다.** 서버는 **dart-fss(`dart_doc._ckey`) primary** + OpenAPI(`dart._canon_account`) 폴백/옛연도채움인데 **두 맵이 달랐다** — dart-fss가 `연결당기순이익`·`당기순이익(손실)` canon을 그대로 두니, 프론트 FinCharts(`canon=='당기순이익'` 정확매칭)·`_add_pl_metrics`(순이익률)가 못 찾아 **순이익 그래프·마진이 통째로 빠짐**. → `_ckey`/`_canon_account`/프론트 매칭 3곳의 표준명을 **일치**시켜라(연결 접두·(손실) 접미·분기/반기 변형 흡수). 단 `연결범위변동`처럼 '연결'이 붙은 별개 계정은 건드리지 말 것.
  4. **열화 저장본이 서빙을 막는다.** `financials._quality_ok`가 기간 수만 봐서 값이 빈 저장본도 '완전'으로 통과 → 80일 서빙(과거 "1주간 미해결"의 원인). 주요계정 구멍 탐지를 게이트에 넣었지만, **오늘 재fetch된(`fetched==today`) 저장본은 당일 재크롤 방지 OR절 때문에 self-heal 안 됨** → 코드 수정 후엔 `server/app/data/serving_cache/financials/*.json` 해당 종목 **파일 삭제**로 강제 갱신. 진단은 반드시 `dart.fetch()` 직접호출 + 라이브 엔드포인트 + 캐시 파일 3층을 구분해서 볼 것(캐시 착시 주의).
- **뉴스는 저장하지 않는 on-demand.** "왜 움직였나" 뉴스 facet은 영속화하지 않고 요청 시점에 가져온다.
- **360 지표는 인사이트 엔진 + 데이터 엔진(펀더멘털·밸류)에 의존한다.** → 데이터 엔진 변경 시 이 모듈이 영향을 받는다.
- **한국 부가데이터(투자자별·컨센서스·추정실적·리포트·공시)는 `server/app/krdata.py` 무료 크롤링.** KIS/유료 아님. 종목·소스별 `lru_cache(일자키)`로 일 1회 fetch, 각 함수가 자체 try/except로 빈 결과 반환(부분 제공). `/market/kr/{code}`가 5소스 `ThreadPoolExecutor` 병렬(~0.3s).
- **데이터 의심 전에 "이 환경의 현재가"부터 본다.** 추정실적·목표가가 비현실적으로 보이면 훈련지식의 과거 주가가 아니라 `fdr`로 *현재가*를 확인하라 — 환경에 따라 주가·실적 수준이 다르다(삼성 32만·SK 215만이면 목표가 53만·295만은 정상). 실제로 9라운드를 소스 의심에 허비했다.
- **FnGuide 추정실적은 BeautifulSoup `id="highlight_D_Y"`(연간 8년) 직접 파싱.** `read_html` 인덱스 의존 금지 — 표8/9는 동종업종 비교표(삼성전자/코스피전기전자/KOSPI)라 매출액·영업이익 행이 있어도 연도별이 아니다. read_html·BeautifulSoup 결과가 같으면 그건 출처 원본값(파싱 오류 아님).
- **공매도는 무료 키-발급형 API가 없다.** ① KRX OPEN API(openapi.krx.co.kr, 키 발급형)에 **공매도 서비스 자체가 없음**(지수·주식·채권·파생·ESG뿐 — 직접 카탈로그 확인). ② data.go.kr 금융위도 공매도 데이터셋 없음. ③ 네이버 등은 KRX iframe만 임베드. → **공매도 무료 출처는 KRX 웹(data.krx.co.kr)뿐인데 봇 차단**: 이 샌드박스에서 `GenerateOTP`만 200, `download.cmd`는 content-length 0(공매도 무관 기본 데이터조차), `getJsonData`는 LOGOUT(400). `krdata.shorting`은 ISIN finder→OTP→CSV 다운로드 크롤러로 구현하되(한글 헤더 substring 매칭, 실패 시 graceful 빈값) **배포(Railway)에서 KRX 접근 가능 여부 검증 대상**. KRX가 데이터센터 IP도 막으면 그 섹션만 비활성(다른 소스 무영향). 대안은 로컬앱 KIS API(자동매매 엔진 영역).
- **투자의견은 `_norm_opinion`으로 BUY/HOLD/SELL 정규화** — 증권사마다 매수/Buy/BUY 혼용. `_OPINION_MAP`(소문자키)+기본 대문자화.
- **투자자별은 네이버 frgn 페이지네이션으로 최대 120거래일** 확보(페이지당 ~20일, 날짜 dedup). 프론트가 1/5/20/60/120일 창 누적 집계.
- **공시(DART)는 서버 `OPENDART_API_KEY` env 전용.** 로컬·미설정 시 `disclosures`가 자연히 `[]`(보안: DART 키는 서버만). 배포 환경에서만 채워짐.
- 상세 설계·로드맵은 희제가 보강.

## 현재 구조 (안정)

**기능.** 종목 하나를 평문으로 풀어 보여주는 단일종목 분석 — 인사이트 엔진 `describe`(단일) 갈래 + "왜 움직였나" 뉴스. 소매 사용자 최대 수요 지점.

**관련 위치(현재 파악 기준 — 희제 확인·보강).**
- `core/quant_core/ir_engine/run.py` — `run_describe_report`(단일종목 360 지표)
- `server/app/routers/ir.py` — `_attach_symbol_news`(서버 엣지 뉴스 facet)
- `web/src/components/ResultCharts.tsx` — `ReportCards`(360 카드 + 뉴스)

## 작업계획 로그 (누적·최신 우선)
- **[완료] Company Analysis 한국 종목 부가데이터 5종** 〔작성: 희제(보강 영역)〕
  - **의도.** Company Analysis(개별종목)에 한국 투자자 친화 데이터를 무료 공개 소스로 붙인다: ①투자자별 순매매(기관/외국인/개인), ②애널리스트 컨센서스(목표주가·투자의견·현재가대비%), ③추정실적(매출/영업이익/순이익/지배주주, 직전+당해/차년 추정E), ④애널리스트 리포트 목록, ⑤최근 공시(DART). 한국 종목(6자리)만 대상.
  - **구현.** `server/app/krdata.py` 신규 — 네이버 금융(frgn 투자자별·리서치 리포트)·네이버 wisereport(컨센서스 표12)·FnGuide(`highlight_D_Y` 추정실적)·DART(OpenDartReader, 서버키). `routers/market.py`에 `GET /market/kr/{symbol}` — 5소스 `ThreadPoolExecutor` 병렬. `web`: `api.krExtras` + `KrExtras` 타입 + `StockDashboard.tsx`의 `KrSections`(투자자별 막대차트·컨센서스 표·추정실적 표·리포트/공시 2열). 단일 한국종목 선택 시 기간 무관하게 종목만 의존하는 `useEffect`로 fetch.
  - **검증.** 백엔드 5소스 실데이터 HTTP 검증 ✓(투자자별 20일·리포트 15·컨센서스 12·추정실적 8년). tsc·vite build ✓. 5180 프록시→백엔드 라우트 등록 확인 ✓. **시각 검증은 환경 제약(Preview MCP 경로버그·Chrome 미연결)으로 자체 스크린샷 불가** — 사용자 브라우저 reload로 확인 필요.
  - **미해결.** 공매도잔량(KRX 봇차단/OTP — 무료 불가), 공시 실데이터(서버 OPENDART_API_KEY 배포 환경에서만), 추정실적 EBITDA(FnGuide 연간표 미포함).
  - 브랜치 `feature/ui-navy-redesign`(PR #128 — Company Analysis 디자인 개편과 동일 베이스).

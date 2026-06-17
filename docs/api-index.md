# 외부 API 문서 레지스트리

이 프로젝트가 쓰는 **외부 API별 "오류 시 문서 확인법"**의 단일 진실원천.
API 호출이 막히거나(에러·예상밖 응답) 새 endpoint를 쓸 때, **추측하지 말고** 아래 표에서 그 API의
**검증된 접근 수단**으로 공식 문서를 확인한다. (배경: OpenDART status 코드를 안 보고 추측해 false
빈결과 마커 사고가 났다 — `docs/incidents/2026-06-10-neon-data-transfer-quota.md` 인접.)

## 접근 수단 범례
- 🟢 **WebFetch 직접** — 공식 문서가 WebFetch로 읽힘(검증됨).
- 🟠 **WebSearch 우회** — 공식 사이트가 봇 차단(403)이라 WebFetch 불가 → WebSearch로 핵심 확보.
- 🟡 **패키지 소스 읽기** — 래퍼 라이브러리. `python -c "import inspect,X; print(inspect.getfile(X))"`로
  설치된 소스를 직접 읽는다(래퍼가 동작을 숨길 때 — OpenDartReader가 status를 숨긴 사례).
- 🔵 **로컬 docs** — repo 내 정리된 문서.
- 🟣 **스킬** — Claude 스킬 호출.
- 🔴 **비공식/스크랩** — 공식 문서 없음 → 우리 코드 + GOTCHAS(실측 누적)만.

## 레지스트리 (검증일 2026-06-10)

| API | 용도 · 코드 위치 | 문서 접근 (검증됨) | 핵심 gotcha |
|---|---|---|---|
| **KIS** | 자동매매·시세·종목마스터 / `local/kis_*`·`server/kis_*` | 🔵 `docs/kis-api/INDEX.md`→`endpoints/{TR_ID}_*.md`→`GOTCHAS.md`→`raw/*.xlsx` (로그인·xlsx라 WebFetch 불가) | 모의=`openapivts`·실전=`openapi`. 마스터=`new.real.download.dws.co.kr` |
| **LS증권** | 자동매매(2nd broker) / `local/localapp/ls_broker.py` (국내주식 draft·키검증 전) | 🔵 `docs/ls-api/INDEX.md`→`endpoints/{tr_cd}_*.md`→`GOTCHAS.md` + 🟢 `openapi.ls-sec.co.kr/howto-sample`(WebFetch가능) | **모의/실전 단일 도메인** `:8080` — appkey로 구분(KIS와 다름). `rsp_cd="00000"`. BnsTpCode: 1=매도·2=매수. IsuNo=`"A"+6자리`(모의). 키 미발급 → ⚠️ 필드 다수 미검증 |
| **OpenDART** | KR 펀더멘털 / `core/.../feeds/fundamental_kr.py` | 🟢 `https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS00X&apiId=Y` (그룹목록=`guide/main.do?apiGrpCd=DS001`~`DS006`) | ⚠`OpenDartReader.finstate_all`이 013·020 status를 **빈 df로 숨김** → 한도/무데이터 구분하려면 `requests`로 직접 호출해 `jo["status"]` 봐야. **020=요청제한(일 20,000건)**·013=데이터없음. [[reference-opendart-api-guide]] |
| **Binance** | 암호화폐 OHLCV / `core/.../data_fetcher.py` | 🟢 `https://developers.binance.com/docs/binance-spot-api-docs/rest-api/...` (랜딩 `binance-docs.github.io`는 redirect만 — **딥링크**라야 됨) | `GET /api/v3/klines` weight 2, limit 최대 1000 |
| **GitHub API** | 로컬앱 자동업데이트 / `local/.../updater.py` | 🟢 `https://docs.github.com/en/rest/...` | secondary rate limit, `Authorization: Bearer` |
| **alternative.me** | 공포탐욕지수 / `core/.../data_fetcher.py` | 🟢 `https://alternative.me/crypto/fear-and-greed-index/` | `GET /fng/` (limit·format·date_format) |
| **FinanceDataReader** | KR OHLCV·섹터·상폐 / `data_fetcher.py`·`feeds/*`·다수 | 🟢 `https://github.com/financedata-org/FinanceDataReader` (readme) + 🟡 패키지 소스 | `StockListing('KRX-DESC')`·`('KRX-DELISTING')`. 라이브러리라 Yahoo/네이버 등 하위소스 의존 |
| **SEC EDGAR** | US 펀더멘털 / `core/.../feeds/fundamental_us.py` | 🔴 `sec.gov` WebFetch **403 봇차단** → 🟠 WebSearch "SEC EDGAR companyfacts API" | ⚠**10 req/s 한도 + User-Agent 필수**(`CompanyName email` 형식, 일반 UA=403 차단). `data.sec.gov/api/xbrl/companyfacts`. bulk=`sec.gov/files/companyfacts.zip` |
| **FRED** | 매크로 시리즈 / `core/.../data_fetcher.py` | 🔴 `fred.stlouisfed.org/docs/api` **403** → 🟠 WebSearch "FRED API series_observations" | 우리는 CSV 다운로드(`graph/fredgraph.csv?id=`) 사용(공식 API키 경로 아님) |
| **NAVER 검색** | 뉴스 / `core/.../feeds/news_kr.py` | 🔴 `developers.naver.com` **fetch 불가** → 🟠 WebSearch, 또는 우리 `news_kr.py`에 형식 명시 | 헤더 `X-Naver-Client-Id`/`X-Naver-Client-Secret`. `search/news.json` (query·display·sort) |
| **NAVER 모바일** | 스크리너 펀더멘털 / `server/.../naver_fundamentals.py` | 🔴 **비공식 스크랩**(공식문서 없음) → 우리 코드 + GOTCHAS only | `m.stock.naver.com/api/stock/{code}/integration`. 상용 ToS 회색 |
| **yfinance** | US/선물 OHLCV·매크로 / `data_fetcher.py`·`us_metrics_cache.py` | 🔴 **비공식**(Yahoo unofficial) → 🟡 패키지 소스 + GOTCHAS | `auto_adjust=True`=배당·분할 조정. 상용 ToS 회색 |
| **Anthropic/Claude** | NL 컴파일러 / `server/.../ir_compiler.py` | 🟣 `/claude-api` 스킬 호출 | 모델ID·툴유즈·캐싱은 스킬 참조 |
| **Google OAuth** | 로그인 / `server` auth | 🟠 WebSearch/공식(미검증) | `google_sub` 검증 |

## 사용법 (오류·새 endpoint 시)
1. 위 표에서 그 API 행을 찾는다.
2. **검증된 수단**으로 문서 확인 — 🟢면 WebFetch, 🔴403/차단이면 🟠 WebSearch, 🟡면 패키지 소스 읽기,
   🔵면 로컬 read, 🟣면 스킬.
3. 비자명한 실측 발견(공식문서에 없는 동작 — 예: OpenDartReader status 숨김)은 **이 표의 gotcha
   칸 또는 `docs/api-gotchas/{api}.md`**(많아지면 분리)에 기록. KIS는 기존 `docs/kis-api/GOTCHAS.md`.

## 검증 기록 (2026-06-10)
- WebFetch 직접 OK: OpenDART·Binance(딥링크)·GitHub·alternative.me·FinanceDataReader.
- WebFetch 403 차단 → WebSearch 우회 확인(SEC): SEC·FRED·NAVER developers.
- 패키지 소스 읽기로 OpenDartReader status-숨김 확인(`dart_finstate.finstate_all`이 비-000을 빈 df 반환).

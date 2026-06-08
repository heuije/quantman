# 데이터 엔진 재설계 — 설계 및 단계별 계획

> 목적: 기능마다 데이터를 따로 긁는 현 구조(3갈래 + 중복 소싱)를, **단일 수집 · 단일 진실원천
> + 다중 서빙 뷰**의 "데이터 엔진"으로 구조적 재설계한다. 단건 대응이 아니라 근본·구조 최적화.
> 차근차근 단계적으로 구현·검증한다.
>
> 적용 맥락: **베타 / 개인용** — 상용 ToS·재배포 제약은 고려 대상 아님. 소스는 오직
> **데이터 폭 · 이력기간 · 종목수 · API 허용량 · 신뢰성**으로 선정한다.

## 0. 원칙 (이 재설계의 자기검토 기준)

1. **근본 해결**: 펀더멘털 빈값 · 섹터 404 · 중복 소싱을 *부류*로 닫는다(단건 패치 아님).
2. **Over-engineering 금지**: `data_cache.py`가 이미 80% 토대 — 새 추상화 전에 그걸 승격해 재사용.
3. **Overthinking 금지**: 물리 저장소를 한 덩어리로 합치지 않는다. 소싱·진실원천만 일원화,
   서빙은 접근 패턴별 뷰로 분리(과일원화가 백테스트·스크리너 둘 다 망침).
4. **검증된 해결책만**: Phase마다 검증 게이트. 골든 14 백테스트 불변이 통과의 전제.
5. **데이터포인트당 소스 1개**: 레지스트리가 *코드로 강제*. 현 NAVER/OpenDART 중복이 위반 사례.
6. **검증된 사실로 소스 선정**: 추측 금지(예: KRX "1년"·OpenDART 쿼터는 실측·공식문서로 확정).

## 1. 현 구조 (감사 결과 요약)

데이터 갈래가 **3개**, 같은 데이터를 따로 긷는다:

| 접근 패턴 | 소비자 | 저장소 | 소스 |
|---|---|---|---|
| ① 과거 전체이력 | 전략연구소·백테스트·미리보기·sync·선물 | A: parquet + `data_cache`(볼륨) | yfinance / FDR DataReader / OpenDART / SEC |
| ② 최신 단면(장중) | 스크리너 | B: 메모리 캐시(`krx_cache`) | FDR StockListing / NAVER / 기술 |
| ③ 단건 on-demand | 개별종목분석 | (없음) | FDR DataReader 직접 |

중복·버그(실측):
- KR 종가 = parquet ① + krx_cache ② + portfolio 직접 ③ (3소스, 같은 값)
- KR 밸류(PER/PBR) = OpenDART→엔진 ①(**키 없어 prod 0%**) vs NAVER→스크리너 ②(작동) — 같은 수치 2소스
- 섹터/상장/상태 = FDR `StockListing` → **2026-02-27 KRX 로그인 의무화로 깨짐**(404; krx_cache·classification·listing 동시 degrade)
- ⚠️ **주의(정정)**: KR **OHLCV 자체는 안 깨졌다** — `FDR DataReader`(NAVER 백엔드, 무로그인)는 정상. 깨진 건 `StockListing` 한 경로뿐.

## 2. 목표 아키텍처

```
[수집 레지스트리]  데이터포인트 → (소스 1개, fetcher, 주기)   ← 단일 진실원천·원칙 강제
       │  cron / on-demand
       ▼
[Canonical Store]  볼륨 위 parquet (data_cache 승격)          ← 모든 수집물의 단일 적재처
       │
       ├── history(syms, cols)     전체이력      → 백테스트·연구소·미리보기
       ├── snapshot(cols)          최신 단면     → 스크리너            (canonical 파생 + 장중 델타)
       └── symbol(sym)             단건 on-demand→ 개별종목분석        (없으면 즉시 수집→적재→서빙)
```

- 모든 소비자는 **이 3개 서빙 함수만** 호출한다. 자기 fetch 금지.
- `data_cache`의 기존 자산 재사용: raw 캐시 · 컬럼 프로젝션(`get_projected`) · 세대 무효화 · 심볼 인덱스.

## 3. 소스 선정 (기준: 종목수 · 데이터범위 · 이력기간 · API허용량 · 신뢰성 — *검증됨 2026-06-08*)

> 핵심 정정: 이전안은 ToS에 과가중해 KRX/DART로 기울었으나, **베타/개인용이라 ToS 무관**.
> 데이터 품질로만 재선정 → 대부분 **현 소스 유지**, 깨진 곳·빈 곳만 손본다.

| 데이터 | 선정 소스 | 종목수 | 데이터범위 | 이력 | 허용량 | 결정 이유 |
|---|---|---|---|---|---|---|
| **KR OHLCV** | **FDR `DataReader`** (NAVER 백엔드) | 전 종목(per-symbol) | OHLCV | **2000~** (KRX옵션 1995~) | 무로그인 | **유지 — 안 깨짐**. 깊은 이력·전 종목. yfinance는 대형주 교차용 |
| **KR 펀더멘털 (이력=백테스트)** | **OpenDART 전체재무제표** | 전 상장 | 전체 BS/IS/CF → PB/PE/EV/ROIC/마진/**총부채(Altman)** | **2015~** | **20,000/일** | **유일한 다년 구조화 소스**. NAVER/yfinance는 스냅샷·3년뿐→밸류 팩터 백테스트 불가 |
| **KR 섹터(업종)** | **data.go.kr 상장종목정보**(무로그인) + NAVER 업종 fallback. *옵션: pykrx+KRX로그인=WICS 고품질* | 전 종목 | 업종 분류 | 현재 | data.go.kr 키 | **깨진 `StockListing` 대체**(이번 유일한 소스 교체) |
| **US OHLCV** | **yfinance** | 전 미국 | OHLCV | 다년 | 무키(429 백오프) | 유지 |
| **US 펀더멘털·섹터** | **SEC EDGAR** | 전 SEC | 재무 PIT + SIC | 다년 | 무키(UA) | 유지 |
| **매크로·금리·환율** | **FRED** | n/a | 거시 다수 | 장기 | 무키 | 유지 |
| **(P3) KR 수급/소유** | **KRX OpenAPI 투자자별** *또는* pykrx+로그인 | 전 종목 | 외인·기관 순매수 | 2010~(단면) | 10k/일 | P3. 무료 |
| **(P3) 뉴스/감성** | **GDELT 2.0** | 글로벌+**한국어** | 헤드라인+tone | — | 무료 | P3. NAVER 스크래핑 대비 안정 |
| **(P3) US 실적·서프라이즈** | **Finnhub** | 미국 | 캘린더·서프라이즈·추천 | — | 60/분 | P3 |
| **(P3) KR 애널 추정치** | **(보류)** | — | — | — | — | 무료 합법 소스 사실상 없음(FnGuide/WISEfn 유료 독점) |

**검증된 사실 (정정 포함):**
- **KRX OpenAPI = 2010~ 이력은 있으나 호출당 하루치 단면(`basDd`)만** — 기간 조회 불가. "1년" 인상은 이 단면 구조 탓. 전 종목 *오늘 단면*엔 효율적이나 per-symbol 장기 백필엔 비효율 → **OHLCV 메인 소스로 부적합**(FDR 유지).
- **2026-02-27 KRX `data.krx.co.kr` 로그인 의무화** = pykrx·FDR `StockListing`·KRX 통계가 깨진 *진짜* 원인. **per-symbol 가격(FDR DataReader, NAVER)·`openapi.krx.co.kr` REST는 영향 없음.**
- **OpenDART 20,000/일** (공식 에러코드 020 기준; 일부 2차자료의 10k는 stale).
- **OpenDART 이력 floor=2015** → 2015 이전 구간은 KR 펀더멘털 백테스트 불가(정직한 한계).

**폐기/대체:**
- **NAVER 펀더멘털** → 캐노니컬에서 폐기. 스냅샷·3년뿐이라 백테스트 불가 + OpenDART가 최신행으로 스냅샷도 커버(price×최신 equity/shares). *단 OpenDART 백필 동안 임시 스냅샷 유지 여부는 Phase 3에서 결정.*
- **FDR `StockListing`** → 섹터/상장 소스를 data.go.kr/NAVER업종(무로그인)으로 교체.
- **KRX OpenAPI를 OHLCV로** 쓰려던 1안 → 철회(단면 구조).

> **선택지(사용자 입력 1개)**: 무료 `data.krx.co.kr` 계정(KRX_ID/PW)을 주면 **WICS 섹터 + 개별종목
> 일별 PER/PBR 이력**까지 직접 확보(베타/개인용이라 가능). 안 주면 무로그인 경로
> (data.go.kr 상장정보 + NAVER 업종 + OpenDART 계산)로 진행. 기본=무로그인.

## 4. 단계별 계획 (각 Phase = 구현 + 검증 게이트, 작업단위 PR)

> 공통 검증: 골든 14 백테스트 byte 불변, 전체 테스트 0 실패, PR 단위 머지·배포 후 다음.
> 각 Phase는 **기존 API 뒤에서 어댑터로 교체** → 한 번에 안 깨짐.

### Phase 0 — 레지스트리 골격 + 서빙 인터페이스 (행위 무변)
- `data_engine` 모듈: `Registry`(datapoint→source/cadence/fetcher) + `history/snapshot/symbol` 인터페이스.
- 초기엔 기존 `data_cache`/`load_dataset`에 위임(어댑터) — **동작 0 변경**.
- 검증: 전체 테스트 0 실패, 골든 불변(순수 추가).

### Phase 1 — KR 펀더멘털 복구 (최고 레버리지)
- (코드) OpenDART 피드에 **`td`(총부채) 필드 추가** → KR `ev_ebitda·net_debt` 살림.
- (코드) **startup 초기 fetch 스레드** 추가(`_refresh_kr_fundamentals`) → 재배포·갭 후 자동 복구. 최근 분기 우선 백필(스냅샷 빨리 채움).
- (코드) 펀더멘털을 레지스트리 경로로 canonical 적재(엔진·스냅샷 공용).
- (인프라·**사용자**) Railway에 `OPENDART_API_KEY` 설정 + 백필 트리거(증분 며칠).
- 검증: 로컬 OpenDART 키로 삼성전자 `pb_ratio`·`ev_ebitda` non-null. prod: 삼성 360 밸류 표시·"저평가주" 결과 비어있지 않음.

### Phase 2 — 섹터/상장 소스 교체 (FDR `StockListing` 404 근본해결)
- `classification`/`listing`/`krx_cache`의 섹터·상장·상태 소싱을 **data.go.kr 상장종목정보 + NAVER 업종**(무로그인)으로 교체. (옵션: KRX 로그인 제공 시 pykrx WICS.)
- FDR `StockListing` 의존 제거. **OHLCV(FDR DataReader)는 그대로** — 이 Phase는 섹터/상장만.
- 검증: 섹터 복구 → "반도체주" 섹터 필터 동작. 상장/상태 정상. 골든 불변.

### Phase 3 — `snapshot` 뷰 (스크리너 이관, store B 폐기 시작)
- canonical 최신행 파생 + 장중 델타로 `snapshot(cols)` 구축.
- 스크리너를 `krx_cache` 메모리 대신 `snapshot()` 소비로 이관. **NAVER 펀더멘털 의존 제거**(OpenDART 최신행으로 대체).
- 검증: 스크리너 결과가 이관 전후 동일(회귀 스냅샷 비교).

### Phase 4 — `symbol` 뷰 (개별종목분석 통합)
- on-demand 단건을 `symbol(sym)`로: 없으면 즉시 수집→canonical 적재→서빙.
- 개별종목분석(portfolio.py)의 직접 FDR fetch 제거.
- 검증: 상관·벤치마크 분석 결과 동일.

### Phase 5 — P3 modality: 수급 + 뉴스 (무료 우선)
- 수급(KRX 투자자별)·뉴스(GDELT)를 레지스트리에 추가 → canonical 적재.
- 엔진 훅(스키마·러너·연산자) + 360 facet("왜 올랐나"=뉴스, 수급 노출) + 웹(4계층).
- 검증: 360 facet 렌더, 이벤트/수급 study 결과.

### Phase 6 — P3 유료/보류 (예산 결정 후)
- US 실적(Finnhub), KR 추정치(FnGuide/WISEfn) — 비용 승인 시. 그 전엔 스코프 제외(정직).

## 5. 리스크·트레이드오프 (정직)

- **장중 신선도**: `snapshot`은 하루 1회 parquet보다 자주 갱신돼야 함 → 같은 소스 파생의 "장중 델타"
  경로 유지(별도 소싱 아님). 실시간 틱(KIS 선물)은 얇은 실시간 경로 유지하되 canonical로 흘려보냄.
- **메모리/속도**: 백테스트 컬럼 프로젝션(~2GB) vs 스크리너 빠른 top-N — 서빙 뷰가 각각 최적화.
- **OpenDART 2015 floor + 백필 지연**: 2015 이전 KR 밸류 백테스트 불가; 백필 며칠(최근 분기 우선으로 스냅샷부터 점등).
- **외부 키 의존(사용자 액션)**: `OPENDART_API_KEY`(Phase 1), data.go.kr 키 / 옵션 KRX 로그인(Phase 2).
- **규모**: 적잖은 리팩터 → 점진적·API 뒤 교체 필수(빅뱅 금지).

## 6. 첫 착수
**Phase 0(골격, 무변) → Phase 1(KR 펀더멘털 복구)** 부터. Phase 1은 코드(내가) + 키 설정(사용자).
각 Phase 머지·배포·검증 후 다음으로.

---

### 부록 — 근거 출처 (검증 2026-06-08)
- 감사(수요/공급/소비자) + 소스 데이터품질: 본 세션 서브에이전트.
- KRX OpenAPI: openapi.krx.co.kr (2010~ 단면 `basDd`, 10k/일). 2026-02-27 KRX 로그인 의무화 → pykrx #276 · FDR #266.
- FDR `DataReader`(NAVER 백엔드 2000~, 무로그인, 정상) — `StockListing`만 로그인 게이트.
- OpenDART: opendart.fss.or.kr (2015~ 전체재무제표, 20k/일, 에러020).
- yfinance: KR 대형주 OHLCV 깊은 이력 OK·전 종목/펀더멘털 약함. SEC EDGAR(US 무키). GDELT(뉴스 무료·한국어). Finnhub(US 실적). FnGuide/WISEfn(KR 추정치 유료 독점).

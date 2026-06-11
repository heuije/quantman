# 데이터 엔진 〔담당: 조대표〕

> 학습 원장. 작업계획 착수 시 로그 entry(의도·계획)를 추가하고, 완수 시 시행착오·인사이트·결과구현을 채우고 전이 가능한 교훈을 맨 위 §교훈으로 distill한다.

## 📌 교훈·함정 (작업 전 먼저 읽기)

- **데이터포인트당 소스 1개(no-backup).** 출처는 데이터포인트마다 1개 원칙 — fallback 소스를 두지 않는다.
- **새 데이터는 `spec.py`부터.** 지원 현황(present/partial/absent)의 진실원천 = `core/quant_core/data/spec.py`. 새 데이터 추가·상태 확인은 항상 여기서 시작.
- **새 데이터 = `feeds/`에 모듈 1개 추가.** 소스별 수집 모듈은 `core/quant_core/data/feeds/`에 하나씩.
- **Store A ↔ Store B 분리는 의도적**(속도 최적화). 스크리너를 무거운 canonical(Store A)로 라우팅하지 말 것 — 회귀다. 일원화는 *출처(sourcing)*만, *서빙(serving)*은 분리 유지.
- **라이브 데이터는 core 밖 엣지에서.** 뉴스는 결정적 엔진 밖(서버 엣지)에서 붙인다 — 라이브 네트워크·env키 의존을 core에 넣으면 골든 테스트 불변식이 깨진다. 같은 이유로 모든 라이브 데이터는 엣지에서.
- **OpenDART 라벨은 필러마다 다르다 — 정확매칭 금지.** 주식총수 보고서의 보통주 행 `se`가 회사별로 '보통주'/'의결권 있는 주식\n(보통주)'/'의결권있는 주식' 등 제각각 → `==` 매칭은 ~8%를 조용히 놓쳤다(주식수 null→pb 미산출). 공시 텍스트 필드는 정규화(공백·개행 제거) 후 포함 판정으로. (`fundamental_kr._is_common_share`, 2026-06-10)
- **KR 펀더멘털 커버리지 천장 = 실기업 수(~2,608/4,307).** 관리종목의 ~40%(1,699)는 ETF/ETN/특수증권이라 DART corp_code 자체가 없음 — "커버리지 90%" 같은 전체 비율 게이트는 도달 불가. 게이트는 "실기업 pb 표면화율"로 잡을 것.

## 현재 구조 (안정)

**기능.** 백테스트·인사이트가 쓰는 모든 데이터를 한 곳에서 정의·수집·검증해 공급. 출처는 데이터포인트당 **1개 원칙**(no-backup). 지원 현황(present/partial/absent)의 **진실원천 = `core/quant_core/data/spec.py`** — 새 데이터 추가/상태 확인은 여기부터.

**폴더.**
- `core/quant_core/data/spec.py` — 데이터 종류 등록부(진실원천). 무엇을 지원/미지원하는지
- `core/quant_core/data/feeds/` — **소스별 수집 모듈**(새 데이터 = 여기에 모듈 1개 추가): `fundamental_kr.py`(OpenDART)·`fundamental_us.py`(SEC)·`classification.py`(섹터/FDR)·`listing.py`(상장폐지)·`news_kr.py`(뉴스/네이버 검색 API)
- `core/quant_core/data_fetcher.py`(가격 OHLCV: FDR·yfinance)·`indicators.py`(기술지표 24종 자체산출)·`dataset.py`(조립)
- `core/quant_core/data/gate.py`·`deps.py`·`manifest.py` — 무결성 게이트(PIT·생존편향)·의존성 도출·수집 기록
- **서빙(server/app)**: `data_cache.py` = **Store A**(canonical parquet 시계열 — 백테스트·360리포트·SELECT 공급) · `krx_cache.py` = **Store B**(스크리너 인메모리 스냅샷, 빠른 조회 전용) · `naver_fundamentals.py`·`technical_cache.py`·`us_metrics_cache.py`(보조 캐시) · `main.py`(cron 오케스트레이션 — KR 펀더멘털 매일 17:30 KST 등)

**구동 워크플로.** `server/app/main.py`의 cron이 시점마다 `feeds/` 수집 → `data_cache`(Store A)에 적재 → 엔진/리포트가 소비. 스크리너는 별도로 가벼운 `krx_cache`(Store B) 스냅샷을 읽음. 뉴스만 예외 — 사전수집 없이 360 리포트 요청 시점에 `server/app/routers/ir.py`에서 **서버 엣지 on-demand 호출**(저장 안 함).

**현황.**
- **partial(되지만 위험):** 한국 시세 분할조정 표기 미검증 / 시총·종목마스터가 백테스트 store 미부착(스크리너 메모리에만) / 소스별 조정정책 혼재.
- **absent(못 가져옴):** 애널 추정치(무료 불가)·수급(외국인/기관, 공식 무료 API 없음·네이버 일별 순매매는 스크랩 가능하나 엔진 소비경로 미배선)·공매도·KR 지수 멤버십 이력.

## 작업계획 로그 (누적·최신 우선)

### [2026-06-08~] 스크리너 PBR/PER을 OpenDART로 통일 (A1) [진행중 — PR 머지 대기]
- 의도: 스크리너(Store B)의 PBR/PER을 360 리포트·백테스트(Store A)와 같은 OpenDART 출처로 통일해 두 화면의 밸류 불일치를 없앤다. 서빙 분리는 유지(출처만 일원화).
- 계획→경과: `feat/data-engine-a1-valuation`에 구현·로컬검증(2026-06-08) 후 "백필 ~90% 대기"로 보류했으나, **게이트 재해석**(2026-06-10): 90%는 ETF 때문에 도달 불가, 올바른 기준=실기업 pb 표면화 — 아래 shares 수정으로 충족됨. 원 브랜치는 main −146커밋 stale이라 직접 머지 대신 **코드 커밋(742d345)만 최신 main에 cherry-pick**(`feat/a1-valuation-rebase`).
- 시행착오: cherry-pick 충돌 2건은 그 사이 cron 개편(10분 청크+17:30 invalidate 앵커) 탓. materialize 배선을 원안(17:30 앵커)이 아닌 **`_refresh_naver` 끝**으로 이동 — 스냅샷(Store B)은 부팅·15:45마다 통째 재구축되므로 NAVER와 같은 자리(부팅+120s·17:00)에서 채워야 재배포 후 밸류 공백이 없다.
- 결과: A1 테스트 10 green(146커밋 전 작성한 스크리너PBR==describePBR 일관성 테스트 포함)·서버 전체 184 green. PR 생성, 머지·배포는 승인 대기.

### [2026-06-10] KR 펀더멘털 백필 완료 검증 + 주식수 라벨 버그 근본수정 [완료]
- 의도: 백필 진행 점검 → "스크리너/describe에서 실사용 가능한가" 검증.
- 시행착오: ①"1,699개 실종목 false 마킹" 진단은 **오진**(실측: 전부 ETF류, 실기업은 수집돼 있었음 — pb=null을 데이터 부재로 오인). ②진짜 버그는 따로 있었음: 실기업 ~8%(216개, LG전자·SK·신한지주 등)가 **주식총수 `se` 라벨 정확매칭 누락**으로 shares=null→pb 미산출.
- 결과: `_is_common_share` robust 매칭(PR#90)+216개 재수집 마이그레이션→**select pb 30/30 검증**. 임시 진단·마이그레이션 코드(coverage/probe/clear_markers/refetch) PR#92로 전부 제거. 교훈 2건은 §교훈에 distill.

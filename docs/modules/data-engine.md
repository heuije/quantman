# 데이터 엔진 〔담당: 조대표〕

> 학습 원장. 작업계획 착수 시 로그 entry(의도·계획)를 추가하고, 완수 시 시행착오·인사이트·결과구현을 채우고 전이 가능한 교훈을 맨 위 §교훈으로 distill한다.

## 📌 교훈·함정 (작업 전 먼저 읽기)

- **데이터포인트당 소스 1개(no-backup).** 출처는 데이터포인트마다 1개 원칙 — fallback 소스를 두지 않는다.
- **새 데이터는 `spec.py`부터.** 지원 현황(present/partial/absent)의 진실원천 = `core/quant_core/data/spec.py`. 새 데이터 추가·상태 확인은 항상 여기서 시작.
- **새 데이터 = `feeds/`에 모듈 1개 추가.** 소스별 수집 모듈은 `core/quant_core/data/feeds/`에 하나씩.
- **Store A ↔ Store B 분리는 의도적**(속도 최적화). 스크리너를 무거운 canonical(Store A)로 라우팅하지 말 것 — 회귀다. 일원화는 *출처(sourcing)*만, *서빙(serving)*은 분리 유지.
- **라이브 데이터는 core 밖 엣지에서.** 뉴스는 결정적 엔진 밖(서버 엣지)에서 붙인다 — 라이브 네트워크·env키 의존을 core에 넣으면 골든 테스트 불변식이 깨진다. 같은 이유로 모든 라이브 데이터는 엣지에서.

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

### [날짜미상] 스크리너 PBR/PER을 OpenDART로 통일 [진행중]
- 의도: 스크리너의 PBR/PER을 360 리포트와 같은 OpenDART 출처로 통일해 밸류에이션 지표 출처를 일원화한다.
- 계획: 브랜치 `feat/data-engine-a1-valuation`에 구현·로컬검증 완료. **백필 커버리지 ~90% 도달 시 배포**(현재 ~60%, 미배포 대기).

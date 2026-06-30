# 데이터 완결성·커버리지 강화 (Data Completeness & Coverage)

**상태:** Phase 0 착수 (2026-06-29). 담당=조대표. 사장님 승인.
**브랜치:** `feat/data-coverage-manifest` (worktree `_wt-data-coverage`).
**선행 분석:** 코드 감사 + 디스크 실측 + 프로덕션 env/로그 + 라이브 프로브(아래 검증 사실).

---

## 1. 목표 & 원칙

챗봇이 유저 질문에 **편향 없이 정확히** 답하도록, 데이터 수집을 *완결적·일관적*으로 강화한다:
- KR/US 각 유니버스 내에서 **동일 기간·동일 필드·무결손** 데이터셋을 지향.
- 편향의 진짜 원인은 "데이터가 얕은 것"이 아니라 **"없는 걸 0/있는 것처럼 취급"**하는 것 → **null ≠ 0** 보장이 1순위.
- 4원칙 준수: 기존 자산 위에 얹는다(중복 구축 금지). 측정→노출을 먼저, 깊이 백필은 그 다음.

## 2. 승인된 결정 (2026-06-29)

1. **US 유니버스 포함규칙** = 보통주 + ETF만 (워런트/유닛/우선주 제외).
2. **깊이 통일** = 가격·수급·컨센서스 모두 **2010** (단편 조각 배제 — 컨센서스는 2006까지 가능하나 2010로 통일).
3. **착수 순서** = **Phase 0(커버리지 측정+노출) 먼저**, 그 다음 깊이 백필.

## 3. 가용 범위 — 검증된 사실

| 데이터 | KR 가능 깊이 | US 가능 깊이 | 비고 |
|---|---|---|---|
| OHLCV | 2010~ (FDR ~2000; 현 `start="2015-01-01"`는 설정) | 2010~(이미) | 설정 변경 + 1회 소급 백필 |
| 지수·선물·원자재 | 2010~(이미) | 2010~(이미) | 코인만 BTC 2017·FNG 2018 (소스한계) |
| 분기재무 | **2015~** (OpenDART 바닥 — 소스한계) | 2009~(이미, SEC) | 2010~14 KR 재무 무료 불가 |
| 컨센서스 | **2006~** (한경, 프로브 검증) | ❌ 없음 | 시장구조 |
| 수급(flow) | **2010~** (pykrx) | ❌ 직접등가 없음 (대안 13F) | KRX 로그인·봇차단 리스크 |
| 섹터·업종 | 전종목 가능 (KSIC/OpenDART) | 전종목 가능 (SIC/SEC) | 현재 KR 3,269·US 503(S&P500)만 |
| 배당수익률 | 2015~ (DPS) | 깊게 가능 | |
| 외인소진/보유 | 2010~ (KRX) | ❌ 개념 미적용 | |

**프로브 결과(2026-06-29):**
- 한경 컨센서스: 2006/2008/2010/2012 전부 140~160건 반환 → **2006까지 제공**.
- US 유니버스 12,153 vs NASDAQ Trader 공식(보통주 7,462·ETF 5,419): 공식 보통주의 **87.1%(6,503) 커버**, 누락 959는 대부분 워런트/우선주/유닛, 미매칭 231=ADR/stale.

**핵심:** "2015부터"의 거의 전부가 소스한계 아닌 **설정 파라미터**. 진짜 바닥은 KR 분기재무(2015)·US 수급(없음)·코인(2017)뿐.

## 4. 기존 자산 (재사용 — 표면 맵 검증)

| 컴포넌트 | 역할 | file |
|---|---|---|
| `spec.py` REGISTRY | 피드 요구 SSOT — 각 피드 `provides`(필드)·`xs_completeness`·`point_in_time`·`current_status` | `core/quant_core/data/spec.py` |
| `DataManifest`/`SymbolManifest`/`FeedManifest` | 실측 메타 스키마 (per-symbol 기간·feed status·PIT) | `core/quant_core/data/manifest.py` |
| `build_dataset_manifest` | dataset→manifest 빌더. **펀더 컬럼 유무로 feed status 실측(:72-86)** = 필드 커버리지 패턴 존재 | `server/app/data_manifest.py` |
| `evaluate_data_soundness` | 4액션 무결손 게이트 (가용성·결손·조정·PIT·생존편향·워밍업) — 완비 | `core/quant_core/data/gate.py:45` |
| `assess_data_quality` | Phase 0.5 실행 전 품질(missing/stale/gap, 교차심볼) | `core/quant_core/ir_engine/data_quality.py` |
| `classify_status` | 결과 품질 계약 — `diagnostics`에 `coverage`·`stale_symbols` 적재, 모델·웹 소비 | `core/quant_core/ir_engine/result_status.py:31-65` |
| `provenance.py` | 출처 큐레이션(챗 프롬프트·UI) | `core/quant_core/data/provenance.py` |

## 5. 진짜 갭 2개 (Phase 0가 닫는 것)

1. **필드×종목×기간 커버리지 행렬 부재** — `SymbolManifest`는 가격 행수만. 펀더/플로우/컨센서스의 종목별 first/last/밀도 없음.
2. **챗 경로 게이트 미배선** — `chat/tools.py:300,356,479`의 `strategy_from_spec(ir, dataset)`가 `manifest=` 미전달 → 게이트·커버리지가 챗 결과에 안 흐름(IR 라우터 `ir.py:254-256`은 전달 — 비대칭).

---

## 6. Phase 0 — 커버리지 측정 + 노출 (상세)

> 원칙: 측정→노출. 프로덕션 데이터 *수집*은 안 바꾼다(자금/외부상태 영향 0). 순수 메타·결과계약만.

### 0a. 필드 커버리지 (manifest 확장)
- `SymbolManifest`에 필드별 커버리지 추가: `field_coverage: dict[field -> {first, last, n}]`.
  관측 가능한 필드(예: `pb_ratio`,`trailing_pe`,`shares_outstanding`,`inst_net_buy`,`foreign_net_buy`,`consensus_target` …)에 대해 dataset의 각 심볼 DataFrame에서 비결측 구간 first/last/count 산출.
- `build_dataset_manifest`(`data_manifest.py`)에서 산출 — 이미 펀더 컬럼 유무를 보는 :72-86 패턴을 *기간*까지 확장.
- 필드↔피드 매핑은 `spec.py`의 `provides`를 권위로 사용(신규 매핑 만들지 않음).

### 0b. 챗 게이트 배선 (비대칭 해소)
- `chat/tools.py:300,356,479`의 `strategy_from_spec(ir, dataset)` → `strategy_from_spec(ir, dataset, manifest=build_dataset_manifest(dataset))`.
- 그러면 `evaluate_data_soundness` 이슈가 `service.py:172` 경로로 `warnings`에 병합되고, `serialize.py`가 보존, `classify_status`가 진단 승격 — IR 라우터와 동일 계약.

### 0c. 결과계약 노출 (null ≠ 0)
- `classify_status`(`result_status.py:59`)의 `diagnostics`에 `field_coverage` 요약 추가:
  질의가 사용한 필드별로 `{covered_symbols, total_symbols, period}` → 모델 식단(`_status_header`)·웹 `ChatResultView`가 단일 계약으로 자동 노출.
- 목적: 챗봇이 "이 필드는 N/M 종목만·기간 X~Y" 를 *알고* 답 → 결손을 0으로 오해 불가.

### 0d. 전역 커버리지 리포트 (백필 기준선)
- 유니버스 전체의 (필드 × 커버리지%·기간) 요약을 산출하는 함수/CLI — Phase 2 백필 진행률 추적·Phase 3 무결손 SLA의 측정 기반.
- 무겁지 않게: per-symbol field_coverage 집계. (영속화는 `save_manifest` 기존 함수 재사용, 필요 시.)

### 검증 게이트 (Phase 0 완료 기준)
- core 단위테스트: field_coverage 산출 정확성(합성 dataset으로 first/last/n), spec.provides 매핑 일치.
- server 테스트: 챗 경로가 게이트 issue를 결과에 싣는지(전/후), `diagnostics.field_coverage` 존재.
- 회귀 0: 기존 core/server/golden 스위트.
- 라이브(로컬 $0): describe/select 1회 → `field_coverage`가 실제 결손(예 flow=null) 표면화 확인.

---

## 7. Phase 1~4 (로드맵)

- **P1 유니버스 고정**: KR=KRX 전종목(유지). US=NASDAQ Trader 디렉터리 권위 채택→보통주+ETF 규칙→12,153 reconcile(stale 231 제거·진짜 보통주 편입).
- **P2 깊이 백필**(`*/N분` 청크+예산+마커, 비차단): KR OHLCV/수급/컨센서스 2010 소급, KR 외인보유 신규, KR 재무 deepv 완주(2015~).
- **P3 무결손 검증**: 코어 패널(KR 2015 all-fields / US 2010) gap 탐지→자동 재수집·표면화 (P0 커버리지 게이트 위에서).
- **P4 갭 필드**: US 시총 이력화(shares×price), 섹터 전종목(KSIC/SIC), US 13F 수급 대안.

## 8. 충돌/협업 노트
- `#243 financials-fill`(dart-fss 5개년) = **P2 재무**에서만 겹침 → 그때 협의.
- `_wt-data-engine`(detached, stale)이 `spec.py`·`classification.py` diff 보유 — P0에서 `spec.py` 편집 시 재확인.
- 데이터엔진=조대표 단독 담당이라 희제 충돌 없음. push·머지·프로덕션 배포는 사장님 명시 허락 후.

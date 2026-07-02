# 챗봇 데이터 커버리지 인지 — 설계 스펙

> 목표: 챗봇(챗 LLM + NL→IR 컴파일러)이 **업데이트된 데이터 엔진의 커버리지를 정확히 인지하고 100% 활용**한다.
> 원칙: 하드코딩 신규 카탈로그 금지 — **기존 SSOT 최신화 + SSOT 파생 + 드리프트 가드**. 코어 엔진 로직 무변경(프롬프트·카탈로그·검증 계층만).

## 1. 문제 (공급 ≫ 소비 갭)

데이터 엔진에 P2-B(공식 KRX API 매크로: V-KOSPI·옵션풋콜비율·KRX채권지수·국고채3/10년·선물 미결제약정·ETF AUM/순자금유입)와 S4(선물 만기물 패널·롤)를 **공급**했으나, 챗봇 LLM의 **지식(소비) 계층**이 따라가지 못한다.

### 실측 갭 (origin/main)
| 갭 | 위치 | 실측 |
|---|---|---|
| 새 매크로 미인지 | `DATA_PROVENANCE`(provenance.py) 매크로 = "FRED+yfinance"만 | macro.krx 8종 **부재** |
| stale 출처 | `DATA_PROVENANCE` 가격 = "KR선물=Investing.com+KIS" | S4가 **KRX 공식 API 패널**로 교체 → 오정보 |
| 컴파일러 심볼 미배선 | `ir_compiler._system_prompt(catalog, capabilities, indicator_cols)` | 지표는 열거·**심볼(valid_keys) 프롬프트 미포함**(사후검증만) → 매크로 크로스에셋 참조 불가 |
| 커버리지 뎁스 미노출 | 챗 프롬프트 | 보유 데이터의 **검증된 실측 뎁스**를 사전 안내 못 함 |

소비 파이프의 SSOT `get_all_indicator_columns()`(BASE+FUND+FLOW+CONSENSUS)는 지표·펀더·수급·컨센서스는 태우나 **매크로 심볼·커버리지 뎁스는 안 태운다** — 갭의 구조적 뿌리.

## 2. 설계 원칙 — 화이트리스트(보유 인벤토리) 기반

미지원을 열거(블록리스트)하지 않는다(무한·유지불가). 대신 **보유 데이터 + 검증 뎁스를 정확히 인지 → 인벤토리에 없으면 정의상 미지원**. null≠0 원칙을 커버리지 *인지* 수준에 적용.

**coverage 뎁스는 하드코딩 문자열이 아니라 데이터 엔진이 실측·검증한 정보다.**

## 3. 커버리지 정본 소스 — 검증 매니페스트 (이미 존재)

데이터 엔진이 이미 검증 커버리지를 생산한다(P0). 이걸 챗에 배선만 안 했을 뿐:

- `build_manifest(dataset, track_fields=…)` (core/quant_core/data/manifest.py): 실제 parquet에서 산출
  - per-symbol: `first`/`last`/`n_rows` (가격·매크로 심볼의 실측 뎁스)
  - per-field: `field_coverage[field] = {first, last, n}` (펀더/플로우/컨센서스 sparse 가용성) — docstring: *"챗봇이 결손을 0으로 오해 못 하게(null≠0)"*
- `coverage_report(manifest)` → `{field: {covered, total, pct, first, last}}` 글로벌 집계
- `_manifest.json` 사이드카로 영속(`default_manifest_path`), cron이 parquet 갱신 시 재빌드 → **백필 중이면 실제 현재 뎁스 반영**(가짜 "2010~" 아님)

## 4. 설계

### A. 데이터 인벤토리(화이트리스트)를 챗 LLM에 주입 — 검증 매니페스트 파생

1. **데이터-유형 커버리지 롤업** (manifest.py에 신규, 데이터 엔진 계층):
   `coverage_inventory(manifest, spec_registry, symbol_groups) -> list[{key, label, depth(first~last), symbols(n), pct, source}]`
   - 심볼 → 데이터-유형 매핑: `data_spec()` 레지스트리 + `MACRO_*_SYMBOLS`/`ASSET_SYMBOLS` 그룹(data_fetcher) + pclass.
   - 유형별 실측 뎁스 = 그 유형 심볼들의 min(first)~max(last)(가격·매크로) 또는 `coverage_report` pct/range(sparse 필드).
   - 매크로는 그룹 요약 + 주요 심볼 개별 뎁스(상세).
2. **챗 프롬프트 주입** (`chat/prompt.py:chat_system_prompt`): `available_themes()`·`capability_spec()` 옆에 컴팩트 렌더. `_manifest.json` 로드(없으면 graceful — 인벤토리 생략).
3. **LLM 지시(핵심)**: *"아래는 보유 데이터의 완전한 검증 인벤토리다(엔진 실측). 여기 없는 데이터·기간은 갖고 있지 않다 — 지어내지 말고 미지원이라 정직히 답하라."*

→ 효과: ① macro.krx 자동 포함 ② stale "KR선물" 자동 정정(매니페스트 정본) ③ 블록리스트 불필요 ④ 백필 진행에 따라 뎁스 자동 갱신.

### B. 컴파일러 심볼 카탈로그 (활용) — MACRO_SYMBOLS SSOT 파생

1. `ir_compiler._system_prompt`에 `<reference_data>` 심볼 섹션 추가(시그니처에 `symbols` 인자): `MACRO_SYMBOLS`에서 파생한 "크로스에셋 참조 가능 심볼" 목록(심볼키). 지표 열거와 동형.
2. `compile_service`: `symbols`를 `compile_nl`에 전달 + `name_map`에 **매크로 별칭** 병합(풋콜비율→옵션풋콜비율·변동성지수→코스피200변동성지수·국고채금리→국고채3년 등).

→ 컴파일러가 `옵션풋콜비율.pct_change_1d` 같은 크로스에셋 신호를 정확히 생성.

### C. 드리프트 가드 (부류 차단) — test_capability_coverage.py 패턴 미러

신규 `core/tests/test_data_coverage_surface.py`: 모든 `MACRO_SYMBOLS`가 ① `data_spec()` 엔트리에 매핑되고 ② 컴파일러 심볼 카탈로그에 포함되는지 단언. → 데이터 추가 시 표면 미배선이면 CI 실패.

## 5. 검증 ($0)

- **analysis_diag**(core, LLM-free): 코퍼스에 매크로 참조 IR("옵션풋콜비율 높을 때 코스피 수익") 추가 → 컴파일·실행·shape green.
- **chat_eval**(구독 `claude -p`, API $0): "V-KOSPI 급등 시 전략?" → 컴파일된 IR이 실제 매크로 심볼 참조하는지 + "선물 투자자수급 있어?"에 미지원 정직 답변 채점.
- **드리프트 가드 테스트** green.
- **로컬 시각 테스트환경**(docs/dev-testenv.md): e2e.

## 6. 파일 변경

| 파일 | 변경 |
|---|---|
| `core/quant_core/data/manifest.py` | `coverage_inventory()` 롤업 신규 |
| `core/quant_core/data/provenance.py` | 인벤토리와 정합(매크로 KR·stale 수정) 또는 인벤토리로 대체 |
| `server/app/chat/prompt.py` | 인벤토리 주입 + "그 외 미지원" 지시 |
| `server/app/ir_compiler.py` | `_system_prompt` 심볼 카탈로그 섹션 |
| `server/app/compile_service.py` | `symbols` 전달 + `name_map` 매크로 별칭 |
| `core/quant_core/data_fetcher.py` | (필요시) 심볼→유형/라벨 그룹 노출 헬퍼 |
| `core/tests/test_data_coverage_surface.py` | 드리프트 가드(신규) |
| `core/tests/analysis_corpus.py` + `scripts/chat_eval` | 검증 케이스 |

## 7. 트레이드오프

- coverage 뎁스 = **매니페스트 실측**(엔진 검증) — data_spec 하드코딩 문자열 대비 정확·자동갱신. 매니페스트 미존재(콜드) 시 인벤토리 graceful 생략(프롬프트 무결).
- DATA_PROVENANCE(큐레이션 출처 문구)와 인벤토리(실측 뎁스)의 역할 분리 유지 — 드리프트는 가드 테스트가 차단.
- 토큰: 인벤토리 컴팩트(유형별 1줄·~25줄)·프롬프트 캐싱.

## 8. 구현 순서

1. `coverage_inventory()` (manifest, 코어) + 단위테스트.
2. 챗 프롬프트 주입 + "그 외 미지원" 지시 (A).
3. 컴파일러 심볼 카탈로그 + name_map (B).
4. 드리프트 가드 (C).
5. provenance 정합/정정.
6. 검증: analysis_diag 코퍼스 + chat_eval + 로컬 시각.

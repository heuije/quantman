# 미니 코스피200선물 자동매매 추가 — 설계서

**날짜:** 2026-06-30
**브랜치:** `feat/mini-kospi200-futures` (origin/main = bfed6a2, Phase 1 capability parity 직후)
**목표(한 줄):** 기존 선물 라인업에 **미니 코스피200선물**(승수 50,000 = 정규 1/5, 동일 KOSPI200 지수)을
정규와 동일 패턴으로 추가해, **실전 자동매매 검증 비용을 ~900만→~180만/계약으로** 낮춘다.

## 동기 / 범위 경계

- 사장님 1순위 = **S&P500 역추종 코스피200선물 실전 자동매매가 정상 작동하는지 최대한 빠르게 검증**.
  정규 코스피200선물은 실전 1계약 증거금이 ~900만이라 부담 → 동일 지수를 1/5 승수로 추종하는 **미니**를 추가.
- **본 작업 범위:** 미니 코스피200선물(국내선물 = `kr_futures`)을 백테스트·빌더·데이터·자동매매 4계층에 추가.
- **범위 밖(명시 제외):**
  - 해외선물(CME, `us_futures`) — Phase 2 별건. 본 작업 무관.
  - **선물 분석 대시보드 카드**(`server/app/futures_config.py`) — 자동매매와 무관. 추가하지 않음(4원칙: over-engineering 금지).
  - 미니 옵션 — 대상 아님.
- **불변식(절대 보존):** **정규 코스피200선물 경로를 byte 단위로 보존.** 미니는 정규 *옆에 추가*되는 것이지
  정규를 *바꾸는* 게 아니다(정규는 Phase 1에서 막 배포·곧 실전 검증할 경로).

## 핵심 발견 — "thin"한 곳 vs "진짜 작업"인 곳

그라운딩(2026-06-29 origin/main 실코드)으로 각 레이어가 정확히 얼마나 바뀌는지 확정:

| 레이어 | 변경 정도 | 근거 |
|---|---|---|
| 카탈로그(`exec_defaults._INSTRUMENTS`) | **1줄 추가** | `instrument_spec`이 SSOT, 엔진이 `.multiplier`를 동적 사용 |
| 백테스트 손익 | **무변경(자동 1/5)** | `engine.py:906` `mult={s: instrument_spec(s).multiplier}` → 손익 = qty×Δ×mult |
| 게이트/capability | **무변경** | `instrument_category`가 currency(KRW)+asset_class(futures)=`kr_futures` 자동 |
| 데이터 | **alias 1곳** | 동일 지수 → 정규 parquet 공유(단일소스). 별도 수급 안 함 |
| 빌더 노출 | **등록 필요** | 빌더 목록 = `dataset_symbol_index()` = `names` 중 parquet 보유. is_futures만으론 목록에 안 들어옴 |
| **계약 해석(주문 라우팅)** | **진짜 변경** | 정규=유일 국내선물 가정이 4군데 박힘 — 미니까지 일반화(아래) |
| 로컬앱 버전/릴리스 | 버전 bump + 빌드 | 자동매매는 로컬앱에서 실행 → 재배포·사장님 앱 업데이트 필요 |

**브로커 주문 필드는 심볼 불문 통과**(KIS `SHTN_PDNO`, LS `FnoIsuNo`) — 계약코드(A05xxx)만 제대로
만들어내면 발주는 자동. 승수는 주문이 아니라 엔진 사이징에 반영(qty=계약수).

## 계약 해석 — 정규 가정이 박힌 4곳 (미니까지 일반화)

미니의 브로커 단축코드는 정규(A01xxx)와 **다른 A05xxx**다. 마스터 라인: 정규 `1A01606 … F 202606 … KOSPI200`,
미니 `BA05606 … 미니F 202606 … KOSPI200`. 정규=라인 첫 글자 `1`, 미니=`B`.

| # | 위치 | 현재(정규 전용) | 일반화 방향 |
|---|---|---|---|
| 1 | `core/quant_core/futures_contract.py:61` (정방향) | `not line.startswith("1")` | 상품별 root char(정규"1"/미니"B") 파라미터화 |
| 2 | `core/quant_core/futures_contract.py:146` (역방향 `dataset_for_contract`) | `^A\d → 정규` | `A01→정규`, `A05→미니` 정밀 분기 |
| 3 | `local/localapp/ls_futures_contracts.py:73` (LS 정방향) | `sh.startswith("A01")` | 상품별 분기(A01/A05) |
| 4 | `local/localapp/ls_futures_contracts.py:143` (LS 역방향) | `code.startswith("A01") → 정규` | `A01→정규`, `A05→미니` |

**단일 출처(DRY):** 두 국내선물의 (마스터 root char, 단축코드 prefix)를 한 곳에 정의:

```python
# futures_contract.py — 국내선물 상품별 식별자(정규/미니). 새 국내선물은 여기 한 줄.
_DOMESTIC_SPEC = {
    "코스피200선물":     ("1", "A01"),   # (마스터 라인 첫 글자, 단축코드 prefix)
    "미니코스피200선물": ("B", "A05"),
}
_DOMESTIC = tuple(_DOMESTIC_SPEC)          # 기존 _DOMESTIC 대체
```

정방향 `_front_domestic`은 `root_char` 인자를 받아 `line.startswith(root_char)`로 필터(기본 "1" → 정규
호출 byte-identical). `resolve_contract`/`front_contract`는 `_DOMESTIC_SPEC[symbol][0]`을 주입.
역방향 `dataset_for_contract`는 prefix(`A01`/`A05`)로 정규/미니 분기. LS resolver도 `resolve(symbol)`이
symbol로 A01(정규)/A05(미니) front를 분기.

⚠ **미검증(사장님 실전/모의 검증이 확정):** KIS·LS가 실제로 A05 미니 주문을 수용하는지 KB에 명시 없음.
코드는 "가능하게" 만들고, **수용 여부는 LS 모의→실전에서 확인**(이게 사장님의 검증 목표 그 자체).

## 데이터 설계 — alias(단일소스)

미니·정규는 **동일 KOSPI200 지수(동일 호가 포인트)**. 손익차는 오직 승수(50k vs 250k). 따라서 미니는
**별도 가격 데이터를 갖지 않고 정규 시리즈를 공유**한다(원칙: 데이터포인트당 소스 1개).

```python
# data_fetcher.py
PRICE_ALIAS = {"미니코스피200선물": "코스피200선물"}   # 동일 지수 — 정규 시리즈 공유(별도 수급 안 함)
```

- `_parquet_path(symbol)`: 진입부에서 `symbol = PRICE_ALIAS.get(symbol, symbol)` → 미니 조회가 정규 parquet로.
  (정규는 키가 아니므로 무변경. 미니는 어떤 수급 리스트에도 없어 `_save(미니)`는 호출되지 않음.)
- `dataset_symbol_index()`의 `names`에 `+ list(PRICE_ALIAS)` → 미니가 (정규 parquet 보유 덕에) 인덱스 포함
  → 빌더 선물탭에 노출(`is_futures`로 asset_class=futures, autotrade_hint=ok).
- `SYMBOL_CATEGORY`: `"미니코스피200선물": "자산"`(비-마스터 카테고리 폴백).
- **수급 파이프라인(ASSET_SYMBOLS/CSV_SEEDED_FUTURES)에는 추가하지 않음** — 미니는 정규 데이터를 빌릴 뿐.

## 자동매매 사이징 — 라이브 시세

자동매매 사이징(preview)이 미니 현재가를 정규 지수와 동일 소스로 받도록 `server/app/live_quote.py`
`_KR_FUTURES_MAP`에 미니 → 동일 FUT 소스 추가(필요 시). 승수(50k)는 instrument_spec에서 자동 반영되어
**더 적은 자본으로 1계약** = 더 싼 증거금.

## 변경 파일 요약

**core/** (`pip install -e` → 로컬앱·서버 공유)
- `quant_core/exec_defaults.py` — 미니 InstrumentSpec 1줄.
- `quant_core/data_fetcher.py` — PRICE_ALIAS + `_parquet_path` alias + `dataset_symbol_index` names + SYMBOL_CATEGORY.
- `quant_core/futures_contract.py` — `_DOMESTIC_SPEC`, `_front_domestic(root_char)`, resolve/front dispatch, `dataset_for_contract` 분기.

**local/** (사용자 PC 자동매매 실행)
- `localapp/ls_futures_contracts.py` — LS 정/역방향 미니 분기.
- (검증) `localapp/broker_router.py`·`trader.py` — 심볼 통과 확인(코드 변경 없을 가능성 큼).
- `localapp/__init__.py` — `__version__` 0.9.56→0.9.57.
- `RELEASE_NOTES_v0.9.57-beta.md` (신규).

**server/**
- `app/live_quote.py` — `_KR_FUTURES_MAP` 미니 엔트리(사이징용, 필요 시).

**무변경(명시):** 게이트(`autotrade_caps_api`·`strategies.py`)·capability SSOT·웹(빌더 자동노출)·
`futures_config.py`(대시보드)·`server/main.py`(수급).

## 검증 계획 (검증된 해결책만)

- **core 단위테스트:** 미니 spec(multiplier 50_000·kr_futures); `_front_domestic` 정규 root"1" 회귀 + 미니 root"B"
  → A05 해석; `dataset_for_contract` A01→정규/A05→미니; 데이터 alias(미니 인덱스 포함·정규 parquet 로드).
- **백테스트 골든:** 동일 전략을 정규 vs 미니 → 미니 손익 = 정규 × 1/5 (승수만 차이) 확인.
- **게이트:** `asset_class_for_symbol("미니코스피200선물")=="kr_futures"`, paper/live 허용.
- **LS resolver 테스트:** symbol→A01(정규)/A05(미니) 분기, 역매핑 분기.
- **웹:** `npm run build` 통과 + 빌더 선물탭에 미니 노출(코드 변경 없음 — 데이터 alias 효과 확인).
- **정규 무손상:** 기존 `test_futures_contract.py`·`test_futures_config.py`·게이트 테스트 전부 그대로 green.
- ⚠ **불가(사장님 영역):** 실제 KIS/LS 미니 주문 수용·체결 = LS 모의→실전 라운드트립. 코드는 경로를
  열어두되 "실전 수용 미검증"으로 보고.

## 미검증·가정 (정직)

- KIS/LS가 A05 미니 단축코드 주문을 수용하는지 **미검증**(KB 침묵) — 실전/모의 검증 항목.
- LS t8467 마스터에 미니(A05) 행이 포함되는지 **미검증** — 모의 검증 항목.
- 미니 마스터 라인이 `startswith("B") + KOSPI200 + "F YYYYMM"`로 유일 식별된다고 가정(옵션은 `F (\d{6})`
  가드가 이미 배제). 실제 `fo_idx_code.mst`에서 구현 중 재확인.
- 정규·미니 지수 호가가 동일하다는 가정(베이시스 차이는 무시 가능 수준).

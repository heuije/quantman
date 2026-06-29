# 미니 코스피200선물 자동매매 추가 — 구현 플랜

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** 기존 선물 라인업에 미니 코스피200선물(승수 50,000=정규 1/5, 동일 KOSPI200 지수)을 정규와 동일 패턴으로 추가 — 백테스트·빌더·데이터·자동매매 4계층.

**Architecture:** 정규=유일 국내선물 가정을 미니까지 일반화하되 **정규 경로는 byte 보존**. 데이터는 정규 시리즈 alias(단일소스). 설계서: `docs/superpowers/specs/2026-06-30-mini-kospi200-futures-design.md`.

**Tech Stack:** Python(core·local·server pytest), React(web npm build — 변경 없음, 노출만 확인).

**불변식:** ① 정규 테스트 전부 green 유지 ② 정규 resolve/사이징/손익 무변경 ③ 미니는 정규 데이터 alias(별도 수급 0) ④ KIS/LS 미니 주문 수용은 미검증→실전 검증 항목(코드는 경로만 연다).

**테스트 명령:**
- core: `cd core && python -m pytest tests/<file> -v`
- server: `cd server && PYTHONPATH=../core python -m pytest tests/<file> -v`
- local: `cd local && python -m pytest tests/<file> -v`
- web: `cd web && npm run build`

---

### Task 1: 카탈로그 — 미니 InstrumentSpec (core)

**Files:**
- Modify: `core/quant_core/exec_defaults.py:140` (`_INSTRUMENTS`에 미니 추가)
- Test: `core/tests/test_exec_defaults.py` (없으면 생성)

- [ ] **Step 1: 실패 테스트**

```python
# core/tests/test_exec_defaults.py
from quant_core.exec_defaults import instrument_spec, is_futures, instrument_category

def test_mini_kospi200_spec():
    sp = instrument_spec("미니코스피200선물")
    assert sp.asset_class == "futures"
    assert sp.multiplier == 50_000.0          # 정규 250,000의 1/5
    assert sp.currency == "KRW"
    assert sp.tick == 0.05                     # 정규와 동일 호가단위
    assert is_futures("미니코스피200선물")
    assert instrument_category("미니코스피200선물") == "kr_futures"

def test_regular_kospi200_unchanged():
    sp = instrument_spec("코스피200선물")
    assert sp.multiplier == 250_000.0          # 정규 무손상
```

- [ ] **Step 2: 실패 확인** — `cd core && python -m pytest tests/test_exec_defaults.py -v` → FAIL(미니 미등록 → equity 폴백).

- [ ] **Step 3: 구현** — `exec_defaults.py` `_INSTRUMENTS`에서 `"코스피200선물"` 줄 바로 아래 추가:

```python
    "미니코스피200선물": InstrumentSpec("futures",  50_000.0, 0.05, "KRW", 0.10, 0.075, "kospi200_2nd_thu", "days_before:5"),
```

(정규와 승수만 다름: 250,000→50,000. 나머지 동일 — 같은 지수·만기·증거금률.)

- [ ] **Step 4: 통과 확인** — 위 테스트 PASS + 기존 `core/tests` 전체 green.

- [ ] **Step 5: 커밋** — `git add core/quant_core/exec_defaults.py core/tests/test_exec_defaults.py && git commit -m "feat(core): 미니 코스피200선물 InstrumentSpec(승수 50k) 추가"`

---

### Task 2: 데이터 alias — 정규 시리즈 공유 + 빌더 노출 (core)

**Files:**
- Modify: `core/quant_core/data_fetcher.py` (PRICE_ALIAS 신규 · `_parquet_path:179` · `dataset_symbol_index:1213` names · `SYMBOL_CATEGORY:144`)
- Test: `core/tests/test_data_alias.py` (신규)

**배경:** 빌더 목록 = `dataset_symbol_index()` = `names`(ALL_SYMBOLS+사용자) 중 parquet 보유 심볼. 미니는 동일 지수라 별도 데이터 없음 → 정규 parquet를 alias. names엔 등록만(수급 리스트엔 추가 안 함).

- [ ] **Step 1: 실패 테스트**

```python
# core/tests/test_data_alias.py
import pandas as pd
from quant_core import data_fetcher as df

def test_mini_parquet_path_aliases_to_regular():
    # 미니 조회는 정규 parquet 파일을 가리킨다(동일 시리즈 공유)
    assert df._parquet_path("미니코스피200선물") == df._parquet_path("코스피200선물")

def test_regular_path_unchanged():
    assert df._parquet_path("코스피200선물").name == "코스피200선물.parquet"

def test_mini_in_symbol_category():
    from quant_core.exec_defaults import symbol_category
    assert symbol_category("미니코스피200선물") == "자산"

def test_mini_in_index_names(monkeypatch, tmp_path):
    # 정규 parquet가 있으면 미니도 인덱스에 (alias 덕에) 등장
    p = df._parquet_path("코스피200선물")
    if not p.exists():
        import pytest; pytest.skip("정규 parquet 부재 환경")
    idx = df.dataset_symbol_index()
    assert "미니코스피200선물" in idx
    assert idx["미니코스피200선물"]["has_ohlc"] is True
```

- [ ] **Step 2: 실패 확인** — `cd core && python -m pytest tests/test_data_alias.py -v` → FAIL.

- [ ] **Step 3: 구현** — `data_fetcher.py`:

`CSV_SEEDED_FUTURES` 근처에 추가:
```python
# 미니 코스피200선물 = 정규와 동일 KOSPI200 지수(동일 호가 포인트). 가격 데이터는 정규
# 시리즈를 공유한다(별도 수급 안 함) — 손익차는 엔진 승수(50k vs 250k)에서만 발생.
PRICE_ALIAS = {"미니코스피200선물": "코스피200선물"}
```

`_parquet_path` 진입부:
```python
def _parquet_path(symbol: str) -> Path:
    symbol = PRICE_ALIAS.get(symbol, symbol)   # 미니→정규 시리즈 공유
    return DATA_DIR / f"{symbol.replace('/', '_')}.parquet"
```

`dataset_symbol_index`의 `names`:
```python
    names = (list(ALL_SYMBOLS)
             + list(PRICE_ALIAS)               # alias 심볼(미니)도 인덱스 후보 — parquet은 정규 공유
             + [s["name"] for s in load_user_stocks()]
             + load_managed_kr_codes()
             + [s["code"] for s in load_managed_overseas()])
```

`SYMBOL_CATEGORY`에 `"미니코스피200선물": "자산",` 추가(`"코스피200선물": "자산"` 옆).

- [ ] **Step 4: 통과 확인** — 테스트 PASS + 기존 `core/tests/test_futures_data.py` 등 green.

- [ ] **Step 5: 커밋** — `git commit -m "feat(core): 미니 코스피200선물 데이터 alias(정규 시리즈 공유)+빌더 노출"`

---

### Task 3: 계약 해석 — 정규 가정 일반화 (core, 정규 byte 보존)

**Files:**
- Modify: `core/quant_core/futures_contract.py` (`_DOMESTIC:39` · `_front_domestic:47` · `parse_front_month_domestic:74` · `resolve_contract:160` · `front_contract:184` · `dataset_for_contract:129`)
- Test: `core/tests/test_futures_contract.py` (기존 — 추가)

- [ ] **Step 1: 실패 테스트** (기존 파일에 추가; 기존 정규 테스트는 그대로 둔다)

```python
from quant_core.futures_contract import (parse_front_month_domestic,
    resolve_contract, dataset_for_contract)
from datetime import date

# 정규+미니 동시 포함 마스터 샘플
_DOM2 = "\n".join([
    "1A01606   KR4A01660005F 202606                  00000.0012001     KOSPI200",
    "1A01609   KR4A01690002F 202609                  00000.0022001     KOSPI200",
    "BA05606   KR4A05660001미니F 202606              00000.0012001     KOSPI200",
    "BA05609   KR4A05690008미니F 202609              00000.0022001     KOSPI200",
    "2A02606   KR4A02660003C 202606  300.0           00000.0012001     KOSPI200",
])

def test_regular_still_resolves_a01():            # 정규 무손상(회귀)
    assert parse_front_month_domestic(_DOM2, date(2026, 6, 7)) == "A01606"

def test_mini_resolves_a05():                     # 미니 근월물
    assert resolve_contract("미니코스피200선물", date(2026, 6, 7),
                            domestic_master=_DOM2) == "A05606"

def test_dataset_for_contract_splits_regular_mini():
    assert dataset_for_contract("A01606") == "코스피200선물"
    assert dataset_for_contract("A05606") == "미니코스피200선물"
```

- [ ] **Step 2: 실패 확인** — `cd core && python -m pytest tests/test_futures_contract.py -v` → 새 테스트 FAIL.

- [ ] **Step 3: 구현** — `futures_contract.py`:

`_DOMESTIC` 정의를 단일출처 spec으로 교체:
```python
# 국내 선물(KRX) 상품별 식별자 — (마스터 라인 첫 글자, 단축코드 prefix). 새 국내선물은 여기 한 줄.
_DOMESTIC_SPEC: dict[str, tuple[str, str]] = {
    "코스피200선물":     ("1", "A01"),
    "미니코스피200선물": ("B", "A05"),
}
_DOMESTIC = tuple(_DOMESTIC_SPEC)
```

`_front_domestic`에 `root_char` 인자(기본 "1" → 정규 호출 byte-identical):
```python
def _front_domestic(master_text: str, today: date,
                    lead_days: int = 0, root_char: str = "1") -> tuple[date, str] | None:
    ...
    for line in master_text.splitlines():
        if "KOSPI200" not in line or not line.startswith(root_char):
            continue
        ...
```

`parse_front_month_domestic`도 `root_char` 통과. `resolve_contract`/`front_contract`의 국내 분기:
```python
    if symbol in _DOMESTIC:
        if not domestic_master:
            return None
        root_char = _DOMESTIC_SPEC[symbol][0]
        return parse_front_month_domestic(domestic_master, today, lead, root_char=root_char)
        # front_contract도 _front_domestic(..., root_char=root_char) 동일 주입
```

`dataset_for_contract` 국내 분기(정밀):
```python
    for sym, (_root, prefix) in _DOMESTIC_SPEC.items():
        if code.startswith(prefix):
            return sym
    # (기존 ^A\d → 정규 단일 매핑을 prefix 분기로 대체; A01→정규, A05→미니)
```

- [ ] **Step 4: 통과 확인** — 새+기존 테스트 전부 PASS(특히 `test_domestic_excludes_mini_and_options`는 정규 해석이라 그대로 green; 미니는 별도 symbol로 명시 호출 시에만 해석).

- [ ] **Step 5: 커밋** — `git commit -m "feat(core): 계약 해석을 미니까지 일반화(정규 root\"1\" 보존·미니 root\"B\")"`

---

### Task 4: LS 계약 해석 미니 분기 (local)

**Files:**
- Modify: `local/localapp/ls_futures_contracts.py` (`_pick_front_kospi200:65` 정방향 · `dataset_for_code_static:141` 역방향 · `resolve` dispatch)
- Test: `local/tests/test_ls_contract_resolver.py` (기존 — 추가)

**배경:** LS resolver는 코어와 별개로 t8467 마스터를 파싱하며 정규(A01)만 해석. `resolve(symbol)`이 symbol로 정규/미니를 분기해야 한다(LS shcode: 정규 A01, 미니 A05 가정 — 실전/모의 검증 항목).

- [ ] **Step 1: 실패 테스트** — 기존 LS resolver 테스트 패턴에 맞춰, 정규+미니 행 포함 t8467 모의 마스터로 `resolve("코스피200선물")→A01…`, `resolve("미니코스피200선물")→A05…`, `dataset_for_code_static("A05…")→"미니코스피200선물"` 검증. (기존 정규 테스트 보존.)

- [ ] **Step 2: 실패 확인** — `cd local && python -m pytest tests/test_ls_contract_resolver.py -v` → FAIL.

- [ ] **Step 3: 구현** — `_pick_front_kospi200`을 상품별(정규 A01 / 미니 A05) 분기로 일반화하고 `resolve(symbol)`이 symbol에 따라 prefix를 고르도록. `dataset_for_code_static`을 A01→정규/A05→미니로. **정규 분기 동작 보존**. (실제 LS 미니 shcode prefix는 코어 `_DOMESTIC_SPEC`과 정합되게.)

- [ ] **Step 4: 통과 확인** — 새+기존 LS 테스트 green.

- [ ] **Step 5: 커밋** — `git commit -m "feat(local): LS 계약 해석 미니 분기(정규 보존)"`

---

### Task 5: 자동매매 사이징 시세 + 발주 경로 무손상 확인 (local/server)

**Files:**
- Inspect: `local/localapp/trader.py`·`broker_router.py` (심볼 통과 — 코드 변경 없을 가능성)
- Modify(필요 시): `server/app/live_quote.py:23` `_KR_FUTURES_MAP`에 미니 엔트리
- Test: 해당 모듈 기존 테스트 green + 사이징 단위테스트(미니 현재가 소스)

- [ ] **Step 1: 사이징 시세 소스 확인** — 자동매매 사이징(preview/Trader)이 미니 현재가를 어디서 받는지 확인. server `live_quote`를 쓰면 `_KR_FUTURES_MAP`에 `"미니코스피200선물"` → 정규와 동일 FUT 소스 추가. 로컬앱이 브로커에서 직접 현재가를 받으면 변경 불요(과추가 금지 — 4원칙).

- [ ] **Step 2: 발주 경로 무손상 테스트** — `broker_router`가 미니 심볼을 resolve→코드 통과시키는지(코어 resolve_contract 경유) 기존 골든/단위 테스트로 확인. 필요 시 미니 심볼 발주 mock 테스트 추가(MockBroker, 자금 0).

- [ ] **Step 3: 통과 확인** — local 전체 테스트 green.

- [ ] **Step 4: 커밋** — 변경 있으면 `git commit -m "feat: 미니 자동매매 사이징 시세 소스 배선"`, 없으면 skip(확인만).

---

### Task 6: 통합 검증 — 백테스트 1/5 손익 + 게이트 + 웹 노출

**Files:**
- Test: `core/tests/test_mini_backtest_parity.py`(신규) · server 게이트 테스트(기존) · `web` 빌드

- [ ] **Step 1: 백테스트 패리티 테스트** — 동일 전략·동일 정규 가격으로 정규 vs 미니 백테스트 → **미니 1계약 손익 = 정규 1계약 × (50_000/250_000)**. (엔진 `mult` 경로 검증.) 가능하면 결정적 소형 시리즈로.

- [ ] **Step 2: 게이트 테스트(기존에 추가)** — `cd server && PYTHONPATH=../core python -m pytest tests/test_autotrade_gate.py -v`:
```python
def test_mini_kospi200_asset_class():
    assert asset_class_for_symbol("미니코스피200선물", "") == "kr_futures"
def test_mini_kospi200_paper_allowed(monkeypatch):
    _assert_live_tradable("paper", _defn(["미니코스피200선물"], "kis"), account_broker="kis")
```

- [ ] **Step 3: 웹 빌드 + 노출 확인** — `cd web && npm run build` 0 에러. (웹 코드 변경 없음 — 미니가 데이터 alias로 /symbols 선물탭에 뜨는지는 서버 기동 후/배포 후 확인 항목으로 기록.)

- [ ] **Step 4: 정규 무손상 전수** — `cd core && python -m pytest`(전체) + server 게이트·futures_config 테스트 green.

- [ ] **Step 5: 커밋** — `git commit -m "test: 미니 백테스트 1/5 패리티 + 게이트 kr_futures"`

---

### Task 7: 로컬앱 버전 + 릴리스 노트

**Files:**
- Modify: `local/localapp/__init__.py:7` (`__version__`)
- Create: `local/RELEASE_NOTES_v0.9.57-beta.md`

- [ ] **Step 1:** `__version__ = "0.9.57-beta"`.
- [ ] **Step 2:** 릴리스 노트 작성 — 미니 코스피200선물 자동매매 추가, 사용자 영향(앱 업데이트 필요), 미검증(KIS/LS 미니 주문 수용=실전 검증 항목).
- [ ] **Step 3: 커밋** — `git commit -m "chore(local): v0.9.57-beta — 미니 코스피200선물"`

(빌드·릴리스 publish·배포는 사용자 명시 허락 시에만 — 자동 금지.)

---

## 최종 리뷰 (전체 구현 후)

- 정규 코스피200선물 경로 byte 보존(테스트 + diff 검토).
- 미니 4계층 정합(카탈로그·데이터 alias·게이트·계약해석).
- 미검증 항목(KIS/LS 미니 주문 수용) 명시 — 자율 "완료" 선언 금지.
- 배포/푸시/릴리스는 사용자 허락 후.

# 자동매매 역량 parity — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 코스피200선물 모의/실전 자동매매를 즉시 적용 가능하게 열고, 자동매매 가능 여부를 (브로커×모드×자산군) capability 표(core SSOT)로 단일화해 게이트·웹이 공유한다. 일본/홍콩은 유니버스에서 제외.

**Architecture:** core에 순수함수 capability 표를 신설(`autotrade_capability`)하고, 서버 게이트(`_assert_live_tradable`)의 종목 화이트리스트(G5)를 그 표 검사로 교체한다. 전략에 비민감 `account_broker`를 저장해 게이트가 브로커를 안다. 서버가 capability 표·심볼 자동매매 힌트를 노출하면 웹은 그대로 소비(테이블 중복 없음). 일본/홍콩은 서버 카탈로그·웹 탭에서 제외.

**Tech Stack:** Python(core 순수함수·FastAPI 서버·pytest) · React+TS+Vite(web) · 부트 마이그레이션(`_NEW_COLS`).

**설계 출처:** `docs/superpowers/specs/2026-06-29-autotrade-capability-parity-design.md` (§3, §4 Phase 1).

**Phase 1 비포함(후속 plan):** 해외선물 배선(Phase 2) · 로컬 런타임 가드·실전 verified 운영(Phase 3). 따라서 Phase 1 capability 표에서 **`us_futures`는 "준비 중"으로 blocked** — Phase 2가 연다.

---

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `core/quant_core/autotrade_capability.py` | (브로커×모드×자산군)→Capability 순수 SSOT | **신규** |
| `core/tests/test_autotrade_capability.py` | capability 표 셀별 회귀 | **신규** |
| `server/app/routers/strategies.py` | 게이트 G5를 capability 검사로 교체 + `account_broker` 입출력 | 수정 |
| `server/app/models.py` | `Strategy.account_broker` 컬럼 | 수정 |
| `server/app/db.py` | `_NEW_COLS`에 `account_broker` 부트 마이그레이션 | 수정 |
| `server/app/schemas.py` | `StrategyCreate/Update`·`StrategyOut`에 `account_broker` | 수정 |
| `server/app/autotrade_caps_api.py` | capability 표 + 심볼 자산군 판정 API 헬퍼(서버측) | **신규** |
| `server/app/routers/backtest.py` | JP/HK(TSE/HKS) 카탈로그 제외 + 심볼 `autotrade_hint` 스탬프 | 수정 |
| `server/tests/test_autotrade_gate.py` | 게이트 capability 분기 회귀 | **신규** |
| `web/src/types.ts` | `account_broker`·capability 타입 | 수정 |
| `web/src/api.ts` | capability 페치 + `account_broker` 전송 | 수정 |
| `web/src/components/AccountPicker.tsx` | 브로커별 capability 필터·사유 표시 | 수정 |
| `web/src/components/MultiSymbolPicker.tsx` | capability 기반 라벨 + 일본/홍콩 탭 제거 | 수정 |

---

## Task 1: capability SSOT 모듈 (core)

**Files:**
- Create: `core/quant_core/autotrade_capability.py`
- Test: `core/tests/test_autotrade_capability.py`

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_autotrade_capability.py
from quant_core.autotrade_capability import autotrade_capability, Capability


def test_kr_futures_open_all_four_cells():
    for broker in ("kis", "ls"):
        for mode in ("paper", "live"):
            cap = autotrade_capability(broker, mode, "kr_futures")
            assert cap.status == "ok", (broker, mode)


def test_overseas_futures_paper_blocked_pending_wiring():
    # Phase 1: 해외선물(KIS·LS 공통) 배선 전 — "준비 중"으로 통일 차단.
    # (LS 해외선물 모의는 본래 지원되나 배선이 Phase 2라 차단 — 사유를 "모의 미지원"으로 쓰지 않는다.)
    cap = autotrade_capability("kis", "paper", "us_futures")
    assert cap.status == "blocked"
    assert "준비" in cap.reason


def test_us_futures_blocked_in_phase1_pending_wiring():
    # Phase 1: 해외선물 배선 전 — 전 셀 blocked(준비 중)
    for broker in ("kis", "ls"):
        for mode in ("paper", "live"):
            assert autotrade_capability(broker, mode, "us_futures").status == "blocked"


def test_ls_us_equity_paper_blocked():
    cap = autotrade_capability("ls", "paper", "us_equity")
    assert cap.status == "blocked"
    assert "모의" in cap.reason


def test_kis_equity_verified_true():
    assert autotrade_capability("kis", "paper", "kr_equity").verified is True
    assert autotrade_capability("kis", "live", "us_equity").verified is True


def test_kr_futures_unverified_in_phase1():
    assert autotrade_capability("kis", "paper", "kr_futures").verified is False


def test_unknown_inputs_blocked_not_crash():
    assert autotrade_capability("kis", "paper", "jp_equity").status == "blocked"
    assert autotrade_capability("etrade", "paper", "kr_equity").status == "blocked"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_autotrade_capability.py -q`
Expected: FAIL — `ModuleNotFoundError: quant_core.autotrade_capability`

- [ ] **Step 3: Write the module**

```python
# core/quant_core/autotrade_capability.py
"""자동매매 가능 여부 단일 진실원천(SSOT) — (브로커 × 모드 × 자산군) → Capability.

순수함수·네트워크 없음. 서버 게이트(strategies._assert_live_tradable)·웹 라벨·
로컬 런타임 가드가 모두 이 한 표를 읽어 "빌더 노출 ⊋ 게이트 통과 ⊋ 실제 체결"의
불일치를 없앤다(설계서 §3.1).

상태:
  ok          — 완전 지원. 적용 가능.
  needs_setup — 적용 가능하나 유저 행동 필요(예: KIS 해외선물 실전 HTS 시세신청).
                비차단 — 시세 미신청이어도 전일종가 사이징+시장가로 발주는 동작.
  blocked     — 하드 차단. 적용 시 422 + reason.
verified: 라이브 검증 완료 셀(실전 무경고). 미검증은 실전 적용 시 경고-확인(게이트가 처리).

⚠ Phase 1: us_futures(CME 해외선물)는 배선 전이라 전 셀 blocked("준비 중"). Phase 2가 연다.
"""
from __future__ import annotations

from dataclasses import dataclass

_BROKERS = ("kis", "ls")
_MODES = ("paper", "live")
_ASSET_CLASSES = ("kr_equity", "kr_futures", "us_equity", "us_futures")

# 실사용으로 검증된 셀(실전 무경고). 나머지는 모의 라운드트립 검증 후 승격.
_VERIFIED = frozenset({
    ("kis", "paper", "kr_equity"), ("kis", "live", "kr_equity"),
    ("kis", "paper", "us_equity"), ("kis", "live", "us_equity"),
})


@dataclass(frozen=True)
class Capability:
    status: str                 # "ok" | "needs_setup" | "blocked"
    verified: bool = False
    reason: str = ""            # blocked/needs_setup 사유(유저 메시지)
    setup_hint: str | None = None


def autotrade_capability(broker: str, mode: str, asset_class: str) -> Capability:
    if broker not in _BROKERS or mode not in _MODES or asset_class not in _ASSET_CLASSES:
        return Capability("blocked", reason="자동매매 미지원 대상입니다")

    verified = (broker, mode, asset_class) in _VERIFIED

    # 구조적 차단 셀
    if asset_class == "us_equity" and broker == "ls" and mode == "paper":
        return Capability("blocked", verified,
                          reason="LS 미국주식은 모의투자 미제공 — KIS 모의를 사용하세요")

    if asset_class == "us_futures":
        # Phase 1: 해외선물 배선 전 — 전 셀 보류. (Phase 2에서 LS ok / KIS live needs_setup)
        return Capability("blocked", verified,
                          reason="해외선물 자동매매는 준비 중입니다(곧 지원)")

    # kr_equity·kr_futures(전 셀) + us_equity(ls/paper 제외) → ok
    return Capability("ok", verified)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && python -m pytest tests/test_autotrade_capability.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add core/quant_core/autotrade_capability.py core/tests/test_autotrade_capability.py
git commit -m "feat(core): 자동매매 capability SSOT (브로커×모드×자산군)"
```

---

## Task 2: 서버측 capability API 헬퍼 + 심볼 자산군 판정

게이트·웹·카탈로그가 공유할 서버 진입점. 심볼→자산군 판정에 **일본/홍콩 방어 차단**을 포함(`None`=미지원).

**Files:**
- Modify: `core/quant_core/autotrade_capability.py` (도메인 상수 public 승격)
- Create: `server/app/autotrade_caps_api.py`
- Test: `server/tests/test_autotrade_gate.py` (이 파일은 Task 4에서 게이트 테스트도 추가)

- [ ] **Step 0: core 도메인 상수 public 승격**

`core/quant_core/autotrade_capability.py`에서 `_BROKERS`·`_MODES`·`_ASSET_CLASSES` 를 **public 이름**(`BROKERS`·`MODES`·`ASSET_CLASSES`)으로 rename(모듈 내부 참조 `autotrade_capability()`의 멤버십 체크 한 줄도 함께 갱신). private 교차-import 스멜을 없애고 도메인 정의를 단일 출처로 유지. 그 뒤 Task 1 테스트 회귀 확인:
`cd core && python -m pytest tests/test_autotrade_capability.py -q` → 8 passed.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_autotrade_gate.py
from app.autotrade_caps_api import asset_class_for_symbol, capability_matrix


def test_asset_class_kr_stock():
    assert asset_class_for_symbol("005930", "KOSPI") == "kr_equity"


def test_asset_class_us_stock():
    assert asset_class_for_symbol("AAPL", "NAS") == "us_equity"


def test_asset_class_kospi200_futures():
    assert asset_class_for_symbol("코스피200선물", "") == "kr_futures"


def test_asset_class_jp_hk_unsupported():
    # 일본(TSE)/홍콩(HKS)은 자동매매 미지원 — None
    assert asset_class_for_symbol("7203", "TSE") is None
    assert asset_class_for_symbol("0700", "HKS") is None


def test_capability_matrix_shape():
    m = capability_matrix()
    # 브로커×모드×자산군 전 셀 직렬화(웹 소비용)
    assert m["kis"]["paper"]["kr_futures"]["status"] == "ok"
    assert m["kis"]["paper"]["us_futures"]["status"] == "blocked"
    assert m["ls"]["paper"]["us_equity"]["status"] == "blocked"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && PYTHONPATH=../core python -m pytest tests/test_autotrade_gate.py -q`
(⚠ `PYTHONPATH=../core` 필수 — env editable install은 platform/core를 가리켜 신규 `quant_core.autotrade_capability`를 못 본다. 워크트리 core를 강제해야 함.)
Expected: FAIL — `ModuleNotFoundError: app.autotrade_caps_api`

- [ ] **Step 3: Write the helper**

```python
# server/app/autotrade_caps_api.py
"""서버측 자동매매 capability 진입점 — core SSOT를 게이트·웹·카탈로그에 연결.

asset_class_for_symbol: 심볼→자산군(미지원 시 None). 일본/홍콩 방어 차단 포함.
capability_matrix: 전 셀을 JSON 직렬화(웹 소비 — 라벨·AccountPicker 필터).
"""
from __future__ import annotations

import quant_core as qc
from quant_core.autotrade_capability import (
    autotrade_capability, BROKERS, MODES, ASSET_CLASSES,
)

# KIS/LS 라우팅 가능한 주식 시장코드(일본 TSE·홍콩 HKS는 dead branch → 제외).
_KR_MARKETS = {"KOSPI", "KOSDAQ"}
_US_MARKETS = {"NAS", "NYS", "AMS"}


def asset_class_for_symbol(symbol: str, market: str) -> str | None:
    """심볼→자산군('kr_equity'|'kr_futures'|'us_equity'|'us_futures') 또는 None(미지원).

    선물은 core instrument_category가 권위(한글 상품명). 주식은 market으로 판별 —
    KR/US 지원 시장만 통과, 일본/홍콩 등은 None(자동매매 미지원)."""
    if qc.is_futures(symbol):
        return qc.instrument_category(symbol)        # kr_futures | us_futures
    if market in _KR_MARKETS:
        return "kr_equity"
    if market in _US_MARKETS:
        return "us_equity"
    return None


def capability_matrix() -> dict:
    """전 셀(broker→mode→asset_class→Capability dict). 웹이 1회 페치해 소비."""
    out: dict = {}
    for b in BROKERS:
        out[b] = {}
        for m in MODES:
            out[b][m] = {}
            for ac in ASSET_CLASSES:
                cap = autotrade_capability(b, m, ac)
                out[b][m][ac] = {"status": cap.status, "verified": cap.verified,
                                 "reason": cap.reason, "setup_hint": cap.setup_hint}
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && PYTHONPATH=../core python -m pytest tests/test_autotrade_gate.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add server/app/autotrade_caps_api.py server/tests/test_autotrade_gate.py
git commit -m "feat(server): capability 서버 헬퍼 + 심볼 자산군 판정(JP/HK 차단)"
```

---

## Task 3: `Strategy.account_broker` 컬럼 (비민감)

`account_ref`(P5-2)와 동일 패턴. 브로커명만 저장("kis"/"ls") — INV-SEC 무관.

**Files:**
- Modify: `server/app/models.py` (account_ref 정의 바로 아래)
- Modify: `server/app/db.py` (`_NEW_COLS`)
- Modify: `server/app/schemas.py` (단일 `StrategyIn`[생성·수정 공용, line ~100] + `StrategyOut`[line ~115] — 둘 다 `account_ref` 보유)

- [ ] **Step 1: models.py — 컬럼 추가**

`server/app/models.py`에서 `account_ref: Optional[str] = None` 줄 바로 아래에 추가:

```python
    # Phase 1(역량 parity) — 적용 시 선택한 브로커("kis"|"ls"). 비민감(키·계좌번호 아님).
    # 게이트가 capability(account_broker, run_mode, asset_class)를 검사. NULL=미바인딩.
    account_broker: Optional[str] = None
```

- [ ] **Step 2: db.py — 부트 마이그레이션**

`server/app/db.py`의 `_NEW_COLS` 리스트에서 `("strategy", "account_ref", "VARCHAR")` 줄 아래에 추가:

```python
    ("strategy",     "account_broker",                   "VARCHAR"),
```

- [ ] **Step 3: schemas.py — 입출력**

`server/app/schemas.py`의 **`StrategyIn`**(생성·수정 공용, `account_ref` 줄 ~100 바로 아래)과 **`StrategyOut`**(`account_ref` 줄 ~115 바로 아래)에 각각 추가:

```python
    account_broker: Optional[str] = None
```

- [ ] **Step 4: strategies.py — 영속화 배선**

`server/app/routers/strategies.py`:
- `_out` (162–168행): 반환에 `account_broker=s.account_broker` 추가.
- `create_strategy` (269행 근처 `account_ref=body.account_ref,` 옆): `account_broker=body.account_broker,` 추가.
- `update_strategy` (329행 `row.account_ref = body.account_ref` 아래): `row.account_broker = body.account_broker` 추가.

- [ ] **Step 5: 회귀 확인 + 커밋**

Run: `cd server && PYTHONPATH=../core python -m pytest tests/test_strategies_ir.py -q`
Expected: PASS (기존 테스트 — 컬럼 추가는 기존 동작 불변)

```bash
git add server/app/models.py server/app/db.py server/app/schemas.py server/app/routers/strategies.py
git commit -m "feat(server): Strategy.account_broker 비민감 컬럼(적용 시 브로커)"
```

---

## Task 4: 게이트 G5를 capability 검사로 교체

기존 G1~G4·G6는 유지. G5(종목 화이트리스트 + `_LIVE_FUTURES_SYMBOLS` + `QP_FUTURES_LIVE_ENABLED`)만 교체.

**Files:**
- Modify: `server/app/routers/strategies.py` (`_assert_live_tradable` 내부 G5 블록, 76·79–81·146–153행)
- Test: `server/tests/test_autotrade_gate.py` (Task 2 파일에 추가)

- [ ] **Step 1: Write the failing test (게이트 분기)**

`server/tests/test_autotrade_gate.py`에 추가:

```python
import pytest
from fastapi import HTTPException
from app.routers.strategies import _assert_live_tradable


def _defn(symbols, broker, direction="long"):
    return {"universe": {"kind": "single", "symbols": symbols},
            "position": {"direction": direction, "entry": {"mode": "scheduled"}},
            "simulation": {}}


def test_kospi200_futures_paper_allowed_when_bound(monkeypatch):
    # 코스피200선물 + KIS 모의 바인딩 → 통과(과거엔 화이트리스트/플래그로 차단됐던 것)
    _assert_live_tradable("paper", _defn(["코스피200선물"], "kis"), account_broker="kis")


def test_overseas_futures_blocked_phase1(monkeypatch):
    with pytest.raises(HTTPException) as e:
        _assert_live_tradable("paper", _defn(["원유선물"], "ls"), account_broker="ls")
    assert e.value.status_code == 422
    assert "준비 중" in e.value.detail


def test_ls_us_stock_paper_blocked(monkeypatch):
    # 게이트는 KIS 마스터에서 심볼→market을 얻어 자산군을 판정한다. 단위 테스트엔
    # 마스터가 없으니 AAPL→NAS를 주입(없으면 market="" → asset_class None → 잘못된 분기).
    monkeypatch.setattr("app.kis_master_cache.get_master_list",
                        lambda: [{"symbol": "AAPL", "market": "NAS", "name": "Apple"}])
    with pytest.raises(HTTPException) as e:
        _assert_live_tradable("paper", _defn(["AAPL"], "ls"), account_broker="ls")
    assert e.value.status_code == 422
    assert "모의" in e.value.detail   # LS 미국주식 모의 미제공


def test_unsupported_market_blocked(monkeypatch):
    # 일본(TSE)/홍콩(HKS) 등 미지원 시장 심볼 → 자산군 None → "미지원 종목" 차단(방어선).
    monkeypatch.setattr("app.kis_master_cache.get_master_list",
                        lambda: [{"symbol": "7203", "market": "TSE", "name": "Toyota"}])
    with pytest.raises(HTTPException) as e:
        _assert_live_tradable("paper", _defn(["7203"], "kis"), account_broker="kis")
    assert e.value.status_code == 422
    assert "미지원" in e.value.detail


def test_unverified_live_requires_ack(monkeypatch):
    # 코스피200선물 KIS 실전은 verified=False → ack 없으면 422
    with pytest.raises(HTTPException) as e:
        _assert_live_tradable("live", _defn(["코스피200선물"], "kis"),
                              account_broker="kis", ack_unverified=False)
    assert e.value.status_code == 422
    assert "검증" in e.value.detail
    # ack=True면 통과
    _assert_live_tradable("live", _defn(["코스피200선물"], "kis"),
                          account_broker="kis", ack_unverified=True)
```

Run: `cd server && PYTHONPATH=../core python -m pytest tests/test_autotrade_gate.py -q`
Expected: FAIL — `_assert_live_tradable`가 `account_broker`/`ack_unverified` 키워드 미지원.

- [ ] **Step 2: `_assert_live_tradable` 시그니처 확장 + G5 교체**

`server/app/routers/strategies.py`:

(a) 상단 import에 추가(strategies.py는 **상대 import** 관례 — `from ..` 사용):
```python
from .. import kis_master_cache
from ..autotrade_caps_api import asset_class_for_symbol
from quant_core.autotrade_capability import autotrade_capability
```

(b) `_LIVE_FUTURES_SYMBOLS`(76행)·`_futures_live_enabled`(79–81행) **삭제**. 삭제 전 `git grep -n "_LIVE_FUTURES_SYMBOLS\|_futures_live_enabled\|QP_FUTURES_LIVE_ENABLED" server/`로 잔여 참조 전수 확인 — test_strategies_ir.py(Step 4) 외 참조가 있으면 함께 정리. G5 교체 후 `tradable_symbols` import가 이 파일에서 미사용이 되면 import도 제거(lint).

(c) 시그니처 변경:
```python
def _assert_live_tradable(run_mode: str, definition: dict,
                          account_broker: str | None = None,
                          ack_unverified: bool = False) -> None:
```

(d) G5 블록(146–153행, `ok = tradable_symbols()` … `bad = [...]` … `raise`)을 아래로 **교체**:

```python
    # G5 (capability): 바인딩된 브로커×모드×자산군이 자동매매 가능한지 검사(설계 §3.3).
    # 미바인딩(account_broker=None) 레거시 주식 전략은 KIS 기본 지원으로 보수 통과.
    broker = account_broker or "kis"
    # 마스터가 비면(드문 부팅 직후 네트워크 실패) 주식이 market="" → asset_class None → 아래서
    # fail-closed 차단(허용보다 차단이 안전 — 자금 게이트). 마스터는 카탈로그와 공유돼 평시 채워짐.
    master_market = {m["symbol"]: m.get("market", "") for m in kis_master_cache.get_master_list()}
    unverified_live: list[str] = []
    for sym in syms:
        ac = asset_class_for_symbol(sym, master_market.get(sym, ""))
        if ac is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"자동매매 미지원 종목입니다(국내/미국 주식·선물만 가능): {sym}")
        cap = autotrade_capability(broker, run_mode, ac)
        if cap.status == "blocked":
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{sym}: {cap.reason}")
        if run_mode == "live" and not cap.verified:
            unverified_live.append(sym)

    if unverified_live and not ack_unverified:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
            "아직 실거래 검증 전 경로입니다(모의 검증 권장): "
            f"{', '.join(unverified_live[:5])}. 확인 후 다시 적용해 주세요.")
```

> 주: `kis_master_cache.get_master_list()`는 검증됨(backtest.py와 동일 사용·각 항목에 `market` 키). **needs_setup/setup_hint 처리는 Phase 1에서 죽은 코드(어떤 셀도 needs_setup 반환 안 함)라 게이트에서 누산하지 않는다 — Phase 2에서 소비처와 함께 도입**(4원칙: YAGNI). `Capability.setup_hint` 필드 자체는 capability_matrix 직렬화에 남는다.

(e) 호출부 — `create_strategy`(266행)·`update_strategy`(310행)의 `_assert_live_tradable(body.run_mode, definition)` 호출에 `, account_broker=body.account_broker, ack_unverified=body.ack_unverified` 추가. 그리고 단일 `StrategyIn` 스키마(schemas.py)에 `ack_unverified: bool = False` 필드 추가(StrategyOut엔 불필요 — 입력 전용).

- [ ] **Step 3: Run test to verify it passes**

Run: `cd server && PYTHONPATH=../core python -m pytest tests/test_autotrade_gate.py tests/test_strategies_ir.py -q`
Expected: 신규 게이트 테스트 PASS. **기존 `test_strategies_ir.py`의 선물 차단 테스트(M1a)들은 화이트리스트/플래그 제거로 의미가 바뀌므로 이 단계에서 갱신**한다(아래 Step 4).

- [ ] **Step 4: 기존 선물 게이트 테스트 갱신**

`server/tests/test_strategies_ir.py`의 M1a 블록(324–355행 근처, `QP_FUTURES_LIVE_ENABLED`·`_LIVE_FUTURES_SYMBOLS` 의존 테스트)을 capability 기준으로 교체:
- "플래그 OFF면 코스피200선물 차단" → **삭제**(이제 바인딩+capability로 허용).
- "코스피200선물 바인딩 시 paper 허용" 테스트로 대체(Task 4 Step 1과 중복되면 test_autotrade_gate로 일원화하고 여기선 제거).
- long_short·short·레버리지 게이트 테스트(G1/G2/G4)는 **불변** — 그대로 통과해야 함.

Run: `cd server && PYTHONPATH=../core python -m pytest tests/test_strategies_ir.py tests/test_autotrade_gate.py -q`
Expected: PASS (전체)

- [ ] **Step 5: Commit**

```bash
git add server/app/routers/strategies.py server/app/schemas.py server/tests/test_strategies_ir.py server/tests/test_autotrade_gate.py
git commit -m "feat(server): 게이트 G5를 capability 검사로 교체(선물 화이트리스트·플래그 철거)"
```

---

## Task 5: JP/HK 카탈로그 제외 + 심볼 autotrade 힌트 스탬프

**Files:**
- Modify: `server/app/routers/backtest.py` (마스터-only 제외 분기 88–90행 + 심볼 dict)
- Test: `server/tests/test_autotrade_gate.py` (추가)

- [ ] **Step 1: Write the failing test**

`/symbols`는 `get_current_user` 인증이 필요하므로 HTTP 대신 **빌더 `_build_symbols_payload()` 직접 호출**(인덱스·마스터 monkeypatch). `server/tests/test_autotrade_gate.py`에 추가:

```python
def test_jp_hk_excluded_and_autotrade_hint(monkeypatch):
    from app.routers import backtest
    idx = {"005930": {"rows": 100, "has_ohlc": True},        # KR 주식
           "코스피200선물": {"rows": 100, "has_ohlc": True},   # KR 선물
           "원유선물": {"rows": 100, "has_ohlc": True},        # 해외선물(us_futures)
           "7203": {"rows": 100, "has_ohlc": True}}           # 일본(TSE) — 제외돼야
    master = [{"symbol": "005930", "market": "KOSPI", "kind": "stock", "name": "삼성"},
              {"symbol": "7203", "market": "TSE", "kind": "stock", "name": "Toyota"},
              {"symbol": "0700", "market": "HKS", "kind": "stock", "name": "Tencent"}]  # 홍콩 master-only
    monkeypatch.setattr(backtest.data_cache, "get_symbol_index", lambda: idx)
    monkeypatch.setattr(backtest.kis_master_cache, "get_master_list", lambda: master)
    monkeypatch.setattr(backtest.kis_master_cache, "get_status", lambda: {})
    payload = backtest._build_symbols_payload()
    syms = {s["symbol"]: s for s in payload["symbols"]}
    # JP/HK 제외(데이터 보유 7203·master-only 0700 둘 다)
    assert "7203" not in syms and "0700" not in syms
    assert not any("일본" in s["category"] or "홍콩" in s["category"] for s in payload["symbols"])
    # autotrade_hint 스탬프(브로커-불문)
    assert syms["005930"]["autotrade_hint"] == "ok"            # KR equity
    assert syms["코스피200선물"]["autotrade_hint"] == "ok"      # KR futures
    assert syms["원유선물"]["autotrade_hint"] == "backtest_only"  # 해외선물 Phase 1 미지원
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && PYTHONPATH=../core python -m pytest tests/test_autotrade_gate.py -k jp_hk -q`
Expected: FAIL — `KeyError: 'autotrade_hint'` 또는 7203/0700 포함.

- [ ] **Step 3: backtest.py 수정**

(a) 마스터-only 제외(현재 미국 데이터-결손만 제외, 88–90행)를 일본/홍콩까지 확대:

```python
        # §4.8 + JP/HK 제외: 미국 데이터-결손 + 일본(TSE)·홍콩(HKS)은 selectable 미노출
        # (자동매매 라우팅 불가 — 설계 §3.5).
        if meta.get("market") in ("NAS", "NYS", "AMS", "TSE", "HKS"):
            continue
```

(b) 데이터-보유 종목 루프(인덱스 기반, 64–82행)에서도 TSE/HKS 마스터 종목이면 스킵하도록, `out.append` 직전에 가드 추가:

```python
        if meta.get("market") in ("TSE", "HKS"):
            continue   # 일본/홍콩 — 자동매매·노출 제외(설계 §3.5)
```

(c) 각 심볼 dict에 `autotrade_hint` 추가(브로커-불문: 어떤 브로커로도 ok면 "ok"). backtest.py는 **상대 import** 관례 — `from ..` 사용. capability 도메인은 core public 상수(`BROKERS`·`MODES`) 재사용(drift 방지):

```python
from ..autotrade_caps_api import asset_class_for_symbol
from quant_core.autotrade_capability import autotrade_capability, BROKERS, MODES

def _autotrade_hint(sym: str, market: str) -> str:
    ac = asset_class_for_symbol(sym, market)
    if ac is None:
        return "backtest_only"
    for b in BROKERS:
        for m in MODES:
            if autotrade_capability(b, m, ac).status in ("ok", "needs_setup"):
                return "ok"
    return "backtest_only"
```
그리고 **두 루프 모두**의 `out.append({...})`에 `"autotrade_hint": _autotrade_hint(sym, meta.get("market", ""))` 추가(loop2는 `code`가 심볼 — `_autotrade_hint(code, meta.get("market",""))`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && PYTHONPATH=../core python -m pytest tests/test_autotrade_gate.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/routers/backtest.py server/tests/test_autotrade_gate.py
git commit -m "feat(server): 일본/홍콩 카탈로그 제외 + 심볼 autotrade_hint 스탬프"
```

---

## Task 6: capability 노출 엔드포인트

웹이 1회 페치해 라벨·AccountPicker 필터에 사용(테이블 중복 방지).

**Files:**
- Modify: `server/app/routers/strategies.py` (정적 라우트 — `/{strategy_id}`보다 **위**에 등록)
- Test: `server/tests/test_autotrade_gate.py`

- [ ] **Step 1: Write the failing test**

엔드포인트는 **비인증**(정적 매트릭스·유저데이터 없음). 테스트는 `client` 픽스처가 없으니 `TestClient(app)` 직접:
```python
def test_capabilities_endpoint():
    from fastapi.testclient import TestClient
    from app.main import app
    r = TestClient(app).get("/strategies/autotrade-capabilities")
    assert r.status_code == 200
    m = r.json()
    assert m["kis"]["paper"]["kr_futures"]["status"] == "ok"
    assert m["ls"]["paper"]["us_equity"]["status"] == "blocked"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && PYTHONPATH=../core python -m pytest tests/test_autotrade_gate.py -k capabilities_endpoint -q`
Expected: FAIL — 404 (또는 `/{strategy_id}` int 변환 실패 422).

- [ ] **Step 3: 엔드포인트 추가**

`server/app/routers/strategies.py`에서 `@router.get("/{strategy_id}", ...)`(286행) **앞**에 추가(상대 import):

```python
@router.get("/autotrade-capabilities")
def autotrade_capabilities():
    """(브로커×모드×자산군) 자동매매 capability 표 — 웹 라벨·AccountPicker 필터용 SSOT."""
    from ..autotrade_caps_api import capability_matrix
    return capability_matrix()
```

> 라우트 등록 순서 중요: `/{strategy_id}`(int)보다 위여야 `autotrade-capabilities`가 path param으로 안 먹힌다. 비인증(Depends 없음) — 정적 표라 유저데이터 없음.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && PYTHONPATH=../core python -m pytest tests/test_autotrade_gate.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add server/app/routers/strategies.py server/tests/test_autotrade_gate.py
git commit -m "feat(server): GET /strategies/autotrade-capabilities (capability SSOT 노출)"
```

---

## Task 7: 적용 바인딩 — account_broker/ack 전송 + AccountPicker capability 필터

전략 "적용" 시 (1) 선택 브로커·ack를 서버 게이트에 전송하고(게이트 필수), (2) AccountPicker가 이 전략을 못 돌리는 계좌를 사유와 함께 비활성화한다(Task 6 엔드포인트 소비). 필터엔 종목→4-way 자산군이 필요하므로 `/symbols` 페이로드에 `autotrade_asset_class`를 SSOT로 더한다.

**Files:** `server/app/routers/backtest.py` · `server/tests/test_autotrade_gate.py` · `web/src/types.ts` · `web/src/api.ts` · `web/src/components/AccountPicker.tsx` · `web/src/pages/IrBuilder.tsx` · `web/src/pages/StrategyDetail.tsx`

> ⚠ 웹은 **npm**(package-lock.json). `bun` 쓰지 말 것. 검증: `cd web && npm run build`(=`tsc -b && vite build`, 타입체크 포함). node_modules는 이미 설치됨.

- [ ] **Step 1: 서버 — `/symbols`에 `autotrade_asset_class` 필드**

`server/app/routers/backtest.py` 의 **두 루프 모두** `out.append({...})`에 추가(이미 import된 `asset_class_for_symbol` 재사용):
```python
        "autotrade_asset_class": asset_class_for_symbol(sym, meta.get("market", "")),
```
(loop2는 심볼 변수가 `code` — `asset_class_for_symbol(code, meta.get("market", ""))`. 값은 "kr_equity"|"kr_futures"|"us_equity"|"us_futures" 또는 None.)
Task 5 테스트(`test_jp_hk_excluded_and_autotrade_hint`)에 단언 추가:
```python
    assert syms["005930"]["autotrade_asset_class"] == "kr_equity"
    assert syms["코스피200선물"]["autotrade_asset_class"] == "kr_futures"
    assert syms["원유선물"]["autotrade_asset_class"] == "us_futures"
```
Run: `cd server && PYTHONPATH=../core python -m pytest tests/test_autotrade_gate.py -k jp_hk -q` → PASS.

- [ ] **Step 2: types.ts**

- `SymbolInfo`(6행~)에 추가: `autotrade_hint?: "ok" | "needs_setup" | "backtest_only";` 와 `autotrade_asset_class?: string | null;`
- `StrategyRow`(160행~)에 추가: `account_broker?: string | null;`
- capability 타입 신규(파일 적절한 위치):
```typescript
export type Capability = { status: "ok" | "needs_setup" | "blocked"; verified: boolean; reason: string; setup_hint: string | null };
export type CapabilityMatrix = Record<string, Record<string, Record<string, Capability>>>;
```

- [ ] **Step 3: api.ts**

api.ts는 `req<T>(path, opts)` 래퍼(자동 Bearer 토큰 첨부) + 메서드 객체 패턴. `createStrategy`(176행)·`updateStrategy`(181행) 끝에 두 옵션 인자 추가 + body에 포함:
```typescript
  createStrategy: (definition: StrategyDef | IrStrategyDef, run_mode: string,
                   engine: "operand" | "ir" = "ir", account_ref: string | null = null,
                   account_broker: string | null = null, ack_unverified = false) =>
    req<StrategyRow>("/strategies", { method: "POST",
      body: JSON.stringify({ definition, run_mode, engine, account_ref, account_broker, ack_unverified }) }),
  updateStrategy: (id: number, definition: StrategyDef | IrStrategyDef, run_mode: string,
                   engine: "operand" | "ir" = "ir", account_ref: string | null = null,
                   account_broker: string | null = null, ack_unverified = false) =>
    req<StrategyRow>(`/strategies/${id}`, { method: "PUT",
      body: JSON.stringify({ definition, run_mode, engine, account_ref, account_broker, ack_unverified }) }),
```
- capability 페치 추가(메서드 객체에, `req` 사용 — 비인증 엔드포인트지만 토큰 첨부는 무해):
```typescript
  autotradeCapabilities: (): Promise<CapabilityMatrix> =>
    req<CapabilityMatrix>("/strategies/autotrade-capabilities"),
```
(`CapabilityMatrix`를 types에서 import. 카탈로그는 기존 `api.symbols()` — 반환 `{symbols: SymbolInfo[], ...}`.)

- [ ] **Step 4: AccountPicker.tsx — capability 필터 + 미검증 경고**

Props에 추가: `capabilities?: CapabilityMatrix;` · `assetClasses?: string[];`(전략 자산군 집합). 컴포넌트 내 헬퍼:
```typescript
const cells = (broker: string, mode: string) =>
  (assetClasses ?? []).map((ac) => capabilities?.[broker]?.[mode]?.[ac]).filter(Boolean) as Capability[];
const blockedReason = (broker: string, mode: string) =>
  cells(broker, mode).find((c) => c.status === "blocked")?.reason ?? null;
const hasUnverified = (broker: string, mode: string) =>
  cells(broker, mode).some((c) => !c.verified);
```
각 핸들 행에서: `const reason = capabilities ? blockedReason(h.broker, h.mode) : null;`
- `reason`이 있으면 행을 **disabled**(`<button disabled>`)로, 부제(`account-picker-sub`)에 `· ${reason}` 노출.
- `pick(h)`의 live confirm 메시지: `hasUnverified(h.broker, h.mode)`면 "이 경로는 아직 실거래 검증 전입니다(모의 검증 권장). " 를 메시지 앞에 덧붙인다.
- onSelect는 그대로(호출부가 broker·ack 처리).

- [ ] **Step 5: 호출부 와이어링 (IrBuilder.tsx + StrategyDetail.tsx)**

공통: `api.autotradeCapabilities()`를 1회 로드(state `capabilities`), 전략 심볼→자산군 집합을 `api.symbols()` 카탈로그의 `autotrade_asset_class`로 매핑(`assetClasses = unique(non-null)`).

- **IrBuilder.tsx**: 빌더는 이미 심볼 카탈로그를 로드함 — 거기서 `autotrade_asset_class` 맵을 만들고 현재 전략의 `def.universe.symbols`로 `assetClasses` 계산. `<AccountPicker>`(1329행)에 `capabilities={capabilities} assetClasses={assetClasses}` 추가. `save` 시그니처를 `save(runMode, accountRef, accountBroker=null, ackUnverified=false)`로 확장하고 `onSelect={(h)=>{ setShowPicker(false); save(h.mode, h.account_id, h.broker, h.mode==="live"); }}`. `save` 내부 `api.createStrategy/updateStrategy(..., accountRef, accountBroker, ackUnverified)`로 전달.
- **StrategyDetail.tsx**: `rebind(h)`(93행)를 `api.updateStrategy(strategy.id, strategy.definition, h.mode, "ir", h.account_id, h.broker, h.mode==="live")`로. AccountPicker(이 페이지 사용처)에 `capabilities`·`assetClasses` 전달 — 이 페이지는 카탈로그가 없으니 `api.symbols()`로 `autotrade_asset_class` 맵을 만들어 `strategy.definition.universe.symbols`로 `assetClasses` 계산.

> 적용 에러(게이트 422)는 기존 `setSaveErr`/`setErr`로 이미 표시됨 — 사용자 알림 경로 유지. 필터는 사전 예방, 게이트는 최종 차단.

- [ ] **Step 6: 빌드 검증 + 커밋**

Run: `cd server && PYTHONPATH=../core python -m pytest tests/test_autotrade_gate.py -q` (서버 필드) → PASS.
Run: `cd web && npm run build` → 타입 에러 0, 빌드 성공.
```bash
git add server/app/routers/backtest.py server/tests/test_autotrade_gate.py web/src/types.ts web/src/api.ts web/src/components/AccountPicker.tsx web/src/pages/IrBuilder.tsx web/src/pages/StrategyDetail.tsx
git commit -m "feat: 적용 시 account_broker/ack 전송 + AccountPicker capability 필터(+autotrade_asset_class)"
```
> 검증 한계(정직): npm build는 타입·컴파일만 보장. 실제 브라우저 E2E(로그인+페어링+적용 422·필터 비활성 확인)는 사장님 영역 — 코드/타입까지만 자동 검증.

---

## Task 8: 웹 — MultiSymbolPicker 라벨(capability 기반) + 일본/홍콩 탭 제거

**Files:**
- Modify: `web/src/components/MultiSymbolPicker.tsx`

- [ ] **Step 1: 일본/홍콩 탭 제거**

`TRADABLE_TAB_ORDER`(34행)에서 `"일본", "홍콩"` 제거:
```typescript
const TRADABLE_TAB_ORDER = [
  "KOSPI", "KOSDAQ", "선물",
  "미국 NASDAQ", "미국 NYSE", "미국 AMEX",
];
```
`categoryFor`의 일본/홍콩 분기(46–47행)도 제거(데이터가 안 오므로 dead).

- [ ] **Step 2: 라벨을 autotrade_hint 기반으로**

뱃지 로직(96–101행 `s.asset_class === "futures" ? "백테스트 전용" : ...`)을 서버 스탬프 `s.autotrade_hint` 기반으로 교체. **Phase 1엔 `autotrade_hint`가 "ok"|"backtest_only"만 (needs_setup 셀 없음)** — needs_setup 분기는 만들지 않는다(4원칙):
```typescript
      badge: s.autotrade_hint === "backtest_only"
        ? "백테스트 전용"                              // 자동매매 불가(지수·매크로·해외선물 등)
        : (scope === "tradable" && s.has_backtest_data === false)
          ? "백테스트 불가"                            // 라이브만 가능·백테스트 데이터 없음
          : undefined,                                 // 자동매매 가능 — 뱃지 없음
```
핵심 효과: **코스피200선물(kr_futures→hint "ok")은 이제 뱃지 없음**(자동매매 가능). 해외선물·지수·매크로(backtest_only)는 "백테스트 전용".
`SymbolInfo`의 `autotrade_hint` 타입은 **Task 7에서 이미 추가됨** — 재추가 불필요. Task 8은 `MultiSymbolPicker.tsx`만 수정.

> Phase 2에서 해외선물이 열리면 서버 스탬프(SSOT)가 자동 "ok"로 바뀌어 웹 변경 없이 뱃지가 사라진다.

- [ ] **Step 3: 타입체크·빌드 검증**

Run: `cd web && npm run build`  (= `tsc -b && vite build` — bun 아님, 타입체크 포함)
Expected: 타입 에러 0, 빌드 성공.

- [ ] **Step 4: 브라우저 검증 — 사장님 영역(정직)**

자동 빌드(Step 3)는 타입·컴파일만 보장. 실제 화면(코스피200선물 뱃지 사라짐·해외선물 "백테스트 전용"·일본/홍콩 탭 부재)은 로그인 필요라 **사장님이 브라우저로 확인**. 코드/타입까지만 자동 검증.

- [ ] **Step 5: Commit**

```bash
git add web/src/components/MultiSymbolPicker.tsx
git commit -m "feat(web): 종목 라벨 capability(autotrade_hint) 기반화 + 일본/홍콩 탭 제거"
```

---

## 최종 검증 (전체 Phase 1)

- [ ] core: `cd core && python -m pytest tests/test_autotrade_capability.py -q` → PASS
- [ ] server: `cd server && PYTHONPATH=../core python -m pytest tests/test_autotrade_gate.py tests/test_strategies_ir.py -q` → PASS
- [ ] server 전체 회귀: `cd server && PYTHONPATH=../core python -m pytest -q` → 신규 0 fail
- [ ] web: `cd web && npm run build` → 성공 (bun 아님)
- [ ] **수동(사장님 영역):** 코스피200선물 모의 전략을 KIS·LS 계좌에 적용 → 게이트 통과 확인(브라우저). 장중 1계약 모의 발주·체결·청산 라운드트립(런북 Phase 1).

## Phase 1 완료 정의

- 코스피200선물을 KIS·LS 계좌(모의/실전)에 바인딩해 자동매매 적용 가능(실전은 미검증 경고-확인).
- 해외선물·일본/홍콩·LS 미국주식 모의 등 미지원 조합은 **명확한 사유로 422 차단** + 빌더 라벨로 표시.
- 기존 주식 자동매매·다른 게이트(레버리지·롱숏·숏) 동작 불변(회귀 0).
- 다음: Phase 2(해외선물 배선) plan.

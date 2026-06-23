# LS증권 Phase F — 해외선물(CME) 자동매매 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** `LsFuturesBroker`에 해외선물(CME) 실행 경로(`overseas_*` 메서드)와 LS 해외선물 심볼 resolver를 추가해, LS 계좌로 원유/금/나스닥선물 등 6종을 자동매매하게 한다 — KIS `KisFuturesBroker` 해외 경로의 1:1 미러.

**Architecture:** `BrokerRouter`는 이미 CME 선물을 `overseas_*` 메서드로 라우팅한다(`broker_router.py:66-74`); CME price/today_open은 0.0 반환(dataset fallback, `:95-112`); CME cancel은 라우터에서 NotImplementedError(`:114-122`); account_snapshot은 `overseas_configured`가 True면 `overseas_account_snapshot()`을 병합(`:151-199`). 따라서 Phase F는 **`LsFuturesBroker`에 overseas 메서드 + 별도 overseas 인증 컨텍스트**, **`LsContractResolver`에 해외선물 해석**, **`secrets_store` overseas 슬롯**, **`runner.make_broker` 게이트**만 추가한다. KIS 듀얼-자격증명 패턴(도메스틱+해외 별도 토큰)을 미러한다.

**Tech Stack:** Python 3.11, requests, pytest/monkeypatch. LS OpenAPI(`:8080`, OAuth2, `tr_cd`, 블록 JSON). 정본 = `docs/ls-api/overseas-futures-research.md`.

---

## 배경 — 구현자(zero-context)가 알아야 할 모든 것

### 이 작업이 어디 들어가나
한국 주식 자동매매 SaaS. 로컬앱(`local/`)이 증권사 REST로 주문 실행. 1번 브로커=KIS, 2번=LS증권. 국내주식(C)·국내선물(D)·해외주식(E) 구현 완료. 이번 **Phase F = 해외(미국)선물(CME)**, 마지막 자산군.

### 절대 규칙
- **수정 파일은 `local/localapp/`의 LS 파일만**: `ls_futures_broker.py`, `ls_futures_contracts.py`, `secrets_store.py`, `runner.py` + 테스트. 그 외(특히 `kis_*.py`, `broker_router.py`, `ls_broker.py`의 `_LsAuth`, `core/`)는 **읽기만, 수정 금지**.
  - 단, `ls_broker.py`의 `_LsAuth`는 **import해서 재사용**(overseas 인증 컨텍스트). 수정하지 말 것.
  - `broker_router.py`는 이미 CME 라우팅 완비 → **변경 불필요**(읽기로 확인만).
- git commit은 각 task 끝. **push/머지 금지.**
- 4원칙: 근본해결·over-engineering 금지(라우터가 안 부르는 메서드 추가 금지)·overthinking 금지·검증된 해결책만.
- Windows: pytest `PYTHONUTF8=1 python -m pytest ...`.

### 재사용 자산
- `from .ls_broker import _LsAuth, normalize_ls_order_resp, canonical_odno` — Phase D에서 이미 `ls_futures_broker.py`가 import. `_LsAuth(creds)`는 독립 인스턴스화 가능(OAuth 토큰·throttle·`_post(path, tr_cd, body, *, is_order=False)` 제공). **overseas 인증은 `self._ov = _LsAuth(overseas_creds)`로 별도 컨텍스트** 구성(KIS `_ov_token` 분리 미러). 같은 appkey면 토큰캐시 공유, 다른 appkey면 별도 토큰 — 어느 쪽이든 동작(G-OF8 robust).
- `normalize_ls_order_resp(raw, *, ordno_field)` → `{success, order_no, message, msg_cd}`. **성공판정=OrdNo 존재**. 해외선물은 `ordno_field="OvrsFutsOrdNo"`.
- `canonical_odno(s)` — 0패딩 제거 매칭(OvrsFutsOrdNo 10자리 0패딩, G-OF9).
- `from quant_core.futures_contract import OVERSEAS_ROOTS` (dataset 심볼→CME globex root, 예 "원유선물"→"CL"), `from quant_core.exec_defaults import instrument_spec` (multiplier 교차검증), `roll_lead_days`.

### KIS 미러 레퍼런스 (읽기 전용)
- `kis_futures_broker.py:320-375` — `__init__` 듀얼 자격증명(`load_kis_futures`+`load_kis_overseas_futures`), `_ov_key/_ov_cano/_ov_token` 별도 컨텍스트, `domestic_configured`/`overseas_configured`.
- `kis_futures_broker.py:525-624` — `overseas_buy/sell/buy_limit/sell_limit`·`overseas_account_snapshot`·`overseas_deposit`·`overseas_cancel`·`overseas_order_status`.
- `kis_overseas_futures.py` — 순수함수: `build_overseas_order_body`(:17)·`build_overseas_cancel_body`(:49, **orgn_ord_dt 필수**)·`parse_overseas_ccld_order_status`(:67)·`parse_overseas_balance`(:118)·`parse_overseas_deposit`(:148, **CRCY_CD=TKR로 KRW**). 롱숏 side: buy/매수, sell/매도.

### LS 해외선물 TR 레퍼런스 (정본 `docs/ls-api/overseas-futures-research.md`)
모든 경로 `_post(path, tr_cd, {InBlock})`. **성공판정=OutBlock2 `OvrsFutsOrdNo` 존재.**

| tr_cd | 용도 | path | request 핵심 | response 핵심 |
|---|---|---|---|---|
| **CIDBT00100** | 신규주문 | `/overseas-futureoption/order` | `OrdDt`·`IsuCodeVal`(ADM23 계약코드)·`FutsOrdTpCode`("1"신규)·`BnsTpCode`("2"매수/"1"매도)·`AbrdFutsOrdPtnCode`("1"시장가/"2"지정가)·`OvrsDrvtOrdPrc`(double)·`OrdQty`(계약수)·`ExchCode`·`DueYymm` | OutBlock2: **`OvrsFutsOrdNo`**·(신규응답 `OrdDt`=취소용 원주문일자) |
| **CIDBT01000** | 취소 | `/overseas-futureoption/order` | `OrdDt`·`IsuCodeVal`·**`OvrsFutsOrgOrdNo`**(0패딩10자리)·`FutsOrdTpCode`("3"취소) | OutBlock2: `OvrsFutsOrdNo` |
| **CIDBQ03000** | 예수금/잔고요약(USD) | `/overseas-futureoption/accno` | `AcntTpCode`("1")·`TrdDt` | OB2[]: `CrcyObjCode`("TOT")·**`EvalAssetAmt`**(평가자산=equity USD)·`PrexchDps`(예탁)·**`AbrdFutsOrdAbleAmt`**(주문가능 USD)·`AbrdFutsCsgnMgn`(위탁증거금)·**`AbrdFutsEvalPnlAmt`**(평가손익) |
| **CIDBQ01500** | 미결제잔고(포지션) | `/overseas-futureoption/accno` | `AcntTpCode`·`BalTpCode`("1") | OB2[]: `IsuCodeVal`·`BnsTpCode`(1매도/2매수)·**`BalQty`**·**`PchsPrc`**(매입단가)·**`OvrsDrvtNowPrc`**(현재가)·`AbrdFutsEvalPnlAmt`·`DueDt` |
| **CIDBQ02400** | 주문체결내역 | `/overseas-futureoption/accno` | `QrySrtDt`·`QryEndDt`·`ThdayTpCode`("1")·`OrdStatCode`("0"전체)·`OvrsDrvtFnoTpCode`("A") | OB2[]: `OvrsFutsOrdNo`·`TrxStatCodeNm`("체결"/"취소"..)·`OrdQty`·**`ExecQty`**·**`UnercQty`**·**`AbrdFutsExecPrc`**(체결가) |
| **CIDBQ05300** | 예탁자산(통화·환율) | `/overseas-futureoption/accno` | `CrcyCode`("USD") | OB2[통화별]: `OvrsFutsDps`·**`Xchrat`**(환율 USD→KRW)·`FcurrRealMxchgAmt` |
| **o3101** | 종목마스터(승수·월물) | `/overseas-futureoption/market-data` | `gubun`("") | OB[]: `Symbol`(ADM23)·**`BscGdsCd`**(기초상품, 예 CL/GC)·`ExchCd`(CME)·**`CtrtPrAmt`**(계약당금액)·`MnChgAmt`(틱가치)·월물·만기 |

### 해외선물 GOTCHAS (research §특화)
- **종목코드 ADM23** = `BscGdsCd + 월물코드(F~Z) + 연2자리`. **KIS 심볼≠LS** → o3101 마스터로 dataset 심볼→LS 코드 해석(resolver).
- **통화 USD.** CIDBQ03000은 USD 합산 → **KRW equity = EvalAssetAmt(USD) × Xchrat(CIDBQ05300)** 환산 필요(KIS는 TKR 직접 — LS는 명시 환산, Phase E `foreign_eval_krw` 동형). ⚠ `Xchrat<=0`이면 raise(−98% 거짓청산 회피).
- **취소는 원주문일자(OrdDt) 필수**(국내선물과 다름·KIS와 동일). 호출부가 신규응답 OrdDt 보관.
- **OvrsFutsOrdNo 0패딩 10자리** → `canonical_odno` 매칭.
- **마진/틱 = o3101/o3105 명시**(CtrtPrAmt·MnChgAmt). resolver 교차검증에 CtrtPrAmt 사용 가능.

### 통화/킬스위치 규칙 (절대 불변)
`broker_router.account_snapshot`은 `overseas_account_snapshot()`의 `account.equity`를 `futures_eval_krw`에 **KRW로 합산**한다(`broker_router.py:173-177` "둘 다 KRW라 FX 추측 없이 직접 합산"). 그러므로 **`overseas_account_snapshot`은 equity를 반드시 KRW로**(USD×Xchrat) 반환해야 한다. `order_cash`도 KRW(사이징 예산). 2-3 TR 중 하나라도 실패하면 raise → 라우터가 `fetch_failed=["futures_overseas"]` 표식(킬스위치 보류).

### 테스트 컨벤션 (`test_ls_futures_resp.py` 패턴)
```python
from localapp import ls_futures_broker as lfb
def _broker():
    return object.__new__(lfb.LsFuturesBroker)   # __init__ 우회
# overseas 메서드는 self._ov._post를 쓰므로, 테스트는 b._ov를 더블로 주입:
class _OvDouble:
    def __init__(self, resp): self._resp = resp; self.calls = []
    def _post(self, path, tr, body, **k): self.calls.append((tr, body)); return self._resp
```
신규 테스트 파일: `local/tests/test_ls_overseas_futures.py`. resolver 테스트는 `test_ls_contract_resolver.py`에 추가 가능(또는 신규).

### 실행/검증
- `cd local && PYTHONUTF8=1 python -m pytest tests/ -q` → 전부 PASS(해외선물 신규 + 회귀).
- KIS byte-identical: `git -C <repo> diff --stat origin/main -- local/localapp/kis_broker.py local/localapp/kis_futures_broker.py` → **빈 출력**.

---

## File Structure
- **Modify:** `local/localapp/ls_futures_broker.py` (overseas_* + 듀얼 인증), `local/localapp/ls_futures_contracts.py` (resolver 해외 확장), `local/localapp/secrets_store.py` (overseas 슬롯), `local/localapp/runner.py` (게이트).
- **Create:** `local/tests/test_ls_overseas_futures.py`.
- **Unchanged (확인만):** `broker_router.py`(CME 라우팅 완비), `ls_broker.py` `_LsAuth`(재사용).

---

### Task F1: secrets 슬롯 + 듀얼 인증 컨텍스트 + overseas_configured

**Files:** Modify `secrets_store.py`, `ls_futures_broker.py`. Test: `test_ls_overseas_futures.py` (신규) + secrets 테스트.

- [ ] **Step 1: 실패 테스트 작성** (`test_ls_overseas_futures.py`)

```python
"""LS 해외선물(CME) 경로 — 듀얼인증·잔고·주문·체결·취소·resolver.
⚠ fixture는 research(overseas-futures-research.md) 기반. 모의 E2E 후 실측 교체."""
from __future__ import annotations
import sys
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))
from localapp import ls_futures_broker as lfb


class _OvDouble:
    """overseas _LsAuth 더블 — _post 캡처."""
    def __init__(self, resp_map=None, resp=None):
        self.resp_map = resp_map or {}   # tr_cd -> resp
        self.resp = resp
        self.calls = []
    def _post(self, path, tr, body, **k):
        self.calls.append((tr, body))
        return self.resp_map.get(tr, self.resp or {})


def test_overseas_configured_true_when_ov_present(monkeypatch):
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble()
    assert b.overseas_configured is True


def test_overseas_configured_false_when_absent():
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = None
    assert b.overseas_configured is False


def test_init_loads_both_creds(monkeypatch):
    """domestic+overseas 둘 다 있으면 self._post=domestic, self._ov=overseas."""
    monkeypatch.setattr(lfb, "load_ls_futures",
                        lambda: {"app_key": "DK", "app_secret": "DS", "account_no": "111", "virtual": True}, raising=False)
    monkeypatch.setattr(lfb, "load_ls_overseas_futures",
                        lambda: {"app_key": "OK", "app_secret": "OS", "account_no": "222", "virtual": True}, raising=False)
    b = lfb.LsFuturesBroker()
    assert b.domestic_configured is True
    assert b.overseas_configured is True
    assert b.key == "DK"           # 도메스틱 인증(상속 _LsAuth)
    assert b._ov.key == "OK"       # 해외 인증(별도 컨텍스트)


def test_init_overseas_only(monkeypatch):
    monkeypatch.setattr(lfb, "load_ls_futures", lambda: None, raising=False)
    monkeypatch.setattr(lfb, "load_ls_overseas_futures",
                        lambda: {"app_key": "OK", "app_secret": "OS", "account_no": "222", "virtual": True}, raising=False)
    b = lfb.LsFuturesBroker()
    assert b.domestic_configured is False
    assert b.overseas_configured is True


def test_init_raises_when_neither(monkeypatch):
    import pytest
    monkeypatch.setattr(lfb, "load_ls_futures", lambda: None, raising=False)
    monkeypatch.setattr(lfb, "load_ls_overseas_futures", lambda: None, raising=False)
    with pytest.raises(RuntimeError):
        lfb.LsFuturesBroker()
```
secrets_store 테스트(`test_secrets_store.py`가 있으면 거기, 없으면 위 파일에): `save_ls_overseas_futures`/`load_ls_overseas_futures` round-trip + `clear()` 포함.

- [ ] **Step 2: 실패 확인.**

- [ ] **Step 3: 구현.**
`secrets_store.py` — Phase D `_LS_FUT` 슬롯 패턴 미러로 `_LS_OVF` 슬롯 + `save_ls_overseas_futures(creds)`/`load_ls_overseas_futures()` 추가, `clear()`에 슬롯 삭제 추가. (Phase D의 `save_ls_futures`/`load_ls_futures` 코드를 그대로 본떠 키 이름만 `_LS_OVF`/`"ls_overseas_futures"`로.)

`ls_futures_broker.py` — import에 `load_ls_overseas_futures` 추가. `__init__` 교체:
```python
    def __init__(self):
        dom = load_ls_futures()
        ovc = load_ls_overseas_futures()
        if not dom and not ovc:
            raise RuntimeError("LS 선물 자격증명이 없습니다. setup에서 등록하세요.")
        # 도메스틱 인증을 베이스로(_LsAuth 상속·self._post=도메스틱·Phase D 무변경).
        # 도메스틱이 없으면 해외 자격증명을 베이스로(해외전용 사용자) — domestic_configured로 게이트.
        super().__init__(dom or ovc)
        self._dom_configured = dom is not None
        # 해외선물 인증은 별도 컨텍스트(KIS _ov_token 분리 미러). 같은 appkey면 토큰캐시 공유.
        self._ov = _LsAuth(ovc) if ovc else None

    @property
    def domestic_configured(self) -> bool:
        return self._dom_configured

    @property
    def overseas_configured(self) -> bool:
        return self._ov is not None
```
> ⚠ 기존 `overseas_configured`(False 하드코딩, :26-27)와 기존 `domestic_configured`(True 하드코딩, :22-23)를 위 property로 교체. 기존 `__init__`(`super().__init__(load_ls_futures())`, raise) 교체. 도메스틱 메서드는 `self._post`(=도메스틱) 그대로 — Phase D 테스트(`b._post` 더블) 호환 유지.

- [ ] **Step 4: 통과 확인** + 전체 회귀(Phase D 도메스틱 테스트 유지 확인).
- [ ] **Step 5: 커밋** — `feat(ls): Phase F1 해외선물 자격증명 슬롯 + 듀얼 인증 컨텍스트`

---

### Task F2: overseas_account_snapshot (CIDBQ03000+CIDBQ01500+CIDBQ05300 → KRW)

**Files:** Modify `ls_futures_broker.py`. Test: `test_ls_overseas_futures.py`.

- [ ] **Step 1: 실패 테스트 추가**

```python
def _ovb(resp_map):
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble(resp_map=resp_map)
    return b


def test_overseas_account_snapshot_krw(monkeypatch):
    b = _ovb({
        "CIDBQ03000": {"CIDBQ03000OutBlock2": [{"CrcyObjCode": "TOT", "EvalAssetAmt": "10000",
                       "AbrdFutsOrdAbleAmt": "8000", "AbrdFutsCsgnMgn": "2000", "AbrdFutsEvalPnlAmt": "150"}]},
        "CIDBQ05300": {"CIDBQ05300OutBlock2": [{"CrcyCode": "USD", "Xchrat": "1350.0"}]},
        "CIDBQ01500": {"CIDBQ01500OutBlock2": [{"IsuCodeVal": "CLM26", "BnsTpCode": "2", "BalQty": "2",
                       "PchsPrc": "70.50", "OvrsDrvtNowPrc": "71.20", "AbrdFutsEvalPnlAmt": "150"}]},
    })
    snap = b.overseas_account_snapshot()
    # equity_krw = 10000 USD * 1350 ; order_cash_krw = 8000 * 1350
    assert snap["account"]["equity"] == 10000 * 1350.0
    assert snap["account"]["order_cash"] == 8000 * 1350.0
    assert snap["account"]["currency"] == "KRW"
    p = snap["positions"][0]
    assert p["symbol"] == "CLM26" and p["side"] == "long" and p["qty"] == 2
    assert p["currency"] == "USD" and p["asset_class"] == "futures"


def test_overseas_account_snapshot_short(monkeypatch):
    b = _ovb({
        "CIDBQ03000": {"CIDBQ03000OutBlock2": [{"EvalAssetAmt": "1", "AbrdFutsOrdAbleAmt": "1"}]},
        "CIDBQ05300": {"CIDBQ05300OutBlock2": [{"CrcyCode": "USD", "Xchrat": "1300"}]},
        "CIDBQ01500": {"CIDBQ01500OutBlock2": [{"IsuCodeVal": "GCM26", "BnsTpCode": "1", "BalQty": "1",
                       "PchsPrc": "2000", "OvrsDrvtNowPrc": "1990"}]},
    })
    assert b.overseas_account_snapshot()["positions"][0]["side"] == "short"


def test_overseas_account_snapshot_raises_on_zero_xchrat():
    """Xchrat<=0 → raise(라우터 fetch_failed·−98% 거짓청산 회피, 0 위장 금지)."""
    import pytest
    b = _ovb({
        "CIDBQ03000": {"CIDBQ03000OutBlock2": [{"EvalAssetAmt": "10000", "AbrdFutsOrdAbleAmt": "8000"}]},
        "CIDBQ05300": {"CIDBQ05300OutBlock2": [{"CrcyCode": "USD", "Xchrat": "0"}]},
        "CIDBQ01500": {"CIDBQ01500OutBlock2": []},
    })
    with pytest.raises(Exception):
        b.overseas_account_snapshot()
```

- [ ] **Step 2: 실패 확인.**

- [ ] **Step 3: 구현** (`ls_futures_broker.py`):
```python
    def _ov_acct_raw(self) -> dict:
        from datetime import datetime
        return self._ov._post("/overseas-futureoption/accno", "CIDBQ03000",
                              {"CIDBQ03000InBlock1": {"AcntTpCode": "1", "TrdDt": datetime.now().strftime("%Y%m%d")}})

    def _ov_xchrat_raw(self) -> dict:
        return self._ov._post("/overseas-futureoption/accno", "CIDBQ05300",
                              {"CIDBQ05300InBlock1": {"CrcyCode": "USD"}})

    def _ov_positions_raw(self) -> dict:
        return self._ov._post("/overseas-futureoption/accno", "CIDBQ01500",
                              {"CIDBQ01500InBlock1": {"AcntTpCode": "1", "BalTpCode": "1"}})

    def overseas_account_snapshot(self) -> dict:
        """해외선물 잔고 — {account(KRW), positions}. KRW equity = USD × Xchrat(CIDBQ05300).
        2-3 TR 중 실패·Xchrat<=0은 raise(라우터 fetch_failed). ⚠ 필드명·G-OF5(USD→KRW 경로) research 기반 — 모의 실측."""
        acct_rows = self._ov_acct_raw().get("CIDBQ03000OutBlock2") or []
        acct = {}
        for r in acct_rows:
            if str(r.get("CrcyObjCode") or "TOT") in ("TOT", "", "USD"):
                acct = r
                break
        if not acct and acct_rows:
            acct = acct_rows[0]
        equity_usd = float(acct.get("EvalAssetAmt") or 0)
        order_cash_usd = float(acct.get("AbrdFutsOrdAbleAmt") or 0)
        xrows = self._ov_xchrat_raw().get("CIDBQ05300OutBlock2") or []
        xchrat = 0.0
        for r in xrows:
            if str(r.get("CrcyCode") or "") in ("USD", ""):
                xchrat = float(r.get("Xchrat") or 0)
                break
        if xchrat <= 0:   # 환율 미수신 → equity KRW 환산 불가 → raise(0 위장 금지·−98% 회피)
            raise RuntimeError(f"LS 해외선물 환율(Xchrat) 미수신({xchrat}) — KRW equity 산출 불가. 보류.")
        account = {
            "equity": equity_usd * xchrat,            # KRW (킬스위치)
            "order_cash": order_cash_usd * xchrat,    # KRW (사이징)
            "margin_total": float(acct.get("AbrdFutsCsgnMgn") or 0) * xchrat,
            "eval_pnl": float(acct.get("AbrdFutsEvalPnlAmt") or 0) * xchrat,
            "currency": "KRW", "fx_usdkrw": xchrat,
        }
        positions = []
        for it in (self._ov_positions_raw().get("CIDBQ01500OutBlock2") or []):
            qty = int(float(it.get("BalQty") or 0))
            if qty == 0:
                continue
            positions.append({
                "symbol": str(it.get("IsuCodeVal") or "").strip(),
                "side": "long" if str(it.get("BnsTpCode") or "") == "2" else "short",
                "qty": qty, "avg_price": float(it.get("PchsPrc") or 0),
                "eval_price": float(it.get("OvrsDrvtNowPrc") or 0),
                "eval_pnl": float(it.get("AbrdFutsEvalPnlAmt") or 0),
                "market": "OVERSEAS", "currency": "USD", "asset_class": "futures",
            })
        return {"account": account, "positions": positions}
```
> 라우터가 `account.equity`(KRW)를 `futures_eval_krw`에 합산하고, positions의 `symbol`(IsuCodeVal=계약코드)을 `dataset_for_code`로 데이터셋 심볼 정규화(F5).

- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** — `feat(ls): Phase F2 해외선물 잔고 CIDBQ03000+01500+05300(USD→KRW)`

---

### Task F3: 해외선물 주문 overseas_buy/sell/limit (CIDBT00100)

**Files:** Modify `ls_futures_broker.py`. Test 추가.

- [ ] **Step 1: 실패 테스트 추가**

```python
def test_overseas_buy_market(monkeypatch):
    b = object.__new__(lfb.LsFuturesBroker)
    ov = _OvDouble(resp={"CIDBT00100OutBlock2": {"OvrsFutsOrdNo": "0000000777", "OrdDt": "20260622"}})
    b._ov = ov
    r = b.overseas_buy("CLM26", 1)
    assert r["success"] is True and r["order_no"] == "0000000777"
    tr, body = ov.calls[-1]
    bd = body["CIDBT00100InBlock1"]
    assert tr == "CIDBT00100"
    assert bd["FutsOrdTpCode"] == "1"        # 신규
    assert bd["BnsTpCode"] == "2"            # 매수
    assert bd["AbrdFutsOrdPtnCode"] == "1"   # 시장가
    assert bd["IsuCodeVal"] == "CLM26"


def test_overseas_sell_limit_double_price(monkeypatch):
    b = object.__new__(lfb.LsFuturesBroker)
    ov = _OvDouble(resp={"CIDBT00100OutBlock2": {"OvrsFutsOrdNo": "8"}})
    b._ov = ov
    b.overseas_sell_limit("GCM26", 2, 1985.5)
    bd = ov.calls[-1][1]["CIDBT00100InBlock1"]
    assert bd["BnsTpCode"] == "1"            # 매도
    assert bd["AbrdFutsOrdPtnCode"] == "2"   # 지정가
    assert bd["OvrsDrvtOrdPrc"] == 1985.5    # double — int 절삭 금지


def test_overseas_order_reject_no_ordno(monkeypatch):
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble(resp={"rsp_cd": "99", "rsp_msg": "증거금부족"})
    r = b.overseas_buy("CLM26", 1)
    assert r["success"] is False and r["order_no"] == ""
```

- [ ] **Step 2: 실패 확인.**

- [ ] **Step 3: 구현:**
```python
    def _ov_submit(self, symbol, qty, side, ord_ptn, unit_price):
        """CIDBT00100 신규주문. BnsTpCode 2매수/1매도, AbrdFutsOrdPtnCode 1시장/2지정. 가격 double."""
        from datetime import datetime
        prc = float(unit_price) if ord_ptn == "2" else 0
        resp = self._ov._post("/overseas-futureoption/order", "CIDBT00100",
                              {"CIDBT00100InBlock1": {
                                  "OrdDt": datetime.now().strftime("%Y%m%d"),
                                  "IsuCodeVal": symbol, "FutsOrdTpCode": "1",
                                  "BnsTpCode": "2" if side == "buy" else "1",
                                  "AbrdFutsOrdPtnCode": ord_ptn, "OvrsDrvtOrdPrc": prc,
                                  "OrdQty": qty}}, is_order=True)
        return normalize_ls_order_resp(resp, ordno_field="OvrsFutsOrdNo")

    def overseas_buy(self, symbol, qty): return self._ov_submit(symbol, qty, "buy", "1", 0)
    def overseas_sell(self, symbol, qty): return self._ov_submit(symbol, qty, "sell", "1", 0)
    def overseas_buy_limit(self, symbol, qty, limit_price): return self._ov_submit(symbol, qty, "buy", "2", float(limit_price))
    def overseas_sell_limit(self, symbol, qty, limit_price): return self._ov_submit(symbol, qty, "sell", "2", float(limit_price))
```
> 롱숏=BnsTpCode net(진입/청산 별도코드 없음). `ExchCode`/`DueYymm`는 IsuCodeVal로 충분하면 생략(⚠ 필요 시 G-OF2 — 모의 실측서 추가). 시장가→지정가 quote 변환은 **불필요**(라우터가 CME price=0.0라 catch-up이 dataset ref 사용·KIS 해외선물도 시장가 직접 — broker_router.py:106-112). 즉 overseas_buy/sell는 시장가 "1" 직접 발주.

- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** — `feat(ls): Phase F3 해외선물 주문 CIDBT00100`

---

### Task F4: overseas_order_status (CIDBQ02400) + overseas_cancel (CIDBT01000)

**Files:** Modify `ls_futures_broker.py`. Test 추가.

> `overseas_order_status`는 라우터가 호출(`broker_router.py:130`). `overseas_cancel`은 라우터가 직접 호출 안 하나(CME cancel은 라우터서 NotImplementedError, M10 직접배선 대상·`:117`) KIS가 보유(`kis_futures_broker.py:592`)하고 모의 E2E 취소 검증에 필요 → 포함.

- [ ] **Step 1: 실패 테스트 추가**

```python
def _ov_ccld(rows):
    return {"CIDBQ02400OutBlock2": rows}


def test_overseas_order_status_filled():
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble(resp=_ov_ccld([{"OvrsFutsOrdNo": "0000000777", "OrdQty": "1", "ExecQty": "1",
                       "UnercQty": "0", "AbrdFutsExecPrc": "71.20", "TrxStatCodeNm": "체결"}]))
    st = b.overseas_order_status("777")
    assert st["status"] == "filled" and st["filled_qty"] == 1 and st["fill_price"] == 71.20


def test_overseas_order_status_partial():
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble(resp=_ov_ccld([{"OvrsFutsOrdNo": "7", "OrdQty": "5", "ExecQty": "2",
                       "UnercQty": "3", "AbrdFutsExecPrc": "71.0"}]))
    assert b.overseas_order_status("7")["status"] == "partial"


def test_overseas_order_status_cancelled():
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble(resp=_ov_ccld([{"OvrsFutsOrdNo": "5", "OrdQty": "1", "ExecQty": "0",
                       "UnercQty": "0", "TrxStatCodeNm": "취소"}]))
    assert b.overseas_order_status("5")["status"] == "cancelled"


def test_overseas_order_status_unknown():
    b = object.__new__(lfb.LsFuturesBroker)
    b._ov = _OvDouble(resp=_ov_ccld([]))
    assert b.overseas_order_status("999")["status"] == "unknown"


def test_overseas_cancel_body():
    b = object.__new__(lfb.LsFuturesBroker)
    ov = _OvDouble(resp={"CIDBT01000OutBlock2": {"OvrsFutsOrdNo": "55"}})
    b._ov = ov
    r = b.overseas_cancel("777", "CLM26", 1, "20260622")
    assert r["success"] is True and "order_no" not in r
    bd = ov.calls[-1][1]["CIDBT01000InBlock1"]
    assert bd["FutsOrdTpCode"] == "3"            # 취소
    assert bd["OvrsFutsOrgOrdNo"] == "777"
    assert bd["OrdDt"] == "20260622"             # 원주문일자 필수
```

- [ ] **Step 2: 실패 확인.**

- [ ] **Step 3: 구현:**
```python
    def _ov_ccld_raw(self, only_unfilled: bool = False) -> dict:
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        return self._ov._post("/overseas-futureoption/accno", "CIDBQ02400",
                              {"CIDBQ02400InBlock1": {"QrySrtDt": today, "QryEndDt": today,
                                                      "ThdayTpCode": "1",
                                                      "OrdStatCode": "2" if only_unfilled else "0",
                                                      "OvrsDrvtFnoTpCode": "A"}})

    def overseas_order_status(self, order_no) -> dict:
        """CIDBQ02400 OvrsFutsOrdNo 매칭 → filled/partial/cancelled/submitted.
        ⚠ G-OF4: TrxStatCodeNm 문자열 실측 전 — '취소' 포함 시 cancelled, 그 외 Exec/Unerc로 판정."""
        try:
            rows = self._ov_ccld_raw().get("CIDBQ02400OutBlock2") or []
        except Exception as e:
            log.warning("LS 해외선물 order_status 실패 [%s]: %s", order_no, e)
            return {"order_no": order_no, "status": "unknown", "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}
        for row in rows:
            if canonical_odno(row.get("OvrsFutsOrdNo")) != canonical_odno(order_no):
                continue
            ex = int(float(row.get("ExecQty") or 0))
            un = int(float(row.get("UnercQty") or 0))
            nm = str(row.get("TrxStatCodeNm") or "")
            if "취소" in nm:
                st = "cancelled"
            elif un == 0 and ex > 0:
                st = "filled"
            elif ex > 0:
                st = "partial"
            else:
                st = "submitted"
            return {"order_no": order_no, "status": st, "filled_qty": ex, "remain_qty": un,
                    "fill_price": float(row.get("AbrdFutsExecPrc") or 0)}
        return {"order_no": order_no, "status": "unknown", "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}

    def overseas_cancel(self, order_no, symbol, orig_dt_or_qty=None, orig_dt=None):
        """CIDBT01000 취소 — 원주문일자(OrdDt) 필수(KIS overseas_cancel 미러).
        호출 형태 overseas_cancel(order_no, symbol, qty, orgn_ord_dt). qty는 LS 취소에 불요(전량)."""
        from datetime import datetime
        # 시그니처 유연성: (order_no, symbol, qty, orgn_ord_dt) 또는 (order_no, symbol, orgn_ord_dt)
        odt = orig_dt if orig_dt is not None else (orig_dt_or_qty if isinstance(orig_dt_or_qty, str) else None)
        resp = self._ov._post("/overseas-futureoption/order", "CIDBT01000",
                              {"CIDBT01000InBlock1": {
                                  "OrdDt": str(odt or datetime.now().strftime("%Y%m%d")),
                                  "IsuCodeVal": symbol, "OvrsFutsOrgOrdNo": str(order_no),
                                  "FutsOrdTpCode": "3"}}, is_order=True)
        r = normalize_ls_order_resp(resp, ordno_field="OvrsFutsOrdNo")
        return {"success": r["success"], "message": r["message"], "msg_cd": r["msg_cd"]}
```
> ⚠ `overseas_cancel` 시그니처는 KIS(`overseas_cancel(order_no, qty, orgn_ord_dt)`)와 라우터 미사용이라 자유롭지만, 위 테스트 형태 `(order_no, symbol, qty, orgn_ord_dt)`로 고정. 단순화 위해 `qty` 미사용이면 받되 무시(LS 취소는 OvrsFutsOrgOrdNo+OrdDt만). **미사용 인자면 제거 고려(4원칙#2)** — 단 OrdDt는 필수.

- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** — `feat(ls): Phase F4 해외선물 체결조회 CIDBQ02400 + 취소 CIDBT01000`

---

### Task F5: 해외선물 resolver (o3101 마스터) + dataset_for_code

**Files:** Modify `ls_futures_contracts.py`, `ls_futures_broker.py` (overseas_futures_master). Test: `test_ls_contract_resolver.py` 추가 또는 신규.

> **최대 미지수.** LS o3101 마스터 형식·근월물 선택을 research 기반으로 구현(Phase D t8432 패턴 미러). 불일치 시 `resolve→None`→발주 skip(오발주 0 안전).

- [ ] **Step 1: 실패 테스트 추가**

```python
from localapp import ls_futures_contracts as lfc


class _MasterBroker:
    def __init__(self, rows): self._rows = rows
    def overseas_futures_master(self): return self._rows


def test_resolve_overseas_front_month(monkeypatch):
    # o3101 마스터: CL(원유) 2개 월물 — 근월물 선택
    rows = [
        {"Symbol": "CLK26", "BscGdsCd": "CL", "ExchCd": "CME", "CtrtPrAmt": "1000"},
        {"Symbol": "CLM26", "BscGdsCd": "CL", "ExchCd": "CME", "CtrtPrAmt": "1000"},
    ]
    r = lfc.LsContractResolver(_MasterBroker(rows))
    # 원유선물 → CL 근월물 코드(과거월 제외·roll 반영) — None 아님, CL로 시작
    code = r.resolve("원유선물")
    assert code is not None and code.startswith("CL")


def test_dataset_for_code_overseas():
    r = lfc.LsContractResolver(_MasterBroker([]))
    assert r.dataset_for_code("CLM26") == "원유선물"
    assert r.dataset_for_code("GCM26") == "금선물"
    assert r.dataset_for_code("101V6000") == "코스피200선물"   # 도메스틱 유지(Phase D)
    assert r.dataset_for_code("UNKNOWN") is None


def test_resolve_overseas_unknown_root_none(monkeypatch):
    r = lfc.LsContractResolver(_MasterBroker([{"Symbol": "ESM26", "BscGdsCd": "ES", "ExchCd": "CME"}]))
    assert r.resolve("원유선물") is None   # 마스터에 CL 없음 → None(발주 skip)
```

- [ ] **Step 2: 실패 확인.**

- [ ] **Step 3: 구현.**
`ls_futures_broker.py` — overseas 마스터 fetch 추가:
```python
    def overseas_futures_master(self) -> list[dict]:
        """o3101 해외선물 종목마스터 — Symbol(ADM23)·BscGdsCd·CtrtPrAmt. resolver가 1일 캐시."""
        body = self._ov._post("/overseas-futureoption/market-data", "o3101", {"o3101InBlock": {"gubun": ""}})
        return body.get("o3101OutBlock") or []
```
`ls_futures_contracts.py` — `LsContractResolver`에 해외 분기 추가. `OVERSEAS_ROOTS`(dataset 심볼↔CME root)를 quant_core에서 import해 매핑. `resolve`에 overseas 분기, `dataset_for_code_static`에 overseas 역매핑, overseas 마스터 1일 캐시:
```python
from quant_core.futures_contract import OVERSEAS_ROOTS  # {"원유선물":"CL","금선물":"GC",...}
# ... 기존 import 유지

class LsContractResolver:
    def __init__(self, futures_broker):
        self.broker = futures_broker
        self._master = None; self._fetched = None             # 도메스틱(t8432)
        self._ov_master = None; self._ov_fetched = None       # 해외(o3101)

    # resolve(symbol): 기존 코스피200(도메스틱) 분기 유지 + 아래 추가
    def resolve(self, symbol):
        if not qc.is_futures(symbol):
            return symbol
        today = datetime.date.today()
        if symbol == _KOSPI200:
            self._ensure(today)                               # 기존 t8432
            return _pick_front_kospi200(self._master, today) if self._master else None
        root = OVERSEAS_ROOTS.get(symbol)                     # 원유선물→CL 등
        if root:
            self._ensure_overseas(today)
            return _pick_front_overseas(self._ov_master, root, symbol, today) if self._ov_master else None
        return None

    def _ensure_overseas(self, today):
        if self._ov_fetched == today:
            return
        try:
            self._ov_master = self.broker.overseas_futures_master()
        except Exception:
            self._ov_master = None
        self._ov_fetched = today
```
모듈 함수 `_pick_front_overseas(master, root, symbol, today)` — o3101에서 `BscGdsCd==root` 행 필터, **CtrtPrAmt로 multiplier 교차검증**(`instrument_spec(symbol).multiplier`와 근사 일치, KIS `parse_front_month_overseas` 미러), Symbol의 월물코드(F~Z)+연도로 근월물 선택(roll lead 반영), 만기경과·스프레드 제외. 미일치 → None.
`dataset_for_code_static` 확장:
```python
    @staticmethod
    def dataset_for_code_static(code):
        if code and code.startswith("101"):
            return _KOSPI200
        # 해외: 앞 1~3자 BscGdsCd가 OVERSEAS_ROOTS 값과 일치 → dataset 심볼
        for sym, root in OVERSEAS_ROOTS.items():
            if code and code[:len(root)] == root and code[len(root):len(root)+1].isalpha():
                return sym
        return None
```
> ⚠ G-OF: o3101 필드명(`Symbol`/`BscGdsCd`/`CtrtPrAmt`)·월물코드 파싱·LS BscGdsCd가 CME globex root와 동일한지(금 GC 등)는 research 기반 — **모의 실측 확정**(불일치 시 resolve None→발주 skip 안전, 거래 불가). `_pick_front_overseas`의 월물 파싱은 Phase D `_pick_front_kospi200`(2nd목요일) 대신 CME 월물코드(F1월~Z12월) 기준.

- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** — `feat(ls): Phase F5 해외선물 resolver o3101 + dataset_for_code 역매핑`

---

### Task F6: runner.make_broker 게이트 + 통합

**Files:** Modify `runner.py`. Test: `test_ls_futures_wiring.py` 추가.

- [ ] **Step 1: 실패 테스트 추가** (wiring — make_broker가 해외선물 자격증명만 있어도 BrokerRouter 반환)

```python
# test_ls_futures_wiring.py 패턴(Phase D)에 맞춰:
def test_make_broker_ls_overseas_futures_only(monkeypatch):
    # active_broker=ls, 도메스틱 선물 없음·해외선물 있음 → BrokerRouter 반환
    # (구체 monkeypatch는 기존 test_ls_futures_wiring.py의 make_broker 더블 패턴 따름)
    ...
```
> 기존 `test_ls_futures_wiring.py`의 패턴을 그대로 따라, `load_ls_overseas_futures`를 추가로 stub하고 게이트가 `load_ls_futures() or load_ls_overseas_futures()`로 동작함을 검증.

- [ ] **Step 2: 실패 확인.**

- [ ] **Step 3: 구현** (`runner.py` LS 브랜치) — Phase D 게이트 `if load_ls_futures()`를 `if load_ls_futures() or load_ls_overseas_futures()`로 확장(KIS `:74` 미러). LsFuturesBroker()는 F1에서 양 자격증명을 내부 로드하므로 생성자 인자 변경 없음. resolver(LsContractResolver)는 동일 브로커로 도메스틱·해외 마스터 둘 다 fetch.
```python
    from .secrets_store import load_ls_futures, load_ls_overseas_futures
    if load_ls_futures() or load_ls_overseas_futures():
        from .ls_futures_broker import LsFuturesBroker
        from .ls_futures_contracts import LsContractResolver
        from .broker_router import BrokerRouter
        r = LsContractResolver(LsFuturesBroker())
        return BrokerRouter(stock, r.broker, resolve=r.resolve,
                            resolve_expiry=r.resolve_expiry, dataset_for_code=r.dataset_for_code)
    return stock
```
> ⚠ 기존 Phase D LS 브랜치를 위로 교체(게이트만 확장). `broker_router.py`는 무변경(CME 라우팅 완비).

- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** — `feat(ls): Phase F6 make_broker 해외선물 게이트 확장`

---

### Task F7: 통합 검증 + KIS byte-identical + 최종 리뷰

- [ ] **Step 1: 전체 테스트** — `cd local && PYTHONUTF8=1 python -m pytest tests/ -q` → 전부 PASS(해외선물 신규 + D/E/C 회귀).
- [ ] **Step 2: KIS byte-identical** — `git -C <repo> diff --stat origin/main -- local/localapp/kis_broker.py local/localapp/kis_futures_broker.py` → **빈 출력**. `broker_router.py` diff도 확인(무변경이어야 함).
- [ ] **Step 3: lint** — `ruff check local/localapp/ls_futures_broker.py ls_futures_contracts.py` → 신규 위반 0.
- [ ] **Step 4: 표면 점검(4원칙#2)** — overseas_cancel `qty` 등 미사용 인자 제거. 라우터가 안 부르는 메서드(overseas_pending/price/orderable) 미추가 확인.
- [ ] **Step 5: 최종 종합 리뷰**(opus) — 전 Phase F diff vs plan·research·KIS 미러·4원칙·byte-identical.

---

## Gaps (모의 E2E 실측)
- **G-OF1** 모의 체결 시뮬 작동 · **G-OF2** 주문 추가필드(ExchCode/DueYymm 필요 여부) · **G-OF4** CIDBQ02400 TrxStatCodeNm 문자열 집합 · **G-OF5** USD→KRW equity(CIDBQ05300 KRW행 직접 vs Xchrat 환산) · **G-OF6** 취소 OrdDt 출처(신규응답 보관) · **G-OF8** AcntNo/Pwd body 필요 여부 → 별도 인증 컨텍스트가 robust하나, 같은 appkey+AcntNo body 방식이면 단순화 가능 · **resolver**: o3101 필드명·월물코드·LS BscGdsCd vs CME root 동일성.
- 미해결은 안전쪽(resolve 실패→발주 skip, fetch/Xchrat 실패→raise→fetch_failed). 오발주/오청산 0.

## 자기검토 (writing-plans)
- **스펙 커버리지**: 라우터가 호출하는 overseas_* 전부(buy/sell/limit·account_snapshot·order_status) + 듀얼인증·resolver·게이트. cancel은 E2E/M10용. price/pending/orderable은 라우터 미호출→미포함(4원칙#2). ✓
- **타입 일관성**: `overseas_account_snapshot`→`{account:{equity(KRW),order_cash,currency},positions}`(라우터 합산 계약)·`OvrsFutsOrdNo` 판정·`canonical_odno`·`OVERSEAS_ROOTS` 전 task 일관. ✓
- **byte-identical**: LS 파일만 수정, KIS·broker_router 무변경. ✓
- **킬스위치 KRW**: equity=USD×Xchrat, Xchrat<=0 raise(−98% 회피). ✓

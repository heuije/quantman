# LS증권 Phase E — 해외주식(미국) 자동매매 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `LsBroker`(국내주식 전용)에 미국주식 실행 경로를 내장해, LS 계좌 하나로 국내+해외주식을 자동매매하게 한다 — KIS `KisBroker` 해외 경로의 1:1 미러.

**Architecture:** 단일 파일 `local/localapp/ls_broker.py`만 수정한다(+테스트). `LsBroker`의 공개 메서드(`buy/sell/buy_limit/sell_limit/cancel/order_status/pending_orders/price/today_open/account_snapshot`)가 시장을 판정해 국내(기존 TR) 또는 해외(신규 LS 해외 TR)로 분기한다. 시장 판정은 **브로커 무관 모듈 `market_index`를 재사용**(KIS와 동일 권위 소스). 예약주문(`buy_resv_limit/sell_resv_limit`)은 현재 `NotImplementedError`인데, 해외분이면 LS 예약 TR로 구현한다. **KIS 파일·공유 파일은 전혀 건드리지 않으므로 KIS byte-identical은 자동 보장**(회귀 테스트로 확인만).

**Tech Stack:** Python 3.11, requests, pytest/monkeypatch. LS OpenAPI(`openapi.ls-sec.co.kr:8080`, OAuth2, `tr_cd` 헤더, 블록 JSON). 정본 = `docs/ls-api/overseas-stock-research.md`.

---

## 배경 — 구현자(zero-context)가 알아야 할 모든 것

### 이 작업이 어디 들어가나
한국 주식 자동매매 SaaS. 사용자 PC의 로컬앱(`local/`)이 증권사 REST로 주문을 실행한다. 1번 브로커는 KIS(`kis_broker.py`), 우리는 2번 브로커 **LS증권**(`ls_broker.py`)을 만든다. 국내주식(Phase C)·국내선물(Phase D)은 이미 구현 완료. 이번 **Phase E = 해외(미국)주식**.

### 절대 규칙
- **수정 파일은 `local/localapp/ls_broker.py` 단 하나 + 테스트 파일.** 그 외(특히 `kis_broker.py`, `kis_futures_broker.py`, `broker_router.py`, `market_index.py`, `core/`)는 **읽기만, 수정 금지.** 공유 파일을 고쳐야 할 것 같으면 멈추고 보고.
- **`market_index.py`는 재사용만**(import해서 호출). 거기에 LS 거래소코드(82/81)를 넣지 말 것 — 그건 LS 전용이라 `ls_broker.py`에 둔다. `market_index`는 NAS/NYS/AMS(브로커 무관)를 반환.
- git commit은 각 task 끝에서(아래 명시). **push/머지 금지.**
- 4원칙: 근본해결·over-engineering 금지·overthinking 금지·검증된 해결책만. KIS가 안 하는 표면(미사용 옵션·추측 분기)을 추가하지 말 것.

### 재사용 자산 (이미 `ls_broker.py`에 존재 — 그대로 씀)
- `class _LsAuth`: OAuth 토큰·throttle·`_post(path, tr_cd, body, *, is_order=False)`. **모든 LS HTTP는 이걸로.** `LsBroker(_LsAuth)`라 `self._post(...)` 가능.
- `normalize_ls_order_resp(raw, *, ordno_field)` → `{success, order_no, message, msg_cd}`. **성공판정 = OrdNo 존재**(rsp_cd 아님 — LS는 TR마다 rsp_cd가 달라 매수 00040 등). 모든 주문·취소 응답을 이걸로 정규화.
- `canonical_odno(s)`: 주문번호 비교용 정규화(선행 0 제거). 조회 매칭에 사용.
- `self.account_no`(하이픈 제거됨), `self.virtual`(모의 여부).

### 재사용 자산 (`market_index` 모듈 — import해서 호출)
```python
from . import market_index
market_index.exchange_of(symbol)   # "NAS"/"NYS"/"AMS" 또는 None(미국 아님)
market_index.is_us(symbol)         # bool
market_index.kis_ticker_of(symbol) # KIS PDNO용 슬래시형(BRK/B). LS는 bare 티커가 필요 → 아래 _ls_ticker 참조
market_index.RoutingError          # 거래소 미확정 시 발주 차단 예외
market_index._looks_domestic(symbol) # 6자리 국내코드 형태 bool
```

### LS 해외주식 TR 레퍼런스 (정본 `docs/ls-api/overseas-stock-research.md`)
모든 경로는 `_post(path, tr_cd, {InBlock})`. 응답 `{rsp_cd, rsp_msg, <TRcd>OutBlock1/2/3/4}`.

| tr_cd | 용도 | path | request 핵심 | response 핵심 |
|---|---|---|---|---|
| **COSAT00301** | 미국 주문(매수`OrdPtnCode`=02/매도=01/취소=08) | `/overseas-stock/order` | `OrdPtnCode`·`OrgOrdNo`(취소시)·`OrdMktCode`(82/81)·`IsuNo`(bare "AAPL")·`OrdQty`·`OvrsOrdPrc`(지정가 USD float, 시장가 0)·`OrdprcPtnCode`(00지정/03시장) | `COSAT00301OutBlock2.OrdNo` |
| **COSAT00400** | 해외 예약주문 등록/취소 | `/overseas-stock/order` | `TrxTpCode`(등록/취소)·`CntryCode`("US")·`BnsTpCode`(2매수/1매도)·**`AcntNo`·`Pwd`(필수)**·`FcurrMktCode`(82/81)·`IsuNo`·`OrdQty`·`OvrsOrdPrc`·`OrdprcPtnCode`(00)·`RsvOrdSrtDt`·`RsvOrdEndDt`(YYYYMMDD) | `COSAT00400OutBlock2.RsvOrdNo` |
| **COSOQ00201** | 해외 종합잔고평가(잔고+환율) | `/overseas-stock/accno` | `BaseDt`(YYYYMMDD)·`CrcyCode`("USD")·`AstkBalTpCode`("00") | OB3[통화별]: `FcurrDps`(외화예수금)·`BaseXchrat`(환율) / OB4[종목별]: `ShtnIsuNo`(티커)·`AstkBalQty`·`FcstckUprc`(매입단가)·`OvrsScrtsCurpri`(현재가)·`FcurrMktCode` |
| **COSAQ00102** | 계좌주문체결내역 | `/overseas-stock/accno` | `OrdDt`(YYYYMMDD)·`ExecYn`(0전체/1체결/2미체결)·`SrtOrdNo`("999999999")·`IsuNo`("") | OB3[]: `OrdNo`·`OrgOrdNo`·`ShtnIsuNo`·`OrdQty`·`ExecQty`(체결)·`OvrsExecPrc`(체결가)·`UnercQty`(미체결)·`OrdTrxPtnNm`("접수완료"/"체결"/"정정완료"/"취소완료") |
| **COSAQ01400** | 예약주문 처리결과 | `/overseas-stock/accno` | `CntryCode`("001")·`SrtDt`·`EndDt`·`RsvOrdStatCode` | OB2[]: 예약내역(RsvOrdNo·상태) |
| **g3101** | 해외 현재가(USD OHLC) | `/overseas-stock/market-data` | `delaygb`("R")·`keysymbol`(exchcd+티커 "82TSLA")·`exchcd`(82/81)·`symbol`(bare) | OB: `price`·`open`·`high`·`low`·`currency`·`volume` |

### 해외 GOTCHAS (research §해외특화)
1. **시장코드 2분할**: `82`=NASDAQ, `81`=NYSE+AMEX (AMEX가 NYSE와 합쳐짐 — KIS의 NAS/NYS/AMS 3분할과 다름). → `market_index.exchange_of`의 NAS→"82", NYS→"81", **AMS→"81"** 매핑 필요.
2. **종목코드 = bare 티커**(A접두 없음). 시세 `keysymbol`=exchcd+symbol.
3. **FX 전용 TR 없음** — USD/KRW는 COSOQ00201 OB3 `BaseXchrat`에 내장. KIS `frst_bltn_exrt`와 동일 패턴.
4. **가격 float**(소수 USD $0.01 틱) — `int` 절삭 금지.
5. **AcntNo/Pwd body**: 정규주문·잔고·체결조회·시세는 **미포함**(토큰서 도출). **예약(COSAT00400)만 포함 필수**.
6. **성공판정 = OrdNo 존재**(예약=RsvOrdNo). `normalize_ls_order_resp` 그대로 사용.
7. **TPS**: 계좌/체결조회=1/s(보수적). `_LsAuth._post`의 전역 throttle로 충분(추가 throttle 불요).
8. **미국 시장가(03) 모의지원 미검증(OG3)** → KIS처럼 **해외는 항상 지정가(00)로 발주**, 시장가 의도면 g3101 현재가로 대체. 검증된 KIS 패턴.

### 통화/킬스위치 규칙 (절대 불변)
킬스위치는 `trader._unified_equity_krw`에서 `total_eval(KRW) + foreign_eval_krw(KRW) + cash_usd*fx`로 합산한다. **브로커는 반드시 KRW 환산값을 채워야** USD가 KRW 임계값에 잘못 비교되지 않는다. 따라서 `account_snapshot`은 KIS와 동일하게:
- `cash_usd` = USD raw, `fx_usdkrw` = 환율, `foreign_eval_krw` = `(cash_usd + Σ qty·eval_price) × fx` **직접 계산**(KIS `overseas_snapshot` 패턴 — 벤더 환산필드 불일치 회피).

### KIS 미러 레퍼런스 (읽기 전용 — 구조 참고)
- `kis_broker.py:383` `overseas_snapshot` — 잔고+환율+포지션, foreign_eval_krw 직접계산.
- `kis_broker.py:279-298` `account_snapshot` overseas 병합 패턴(fetch_failed 마커).
- `kis_broker.py:456` `_detect_market` — DOMESTIC/NAS/NYS/AMS, RoutingError.
- `kis_broker.py:642` `_submit_overseas` — 시장가→지정가 quote 대체, body, normalize.
- `kis_broker.py:685` `_submit_overseas_resv` — 예약(지정가).
- `kis_broker.py:778` `_cancel_overseas`.
- `kis_broker.py:904` `_overseas_order_status` / `:957` `_overseas_pending` / `:996` `pending_orders` 병합.

### 테스트 컨벤션 (기존 LS 테스트와 통일 — `test_ls_futures_resp.py` 참고)
```python
from localapp import ls_broker as lb
def _broker():
    return object.__new__(lb.LsBroker)   # __init__(자격증명 로드) 우회
# 그 후 monkeypatch.setattr(b, "_post", fake, raising=False) 또는 _*_raw 스텁
```
시장판정 테스트는 `monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: "NAS", raising=False)` 식으로 `market_index`를 스텁. 신규 테스트 파일: `local/tests/test_ls_overseas_stock.py`.

### 실행/검증 명령
- 단일 테스트: `cd local && PYTHONUTF8=1 python -m pytest tests/test_ls_overseas_stock.py -v`
- 전체 LS+KIS 회귀: `cd local && PYTHONUTF8=1 python -m pytest tests/ -q`
- KIS byte-identical 확인: `git -C <repo> diff --stat origin/main -- local/localapp/kis_broker.py local/localapp/kis_futures_broker.py` → **빈 출력이어야 함**.

---

## File Structure

- **Modify:** `local/localapp/ls_broker.py` — `LsBroker`에 해외 메서드 추가 + 공개 메서드 분기. (`_LsAuth`·`normalize_ls_order_resp`·`canonical_odno` 무변경.)
- **Create:** `local/tests/test_ls_overseas_stock.py` — 해외 경로 전수 테스트.

각 task는 `ls_broker.py`의 서로 다른 메서드군을 추가/수정하므로 독립적이며, 테스트가 계약이다.

---

### Task E1: 시장 판정 + LS 거래소코드 + 티커 정규화

**Files:**
- Modify: `local/localapp/ls_broker.py` (LsBroker에 헬퍼 추가)
- Test: `local/tests/test_ls_overseas_stock.py` (신규)

- [ ] **Step 1: 실패 테스트 작성**

```python
"""LS 해외주식(미국) 경로 — 시장판정·잔고·시세·주문·취소·조회·예약 전수.
⚠ fixture는 research(overseas-stock-research.md) 기반. 모의 E2E 후 실측 교체."""
from __future__ import annotations
import sys
from pathlib import Path
_LOCAL = Path(__file__).resolve().parent.parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))
from localapp import ls_broker as lb


def _broker():
    b = object.__new__(lb.LsBroker)
    b.account_no = "55500000000"
    b.virtual = True
    return b


def test_detect_market_domestic(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: None, raising=False)
    monkeypatch.setattr(lb.market_index, "_looks_domestic", lambda s: True, raising=False)
    assert b._detect_market("000660") == "DOMESTIC"


def test_detect_market_us(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: "NAS", raising=False)
    assert b._detect_market("AAPL") == "NAS"


def test_detect_market_unknown_us_ticker_raises(monkeypatch):
    """미국 티커 형태인데 인덱스에 없으면 추측 금지 → RoutingError(발주 차단)."""
    import pytest
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: None, raising=False)
    monkeypatch.setattr(lb.market_index, "_looks_domestic", lambda s: False, raising=False)
    with pytest.raises(lb.market_index.RoutingError):
        b._detect_market("XYZ")


def test_ls_excd_mapping():
    b = _broker()
    assert b._ls_excd("NAS") == "82"   # NASDAQ
    assert b._ls_excd("NYS") == "81"   # NYSE
    assert b._ls_excd("AMS") == "81"   # AMEX→NYSE 통합(G23-1)


def test_ls_ticker_bare(monkeypatch):
    """LS IsuNo/keysymbol는 bare 티커(A접두·슬래시 없음)."""
    b = _broker()
    assert b._ls_ticker("AAPL") == "AAPL"
    assert b._ls_ticker("aapl") == "AAPL"
```

- [ ] **Step 2: 실패 확인** — `pytest tests/test_ls_overseas_stock.py -v` → `AttributeError: _detect_market`.

- [ ] **Step 3: 구현** — `LsBroker`에 추가(KIS `_detect_market` kis_broker.py:456 미러):

```python
    # ── 해외(미국) 시장 라우팅 ───────────────────────────────────────────────
    # 거래소코드 — research G23-1: 82=NASDAQ, 81=NYSE+AMEX(AMEX 통합). KIS NAS/NYS/AMS 3분할과 다름.
    _LS_EXCD = {"NAS": "82", "NYS": "81", "AMS": "81"}

    def _detect_market(self, symbol: str) -> str:
        """종목→시장. 'DOMESTIC' 또는 미국 거래소 'NAS'/'NYS'/'AMS'.
        market_index(브로커 무관 권위 소스) 재사용 — KIS _detect_market와 동일.
        미국 티커 형태인데 인덱스에 없으면 추측 않고 RoutingError(발주 차단)."""
        from . import market_index
        exch = market_index.exchange_of(symbol)
        if exch:
            return exch
        if market_index._looks_domestic(symbol):
            return "DOMESTIC"
        s = symbol.strip().upper()
        if s.isalpha() and 1 <= len(s) <= 5:
            raise market_index.RoutingError(
                f"미국 티커로 보이나 마스터 인덱스에 없음: {symbol} — 인덱스 갱신 필요. 발주 보류.")
        return "DOMESTIC"

    def _ls_excd(self, market: str) -> str:
        """미국 거래소(NAS/NYS/AMS) → LS 시장코드(82/81)."""
        return self._LS_EXCD.get(market, "82")

    def _ls_ticker(self, symbol: str) -> str:
        """LS 해외 IsuNo/keysymbol용 bare 티커(대문자). 클래스주(BRK-B)는 OG-E1(모의 실측)."""
        return symbol.strip().upper()
```

> ⚠ 모듈 상단에 `from . import market_index`가 이미 있으면 재사용, 없으면 메서드 내 지역 import(KIS 패턴). `_detect_market`은 `lb.market_index`로 monkeypatch되도록 **메서드 내 `from . import market_index`** 사용. 테스트가 `lb.market_index`를 패치하므로, 모듈 상단에 `from . import market_index`를 한 번 두고 메서드에서 `market_index.X`로 참조하는 편이 패치가 잘 먹는다. **모듈 상단 import 권장.**

- [ ] **Step 4: 통과 확인** — `pytest tests/test_ls_overseas_stock.py -v` → 5 PASS.
- [ ] **Step 5: 커밋** — `git add local/localapp/ls_broker.py local/tests/test_ls_overseas_stock.py && git commit -m "feat(ls): Phase E1 해외 시장판정·거래소코드·티커 정규화"`

---

### Task E2: 해외 잔고 + account_snapshot 병합 (COSOQ00201)

**Files:**
- Modify: `local/localapp/ls_broker.py` (`overseas_snapshot` 추가, `account_snapshot` 수정)
- Test: `local/tests/test_ls_overseas_stock.py`

- [ ] **Step 1: 실패 테스트 추가**

```python
def _us_index_stub(monkeypatch):
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: "NAS", raising=False)


def test_overseas_snapshot_fields(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    monkeypatch.setattr(b, "_overseas_balance_raw", lambda: {
        "COSOQ00201OutBlock3": [{"CrcyCode": "USD", "FcurrDps": "10000.50", "BaseXchrat": "1350.0"}],
        "COSOQ00201OutBlock4": [
            {"ShtnIsuNo": "AAPL", "AstkBalQty": "10", "FcstckUprc": "150.0",
             "OvrsScrtsCurpri": "200.0", "FcurrMktCode": "82"}]}, raising=False)
    ov = b.overseas_snapshot()
    assert ov["usd_cash"] == 10000.50
    assert ov["fx_usdkrw"] == 1350.0
    # foreign_eval_krw = (10000.50 + 10*200.0) * 1350.0  직접계산
    assert ov["foreign_eval_krw"] == (10000.50 + 10 * 200.0) * 1350.0
    p = ov["positions"][0]
    assert p["symbol"] == "AAPL" and p["qty"] == 10 and p["currency"] == "USD"
    assert p["avg_price"] == 150.0 and p["eval_price"] == 200.0


def test_account_snapshot_merges_overseas(monkeypatch):
    b = _broker()
    monkeypatch.setattr(b, "_balance_raw", lambda: {
        "t0424OutBlock": {"sunamt": "5000000", "sunamt1": "5000000"},
        "t0424OutBlock1": []}, raising=False)
    monkeypatch.setattr(b, "overseas_snapshot", lambda: {
        "usd_cash": 1000.0, "fx_usdkrw": 1300.0, "foreign_eval_krw": 2600000.0,
        "positions": [{"symbol": "AAPL", "qty": 5, "currency": "USD", "market": "NAS",
                       "avg_price": 100.0, "eval_price": 120.0}]}, raising=False)
    snap = b.account_snapshot(overseas=True)
    bal = snap["balance"]
    assert bal["total_eval"] == 5000000        # 국내 KRW 유지
    assert bal["cash_usd"] == 1000.0
    assert bal["fx_usdkrw"] == 1300.0
    assert bal["foreign_eval_krw"] == 2600000.0
    assert any(p["symbol"] == "AAPL" for p in snap["positions"])


def test_account_snapshot_overseas_failure_marks_fetch_failed(monkeypatch):
    """해외 조회 실패 → 국내는 유지, balance['fetch_failed']=['overseas'] (0 위장 금지)."""
    b = _broker()
    monkeypatch.setattr(b, "_balance_raw", lambda: {
        "t0424OutBlock": {"sunamt": "5000000", "sunamt1": "5000000"}, "t0424OutBlock1": []}, raising=False)
    monkeypatch.setattr(b, "overseas_snapshot",
                        lambda: (_ for _ in ()).throw(RuntimeError("5xx")), raising=False)
    snap = b.account_snapshot(overseas=True)
    assert snap["balance"]["total_eval"] == 5000000
    assert snap["balance"]["fetch_failed"] == ["overseas"]


def test_account_snapshot_overseas_false_skips(monkeypatch):
    """overseas=False면 해외 조회 안 함(국내만)."""
    b = _broker()
    monkeypatch.setattr(b, "_balance_raw", lambda: {
        "t0424OutBlock": {"sunamt": "5000000", "sunamt1": "5000000"}, "t0424OutBlock1": []}, raising=False)
    called = {"n": 0}
    monkeypatch.setattr(b, "overseas_snapshot",
                        lambda: called.__setitem__("n", called["n"] + 1) or {}, raising=False)
    b.account_snapshot(overseas=False)
    assert called["n"] == 0
```

- [ ] **Step 2: 실패 확인.**

- [ ] **Step 3: 구현** — `overseas_snapshot` 추가(KIS kis_broker.py:383 미러, 필드명만 LS로) + `account_snapshot` 수정.

`overseas_snapshot`:
```python
    def _overseas_balance_raw(self) -> dict:
        """COSOQ00201 해외 종합잔고평가 — 통화별(OB3)·종목별(OB4). BaseDt=당일."""
        from datetime import datetime
        return self._post("/overseas-stock/accno", "COSOQ00201",
                          {"COSOQ00201InBlock1": {"BaseDt": datetime.now().strftime("%Y%m%d"),
                                                  "CrcyCode": "USD", "AstkBalTpCode": "00"}})

    def overseas_snapshot(self) -> dict:
        """미국 USD 예수금+환율+보유종목. foreign_eval_krw는 직접계산(벤더 환산필드 불일치 회피·KIS 동일).
        ⚠ 필드명(FcurrDps/BaseXchrat/ShtnIsuNo/AstkBalQty/FcstckUprc/OvrsScrtsCurpri) research 기반 — 모의 실측 확정."""
        from . import market_index
        body = self._overseas_balance_raw()
        usd_cash = fx = 0.0
        for row in body.get("COSOQ00201OutBlock3") or []:
            if str(row.get("CrcyCode") or "") == "USD":
                usd_cash = float(row.get("FcurrDps") or 0)
                fx = float(row.get("BaseXchrat") or 0)
                break
        positions = []
        for it in body.get("COSOQ00201OutBlock4") or []:
            qty = int(float(it.get("AstkBalQty") or 0))
            if qty <= 0:
                continue
            sym = str(it.get("ShtnIsuNo") or "").strip().upper()
            positions.append({
                "symbol": sym, "name": str(it.get("IsuKorNm") or it.get("IsuNm") or ""),
                "qty": qty,
                "avg_price": float(it.get("FcstckUprc") or 0),
                "eval_price": float(it.get("OvrsScrtsCurpri") or 0),
                "market": market_index.exchange_of(sym) or "US", "currency": "USD",
            })
        positions_eval_usd = sum(p["qty"] * p["eval_price"] for p in positions)
        foreign_eval_krw = (usd_cash + positions_eval_usd) * fx if fx > 0 else 0.0
        return {"usd_cash": usd_cash, "fx_usdkrw": fx,
                "foreign_eval_krw": foreign_eval_krw, "positions": positions}
```

`account_snapshot` 수정 — 기존 국내 로직 끝(현재 `return {"balance": {...국내...}, "positions": positions}`)을 KIS 병합 패턴으로(kis_broker.py:279-298):
```python
        balance = {
            "cash": cash, "total_eval": total_eval,
            "cash_usd": 0.0, "fx_usdkrw": 0.0, "foreign_eval_krw": 0.0,
        }
        if overseas:
            try:
                ov = self.overseas_snapshot()
                balance["cash_usd"] = ov["usd_cash"]
                balance["fx_usdkrw"] = ov["fx_usdkrw"]
                balance["foreign_eval_krw"] = ov["foreign_eval_krw"]
                positions.extend(ov["positions"])
            except Exception as e:
                log.warning("LS 해외 잔고 조회 실패 — 국내만 반영: %s", e)
                balance["fetch_failed"] = ["overseas"]
        return {"balance": balance, "positions": positions}
```
> ⚠ 기존 docstring의 "LS는 국내 only" 문구를 갱신("overseas=True면 미국 잔고 병합"). 국내 조회 실패 시 기존 `fetch_failed=["domestic"]` 분기는 그대로 유지.

- [ ] **Step 4: 통과 확인.** (E1 테스트도 여전히 PASS)
- [ ] **Step 5: 커밋** — `feat(ls): Phase E2 해외 잔고 COSOQ00201 + account_snapshot 병합(KRW 환산)`

---

### Task E3: 해외 시세 price/today_open (g3101)

**Files:** Modify `ls_broker.py` (`_price_overseas` 추가, `price`/`today_open` 분기), Test 추가.

- [ ] **Step 1: 실패 테스트 추가**

```python
def test_price_overseas(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["keysymbol"] = body["g3101InBlock"]["keysymbol"]
        return {"g3101OutBlock": {"price": "201.55", "open": "199.00"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    assert b.price("TSLA") == 201.55
    assert captured["keysymbol"] == "82TSLA"     # exchcd(82)+티커


def test_today_open_overseas(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"g3101OutBlock": {"open": "199.00"}}, raising=False)
    assert b.today_open("TSLA") == 199.00


def test_price_domestic_unchanged(monkeypatch):
    """국내는 기존 t1102 경로 유지(분기 회귀 없음)."""
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: None, raising=False)
    monkeypatch.setattr(lb.market_index, "_looks_domestic", lambda s: True, raising=False)
    monkeypatch.setattr(b, "_price_raw", lambda s: {"t1102OutBlock": {"price": "70000", "open": "69500"}}, raising=False)
    assert b.price("000660") == 70000.0
    assert b.today_open("000660") == 69500.0


def test_today_open_overseas_zero_fallback(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"g3101OutBlock": {"open": ""}}, raising=False)
    assert b.today_open("TSLA") == 0.0
```

- [ ] **Step 2: 실패 확인.**

- [ ] **Step 3: 구현** — `_price_overseas` 추가 + `price`/`today_open`을 `_detect_market` 분기로 변경.

```python
    def _quote_overseas_raw(self, symbol: str, market: str) -> dict:
        """g3101 해외 현재가 — keysymbol = exchcd(82/81) + bare 티커."""
        excd = self._ls_excd(market)
        return self._post("/overseas-stock/market-data", "g3101",
                          {"g3101InBlock": {"delaygb": "R", "keysymbol": f"{excd}{self._ls_ticker(symbol)}",
                                            "exchcd": excd, "symbol": self._ls_ticker(symbol)}})

    def _price_overseas(self, symbol: str, market: str) -> float:
        out = self._quote_overseas_raw(symbol, market).get("g3101OutBlock") or {}
        return float(out.get("price") or 0)
```

`price`/`today_open` 변경 — 기존 국내 본문을 `_detect_market` 분기로 감싼다:
```python
    def price(self, symbol: str) -> float:
        market = self._detect_market(symbol)
        if market == "DOMESTIC":
            out = self._price_raw(symbol).get("t1102OutBlock") or {}
            return float(out.get("price") or 0)
        return self._price_overseas(symbol, market)

    def today_open(self, symbol: str) -> float:
        try:
            market = self._detect_market(symbol)
            if market == "DOMESTIC":
                out = self._price_raw(symbol).get("t1102OutBlock") or {}
                v = out.get("open")
            else:
                out = self._quote_overseas_raw(symbol, market).get("g3101OutBlock") or {}
                v = out.get("open")
            return float(v) if v not in (None, "", 0, "0") else 0.0
        except Exception:
            return 0.0
```
> 기존 `price`/`today_open` 본문(국내 t1102)을 위 분기형으로 교체. `_price_raw`(국내)는 그대로 유지.

- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** — `feat(ls): Phase E3 해외 시세 g3101 + price/today_open 분기`

---

### Task E4: 해외 주문 buy/sell/limit (COSAT00301)

**Files:** Modify `ls_broker.py` (`_submit_overseas` 추가, `buy/sell/buy_limit/sell_limit` 분기), Test 추가.

- [ ] **Step 1: 실패 테스트 추가**

```python
def test_buy_overseas_market_quotes_to_limit(monkeypatch):
    """해외 시장가는 g3101 현재가로 지정가 대체(KIS 패턴·OG3 안전)."""
    b = _broker()
    _us_index_stub(monkeypatch)
    monkeypatch.setattr(b, "_price_overseas", lambda s, m: 201.55, raising=False)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["body"] = body["COSAT00301InBlock1"]
        return {"COSAT00301OutBlock2": {"OrdNo": "12345"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    r = b.buy("AAPL", 3)
    assert r["success"] is True and r["order_no"] == "12345"
    bd = captured["body"]
    assert bd["OrdPtnCode"] == "02"        # 매수
    assert bd["OrdMktCode"] == "82"        # NASDAQ
    assert bd["IsuNo"] == "AAPL"
    assert bd["OrdprcPtnCode"] == "00"     # 지정가 강제
    assert bd["OvrsOrdPrc"] == 201.55      # 현재가로 대체(float)


def test_sell_overseas_limit_float_price(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["body"] = body["COSAT00301InBlock1"]
        return {"COSAT00301OutBlock2": {"OrdNo": "9"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    b.sell_limit("AAPL", 2, 198.25)
    bd = captured["body"]
    assert bd["OrdPtnCode"] == "01"        # 매도
    assert bd["OvrsOrdPrc"] == 198.25      # float 유지(int 절삭 금지)
    assert bd["OrdprcPtnCode"] == "00"


def test_buy_overseas_quote_fail_raises(monkeypatch):
    """시장가인데 현재가 0 → 추측발주 금지, RuntimeError."""
    import pytest
    b = _broker()
    _us_index_stub(monkeypatch)
    monkeypatch.setattr(b, "_price_overseas", lambda s, m: 0.0, raising=False)
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"COSAT00301OutBlock2": {"OrdNo": "1"}}, raising=False)
    with pytest.raises(RuntimeError):
        b.buy("AAPL", 1)


def test_buy_domestic_unchanged(monkeypatch):
    """국내 매수는 기존 CSPAT00601 경로 유지."""
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: None, raising=False)
    monkeypatch.setattr(lb.market_index, "_looks_domestic", lambda s: True, raising=False)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["tr"] = tr; captured["body"] = body
        return {"CSPAT00601OutBlock2": {"OrdNo": "777"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    r = b.buy("000660", 1)
    assert r["order_no"] == "777"
    assert captured["tr"] == "CSPAT00601"
    assert captured["body"]["CSPAT00601InBlock1"]["IsuNo"] == "A000660"


def test_order_reject_overseas_no_ordno(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    monkeypatch.setattr(b, "_price_overseas", lambda s, m: 200.0, raising=False)
    monkeypatch.setattr(b, "_post", lambda *a, **k: {"rsp_cd": "99", "rsp_msg": "증거금부족"}, raising=False)
    r = b.buy("AAPL", 1)
    assert r["success"] is False and r["order_no"] == ""
```

- [ ] **Step 2: 실패 확인.**

- [ ] **Step 3: 구현** — `_submit_overseas` 추가(KIS kis_broker.py:642 미러) + 공개 메서드 분기.

```python
    def _submit_overseas(self, symbol, qty, side, ord_ptn_code, unit_price, market):
        """COSAT00301 미국 주문. OrdPtnCode 02매수/01매도. 해외는 지정가(00) 강제 —
        시장가 의도(unit_price<=0)는 g3101 현재가로 대체(OG3 안전·KIS 패턴). 가격 float."""
        if unit_price <= 0:
            quoted = self._price_overseas(symbol, market)
            if quoted <= 0:
                raise RuntimeError(f"해외 {market} {symbol} 현재가 조회 실패({quoted}) — 지정가 발주 불가. 주문 보류.")
            unit_price = quoted
        ord_ptn = "02" if side == "buy" else "01"
        resp = self._post("/overseas-stock/order", "COSAT00301",
                          {"COSAT00301InBlock1": {
                              "OrdPtnCode": ord_ptn, "OrgOrdNo": 0,
                              "OrdMktCode": self._ls_excd(market), "IsuNo": self._ls_ticker(symbol),
                              "OrdQty": qty, "OvrsOrdPrc": float(unit_price),
                              "OrdprcPtnCode": "00"}}, is_order=True)
        return normalize_ls_order_resp(resp, ordno_field="OrdNo")
```

공개 메서드 분기 — 기존 `buy/sell/buy_limit/sell_limit`을 `_detect_market`로 감싼다(국내는 기존 `_submit`):
```python
    def buy(self, symbol, qty):
        m = self._detect_market(symbol)
        return self._submit(symbol, qty, "buy", "03", 0.0) if m == "DOMESTIC" \
            else self._submit_overseas(symbol, qty, "buy", "00", 0.0, m)

    def sell(self, symbol, qty):
        m = self._detect_market(symbol)
        return self._submit(symbol, qty, "sell", "03", 0.0) if m == "DOMESTIC" \
            else self._submit_overseas(symbol, qty, "sell", "00", 0.0, m)

    def buy_limit(self, symbol, qty, limit_price):
        m = self._detect_market(symbol)
        return self._submit(symbol, qty, "buy", "00", float(limit_price)) if m == "DOMESTIC" \
            else self._submit_overseas(symbol, qty, "buy", "00", float(limit_price), m)

    def sell_limit(self, symbol, qty, limit_price):
        m = self._detect_market(symbol)
        return self._submit(symbol, qty, "sell", "00", float(limit_price)) if m == "DOMESTIC" \
            else self._submit_overseas(symbol, qty, "sell", "00", float(limit_price), m)
```
> ⚠ 국내 `_submit`(CSPAT00601)·`buy`의 기존 시그니처/본문은 분기 안으로만 이동, 로직 무변경. `_submit_overseas`의 `ord_ptn_code` 인자는 항상 "00"(지정가)이라 시그니처 단순화 가능하나 KIS 대칭 위해 유지(미사용이면 제거 — 4원칙#2). **호출에서 안 쓰면 인자 빼라.**

- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** — `feat(ls): Phase E4 해외 주문 COSAT00301 + buy/sell 분기`

---

### Task E5: 해외 취소 cancel (COSAT00301 OrdPtnCode=08)

**Files:** Modify `ls_broker.py` (`_cancel_overseas` 추가, `cancel` 분기), Test 추가.

- [ ] **Step 1: 실패 테스트 추가**

```python
def test_cancel_overseas(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "is_us", lambda s: True, raising=False)
    _us_index_stub(monkeypatch)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["body"] = body["COSAT00301InBlock1"]
        return {"COSAT00301OutBlock2": {"OrdNo": "55"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    r = b.cancel("12345", "AAPL", 3)
    assert r["success"] is True
    assert "order_no" not in r                     # cancel 계약: success/message/msg_cd만
    assert captured["body"]["OrdPtnCode"] == "08"  # 취소
    assert captured["body"]["OrgOrdNo"] == 12345
    assert captured["body"]["IsuNo"] == "AAPL"


def test_cancel_domestic_unchanged(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "is_us", lambda s: False, raising=False)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["tr"] = tr
        return {"CSPAT00801OutBlock2": {"OrdNo": "9"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    b.cancel("100", "000660", 1)
    assert captured["tr"] == "CSPAT00801"          # 국내 취소 TR 유지
```

- [ ] **Step 2: 실패 확인.**

- [ ] **Step 3: 구현** — `_cancel_overseas` 추가 + `cancel`을 `market_index.is_us` 분기로(KIS kis_broker.py:757 패턴).

```python
    def _cancel_overseas(self, order_no, symbol, qty):
        """COSAT00301 OrdPtnCode='08' 취소(OrgOrdNo+IsuNo+OrdQty)."""
        from . import market_index
        market = market_index.exchange_of(symbol) or "NAS"
        resp = self._post("/overseas-stock/order", "COSAT00301",
                          {"COSAT00301InBlock1": {
                              "OrdPtnCode": "08",
                              "OrgOrdNo": int(order_no) if str(order_no).isdigit() else order_no,
                              "OrdMktCode": self._ls_excd(market), "IsuNo": self._ls_ticker(symbol),
                              "OrdQty": qty, "OvrsOrdPrc": 0, "OrdprcPtnCode": "00"}}, is_order=True)
        r = normalize_ls_order_resp(resp, ordno_field="OrdNo")
        return {"success": r["success"], "message": r["message"], "msg_cd": r["msg_cd"]}
```

`cancel` 분기 — 기존 국내 본문 앞에 추가:
```python
    def cancel(self, order_no, symbol, qty):
        from . import market_index
        if symbol and market_index.is_us(symbol):
            return self._cancel_overseas(order_no, symbol, qty)
        # ...기존 국내 CSPAT00801 본문 그대로...
```

- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** — `feat(ls): Phase E5 해외 취소 COSAT00301(08) + cancel 분기`

---

### Task E6: 해외 체결조회·미체결 order_status/pending (COSAQ00102)

**Files:** Modify `ls_broker.py` (`_overseas_order_status`/`_overseas_pending` 추가, `order_status`/`pending_orders` 분기), Test 추가.

- [ ] **Step 1: 실패 테스트 추가**

```python
def _ccld_rows(rows):
    return {"COSAQ00102OutBlock3": rows}


def test_overseas_order_status_filled(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "is_us", lambda s: True, raising=False)
    monkeypatch.setattr(b, "_overseas_ccld_raw", lambda exec_yn: _ccld_rows([
        {"OrdNo": "12345", "OrgOrdNo": "0", "ShtnIsuNo": "AAPL", "OrdQty": "3",
         "ExecQty": "3", "UnercQty": "0", "OvrsExecPrc": "201.55", "OrdTrxPtnNm": "체결"}]), raising=False)
    st = b.order_status("12345", symbol="AAPL")
    assert st["status"] == "filled" and st["filled_qty"] == 3 and st["fill_price"] == 201.55


def test_overseas_order_status_cancelled(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "is_us", lambda s: True, raising=False)
    monkeypatch.setattr(b, "_overseas_ccld_raw", lambda exec_yn: _ccld_rows([
        {"OrdNo": "5", "OrgOrdNo": "0", "ShtnIsuNo": "AAPL", "OrdQty": "1",
         "ExecQty": "0", "UnercQty": "0", "OrdTrxPtnNm": "취소완료"}]), raising=False)
    assert b.order_status("5", symbol="AAPL")["status"] == "cancelled"


def test_overseas_order_status_partial(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "is_us", lambda s: True, raising=False)
    monkeypatch.setattr(b, "_overseas_ccld_raw", lambda exec_yn: _ccld_rows([
        {"OrdNo": "7", "OrgOrdNo": "0", "ShtnIsuNo": "AAPL", "OrdQty": "10",
         "ExecQty": "4", "UnercQty": "6", "OvrsExecPrc": "200.0", "OrdTrxPtnNm": "체결"}]), raising=False)
    assert b.order_status("7", symbol="AAPL")["status"] == "partial"


def test_overseas_order_status_unknown(monkeypatch):
    b = _broker()
    monkeypatch.setattr(lb.market_index, "is_us", lambda s: True, raising=False)
    monkeypatch.setattr(b, "_overseas_ccld_raw", lambda exec_yn: _ccld_rows([]), raising=False)
    assert b.order_status("999", symbol="AAPL")["status"] == "unknown"


def test_overseas_pending_merges(monkeypatch):
    """pending_orders = 국내 + 해외 병합."""
    b = _broker()
    monkeypatch.setattr(b, "_pending_raw", lambda: {"t0425OutBlock1": []}, raising=False)
    monkeypatch.setattr(b, "_overseas_ccld_raw", lambda exec_yn: _ccld_rows([
        {"OrdNo": "10", "OrgOrdNo": "0", "ShtnIsuNo": "AAPL", "OrdQty": "5",
         "ExecQty": "0", "UnercQty": "5", "OvrsOrdPrc": "190.0", "OrdMktCode": "82"}]), raising=False)
    pend = b.pending_orders()
    assert len(pend) == 1 and pend[0]["order_no"] == "10" and pend[0]["currency"] == "USD"
```

- [ ] **Step 2: 실패 확인.**

- [ ] **Step 3: 구현** — COSAQ00102 기반(KIS overseas_order_status/pending 미러, 단 KIS의 예약 fuzzy-match는 **포팅 안 함** — LS 번호공간 불일치 미확인·over-engineering 회피. 예약 status는 E7에서 COSAQ01400로 별도).

```python
    def _overseas_ccld_raw(self, exec_yn: str) -> dict:
        """COSAQ00102 계좌주문체결내역 — ExecYn 0전체/1체결/2미체결. OrdDt=당일."""
        from datetime import datetime
        return self._post("/overseas-stock/accno", "COSAQ00102",
                          {"COSAQ00102InBlock1": {"OrdDt": datetime.now().strftime("%Y%m%d"),
                                                  "ExecYn": exec_yn, "SrtOrdNo": "999999999", "IsuNo": ""}})

    def _overseas_order_status(self, order_no, symbol):
        """COSAQ00102(ExecYn='0' 전체) OrdNo 매칭 → filled/partial/cancelled/submitted.
        ⚠ G-E2: OrdTrxPtnNm 부분체결/거부 문자열 실측 전 — '취소' 포함 시 cancelled, 그 외 Exec/Unerc로 판정."""
        try:
            rows = self._overseas_ccld_raw("0").get("COSAQ00102OutBlock3") or []
        except Exception as e:
            log.warning("LS 해외 order_status 실패 [%s]: %s", order_no, e)
            return {"order_no": order_no, "status": "unknown", "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}
        for row in rows:
            if canonical_odno(row.get("OrdNo")) != canonical_odno(order_no):
                continue
            exec_q = int(float(row.get("ExecQty") or 0))
            unerc = int(float(row.get("UnercQty") or 0))
            nm = str(row.get("OrdTrxPtnNm") or "")
            if "취소" in nm:
                st = "cancelled"
            elif unerc == 0 and exec_q > 0:
                st = "filled"
            elif exec_q > 0:
                st = "partial"
            else:
                st = "submitted"
            return {"order_no": order_no, "status": st, "filled_qty": exec_q, "remain_qty": unerc,
                    "fill_price": float(row.get("OvrsExecPrc") or 0)}
        return {"order_no": order_no, "status": "unknown", "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}

    def _overseas_pending(self) -> list[dict]:
        try:
            rows = self._overseas_ccld_raw("2").get("COSAQ00102OutBlock3") or []
        except Exception as e:
            log.warning("LS 해외 pending 실패: %s", e)
            return []
        out = []
        for row in rows:
            if str(row.get("OrgOrdNo") or "0") not in ("0", "", "000000000"):  # 정정/취소행 제외
                continue
            unerc = int(float(row.get("UnercQty") or 0))
            if unerc <= 0:
                continue
            out.append({"order_no": str(row.get("OrdNo") or ""), "symbol": str(row.get("ShtnIsuNo") or "").strip().upper(),
                        "name": "", "side": "buy", "qty": int(float(row.get("OrdQty") or 0)),
                        "filled_qty": int(float(row.get("ExecQty") or 0)), "remain_qty": unerc,
                        "limit_price": float(row.get("OvrsOrdPrc") or 0), "ord_branch": "",
                        "submitted_at": str(row.get("OrdTime") or ""), "market": "US", "currency": "USD"})
        return out
```
> ⚠ `side`: COSAQ00102에 매수/매도 구분 필드(예: `BnsTpCode`)가 있으면 매핑(2→buy/1→sell). 필드명 미확정 시 우선 "buy" 두고 G-E3로 표기 — **research 재확인 후 정확 필드 사용.**

`order_status`/`pending_orders` 분기:
```python
    def order_status(self, order_no, symbol=None, hint=None):
        from . import market_index
        if symbol and market_index.is_us(symbol):
            return self._overseas_order_status(order_no, symbol)
        # ...기존 국내 t0425 본문 그대로...

    def pending_orders(self):
        out = []
        try:
            # ...기존 국내 t0425 본문으로 out 채움...
        except Exception as e:
            log.warning("LS 국내 pending 실패: %s", e)
        try:
            out.extend(self._overseas_pending())
        except Exception as e:
            log.warning("LS 해외 pending 실패: %s", e)
        return out
```

- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** — `feat(ls): Phase E6 해외 체결조회 COSAQ00102 + order_status/pending 분기`

---

### Task E7: 해외 예약주문 buy_resv_limit/sell_resv_limit (COSAT00400)

**Files:** Modify `ls_broker.py` (`_submit_overseas_resv` 추가, `buy_resv_limit`/`sell_resv_limit` 분기), Test 추가.

> 배경: 현재 두 메서드는 `NotImplementedError("해외주식 단계(후속 plan)")`. 이제 해외분이면 LS 예약 TR로 구현(KIS는 미국 예약을 pre-market catch-up에 사용 — kis_broker.py:732). 국내분은 여전히 미지원(LS 국내주식 예약 TR 없음) → NotImplementedError 유지.

- [ ] **Step 1: 실패 테스트 추가**

```python
def test_resv_buy_overseas(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    captured = {}
    def fake_post(path, tr, body, **k):
        captured["body"] = body["COSAT00400InBlock1"]
        return {"COSAT00400OutBlock2": {"RsvOrdNo": "448"}}
    monkeypatch.setattr(b, "_post", fake_post, raising=False)
    r = b.buy_resv_limit("AAPL", 2, 195.50)
    assert r["success"] is True and r["order_no"] == "448"   # RsvOrdNo가 order_no
    bd = captured["body"]
    assert bd["BnsTpCode"] == "2"           # 매수
    assert bd["CntryCode"] == "US"
    assert bd["IsuNo"] == "AAPL"
    assert bd["OvrsOrdPrc"] == 195.50
    assert "AcntNo" in bd and "Pwd" in bd   # 예약은 AcntNo/Pwd body 필수(G23-5)
    assert bd["RsvOrdSrtDt"] and bd["RsvOrdEndDt"]


def test_resv_sell_overseas_bnstp(monkeypatch):
    b = _broker()
    _us_index_stub(monkeypatch)
    captured = {}
    monkeypatch.setattr(b, "_post", lambda p, t, body, **k: captured.update(body["COSAT00400InBlock1"])
                        or {"COSAT00400OutBlock2": {"RsvOrdNo": "9"}}, raising=False)
    b.sell_resv_limit("AAPL", 1, 210.0)
    assert captured["BnsTpCode"] == "1"     # 매도


def test_resv_domestic_still_not_implemented(monkeypatch):
    import pytest
    b = _broker()
    monkeypatch.setattr(lb.market_index, "exchange_of", lambda s: None, raising=False)
    monkeypatch.setattr(lb.market_index, "_looks_domestic", lambda s: True, raising=False)
    with pytest.raises(NotImplementedError):
        b.buy_resv_limit("000660", 1, 70000)
```

- [ ] **Step 2: 실패 확인.**

- [ ] **Step 3: 구현** — `_submit_overseas_resv` 추가 + 두 메서드 분기.

```python
    def _submit_overseas_resv(self, symbol, qty, side, unit_price, market):
        """COSAT00400 미국 예약주문(등록). 지정가(00). AcntNo/Pwd body 필수(G23-5).
        실행일창 = 오늘~오늘(당일 개장 단일가). ⚠ enum/필드 research 기반 — 모의 실측(OG4)."""
        from datetime import datetime
        today = datetime.now().strftime("%Y%m%d")
        resp = self._post("/overseas-stock/order", "COSAT00400",
                          {"COSAT00400InBlock1": {
                              "TrxTpCode": "1",                 # 등록 ⚠ enum OG4
                              "CntryCode": "US",
                              "BnsTpCode": "2" if side == "buy" else "1",
                              "AcntNo": self.account_no, "Pwd": "",
                              "FcurrMktCode": self._ls_excd(market), "IsuNo": self._ls_ticker(symbol),
                              "OrdQty": qty, "OvrsOrdPrc": float(unit_price), "OrdprcPtnCode": "00",
                              "RsvOrdSrtDt": today, "RsvOrdEndDt": today}}, is_order=True)
        return normalize_ls_order_resp(resp, ordno_field="RsvOrdNo")

    def buy_resv_limit(self, symbol, qty, limit_price):
        from . import market_index
        m = self._detect_market(symbol)
        if m == "DOMESTIC":
            raise NotImplementedError("LS 국내주식 예약주문 미지원")
        return self._submit_overseas_resv(symbol, qty, "buy", float(limit_price), m)

    def sell_resv_limit(self, symbol, qty, limit_price):
        m = self._detect_market(symbol)
        if m == "DOMESTIC":
            raise NotImplementedError("LS 국내주식 예약주문 미지원")
        return self._submit_overseas_resv(symbol, qty, "sell", float(limit_price), m)
```
> ⚠ 기존 `buy_resv_limit`/`sell_resv_limit`(전부 NotImplementedError) 본문을 위 분기형으로 교체.
> ⚠ 예약 status 추적(COSAQ01400 by RsvOrdNo): KIS는 예약 번호공간 불일치로 fuzzy-match가 필요했으나, **LS 동일 여부 미확정**. 현 단계는 예약 등록까지만 구현하고, status는 정규 COSAQ00102가 잡으면 그대로·아니면 모의 E2E에서 COSAQ01400 배선 결정(G-E4). 추측 포팅 금지(4원칙#2).

- [ ] **Step 4: 통과 확인.**
- [ ] **Step 5: 커밋** — `feat(ls): Phase E7 해외 예약주문 COSAT00400 + resv 분기`

---

### Task E8: 통합 검증 + KIS byte-identical 회귀 게이트

**Files:** 없음(검증만). 필요 시 docstring/주석 정리.

- [ ] **Step 1: 전체 테스트** — `cd local && PYTHONUTF8=1 python -m pytest tests/ -q` → 전부 PASS(해외 신규 + 국내/선물 회귀).
- [ ] **Step 2: KIS byte-identical** — `git -C <repo> diff --stat origin/main -- local/localapp/kis_broker.py local/localapp/kis_futures_broker.py` → **빈 출력**. (Phase E는 ls_broker.py만 수정하므로 자동 보장 — 확인.)
- [ ] **Step 3: lint** — `cd local && ruff check localapp/ls_broker.py tests/test_ls_overseas_stock.py` → 신규 위반 0.
- [ ] **Step 4: 메서드 표면 점검(4원칙#2)** — `_submit_overseas`의 `ord_ptn_code` 등 미사용 인자 제거 확인. KIS가 안 하는 표면(미사용 옵션·추측 분기) 없는지.
- [ ] **Step 5: 커밋(있으면)** — `chore(ls): Phase E8 통합 검증·정리`

---

## Gaps (모의 E2E에서 실측 확정 — research OG + 본 plan G-E)
- **OG3 / 시장가**: LS 해외 시장가(03) 모의지원 여부 → 확인 시 quote→limit 단순화 가능.
- **OG4 / 예약 enum**: COSAT00400 `TrxTpCode`·필수필드·Pwd 빈값 허용 여부.
- **G-E2 / 체결 status 문자열**: `OrdTrxPtnNm` 부분체결·거부 전체집합.
- **G-E3 / pending side 필드**: COSAQ00102 매수/매도 구분 필드명.
- **G-E4 / 예약 status**: 예약 RsvOrdNo→체결 추적 경로(COSAQ01400 fuzzy 필요 여부).
- **OG-E1 / 클래스주**: BRK-B 등 LS bare 티커 형식.
- **OG6 / 해외잔고 무계좌 응답 (킬스위치 안전 — E2 리뷰 발견)**: 해외계좌 없는 LS 계좌의 COSOQ00201이 HTTP-200 빈응답(→`overseas_snapshot` 0 반환·`fetch_failed` 미설정·킬스위치 국내 equity 정상 평가)인지 HTTP-에러(→예외 전파·`fetch_failed=["overseas"]`·도메스틱 전용 LS 사용자 킬스위치 **매 사이클 보류**)인지 미확정. KIS는 present-balance가 200-빈응답이라 이 문제 없음 — LS는 단일 TR이라 OG6 의존. **E2E 실측 후** HTTP-에러로 판명되면 '계좌없음' 에러코드만 0으로 매핑하는 가드 추가(일시적 실패는 계속 `fetch_failed` 유지). **degrade-to-zeros 일괄 적용 금지**(미국 보유 사용자 일시 실패를 0으로 읽어 −98% 거짓 청산 재발).
- 미해결은 안전쪽(resolve 실패→발주 보류, fetch 실패→fetch_failed 마커). 오발주/오청산 0.

## 자기검토 (writing-plans)
- **스펙 커버리지**: account_snapshot(E2)·price/today_open(E3)·buy/sell/limit(E4)·cancel(E5)·order_status/pending(E6)·resv(E7) — Broker Protocol 12메서드 전부 해외 분기 커버. ✓
- **타입 일관성**: `_detect_market`("DOMESTIC"/"NAS"/"NYS"/"AMS")·`_ls_excd`("82"/"81")·`_ls_ticker`(bare)·`normalize_ls_order_resp`(OrdNo/RsvOrdNo) 전 task 일관. ✓
- **플레이스홀더 없음**: 모든 step에 실제 테스트·구현 코드. ✓
- **byte-identical**: ls_broker.py·테스트만 수정 → KIS/공유 파일 무변경. ✓

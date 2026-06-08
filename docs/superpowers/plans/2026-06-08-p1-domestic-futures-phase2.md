# P1 — 국내선물 브로커 완성(phase2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** 국내선물 `KisFuturesBroker`의 phase2 메서드(시장가 매수/매도·정정취소·체결조회/미체결)를 스펙대로 구현 — Broker 프로토콜 호환(SimBroker와 동일 반환형).

**Architecture:** 기존 지정가(`build_futures_order_body`)에 `order_type`(limit/market) 추가, 취소 바디·체결조회 파서를 순수함수로 추가, 브로커 메서드(`buy`/`sell`/`cancel`/`order_status`/`pending_orders`)의 `NotImplementedError`를 실 구현으로 교체. 시세 raw·라우팅은 기존대로. **라이브 체결검증(A01606 라운드트립)은 다음 국장으로 위임** — P1은 코드+단위검증(순수함수)까지.

**Tech Stack:** Python, requests, pytest. 기존: `local/localapp/kis_futures_broker.py`. 스펙: 정정취소 VTTO1103U `/order-rvsecncl`, 체결조회 VTTO5201R `/inquire-ccnl`, 시장가 `ORD_DVSN_CD=02`+`UNIT_PRICE=0`.

**규칙:** worktree `C:/Users/USER/_wt-p1`(브랜치 `plan/p1-domestic-futures-phase2`, 최신 origin/main 위). 테스트 `cd .../local && python -m pytest ...`. 기존 국내·해외·sim·시나리오 테스트 무회귀 green. main 직접 push 금지(T4 PR). Broker 프로토콜 반환형: `order_status`→{order_no,status,filled_qty,remain_qty,fill_price}, `pending_orders`→list, `cancel`→{...}.

---

## 파일 구조
| 파일 | 책임 | 변경 |
|---|---|---|
| `local/localapp/kis_futures_broker.py` | order_type(limit/market)·취소바디·체결파서·5 메서드 구현 | 수정 |
| `local/tests/test_kis_futures_broker.py` | 순수함수 단위검증 추가 | 수정(추가) |

---

### Task 1: 시장가 주문 (build_futures_order_body order_type + buy/sell)

**Files:** Modify `local/localapp/kis_futures_broker.py`; add tests to `local/tests/test_kis_futures_broker.py`.

- [ ] **Step 1: 실패 테스트** — append to `local/tests/test_kis_futures_broker.py`:

```python
def test_build_order_body_market_buy():
    b = build_futures_order_body(cano="50188802", acnt_prdt_cd="03", symbol="A01606",
                                 qty=1, price=0, side="buy", order_type="market")
    assert b["ORD_DVSN_CD"] == "02"            # 시장가
    assert b["UNIT_PRICE"] == "0"              # 시장가 가격 0
    assert b["SLL_BUY_DVSN_CD"] == "02"


def test_build_order_body_limit_default_unchanged():
    # order_type 미지정 → 기존 지정가 동작(회귀).
    b = build_futures_order_body(cano="5", acnt_prdt_cd="03", symbol="A01606",
                                 qty=2, price=375.0, side="sell")
    assert b["ORD_DVSN_CD"] == "01" and b["UNIT_PRICE"] == "375.0"
```

- [ ] **Step 2: 실패 확인** — `cd C:/Users/USER/_wt-p1/local && python -m pytest tests/test_kis_futures_broker.py -q` → FAIL (order_type kwarg 미지원).

- [ ] **Step 3: 구현** — in `local/localapp/kis_futures_broker.py`:

(a) Change `build_futures_order_body` signature + body. Replace the function header line:
```python
def build_futures_order_body(*, cano: str, acnt_prdt_cd: str, symbol: str,
                             qty: int, price, side: str) -> dict:
```
with:
```python
def build_futures_order_body(*, cano: str, acnt_prdt_cd: str, symbol: str,
                             qty: int, price, side: str, order_type: str = "limit") -> dict:
```
Add a guard after the `side` check:
```python
    if order_type not in ("limit", "market"):
        raise ValueError(f"order_type는 limit|market: {order_type}")
    _is_limit = order_type == "limit"
```
Then change the `UNIT_PRICE` and `ORD_DVSN_CD` lines in the returned dict:
```python
        "UNIT_PRICE": str(price) if _is_limit else "0",     # 시장가는 0
        ...
        "ORD_DVSN_CD": "01" if _is_limit else "02",         # 01 지정가 / 02 시장가
```
(Keep the docstring; update its first line to mention 지정가/시장가.)

(b) Add `order_type` passthrough to `_submit_order`. Find:
```python
    def _submit_order(self, symbol: str, qty: int, price, side: str) -> dict:
        body = build_futures_order_body(cano=self.cano, acnt_prdt_cd=self.acnt_prdt_cd,
                                        symbol=symbol, qty=qty, price=price, side=side)
```
Replace with:
```python
    def _submit_order(self, symbol: str, qty: int, price, side: str,
                      order_type: str = "limit") -> dict:
        body = build_futures_order_body(cano=self.cano, acnt_prdt_cd=self.acnt_prdt_cd,
                                        symbol=symbol, qty=qty, price=price, side=side,
                                        order_type=order_type)
```
(buy_limit/sell_limit call `_submit_order(symbol, qty, limit_price, "buy")` → default limit, unchanged.)

(c) Replace the `buy`/`sell` NotImplementedError methods:
```python
    def buy(self, symbol: str, qty: int) -> dict:
        return self._submit_order(symbol, qty, 0, "buy", order_type="market")

    def sell(self, symbol: str, qty: int) -> dict:
        return self._submit_order(symbol, qty, 0, "sell", order_type="market")
```

- [ ] **Step 4: 통과 + 회귀** — `cd C:/Users/USER/_wt-p1/local && python -m pytest tests/test_kis_futures_broker.py -q` → 기존 + 신규 2개 PASS(기존 지정가 테스트 무변경).

- [ ] **Step 5: 커밋**
```
cd C:/Users/USER/_wt-p1
git add local/localapp/kis_futures_broker.py local/tests/test_kis_futures_broker.py
git commit -m "feat(local): 국내선물 시장가 주문(ORD_DVSN_CD=02) — build_futures_order_body order_type + buy/sell"
```

---

### Task 2: 정정취소 (build_futures_cancel_body + cancel)

**Files:** Modify `local/localapp/kis_futures_broker.py`; add tests.

스펙 VTTO1103U `/order-rvsecncl`: 취소=RVSE_CNCL_DVSN_CD=02·UNIT_PRICE=0·KRX_NMPR_CNDT_CD=0·ORD_DVSN_CD=01·RMN_QTY_YN=Y·ORD_QTY(모의 필수).

- [ ] **Step 1: 실패 테스트** — append:

```python
from localapp.kis_futures_broker import build_futures_cancel_body


def test_build_cancel_body():
    b = build_futures_cancel_body(cano="50188802", acnt_prdt_cd="03",
                                  order_no="0000005605", qty=1)
    assert b["RVSE_CNCL_DVSN_CD"] == "02"      # 취소
    assert b["ORGN_ODNO"] == "0000005605"
    assert b["ORD_QTY"] == "1"                 # 모의 필수
    assert b["UNIT_PRICE"] == "0"              # 취소 시 0
    assert b["KRX_NMPR_CNDT_CD"] == "0"        # 취소 시 0
    assert b["ORD_DVSN_CD"] == "01"            # 취소 시 01
    assert b["RMN_QTY_YN"] == "Y"
    assert b["ORD_PRCS_DVSN_CD"] == "02"
```

- [ ] **Step 2: 실패 확인** — pytest → FAIL (no build_futures_cancel_body).

- [ ] **Step 3: 구현** — in `local/localapp/kis_futures_broker.py`:

(a) Add a path constant near `_BALANCE_PATH`:
```python
_CANCEL_PATH = "/uapi/domestic-futureoption/v1/trading/order-rvsecncl"
```
(b) Add the pure function after `build_futures_order_body`:
```python
def build_futures_cancel_body(*, cano: str, acnt_prdt_cd: str, order_no, qty: int) -> dict:
    """VTTO1103U/TTTO1103U 취소 바디(전량). 순수함수 — 단위검증 대상.

    취소: RVSE_CNCL_DVSN_CD=02·UNIT_PRICE=0·KRX_NMPR_CNDT_CD=0·ORD_DVSN_CD=01·RMN_QTY_YN=Y.
    ORD_QTY는 모의계좌 필수(전량이라도 입력).
    """
    return {
        "ORD_PRCS_DVSN_CD": "02",
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "RVSE_CNCL_DVSN_CD": "02",          # 02: 취소
        "ORGN_ODNO": str(order_no),
        "ORD_QTY": str(int(qty)),
        "UNIT_PRICE": "0",
        "NMPR_TYPE_CD": "",
        "KRX_NMPR_CNDT_CD": "0",
        "RMN_QTY_YN": "Y",                  # 전량
        "FUOP_ITEM_DVSN_CD": "",
        "ORD_DVSN_CD": "01",
    }
```
(c) Replace the `cancel` NotImplementedError method:
```python
    def cancel(self, order_no: str, symbol: str, qty: int) -> dict:
        body = build_futures_cancel_body(cano=self.cano, acnt_prdt_cd=self.acnt_prdt_cd,
                                         order_no=order_no, qty=qty)
        tr = "VTTO1103U" if self.virtual else "TTTO1103U"
        r = requests.post(f"{self.base}{_CANCEL_PATH}", headers=self._headers(tr),
                          json=body, timeout=10)
        r.raise_for_status()
        return _json(r)
```

- [ ] **Step 4: 통과** — pytest → PASS.

- [ ] **Step 5: 커밋**
```
cd C:/Users/USER/_wt-p1
git add local/localapp/kis_futures_broker.py local/tests/test_kis_futures_broker.py
git commit -m "feat(local): 국내선물 정정취소(VTTO1103U order-rvsecncl) — cancel"
```

---

### Task 3: 체결조회 (parse_ccnl_order_status + order_status/pending_orders)

**Files:** Modify `local/localapp/kis_futures_broker.py`; add tests.

스펙 VTTO5201R `/inquire-ccnl`: output1=주문 array. 각 행 odno·tot_ccld_qty(체결)·qty(잔량)·avg_idx(평균가)·rjct_qty(거부). status 유도.

- [ ] **Step 1: 실패 테스트** — append:

```python
from localapp.kis_futures_broker import parse_ccnl_order_status


def test_parse_ccnl_filled():
    resp = {"output1": [
        {"odno": "0000007045", "tot_ccld_qty": "1", "qty": "0", "avg_idx": "400.00", "rjct_qty": "0"},
    ]}
    s = parse_ccnl_order_status(resp, "0000007045")
    assert s["status"] == "filled" and s["filled_qty"] == 1 and s["remain_qty"] == 0
    assert s["fill_price"] == 400.0 and s["order_no"] == "0000007045"


def test_parse_ccnl_partial_and_canonical_odno():
    # 부분체결 + 0-패딩 무관 매칭(canonical lstrip("0")).
    resp = {"output1": [
        {"odno": "0000007006", "tot_ccld_qty": "1", "qty": "1", "avg_idx": "375.0", "rjct_qty": "0"},
    ]}
    s = parse_ccnl_order_status(resp, "7006")
    assert s["status"] == "partial" and s["filled_qty"] == 1 and s["remain_qty"] == 1


def test_parse_ccnl_rejected_and_unknown():
    resp = {"output1": [{"odno": "0000000001", "tot_ccld_qty": "0", "qty": "0", "rjct_qty": "1"}]}
    assert parse_ccnl_order_status(resp, "1")["status"] == "rejected"
    assert parse_ccnl_order_status({}, "999")["status"] == "unknown"
```

- [ ] **Step 2: 실패 확인** — pytest → FAIL.

- [ ] **Step 3: 구현** — in `local/localapp/kis_futures_broker.py`:

(a) Add constant near `_CANCEL_PATH`:
```python
_CCNL_PATH = "/uapi/domestic-futureoption/v1/trading/inquire-ccnl"
```
(b) Add the pure parser after `parse_futures_balance`:
```python
def parse_ccnl_order_status(resp: dict, order_no) -> dict:
    """inquire-ccnl output1에서 order_no 행 → {order_no,status,filled_qty,remain_qty,fill_price}.

    canonical odno 비교(lstrip "0"). status: rejected(rjct>0) / filled(잔량0·체결>0) /
    partial(체결>0) / submitted(체결0) / unknown(행 없음).
    """
    rows = resp.get("output1")
    if not isinstance(rows, list):
        rows = []
    target = str(order_no).lstrip("0")
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("odno", "")).lstrip("0") == target:
            def _i(k):
                try:
                    return int(float(r.get(k, 0) or 0))
                except (ValueError, TypeError):
                    return 0
            filled, remain, rjct = _i("tot_ccld_qty"), _i("qty"), _i("rjct_qty")
            if rjct > 0:
                status = "rejected"
            elif remain == 0 and filled > 0:
                status = "filled"
            elif filled > 0:
                status = "partial"
            else:
                status = "submitted"
            return {"order_no": str(r.get("odno", "")), "status": status,
                    "filled_qty": filled, "remain_qty": remain,
                    "fill_price": float(r.get("avg_idx", 0) or 0)}
    return {"order_no": str(order_no), "status": "unknown",
            "filled_qty": 0, "remain_qty": 0, "fill_price": 0.0}
```
(c) Add a private query helper + replace `order_status`/`pending_orders` methods. Replace the two NotImplementedError methods with:
```python
    def _inquire_ccnl(self, only_unfilled: bool = False) -> dict:
        """inquire-ccnl 조회(당일). only_unfilled=True면 미체결만."""
        import datetime
        today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y%m%d")
        tr = "VTTO5201R" if self.virtual else "TTTO5201R"
        params = {"CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                  "STRT_ORD_DT": today, "END_ORD_DT": today,
                  "SLL_BUY_DVSN_CD": "00", "CCLD_NCCS_DVSN": "02" if only_unfilled else "00",
                  "SORT_SQN": "DS", "STRT_ODNO": "", "PDNO": "", "MKET_ID_CD": "00",
                  "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
        r = requests.get(f"{self.base}{_CCNL_PATH}", headers=self._headers(tr),
                         params=params, timeout=10)
        r.raise_for_status()
        return _json(r)

    def order_status(self, order_no: str) -> dict:
        return parse_ccnl_order_status(self._inquire_ccnl(), order_no)

    def pending_orders(self) -> list[dict]:
        rows = self._inquire_ccnl(only_unfilled=True).get("output1") or []
        return [parse_ccnl_order_status({"output1": [r]}, r.get("odno", ""))
                for r in rows if isinstance(r, dict)]
```

- [ ] **Step 4: 통과 + 전체 회귀** — `cd C:/Users/USER/_wt-p1/local && python -m pytest tests/test_kis_futures_broker.py -q` → 신규 포함 전부 PASS.

- [ ] **Step 5: 커밋**
```
cd C:/Users/USER/_wt-p1
git add local/localapp/kis_futures_broker.py local/tests/test_kis_futures_broker.py
git commit -m "feat(local): 국내선물 체결조회(VTTO5201R inquire-ccnl) — order_status/pending_orders"
```

---

### Task 4: 전체 회귀 + PR

- [ ] **Step 1: 전체 local 테스트** — `cd C:/Users/USER/_wt-p1/local && python -m pytest tests/ -q` → 0 실패(국내·해외·sim·주식 시나리오 무회귀 + P1 신규).

- [ ] **Step 2: PR·머지**
```
cd C:/Users/USER/_wt-p1
git push -u origin plan/p1-domestic-futures-phase2
gh pr create --base main --head plan/p1-domestic-futures-phase2 \
  --title "feat(local): P1 국내선물 phase2 — 시장가·정정취소·체결조회" \
  --body "선물 자동매매 P1. 시장가(ORD_DVSN_CD=02)·정정취소(VTTO1103U)·체결조회(VTTO5201R order_status/pending). 순수함수 단위검증. 라이브 체결검증(A01606 라운드트립)은 다음 국장(국내 모의). Broker 프로토콜 반환형 호환. 무회귀. 🤖 Generated with Claude Code"
gh pr merge --merge --delete-branch
```

- [ ] **Step 3: 워크트리 정리**
```
cd "C:/Users/USER/Desktop/창업/퀀트/platform"
git worktree remove C:/Users/USER/_wt-p1 --force
git worktree prune
```

---

## P1 완료 기준
- 시장가·정정취소·체결조회 순수함수 단위검증 green, 메서드 NotImplementedError 해제.
- 기존 국내·해외·sim·시나리오 테스트 무회귀.
- **라이브 체결검증(A01606 시장가 라운드트립)은 다음 국장**(국내 모의 60044290) — [[project_futures_review_state]] 체크리스트. 그때 시장가 응답·체결조회 output1 형태·취소 라운드트립 확정.
- **다음(P3)**: Trader가 이 브로커(buy/sell/cancel/order_status)를 SimBroker와 동일 프로토콜로 호출.

# P2 — 해외선물 브로커 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 통합 `KisFuturesBroker`가 국내(KRX) + 해외(CME via KIS) 선물을 market 라우팅으로 거래한다 — 해외 주문(지정가+시장가)·잔고·시세·주문가능 + 자격증명 슬롯.

**Architecture:** KisBroker가 주식 국내+해외를 한 클래스에서 다루듯, KisFuturesBroker를 **이중 market 컨텍스트**(domestic·overseas)로 확장. 각 컨텍스트는 자기 자격증명(키·계좌·virtual)·base path·통화를 가짐. 국내 거래 API는 전면 상이하므로 시장별 **순수함수**(주문바디·잔고파싱)를 분리하고 공유는 OAuth·_json·레이트리밋 재시도뿐. 해외 회계는 P0 testbed(instrument_spec)와 정합.

**핵심 제약(확정):** 해외선물 **모의 미지원(실전만)** → 라이브 주문검증은 사용자 실거래로 위임. **CME 실시간 시세 유료** → 자동매매는 yfinance 가격(엔진 dataset) + 시장가/지정가로 동작, KIS 시세는 선택. 해외 prices는 **sCalcDesz 소수점 스케일**(ffcode.mst) 필요.

**Tech Stack:** Python, requests, pytest. 기존: `local/localapp/kis_futures_broker.py`(국내 브로커, PR#22·#26)·`secrets_store.py`. 스펙: 사용자 Downloads `[해외선물옵션] 주문_계좌·기본시세.xlsx`.

**규칙:** worktree `C:/Users/USER/_wt-p2`(브랜치 `plan/p2-overseas-futures-broker`, 최신 origin/main 위). 테스트 `cd .../local && python -m pytest ...`. 국내 브로커 테스트(`test_kis_futures_broker.py`) 무회귀 green 필수. side 정규형 long/short(KIS 매수02→long/매도01→short). main 직접 push 금지(T5 PR). **해외는 모의 없음 → 라이브 주문 호출 코드는 작성하되 실제 발주 검증은 사용자 실거래로 위임**(SimBroker·단위테스트로 로직 검증).

---

## 파일 구조

| 파일 | 책임 | 변경 |
|---|---|---|
| `local/localapp/secrets_store.py` | 해외선물 자격증명 슬롯 `kis_overseas_futures_credentials` | 수정 |
| `local/localapp/kis_overseas_futures.py` | 해외 순수함수: 주문바디(OTFM3001U)·잔고파싱(OTFM1412R)·시세 스케일 | **신규** |
| `local/localapp/kis_futures_broker.py` | 통합 market 라우팅(domestic+overseas 컨텍스트) | 수정 |
| `local/tests/test_kis_overseas_futures.py` | 해외 순수함수 단위검증(스펙 예시 기반) | **신규** |
| `local/tests/test_kis_futures_broker.py` | 통합 라우팅 회귀(국내 무변경 + 해외 라우팅) | 수정(추가) |

> 해외 심볼/스펙(CME globex 코드·sCalcDesz·증거금)은 KIS `ffcode.mst` 마스터 + `상품기본정보`(HHDFC55200000)가 출처. 본 P2는 코드/스케일을 **파라미터/레지스트리로 받는** 순수함수로 분리하고, 라이브 마스터 연동(근월물 코드 산출)은 P3 배선 시 결정(주문은 심볼 문자열을 받으므로 브로커는 심볼 무관). 6종 CME 루트(GC·CL·NQ·NG·SI·BTC)와 sCalcDesz는 T2 주석에 스펙 근거로 명시.

---

### Task 1: 해외선물 자격증명 슬롯

**Files:** Modify `local/localapp/secrets_store.py`; Test: `local/tests/test_kis_overseas_futures.py`.

해외선물은 별도 계좌/키(상품코드 08). 국내(`kis_futures_credentials`)와 분리된 슬롯. 모의 미지원이라 virtual은 항상 False(실전)지만 필드는 유지(일관).

- [ ] **Step 1: 실패 테스트** — create `local/tests/test_kis_overseas_futures.py`:

```python
"""해외선물 자격증명 슬롯 + 순수함수 단위검증."""
from __future__ import annotations

import sys
from pathlib import Path

_LOCAL = Path(__file__).resolve().parent.parent
for _p in (str(_LOCAL), str(_LOCAL.parent / "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def test_overseas_creds_roundtrip(monkeypatch):
    import localapp.secrets_store as ss
    store = {}
    monkeypatch.setattr(ss.keyring, "set_password", lambda s, k, v: store.__setitem__(k, v))
    monkeypatch.setattr(ss.keyring, "get_password", lambda s, k: store.get(k))
    assert ss.load_kis_overseas_futures() is None
    ss.save_kis_overseas_futures("AK", "SK", "80012345-08", virtual=False)
    c = ss.load_kis_overseas_futures()
    assert c == {"app_key": "AK", "app_secret": "SK",
                 "account_no": "80012345-08", "virtual": False}
```

- [ ] **Step 2: 실패 확인** — `cd C:/Users/USER/_wt-p2/local && python -m pytest tests/test_kis_overseas_futures.py -q` → FAIL (no `save_kis_overseas_futures`).

- [ ] **Step 3: 구현** — in `local/localapp/secrets_store.py`, add a module constant near `_KIS_FUT`:
```python
_KIS_OVF = "kis_overseas_futures_credentials"   # 해외선물옵션 계좌(상품코드 08) — 국내선물·주식과 별개
```
and add two functions after `load_kis_futures`:
```python
def save_kis_overseas_futures(app_key: str, app_secret: str, account_no: str,
                              virtual: bool = False) -> None:
    """해외선물옵션 *거래* 자격증명 저장(국내선물·주식과 별개 — 상품코드 08).

    ⚠ KIS 해외선물은 모의투자 미지원 → 실전 전용(virtual=False 고정 권장). 로컬 PC 전용(서버·리포 전송 금지).
    """
    keyring.set_password(KEYRING_SERVICE, _KIS_OVF, json.dumps({
        "app_key": app_key, "app_secret": app_secret,
        "account_no": account_no, "virtual": virtual,
    }))


def load_kis_overseas_futures() -> dict | None:
    raw = keyring.get_password(KEYRING_SERVICE, _KIS_OVF)
    return json.loads(raw) if raw else None
```
Also add `_KIS_OVF` to the `clear()` loop tuple (so cleanup removes it): change `for key in (_KIS, _KIS_FUT, _DEVICE):` → `for key in (_KIS, _KIS_FUT, _KIS_OVF, _DEVICE):`.

- [ ] **Step 4: 통과 확인** — same pytest → PASS (1 passed).

- [ ] **Step 5: 커밋**
```
cd C:/Users/USER/_wt-p2
git add local/localapp/secrets_store.py local/tests/test_kis_overseas_futures.py
git commit -m "feat(local): 해외선물 자격증명 슬롯(kis_overseas_futures) — 상품코드 08·실전전용"
```

---

### Task 2: 해외 순수함수 — 주문바디·잔고파싱·시세 스케일

**Files:** Create `local/localapp/kis_overseas_futures.py`; add tests to `local/tests/test_kis_overseas_futures.py`.

스펙 근거: 주문 OTFM3001U, 잔고 OTFM1412R(output=행형 array), 시세 HHDFC55010000(output1, sCalcDesz 스케일).

- [ ] **Step 1: 실패 테스트** — append to `local/tests/test_kis_overseas_futures.py`:

```python
from localapp.kis_overseas_futures import (
    build_overseas_order_body, parse_overseas_balance, scale_overseas_price,
)


def test_build_order_body_limit_buy():
    b = build_overseas_order_body(cano="81012345", acnt_prdt_cd="08",
                                  symbol="6BZ22", side="buy", qty=1,
                                  price=1.17, order_type="limit")
    assert b["OVRS_FUTR_FX_PDNO"] == "6BZ22"
    assert b["SLL_BUY_DVSN_CD"] == "02"        # 매수
    assert b["PRIC_DVSN_CD"] == "1"            # 지정가
    assert b["FM_LIMIT_ORD_PRIC"] == "1.17"
    assert b["FM_STOP_ORD_PRIC"] == ""
    assert b["FM_ORD_QTY"] == "1"
    assert b["CCLD_CNDT_CD"] == "6"            # 지정가 EOD
    assert b["CPLX_ORD_DVSN_CD"] == "0" and b["ECIS_RSVN_ORD_YN"] == "N"


def test_build_order_body_market_sell():
    s = build_overseas_order_body(cano="81012345", acnt_prdt_cd="08",
                                  symbol="GCZ25", side="sell", qty=2,
                                  price=0, order_type="market")
    assert s["SLL_BUY_DVSN_CD"] == "01"        # 매도
    assert s["PRIC_DVSN_CD"] == "2"            # 시장가
    assert s["FM_LIMIT_ORD_PRIC"] == ""        # 시장가 → 가격 공란
    assert s["CCLD_CNDT_CD"] == "2"            # 시장가
    assert s["FM_ORD_QTY"] == "2"


def test_build_order_body_bad_side():
    import pytest
    with pytest.raises(ValueError):
        build_overseas_order_body(cano="1", acnt_prdt_cd="08", symbol="x",
                                  side="hold", qty=1, price=1, order_type="limit")


def test_parse_overseas_balance_row_array():
    # OTFM1412R output = 행형 array. side: 02 매수→long, 01 매도→short.
    resp = {"output": [
        {"ovrs_futr_fx_pdno": "6BZ22", "sll_buy_dvsn_cd": "02", "fm_ustl_qty": "2",
         "fm_ccld_avg_pric": "1.1898", "fm_now_pric": "1.2350",
         "fm_evlu_pfls_amt": "5656.24", "crcy_cd": "USD"},
        {"ovrs_futr_fx_pdno": "ZBZ22", "sll_buy_dvsn_cd": "01", "fm_ustl_qty": "100",
         "fm_ccld_avg_pric": "132.29", "fm_now_pric": "131.21",
         "fm_evlu_pfls_amt": "107438.00", "crcy_cd": "USD"},
    ]}
    out = parse_overseas_balance(resp)
    assert len(out["positions"]) == 2
    p0 = out["positions"][0]
    assert p0["symbol"] == "6BZ22" and p0["side"] == "long" and p0["qty"] == 2
    assert p0["avg_price"] == 1.1898 and p0["eval_price"] == 1.2350
    assert p0["eval_pnl"] == 5656.24 and p0["currency"] == "USD"
    assert out["positions"][1]["side"] == "short" and out["positions"][1]["qty"] == 100


def test_parse_overseas_balance_empty():
    assert parse_overseas_balance({}) == {"positions": []}
    assert parse_overseas_balance({"output": []}) == {"positions": []}


def test_scale_overseas_price():
    # sCalcDesz(계산소수점): GC -1 → raw 19225 = 1922.5 ; 6A -4 → 68825 = 6.8825 (=6882.5*0.0001)
    assert scale_overseas_price("19225", -1) == 1922.5
    assert scale_overseas_price("  75.63 ", 0) == 75.63    # 스케일 0 = 그대로(trim)
    assert scale_overseas_price("", -1) == 0.0
```

- [ ] **Step 2: 실패 확인** — `cd C:/Users/USER/_wt-p2/local && python -m pytest tests/test_kis_overseas_futures.py -q` → FAIL (no module).

- [ ] **Step 3: 구현** — create `local/localapp/kis_overseas_futures.py`:

```python
"""KIS 해외선물옵션 *거래* 순수함수 — 주문바디(OTFM3001U)·잔고파싱(OTFM1412R)·시세 스케일.

해외는 모의투자 미지원(실전 전용). 국내선물(kis_futures_broker)과 API 전면 상이:
심볼=OVRS_FUTR_FX_PDNO(CME globex, 예 GCZ25), 잔고 output=행형 array, 통화 USD.
side 정규형 long(매수 02)/short(매도 01). 네트워크 없는 순수함수 — 단위검증 대상.

⚠ 시세(HHDFC55010000)는 raw 정수를 ffcode.mst의 sCalcDesz(계산소수점)로 스케일해야 정확
   (예: GC sCalcDesz -1 → raw 19225 = 1922.5). scale_overseas_price가 그 변환.
6종 CME 루트(분석 dashboard 정합): GC(금)·CL(원유)·NQ(나스닥)·NG(천연가스)·SI(은)·BTC(비트코인).
"""
from __future__ import annotations

_SIDE_CD = {"buy": "02", "sell": "01"}          # 주문 SLL_BUY_DVSN_CD
_POS_SIDE = {"02": "long", "01": "short"}       # 잔고 sll_buy_dvsn_cd → 정규 side


def build_overseas_order_body(*, cano: str, acnt_prdt_cd: str, symbol: str,
                              side: str, qty: int, price, order_type: str) -> dict:
    """OTFM3001U 주문 바디. side: buy|sell. order_type: limit|market.

    지정가: PRIC_DVSN_CD=1·FM_LIMIT_ORD_PRIC=price·CCLD_CNDT_CD=6(EOD).
    시장가: PRIC_DVSN_CD=2·FM_LIMIT_ORD_PRIC=""·CCLD_CNDT_CD=2.
    """
    if side not in _SIDE_CD:
        raise ValueError(f"side는 buy|sell: {side}")
    if order_type not in ("limit", "market"):
        raise ValueError(f"order_type는 limit|market: {order_type}")
    is_limit = order_type == "limit"
    return {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,                       # 해외선물 "08"
        "OVRS_FUTR_FX_PDNO": symbol,                        # CME globex 코드
        "SLL_BUY_DVSN_CD": _SIDE_CD[side],
        "FM_LQD_USTL_CCLD_DT": "",
        "FM_LQD_USTL_CCNO": "",
        "PRIC_DVSN_CD": "1" if is_limit else "2",           # 1 지정 / 2 시장
        "FM_LIMIT_ORD_PRIC": str(price) if is_limit else "",
        "FM_STOP_ORD_PRIC": "",
        "FM_ORD_QTY": str(int(qty)),
        "FM_LQD_LMT_ORD_PRIC": "",
        "FM_LQD_STOP_ORD_PRIC": "",
        "CCLD_CNDT_CD": "6" if is_limit else "2",           # 6 지정가EOD / 2 시장가
        "CPLX_ORD_DVSN_CD": "0",
        "ECIS_RSVN_ORD_YN": "N",
        "FM_HDGE_ORD_SCRN_YN": "N",
    }


def parse_overseas_balance(resp: dict) -> dict:
    """OTFM1412R(미결제내역=잔고) → {positions:[{symbol,side,qty,avg_price,eval_price,eval_pnl,currency}]}.

    output = 행형 array. 0수량 제외. side: 02 매수→long / 01 매도→short.
    """
    rows = resp.get("output")
    if not isinstance(rows, list):
        rows = []
    positions = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            qty = int(float(r.get("fm_ustl_qty", 0) or 0))
        except (ValueError, TypeError):
            qty = 0
        if qty == 0:
            continue
        positions.append({
            "symbol": str(r.get("ovrs_futr_fx_pdno", "") or "").strip(),
            "side": _POS_SIDE.get(str(r.get("sll_buy_dvsn_cd", "")).strip(), ""),
            "qty": qty,
            "avg_price": float(r.get("fm_ccld_avg_pric", 0) or 0),
            "eval_price": float(r.get("fm_now_pric", 0) or 0),
            "eval_pnl": float(r.get("fm_evlu_pfls_amt", 0) or 0),
            "currency": str(r.get("crcy_cd", "") or "").strip(),
        })
    return {"positions": positions}


def scale_overseas_price(raw, scalc_desz: int) -> float:
    """해외 시세 raw 값을 sCalcDesz(계산소수점)로 스케일. raw×10^scalc_desz.

    예: GC sCalcDesz=-1 → "19225"×10^-1 = 1922.5. 빈값/이상치는 0.0.
    """
    s = str(raw).strip()
    if not s:
        return 0.0
    try:
        return float(s) * (10.0 ** scalc_desz)
    except (ValueError, TypeError):
        return 0.0
```

- [ ] **Step 4: 통과 확인** — `cd C:/Users/USER/_wt-p2/local && python -m pytest tests/test_kis_overseas_futures.py -q` → PASS (7 passed: 1 from T1 + 6 here).

> 주의: `test_scale_overseas_price`의 `6A -4 → 68825 = 6.8825` 주석은 스펙 예시(6882.5→0.68825는 raw 6882.5 기준)와 표기가 다를 수 있다. 단언은 `19225,-1→1922.5`와 `75.63,0→75.63`·빈값→0.0만 검증(스케일 공식 raw×10^desz 자체). 공식이 스펙과 어긋나면(예: 다른 부호규약) STOP하고 DONE_WITH_CONCERNS로 보고.

- [ ] **Step 5: 커밋**
```
cd C:/Users/USER/_wt-p2
git add local/localapp/kis_overseas_futures.py local/tests/test_kis_overseas_futures.py
git commit -m "feat(local): 해외선물 순수함수 — 주문바디(지정가·시장가)·잔고파싱(행형)·시세 스케일"
```

---

### Task 3: 통합 KisFuturesBroker — market 라우팅

**Files:** Modify `local/localapp/kis_futures_broker.py`; add tests to `local/tests/test_kis_futures_broker.py`.

KisFuturesBroker를 국내+해외 **이중 컨텍스트**로 확장. 국내 경로는 기존과 동일 동작(회귀 보존), 해외 경로를 추가하고 메서드가 market으로 라우팅. **먼저 현재 파일 전체를 읽어** 기존 __init__/_token/_headers/buy_limit/account_snapshot 정확한 코드를 파악할 것.

설계:
- `__init__`: `load_kis_futures()`(국내) + `load_kis_overseas_futures()`(해외) 둘 다 시도. 각각 `_MarketCtx`(key·secret·account(cano,prdt)·virtual·base·token cache) 생성, 없으면 None. 최소 한 쪽 필수(둘 다 없으면 기존처럼 RuntimeError).
- `_MarketCtx`: 토큰 발급/캐시·_headers를 컨텍스트별로(키가 달라 토큰 별개). 국내 base=domestic-futureoption(VTS/REAL by virtual), 해외 base=overseas-futureoption(REAL 고정, 모의없음).
- market 라우팅: 메서드에 `market` 인자(또는 심볼→market 매핑). 국내 메서드(buy_limit/sell_limit/account_snapshot/price/today_open)는 domestic ctx로, 해외 신규 메서드는 overseas ctx로. **기존 공개 메서드 시그니처·동작은 국내 그대로 유지**(test_kis_futures_broker.py 회귀).
- 해외 메서드: `overseas_buy_limit/sell_limit`(build_overseas_order_body limit), `overseas_buy/sell`(market), `overseas_account_snapshot`(OTFM1412R→parse_overseas_balance), `overseas_orderable`(OTFM3304R). 통화 USD.

> 본 태스크는 통합 리팩토링이라 정확한 코드는 현재 파일 의존 → 구현자가 파일을 읽고 TDD로 진행. 아래 테스트가 계약을 고정한다.

- [ ] **Step 1: 회귀+라우팅 테스트** — append to `local/tests/test_kis_futures_broker.py` (the import block already adds local/ to path):

```python
def test_overseas_order_body_via_helper_is_used(monkeypatch):
    # 통합 브로커가 해외 주문에 build_overseas_order_body를 쓰는지(라우팅) — 네트워크 모킹.
    import localapp.kis_futures_broker as kfb
    captured = {}

    class _Resp:
        status_code = 200
        content = b'{"rt_cd":"0","msg_cd":"APBK0013","msg1":"ok","output":{"ODNO":"00298040","ORD_DT":"20260608"}}'
        def raise_for_status(self): pass

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url; captured["body"] = json; captured["tr"] = headers.get("tr_id")
        return _Resp()

    # overseas 컨텍스트만 가진 브로커 구성(국내 None)
    b = kfb.KisFuturesBroker.__new__(kfb.KisFuturesBroker)
    kfb._install_test_overseas_ctx(b, key="AK", secret="SK", account_no="80012345-08")  # 헬퍼(아래 구현)
    monkeypatch.setattr(kfb.requests, "post", fake_post)
    monkeypatch.setattr(b, "_overseas_token", lambda: "TKN") if hasattr(b, "_overseas_token") else None

    r = b.overseas_buy_limit("GCZ25", 1, 1922.5)
    assert "overseas-futureoption/v1/trading/order" in captured["url"]
    assert captured["body"]["OVRS_FUTR_FX_PDNO"] == "GCZ25"
    assert captured["body"]["SLL_BUY_DVSN_CD"] == "02" and captured["body"]["PRIC_DVSN_CD"] == "1"
    assert r["output"]["ODNO"] == "00298040"
```

> ⚠ 이 테스트의 `_install_test_overseas_ctx`·`_overseas_token` 등은 **구현 설계에 종속**된다. 구현자는: (1) 먼저 현재 broker 파일을 읽고 컨텍스트 구조를 확정, (2) 위 테스트를 *그 구조에 맞게* 조정(네트워크는 monkeypatch로 차단, 실제 발주 없음), (3) 핵심 단언(해외 endpoint·OVRS_FUTR_FX_PDNO·side·지정가 코드가 라우팅돼 전달됨)은 유지. 목표=「해외 주문이 overseas endpoint+해외 바디로 라우팅된다」를 네트워크 없이 검증.

- [ ] **Step 2: 국내 회귀 먼저 확인** — `cd C:/Users/USER/_wt-p2/local && python -m pytest tests/test_kis_futures_broker.py -q` (리팩토링 전 baseline 통과 수 기록).

- [ ] **Step 3: 구현** — 현재 `kis_futures_broker.py`를 읽고 이중 컨텍스트로 리팩토링:
  - `_MarketCtx` 내부 클래스/데이터구조: (key, secret, cano, acnt_prdt_cd, virtual, base, _tok, _tok_exp) + `token()`·`headers(tr_id)`.
  - `__init__`: 국내(load_kis_futures)·해외(load_kis_overseas_futures) 각각 있으면 ctx 생성. 기존 `self.base/self.virtual/self.cano` 등 **국내 속성은 호환 유지**(기존 국내 메서드가 그대로 동작하도록) 또는 국내 메서드가 domestic ctx를 쓰도록 내부 위임(공개 동작 동일).
  - 해외 메서드 추가(위 설계). 해외 주문은 `build_overseas_order_body`, 잔고는 `parse_overseas_balance`(from .kis_overseas_futures import), tr_id=OTFM3001U(주문)·OTFM1412R(잔고)·OTFM3304R(주문가능)·HHDFC55010000(시세). 해외 base=`https://openapi.koreainvestment.com:9443`(실전).
  - 테스트 헬퍼(`_install_test_overseas_ctx` 등)는 구현 구조에 맞춰 정의하거나, 테스트를 `KisFuturesBroker.__new__`+속성주입 방식으로 작성.

- [ ] **Step 4: 통과 + 국내 회귀** — `cd C:/Users/USER/_wt-p2/local && python -m pytest tests/test_kis_futures_broker.py -q` → 기존 국내 테스트 전부 + 신규 해외 라우팅 테스트 PASS. (국내 공개 동작 무변경)

- [ ] **Step 5: 커밋**
```
cd C:/Users/USER/_wt-p2
git add local/localapp/kis_futures_broker.py local/tests/test_kis_futures_broker.py
git commit -m "feat(local): KisFuturesBroker 통합 market 라우팅 — 해외선물 컨텍스트 추가(국내 무변경)"
```

---

### Task 4: 전체 회귀 + PR

**Files:** (검증·PR만)

- [ ] **Step 1: 전체 local 테스트** — `cd C:/Users/USER/_wt-p2/local && python -m pytest tests/ -q` → 신규(해외 7+라우팅) + 기존(국내 브로커·sim 선물·주식 시나리오) 모두 green, 0 실패.

- [ ] **Step 2: PR·머지**
```
cd C:/Users/USER/_wt-p2
git push -u origin plan/p2-overseas-futures-broker
gh pr create --base main --head plan/p2-overseas-futures-broker \
  --title "feat(local): P2 해외선물 브로커 — 통합 market 라우팅(주문 지정가·시장가·잔고·시세)" \
  --body "선물 자동매매 P2. 해외선물 자격증명 슬롯·순수함수(OTFM3001U 주문바디·OTFM1412R 행형 잔고파싱·sCalcDesz 시세스케일)·KisFuturesBroker 통합 market 라우팅. 해외 모의 미지원→실주문 검증은 사용자 실거래로 위임(SimBroker·단위테스트로 로직검증). CME 유료시세 불필요(yfinance 가격+시장가/지정가). 국내 브로커 무회귀. 🤖 Generated with Claude Code"
gh pr merge --merge --delete-branch
```

- [ ] **Step 3: 워크트리 정리**
```
cd "C:/Users/USER/Desktop/창업/퀀트/platform"
git worktree remove C:/Users/USER/_wt-p2 --force
git worktree prune
```

---

## P2 완료 기준

- 해외 자격증명 슬롯·순수함수(주문바디 지정가+시장가·행형 잔고파싱·시세 스케일) 단위검증 green.
- KisFuturesBroker가 국내+해외 라우팅, **국내 공개 동작 무변경**(기존 테스트 green).
- 전체 local 테스트 0 실패.
- **검증 위임 명시**: 해외 실주문 경로는 사용자 실거래로 검증(모의 없음). read-only 실전(시세·잔고·주문가능)은 사용자 해외 실전키 제공 시 별도 1회.
- **다음(P3)**: Trader가 심볼→market 라우팅으로 이 브로커를 호출(증거금 사이징·롱숏·reconcile). 근월물 CME 코드 산출(ffcode.mst/상품기본정보)은 P3 배선 시 확정.

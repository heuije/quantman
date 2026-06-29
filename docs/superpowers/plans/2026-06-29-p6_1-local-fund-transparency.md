# P6-1 — 로컬 체결 투입 투명성 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. 상위 spec:
> [account-linked-strategy](../specs/2026-06-29-account-linked-strategy-and-fund-transparency-design.md) §3.5(사후 투명성).

**Goal:** 체결 시점에 per-order **투입금액**(주식)·**명목·증거금·레버리지**(선물)를 계산해 trade 기록과
decision에 실어, "코스피200선물을 몇 계약·명목 얼마·증거금 얼마·몇 배 레버리지로 매매했는지"가 snapshot으로
표면화되게 한다. (서버 preview는 선물을 사이징 못 함 — 보안경계 — 이라 *발주 시점 로컬*이 진실원천.)

**Architecture:** `trader._apply_fill`(체결 반영, line 541-664)는 이미 `spec = instrument_spec(symbol)`
(`multiplier`·`init_margin_rate`·`currency`)·`is_fut`·`mult`·`filled_qty`·`fill_price`를 가진다. 여기서 `invest`
dict를 만들어 ① trade `ev`(line 599·643) ② `bought`/`sold` decision의 extra(line 612·658) ③ detail 문자열에
싣는다. 새 데이터·새 조회 0(기존 spec·체결값 재사용). 표시(웹·로컬 GUI 열)는 P6-3 후속.

**Tech Stack:** Python(localapp trader). `quant_core.exec_defaults.instrument_spec`(이미 import). pytest 시나리오.

**불변식:** 주식 경로 무변경(invest.amount만 추가). INV-SEC 무관(금액·레버리지는 체결요약 범주 — 계좌번호 아님).

---

## Task 1: `_apply_fill`에 invest(투입 투명성) 부착 (TDD)

**Files:**
- Modify: `local/localapp/trader.py` (`_apply_fill`, line 565 직후 헬퍼 + line 599·612·643·658)
- Test: `local/tests/test_fund_transparency.py` (신규 — `tests/scenarios/conftest.py` isolated_trader 재사용)

- [ ] **Step 1: 실패 테스트 작성**

`tests/scenarios/test_coverage_gate.py`·`test_account_guard.py`의 isolated_trader·SimBroker·_DEF·_DS 패턴 재사용
(scenarios/conftest.py fixture). 케이스:
```python
"""P6-1 — 체결 invest(투입 투명성) 부착. scenarios 하니스 재사용."""

def test_stock_buy_records_amount(...):
    # SimBroker 주식 매수 체결 → "bought" decision.extra.invest.amount == qty*fill, currency KRW
    # (또는 trade 기록 ev.invest.amount). amount = filled_qty * fill_price.
    ...
    assert inv["amount"] == approx(qty * fill)
    assert "leverage" not in inv          # 주식엔 레버리지 없음

def test_futures_buy_records_notional_margin_leverage(...):
    # 코스피200선물 매수 체결 → invest.notional == qty*fill*250000,
    # invest.margin == notional*0.10, invest.leverage == 10.0 (1/init_margin_rate)
    ...
    assert inv["notional"] == approx(qty * fill * 250_000)
    assert inv["margin"] == approx(inv["notional"] * 0.10)
    assert inv["leverage"] == approx(10.0)
```
> 코스피200선물 spec: multiplier 250000·init_margin_rate 0.10(=레버리지 10x). SimBroker 체결가·수량은
> 하니스가 정함 — 단언은 비율(approx)로. invest 위치(trade ev vs decision extra)는 둘 다 부착하므로 둘 중 하나로 단언.

- [ ] **Step 2: 테스트 실패 확인**

Run: `cd local && python -m pytest tests/test_fund_transparency.py -v -p no:faulthandler`
Expected: FAIL (invest 키 없음).

- [ ] **Step 3: 구현 — invest 계산 + 부착**

`_apply_fill`의 `spec`/`is_fut`/`mult` 확보 직후(line 567 이후) 헬퍼:
```python
            def _invest_of(qty: int, px: float) -> dict:
                """체결 per-order 투입 투명성(snapshot 표면화용). 주식=투입금액, 선물=명목·증거금·레버리지.
                새 조회 0 — spec(이미 보유)·체결값만. 금액은 체결요약 범주(INV-SEC 무관)."""
                if is_fut:
                    notional = qty * px * mult
                    mr = spec.init_margin_rate or 0.0
                    return {"notional": round(notional, 2),
                            "margin": round(notional * mr, 2) if mr else None,
                            "leverage": round(1.0 / mr, 1) if mr else None,
                            "currency": spec.currency}
                return {"amount": round(qty * px, 2), "currency": spec.currency}
```
buy/sell 양쪽에서 `inv = _invest_of(filled_qty, fill_price)` 계산 후:
- trade `ev`(line 599·643 dict)에 `"invest": inv` 추가.
- `bought` decision extra(line 612-614)를 `{"intended": intended, "fill": fill_price, "invest": inv}`로.
- `sold` decision(line 658-659)에 extra 추가: `..., detail, {"fill": fill_price, "invest": inv})`
  (현재 sold는 extra 없음 — order_log.decision 시그니처가 extra optional인지 확인; 그렇게 호출).
- detail 문자열 enrich(가독): 선물이면 `detail += f" · 명목 {inv['notional']:,.0f} 증거금 {inv['margin']:,.0f} (레버리지 {inv['leverage']}x)"`(margin/leverage None 아닐 때), 주식이면 `detail += f" · 투입 {inv['amount']:,.0f}원"`.

- [ ] **Step 4: 테스트 통과 확인**

Run: `cd local && python -m pytest tests/test_fund_transparency.py -v -p no:faulthandler`
Expected: PASS.

- [ ] **Step 5: 전체 회귀 + 커밋**

Run: `cd local && python -m pytest -q -p no:faulthandler` → 전부 pass(기존 trade/decision 소비처 무영향 — 필드 *추가*만).
```bash
git add local/localapp/trader.py local/tests/test_fund_transparency.py
git commit -m "feat(local): 체결 투입 투명성 — 금액·(선물)명목·증거금·레버리지 (P6-1)"
```

---

## 비범위 (후속)
- **P6-2 (서버 preview):** 주식 예상수수료 + 선물 정적 레버리지 정보. (별도)
- **P6-3 (표시):** 웹 ChatResult/타임라인·로컬 GUI 주문 열에 invest 표시. (별도)
- **P7:** 실제 수수료(KIS TTTC8715R) + net 실현손익.

## Self-Review
- **Spec §3.5 커버리지:** 체결 per-trade 투입금액 + (선물)레버리지·증거금 → Task 1. ✓ (표시는 P6-3.)
- **Placeholder:** 테스트는 scenarios 하니스 재사용 *명시*. invest 위치(ev/extra 둘 다) 명시.
- **타입 일관성:** `_invest_of(qty,px)->dict`. 주식={amount,currency}, 선물={notional,margin,leverage,currency}.
  buy·sell 양쪽 동일 헬퍼. decision extra·trade ev 동일 dict.
- **무영향:** 기존 trade/decision은 필드 *추가*만 — 소비처(웹·catchup·order_log) 회귀 없음(전체 스위트가 잠금).
  margin/leverage는 init_margin_rate=0(비정상)이면 None(보수적·크래시 없음).

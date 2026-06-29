# P6-4 — 실행 명세 요약 + 수수료 라벨 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. 상위 spec §3.4·§3.5
> + "투명성 계약" 검토(2026-06-29). 사장님 #1(수수료=가정 라벨)·#2(실행 명세 요약). #3(슬리피지 가드)는 취소.

**Goal:** 전략이 **정확히 어떻게 매매되는지**를 평문으로, 4분류(확정/가정/발주시점/미지)로 보여줘 사용자가
오해 없이 신뢰하고 맡길 수 있게 한다. 수수료·슬리피지 등 *가정값*은 명시적으로 "가정"이라 표기.

**Architecture:** 엔진 사실(IR + exec_defaults + 상품카탈로그)의 **단일 출처 = core**. `execution_summary(def)`가
4분류 요약을 만들고(순수·테스트가능), 서버가 endpoint로 노출, 웹이 렌더(StrategyDetail 상시). 가정값(수수료·
슬리피지·tolerance·갭필터)을 TS에 중복하지 않음(드리프트 방지). #1(수수료 라벨)은 요약의 '가정' 항목 +
P6-3 MonitorCards 라벨에 반영.

**Tech Stack:** core(quant_core ir_engine·exec_defaults), FastAPI, React+TS.

**불변식:** 읽기 전용 파생(전략 변경 0). 가정값은 "가정" 명시 — 실율·실슬리피지로 오해 방지(정직).

---

## Task 1: core `execution_summary` (순수·TDD)

**Files:**
- Create: `core/quant_core/ir_engine/execution_summary.py`
- Test: `core/tests/test_execution_summary.py`

- [ ] **Step 1: 실패 테스트**

```python
"""execution_summary — 전략 실행 명세 4분류 요약(확정/가정/발주시점/미지)."""
from quant_core.ir_engine.execution_summary import execution_summary

def test_stock_pct_cash_summary():
    d = {"universe": {"kind": "list", "symbols": ["005930","000660"]},
         "signal": {...최소 유효...},
         "position": {"direction": "long",
                      "sizing": {"mode": "pct_cash", "amount_pct": 10.0},
                      "exit": {"stop_loss": -3.0, "take_profit": 5.0, "hold_days": 5}}}
    s = execution_summary(d)
    cats = {e["label"]: e["value"] for e in s["confirmed"]}
    assert "롱" in cats["방향"]
    assert "10" in cats["사이징"]                 # amount_pct
    assert "손절" in cats["청산"] and "보유" in cats["청산"]
    assumed = {e["label"]: e["value"] for e in s["assumed"]}
    assert "가정" in assumed["수수료"]            # 가정 명시
    assert any("실제 수수료" in u for u in s["unknown"])

def test_futures_summary_has_leverage():
    d = {"universe": {"kind": "single", "symbols": ["코스피200선물"]},
         "signal": {...}, "position": {"direction": "long",
         "sizing": {"mode": "pct_cash", "futures_margin_pct": 20.0},
         "exit": {"hold_days": 0}}}
    s = execution_summary(d)
    conf = {e["label"]: e["value"] for e in s["confirmed"]}
    assert "20%" in conf["사이징"]                # futures_margin_pct
    assert any("레버리지" in e["value"] or "레버리지" in e["label"] for e in s["assumed"])
    assert any("계약수" in x for x in s["at_order"])
    assert "당일" in conf["청산"]                 # hold_days=0
```

- [ ] **Step 2: 테스트 실패 확인** — `cd core && python -m pytest tests/test_execution_summary.py -v` → FAIL(모듈 없음).

- [ ] **Step 3: 구현**

`execution_summary.py`:
```python
"""전략 IR + exec_defaults + 상품카탈로그 → 사용자용 실행 명세 4분류 요약.
확정=전략 설정값 · 가정=시스템/백테스트 default(명시) · 발주시점=실시간 결정 · 미지=사후/외부.
엔진 사실의 단일 출처(core) — 웹/서버가 이걸 렌더만."""
from __future__ import annotations
from .spec import StrategyIR
from ..exec_defaults import merged_execution, instrument_spec

_DIR = {"long": "롱(매수)", "short": "숏(매도)", "long_short": "롱/숏"}
_SIZING = {"pct_cash": "자본의 {amount_pct}%", "fixed_amount": "종목당 {amount_krw}원",
           "equal_weight": "동일가중", "signal_proportional": "신호비례", ...}

def execution_summary(strategy_def: dict) -> dict:
    ir = StrategyIR.model_validate(strategy_def)
    ex = merged_execution(strategy_def.get("execution"))
    pos, siz, exit_ = ir.position, ir.position.sizing, ir.position.exit
    syms = (ir.universe.symbols or [])
    is_fut = any(instrument_spec(s).asset_class == "futures" for s in syms)

    confirmed = [
        {"label": "종목", "value": _universe_desc(ir.universe)},
        {"label": "방향", "value": _DIR.get(pos.direction, pos.direction)},
        {"label": "사이징", "value": _sizing_desc(siz, is_fut)},   # 주식=현금%/정액, 선물=증거금 N%
        {"label": "청산", "value": _exit_desc(exit_)},             # 손절/익절/보유/트레일링/조건 OR
        {"label": "진입 시점", "value": _entry_desc(ir)},          # 시초가/예약·신호평가
    ]
    assumed = [
        {"label": "수수료", "value": f"{_comm_pct(ir, ex)}% (가정 — 실제 계좌 수수료율 아님)"},
        {"label": "매도세", "value": f"{ex['bt_sell_tax_bps']/100:.2f}% (가정)"},
        {"label": "슬리피지(백테스트 가정)", "value": f"{ex['bt_slippage_bps']/100:.2f}%"},
        {"label": "주문 가격", "value": "국내 시장가(단일가) · 미국 지정가 ±{}%".format(ex["buy_tolerance_pct"])},
        {"label": "갭 필터", "value": f"전일比 {ex['gap_filter_pct']}% 초과 갭이면 신규진입 폐기"},
        {"label": "가격 제한", "value": "국내 ±30% 클램프"},
    ]
    if is_fut:
        sp = next(instrument_spec(s) for s in syms if instrument_spec(s).asset_class == "futures")
        lev = round(1.0/sp.init_margin_rate, 1) if sp.init_margin_rate else None
        assumed.append({"label": "레버리지(선물)",
                        "value": f"최대 {lev}x (개시증거금률 {sp.init_margin_rate*100:.0f}%, 승수 {sp.multiplier:,.0f})"})
    at_order = ["실제 수량/계약수 (발주 시점 주문가능현금으로 계산)",
                "실제 주문 가격 (발주 시점 현재가)"]
    if is_fut:
        at_order.append("선물 명목·증거금 (= 계약수 × 가격 × 승수, 증거금=명목×증거금률)")
    unknown = ["실제 수수료 (실전 청산 후 KIS 조회로 확정)",
               "실제 체결 슬리피지 (체결 후 사후 측정)"]
    return {"confirmed": confirmed, "assumed": assumed,
            "at_order": at_order, "unknown": unknown}
```
헬퍼(`_universe_desc`·`_sizing_desc`·`_exit_desc`·`_entry_desc`·`_comm_pct`)는 IR 필드를 평문으로.
`_comm_pct`: `ir.simulation.commission`(있으면 ×100) 아니면 `ex["bt_commission_bps"]/100`(=0.03). 단일 결정.
주식 사이징: `single`이면 "현금 100%"·다종목이면 amount_pct%. 선물이면 "가용현금의 futures_margin_pct%(증거금)".
exit: 채워진 규칙만 "손절 −3% · 익절 +5% · 보유 5일" 식 OR 결합; hold_days==0 → "당일 종가 청산".

- [ ] **Step 4: 통과 + 커밋** — `cd core && python -m pytest tests/test_execution_summary.py -v` → PASS.
```bash
git add core/quant_core/ir_engine/execution_summary.py core/tests/test_execution_summary.py
git commit -m "feat(core): execution_summary — 실행 명세 4분류 요약 (P6-4)"
```

---

## Task 2: 서버 endpoint (TDD)

**Files:**
- Modify: `server/app/routers/strategies.py` (신규 endpoint)
- Test: `server/tests/test_execution_summary_api.py`

- [ ] `GET /strategies/{id}/execution-summary` — 소유 검사 후 `execution_summary(row.definition)` 반환. 기존 strategies 테스트 fixture 재사용. 라운드트립 테스트(주식·선물 요약 4분류 키 존재).
- [ ] 커밋: `feat(server): GET /strategies/{id}/execution-summary (P6-4)`

---

## Task 3: 웹 렌더 + 수수료 라벨 (#1) (tsc/build)

**Files:**
- Modify: `web/src/api.ts`(executionSummary fetch)·`web/src/types.ts`(ExecutionSummary 타입)·`web/src/pages/StrategyDetail.tsx`(섹션)·`web/src/components/MonitorCards.tsx`(#1 라벨)

- [ ] StrategyDetail에 "**이 전략은 이렇게 매매합니다**" 섹션: 4분류(확정/가정/발주시점/미지)를 구분 렌더(확정=강조, 가정=muted+"가정" 톤, 미지=옅게). `api.executionSummary(id)` fetch. DESIGN 토큰.
- [ ] #1: MonitorCards의 est_fee 라벨을 "예상수수료(가정)"로 + (가능하면) tooltip "실제 계좌 수수료율이 아니라 가정치입니다. 전략 설정에서 실수수료율 입력 가능."
- [ ] tsc+build green. 커밋: `feat(web): 실행 명세 요약 섹션 + 수수료 '가정' 라벨 (P6-4 #1·#2)`

---

## Self-Review
- **검토 커버리지:** #1 수수료=가정 라벨(Task1 assumed·Task3 MonitorCards)·#2 4분류 요약(Task1 core·Task2 노출·Task3 렌더). #3 취소(미포함). ✓
- **단일 출처:** 가정값(수수료·슬리피지·tolerance·갭필터)은 core exec_defaults에서만 — 웹 TS 중복 0(드리프트 방지).
- **타입 일관성:** core `{confirmed,assumed:[{label,value}], at_order:[str], unknown:[str]}` ↔ server passthrough ↔ web ExecutionSummary 타입. 4분류 키 동일.
- **검증:** Task1 core 단위테스트(IR→요약)·Task2 API 라운드트립·Task3 tsc+build. 요약은 IR 파생이라 **실거래 불필요 = dev 검증 가능**(P5-4/P6-3과 달리 populated 렌더 검증됨).
- **정직:** 가정/미지 분류로 "이건 가정·이건 사후"를 명시 — 사장님 신뢰 요구의 핵심.

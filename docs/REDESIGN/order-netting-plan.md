# 발주창 넷팅 구현 계획 (Order Netting)

> **For agentic workers:** 이 계획은 §13 근본 아키텍처([order-netting-design.md](order-netting-design.md))를 TDD로 구현한다.
> 단계는 체크박스(`- [ ]`)로 추적. 자금 경로라 각 단계 MockBroker/골든으로 검증하고 커밋.

**Goal:** 한 발주창에서 같은 물리계약·같은 포지션 side의 open↔close가 겹치면, 겹치는 수량을 브로커 주문 없이 원장 이관(합성 체결)해 왕복 수수료를 제거한다.

**Architecture:** PLAN(단일 스냅샷·로컬 여력원장으로 청산·진입 의도 산출) → NET(순수 함수, `(contract_key, position_side)`별 same-side open↔close만 상쇄) → APPLY(핸드오프=기존 `_apply_fill`+intent+락 재사용 합성 체결 2건, 잔여=기존 `_submit_*`) → REPORT(netted 분리 카운트).

**Tech Stack:** Python 3.11, pytest, 기존 `local/localapp/` (trader/runner/order_log/intents), MockBroker 시나리오 테스트.

---

## 파일 구조

| 파일 | 책임 | 신규/수정 |
|---|---|---|
| `local/localapp/netting.py` | 순수 넷팅 함수 + 데이터 타입. 브로커/IO 없음 | **신규** |
| `local/tests/test_netting.py` | netting.py 단위 테스트 | **신규** |
| `local/localapp/trader.py` | PLAN(의도 산출·여력원장)·APPLY(`_apply_netted_leg`)·contract_key | 수정 |
| `local/localapp/runner.py` | run_close_cycle + 아침 cycle을 PLAN-NET-APPLY로 배선 | 수정 |
| `local/localapp/order_log.py` | netted 필드·commission_saved·cycle 요약 n_netted | 수정 |
| `local/localapp/gui.py` · `gui_format.py` | 넷팅 행 표시·절약액 | 수정 |
| `server/app/routers/trading.py` (+models) | cycle 요약 n_netted·commission_saved 수용 | 수정 |
| `local/tests/scenarios/test_netting_cycle.py` | MockBroker 종가·아침 cycle 넷팅 시나리오 | **신규** |

## 데이터 모델 (netting.py)

```python
@dataclass(frozen=True)
class Intent:
    sid: str                 # 원장 키(strategy:symbol[:idx])
    strategy_id: str
    strategy_name: str
    contract_key: str        # 물리 계약: 선물=contract_code, 주식=symbol
    symbol: str              # 상품명/종목코드(로그·참조가용)
    kind: str                # "entry" | "exit"
    position_side: str       # "long" | "short" — 이 주문이 열거나 닫는 포지션 방향
    order_side: str          # "buy" | "sell" (long-entry=buy, long-exit=sell, short-entry=sell, short-exit=buy)
    qty: int                 # 클램프(exit)·사이징(entry) 완료 수량
    ref_price: float
    entry_price: float | None  # exit일 때 실현손익용 진입가
    mult: float
    currency: str            # "KRW" | "USD"
    definition: dict         # entry일 때 원장 슬롯 생성용

@dataclass(frozen=True)
class NetResult:
    broker_orders: list[Intent]   # 잔여 — 기존 _submit_*로 실발주
    book_legs: list[Intent]       # 핸드오프 — 합성 체결로 원장 이관(수수료0)
    netted: list[dict]            # [{contract_key, symbol, position_side, netted_qty}]
```

`net_window(intents: list[Intent]) -> NetResult` — 순수. 커미션 KRW는 APPLY/REPORT에서 계산(순수성 유지).

---

## Phase 1 — 순수 넷팅 함수 (netting.py, TDD)

### Task 1.1: 모듈 + 데이터 타입 + no-op

**Files:** Create `local/localapp/netting.py`, `local/tests/test_netting.py`

- [ ] **Step 1: 실패 테스트 — 겹침 없으면 입력=출력(no-op)**

```python
# local/tests/test_netting.py
from localapp.netting import Intent, net_window

def _entry(sid, ck, side, qty, ref=100.0, mult=1.0):
    return Intent(sid=sid, strategy_id=sid, strategy_name=sid, contract_key=ck,
                  symbol=ck, kind="entry", position_side=side,
                  order_side=("buy" if side == "long" else "sell"),
                  qty=qty, ref_price=ref, entry_price=None, mult=mult,
                  currency="KRW", definition={})

def _exit(sid, ck, side, qty, entry=90.0, ref=100.0, mult=1.0):
    return Intent(sid=sid, strategy_id=sid, strategy_name=sid, contract_key=ck,
                  symbol=ck, kind="exit", position_side=side,
                  order_side=("sell" if side == "long" else "buy"),
                  qty=qty, ref_price=ref, entry_price=entry, mult=mult,
                  currency="KRW", definition={})

def test_no_overlap_is_noop():
    ins = [_entry("B", "K200", "long", 3)]
    r = net_window(ins)
    assert r.broker_orders == ins
    assert r.book_legs == []
    assert r.netted == []
```

- [ ] **Step 2: 실패 확인** — `pytest local/tests/test_netting.py::test_no_overlap_is_noop -v` → ImportError/FAIL
- [ ] **Step 3: 최소 구현** — Intent/NetResult dataclass + `net_window`가 그룹핑 없이 전부 broker_orders로 반환.
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋** `git add local/localapp/netting.py local/tests/test_netting.py && git commit -m "feat(netting): 순수 넷팅 함수 스켈레톤 + no-op"`

### Task 1.2: same-side open↔close 전량 상쇄

- [ ] **Step 1: 실패 테스트 — 하락일 종가: A 롱청산5 + B 롱진입5 → 브로커 0·book 2**

```python
def test_same_side_full_handoff():
    ins = [_exit("A", "K200", "long", 5, entry=90, ref=100),
           _entry("B", "K200", "long", 5, ref=100)]
    r = net_window(ins)
    assert r.broker_orders == []          # 순증분 0 → 실주문 없음
    assert len(r.book_legs) == 2          # A exit + B entry 합성체결
    assert {l.sid for l in r.book_legs} == {"A", "B"}
    assert r.netted == [{"contract_key": "K200", "symbol": "K200",
                          "position_side": "long", "netted_qty": 5}]
```

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** — `(contract_key, position_side)`별 그룹. opens=entry, closes=exit. `handoff=min(Σopen,Σclose)`. handoff>0이면 그 side의 entry/exit legs를 FIFO(strategy_id 정렬)로 handoff만큼 book_legs, 나머지 broker_orders. netted에 집계.
- [ ] **Step 4: 통과 확인**
- [ ] **Step 5: 커밋**

### Task 1.3: 수량 불일치 → 잔여 실주문

- [ ] **Step 1: 실패 테스트 — A 롱청산3 + B 롱진입5 → book 3+3, 잔여 진입 2**

```python
def test_qty_mismatch_residual():
    ins = [_exit("A", "K200", "long", 3, ref=100),
           _entry("B", "K200", "long", 5, ref=100)]
    r = net_window(ins)
    # 상쇄 3: A exit3(book) + B entry3(book). 잔여 B entry2 실주문.
    assert sum(l.qty for l in r.book_legs if l.kind == "exit") == 3
    assert sum(l.qty for l in r.book_legs if l.kind == "entry") == 3
    assert len(r.broker_orders) == 1
    assert r.broker_orders[0].sid == "B" and r.broker_orders[0].qty == 2
    assert r.netted[0]["netted_qty"] == 3
```

- [ ] **Step 2~4:** 구현(FIFO 분할: leg qty를 handoff 소진분=book, 초과분=broker_orders로 쪼갬 — Intent를 qty만 바꿔 복제) + 통과.
- [ ] **Step 5: 커밋**

### Task 1.4: 교차 side 절대 미상쇄 (§13.4)

- [ ] **Step 1: 실패 테스트 — 롱진입 vs 숏진입 / 롱청산 vs 숏청산 미상쇄**

```python
def test_cross_side_never_nets():
    # 교차 진입: 롱진입5 + 숏진입5 (같은 계약) → 상쇄 안 함, 둘 다 실주문
    ins = [_entry("X", "K200", "long", 5), _entry("Y", "K200", "short", 5)]
    r = net_window(ins)
    assert r.book_legs == [] and len(r.broker_orders) == 2 and r.netted == []
    # 교차 청산: 롱청산5 + 숏청산5 → 상쇄 안 함
    ins2 = [_exit("X", "K200", "long", 5), _exit("Y", "K200", "short", 5)]
    r2 = net_window(ins2)
    assert r2.book_legs == [] and len(r2.broker_orders) == 2 and r2.netted == []
```

- [ ] **Step 2~4:** (contract, position_side) 그룹이 교차 side를 자연히 분리하므로 통과해야 함(그룹 내 open·close 둘 다 있어야 handoff). 롱그룹=진입만(close 0)·숏그룹=진입만 → handoff 0. 확인.
- [ ] **Step 5: 커밋**

### Task 1.5: 다계약 독립 + contract_key(선물 만기물) (E6)

- [ ] **Step 1: 실패 테스트 — 다른 contract_key는 안 섞임**

```python
def test_different_contract_keys_isolated():
    # 같은 상품이라도 contract_key(계약코드)가 다르면 상쇄 안 함(롤 경계 방어)
    ins = [_exit("A", "A01606", "long", 5), _entry("B", "A01609", "long", 5)]
    r = net_window(ins)
    assert r.book_legs == [] and len(r.broker_orders) == 2 and r.netted == []
```

- [ ] **Step 2~4:** 그룹 키=contract_key라 자연 통과. 확인.
- [ ] **Step 5: 커밋**

### Task 1.6: 결정론 (FIFO strategy_id) + 숏 side 핸드오프

- [ ] **Step 1: 실패 테스트 — 숏 핸드오프 + FIFO 결정론**

```python
def test_short_side_handoff_and_fifo():
    # 숏청산(buy) + 숏진입(sell) 상쇄
    ins = [_exit("Z", "K200", "short", 4, entry=110, ref=100),
           _entry("W", "K200", "short", 4, ref=100)]
    r = net_window(ins)
    assert r.broker_orders == [] and len(r.book_legs) == 2
    # 다중 청산 FIFO: 청산 A2, C2 + 진입 B3 → handoff3=A2+C1(strategy_id 정렬), 잔여 C1 실청산
    ins2 = [_exit("A", "K200", "long", 2, ref=100), _exit("C", "K200", "long", 2, ref=100),
            _entry("B", "K200", "long", 3, ref=100)]
    r2 = net_window(ins2)
    booked_exit = sorted((l.sid, l.qty) for l in r2.book_legs if l.kind == "exit")
    assert booked_exit == [("A", 2), ("C", 1)]      # FIFO by strategy_id
    assert [(o.sid, o.qty) for o in r2.broker_orders] == [("C", 1)]
```

- [ ] **Step 2~4:** exit legs를 strategy_id 정렬 후 FIFO 소진. 통과.
- [ ] **Step 5: 커밋**

---

## Phase 2 — APPLY: 합성 체결 (trader.py)

### Task 2.1: `_apply_netted_leg` — 기존 _apply_fill 경로 재사용

**Files:** Modify `local/localapp/trader.py` (신규 메서드), `local/tests/scenarios/test_netting_cycle.py`

책임: book_leg 1건을 **합성 체결**로 원장 반영. 기존 `_apply_fill`의 ledger/realized/통화/side 분기를 재사용하되 브로커 미호출·수수료 0.
- exit leg: A 슬롯 축소 + realized(`(ref−entry)×qty×부호×mult`) trades.jsonl 기록.
- entry leg: B 슬롯 생성/증가(entry_price=ref, peak=ref, definition=B).
- 공통: `order_log.log_order(event="filled", ..., kind, extra={"netted": True, "commission_saved": ...})`.
- **slippage skip:** `order_log.log_order`가 `extra.netted`면 `record_slippage` 건너뜀(N2).
- **intent seed:** `intents.begin(...)` + `intents.mark_submitted(order_no="NETTED-"+id)` → `is_active` 게이트가 재실행 차단(N1).
- **락:** 호출부가 `_CYCLE_LOCK` 보유·전체 book_legs 한 임계구역·원자 저장(N6).

- [ ] **Step 1: 실패 시나리오 — 하락일 핸드오프, 브로커 주문 0·원장 이관·realized 정확**

```python
# test_netting_cycle.py (MockBroker)
def test_close_handoff_zero_broker_orders(mock_trader):
    # A 롱 5 @90 보유(day_trade), B 오버나이트 롱 후보 5, 현재가 100
    t = mock_trader(ledger={"A:K200": _pos("K200","long",5,90)}, price={"K200":100})
    net = t.run_close_netting(...)          # 종가 PLAN-NET-APPLY
    assert t.broker.order_calls == []       # 브로커 실주문 0
    assert t.ledger["A:K200"] is absent     # A 청산됨
    assert t.ledger["B:K200"].qty == 5 and t.ledger["B:K200"].entry_price == 100
    assert net["commission_saved_krw"] > 0
    # realized: A (100-90)*5*mult 기록
```

- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현** `_apply_netted_leg` + order_log netted skip + intent seed.
- [ ] **Step 4: 통과 확인** + 기존 trader 테스트 회귀 없음.
- [ ] **Step 5: 커밋**

### Task 2.2: contract_key 해석 (선물 계약코드)

- [ ] **Step 1: 실패 테스트** — 선물 Intent.contract_key = `_record_contract_meta`가 쓰는 실제 계약코드; 주식 = symbol.
- [ ] **Step 2~4:** `contract_key_of(symbol, ledger_meta)` 헬퍼 — 선물이면 원장의 contract_code(없으면 브로커 해석), 주식이면 symbol. 통과.
- [ ] **Step 5: 커밋**

---

## Phase 3 — PLAN: 의도 산출 (trader.py)

### Task 3.1: 청산 의도 산출 (submit 분리)

책임: `liquidate_day_trades`의 "무엇을 청산할지" 결정(클램프 포함)을 **Intent 리스트 반환**으로 분리(발주 안 함). 기존 submit 경로는 잔여 발주에 재사용.

- [ ] **Step 1: 실패 테스트** — `plan_liquidations(dataset, instrument_class, market)` 가 hold_days==0·클램프된 Intent(kind=exit, position_side, contract_key, entry_price, mult) 반환. 외부 수동매도(실보유<원장)면 클램프 수량으로(E4).
- [ ] **Step 2~4:** 기존 루프(1539~1587)에서 submit 대신 Intent 생성. 통과.
- [ ] **Step 5: 커밋**

### Task 3.2: 진입 의도 산출 + 로컬 여력원장 (E1·E2·N7·N9)

책임: `_enter_from_preview`의 사이징을 **단일 스냅샷 + 로컬 여력원장**으로 — live 재조회 대신 스냅샷 시드 + **같은 contract 청산 회수여력 크레딧(클램프 수량만)** + 진입마다 차감. 게이트(킬스위치·drawdown) 적용. Intent(kind=entry) 반환.

- [ ] **Step 1: 실패 테스트** — 하락일: A 청산 5(선물)면 B 진입 사이징의 유효여력 = live_orderable + 5. 청산 없으면 = live_orderable(현행 동일). 다중 진입 순차 차감.
- [ ] **Step 2~4:** `plan_entries(by_strategy, strategies, dataset, liquidation_intents, ...)`. 여력원장=`{contract_key: live_orderable + Σ클램프청산}`, 진입마다 `-= qty`. 게이트 통과분만 Intent. 통과.
- [ ] **Step 5: 커밋**

---

## Phase 4 — 배선: PLAN-NET-APPLY (runner.py)

### Task 4.1: run_close_cycle → PLAN-NET-APPLY

- [ ] **Step 1: 실패 시나리오** — 종가 하락일 넷팅 발생·브로커 0·요약 n_netted=1; 상승일(동방향) 넷팅 0·현행과 동일 발주; 넷팅 대상 없는 날 현행 결과 동일.
- [ ] **Step 2~4:** `plan_liquidations` + `plan_entries` → `net_window` → book_legs `_apply_netted_leg`(락 내) → broker_orders 기존 `_submit_*`(청산 먼저). 요약 병합에 n_netted·commission_saved. 통과.
- [ ] **Step 5: 커밋**

### Task 4.2: 아침 _cycle_body → PLAN-NET-APPLY + 골든 보존

- [ ] **Step 1: 실패 시나리오** — 아침 하락일: B 오버나이트 청산(시가매도) + A 당일 진입(시가매수) 넷팅. **무넷팅 시 기존 골든 byte-identical**.
- [ ] **Step 2~4:** §2 청산·§3 진입을 PLAN-NET-APPLY로. no-op 경로가 현행과 동일한지 골든/스냅샷으로 실증(catch-up은 넷팅 제외 — E9). 통과.
- [ ] **Step 5: 커밋**

---

## Phase 5 — 투명성 (REPORT)

### Task 5.1: order_log·cycle 요약 netted 필드
- [ ] netted(bool)·commission_saved 필드 + cycle 요약 n_netted·commission_saved_krw(실거래수와 분리 카운트 — N3). 단위 테스트. 커밋.

### Task 5.2: 로컬앱 GUI 넷팅 표시
- [ ] gui_format: 넷팅 행 라벨(`넷팅 · 청산 · 수수료 X원 절약`). gui 주문내역/요약. 단위 테스트(gui_format 순수부). 커밋.

### Task 5.3: 서버 집계 + 웹 트레이딩 탭
- [ ] server/trading.py cycle 요약 n_netted·commission_saved 수용(집계만·보안 §4). 웹 트레이딩 탭 표시(담당 확인 후). 서버 테스트. 커밋.

---

## 최종 검증
- [ ] `pytest local/tests -q` 전체 green (netting 단위 + 시나리오).
- [ ] 골든: 넷팅 대상 없는 아침 cycle 결과 현행과 동일.
- [ ] 코드 리뷰(§12 N1~N11·E1~E10 각 대응 확인).
- [ ] ⚠ 자금 실검증(유효여력 근사·상계증거금)은 모의계좌 라이브 단계 — 설계단계 검증 불가로 명시.

# 국내 선물 라이브 사이징 — 브로커-주문가능 1차 기준 (모델 A)

**날짜:** 2026-06-30 · **브랜치:** `feat/futures-orderable-clamp` (origin/main 4c6c260)

## 목표 (한 줄)
국내 선물 라이브 사이징의 **계약수를 "추정 증거금률(카탈로그 0.10)"이 아니라 "브로커가 알려주는 실제
주문가능수량"에서 도출**한다. 유저는 *위험 크기(사용률)*만 정하고, *계약당 증거금률*은 100% 브로커가 정한다.
→ **라이브 계약수 = floor(사용률% × 브로커_주문가능수량).** (사장님 결정: 모델 A.)

## 용어 (혼동 방지 — 설계의 핵심)
| | 무엇 | 누가 |
|---|---|---|
| **증거금 사용률** `futures_margin_pct`(기본 20%) | 위험 크기 — 한도의 몇 %를 쓸지 | **유저**(빌더) |
| **증거금률** `init_margin_rate`(0.10) | 계약당 명목 대비 증거금 비율 | 카탈로그(유저 못 정함) |

유저는 *지금도* 증거금률을 못 정한다. 문제는 카탈로그 0.10이 브로커 실제(~19.5%·동적)와 달라
계약수를 ~2배 과다 산정한 것. **모델 A는 라이브에서 그 추정 률을 제거**한다.

## 모델 A — 라이브 사이징 (국내 선물)
브로커 주문가능수량(KIS `ord_psbl_qty` / LS `NewOrdAbleQty`)은 **이미 브로커의 실제·동적 증거금률로
계산된 "최대 신규 계약수"**. 유저 사용률로 그 한도를 스케일:

```
orderable = broker.orderable_qty(symbol, side, price)     # 브로커 실제 률 반영 최대 계약수
if orderable is not None:
    if 사용률(%) 모드:  qty = floor(orderable × 사용률/100)   # ★ 모델 A — 1차 기준
    else(fixed_amount): qty = min(event_buy_qty(...), orderable)  # 기존 + 안전 상한
else:               qty = event_buy_qty(...)              # 조회 불가 → 카탈로그 degrade(현행)
```

- **항상 ≤ 브로커 한도** → 과발주·과레버리지 구조적 불가. 사용률이 *말 그대로*가 됨(20%→실제 20%).
- 변동성으로 브로커 률↑ → orderable↓ → **자동 추종**. 카탈로그 0.10이 틀려도 라이브 무관.
- "tolerance"는 사용률 자체 — (100%−사용률)=남기는 증거금 버퍼. 별도 필드 불필요.
- `side`: 진입 방향(롱=buy/숏=sell)을 그대로 전달(S&P500 역추종 숏 진입 = sell-side 주문가능).

## 무결성 3계층 / 이미 보장된 안전
| 계층 | 시점 | 본 작업 |
|---|---|---|
| 구조 | 빌더 | ✅ capability 게이트(완료) |
| **실행** | **발주 직전** | **★ 모델 A (본 작업)** |
| 사전 | 계좌 적용 | Phase 2 후보(미리보기) |

> 두 브로커 발주 API가 증거금 부족 시 주문을 **거부**(KIS EGW·LS OrdNo 부재) — 위반 발주 차단은 이미
> 발주 시점 보장. 모델 A는 그 위에서 *애초에 정확히* 사이징해 거부를 없애고 사용률을 정직하게 만든다.
> **조회 실패해도 과발주는 브로커가 막는다**(degrade fail-safe).

## 변경 파일 — Phase 1 (라이브, 본 작업)

### 1. `kis_futures_broker.py` — `orderable_qty(code, side, price) -> int`
- TR `VTTO5105R`(모의)/`TTTO5105R`(실전), GET `/uapi/domestic-futureoption/v1/trading/inquire-psbl-order`.
- **검증된 요청(raw 정본 `국내선물옵션_주문_계좌.xlsx` "선물옵션 주문가능" 시트):** `CANO`, `ACNT_PRDT_CD`,
  `PDNO`=code(선물 6자리), `SLL_BUY_DVSN_CD`("02"매수/"01"매도), `UNIT_PRICE`=str(price)("0"이면 기준가),
  `ORD_DVSN_CD`="01"(지정가). _read_get(idempotent READ 재시도) 사용.
- **검증된 응답:** `output`(단일 object)`["ord_psbl_qty"]` = **주문가능수량(계약수·string)**. 정본 예시
  `"ord_psbl_qty": "11665"` — KIS가 실제 동적 증거금률로 계산한 최대 신규 계약수. `int(float(...))` 파싱.
  (그 외 tot_psbl_qty·lqd_psbl_qty1·bass_idx — 미사용.) ⚠ output이 array로 와도 방어(첫 원소).
- ✅ **PDNO 형식 확정:** resolved code(예 `A01606`, 6자리)를 그대로 PDNO에. 주문 TR(SHTN_PDNO)과 동일 코드 —
  브로커 주석 16~19행 라이브 검증(`A01606→주문가능`, `1A01606→조회실패`). 미니(A05xxx)만 모의서 확정(degrade fail-safe).

### 2. `ls_futures_broker.py` — `orderable_qty(code, side, price) -> int`
- TR `CFOAQ10100`, `/futureoption/accno`. **기존 `_orderable_amt_krw`(line 62~79)가 이 TR을 모의 실측**
  (2026-06-23 OrdAbleAmt=5억)했으므로 그 검증된 InBlock을 재사용하고 **읽는 필드만 교체**한다.
- **InBlock(검증된 형태 재사용):** RecCnt=1, QryTp="1", OrdAmt=0, RatVal=0.0, `FnoIsuNo`=code,
  `BnsTpCode`("2"매수/"1"매도 ← side), `FnoOrdPrc`=**float(price)**(수량은 가격 의존 — 0.0 대신 실가격),
  FnoOrdprcPtnCode="00"(지정가).
- **응답:** `CFOAQ10100OutBlock2["NewOrdAbleQty"]`(신규 주문가능수량·string→int). 정본=domestic-futures-research.md
  19·35행("orderable_qty = CFOAQ10100 NewOrdAbleQty"). `_orderable_amt_krw`가 읽는 `OrdAbleAmt`(금액)와 다른 키.
- 조회실패→raise(호출자 degrade). ⚠ FnoIsuNo 형식은 기존 t8467 미검증과 동일(모의 확정). `_pick_front_kospi200`
  헬퍼는 _orderable_amt_krw 전용(계좌단위 금액 조회)이라 여기선 불필요 — code를 인자로 직접 받는다.

### 3. `broker_router.py` — `orderable_qty(symbol, side, price) -> int | None`
국내 선물만(CME·주식=None). `_broker(symbol)`·`_code(symbol)`로 라우팅. 브로커가 메서드 없으면 None(degrade).

### 4. 모델 A 사이징 — 순수 헬퍼(core) + trader 분기(local)
**4a. `core/quant_core/ir_engine/live.py` — 순수 헬퍼 2개(event_buy_qty 옆, core에서 단위테스트)**
- `futures_margin_pct_of(strategy) -> float|None`: `event_buy_qty`와 **동일 모드 판정**(단일출처).
  `sz = strategy.position.sizing`; `if sz.mode == "fixed_amount" and sz.amount_krw: return None`(금액 기준)
  `else: return float(sz.futures_margin_pct)`. 테스트: % 모드→20.0(기본)·custom, fixed_amount→None.
- `model_a_qty(event_qty, orderable, pct) -> int`: 모델 A 라이브 계약수(순수 산술).
  `if pct is not None: return min(orderable, int(orderable * pct / 100.0))`(% 모드; **min 캡으로 "항상 ≤
  브로커 한도" 불변식 보장** — 사용률>100 방어) `else: return min(event_qty, orderable)`(fixed_amount 안전 상한).
  테스트: (7,20→1)·(orderable=5,pct=100→5)·(pct=150 캡→orderable)·(fixed: event<orderable→event, event>orderable→orderable)·(orderable=0→0).

**4b. `local/localapp/trader.py` — 국내 선물 라이브 사이징을 모델 A로 (event_buy_qty 직후 분기)**
- `_ir = StrategyIR.model_validate(strat_def)` 1회 생성해 event_buy_qty·futures_margin_pct_of 공유.
- 분기 조건: `qc.is_futures(symbol) and futures_market(symbol) != "CME"`(국내선물만). `side = "sell" if is_short else "buy"`.
- **`getattr(self.broker, "orderable_qty", None)` 가드**로 sim/paper(메서드 없음→event_buy_qty 유지·degrade 아님)와
  live(router→모델A)를 분리 — 기존 SimBroker 시나리오는 블록 건너뛰어 **byte-identical**(회귀 0).
- 메서드 있음(router): `orderable = oq_fn(symbol, side, prev_close)`. `None`(조회 실패)→`log.warning`로 degrade 사유 표면화·event_buy_qty 유지. 정수→`pct = futures_margin_pct_of(_ir)`; `qty = model_a_qty(qty, orderable, pct)`; `log.info`로 "주문가능 N → qty계약" 표면화.
- 사유 표면화는 **Python 로거**(`log.info`/`log.warning`, §9 localapp.log) — `order_log.decision` action 어휘(bought/skip_*/error 닫힘)는 미오염(시나리오 테스트 보존). qty==0(증거금 부족)→ 기존 `qty<=0 → skip_funds`(현행)가 처리.
- ⚠ init_margin_rate 0.10은 라이브 event_buy_qty 경로에서 **더는 결과를 좌우하지 않음**(% 모드는 orderable이 1차 기준).
- 검증: core 헬퍼 단위테스트 + trader 시나리오 1건(broker에 orderable_qty 주입→발주 qty가 모델A 반영, test_futures_sim.py 템플릿) + 기존 671 green 유지.

### 5. 테스트 (mock 브로커·자금 0)
- %모드: orderable 5·사용률 20% → 1계약 / 100% → 5계약 / orderable 0 → 0(보류).
- fixed_amount: event_buy_qty 값 > orderable → 클램프; ≤ → 유지.
- 조회 None/예외 → event_buy_qty 값 유지(degrade) + 로그.
- side buy/sell → 구분코드 매핑(KIS 02/01·LS 2/1).
- 라우터: 국내만 호출, CME·주식 None.

## Phase 1.5 (후속 — 별도, ripple 주의)
**카탈로그 `init_margin_rate` 0.10 → 실측(~19.5%) 교정** — 라이브는 모델 A라 무관하지만:
- **백테스트** 레버리지 현실화(브로커 조회 불가하니 률 필요) + **체결 레버리지 표시**(1/rate) 정확화.
- ⚠ **기존 선물 백테스트 결과·여러 테스트 기대값이 바뀜**(정규 코스피200·미니·CME 전부). 정확값·범위는
  별도 확인 후 신중히. 라이브 안전과 분리된 "현실성" 개선이라 Phase 1 후 결정.

## Phase 2 후보
- 사전 미리보기(계좌 적용 시 "0계약·자금부족" 경고) · 빌더 사용률 라벨/툴팁을 "브로커 한도의 %"로 보강 ·
  해외선물(CME) orderable(별도 overseas 경로) · fixed_amount 선물의 브로커-금액 변환(KIS는 orderable에 금액 미반환).

## 검증
- 단위: 위 mock 테스트. 정규 무손상(기존 trader/broker/router 테스트 green).
- ⚠ 실측(내일 모의): KIS PDNO 형식·LS FnoIsuNo 형식·응답 필드 실제값. 어느 쪽이든 과발주는 브로커가 막아 자금 안전.

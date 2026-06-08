# 선물 자동매매 프로그램 — Trader 배선·배포·릴리즈 마스터 플랜 (M1~M10)

> 작성 2026-06-08. 선행 설계 `2026-06-08-futures-auto-trading-design.md`(PR #28) 위에 올린
> **확정 실행 계획**. P0(테스트베드)·P1(국내브로커)·P2(해외브로커) 완료 후, 이를 실제
> Trader 자동 루프에 배선하고 프로덕션 배포·로컬앱 릴리즈까지 가는 단일 진실원천.

## 1. 최종목표
국내(KRX)+해외(CME via KIS) **선물 자동매매**를 Broker→Trader **배선**하여, **프로덕션
배포**·**로컬앱 릴리즈**까지 완성한다. 사용자가 전략연구소에서 만든 선물 전략(롱/숏)을
실거래 자동매매로 돌릴 수 있게 한다. 4계층(노코드 UI·백테스트·데이터·**자동매매**) 중
**#4 자동매매를 선물로 확장**하는 작업. 분석·백테스트·데이터는 이미 선물 지원.

## 2. 현황 (완료분)
| 단계 | 내용 | 머지 | 라이브 검증 |
|---|---|---|---|
| P0 | SimBroker 선물화(증거금·롱숏·승수·정산·INV-FUT) | PR #30 | — (sim) |
| P1 | 국내선물 브로커(지정가·시장가·취소·체결조회) | PR #35 | 모의 토큰/잔고/시세 ✓, **주문 라운드트립=다음 국장** |
| P2 | 해외선물 브로커(주문·잔고·시세, 통합 라우팅) | PR #34 | **실전 토큰/잔고 read-only ✓**(계좌 47272165-08), 시세=COMEX 유료구독, **주문=실거래 위임** |

핵심: `event_buy_qty`(live.py)는 이미 선물 계약수 사이징 분기 보유(변경 불필요). 브로커
표면(주문·잔고·시세·취소·체결조회)은 국내·해외 모두 구현·단위검증됨.

## 3. 확정 결정 (사용자 align 2026-06-08)
1. **방향성 = 롱/숏 전폭.** IR/백테스트는 이미 short 지원(`PositionSpec.direction`, signed qty,
   마진콜·차입비). 라이브 경로(event_buy_qty·_submit_buy·ledger·reconcile)에 short 배선 추가.
2. **만기/롤오버 모델 = ①Exit청산 + ②만기 자동청산 (no-roll).**
   - ① **사용자 설정 청산 규칙(Exit)** = 이미 완전 배선(백테스트·라이브·UI). 선물 동일 적용. **추가구현 0.**
   - ② **만기 자동청산** = 신규(일수기반 강제청산). `expiry_rule` 카탈로그 존재, 엔진 로직 0%.
   - 진짜 롤오버(차월 재진입)는 **v1 제외**(사용자 모델은 "청산"이지 "재진입" 아님). E2 후속.
3. **출시 라벨 = 국내+해외 동시 정식.** (해외는 모의 미지원→라이브 미검증 잔존 리스크 R1 수용)
4. **웹/서버 표시 = 필수 교정 + 최소 표기.** portfolio 평가금액 승수 단위버그 교정(정확성 필수)
   + 포지션 카드 계약수·롱숏 최소표기. 풀 증거금 대시보드는 후속.

## 4. 4계층 검증으로 도출한 발견 (근거)
- **F1 (라이브 승격 게이트가 선물 차단):** 선물 전략 생성·저장·백테스트는 가능하나, 모의/실전
  승격 시 `server/app/symbols.py::tradable_symbols()`가 **KIS 현물 마스터에만 의존**해 선물
  거부(선물 키는 한글 상품명이라 마스터에 없음). 게이트: `server/app/routers/strategies.py`.
- **F2 (라이브는 롱온리):** `PositionSpec.direction`(spec.py)·engine.py 588-914는 short 지원하나,
  라이브 `live.py::event_buy_qty`(양수만)·`trader.py::_submit_buy`(매수전용)·ledger(qty≥0)는 롱온리.
- **F3 (연속가격↔특정계약 갭, 최대 난제):** 백테스트=연속 시계열(yfinance `CL=F`·KOSPI200 CSV),
  라이브=특정 만기 계약(A01606·GCZ25). **심볼→front-month 매핑 전무** → trader가 한글 상품명을
  그대로 KIS에 보내 거절. `expiry_rule`/`default_roll`(exec_defaults) 정의됐으나 **미배선(사용처 0)**.
- **F4 (웹/서버 표시 미비 + 단위버그):** payload는 raw JSON이라 필드 통과하나, KisFuturesBroker
  미통합으로 선물 포지션이 snapshot에 안 들어감. `server/app/routers/portfolio.py`의 평가금액
  =`qty×eval_price`로 **승수 미적용(1계약을 1주처럼 계산)**. 웹은 주식 가정.
- **F5 (긍정):** `event_buy_qty`(live.py:78-81) 선물 계약수 사이징 정상. 변경 불필요.

## 5. 마일스톤 (P0·P1·P2 완료 위에)
| 단계 | M | 마일스톤 | 핵심 작업 | 건드리는 파일 | 위험 |
|---|---|---|---|---|---|
| 1·기반 | M2 | **심볼→계약 해석 + market 라우팅** | 한글상품명→(asset_class, market, **front-month 계약코드**). 국내 A01606·해외 globex. 브로커 마스터(fo_idx_code.mst / 상품기본정보 HHDFC55200000) 대조 | `exec_defaults.py`·`market_index.py`·신규 resolver | 🟡(R3) |
| 2·통합 | M3 | **BrokerRouter** | KisBroker+KisFuturesBroker를 Broker Protocol로 감싸 symbol 라우팅. `runner.make_broker` 라우터화. KisFuturesBroker `buy_resv_limit`/`sell_resv_moo` 보강(국내 no-op/해외 위임) | `runner.py`·`broker.py`·`kis_futures_broker.py` | 🟢 주식 무영향 |
| 3·머니패스(롱숏) | M4 | **Ledger 선물화 + 정산 + signed qty** | ledger에 `asset_class·side·multiplier·init_margin_rate`. `_apply_fill` side 분기. 정산손익 `(exit−entry)×qty×승수×부호`. `event_buy_qty` direction | `trader.py`·`live.py` | 🔴 공유 머니패스 |
| | M5 | **숏 진입/청산 + reconcile 숏** | 매도진입(sell-to-open) 라이브 경로. reconcile `(symbol,side)` 매칭·외부 숏청산 감지. `enrich_positions`/`held_qty` side 분기 | `trader.py`·`analytics.py` | 🔴 |
| 4·만기 | M6 | **만기 자동청산(②)** | 만기일 계산(`expiry_rule` 소비)+`days_before_expiry` 강제청산. ①Exit은 기존 동작 | `live.py`·`trader.py`·`exec_defaults.py` | 🟡 |
| 5·검증 | M7 | **SimBroker E2E 통합검증** | 롱·숏·국내·해외·만기청산 풀 시나리오 + 주식 골든 회귀 0 | 테스트만 | 🟢 |
| | M8 | **국내 라이브 라운드트립** | 다음 국장 모의 A01606 시장가 buy→체결→populated 잔고→sell flatten(+가능시 실전 소액) | — | 🟡 외부 |
| 6·배포 | **M1** | **라이브 승격 게이트 개방** | `tradable_symbols()`에 선물 허용(`is_futures`). **⚠ 안전상 맨 마지막 — main 자동배포라 실행 배선 완료 후 개방** | `server/app/symbols.py` | 🟢 서버 |
| | M9 | **배포 + 웹 선물표시 + 릴리즈** | portfolio 승수 교정(F4)+웹 계약/롱숏 최소표기+로컬앱 version bump·CI 태그→quantman-releases·Railway/Vercel | `portfolio.py`·`web/`·로컬앱·CI | 🟡 |
| | M10 | **해외 첫 실거래 검증(사후)** | 사용자 위임(모의 없음) | — | 🟡 |

### 의존 그래프
```
M2 → M3 → {M4 → M5} → M6 → M7 ─┐
                                ├→ (M1 게이트 + M9 배포) → M10
                  M8(국내라이브)─┘
```
M8은 M3 후 국장 시점에 병행. M1은 의존 단계 없음 → **안전상 M9 릴리즈에 묶어 맨 마지막**.

## 6. 머지/배포 안전 전략 (중요)
- main은 Railway(서버)+Vercel(웹) **자동배포**. → 절반만 된 선물 경로를 프로덕션에 노출 금지.
- **M2~M8은 휴면 상태로 안전 머지 가능:** 선물 심볼이 라이브 Trader에 도달하려면 M1(승격 게이트)이
  열려야 하는데, M1이 닫혀 있으면 사용자가 선물 전략을 라이브 승격할 수 없다 → M2~M8의 선물 코드
  경로는 활성화되지 않음(주식만 동작). 테스트는 게이트 무관(픽스처·직접 호출).
- **M1(게이트 개방)을 M9 릴리즈와 함께 맨 마지막에** → 실행 배선·검증이 끝난 뒤에만 사용자에게 노출.
- 마일스톤별 branch+PR, 작업 전 `origin/main` pull. main 직접 push 금지. 머지 후 알림.

## 7. 잔존 리스크
- **R1 (해외 숏 라이브 미검증):** "동시 정식"+"롱/숏"+"해외 모의 없음" 조합 최고위험. 해외 숏 주문
  경로는 SimBroker E2E + read-only 실전 + 스펙으로만 검증, **실주문 라이브 검증 불가**. 완화: 해외
  첫 실거래 최소계약·보수적 사이징, M9에 "해외 라이브 미검증" 내부 표기.
- **R2 (백테스트↔라이브 만기 불일치):** 백테스트는 연속물을 만기 가로질러 보유, 라이브 v1은 ②만기
  자동청산으로 만기에 끊김. 단기·스윙 전략은 영향 미미, **다월 장기보유 전략은 발산** → 출시 시 고지.
- **R3 (front-month 코드 생성, M2의 외부 미지수):** 국내 만기월 인코딩(A01606)·해외 globex 월코드
  (Z=12월 등). 해결 = 브로커 마스터 런타임 조회(fo_idx_code.mst / 상품기본정보 HHDFC55200000)로
  활성 front-month 선택. M2 착수 시 라이브 대조.

## 8. 핵심 코드 참조 (executor용)
- 승격 게이트: `server/app/symbols.py::tradable_symbols()`, `server/app/routers/strategies.py`
- 사이징: `core/quant_core/ir_engine/live.py::event_buy_qty()` (선물 분기 :78-81, 변경 불필요)
- 청산(①, 이미 동작): `spec.py::Exit`(:83-96), `engine.py::price_exit_reason/exit_reason`,
  `live.py::cycle_exit_reason/intraday_exit_reason`
- 계약 카탈로그: `core/quant_core/exec_defaults.py::_INSTRUMENTS`(:122-133)·`instrument_spec`·`is_futures`·
  `expiry_rule`/`default_roll`(예약·미소비)
- Trader ledger: `local/localapp/trader.py` ledger 생성(:381-387, side/multiplier 없음)·`_apply_fill`·`_submit_buy`·`_submit_sell`
- reconcile: `local/localapp/analytics.py::reconcile_ledger`(symbol-only, qty>0)·`trader.py::reconcile_with_kis`
- 브로커 라우팅: `local/localapp/runner.py::make_broker()`·`broker.py` Protocol·`kis_futures_broker.py`(국내+해외 통합)
- 웹/서버 표시: `server/app/routers/portfolio.py`(평가금액 승수버그)·`web/src/components/MonitorCards.tsx`·`local/localapp/runner.py` snapshot payload

## 9. 검증 기준 (Definition of Done)
- 각 마일스톤: 단위 TDD green + 전체 회귀(주식 골든 무변경) + (해당 시) SimBroker E2E.
- 출시(M9): SimBroker E2E(롱·숏·국내·해외·만기청산) + 국내 모의 라운드트립(M8) + read-only 실전(해외).
- 해외 실주문은 M10(사용자 첫 실거래)로 위임 — "정식" 라벨이나 라이브 미검증 잔존(R1) 내부 표기.

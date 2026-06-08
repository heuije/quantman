# 선물 자동매매 (국내 + 해외) — 설계

> Date: 2026-06-08 · Status: 설계(승인 대기) · Owner: 선물 #4 계층
> 관련: [futures-ir-design](../../futures-ir-design.md) · [INVARIANTS](../../INVARIANTS.md) ·
> [ORDER_STATE_MACHINE](../../ORDER_STATE_MACHINE.md) · [Trading Workbench](2026-05-30-trading-workbench-design.md) ·
> [docs/kis-api](../../kis-api/INDEX.md)

## 1. 목표

국내(KRX)·해외(CME via KIS) **선물 자동매매**를 Broker → Trader 배선까지 완성하고,
**프로덕션 배포 + 로컬앱 릴리즈**로 실제 자동매매를 지원한다. 4계층 호환 계약
(노코드 UI·백테스트·데이터·자동매매) 중 **#4(자동매매)** 계층을 선물로 확장한다.

분석·백테스트(글로벌6종 + KOSPI200)는 이미 동작하며 본 설계는 그 위에 거래 실행만 얹는다.

## 2. 범위

**IN**
- 통합 `KisFuturesBroker`: 국내 + 해외 선물 거래(주문·잔고·시세), market 라우팅.
- 테스트베드 선물화: SimBroker + Broker 프로토콜 + Trader 사이징/리스크가 선물(증거금·계약·롱숏·승수·정산)을 인지.
- Trader 배선: 전략 신호 → 선물 주문(market 라우팅·증거금 사이징·롱숏·포지션 reconcile).
- 배포·릴리즈: 서버/웹 변경(최소) + 로컬앱 CI 빌드 → 릴리즈.

**OUT (이번 아님)**
- 옵션 거래(선물만). 야간선물(KRX야간·모의 일부 미지원).
- 새 전략 타입(기존 전략 IR 재사용). 자동 롤오버(만기 알림만, 수동 롤).

## 3. 핵심 제약 (검증으로 확정한 사실)

1. **🔴 해외선물 모의투자 미지원 — 실전 전용.** KIS 해외선물옵션 전 API의 모의 TR_ID·도메인이
   "모의투자 미지원". → 해외 주문 라이브검증 불가. **SimBroker가 유일한 안전 테스트 환경.**
2. **SimBroker는 주식 모델.** 잔고=cash/eval/fx, 포지션={symbol,qty,avg_price}(암묵 롱). 선물
   개념(증거금·계약수·숏·승수·정산 손익) 부재 → **P0에서 확장**.
3. **선물 자격증명은 product별 별도.** 국내선물 키/계좌(상품코드 03) ≠ 주식 키, 해외선물(상품코드 08)은 또 별도.
   모의/실전은 *라우팅 아님* — 각 자격증명 슬롯의 `virtual` 플래그(도메인·TR 접두만 가름).
4. **국내 vs 해외 API 전면 상이.** 공유는 OAuth(~30줄)뿐.

| 항목 | 국내선물 | 해외선물 |
|---|---|---|
| 모의투자 | ✅ 있음 | ❌ 없음(실전만) |
| 주문 TR / path | VTTO1101U / domestic-futureoption/order | OTFM3001U(실전) / overseas-futureoption/order |
| 심볼 | 6자 단축(`A01606`) `SHTN_PDNO` | CME globex(`GCZ25`) `OVRS_FUTR_FX_PDNO` |
| 주문 가격구분 | `ORD_DVSN_CD`(01지정/02시장) | `PRIC_DVSN_CD`(1지정/2시장/3STOP) + `CCLD_CNDT_CD` |
| 잔고 TR | VTFO6118R(컬럼형 dict-of-arrays) | OTFM1412R inquire-unpd(**행형 array**) |
| 잔고 포지션 키 | shtn_pdno·cblc_qty·sll_buy_dvsn_name·ccld_avg_unpr1·excc_unpr·evlu_pfls_amt | ovrs_futr_fx_pdno·fm_ustl_qty·sll_buy_dvsn_cd·fm_ccld_avg_pric·fm_now_pric·fm_evlu_pfls_amt·crcy_cd |
| 계좌상품코드 | 03 | 08 |
| 통화 | KRW | USD (+FX 환산) |
| 시세 TR | FHMIF10000000 inquire-price(output1) | 해외선물종목현재가(별도 TR) |

## 4. 아키텍처

통합 `KisFuturesBroker`(국내선물 모듈 확장) — KisBroker가 주식 국내+해외를 한 곳에서 다루는 방식과 동일.

```
KisFuturesBroker
 ├─ ctx[domestic] = _MarketCtx(키·계좌·virtual·토큰, base=domestic-futureoption, KRW)   # load_kis_futures()
 ├─ ctx[overseas] = _MarketCtx(키·계좌·virtual·토큰, base=overseas-futureoption, USD)   # load_kis_overseas_futures() [신규]
 │     · 있는 슬롯만 활성(graceful). virtual=모의/실전(해외는 실전 고정).
 ├─ 공유: _json(UTF-8)·토큰 발급·_headers 골격·레이트리밋 재시도(EGW00201)
 ├─ 시장별 순수함수(단위검증): build_*_order_body / parse_*_balance / *_quote_fields
 └─ 라우팅: symbol → market (레지스트리). 글로벌6종=overseas, KOSPI200=domestic.
```

- **심볼 매핑 레지스트리**(작은 config): 내부 심볼("gold"/"oil"…) → {market, KIS 코드}. 국내는 `A01606`류,
  해외는 CME globex(`GCZ25`·`CLF26`…) 근월물. 근월물 코드 산출은 마스터/상품기본정보 기반.
- **순수함수 우선**: 주문 바디 빌드·잔고 파싱을 시장별 순수함수로 분리 → 네트워크 없이 단위검증
  (국내선물에서 검증된 패턴). 해외 잔고는 행형 array라 국내 `_balance_rows`와 별도 파서.
- **국내 부분은 이미 검증된 현 코드 유지**(PR #22·#26). 해외 메서드만 추가 + 라우팅 얇게.

### 신규 노출 표면 (phase1, 시장별)
- 지정가 주문 `buy_limit`/`sell_limit`, 잔고 `account_snapshot`(positions+account), 시세 `price`/`today_open`,
  주문가능 `orderable`(국내 VTTO5105R·해외 OTFM3304R: 증거금 기반 가능수량).
- phase2(라이브검증 후): 시장가, 정정취소, 체결조회.

## 5. 검증 매트릭스 (원칙: 검증된 해결책만)

| 시장 | 시세 | 잔고 | 주문(라이브) |
|---|---|---|---|
| 국내선물 | 모의 VTS 라이브(확인됨 A01606=1178.25) | 모의 라이브(필드 확정) | **모의 라이브**(다음 국장, A01606 시장가 라운드트립) |
| 해외선물 | read-only 실전(시세·상품기본정보) | read-only 실전(미결제내역) | **위임** — SimBroker+스펙 단위검증, read-only로 심볼·필드 확인, 실주문은 사용자 첫 실거래로 |
| 양 시장 로직 | — | — | **SimBroker E2E**(P0 선물화 후, Trader 배선까지 오프라인 결정론 검증) |

해외 실주문 경로는 KIS에 실제로 쏘기 전까지 미검증 — 이 사실을 코드·릴리즈 노트에 명시하고,
SimBroker로 *로직*은 완전 검증한다. (자금위험 0, 단 주문경로 라이브 미검증을 투명 고지)

## 6. 단계별 계획

의존: P0(토대) → P3(배선). P1·P2 브로커는 P0와 독립이라 병렬 가능. P4는 전부 의존.

### P0 — 테스트베드 선물화 (토대)
- **SimBroker 확장**: 포지션에 `side`(롱/숏)·`contracts`·`multiplier`, 잔고에 `margin`/`order_cash`,
  체결 시 증거금 점유·정산 손익. 기존 주식 동작 회귀 보존.
- **Broker 프로토콜/포지션 모델**: 선물 포지션 표현(롱+숏, 계약수, 평가손익) 통일.
- **Trader 사이징/리스크**: 현금% 사이징 → **증거금 기반 가능수량**(주문가능 API/계산). 롱·숏 진입/청산.
- **INVARIANTS 확장**: 선물 머니패스 불변식(증거금 초과 발주 금지·숏 청산 정합·정산 NAV) 추가.
- 산출: SimBroker 선물 시나리오 + 회귀 green. 문서(INVARIANTS·ORDER_STATE_MACHINE) 갱신.

### P1 — 국내선물 브로커 완성
- 현 브로커(PR #22·#26)에 phase2: 시장가(ORD_DVSN_CD=02)·정정취소(order-rvsecncl)·체결조회(inquire-ccnl).
- **국내 모의 라이브검증**(다음 국장): A01606 시장가 매수1→체결→잔고 populated 형태·포지션키 확정→청산.
- 산출: 국내선물 거래 표면 완성 + 모의 라이브검증 통과.

### P2 — 해외선물 브로커 추가
- 통합 브로커에 해외 주문(OTFM3001U)·잔고(OTFM1412R 행형 파서)·시세(현재가)·주문가능(OTFM3304R).
- CME globex 심볼 매핑 6종(근월물 코드 산출). `kis_overseas_futures_credentials` 슬롯 + secrets_store.
- **검증**: 순수함수 단위테스트(주문바디·잔고파싱) + read-only 실전(시세·잔고·주문가능)으로 심볼·필드 확인. 실주문 위임.
- 산출: 해외선물 거래 표면(실주문 경로는 SimBroker 검증·실전 위임).

### P3 — Trader 배선 (양 시장)
- 전략 신호 → 선물 주문: market 라우팅, 증거금 사이징(P0), 롱/숏 진입·청산, 포지션 reconcile(선물).
- killswitch·접수창·재시도 등 기존 자동매매 안전장치를 선물에 적용.
- **검증**: SimBroker E2E 시나리오(P0) — 하루 흐름·분할체결·외부drift·killswitch를 선물로.
- 산출: 자동 선물 매매 루프(국내=모의/실전, 해외=실전) — SimBroker로 전수 검증.

### P4 — 배포 · 릴리즈
- 서버/웹: 거래는 로컬앱(거래키 로컬 전용)이라 변경 최소 — 전략 정의·체결 로그 요약·잔고 스냅샷만(안전정보).
  필요 시 선물 자동매매 설정 UI(전략↔종목 매핑·on/off).
- 로컬앱: 버전 bump + 릴리즈노트 → CI(build-local.yml 3.11) 태그 push → quantman-releases publish.
- 프로덕션: Railway(서버)·Vercel(웹) origin/main 자동배포.
- 산출: 프로덕션 배포 + 로컬앱 릴리즈로 선물 자동매매 라이브.

## 7. 위험 · 미해결

- **해외 실주문 경로 미검증(위임).** SimBroker로 로직은 검증하나 실제 KIS 주문은 사용자 첫 실거래로 확인.
  완화: read-only 실전으로 심볼·잔고·주문가능 확인, 릴리즈노트에 "해외 첫 실거래는 1계약 소액으로" 명시.
- **증거금 사이징 모델.** 주식 현금% 사이징과 다름 — 주문가능 API 우선, 보수적 마진버퍼. P0/P3에서 확정.
- **CME 근월물 코드 산출.** globex 월물코드(F/G/H…+연도) + 롤 시점. 마스터/상품기본정보로 결정, 만기 알림.
- **자동매매 = 실제 자금.** 외부 상태 영향 — 단계마다 SimBroker 우선, 라이브는 모의(국내)·소액(해외)부터.
  서버에 거래키 절대 미유입(로컬 전용) 원칙 유지.

## 8. 성공 기준

- SimBroker로 국내·해외 선물 자동매매 루프가 결정론적으로 통과(증거금·롱숏·정산 불변식 포함).
- 국내선물: 모의 라이브 주문 라운드트립 통과.
- 해외선물: 순수함수 단위검증 + read-only 실전 확인(실주문 위임 명시).
- 프로덕션 배포 + 로컬앱 릴리즈 완료, 사용자가 선물 자동매매 on/off 가능.

# LS API TR Index

국내주식 TR 색인. **작업 전 `grep -i <키워드> INDEX.md`로 후보 찾기.**
상세는 `endpoints/{tr_cd}_*.md` 참조.

> 이 KB는 A2 초안(2026-06-17) + 공식문서 대조 검토(2026-06-19). 국내주식 핵심 6종 + 예수금 TR.
> 선물·해외주식·조건검색 등은 필요 시 추가.
>
> 🧪 **라이브 테스트(Phase C)**: [`PHASE-C-LIVE-TEST.md`](PHASE-C-LIVE-TEST.md) = 단계별 런북 + ⚠필드 실측 체크리스트.
> 실측 도구 = [`local/verify_ls.py`](../../local/verify_ls.py) (각 TR raw 응답 캡처 — `python verify_ls.py [--kosdaq|--order]`).

---

## 국내주식 — 주문 (3 TRs)

| tr_cd | 이름 | 용도 | 우리 코드 위치 (LsBroker 메서드) | 검증상태 |
|---|---|---|---|---|
| [`CSPAT00601`](endpoints/CSPAT00601_현물신규주문.md) | 현물신규주문 | 매수·매도 통합 단일 TR (BnsTpCode로 구분). 경로=`POST /stock/order` (tr_cd 헤더로 구분, `/stock/order-cancel` 별도경로 없음) | `buy()`·`sell()`·`buy_limit()`·`sell_limit()` (via `_submit`) | 🟢 크로스소스 확인 |
| [`CSPAT00701`](endpoints/CSPAT00701_현물정정주문.md) | 현물정정주문 | 가격·수량 정정. 경로=`POST /stock/order` (tr_cd=CSPAT00701) | 미배선 (Broker 프로토콜에 정정 없음 — 취소+재주문) | 🟢 크로스소스 확인 |
| [`CSPAT00801`](endpoints/CSPAT00801_현물취소주문.md) | 현물취소주문 | 주문 취소. 경로=`POST /stock/order` (tr_cd=CSPAT00801) | `cancel()` | 🟢 크로스소스 확인 |

## 국내주식 — 잔고·미체결 (2 TRs)

| tr_cd | 이름 | 용도 | 우리 코드 위치 (LsBroker 메서드) | 검증상태 |
|---|---|---|---|---|
| [`t0424`](endpoints/t0424_주식잔고조회2.md) | 주식잔고조회2 | 보유 포지션 목록 (체결기준). **sunamt=추정순자산=총자산(total_eval)**·tappamt=평가금액(보유 시가만·≠total_eval·현금제외)·mamt=원가·**sunamt1=추정D2예수금(cash)**·dtsunik=실현손익·tdtsunik=평가손익 | `account_snapshot()` (via `_balance_raw`) | 🟢 모의 실측 확정(2026-06-20·G22) |
| [`t0425`](endpoints/t0425_주식미체결조회.md) | 주식미체결 | chegb="2"=미체결only / chegb="0"=전체(체결·취소 포함). OutBlock1: **cheprice=체결가**·price=주문가·medosu="매수"/"매도"(문자열)·price1=현재가·**status(char10)=체결상태**. hname 없음(종목명 t0424에만) | `pending_orders()`(chegb="2")·`order_status()`(chegb="0"으로 전환 예정) (via `_pending_raw`) | 🟢 필드 크로스소스 / ⚠ status값 키검증 |

## 국내주식 — 시세 (1 TR)

| tr_cd | 이름 | 용도 | 우리 코드 위치 (LsBroker 메서드) | 검증상태 |
|---|---|---|---|---|
| [`t1102`](endpoints/t1102_주식현재가.md) | 주식현재가 | 경로=`POST /stock/market-data`. InBlock: shcode(6자리,A접두사없음)·**exchgubun 불요**(KOSPI·KOSDAQ 모두 미전송으로 동작). OutBlock: price=현재가·open=시가·high·low·volume·**recprice=전일종가**(≠pclose)·hname=종목명 | `price()`·`today_open()` | 🟢 모의 실측 확정(2026-06-20·G21해소) |

## 기타 국내주식 (미작성, 색인만)

아래는 필요 시 endpoint .md 추가. 현재는 INDEX 항목만.

| tr_cd | 이름 | 용도 | 우리 코드 위치 | 검증상태 |
|---|---|---|---|---|
| `CSPAQ22200` | 현물계좌예수금 | **정확한 주문가능금액(D2entra 등)** 소스. t0424의 sunamt1(추정D2예수금) 근사를 대체해 cash 정밀화 — Phase C 구현 예정 | 미배선 (현재 t0424.sunamt1로 근사) | ⚠️ 필드명·응답구조 키검증 필요 (Phase C) |
| `CSPAQ12300` | 현물계좌잔고내역 | 대안 잔고조회 (t0424 보완) | 미배선 (t0424로 대체) | 🟢 필드 일부 확인 |
| `t1101` | 주식현재가호가 | 10단 호가 | 미배선 | 🟢 |
| `t1301` | 주식시간대별체결 | 시간대별 체결 내역 | 미배선 | 🟢 |

---

## 인증

| 엔드포인트 | 용도 | 검증상태 |
|---|---|---|
| `POST /oauth2/token` | Access Token 발급 (익일 07:00 만료) | 🟢 구조 확인, ⚠️ 응답 필드 상세 미검증 |

---

## 빠른 사용 가이드

- **국내주식 매수**: `CSPAT00601` (BnsTpCode=2)
- **국내주식 매도**: `CSPAT00601` (BnsTpCode=1)
- **정정**: `CSPAT00701`
- **취소**: `CSPAT00801`
- **보유 잔고**: `t0424`
- **미체결 조회**: `t0425`
- **현재가**: `t1102` (shcode=6자리 종목코드)
- **예수금·주문가능액**: `CSPAQ22200`

## 도메인

| 용도 | 도메인 |
|---|---|
| REST (모의·실전 공통) | `https://openapi.ls-sec.co.kr:8080` |
| WebSocket (실시간) | `wss://openapi.ls-sec.co.kr:9443/websocket` |

> ⚠️ KIS와 달리 모의/실전이 **같은 도메인** — appkey로 환경 결정. 별도 도메인 존재 여부 키 발급 후 확인.

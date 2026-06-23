# LS 해외주식(미국주식) TR 조사 — Phase E 근거

> 조사 2026-06-21 (모의키 발급 후, read-only 리서치). 출처 = **LsApiHelper specs**(LS 공식 포털 미러·실 req/res 예시) + **programgarden-finance**(독립 Pydantic 타입·한글 필드설명) 교차. 🟢=2소스 일치. ⚠=단일소스/모의 실측 필요.
> 구현(Phase E) 착수 시 이 표를 `endpoints/`·`INDEX.md`·`GOTCHAS.md`로 분해한다.

**공통 구조(국내와 동일·G2/G17/G18 적용):** 단일 도메인 `openapi.ls-sec.co.kr:8080`, OAuth2 client_credentials, 헤더 `tr_cd`, body `{<TRcd>InBlock1:{...}}`, POST only, 응답 `{rsp_cd, rsp_msg, <TRcd>OutBlock1/2/3/4}`. 모의/실전=키로 분기.

## TR 표

| tr_cd | 용도 | 경로 | request 핵심 | response 핵심 | Broker 메서드 | 신뢰도 |
|---|---|---|---|---|---|---|
| **COSAT00301** | 미국 주문(매수02/매도01/**취소08**) | `/overseas-stock/order` | `OrdPtnCode`·`OrgOrdNo`(취소)·`OrdMktCode`(82NASDAQ/81NYSE+AMEX)·`IsuNo`(bare "AAPL")·`OrdQty`·`OvrsOrdPrc`(지정가 USD float, 시장가0)·`OrdprcPtnCode`(00지정/03시장/M1 LOO/M2 LOC/M3 MOO/M4 MOC) | OutBlock2: **`OrdNo`** | buy/sell/buy_limit/sell_limit/cancel | 🟢 |
| **COSAT00311** | 미국 정정 | `/overseas-stock/order` | `OrdPtnCode`="07"·`OrgOrdNo`·… | OutBlock2:`OrdNo` | (취소+재주문) | 🟢코드/⚠무예시 |
| **COSAT00400** | 해외 **예약주문** 등록/취소 | `/overseas-stock/order` | `TrxTpCode`(등록/취소)·`CntryCode`("US")·`RsvOrdNo`(취소)·`BnsTpCode`·**`AcntNo`·`Pwd`(body 필수)**·`FcurrMktCode`·`IsuNo`·`OrdQty`·`OvrsOrdPrc`·`OrdprcPtnCode`·**`RsvOrdSrtDt`·`RsvOrdEndDt`**(실행일창) | OutBlock2: **`RsvOrdNo`** | buy_resv_limit/sell_resv_limit | 🟢필드/⚠enum |
| **COSOQ00201** | **해외 종합잔고평가**(잔고+평가+환율) | `/overseas-stock/accno` | `BaseDt`(필수)·`CrcyCode`("ALL"/"USD")·`AstkBalTpCode`("00") | OutBlock3[통화별]: `FcurrDps`(외화예수금)·**`BaseXchrat`**(환율)·`FcurrOrdAbleAmt` / OutBlock4[종목별]: `ShtnIsuNo`(티커)·`AstkBalQty`·`FcstckUprc`(매입단가)·`OvrsScrtsCurpri`(현재가)·`FcurrMktCode` | **account_snapshot** | 🟢 실예시 |
| **COSOQ02701** | 해외 예수금(통화별 정밀) | `/overseas-stock/accno` | `CrcyCode` | OutBlock3: `FcurrOrdAbleAmt`·`BaseXchrat` | (선택 정밀 cash) | 🟢 |
| **COSAQ00102** | **계좌주문체결내역**(체결/미체결) | `/overseas-stock/accno` | `OrdDt`(필수)·**`ExecYn`**(0전체/1체결/2미체결)·`SrtOrdNo`(999999999)·`IsuNo`(""전체) | OutBlock3[]: `OrdNo`·`OrgOrdNo`·`ShtnIsuNo`·`OrdQty`·**`ExecQty`**·**`OvrsExecPrc`**(체결가)·**`UnercQty`**(미체결)·**`OrdTrxPtnNm`**("접수완료"/"체결"/"정정완료"/"취소완료") | **order_status·pending_orders** | 🟢 실예시 |
| **COSAQ01400** | 예약주문 처리결과 | `/overseas-stock/accno` | `CntryCode`("001")·`SrtDt`·`EndDt`·`RsvOrdStatCode` | OutBlock2[] 예약내역 | order_status 보조(예약) | 🟢 |
| **g3101** | **해외 현재가**(USD OHLC) | `/overseas-stock/market-data` | `delaygb`("R")·`keysymbol`(**exchcd+티커** "82TSLA")·`exchcd`·`symbol` | OutBlock: **`price`**·`open`·`high`·`low`·`currency`·`floatpoint`·`volume` | **price·today_open** | 🟢 실예시 |
| **g3104** | 해외 종목정보(틱·정산환율) | `/overseas-stock/market-data` | (g3101 동일) | OutBlock: `untprc`(호가단위)·**`exrate`**(정산환율)·메타 | (선택 틱·메타) | 🟢 |
| **g3106** | 해외 10단호가 | `/overseas-stock/market-data` | (동일) | offerho1..10·bidho1..10 | (필요시 슬리피지) | 🟢 |
| g3190 | 해외 마스터(페이징) | `/overseas-stock/market-data` | `natcode`("US")·`exgubun`("2") | keysymbol·symbol·exchcd·korname | (선택 라우팅) | 🟢 |

WS 실시간(Phase 3): `wss://…:9443/websocket/overseas-stock` — AS0접수/AS1체결/AS2정정/AS3취소/AS4거부, GSH호가/GSC체결.

## Broker 메서드 매핑 (KIS 해외경로 대칭)

- **account_snapshot(overseas=True)** = COSOQ00201: `cash_usd`=OB3[USD].FcurrDps · `fx_usdkrw`=OB3[USD].**BaseXchrat** · positions=OB4[](symbol=ShtnIsuNo, qty=AstkBalQty, avg=FcstckUprc, eval=OvrsScrtsCurpri, mkt 82→NAS/81→NYS, ccy=USD) · **`foreign_eval_krw` = (cash_usd + Σ qty·eval)×fx 직접계산**(KIS와 동일 — WonEvalSumAmt는 주식만이라 사용주의, frcr 불일치 회피).
- **price/today_open** = g3101 `price`/`open`(keysymbol=exchcd+symbol). 별도 시초가 TR 불필요.
- **buy/sell/limit** = COSAT00301(02/01, 시장가03/지정가00). `OvrsOrdPrc`=USD **float 유지(int 절삭 금지·$0.01 틱)**.
- **buy_resv_limit/sell_resv_limit** = COSAT00400(예약, RsvOrdSrtDt/EndDt, **AcntNo·Pwd body 필수**).
- **cancel** = COSAT00301(OrdPtnCode="08"+OrgOrdNo) / 예약취소=COSAT00400.
- **order_status** = COSAQ00102: filled=ExecQty>0&UnercQty==0 / partial=0<UnercQty<OrdQty / cancelled=OrdTrxPtnNm "취소완료". 예약은 RsvOrdNo→COSAQ01400. fill_price=OvrsExecPrc.
- **pending_orders** = COSAQ00102(ExecYn="2").

## 해외 특화 (GOTCHAS 후보 G23~)

1. **시장코드 2분할**: **82=NASDAQ**, **81=NYSE+AMEX**(AMEX가 NYSE와 합쳐짐 — KIS는 NAS/NYS/AMS 3분할). market_index의 AMS→"81" 매핑 필요. ⚠ AMEX 81 실측(OG5).
2. **종목코드 = bare 티커**(A접두사 없음). 시세는 `keysymbol`=exchcd+symbol.
3. **FX 전용 TR 없음** — USD/KRW는 COSOQ00201 OutBlock3 `BaseXchrat`에 내장(별도 호출 불요). 보조 g3104 `exrate`. KIS frst_bltn_exrt와 동일 패턴.
4. **가격 float**(소수 USD) — int 직렬화 금지.
5. **AcntNo/Pwd body**: 정규주문·잔고·체결조회·시세는 **미포함**(토큰서 도출). **예약(COSAT00400)·신용(COSMT00300)만 포함 필수**(국내 G11 연장). ⚠ 모의 실측.
6. **성공판정 = OrdNo 존재**(주문 rsp_cd 미선언 — 국내 G17과 동일 부류). 예약=RsvOrdNo.
7. **TPS 차등**: 시세 g31xx·주문 COSAT00301/00311=10/s, **계좌/예약/체결조회=1/s**(국내 ~2/s보다 보수적). `_Throttle` TR별 차등 또는 전역 1/s 검토.
8. **미국 시장가(03)·M코드 모의 지원 ⚠**: KIS는 해외 시장가 모의 미지원→지정가 강제. LS 동일 여부 OG3 실측.

## Gaps (모의키 프로빙 / 로그인 공식문서)
OG1 정정·예약·신용 OutBlock 전체필드 · OG2 주문 성공 rsp_cd 정확값 · OG3 시장가/M코드 모의지원 · OG4 COSAT00400 AcntNo/Pwd 필수+enum · OG5 AMEX 81 · OG6 COSOQ00201 당일 BaseDt 동작 · OG8 OrdTrxPtnNm 부분체결/거부 문자열.
→ 공식 `openapi.ls-sec.co.kr/apiservice` [해외주식](로그인)에서 OG1·OG2·OG4 해소 가능(모의키 없이).

출처: [LsApiHelper](https://github.com/xorrhks0216/LsApiHelper)(specs) · [programgarden-finance](https://pypi.org/project/programgarden-finance/) · KIS 레퍼런스 `local/localapp/kis_broker.py`(overseas).

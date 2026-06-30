# LS 국내선물(KOSPI200 지수선물) TR 조사 — Phase D 근거

> 조사 2026-06-22 (read-only). 출처 = **LsApiHelper specs**(LS 공식 포털 미러·실 req/res 예시·blocks.json 364 TR) + **teranum/ls-openapi-samples**(`15.국내선물`·`08.지수선물마스터`·`22.실시간` — **모의투자 라이브 출력 포함**) 교차. 🟢=2소스 일치. ⚠=단일/모의 실측 필요.
> 공통 구조는 국내주식 KB와 동일(단일 도메인 `:8080`·OAuth2·`tr_cd` 헤더·블록 JSON·POST). programgarden-finance는 국내선물 미수록 → 2소스는 teranum 라이브.

## 🔬 2026-06-30 실측 확정 (verify_ls.py 모의+실전 프로브 — 차이 시 이 절이 정본)
모의(계좌 …51)+실전(…02) read-only 프로브로 확정. 아래 inline 값과 다르면 **이 절 우선**:
- **지수선물 마스터 TR = `t8467`** (`index_futures_broker.index_futures_master()`가 호출). OB=`t8467OutBlock[]`:
  `hname`("F 2609" = YYMM 4자리)·`shcode`·`expcode`(ISIN)·상하한·`jnilclose`. ⚠ 아래 표의 **t8432/t9943은 미사용/구**.
- **계약(단축)코드 = `A01…` 8자** (예 `A0169000`=26-09·`A016C000`=26-12·`A0173000`=27-03·ISIN `KR4A01690002`).
  표/§선물특화·§대비의 **`101V6000`은 stale** — 실측은 `A01…`. resolver prefix `"A01"`·core `_DOMESTIC_SPEC`와 일치.
- **CFOAQ10100 `NewOrdAbleQty` 필드 실재** — 모의 값 3(가용 236M÷계약증거금[1360×250k×**~0.195**]≈3 = 실 동적
  증거금률 반영 확인)·실전 0(빈계좌). 모델 A 사이징 토대 확정. OB2에 `UsePreargMgn`(잠긴증거금)도 있음(모의 equity 복원용).
- **CFOAQ50600: 실전은 `EvalDpsamtTotamt`(추정예탁자산) 정상 제공**(rsp_cd 00136·전체 평가 OB2·예수금·증거금·평가손익).
  **모의는 미제공**(rsp_cd 01900→OrdAbleAmt 근사 → 포지션 시 equity 가짜하락). **G-DF4 실전 해소**(실금 후 값 확정).
- **G-DF9 해소**: hname YYMM 4자리·resolver 정규식 일치(모의+실전 resolve→`A0169000`). **G-DF8 해소**: t0441 보유행 실측
  (expcode `A0169000`·medosu "매도"·jqty·pamt·dtsunik1, 모의).
- **주문 WS `C01` = 실전 port 9443 연결 + 구독 ack rsp_cd 00000 확인**(발주 0). SC1(주식) 미설정 라우터서 정상 skip.
- 미해소(실금 필요·Phase 1): 실전 발주(CFOAT00100 `OrdNo`)·체결·t0441 보유형식·C01 체결 payload.

## 🟢 TOP — 국내선물옵션 모의투자 지원됨
teranum `15.국내선물` 라이브 = "접속서버: **모의투자**" + CFOAT00100 매수/CFOAT00200 정정이 모의서버에서 실제 처리("모의투자 정정주문 완료"). → 모의 키로 paper 주문 라운드트립 E2E 가능(KIS 국내선물 모의 지원과 동일). **4자산군 전부 모의 지원 확정.**

## TR 표 (도메인 `:8080`, 인증·헤더 국내주식 동일)
⚠ rsp_cd TR마다 다름(매수신규 "00040"·정정 "00132"·취소 "00156"·조회 "00136") → **성공판정 = OutBlock2 `OrdNo` 존재**(국내 G17 패턴).

| tr_cd | 용도 | 경로 | request 핵심 | response 핵심 | Broker 메서드 | 신뢰도 |
|---|---|---|---|---|---|---|
| **CFOAT00100** | 신규주문(매수/매도) | `/futureoption/order` | `FnoIsuNo`(8자 "101V6000")·`BnsTpCode`(1매도/2매수)·`FnoOrdprcPtnCode`(00지정/03시장)·`FnoOrdPrc`(double 포인트)·`OrdQty` | OB2: **`OrdNo`**·`OrdMgn`(주문증거금)·`OrdAbleQty` | buy/sell/buy_limit/sell_limit | 🟢 |
| **CFOAT00200** | 정정 | `/futureoption/order` | `OrgOrdNo`·`FnoIsuNo`·`FnoOrdPrc`·`MdfyQty` | OB2:`OrdNo` | (취소+재) | 🟢 |
| **CFOAT00300** | 취소 | `/futureoption/order` | `OrgOrdNo`·`FnoIsuNo`·`CancQty` | OB2:`OrdNo` | cancel | 🟢 |
| **t0441** | 선물 잔고(평균단가) | `/futureoption/accno` | `cts_expcode`("")·`cts_medocd`("") | OB1[]: `expcode`·`medosu`("매수"/"매도")·**`jqty`**(잔고)·`cqty`(청산가능)·`pamt`(평단)·`price`(현재가)·**`dtsunik1`**(평가손익) | account_snapshot(positions) | 🟢 |
| **CFOAQ50600** | 예탁금·증거금 현황3 | `/futureoption/accno` | `OrdDt`·`BalEvalTp`("1")·`FutsPrcEvalTp`("1") | OB2: **`EvalDpsamtTotamt`**(추정예탁자산=equity)·`DpstgMny`(예수금)·**`MnyOrdAbleAmt`**(현금주문가능)·`CsgnMgnTotamt`(위탁증거금)·`MtmgnTotamt`(유지증거금)·**`FutsEvalPnlAmt`**(평가손익)·`FutsAdjstDfamt`(정산차금) | account_snapshot(account) | 🟢 |
| **CFOAQ10100** | 주문가능수량 | `/futureoption/accno` | `QryTp`("1")·`FnoIsuNo`·`BnsTpCode`·`FnoOrdPrc`·`FnoOrdprcPtnCode` | OB2: **`NewOrdAbleQty`**(신규)·`LqdtOrdAbleQty`(청산)·`OrdAbleQty` | orderable_qty | 🟢 |
| **t0434** | 체결/미체결 | `/futureoption/accno` | `chegb`(0전체/1체결/2미체결)·`sortgb`("1")·`cts_ordno`("") | OB1[]: `ordno`·`orgordno`(원주문≠0=정정/취소)·`qty`·**`cheqty`**(체결)·**`ordrem`**(미체결잔량)·`price`(주문가)·**`cheprice`**(체결가)·**`status`**("완료"/"접수"/"확인") | order_status·pending_orders | 🟢 |
| **t2101** | 현재가(체결) | `/futureoption/market-data` | `focode`(8자) | OB: **`price`**·**`open`**·`high`·`low`·`recprice`/`jnilclose`(전일정산)·`volume`·`uplmtprice`/`dnlmtprice`(상하한)·`hname` | price·today_open | 🟢 |
| **t2105** | 5단 호가 | `/futureoption/market-data` | `shcode` | offerho1..5·bidho1..5 | (슬리피지) | 🟢 |
| **t9943** | 지수선물 종목리스트(경량) | `/futureoption/market-data` | `gubun`(0전체/1코스피/2코스닥) | `shcode`·`expcode`(ISIN)·`hname`("F 2406") | (부팅·근월물 해석) | 🟢 |
| **t8432** | 지수선물 마스터(상세) | `/futureoption/market-data` | `gubun`("0") | `shcode`(101V6000)·`expcode`(KR4101V60002)·`jnilclose`·상하한 | (부팅·심볼 마스터) | 🟢 |

부가(후보): CFOEQ11100(일별 정산차금), t2201(시간대별), WS FC0(KOSPI200선물 체결)·FH0(호가).

## Broker 매핑 (KIS `kis_futures_broker.py` 국내선물 대칭)
- **account_snapshot** = CFOAQ50600(account) + t0441(positions) **[2 TR 합성]**: equity=`EvalDpsamtTotamt`(KIS prsm_dpast_amt 동의·kill-switch) · order_cash=`MnyOrdAbleAmt` · margin=`CsgnMgnTotamt` · eval_pnl=`FutsEvalPnlAmt`. positions: symbol=expcode, side=medosu(매수→롱/매도→숏), qty=jqty, avg=pamt, eval_pnl=dtsunik1.
- **price/today_open** = t2101 price/open (전일정산 jnilclose).
- **buy/sell** = CFOAT00100(시장가 "03"·FnoOrdPrc 0, 매수 BnsTp"2"/매도"1"). limit="00"+FnoOrdPrc(**double 포인트값**, int 절삭 금지).
- **cancel** = CFOAT00300(OrgOrdNo+CancQty). ⚠ **국내선물 취소는 원주문일자 불요**(해외선물 CIDBT01000과 다름·국내주식 CSPAT00801과 동일).
- **order_status** = t0434(chegb="0" 전체): filled=ordrem==0&cheqty>0 / partial=cheqty>0&ordrem>0 / status 문자열로 cancelled/rejected. fill_price=cheprice.
- **pending_orders** = t0434(chegb="2"): ⚠ `orgordno≠0`(정정/취소) 제외(KIS orgn_odno 필터 동일 — 취소주문 pending 오보고 방지).
- **orderable_qty** = CFOAQ10100 NewOrdAbleQty.
- buy_resv_limit/sell_resv_* = NotImplementedError(국내선물 예약 미지원, KIS 선물과 동일).

## 선물 특화
- **롱/숏 = BnsTpCode net position**(진입/청산 별도코드 없음 — KIS 국내선물 동일): 롱진입=매수2·롱청산=매도1·숏진입=매도1·숏청산=매수2. (해외선물 FutsOrdTpCode 신규/청산과 대조.)
- **multiplier = 250,000원/pt**(KOSPI200, KRX 표준). repo `instrument_spec("코스피200선물").multiplier`에 정의 — 엔진 재사용, broker는 가격만 포인트로 전송.
- **종목코드 8자 단축코드** `101V6000`(101=KOSPI200정규·char3=연도cipher V=2024/W=2025/A016=2026·char4:6=월03/06/09/12·000필러). **ISIN** `KR4101V60002`. 스프레드(401…S) 제외.
- **일일정산**: 종가(정산가)로 변동손익 현금정산. equity(EvalDpsamtTotamt)에 정산 반영. NAV는 jnilclose(전일정산) 기준(KIS 선물 동일).

## 국내주식 LsBroker 대비 (재사용 vs 신규)
**재사용**: OAuth·Bearer·익일07시 만료·tr_cd 헤더·블록 응답·단일도메인·OrdNo 성공판정·throttle — 전부 동일.
**신규**: 경로 `/futureoption/{order,accno,market-data}` · 계좌 **2-TR 합성**(CFOAQ50600+t0441) · 시세키 `focode`/잔고 `expcode`(8자, 주식 6자와 다름) · **LS 전용 심볼 resolver**(t8432/t9943 마스터 — KIS `A01606`≠LS `101V6000`; ISIN 공통키로 매핑) · accno 1/s(보수적).

## Gaps (모의키 프로빙 / 로그인 공식문서)
G-DF1 주문 rsp_cd 코드표(OrdNo판정이라 무관) · G-DF2 AcntNo/InptPwd body 필수여부(G11 동형) · **G-DF3 t0434.status 문자열 전체집합**(완료/접수/확인/취소/거부 — filled/cancelled 분기 전 필수) · G-DF4 보유시 EvalDpsamtTotamt 정산반영 · G-DF5 단축코드 cipher 전체규칙(t8432 마스터 런타임해석이 근본해결) · G-DF6 모의 시장가("03") 지원 · G-DF7 모의 호출제한·체결시뮬 · G-DF8 t0441 행형 확정(✅사실상 해소) · **G-DF9 t8432 hname 필드 존재·형식**(t9943은 "F 2406" YYMM 4자리인데 resolver 정규식은 YYYYMM 6자리 가정 — t8432 실측 확정 필요; 미일치 시 resolve None→발주 skip 안전하나 거래 불가).
→ 공식 apiservice(로그인)에서 G-DF1~3 해소 가능. 나머지 모의키 프로빙(`verify_ls.py` 선물 확장).

출처: [LsApiHelper](https://github.com/xorrhks0216/LsApiHelper) specs(b579d38a·09a668df·9f467798) · [teranum/ls-openapi-samples](https://github.com/teranum/ls-openapi-samples)(모의 라이브) · KIS `local/localapp/kis_futures_broker.py`·`broker_router.py`.

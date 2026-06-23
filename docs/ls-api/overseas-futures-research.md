# LS 해외선물(CME 등) TR 조사 — Phase F 근거

> 조사 2026-06-21 (read-only). 출처 = **LsApiHelper specs**(LS 공식 포털 미러·실 req/res 예시 — `specs/apis/{b820f925,44c1c082,d61d4f85}.json`) + programgarden-finance + xingAPI legacy 교차. 🟢=실 resExample 확정.
> 공통 구조는 국내주식 KB와 동일(단일 도메인·OAuth2·tr_cd 헤더·블록 응답).

## 🟢 TOP — 해외선물 **모의투자 지원됨** (KIS와 정반대 · Phase F 핵심)

- **확정**: LS 상시모의투자 참가부문 = `국내주식/해외주식·국내선물옵션·**해외선물**`. 공식 포털이 TR마다 "모의투자 도메인" 컬럼 노출 + 해외선물 1급 카테고리. → **모의 키로 paper E2E(주문 라운드트립) 검증 가능 — SimBroker 우회 불필요**(KIS 해외선물 모의 미지원 함정이 LS엔 없음).
- caveat: 모의서버 REST 호출제한 실전보다 낮음·모의 중복로그인 제한·모의 신청 3개월 주기. ⚠ 모의서버 **체결 시뮬 실작동 여부**는 첫 발주로 실측(G-OF1).

## TR 표 (도메인 `:8080` 공통, 인증 국내와 동일)

⚠ rsp_cd가 TR마다 다름(조회 "00136"·신규 "00000"·취소 "00156" 등) → **성공판정은 OutBlock2 `OvrsFutsOrdNo` 존재**로(국내 G17 패턴).

| tr_cd | 용도 | 경로 | request 핵심 | response 핵심 | Broker 메서드 | 신뢰도 |
|---|---|---|---|---|---|---|
| **CIDBT00100** | 해외선물 신규주문 | `/overseas-futureoption/order` | `OrdDt`·`IsuCodeVal`(ADM23)·`FutsOrdTpCode`("1"신규)·`BnsTpCode`("1"매도/"2"매수)·`AbrdFutsOrdPtnCode`("1"시장가/"2"지정가)·`OvrsDrvtOrdPrc`(double)·`OrdQty`(계약)·`ExchCode`·`DueYymm` | OutBlock2: **`OvrsFutsOrdNo`** | buy/sell/buy_limit/sell_limit | 🟢 |
| **CIDBT00900** | 정정 | `/overseas-futureoption/order` | `OvrsFutsOrgOrdNo`+… | OutBlock2:`OvrsFutsOrdNo` | (취소+재) | 🟢 |
| **CIDBT01000** | 취소 | `/overseas-futureoption/order` | `OrdDt`·`IsuCodeVal`·**`OvrsFutsOrgOrdNo`**(0패딩10자리)·`FutsOrdTpCode`("3"취소) | OutBlock2:`OvrsFutsOrdNo` | cancel | 🟢 |
| **CIDBQ03000** | **예수금/잔고현황**(USD 요약) | `/overseas-futureoption/accno` | `AcntTpCode`("1")·`TrdDt` | OB2[]: `CrcyObjCode`("TOT(USD)")·**`EvalAssetAmt`**(평가자산=equity)·`PrexchDps`(예탁)·**`AbrdFutsOrdAbleAmt`**(주문가능)·`AbrdFutsCsgnMgn`(위탁증거금)·**`AbrdFutsEvalPnlAmt`**(평가손익)·`LastSettPnlAmt`(전일정산) | account_snapshot(요약) | 🟢 |
| **CIDBQ01500** | **미결제잔고**(포지션) | `/overseas-futureoption/accno` | `AcntTpCode`·`BalTpCode`("1") | OB2[]: `IsuCodeVal`·`BnsTpCode`(1매도/2매수)·**`BalQty`**·**`PchsPrc`**(매입단가)·**`OvrsDrvtNowPrc`**(현재가)·**`AbrdFutsEvalPnlAmt`**·`CsgnMgn`/`MaintMgn`·`DueDt` | account_snapshot(positions) | 🟢 |
| **CIDBQ02400** | **주문체결내역**(체결/미체결) | `/overseas-futureoption/accno` | `QrySrtDt`·`QryEndDt`·`ThdayTpCode`("1")·`OrdStatCode`("0"전체)·`OvrsDrvtFnoTpCode`("A") | OB2[]: `OvrsFutsOrdNo`·`TrxStatCodeNm`("체결")·`OrdQty`·**`ExecQty`**·**`UnercQty`**·**`AbrdFutsExecPrc`**(체결가)·`OrdPtnNm` | order_status·pending_orders | 🟢 |
| **CIDBQ01400** | 주문가능수량 | `/overseas-futureoption/accno` | `IsuCodeVal`·`BnsTpCode`·`OvrsDrvtOrdPrc` | OB2: **`OrdAbleQty`** | (사이징 클램프) | 🟢 |
| **CIDBQ05300** | 예탁자산(통화별·환율) | `/overseas-futureoption/accno` | `CrcyCode`("ALL"/"USD"/"KRW") | OB2[통화별]: `OvrsFutsDps`·**`Xchrat`**(환율)·`FcurrRealMxchgAmt` / OB3: `OvrsFutsMaintMgn`(유지증거금)·**`MgnclRat`**(마진콜율) | (FX·증거금 상세) | 🟢 |
| **o3105** | **해외선물 현재가**(OHLC·계약정보) | `/overseas-futureoption/market-data` | `symbol`(8자 "CUSN23  ") | **`TrdP`**(현재가)·**`OpenP`**(시가)·`HighP`·`LowP`·`CloseP`(전일)·`UntPrc`(틱크기)·**`CtrtPrAmt`**(계약당금액)·**`MnChgAmt`**(틱가치)·`DotGb`(소수자릿)·`CrncyCd`(USD) | price·today_open | 🟢 |
| **o3101** | **종목 마스터**(승수·증거금·tick) | `/overseas-futureoption/market-data` | `gubun`("") | OB[]: `Symbol`(ADM23)·`BscGdsCd`(AD)·`ExchCd`(CME)·**`CtrtPrAmt`**·**`MnChgAmt`**·**`UntPrc`**·`DotGb`·`OpngMgn`/`MntncMgn`(개시/유지증거금)·월물·거래시간 | (부팅 1회 로드·심볼해석) | 🟢 |

## Broker 매핑 (KIS 해외선물 대칭 — 단 LS는 모의 지원)
- buy/sell = CIDBT00100(시장가 OrdPtnCode"1", 매수 BnsTp"2"/매도"1"). limit=OrdPtnCode"2"+OvrsDrvtOrdPrc.
- **cancel** = CIDBT01000(OvrsFutsOrgOrdNo + **OrdDt 원주문일자 필수** — 호출부가 신규응답 OrdDt 보관, KIS와 동일).
- account_snapshot = CIDBQ01500(positions)+CIDBQ03000(account): equity=EvalAssetAmt, order_cash=AbrdFutsOrdAbleAmt, margin=AbrdFutsCsgnMgn, eval_pnl=AbrdFutsEvalPnlAmt. **USD 표시**.
- price/today_open = o3105 TrdP/OpenP.
- order_status = CIDBQ02400: filled=UnercQty==0&ExecQty>0 / partial=ExecQty>0 / fill_price=AbrdFutsExecPrc. ⚠ OrdStatCode 미체결값 실측(G-OF4).
- orderable = CIDBQ01400 OrdAbleQty.

## 특화 (GOTCHAS 후보)
- **종목코드** `BscGdsCd+월물+연2자리`(ADM23=AD+M(6월)+23). CME 월물코드 F~Z. **KIS 심볼≠LS 심볼** → 매핑 레이어 필요(금 KIS`GC`↔LS BscGdsCd 마스터조회).
- **승수/틱 = o3101/o3105 명시**: CtrtPrAmt(계약당금액)·MnChgAmt(틱가치 USD)·UntPrc(틱크기)·DotGb(소수자릿). KIS sCalcDesz보다 명시적.
- **통화 USD**. CIDBQ03000은 TOT(USD) 합산 → **KRW 통합 equity는 CIDBQ05300 `Xchrat`로 USD→KRW 환산 필요**(KIS는 TKR KRW요약 직접 — LS는 ⚠ CIDBQ05300 KRW행 의미 실측 G-OF5).
- **마진콜율** MgnclRat(CIDBQ05300 OB3) — kill-switch 활용 가능.
- **OvrsFutsOrdNo 0패딩 10자리** → canonical(lstrip"0") 매칭(KIS _canon_odno 패턴).

## Gaps (모의키 실측)
G-OF1 모의 체결 시뮬 작동 · G-OF2 주문코드값(정정/STOP) · G-OF3 o3105 가격스케일(DotGb 적용 여부) · G-OF4 미체결 OrdStatCode · G-OF5 USD→KRW equity 경로 · G-OF6 취소 OrdDt 출처 · G-OF7 모의 rate limit · G-OF8 AcntNo/Pwd body · G-OF9 OrdNo 패딩.

출처: [LsApiHelper](https://github.com/xorrhks0216/LsApiHelper) specs(b820f925·44c1c082·d61d4f85) · programgarden-finance · LS 상시모의투자 안내(해외선물 포함) · KIS `local/localapp/kis_futures_broker.py`.

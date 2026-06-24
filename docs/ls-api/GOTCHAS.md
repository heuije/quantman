# LS API GOTCHAS — 알려진 함정·가정

공식 docs·실측 차이·알려진 한계. 작업 전 한 번 훑기.
새 발견 시 **위에 추가** (최신순). 각 entry: 날짜·증상·원인·해결.

---

## 실시간 WebSocket 사양 (2026-06-25 모의 연결 프로브 실측) — 실시간 WS 배선

LS 모의 WS 연결 프로브(read-only·주문 없음)로 확정. 가이드(§15897 선물·§43641 주식)와 **차이 있음**.

### G-WS1 — 엔드포인트: 단일 bare `/websocket`, 모의=29443 🔴 가이드 오류 교정
- **실측**: `wss://openapi.ls-sec.co.kr:29443/websocket`(모의)·`:9443/websocket`(실전), **단일 bare 경로**. tr_cd가 stock/futures 라우팅.
- 가이드의 `/websocket/stock`·`/websocket/futureoption`은 **양 포트 모두 connect timeout** — 不通. bare `/websocket`만 SC1·C01·FC9·S3_ 정상처리.
- 9443에 모의 토큰 → `rsp_cd 10001 "token 계좌정보와 서버정보(실전,모의)가 일치하지 않습니다"` 거부 → **9443=실전 전용, 29443=모의**.

### G-WS2 — 인증: OAuth access token 재사용 (approval_key·AES 불필요) 🟢
- 구독: `{"header":{"token":<access_token>,"tr_type":"1"등록/"2"해제},"body":{"tr_cd","tr_key"}}`. 전부 평문 JSON.
- ack: `{"header":{tr_type,tr_cd,rsp_cd,rsp_msg},"body":null}` (rsp_cd `"00000"`=정상). KIS의 approval_key 발급·AES-CBC 복호화·pipe payload **전부 불필요**.

### G-WS3 — 실시간 TR
- **체결통보(fill)**: 주식 `SC1`·국내선물 `C01` (tr_key="" = 내 주문 전체). 모의 정상처리 확인.
  - SC1 body: `ordno`·`shtnIsuno`(A005930)·`execqty`·`execprc`·`exectime`·`gubun`(B=매수). C01 body: `ordno`·`expcode`·`cheprice`·`chevol`·`chetime`·`dosugb`.
  - SC계열: SC0=접수·**SC1=체결**·SC2=정정·SC3=취소·SC4=거부 (체결만 보려면 SC1).
- **시세(체결가)**: 주식 `S3_`(KOSPI)·`K3_`(KOSDAQ)·선물 `FC9`(KOSPI200)·`JC0`(주식선물), tr_key=종목/계약코드. body 공통 `price`(체결가). 모의 FC9(A0169000)·SC1·C01 정상처리 확인.
- ⚠ **데이터(틱) 실제 전달은 장중에만** — 모의 라이브(국장)서 최종 확인 예정. 현재는 connect+구독 ack(`00000`)까지 확정.

---

## 모의 라이브 실측 (2026-06-20, 키 발급 후) — Phase C C-0 (verify_ls.py 읽기전용)

모의 키(5억 예수금·보유0)로 토큰·t0424·t1102(KOSPI+KOSDAQ)·t0425를 raw 캡처해 확정.

### G22 — total_eval = `sunamt`(총자산), `tappamt` 아님 🔴 CRITICAL 교정 (G15 수정)

- **실측**: 보유0·현금5억 계좌에서 `t0424OutBlock` = `{sunamt:500000000, sunamt1:500000000, tappamt:0, mamt:0, dtsunik:0, tdtsunik:0}`.
  → `tappamt`(평가금액)는 **보유종목 시가만**이라 보유0이면 0. `sunamt`(추정순자산)는 **주식평가+예수금=총자산** = 5억.
- **버그(직전 G15/C3)**: `total_eval=tappamt`로 매핑 → `_unified_equity_krw`(trader.py:149는 국내 equity=`total_eval`, 현금 별도 가산 안 함)가 **현금을 누락**. 현금 비중 큰/미보유 계좌에서 equity≈0 → 킬스위치 거짓 -100% 발동(−98% 부류버그 재발).
- **교정**: `total_eval = sunamt`. KIS의 `tot_evlu_amt`(총평가=유가증권+예수금, 현금 포함)와 **동일 의미**의 LS 필드가 `sunamt`. `cash=sunamt1`은 유지(정확값은 여전히 CSPAQ22200, G16). `ls_broker.account_snapshot` 수정 완료 + 회귀테스트(sunamt=총자산, tappamt/mamt=decoy).
- ⚠ 보유 시 sunamt가 시가 손실을 반영하는지는 C-1/E2E(포지션 보유)에서 최종 확인.

### G21 해소 — KOSDAQ `exchgubun` 불요 🟢 실측 확정

- **실측**: 카카오(035720, KOSDAQ)를 `exchgubun` 없이 `{shcode:"035720"}`만으로 조회 → 정상(price 37750·hname "카카오"). KOSPI(005930)도 동일. → **현 코드(exchgubun 미전송)가 양 시장 모두 동작**. G21 ⚠ 종결, 코드 변경 불요.

### 블록·필드명 확정 🟢 (draft 가정 → 실측 일치)

- **t0424**: 블록 `t0424OutBlock`(요약 dict)·`t0424OutBlock1`(보유 list, 보유0이면 `[]`) 확정. 쿼리 성공 `rsp_cd="00000"`(G1 조회 성공코드 확정).
- **t1102**: 경로 `/stock/market-data`·블록 `t1102OutBlock` 확정. `price`(현재가)·`open`(시가)·`high`·`low`·`volume`·`recprice`(전일종가)·**`hname`(종목명, t1102엔 있음)** 확정. `sign`/`change`/`diff` 등 부가필드 다수(미사용).
- **t0425**: 미체결0이면 응답에 `t0425OutBlock1` 키 **자체가 없음**(빈 list 아님) — 코드 `body.get("t0425OutBlock1") or []`로 안전 처리됨. ⚠ 체결행 status·필드는 **C-1(--order)** 에서 확정.

---

## 공식문서 대조·KIS패리티 검토 (2026-06-19, 키발급 전 cross-source) — 신규 확정/수정

아래는 공식(openapi.ls-sec.co.kr), teranum/ls-openapi-samples, gobenpark/lssec-go,
k-im-minsu/k-ebest-im, programgarden-finance, xingAPI .res 스키마 등 복수 소스를 교차 확인한 결과다.
🟢=크로스소스 확인 / ⚠=키 발급 후 실측 필요.

---

### G11 — AcntNo/InptPwd 주문 body 포함 여부 ⚠ 키검증

- **상황**: REST 래퍼(lssec-go·programgarden)는 `AcntNo`·`InptPwd`를 **생략**(Bearer 토큰에서 계좌 추론). xingAPI legacy는 포함.
- **현 코드**: `CSPAT00601InBlock1`에 `AcntNo`·`InptPwd` 포함 중.
- **위험**: 엄격 스키마면 여분 필드로 400 거부 가능성(미확인).
- **근거**: gobenpark/lssec-go REST wrapper 직독 — OrderNewOption 구조에 AcntNo 없음.
- **확정 방법**: ⚠ 모의 키 첫 주문 응답(성공/400) 확인 후 생략 여부 결정.

---

### G12 — OrdPrc 타입: 스펙 double, 코드 int 전송 ⚠ 무해 추정

- **상황**: CSPAT00601 스펙은 `OrdPrc`를 double(13.2)로 정의. 현 코드는 Python int를 JSON으로 직렬화 → JSON 정수.
- **추정**: JSON 정수는 서버가 double로 읽으므로 대개 무해.
- **근거**: 타입 불일치가 이슈가 된 커뮤니티 보고 없음.
- **확정 방법**: ⚠ 모의 키로 지정가 주문(OrdPrc=정수) 성공 확인.

---

### G13 — mac_address 헤더: 법인 필수, 개인 불필요 🟢 크로스소스

- **내용**: `mac_address` 요청 헤더는 법인 계좌에만 필수. 개인 계좌(현 타깃)는 불필요 → 미포함 무방.
- **참고**: KIS의 `hashkey` 개념은 LS에 **없음** — 헤더에 hashkey 포함하지 않는다.
- **근거**: programgarden-finance 개인 계좌 래퍼에 mac_address 헤더 없음; 공식 about-openapi 법인 전용 명시.

---

### G14 — rate limit: 일 5,000회 + 초당 ~2회 실측 🟢 크로스소스 / ⚠ 공식 미공개

- **내용**: 일 5,000회(커뮤니티 다수 언급) + TPS **~2회/s**(커뮤니티 실측, 공식 미공개). 현 코드(`_Throttle`)는 보수적 3/s로 설정됨. → **2/s로 낮추는 게 더 안전**(현 브랜치 M3에서 반영).
- **원래 G4(TPS 미확인)**의 TPS 부분을 이 entry가 업데이트한다. G4는 일 5,000회 근거로 유지.
- **근거**: k-im-minsu/k-ebest-im 이슈 스레드, programgarden-finance 코드 주석.

---

### G15 — t0424OutBlock 필드 의미 확정 🟢 크로스소스

- **내용 (이전 가정 교정)**:
  - `sunamt` = 추정순자산(주식 포함, **≠ 예수금**) → **킬스위치 손실감지에 쓰면 안 됨**(직전 draft 오류)
  - `mamt` = 매입금액(원가)
  - `tappamt` = 평가금액(시가) → **total_eval = tappamt**(이 브랜치 수정 완료)
  - `sunamt1` = 추정D2예수금 → **cash 근사값으로 사용**(직전 sunamt 사용은 순자산이라 과대사이징 위험)
  - `dtsunik` = 실현손익, `tdtsunik` = 평가손익
- **영향**: `total_eval`에 `mamt`(원가)를 쓰면 킬스위치 손실 감지 불가. `cash`에 `sunamt`(순자산)을 쓰면 매수 사이징이 잔고보다 커질 수 있음.
- **근거**: t0424.res xingAPI 스키마 + programgarden-finance 라이브 샘플 응답 필드 해설.

---

### G16 — 진짜 주문가능금액은 t0424에 없음 → CSPAQ22200 필요 🟢 크로스소스 / ⚠ 필드 키검증

- **내용**: t0424는 잔고/평가 위주이고 정확한 주문가능금액(D2entra 등 D+2 결제 기준 가용현금)은 별도 TR `CSPAQ22200`(현물계좌예수금)에 있다.
- **현 코드**: `sunamt1`(추정D2예수금)으로 근사 — 방향은 맞으나 CSPAQ22200보다 부정확.
- **Phase C**: CSPAQ22200 필드명·응답 구조를 키로 확인 후 cash 소스를 전환할지 결정.
- **근거**: lssec-go 예수금 조회 별도 메서드 존재 + INDEX.md 기타 항목.

---

### G17 — 주문 성공코드: 조회 "00000" ≠ 주문 "00039/00040" ⚠ 코드표 키검증

- **내용 (G1 교정)**: **CSPAT006xx(주문) TR은 성공 시 `rsp_cd="00040"`(매수) / `"00039"`(매도)** 반환. `"00000"`은 *조회* TR 전용 성공코드다 — 주문 응답에 "00000"을 체크하면 **항상 실패 판정**한다.
- **현재 수정**: 특정 코드 대신 **OutBlock2의 `OrdNo` 존재**로 성공 판정 → 코드값 불확실성과 무관하게 견고.
  (`normalize_ls_order_resp`의 성공 판정을 OrdNo 기준으로 전환, `_RSP_OK` 상수 사용 제거 — 이 브랜치 수정 완료.)
- **근거**: programgarden-finance 주문 응답 해설("매수=00040, 매도=00039").
- **확정 방법**: ⚠ 키로 주문 1회 발행해 실제 rsp_cd 값 확인(정확 코드표).

---

### G18 — 주문 경로: 단일 POST /stock/order (취소도 같음) 🟢 크로스소스

- **내용**: 신규(`CSPAT00601`)·정정(`CSPAT00701`)·취소(`CSPAT00801`) **모두 `POST /stock/order`**, `tr_cd` 헤더로 TR 구분. `/stock/order-cancel` 같은 별도 경로는 **존재하지 않음**(404).
- **근거**: gobenpark/lssec-go `OrderCancelOption.Path()` → `/stock/order` 반환.

---

### G19 — t0425OutBlock1 체결/미체결 구분: status + chegb="0" 🟢 필드 확인 / ⚠ status값 키검증

- **내용 (G10 해결 경로 업데이트)**:
  - `t0425OutBlock1`에 `status` 필드(char10)가 존재한다는 것이 크로스소스로 확인됨.
  - `chegb="0"`(전체조회)로 호출하면 체결·취소 행도 결과에 포함되고 `status`로 구분 가능.
  - **G10의 "체결/취소 인지 불가" 문제의 해결 경로가 확정됨**: `order_status`를 `chegb="0"` 전환 + `status` 값으로 filled/cancelled 판별.
  - **단, `status`의 실제 값**("접수"/"체결"/"취소" 등 정확 문자열)은 ⚠ 키 발급 후 실측 필요.
  - **Phase C 전까지 변경 금지**: status 값 오독 시 취소→체결 오인 위험.
- **근거**: xingAPI .res 스키마에 status(char10) 필드 정의 확인.

---

### G20 — t0425OutBlock1 필드 의미 교정 🟢 크로스소스

- **내용 (draft 필드명 교정)**:
  - `cheprice` = 체결가 (≠ `price` = 주문가 — 주문가와 체결가는 다름)
  - `price1` = 현재가
  - `medosu` = `"매수"` / `"매도"` **문자열** (정수 코드 아님)
  - `hname` **없음**: 종목명(hname)은 t0424OutBlock1에만 있음. t0425에서 종목명을 꺼내려면 별도 조회 필요.
- **fill_price는 `cheprice` 사용** (이 브랜치 M1 수정 완료 — 직전 `price` 사용은 주문가를 체결가로 오인).
- **근거**: teranum/ls-openapi-samples t0425 응답 예시 + xingAPI .res 필드 정의.

---

### G21 — t1102: 경로·필드·KOSDAQ exchgubun 🟢 3소스 일치 / ⚠ KOSDAQ

- **내용**:
  - 경로: `POST /stock/market-data`
  - InBlock: `shcode` (bare 6자리, "A" 접두사 없음)
  - OutBlock: `price`=현재가, `open`=시가, `high`, `low`, `volume`, `recprice`=전일종가 (**≠ `pclose`** — KIS의 stck_prdy_clpr와 이름 다름)
  - `exchgubun` 선택 InBlock 파라미터, 기본 `'K'`=KOSPI
  - **⚠ KOSDAQ 종목**: `exchgubun='Q'` 필요 가능성 있음 — 미지정 시 KOSDAQ 시세가 KOSPI 조회로 잘못될 수 있음. 키 발급 후 KOSDAQ 종목(예: 035720 카카오)으로 확인.
- **근거**: teranum/ls-openapi-samples t1102.py + gobenpark/lssec-go + k-im-minsu/k-ebest-im 3소스 일치.

---

## 초안 수록 (2026-06-17, A2) — 키 미발급 상태 가정 목록

아래는 라이브 확인 전 "가정" 수준의 함정들이다. 🟢=공개 소스 확인, ⚠️ 가정=추론.

---

### G1 — rsp_cd 성공 코드 정확값 ⚠️ 가정 → **G17에서 교정됨 (2026-06-19)**

- **가정(원문)**: 성공 시 `rsp_cd = "00000"` (5자리 "0")
- **교정(G17)**: 이 가정은 *조회* TR에만 적용된다. 주문 TR은 `"00039"/"00040"` 반환. 현 코드는 OrdNo 기준으로 수정 완료.
- **근거**: LS 공식 howto-sample 페이지에서 Standard Response 섹션이 `"00000"=success`로 명시 (조회 전용)
- **확정 방법**: 키 발급 후 주문 1회 발행해 실제 rsp_cd 값 확인.

---

### G2 — 모의/실전 단일 도메인 키 라우팅 ⚠️ 가정

- **가정**: LS는 KIS와 달리 모의·실전 모두 `openapi.ls-sec.co.kr:8080` **동일 도메인** 사용.
  appkey/appsecretkey가 모의용이면 모의, 실전용이면 실전으로 라우팅.
- **근거**: 커뮤니티 Python wrapper(`ebest` 패키지)가 도메인 분기 없이 단일 URL 사용하며,
  `api.is_simulation` 속성으로 서버 환경을 구분하는 것이 확인됨.
- **주의**: KIS처럼 `openapivts.*` 같은 별도 모의 도메인이 없다면 LsBroker에서 도메인 분기 불필요.
  단, 키 발급 후 공식 문서 재확인 필수.
- **확정 방법**: 모의 키 발급 후 단일 URL 호출 성공 여부.

---

### G3 — 토큰 만료: 익일 07:00 KST (절대 시각) ⚠️ 가정

- **증상**: `expires_in`을 "초 후" 만료로 잘못 계산하면, 익일 07:00 이전에 토큰이
  유효한데도 재발급을 시도하거나, 07:00 이후에도 만료를 모른다.
- **가정**: 토큰은 "신청일 익일 07:00 KST"까지 유효. `expires_in`은 발급 시점 기준 남은 초.
  즉, 23:00 발급 시 `expires_in ≈ 8*3600 = 28800`.
- **근거**: LS 공식 howto-sample 설명("접근토큰 유효기간은 개인/법인: 신청일로부터 익일 07시까지")
- **처리 방안**: KIS처럼 `expires_at = now + timedelta(seconds=expires_in)`으로 저장.
  단, KIS는 24h 고정이고 LS는 가변(발급 시각에 따라 다름).
- **확정 방법**: 키 발급 후 23:00~01:00 구간 토큰 유효성 실측.

---

### G4 — rate limit: 일 5,000회, TPS 미확인 ⚠️ 가정

- **가정**: 일 API 호출 한도 5,000회.
- **근거**: 커뮤니티 검색 결과 다수에서 "5,000/일" 언급. 공식 문서 직접 확인 안 됨.
- **TPS(초당 한도)**: 미확인. KIS는 초당 20회. LS는 관련 공식 문서 접근 불가.
- **주의**: 폴링 루프(미체결 주기적 조회 등)는 일 한도를 고려해 간격 설정.
- **확정 방법**: 키 발급 후 공식 문서 확인 또는 빠른 연속 호출 시 에러 코드 관찰.

---

### G5 — tr_cont 연속 조회 방식 (KIS와 유사하나 차이 있음) 🟢 부분 확인

- **확인 사항**: 요청 헤더 `tr_cont="N"` (초회), `tr_cont="Y"` (연속). `tr_cont_key`는 연속 시 이전 응답 값 사용.
  `t0424InBlock`의 `cts_expcode` 필드가 연속 조회 키 역할도 함.
- **차이점**: KIS는 응답 헤더로 연속 여부 반환. LS는 OutBlock 내 `cts_*` 필드가 연속 키.
- **주의**: 잔고가 많은 계좌는 t0424가 page-1만 가져온다 — `cts_expcode` 비어있으면 마지막 페이지.
- **확정 방법**: 실계좌에서 잔고 100종목 이상 시 연속 조회 동작 확인.

---

### G6 — 계좌번호 형식 ⚠️ 가정

- **가정**: LS 계좌번호는 CSPAT00601 `InBlock1.AcntNo`에 공백 없이 전달(최대 20자).
  xingAPI 기준 계좌번호 구분 없이 단일 필드.
- **주의**: KIS는 계좌번호 8자리 + 상품코드 2자리 분리(`CANO`+`ACNT_PRDT_CD`).
  LS는 CSPAT00601 샘플에서 `AcntNo` 단일 필드로 처리함.
- **비밀번호**: `InptPwd` (8자리) — 잔고·주문 TR에 매번 포함 필요. ⚠️ 전 TR에 공통 요구 여부 미확인.
- **확정 방법**: 키 발급 후 계좌번호 형식·InptPwd 요구 여부 확인.

---

### G7 — 매매구분(BnsTpCode) 코드값 🟢 확인

- **확인**: `BnsTpCode = "1"` 매도, `"2"` 매수
- **근거**: `ls-openapi-samples/14.주식잔고-미체결-주문.py` 직독:
  `'BnsTpCode': '2' if 주문요청 == '1' else '1'` (1번입력=매수→BnsTpCode=2)
- **주의**: KIS는 매수/매도 TR_ID가 다름(TTTC0012U/TTTC0011U). LS는 동일 TR에 BnsTpCode 구분.

---

### G8 — 호가유형코드(OrdprcPtnCode) 값 🟢 부분 확인

- **확인**: `"00"` = 지정가, `"03"` = 시장가
- **근거**: `ls-openapi-samples/14.주식잔고-미체결-주문.py` 주석 및 샘플 코드 직독.
- **주의**: 그 외 코드(IOC·FOK·최유리 등) 값은 ⚠️ 미확인. xingAPI legacy와 동일할 가능성 있으나 실측 필요.

---

### G9 — 종목코드 형식: "A" + 6자리 ⚠️ 가정(모의)

- **가정**: IsuNo 필드에 종목코드를 `"A" + 종목코드 6자리`로 입력 (예: `"A005930"`).
  주석에 "주식/ETF: 종목코드 or A+종목코드(모의투자는 A+종목코드)"라고 기재됨.
- **근거**: `ls-openapi-samples/14.주식잔고-미체결-주문.py` 직독:
  `'IsuNo': 'A'+종목코드`
- **주의**: 실전에서는 `"A"` 접두사 없이 6자리만 가능할 수 있음. 실전 키 확인 전까지 "A" 포함 사용.
- **확정 방법**: 실전 키 발급 후 양 형식 테스트.

---

### G10 — order_status 체결/취소 인식: t0425 chegb="2"는 미체결만 (B6 발견) ⚠️ 한계

- **증상**: `LsBroker.order_status`가 t0425를 `chegb="2"`(미체결만)로 조회한다. 주문이 **전량 체결**되거나 **취소**되면 그 행은 t0425 미체결 목록에서 **사라진다**(KB t0425 "이미 체결된 건은 안 나옴"). → 매칭 실패 → `status="unknown"` 반환.
- **영향**: 폴링 경로로는 LS **체결을 인지하지 못한다**(`remain_qty==0 → filled` 분기가 라이브에선 도달 불가, 합성 테스트에서만 발화). 체결 반영은 15:50 정산 `reconcile_with_kis`(브로커 실보유 ↔ ledger diff)가 **백스톱**으로 잡는다 — 자금/방향 위험은 없으나 폴링 기반 즉시 인지가 안 됨. 취소(`cancelled`)도 같은 이유로 인지 불가 → Trader의 cancelled 분기(미체결 종가 자동취소 처리) 미발화.
- **원인**: KIS는 `inquire-daily-ccld`(체결+미체결+취소 통합)로 해결. LS는 미체결-only TR을 재사용.
- **해결 방향 (Phase C, 키 발급 후)**: `order_status`의 체결 판정을 t0425 `chegb="0"`(전체) 또는 일별체결 TR로 전환해 체결/취소 행이 보이게 한다(`pending_orders`는 `chegb="2"` 유지). **단 `chegb="0"`에서 filled(remain=0)와 cancelled(remain=0)를 구분하는 상태 필드가 무엇인지 실측 필요** — 이 구분 없이 추측하면 취소를 체결로 오인할 수 있어, 키 없이는 변경하지 않고 현 graceful degradation(unknown→정산 백스톱) 유지.
- **확정 방법**: 모의 키 발급 후 체결·취소 1건씩 발생시켜 t0425 chegb="0" 응답에서 filled/cancelled 구분 필드 확인.

---

## 향후 발견 시 여기에 추가

새 발견 시 이 섹션 **위**에 날짜·증상·원인·해결을 추가한다.

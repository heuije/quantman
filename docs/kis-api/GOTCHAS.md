# KIS API GOTCHAS — 실측 발견사항

공식 docs와 다른 동작·알려진 한계·workaround. 작업 전 한 번 훑기.
새 발견 시 **위에 추가** (최신순). 각 entry: 날짜·증상·원인·해결.

---

## 2026-06-12 — 체결 확인은 발주 직후 단일 조회로 안 잡힌다 (모의 ~27초 지연·종가 단일가)

- **증상**: 선물 종가청산(15:40 발주, 0000004525)이 종가 단일가(~15:45)에 정상
  체결됐는데 status 조회가 발주 직후 1회뿐이라 'unknown' → pending 박제·ledger
  미정리. 18:54 수동 재실행으로야 fill 1299.85 확인(+24.9M 기록).
- **실측**: 모의투자는 시장가 즉시 주문도 체결 반영까지 **~27초** 지연(08:55:35
  발주 → 08:56:02 체결 확인). 동시호가 발주는 단일가 확정 시각(시초가 09:00·주식
  종가 15:30·선물 종가 15:45)까지 체결 자체가 없다 — 발주 직후 조회는 구조적으로 0건.
- **해결**: 모든 발주 경로는 `_wait_pending`(60s/20s 폴링)을 거치고, 발주창 **이후**
  정산(resolve+reconcile) 패스가 반드시 오도록 cron 배치(정산 15:35→15:50, θ).
- **우리 코드**: `trader.py` `liquidate_day_trades`(wait+선행 resolve) ·
  `scheduler.py`(krx_settlement 15:50) · `docs/incidents/2026-06-12-futures-close-fill-unrecorded.md`.

---

## 2026-06-12 — 해외 체결조회(inquire-ccnl): 주문일자는 미국 현지 날짜 + 예약주문 번호공간 불일치

- **증상 1 (RC1)**: KST 04:55 발주한 GOOG 매도(odno 0000040620)가 체결됐는데(KIS 보유
  263→0) `_overseas_ccnl_today`(VTTS3035R/TTTS3035R, ORD_STRT/END_DT=KST 당일)가 **0행**
  → status 'unknown' → 체결 영구 미기록 → settlement reconcile이 보유 diff로 "외부 매도
  추정" 제거(정산손익·전략연결 소실).
- **원인 1**: 체결행의 주문일자가 **미국 현지 날짜**(해당 건 20260611). KST 자정~미장마감
  (≈06:00) 구간은 KST 날짜가 하루 앞서 당일 조회가 빗나간다. 22:30~24:00 KST(미장 초반
  1.5h)만 날짜가 일치 — 그 시간대 체결만 잡히던 것.
- **해결 1**: 조회창 [미국 동부 D-1, KST 오늘] (`_overseas_query_window`). 오래된 pending은
  제출일(동부) D-1로 시작일 확장. ⚠ CTX 연속조회는 여전히 미구현(첫 페이지만).
- **증상 2 (RC2)**: 예약매수 접수 odno **448**(3자리, order-resv 응답)이 체결됐는데 체결행
  odno(10자리 주문 번호공간)와 불일치 → odno 정확 매칭이 영원히 실패 → 'unknown'.
- **원인 2**: 예약주문접수(TTTT3014U/3016U) 응답 ODNO는 **예약 번호공간** — 개장 시 자동
  전송되며 새 주문번호가 발급되고, 예약↔본주문 번호를 잇는 조회 TR은 미배선(예약내역
  조회 TTTT3039R 미사용).
- **해결 2**: `_overseas_order_status`가 hint(side·qty·reserved·exclude_odnos)를 받아
  예약주문은 **종목+매수매도+수량**으로 체결행 매칭. 청구한 체결행 odno를
  `claimed_fills.json`에 영속해 동형 주문/사이클 간 이중 기장 차단.
- **우리 코드**: `kis_broker.py` `_overseas_query_window`·`_overseas_ccnl_today`·
  `_overseas_order_status` · `trader.py` `_resolve_pending_locked`(hint·청구 레지스트리·
  7일 GC) · 테스트 `local/tests/test_overseas_fill_detection.py`.

---

## 2026-06-11 — 미국주식: 신선한 현재가 지정가 + 예약매도 지정가 통일 + 종가청산 (v0.9.35)

- **배경**: 미국주식은 KIS가 연속장 시장가를 미지원(지정가/LOO만)이라 국내 시장가화의
  대상이 아니다. 대신 미국 발주를 **지정가 + 신선한 현재가 + 모의=실전 통일**로 정렬.
- **발견 1 — 진입가가 전일종가 기반이라 갭 미체결**: `_submit_buy`가 예약매수 limit을
  `prev_close × (1+tol)`로 잡았는데, 전일종가(≈17h 전)와 다음 개장 사이 애프터/프리마켓
  갭이 tol을 넘으면 시초가에 미달→미체결. → `_us_limit`이 `_safe_price`(HHDFS00000300
  실시간/프리마켓)×(1±tol)로 발주(전일종가는 조회실패 fallback). **사이징은 prev_close 유지**(패리티).
- **발견 2 — 예약매도 MOO(31) 모의 미검증**: 예약주문접수(VTTT3016U) 매도는 00/31을
  열어두나, MOO(31)의 모의 실접수는 미검증 게이트였다. 모의=실전 통일 위해 예약 매수·매도
  **둘 다 00 지정가**(`buy_resv_limit`/`sell_resv_limit`)로 고정. 매도 limit=신선한가×(1−tol).
- **발견 3 — 미국 종가청산 사이클 부재**: 미국 당일매매 보유분 청산 cron이 없어 다음 개장
  MOO로 하루 늦게 청산됐다. → `run_close_cycle(market="US")`를 스케줄러 **폐장−5분**에 등록
  (신규 Trader라 `_reserved_us`=False → 라이브 `sell_limit`). MOC 모의 미지원이라 연속장
  막판 지정가가 최선의 종가 근사(백테스트 종가와 미세 발산은 불가피).
- **tolerance**: 미국 전용 라이브 버퍼. default ±3%(종전 1%/2%는 미국 갭에 타이트).
  전략 execution(`buy/sell_tolerance_pct`)으로 유저 override. 국내는 시장가라 미사용.
- **우리 코드**: `trader.py` `_us_limit`·`_submit_buy/_submit_sell`(USD 분기) ·
  `kis_broker.py` `sell_resv_limit`·`_submit_overseas_resv`(00 고정) · `scheduler.py`
  `_plan_us_session`(us_close_cycle) · `core/quant_core/exec_defaults.py`(tol 3.0).
- ⚠ **라이브 실측 게이트**(다음 미국 세션): 예약 모의 접수·전송·체결 / `_safe_price`가
  프리마켓(개장−20분)에 실가를 주는지 / 폐장−5분 종가청산 라운드트립. 확인 후 여기 기록.

---

## 2026-06-11 — "모의투자 상/하한가 오류"(선물) = 실시간가격제한(±1%) 위반 포함

- **증상**: 코스피200선물 모의 지정가 주문이 정적 상·하한(futs_mxpr/llam, ±8%)
  **안쪽**인데도 "모의투자 상/하한가 오류"로 거부.
- **원인**: KRX 파생 **실시간가격제한** — 직전약정가 ±1%(코스피200선물)를 넘는
  호가는 거래소/모의서버가 즉시 거부. 에러 메시지가 정적 상하한과 동일해 오인 유발.
  실측 2건: 지정가 1231.30(밴드 1231.25)·1247.55(밴드 1247.50) — 현재가×1.01을
  틱 *올림*하면 정확히 1틱 초과.
- **해결(최종, v0.9.34)**: 국내(주식·선물) 발주를 **시장가 단일로 전환** — 가격을
  지정하지 않으므로 ±1% 밴드 위반 자체가 불가(시장가호가는 실시간가격제한가에 의제
  접수). 시장가는 동시호가 단일가(시초가/종가)에 체결돼 지정가 대비 슬리피지 손해도
  없다. (중간 시도였던 `_live_limit` ±1% 밴드 클램프는 국내 지정가 제거로 함께 삭제.)
- **우리 코드**: `local/localapp/trader.py` `_submit_buy/_submit_sell` else 분기(시장가) ·
  회귀 `local/tests/scenarios/test_order_price_plane.py`.
- ⚠ **시장가 미체결 잔량 처리**(국내선물/해외선물)는 KB 미기재 — 실측 필요.
- ⚠ 미국주식은 KIS 연속장 시장가 미지원 → 지정가(예약) 유지(별개 경로).

---

## 2026-06-05 — KIS 선물옵션 API 표면 도입 (6 xlsx 추출 — 일부 미실측 ⚠)

선물옵션(국내·해외) 6개 raw xlsx를 `raw/`에 보존하고 INDEX에 80 endpoint 색인.
아래는 **시트 명세 추출** 기준 — runtime 실측 전 항목은 ⚠로 표시(추측 완료 금지, PR-4 예방).

1. **해외선물옵션 전 API 모의투자 미지원** (실전 계좌 전용) → paper trading 불가.
   자동매매 검증은 (a) 마이크로 1계약 실전 또는 (b) 자체 MockBroker로만 가능.
   대조: **국내선물옵션은 핵심 5종(주문/정정/체결내역/잔고/주문가능) 모의 지원**(`V…`).
2. **해외 CME·SGX 시세 유료** (HTS/MTS 가입 후 익일부터) → 미가입 시 시세 자체 미수신.
   해외 시세 수치는 종목마스터(`ffcode/focode/fostkcode.mst`) `sCalcDesz`(계산소수점) 적용 필요.
3. **국내선물 주문은 hashkey 헤더 불필요** (주식 주문과 다름). order body에 hashkey 없음.
4. **백테스트 데이터 한계**:
   - 국내: `FHKIF03020100`(일/주/월/년봉) **모의 지원** → 일봉 백테스트 데이터 확보 가능.
     단 과거 보관 깊이(몇 년치) 시트 미기재 → ⚠ 실측 필요.
   - 해외선물: 분봉 120/콜·틱 40/콜, `QRY_TP=P + INDEX_KEY` 페이지네이션으로 누적 수집.
   - **해외옵션 일/주/월봉은 "최근 120건"만**(고정 한계), 해외옵션 분봉은 CLOSE_DATE_TIME 무시.
   - 해외선물 일/주/월봉 1회 건수·총 보관기간 ⚠ 시트 불명확(실측 필요).
   - 만기물별 과거 데이터(만료 계약) 제공 여부 ⚠ 미확인 — yfinance 때처럼 만료 계약 0행
     가능성. 정확한 롤 비용·연속물 구성에 영향. 장기 백테스트엔 전용 벤더 검토 여지.
5. ⚠ **국내 야간 TR 신/구 병기**: 야간 주문·조회가 (신)`STTN…`/`CTFN…` 와 (구)`JTCE…` 병기 —
   어느 쪽이 현재 유효한지 시트만으론 단정 불가.
6. ⚠ **KRX야간 체결통보 TR_ID/path 표기 불일치**: 시트상 `H0MFCNI0` ↔ path `/tryitout/H0EUCNI0`,
   야간옵션·야간선물 체결통보가 `H0MFCNI0`로 중복 기재 — 사용 전 실측 필요.

**대응방향**: 백테스트는 국내(모의 일봉)부터 검증 가능. 해외는 시세 유료·과거 깊이
블로커를 먼저 해소(또는 전용 데이터 벤더). 자동매매는 국내(모의 paper)부터.

**우리 코드**: 아직 미사용 (Phase 0 = 지식 기록). endpoint `.md`는 백테스트/매매가
실제 endpoint를 wire할 때 작성(README 규칙). raw: `raw/국내선물옵션_*.xlsx`·`raw/해외선물옵션_*.xlsx`.

---

## 2026-05-28 — 해외 체결통보 `H0GSCNI0` 별도 구독 필요 (국내 H0STCNI0과 다름)

**증상**: 미장 catch-up 매수 4건이 KIS에선 정상 체결됐는데 우리 `orders.jsonl`에
filled 이벤트 0건, `pending_orders.json` stale. cycle summary `n_bought=0`.

**원인**: 우리 옛 `KisOrderWebSocket` (v0.9.11까지)은 `H0STCNI0/H0STCNI9` (국내주식
실시간체결통보)만 구독. **해외주식은 `H0GSCNI0/H0GSCNI9` 별도 endpoint** —
fields 25개·키 이름 일부 다름·미국 종목 체결단가는 4자리 packed (`'001480100'` →
148.01). 옛 코드 line 92 `self._tr_id = "H0STCNI9" if broker.virtual else "H0STCNI0"`
에 해외 분기 없음.

**해결** (v0.9.12):
- `EXEC_FIELDS_OVERSEAS` 신규 + `_decode_us_price` 함수
- `KisOrderWebSocket(market=...)` 인자 — KR/US 시장별 tr_id·fields·decode 분기
- `intraday_loop`에서 시장에 따라 `market=` 전달

**우리 코드**: `local/localapp/kis_order_websocket.py:91-103` (`__init__`), `:174-188`
(`_handle_exec_message`). `local/localapp/intraday_loop.py:415-422`.

---

## 2026-05-28 — `inquire-present-balance` `output3.frcr_evlu_tota` 필드 의미 mismatch

**증상**: 사용자 보유 ~$39K (3종목)인데 `foreign_eval_krw` 응답 ₩319,541,151
(=$213K equivalent). 실제 환산 ₩59M와 ~5.4배 차이.

**원인**: KIS docs description 부재 + 실측 mismatch. "외화평가총액"이라는 필드명만
있고 환산 식 불명. 매수원가 + 평가 + 신용한도 등 합산 가능성 (확정 못 함).

**해결** (v0.9.12): KIS 필드 의문 회피 — 직접 계산.
`foreign_eval_krw = (usd_cash + Σ qty·eval_price) × fx`. 모든 보유 USD 가정.

**검증** (사용자 PC 실측):
- positions_eval_usd: $39,421.17, fx: 1,501.60
- 새 식 결과: ₩59,194,829 (= USD × fx)

**우리 코드**: `local/localapp/kis_broker.py:286-329` (`overseas_snapshot`).

---

## 2026-05-28 — `HHDFS00000300` 응답에 OHLC 없음 (open 필드 X)

**증상**: trader catch-up이 `broker.today_open(symbol)` 호출 시 6 종목 모두 0.0 반환 → 매수 0건.

**원인**: 우리 옛 코드(`_open_overseas`)가 `HHDFS00000300` (해외주식 현재체결가) 호출. 실측한 응답 11 fields에 `open` 필드 자체 없음:
```
output: [base, diff, last, ordy, pvol, rate, rsym, sign, tamt, tvol, zdiv]
        ↑ 전일종가 ↑ 현재가 ↑ 거래량
        open / high / low ✗ 모두 미제공 (doc spec상 보장 안 함)
```

**해결**: `HHDFS76200200` (해외주식 현재가상세)로 교체 — 41 fields, open/high/low 모두 정상 제공. [v0.9.7-beta fix](../../local/RELEASE_NOTES_v0.9.7-beta.md).

**우리 코드**: `local/localapp/kis_broker.py::_open_overseas`

---

## 2026-05-28 — `quote_base`는 모든 사용자 실전 도메인 (모의/실전 무관)

**증상**: doc에 "모의 미지원"이라 표시된 시세 endpoint가 모의 사용자도 동작.

**원인**: `KisBroker.__init__`이 `self.quote_base = _REAL` 고정. 시세는 KIS 정책상 모의·실전 사용자 모두 실전 도메인 호출. 즉 모의 appkey + 실전 도메인 조합.

**결과**:
- 일부 시세 endpoint (`HHDFS76200200`·`HHDFS76950200` 등): 모의 appkey도 실전 도메인에서 정상 응답 (rt_cd: 0)
- 일부 (`FHKST03030100` 일별차트): 거부 — "실전투자 도메인은 모의투자 앱키로 호출 불가" (HTTP 500 EGW02004)

**해결**: doc만 보지 말고 실측. INDEX.md "모의" 컬럼은 실측 결과 기준 갱신.

**우리 코드**: `local/localapp/kis_broker.py:95-96`

---

## 2026-05-28 — `HHDFS76950200` 분봉 NREC 한도 ~120봉

**증상**: NREC=400 명시 호출해도 응답 분봉 ~120개. 미장 4시간+ 진행 시 09:30 EDT 시초가 분봉 도달 불가.

**원인**: KIS 분봉 endpoint의 응답 한도는 최근 N개 (실측 ~120봉). NREC param이 상한이 아닌 hint 정도. 시초가 분봉 받으려면 NEXT/KEYB로 페이징 필요 (복잡).

**해결**: 시초가 받기엔 부적합. **`HHDFS76200200` 현재가상세의 output.open이 단일 호출로 시초가 제공** — 그것 사용.

**우리 코드**: 분봉 endpoint는 우리 자동매매에서 미사용. 시초가는 HHDFS76200200으로 대체.

---

## 2026-05-28 — Transfer-Encoding chunked + `requests.r.raw` 충돌

**증상**: 서버 dataset bundle (Railway, `Transfer-Encoding: chunked`) 디코드 시 `zstandard: Unknown frame descriptor`.

**원인**: `requests.get(stream=True).raw`로 chunked response 받으면 chunk header (hex digit + CRLF)가 raw bytes에 섞임. zstd magic (`28 b5 2f fd`) 앞에 hex digit이 끼어 디코드 실패.

**해결**: `r.iter_content(chunk_size=...)`로 chunk를 자동 de-chunk 처리해서 임시파일 거쳐 디코드. [v0.9.5-beta fix](../../local/RELEASE_NOTES_v0.9.5-beta.md).

**우리 코드**: `local/localapp/sync_client.py::fetch_dataset_bundle`

⚠ KIS API 직접 관련 아니나, KIS·외부 API 모두 동일 패턴 위험.

---

## 2026-05-28 — KIS 모의투자는 일부 시세 API에서 일부 필드 누락

**증상**: 같은 endpoint·같은 응답 구조라도 모의 사용자는 일부 필드 빈 값.

**원인**: KIS 모의투자 환경의 시세 데이터 한계. 종가·전일종가·당일거래량은 OK, 시초가·고가·저가는 누락 가능.

**해결**:
- 모의에서 OHLC 받으려면 OHLC 명시 제공하는 endpoint 선택 (`HHDFS76200200`)
- 또는 prev_close 기반 fallback 설계 (PR-1 정당 — KIS 한계)

---

## 작성 가이드

새 entry 추가 시:
1. 위에 (최신순) 추가
2. **날짜** + **증상** (사용자가 본 현상) + **원인** (root cause) + **해결** (어떤 endpoint·코드로 회피)
3. **우리 코드** 섹션 — `file:line` 형식 reference
4. fix가 release에 들어갔으면 [RELEASE_NOTES](../../local/) link

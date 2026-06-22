# LS증권 4자산군 일괄 모의 E2E 라이브 테스트 런북

> 다음 정규장에 이 문서를 위→아래로 따라 돌린다. **모든 LS 응답 필드·심볼 마스터는 research 기반(미실측)** —
> 이 E2E의 목적은 그 가정을 **실측으로 확정**하고 4자산군 paper 라운드트립(발주→체결인지→잔고→청산)을
> 검증하는 것이다. 완료선언은 이 E2E 통과 후(4원칙#4).
>
> 자매 문서: 국내주식 단독 런북 `PHASE-C-LIVE-TEST.md`(Phase C). 이 문서는 그것을 4자산군으로 확장한다.
> 구현 현황·gap 정의: `docs/ls-api/{domestic-futures,overseas-stock,overseas-futures}-research.md`.

---

## 0. 사전 준비 (테스트 전 1회)

### 0-1. 모의 계좌·키 (LS OPEN API는 **계좌단위**)
4자산군은 LS에서 **서로 다른 계좌**일 수 있다. 각 자산군마다 모의 계좌 + 그 계좌의 appkey/secret이 필요하다:

| 자산군 | 필요 모의 계좌 | 자격증명 슬롯(GUI 등록) |
|---|---|---|
| 국내주식 (C) | 모의 주식계좌 | LS증권(주식) — `load_ls` |
| 해외주식 (E) | 위 **주식계좌에 해외주식 권한**(보통 동일 계좌·동일 키) | 〃 (E는 같은 LsBroker) |
| 국내선물 (D) | 모의 **선물옵션계좌** | LS 선물 — `load_ls_futures` |
| 해외선물 (F) | 모의 **해외선물계좌** | LS 해외선물 — `load_ls_overseas_futures` |

> ⚠ **확인 1순위(G-OF8 등):** 위 표의 "계좌 분리" 가정 자체가 LS 실태와 다를 수 있다. 만약 한 appkey로
> 모든 계좌에 접근되면(같은 토큰) 슬롯은 같은 키·다른 계좌번호가 된다. **모의 신청 화면에서 어떤 계좌가
> 발급됐는지부터 확인**하고, 없는 자산군은 그 자산군만 건너뛴다(나머지는 독립적으로 진행 가능).

### 0-2. 자격증명 등록
`python desktop.py` → 설정 wizard → 각 자산군 키 입력·저장. (`run.py setup`은 KIS 전용 — LS는 GUI wizard.)
등록 안 된 자산군의 프로브는 자동 skip된다.

### 0-3. 워크트리 (이 코드 정본)
이 E2E는 **`_wt-ls-phasec` 워크트리의 소스로 실행**한다(`feat/ls-broker-phase-c`, 미push·미배포).
배포된 `MyStock.exe`엔 이 코드가 없다. 로컬앱 GUI도 이 워크트리에서: `cd local && python desktop.py`.

---

## 안전 게이트 (전 단계 공통 — 위반 금지)

1. **모의 먼저.** 실전 키로는 `--order`를 돌리지 않는다(읽기 전용은 실전이어도 안전). 프로브가 실전+주문이면 'REAL' 명시 입력을 요구한다.
2. **최소 수량.** 주문은 **1주 / 1계약**만.
3. **읽기 → 주문 순서.** 먼저 Phase 0(읽기 전용)으로 잔고·시세·심볼해석을 실측 확정한 뒤에만 Phase 1(주문)로.
4. **장중에만.** 체결은 각 시장 정규장에만. 장외 발주는 미체결로 남아 정리 단계에서 취소된다.
5. **킬스위치 수동 감시.** Phase 0에서 **equity(평가금액)가 KRW로 정상 표시**되는지 먼저 확인(0이거나 비정상이면 주문 금지 — 거짓 청산 위험).
6. **한 번에 한 자산군.** 자산군별로 따로 돌려 raw를 깨끗이 캡처한다.

---

## Phase 0 — 읽기 전용 필드 캡처 (가장 안전·가장 중요)

주문 없이 각 자산군의 **잔고·시세·심볼해석 raw 응답**을 덤프해 ⚠ 필드 가정을 실측 확정한다.
대부분의 G-* gap(필드명·응답형상·KRW 환산)이 여기서 닫힌다.

```bash
cd local
python verify_ls.py                      # 국내주식 (C) — 기존
python verify_ls.py --futures            # 국내선물 (D) — 잔고·시세·resolver·마스터
python verify_ls.py --overseas-stock     # 해외주식 (E) — 해외잔고(FX)·US 시세
python verify_ls.py --overseas-futures   # 해외선물 (F) — 해외선물잔고(USD→KRW)·resolver·o3101
python verify_ls.py --all                # 등록된 자산군 전부 (읽기 전용)
```

각 출력에서 확인할 것:

### 국내주식 (C) — `verify_ls.py`
- `[2] 잔고` RAW: `t0424OutBlock` 필드명(`sunamt`=총자산·`sunamt1`=예수금) 일치? **total_eval가 0 아님**(현금 포함).
- `[3] 시세` RAW: `t1102OutBlock.price/open`. (Phase C에서 대부분 확정됨 — G11~G21.)

### 국내선물 (D) — `--futures`
- **잔고(2-TR 합성):** `CFOAQ50600OutBlock2`(`EvalDpsamtTotamt`=equity·`MnyOrdAbleAmt`·`FutsEvalPnlAmt`) + `t0441OutBlock1`(`expcode`·`medosu`·`jqty`). → **G-DF8 행형, equity 정상값.**
- **시세:** `t2101OutBlock.price/open` (focode).
- **resolver:** `resolve("코스피200선물")` → `101V6000`류 근월물 코드가 나오나? `t8432`/`t9943` 마스터의 `hname` 형식("F 2406" YYMM vs YYYYMM) → **G-DF9**(정규식 교정 근거).
- **체결 status 문자열:** `t0434` `status` 값 집합 → **G-DF3**.

### 해외주식 (E) — `--overseas-stock`
- **해외잔고:** `COSOQ00201OutBlock3`(`FcurrDps`=USD현금·**`BaseXchrat`=환율**) + `OutBlock4`(`ShtnIsuNo`·`AstkBalQty`·`FcstckUprc`·`OvrsScrtsCurpri`). → **foreign_eval_krw가 KRW로 정상**(USD×fx). → **OG6**(무계좌 시 200-빈응답 vs HTTP에러).
- **US 시세:** `g3101OutBlock.price/open` (keysymbol="82AAPL"). 시장가(03) 모의지원 여부는 주문 단계 → **OG3**.

### 해외선물 (F) — `--overseas-futures`
- **해외선물잔고(USD→KRW):** `CIDBQ03000OutBlock2`(`EvalAssetAmt`=USD equity) + `CIDBQ05300OutBlock2`(**`Xchrat`**=환율) + `CIDBQ01500OutBlock2`(positions). → **equity = USD×Xchrat가 KRW로 정상**. **Xchrat 행이 USD로 나오나** → **G-OF5**. (Xchrat≤0이면 코드가 raise→킬스위치 보류 — 정상 동작이나 거래 불가니 환율 수신 확인.)
- **resolver:** `resolve("금선물")` → `GC`+월물 코드가 나오나? **o3101 `BscGdsCd`가 CME root(CL/GC/…)와 같은지** → 최대 미지수. 다르면 resolve→None→발주 skip(안전하나 거래 불가) → 매핑 교정 필요.
- **체결 status:** `CIDBQ02400` `TrxStatCodeNm` 값 → **G-OF4**.

> **Phase 0 산출물:** 각 `===== RAW: ... =====` 블록을 통째로 복사해 전달 → 내가 ⚠ 필드를 코드에 실측 반영
> (fixture·필드명 교체, 불일치 시 정규식·매핑 교정). **이 단계만으로 G-DF3/8/9·OG6·G-OF4/5·resolver 매핑이 닫힌다.**

---

## Phase 1 — 자산군별 주문 라운드트립 (모의·장중·1주/1계약)

Phase 0로 필드가 확정된 뒤, 각 자산군의 발주→체결인지→잔고반영→청산을 검증한다.

```bash
python verify_ls.py --futures --order            # 국내선물 1계약 라운드트립
python verify_ls.py --overseas-stock --order     # 해외주식 1주 (미국 정규장: 한국시간 밤)
python verify_ls.py --overseas-futures --order   # 해외선물 1계약 (CME 거래시간)
# 국내주식: python verify_ls.py --order  (기존)
```

각 라운드트립이 검증하는 것:
- **발주 성공판정 = OrdNo/OvrsFutsOrdNo/RsvOrdNo 존재** (rsp_cd 아님). 정규화결과 `success=True`·`order_no` 채워짐 → **G17/G-OF13/OG2**.
- **체결 인지** — 폴링으로 `order_status`가 `filled`로 전이? 부분체결·취소 문자열 → **G-DF3/G-E2/G-OF4**.
- **잔고 반영** — `account_snapshot` positions에 방금 체결 포지션이 보이나(심볼=데이터셋 정규화).
- **청산** — 보유분 1주/1계약 반대매매로 정리. 미체결이면 취소.
  - ⚠ **해외선물 취소는 원주문일자(OrdDt) 필수** — 라운드트립이 매수응답의 `OrdDt`를 보관해 `overseas_cancel(order_no, symbol, ord_dt)`에 전달하는지 확인(**G-OF6**).
- **시장가 처리** — 해외주식은 코드가 지정가(00)+현재가로 발주(OG3 회피), 해외선물은 시장가(1) 직접. 거부(rejected)면 raw에서 사유 확인.

**시장 거래시간 주의:** 국내(주식·선물) 09:00~15:20(선물 15:45) · 미국주식 한국시간 23:30~06:00(서머타임 22:30~) · CME 해외선물 상품별 거의 24시간(점검시간 제외).

---

## Phase 2 — 전체 사이클 E2E (전략→ledger→종가청산)

브로커 단위(Phase 1)를 넘어, **실제 전략 사이클**이 LS로 도는지 검증한다. 특히 **당일매매(hold_days=0)**의
fill→ledger 기록→종가청산 전 흐름. (이게 Phase C에서 국내주식 G19로 확인하려던 핵심 — 4자산군으로 확장.)

1. 웹/GUI에서 각 자산군 1전략을 모의 배정(예: 국내주식=SK하이닉스 시가매수·종가매도 hold_days=0).
2. GUI 로컬앱(`python desktop.py`)을 LS 활성 브로커로 켜고 스케줄러 가동(또는 RUN_CYCLE_NOW).
3. 확인:
   - 시가 사이클: 발주→체결→`cycles.jsonl`/`orders.jsonl`/ledger에 기록.
   - **킬스위치**: equity 시계열이 KRW로 정상 누적(거짓 −98% 없음). 해외 자산군은 equity가 KRW 환산값.
   - 종가 사이클: 보유분 청산 발주→체결→ledger 반영. (당일매매가 다음날로 넘어가지 않는지.)
4. `~/.quant-platform/logs/localapp.log`·`cycles.jsonl`·`orders.jsonl` 확인(진단은 CLAUDE.md §9).

> Phase 2는 자산군마다 **하루 라운드(시가~종가)**가 걸린다. 국내주식부터(가장 검증됨) → 국내선물 → 해외.

---

## G-* gap → 실측 매핑 (이 E2E에서 닫는 것)

| gap | 자산군 | TR·필드 | 캡처 단계 | 교정 액션 |
|---|---|---|---|---|
| G-DF3 | 국내선물 | t0434 `status` 문자열 | Phase 0 `--futures` / P1 | filled/cancelled 분기 문자열 확정 |
| G-DF8 | 국내선물 | t0441 행형 | Phase 0 `--futures` | positions 필드 확정 |
| G-DF9 | 국내선물 | t8432 `hname` 형식(YYMM?) | Phase 0 `--futures` | resolver 월물 정규식 교정 |
| OG3 | 해외주식 | 시장가(03) 모의지원 | P1 `--overseas-stock --order` | 지원 시 quote→limit 단순화 |
| OG6 | 해외주식 | COSOQ00201 무계좌 응답(200빈 vs 에러) | Phase 0 `--overseas-stock` | 도메스틱전용 killswitch 보류 여부 |
| G-E2 | 해외주식 | COSAQ00102 `OrdTrxPtnNm` | P1 | 부분체결/거부 문자열 |
| G-E3 | 해외주식 | COSAQ00102 매수/매도 필드 | Phase 0/P1 | pending side 필드명(BnsTpCode?) |
| OG-E1 | 해외주식 | 클래스주 bare 티커 형식 | (BRK-B 등 거래 시) | IsuNo 포맷 |
| G-OF4 | 해외선물 | CIDBQ02400 `TrxStatCodeNm` | Phase 0 `--overseas-futures` / P1 | status 문자열 |
| G-OF5 | 해외선물 | CIDBQ05300 KRW행 vs Xchrat환산 | Phase 0 `--overseas-futures` | equity 환산 경로 |
| G-OF6 | 해외선물 | 취소 OrdDt 출처 | P1 `--overseas-futures --order` | 매수응답 OrdDt 보관·전달 |
| G-OF8 | 해외선물 | AcntNo/Pwd body·계좌모델 | Phase 0 (토큰·계좌) | 자격증명 구조 확정 |
| resolver | 해외선물 | o3101 `BscGdsCd`==CME root? | Phase 0 `--overseas-futures` | dataset 심볼→LS코드 매핑 |

**원칙:** 실측이 코드 가정과 다르면 → 그 자산군 브로커의 fixture·필드명·정규식·매핑을 교정하고
회귀 테스트를 실측 fixture로 교체(출력 계약은 유지). 모든 자산군 Phase 1 통과 + Phase 2 1라운드 통과 후 "완료" 선언.

---

## 체크리스트 (출력 보관용)

### Phase 0 (읽기 전용)
- [ ] C 국내주식: equity≠0(KRW)·시세 OK
- [ ] D 국내선물: equity(EvalDpsamtTotamt) KRW·시세·resolve("코스피200선물")≠None·t8432 hname 형식 확인
- [ ] E 해외주식: foreign_eval_krw KRW(USD×fx)·US 시세·BaseXchrat 수신
- [ ] F 해외선물: equity(USD×Xchrat) KRW·resolve("금선물")≠None·BscGdsCd vs root 확인
- [ ] 각 RAW 블록 전달 → 필드 실측 반영 완료

### Phase 1 (주문 라운드트립, 모의·1주/1계약)
- [ ] C 매수→체결인지(t0425 chegb=0 status)→청산
- [ ] D 매수→체결(t0434)→포지션 반영→청산
- [ ] E 매수(지정가)→체결(COSAQ00102)→청산
- [ ] F 매수(시장가)→체결(CIDBQ02400)→청산(취소 OrdDt 전달 확인)

### Phase 2 (전체 사이클)
- [ ] 자산군별 시가~종가 1라운드·ledger·킬스위치 KRW 정상
- [ ] 당일매매(hold_days=0) 종가청산 동작

### 완료 후
- [ ] 실측 반영 커밋 → push/PR/머지(사용자 허락) → 라이브 게이트(실전 마이크로 1주)

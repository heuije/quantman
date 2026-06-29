# P7 — 실제 수수료(KIS TTTC8715R) Implementation Plan (캡처-우선)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. 상위 spec §3.5·§2.3.
> ⚠ **이 단계는 캡처-우선.** TTTC8715R은 실전 전용·청산 후에만 집계 → dev/모의 검증 불가. 실제 응답을
> 확보하기 전엔 fetch/parse를 확정 구현하지 않는다(원칙4 "검증된 해결책만"; KIS GOTCHAS 빈발).

**Goal:** 청산된 거래의 **실제 수수료+제세금**(KIS `TTTC8715R` 종목별 `fee`/`tl_tax`)을 가져와 **net 실현손익**
(현재 GROSS)과 함께 표면화. 해외=`CTOS4001R`/`TTTS3039R`. (LS는 미검증·종목단위 — 라이브 확정 후.)

**Architecture (확정 단계 = 캡처 후):** 로컬 `kis_broker`에 `fetch_realized_fees(start, end)` 신설 →
`/uapi/domestic-stock/v1/trading/inquire-period-trade-profit`(TTTC8715R) 호출 → output1 종목행에서
`fee`+`tl_tax`, output2에서 `tot_fee`/`tot_tltx` 파싱. 호출 시점 = 종가청산/settlement 사이클(실현손익 TR은
청산 후 집계). net 실현손익 = 기존 GROSS − (fee+tax). 표면화 = trade/snapshot에 `fee`/`net_pnl` 추가 →
웹 표시(P6-3 패턴). **실전 전용** — 모의/LS는 "수수료 집계 전(예상치 표시)"로 정직 표기.

---

## Task 0 (BLOCKING): 실전 라이브 캡처 — TTTC8715R 응답 확정

> P5.0 LS 캡처와 동형. **사장님이 실전 KIS 계좌 + 청산 완료 거래가 있을 때** 1회 실행(read-only).

- [ ] **Step 1: 캡처 스크립트 작성** `local/verify_kis_fees.py`(read-only) — keyring 실전 KIS 자격증명으로
  TTTC8715R을 최근 기간으로 호출, RAW 응답을 출력(값 마스킹·필드명 위주). `verify_ls_account.py` 패턴.
- [ ] **Step 2: 사장님 실행** — 실전 계좌·청산 거래 존재 시 1회. RAW(필드 구조)·rt_cd·output1/output2 키 확인.
- [ ] **Step 3: 결과 기록** — `fee`/`tl_tax`/`tot_fee` 실제 키·타입·단위 확정 → Task 1의 파서를 실데이터로.
  (모의·LS는 미지원 확인 → "예상치" 폴백 유지.)

## Task 1 (캡처 후): fetch_realized_fees + 파서 (TDD)
- `kis_broker.fetch_realized_fees(start_iso, end_iso) -> {symbol: {fee, tax}}` + 계좌합계. 단위 테스트는
  **Task 0 캡처 RAW를 fixture로** mock(추측 mock 금지 — 실응답 기반).
- 해외: `CTOS4001R`/`TTTS3039R` 동형(별도, 해외 실전 캡처 후).

## Task 2 (캡처 후): net 실현손익 + 표면화
- settlement/종가청산 사이클에서 `fetch_realized_fees` 호출 → 당일 청산 종목의 fee/tax를 trade 기록에 병합,
  `net_pnl = realized_pnl − fee − tax` 추가(GROSS도 보존·라벨 구분). snapshot → 웹 표시(P6-3 invest 패턴 확장).
- 모의/LS: "실수수료 미지원 — 예상치(P6-2 비용모델)" 정직 표기.

---

## 왜 캡처-우선인가 (정직)
- TTTC8715R은 **실전 전용**(모의 미지원) + **청산 후에만** 데이터 → dev/모의에서 실응답 확보 불가.
- KIS API는 문서-실측 차이(GOTCHAS)가 잦아, 실응답 없이 파서를 확정하면 라이브에서 처음 깨진다(추측 완료).
- LS는 t0424 fee/tax가 미검증·종목단위 — 별도 라이브 확정.
- ⇒ **Task 0(사장님 실전 캡처)이 Task 1·2의 전제.** 캡처 전엔 구현 확정 보류 — P6-2 예상수수료가 그동안의
  최선(추정) 표면.

## 현재 상태
- P6-1(로컬 체결 invest)·P6-2(서버 예상수수료)·P6-3(웹 표시)로 **예상 기반 투명성은 완비**.
- P7(실제 수수료)은 **사장님 실전 데이터 확보 시 캡처-우선으로 진행**.

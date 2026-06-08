# 자동매매 머니패스 불변식 (INVARIANTS)

> 이 문서는 자동매매 모듈이 **항상 참이어야 하는 법칙**의 단일 출처다. 엣지케이스는
> 단건 패치가 아니라 *불변식을 보존하는가*로 판단한다. 각 불변식은
> [Trading Workbench](specs/2026-05-30-trading-workbench-design.md)의 시뮬레이터가
> 단언하는 대상이며, `sim/invariants.py`의 실행 가능 체크와 ID로 1:1 대응한다.

**형식**: `ID · 진술 · 근거 · 강제지점(file:func) · 수호 테스트`. 강제 안 되는 항목은
`⚠️ 갭`으로 표기 — 시나리오/코드로 채워야 할 대상.

엣지케이스는 9축으로 MECE 분류한다(아래 섹션). 상태/전이 관점은
[ORDER_STATE_MACHINE.md](ORDER_STATE_MACHINE.md) 참조.

---

## A1. 시장·캘린더 상태

- **INV-TIME-1** — 거래일·dedup·원장의 'today'는 PC tz가 아닌 **KST 기준**이다.
  - 근거: 해외 거주/여행 사용자의 PC tz가 한국장 거래일을 어긋나게 함 (L-06).
  - 강제: `trader.py:kst_today` (`datetime.now(ZoneInfo("Asia/Seoul")).date()`).
  - 테스트: `test_atomic_save_kst.py`.
- **INV-CAL-1** — 휴장일에는 settlement·신규 진입이 무동작이다.
  - 근거: 휴장일 reconcile은 KIS 잔고 부작용 우려 (L-03).
  - 강제: `runner.py:_run_settlement_locked` (`market_calendar.is_session_day` 가드).
  - 테스트: `test_calendar_sync.py` (간접).
- **INV-CAL-2** — KIS 점검창은 *실측 probe*로만 차단하고 추정 시간가드로 막지 않는다.
  - 근거: 추정 가드가 미장 마감 직전 catch-up 매수를 오차단한 사고 (2026-05-28, PR-1).
  - 강제: `trader.py:_cycle_body` (시간 가드 제거 + 잔고 health check가 실패 시 cycle 중단).
  - 테스트: ⚠️ 갭 — incident 재현 시나리오 필요 (Workbench P4 §9).

## A2. 주문 생애주기

- **INV-ORDER-1** — intent journal 기록 없는 발주는 없다.
  - 근거: 크래시-재기동 시 reconcile이 당일 주문 조회로 매칭해 중복 발주를 막음 (L-01).
  - 강제: `trader.py:_submit_buy`/`_submit_sell` (발주 직전 `intents.begin` fsync).
  - 테스트: `test_intents.py`.
- **INV-ORDER-3** — 미국 정상 cycle 진입은 예약주문(매수=지정가·매도=MOO)으로만 발주한다.
  - 근거: 개장 전 접수창 발주 → 개장 자동전송. 장중 청산 재진입(reserved=False)은 즉시 시장주문.
  - 강제: `trader.py:_is_reserved_us` (단일 판정) → `_submit_buy`/`_submit_sell` 분기.
  - 테스트: `test_overseas_reserved_order.py`.

## A3. 체결 인지

- **INV-FILL-1** — 각 체결은 원장에 **정확히 한 번** 반영된다(이중 가산 없음).
  - 근거: KIS가 같은 H0STCNI0 체결통보를 두 번 보내면 over-position (L-09). 단일 writer로 수렴.
  - 강제: `trader.py:_apply_fill` (유일 원장 writer) + `intraday_loop.py:_on_exec_event` dedup
    `(STCK_CNTG_HOUR, qty, price)` 3-tuple.
  - 테스트: `test_exec_notice_dedup.py`.
- **INV-FILL-2** — ODNO 비교는 항상 **canonical**(`lstrip("0")`)로 한다.
  - 근거: 발주응답·daily-ccld는 zero-padded-10, WS 실시간 패딩 미보장 → 3경로 정규화 불일치가
    체결 누락을 유발했던 결함 class (M2).
  - 강제: `kis_broker.py:canonical_odno` (단일 출처); `_on_exec_event`·`order_status`·
    `_overseas_order_status`가 비교 시 canonical 사용. `pending` 키 = canonical, 엔트리는 raw 보존.
  - 테스트: `test_canonical_odno.py`.
- **INV-FILL-3** — 체결 인지는 WS 통보·REST 폴링 어느 경로로 와도 동일 결과다(경로 독립).
  - 근거: WS 끊김 시 REST 폴링 fallback (Q3). 두 경로 모두 `_apply_fill`로 수렴.
  - 강제: `intraday_loop.py:_on_exec_event`(WS) / `trader.py:_resolve_pending`(REST) → 같은 `_apply_fill`.
  - 테스트: `test_polling_fallback.py` + Workbench 시나리오(경로 교차).

## A4. 크래시·재기동·catch-up

- **INV-ORDER-2** — 한 intent당 제출은 최대 1회다(재기동 멱등).
  - 근거: 크래시 후 재기동 시 같은 의도를 두 번 발주하면 안 됨 (L-01 reconcile).
  - 강제: `intents.py:reconcile_submitting` (당일 KIS 주문 조회로 submitting intent 매칭).
  - 테스트: `test_intents.py`.
- **INV-CATCHUP-1** — 이미 실행된 cycle은 catch-up이 재실행하지 않는다.
  - 근거: PC가 꺼져 놓친 cycle만 보완, `cycles.jsonl` 기반 멱등 (Phase 7).
  - 강제: `catchup.py:run_catchup_on_startup`.
  - 테스트: ⚠️ 갭 — Workbench 시나리오 필요 (P4).

## A5. 동시성

- **INV-CONC-1** — `self.ledger`/`self.pending` 변경은 `_CYCLE_LOCK`(RLock) 직렬화 하에서만 일어난다.
  - 근거: 스케줄러·WS 체결·60초 monitor 3스레드가 같은 원장을 동시 변경하는 race (M3).
  - 강제: `trader.py:_CYCLE_LOCK` + `_apply_fill`·`_after_submit`·`cancel_all_pending`·
    `reconcile_with_kis`·`cycle`이 락 안에서만 변경. ks hook은 락 밖 defer(데드락 회피).
  - 테스트: `test_trader_concurrency.py`.

## A6. 계좌·원장 정합성

- **INV-LEDGER-1** — 원장에 남은 포지션의 qty는 **양수**다(0 이하는 삭제).
  - 근거: 과매도/이중 차감 방지 (L-04). 0 수량 유령 포지션 금지.
  - 강제: `trader.py:_apply_fill` 매도 분기 (`qty<=0 → del`), over-sell clamp (L-04).
  - 테스트: `test_oversell_clamp.py` + `sim/invariants.py:check_ledger_nonneg`.
- **INV-LEDGER-2** — 추가 매수 시 평균단가는 수량가중 평균이다.
  - 근거: 분할 매수 평단 정확성. ZeroDivision 가드 포함 (L-05).
  - 강제: `trader.py:_apply_fill` 매수 분기 (`(entry*qty + fill*fqty)/total`).
  - 테스트: ⚠️ 갭 — Workbench 시나리오(분할 매수) 필요.
- **INV-RECON-1** — reconcile은 외부(HTS/MTS) 수동 매도분만 차감하고 외부 매수는 원장을 건드리지 않는다.
  - 근거: 자동매매가 사지 않은 포지션을 원장에 넣으면 청산 로직이 오작동 (Phase 40).
  - 강제: `trader.py:reconcile_with_kis` + `analytics.py:reconcile_ledger`/`plan_orphan_adjustments`.
  - 테스트: ⚠️ 갭 — Workbench 시나리오(외부 매도 drift) 필요.
- **INV-FUT-1** — 선물 포지션 side는 **long|short**이고 qty > 0다(flat은 미보유=목록부재).
  - 근거: 선물 숏 포지션의 부호 방향 혼동 방지 (증거금·손익 계산 독립변수).
  - 강제: `sim/invariants.py:check_futures_sign` (발주 전 위반 검사 미배선, Workbench 진입 시 추가).
  - 테스트: `test_sim_futures.py::test_inv_fut_sign_ok/rejects_bad_side`.
- **INV-FUT-2** — 선물 eval_pnl = (eval−avg)×qty×승수×부호(롱+1/숏−1).
  - 근거: 미결제손익 계산 정확성 (장 중·정산 평가에서 수렴성 보증).
  - 강제: `sim/invariants.py:check_futures_pnl` (position 생성·갱신마다 자동 검증).
  - 테스트: `test_sim_futures.py::test_inv_fut_pnl_ok/rejects_wrong_pnl`.

## A7. 리스크·안전

- **INV-KS-1** — kill switch active면 신규 매수는 0이다.
  - 근거: 일일 손실 한도 도달 시 자금 노출 차단 (Q5). 보유 청산은 별개.
  - 강제: `trader.py:_cycle_body` (ks_active 시 매수 패스 skip), `killswitch.py:is_active`.
  - 테스트: `test_killswitch_intraday.py` (평가) + ⚠️ 갭(매수 0 시나리오).
- **INV-KS-2** — kill switch 발동 시 미체결 주문은 즉시 cancel 시도된다.
  - 근거: 미체결 매수가 늦게 잡혀 손실을 키우는 시나리오 차단 (Q5, FCA "cancel all").
  - 강제: `trader.py:cancel_all_pending`.
  - 테스트: `test_killswitch_intraday.py`.
- **INV-PRICE-1** — 지정가 발주는 한국 ±30% 가격제한폭 안으로 사전 클램프된다.
  - 근거: KIS 서버 거부 누적 방지.
  - 강제: `trader.py:_submit_buy`/`_submit_sell` (`qc.apply_daily_price_limit`).
  - 테스트: ⚠️ 갭 — Workbench 시나리오 필요.
- **INV-FUT-3** — 선물: 점유 증거금 ≤ 가용 증거금 (과레버리지 차단).
  - 근거: 증거금 부족으로 인한 강제청산 방지 (마진콜 전 조기 정지).
  - 강제: `sim/invariants.py:check_futures_margin` (account_snapshot 평가 시 검증).
  - 테스트: `test_sim_futures.py::test_inv_fut_margin_ok/rejects_overleverage`.

## A8. 외부 API 한계

- **INV-API-1** — KIS 호출은 초당 ≤8건으로 throttle된다.
  - 근거: KIS 개인 한도 10건/초, 안전마진 8 (Phase 48).
  - 강제: `kis_broker.py:_Throttle` (프로세스 전역 단일 인스턴스).
  - 테스트: ⚠️ 갭(단위 throttle 테스트).
- **INV-API-2** — EGW00201(초당 한도 초과)은 reactive 재시도로 흡수한다.
  - 근거: burst 시 일시 초과 복구.
  - 강제: `kis_broker.py:_get_retry`/`_post_retry`.
  - 테스트: ⚠️ 갭.

## A9. 영속화·보안

- **INV-PERSIST-1** — 민감 상태 저장은 원자적(tmp+os.replace)이고 owner-only ACL이다.
  - 근거: 크래시 시 부분기록 손상 방지(L-02) + 같은 PC 타 사용자 노출 차단(R5/M0).
  - 강제: `state_store.py:save_json`/`append_jsonl`.
  - 테스트: `test_state_store.py`.
- **INV-SEC-1** — KIS 자격증명·계좌번호·원시 주문은 서버 payload·로그에 들어가지 않는다.
  - 근거: 보안 원칙(CLAUDE.md §3) — 로컬 PC 전용.
  - 강제: `sync_client.py` push payload는 안전정보(전략·체결요약·잔고스냅샷)만. 자격증명은 `secrets_store`(keyring).
  - 테스트: ⚠️ 갭 — push payload에 민감 키 부재 단언 테스트 필요.

---

## 갭 요약 (강제 안 됨 / 테스트 미비 — Workbench가 채울 대상)

**P4에서 채워진 항목** (시나리오 추가 완료):
- ✅ INV-FILL-2 — `test_fill_paths.py::test_unpadded_ws_oder_no_is_recognized` (E2E unpadded WS)
- ✅ INV-FILL-3 — `test_fill_paths.py::test_rest_polling_recognizes_fill`
- ✅ INV-LEDGER-2 — `test_ledger_consistency.py::test_split_buy_weighted_average`
- ✅ INV-RECON-1 — `test_ledger_consistency.py::test_external_sale_drift_reconciled` + `test_external_buy_does_not_touch_ledger`
- ✅ INV-PRICE-1 — `test_risk_guards.py::test_buy_limit_clamped_to_daily_price_limit`
- ✅ INV-KS-1 — `test_risk_guards.py::test_killswitch_blocks_new_buys` (실 `trader.cycle` 진입게이트)

**남은 갭 (정직한 상태 분류):**

| INV | 상태 | 사유/다음 단계 |
|---|---|---|
| INV-CATCHUP-1 | 강제 O, 테스트 X | cycles.jsonl + catchup 머신 셋업 → full-cycle 증분(저우선) |
| INV-CAL-2 | 강제 O, 테스트 X | 점검가드 *제거됨* — 과거 버그 재현엔 옛 코드 필요(현 코드 회귀는 약함) |
| INV-API-1/2 | 강제 O, 테스트 X | throttle 단위 테스트(별도·저우선) |
| INV-SEC-1 | 강제 O(경계설계) | **sim 부적합** — SimBroker엔 유출할 자격증명이 없어 누설을 못 잡음. account_snapshot 안전필드만 + 코드리뷰로 강제. live 페이로드 정적검사가 더 적합 |

> 남은 항목 = (a) full-cycle scaffold 필요 증분, (b) live 게이트 의존, (c) sim 부적합
> (경계설계로 강제). 환경이 갖춰져 (a)는 "택소노미 행+불변식+시나리오" 루틴으로 추가
> 가능 — 억지로 무거운 scaffold를 지금 만들지 않는다(Over-engineering 금지).

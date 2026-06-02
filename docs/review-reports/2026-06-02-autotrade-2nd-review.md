# 자동매매 모듈 2차 코드리뷰 — 디버깅·리팩토링 (2026-06-02)

다른 세션의 IR 엔진 통합(trader/intraday_stop/catchup) + 자금안전 fix(8cd5e8b) 위에서,
**SimBroker 하베스트 기반** 디버깅·리팩토링 리뷰. 동일 루브릭(4원칙 + INVARIANTS.md 계약)
으로 3슬라이스(체결·원장 / 청산·손절 IR / 오케스트레이션·브로커) 읽기전용 병렬 리뷰 →
독립 수렴한 상위 결함을 SimBroker로 재현·검증 후 수정. **최종 156 passed.**

## 수정 완료 (모두 TDD 회귀 추가)

| ID | 결함 | INV | 수정 | 커밋 |
|---|---|---|---|---|
| A | WS `_on_exec_event`·`_resolve_pending`의 pending 변경이 `_CYCLE_LOCK` 밖 → 이중 del(KeyError)·dedup 경합 | CONC-1 | 두 지점 락 직렬화 | b1639e4 |
| B | `_resolve_pending` filled 분기가 부분반영분 재가산 → over-position(40+60=100) | FILL-1/3 | `delta=filled-already` 통일 | b1639e4 |
| C | 매도 멱등 게이트가 EOD에만 → 장중·catch-up 이중매도 | FILL-1 | `_submit_sell` 진입부 단일 게이트화 | 808e4cb |
| D | `_in_cycle` 인스턴스 bool이 cycle 중 WS체결 ks 평가 오억제 | KS-1 | thread-local property | 808e4cb |
| ① | run_cycle 재시도(60/300/900s)가 US 예약 접수창(개장-10분) 초과 → 예약발주 누락 | — | `_cycle_backoffs(reserved)`로 예약은 (30,60)s | 00aa11f |
| ②a | catch-up이 raw `market_group_of` → 한 종목 RoutingError가 전체 abort | — | `_market_group_safe` 일관화 | 00aa11f |
| ②b | over-sell 클램프가 EOD·intraday 각자 구현(EOD는 사후 추가된 결함 class) | LEDGER-1 | `clamp_sell_qty` 단일 출처 | 00aa11f |
| ③#4 | kill switch 발동 시 파싱실패 고아가 except→continue로 청산 누락 | KS | except에서 reason=None→ks 분기 적용 | 4674e7e |
| ③#2 | `_atr14_of`의 광범위 except가 코드결함까지 흡수(ATR-트레일 silent 무력화) | — | `(ValueError,TypeError,IndexError)`로 좁힘 | 4674e7e |

**A·D는 직전 M3 동시성 작업의 빈틈**(락 목록에 _on_exec_event/_resolve_pending 누락,
_in_cycle을 thread-local로 안 둠)이었음 — INVARIANTS.md INV-CONC-1 명세와 구현 갭.

## 검토 후 변경 안 함 (4원칙 근거 — 투기적/과한 변경 회피)

- **③#1 catch-up 트레일 peak stale**: PC-off 동안 장중 고점이 ledger.peak에 미반영돼
  트레일이 약하게 트리거될 수 있음. 고정 sl/tp는 무영향. 장중 고점(분봉) 조회는 드문
  PC-off 경로 대비 비용 과다 → **코드 주석으로 한계 명시**(catchup.py), 미구현.
- **③#3 reconcile 기록 ODNO canonical 미적용**: intent journal에 기록되는 odno가 raw
  zero-padded. 매칭 로직은 ODNO 부재로 qty/price 사용(정당), 기록값은 정보용이라 버그
  아님. canonical화는 intents→kis_broker import 결합만 늘리는 cosmetic → **미변경**.
- **③#5 예약 `exchange_of or "NAS"` fallback**: 미해결 거래소를 NAS로 기본. 대부분 US
  종목엔 합리적 fallback이고, RoutingError로 바꾸면 *작동하던 발주가 깨질* 투기적 변경
  (검증된 해결책만 위반) → **미변경**. TSE/HKS 주문 TR은 dead config이나 동시 세션이
  kis_broker 편집 중이라 충돌 위험 + 제거 가치 marginal → **미변경(문서화)**.

## 신규 회귀 테스트
`test_concurrency_locks.py`(락 직렬화·_in_cycle thread-local), `test_fill_paths.py`
(부분→full over-position), `test_sell_idempotency.py`(2경로 이중매도),
`test_cycle_retry_window.py`(예약 backoff), `test_catchup_routing.py`(라우팅 abort),
`test_clamp_sell_qty.py`(클램프 계약), `test_risk_guards.py`(고아 청산).

## 브랜치 주의
이 fix들은 `main`(A~D) + `feat/screener-into-symbol-picker`(①②③) 에 걸쳐 커밋됨
(동시 세션의 브랜치 전환). 모두 같은 working tree에서 검증(156 passed), 브랜치 merge
시 main 합류. A~D가 feat 브랜치 조상이라 이력 일관.

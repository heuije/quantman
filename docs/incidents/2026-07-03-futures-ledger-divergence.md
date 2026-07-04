# 2026-07-03 — 선물 원장↔브로커 분기 (LS 모의 · reconcile 파괴적 오정정)

**심각도: Critical(정합성)** · 모듈: 자동매매 엔진(`local/localapp/`) · 계좌: LS 국내선물 [모의]
**상태: 수정 구현·테스트 완료(브랜치 `fix/autotrade-position-integrity`) — 모의 재검증·릴리스 게이트 대기**

## 요약

앱 원장(ledger.json)과 브로커 실포지션이 06-30부터 완전 분기했다. 07-03 실측:
원장 `{}` · 브로커 코스피200선물 **롱 4**(웹 표시 "(수동 매수)" — 사용자는 수동매수 안 함) ·
앱 자체 주문 net **숏 1**. 세 장부가 전부 불일치. 방아쇠는 LS 잔고 계약코드 정규화의 조용한
실패, 근본은 reconcile의 파괴적(fail-dangerous) 자동 정정과 정합 불변식 부재.

## 발견

- 07-03 사용자 진단 요청("모의계좌 자동매매 진행중인데 지금까지는 문제 없나요?") →
  아티팩트 기반 정밀 진단(cycles.jsonl·orders.jsonl·trades.jsonl·localapp.log·preview_cache).
- 웹 스냅샷: 보유 코스피200선물 롱4 @1240.90 "(수동 매수)" · reconcile `in_sync 0`.

## 영향

- 모의 계좌라 실손 없음. 단 원장이 비어 앱이 브로커 실포지션을 인지·청산 불가(유령화),
  reconcile이 매 정산 오판을 반복(external_extras=1 지속). 실전이었다면 무관리 방치 포지션.
- 부수: 07-02 KRX 종가창 cron 미발화(무감지) → 일중 숏이 의도치 않은 오버나이트 노출
  (익일 아침 "보유기간" 청산으로 회수 — P&L 자체는 앱 관점 정확).

## 근본 원인 (부류 4개 — 상세: docs/REDESIGN/autotrade-position-integrity-redesign.md)

1. **R2(방아쇠)** — LS 잔고(t0441)는 포지션 symbol을 KRX 상품코드형 `101T9000`(8자)으로
   보고하는데, LS 역매퍼(`ls_futures_contracts.dataset_for_code_static`)는 shcode 프리픽스
   (A01/A05)만 인식 → `None` → 라우터(`broker_router.account_snapshot`)의 `if ds:` 가드가
   **조용히 정규화 스킵** → 브로커 키(원시 코드) ≠ 원장 키(상품명 "코스피200선물").
2. **R1(근본)** — `trader.reconcile_with_kis`가 "원장에 있는데 브로커에 없음 = 외부 매도"로
   단정하고 **원장을 자동 삭제**. 매칭 신뢰성 검사가 없어, 매칭이 깨진 상태의 오판을
   파괴적으로 집행했다. 실증: 06-30 `reconcile: ledger 제거 [10] 코스피200선물 qty 3 → 0
   (외부 매도 추정)` — 해당 롱3은 브로커에 실존(브로커 매도 주문 0건).
3. **R3** — 3중 장부(브로커·원장·주문로그) 간 정합 불변식 부재 → 분기가 "drift 없음
   (in_sync 0종목)" 로그와 함께 정상처럼 누적.
4. **R4** — 07-02 15:25/15:40/15:45 종가창 cron 미발화(앱 생존 중 — SSE 재연결 로그 실존·
   misfire 이벤트도 없음·내부 기전 미확정, 동시간 Railway 502 폭풍은 정황). 종가창엔
   catch-up·감지 장치가 없어 그날 유실이 조용히 확정.

같은 부류의 잠복 사고 추가 발견: 선물 leg 조회 실패(`fetch_failed`) 시에도 원장 선물 전부가
orphan으로 보여 **전량 삭제**될 수 있었다(R1과 동일 부류·미발화 상태로 존재).

## 대응 (수정 = 부류 단위)

브랜치 `fix/autotrade-position-integrity` (서버/웹 무변경):

- **D1** `core/quant_core/futures_contract.py` — KRX 숫자형 상품코드 프리픽스(101 정규·105
  미니) 역매핑을 `dataset_for_contract`에 추가(8자 가드로 주식 6자 오매칭 방지).
- **D2** `ls_futures_contracts.dataset_for_code_static` — shcode 매칭 후 core로 위임(역매핑
  지식 단일출처). `broker_router` — 정규화 실패 시 `log.error` + `symbol_unmapped` 표식
  (조용한 실패 부류 재은닉 불가).
- **D3** `trader.reconcile_with_kis` — `fetch_failed` 또는 `symbol_unmapped` 존재 시 **선물
  orphan 파괴적 정정 전면 차단**(보고·표면화만, `reconcile_blocked` 결과 필드). 주식 자동
  차감(승인된 수동매도 대응)은 신원 무관 유지.
- **D4** `runner._run_settlement_locked` — 불변식 I5: 정산 시점 당일매매(hold_days==0) 잔존
  포지션 감지 → error decision + `n_daytrade_unclosed` 표면화(종가창 미실행류 당일 인지).
- 테스트: 인시던트 재현 회귀(`test_reconcile_failsafe.py`) 포함 신규·확장 5파일.
  `local/tests`+`core/tests` 1404 passed (1 fail은 선재 캘린더 테스트 격리 플레이크 —
  본 변경 무관·별도 태스크 분리).

## 결과 (해소 검증)

- 로컬 테스트 green(위). **모의 라이브 재검증은 릴리스 게이트로 남음**: LS 선물 진입 후
  reconcile `in_sync=1`·정산 `n_daytrade_unclosed=0`·주식 수동매도 차감 유지 확인 필요.
- 기존 분기 상태는 복원하지 않음(사용자 결정 — 모의 초기화 후 재실행). 배포 전 사용자가
  브로커 모의 포지션 flat 정리 + 자동매매 초기화.

## 재발 방지

- 정규화 실패·조회 실패 시 reconcile은 fail-safe(무동작+경보) — "매칭이 신뢰 가능할 때만
  파괴적 정정" 원칙을 코드로 강제(I2).
- 브로커 잔고 코드는 주문 코드와 **다른 코드공간**일 수 있다(LS: shcode↔KRX형↔ISIN 3종) —
  역매핑은 core 단일출처에 두고, 새 상품·새 브로커는 그 한 곳에 등록.
- 정산이 상태 불변식(당일매매 잔존=0)을 매일 검사 — 종가창 유실이 재발하면 당일 15:50에
  로그·웹으로 드러난다(07-02 미발화의 내부 기전은 이 신호로 후속 진단).
- 잔여(후속): R5 commingle 귀속·결함 C(오버나이트 롱 `hold_days=0` 미진입 — 서버 컴파일러/
  preview 정합)·07-02 스케줄러 미발화 기전 규명.

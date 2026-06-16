# 2026-06-12 — 선물 종가청산 체결 미기록 (θ: 단일 resolve + 정산 cron 역순)

| | |
|--|--|
| 일자 | 2026-06-12 (모의투자 라이브) |
| 심각도 | High — 체결은 정상, 기록·표시·후속 자율성 상실 |
| 상태 | ✅ 해소 (fix/close-cycle-reliability — θ PR) |

## 요약
코스피200 선물 당일매매 종가청산 주문(0000004525)이 15:40 정상 발주·종가 단일가
(~15:45)에 정상 체결됐으나, 로컬앱이 체결을 **기록하지 못해** pending에 박제됐다.
18:54 사용자가 "지금 실행"을 눌러서야 재-resolve로 체결이 잡혀 정산손익
+24,900,000원이 기록됐다. 같은 날 미장 GOOG에서는 동일 부류(체결-기록 단절)가
δ(해외 체결감지 결함)와 결합해 261주가 미기록·미청산으로 주말 방치됐다.

## 발견
- 사용자가 15:40 이후 "국장선물 종가청산 잘 됐는지" 검토 요청.
- `~/.quant-platform/pending_orders.json`: 0000004525 잔존(상태 미해결).
- `ledger.json`: 선물 3계약 보유 그대로. `trades.jsonl`: 정산 기록 없음.
- 18:54 "지금 실행" 후 fill 1299.85 적용 → ledger 정리·realized_pnl +24.9M 기록.

## 영향
- 자율 사이클만으로는 종가청산 체결이 영원히 미기록 — 사람 개입 필수(자율성 상실).
- ledger가 이미 청산된 포지션을 보유로 유지 → 다음날 위험 평가·이중 매도 시도 위험.
- 타임라인엔 종가청산 마일스톤 자체가 없어 성공/실패를 로그를 파야 알 수 있었다.

## 근본 원인 (2축, 둘 다 구조적)
1. **단일 resolve(θ)** — `trader.liquidate_day_trades`가 발주 직후
   `_resolve_pending` 1회만 호출. 일반 cycle의 `_wait_pending`(60s/20s 폴링)이
   없어, 모의 ~27초 체결 지연·실전 종가 단일가(15:35~15:45) 체결을 못 본다.
2. **cron 역순** — 정산(`krx_settlement`)이 15:35로, 선물 종가청산(15:40)보다
   **먼저** 돌았다. 선물 종가창(~15:45) 체결을 그날 안에 다시 확인할 패스가
   구조적으로 없었다. (정산 15:35는 선물 도입 전 "매매 끝난 직후" 기준 —
   선물이 거래일을 15:45로 늘렸는데 정산이 따라가지 않았다.)

## 대응 (fix/close-cycle-reliability)
- θ: `liquidate_day_trades`에 일반 cycle과 동일한 `_wait_pending` 추가.
- θ: 정산 cron 15:35 → **15:50** 재배치(모든 KRX 종가창 이후). catch-up 임계도 정합.
- N1: 종가청산 시작 시 `_resolve_pending` 선실행 — 미기록 진입 체결(δ류)을 ledger에
  복원한 뒤 순회(GOOG 부류 방어). 체결확인 불능+계좌 보유>원장이면 추측 발주 없이
  "당일청산 불능" 명시 표면화(외부 보유 오인 매도 금지 — 병1 불변식).
- N2: 타임라인에 종가청산 마일스톤 3종(주식 15:25·선물 15:40·미장 close−5분) 노출,
  kind-aware 매칭, `n_pending_unresolved>0`이면 ✓ 대신 ⚠(거짓 녹색 제거).

## 결과 (해소 검증)
- 재현 테스트 선작성 → red 확인 → 구현 → green:
  `local/tests/scenarios/test_theta_close_reliability.py`(4),
  `local/tests/test_close_ordering.py`(2), `server/tests/test_timeline_close_events.py`(9).
- 전체 스위트: local 370 · server 196 · 루트 골든 390 모두 green(골든 보존).
- 라이브 게이트(배포 후): 다음 국장 선물 종가(15:40)에서 수동 개입 없이
  체결 기록·15:50 정산·타임라인 ✓ 확인 — 로드맵 Phase 3.

## 재발 방지
- "발주창 이후에 반드시 resolve 패스가 온다"를 cron 배치의 불변식으로 명문화
  (scheduler.py 주석·`docs/REDESIGN/autotrade-reliability-roadmap.md`).
- 서버 타임라인 시각 상수는 로컬 scheduler.py와 동일 출처 유지(D6-1 재확인) —
  이번에 정산 15:50을 양쪽 동시 갱신, 구버전 push는 same-day fallback으로 호환.

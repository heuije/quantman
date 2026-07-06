# 2026-07-06 — 비상청산이 원장에 반대방향 유령 포지션 생성 (sid-미스매치)

**심각도: Critical(정합성·자금경로)** · 모듈: 자동매매 엔진(`local/localapp/trader.py`) · 계좌: LS 국내선물 [모의]
**상태: 🟠 진단·설계·구현·로컬검증 완료(브랜치 `fix/emergency-liquidation-orphan`) — 모의 재검증·릴리스 게이트 대기**

## 요약

사용자가 잘못된 포지션을 정리하려 **계좌 킬스위치(LIQUIDATE_ALL)** 로 비상청산을 실행(12:31:13)했는데,
청산이 완료되기는커녕 **원장에 반대방향(롱4) 유령 포지션이 새로 생겼다**. 비상청산은 브로커 숏을
BUY(환매)로 닫았으나, 그 체결이 원장 회계에서 "청산"이 아니라 **"신규 롱 진입"** 으로 잘못 기록됐다.
근본은 `_apply_fill`이 체결의 open/close를 **전략 id(sid)로 판정**하는데, 비상청산이 어떤 원장 항목과도
매칭 안 되는 **합성 sid `liquidate:{symbol}`** 로 주문하기 때문. 2026-07-03 인시던트(reconcile 파괴적
삭제·LS 코드 정규화)와 **같은 정합성 부류의 다른 진입점**(v0.9.65/66이 못 덮은 새 벡터 = R6).

## 발견

- 07-06 사용자 질문("12:31:13에 비상청산 실행하면 모든 게 해결됐어야 하는데 왜 고아가 생겼나?") →
  `~/.quant-platform` 아티팩트 기반 정밀 진단.
- `orders.jsonl` **12:31:05→06**: `청산 buy 4 코스피200선물 (kill-switch)` 제출·체결.
- `cycles.jsonl` **12:31:13** 결정: `{"action":"bought","strategy_id":"liquidate:코스피200선물",
  "strategy_name":"비상청산", reason:"4계약 @ 1,283.96 · 명목… 증거금…"}` — **정산손익 표기 없음**
  (숏청산이면 `정산 +xx`가 찍힘). 순수 신규 롱 booking 확정.
- `ledger.json`: `liquidate:코스피200선물` **롱4** + 이후 12:33 전략17 **숏10**(당일매매 신규진입).
- `cycles.jsonl` **12:33** catchup_cycle: 그 롱4를 `unparseable_orphan`("전략 정의 파싱 실패 —
  자동 청산 불가·수동 정리 필요")로 표면화. `reconcile_drift:true·applied:0`(D3 fail-safe가 파괴는 차단).

## 영향

- **모의 계좌라 실손 없음.** 단, 비상청산이 "모든 포지션 정리"라는 사용자 기대와 정반대로 **원장에 유령을
  추가**해 장부↔브로커 분기를 오히려 키웠다(원장 net = 유령롱4 + 전략숏10, 브로커 net은 별도 실측 필요).
- 유령은 매 사이클 `unparseable_orphan`으로 잔존(definition={} → 자동청산 불가). D3 덕에 브로커로
  역전파는 안 됐으나(우연적 방어 — definition이 비어서), parseable했다면 다음 사이클이 유령 롱을
  매도해 **브로커에 실제 반대 포지션을 만들** 수도 있었다(잠복 확대 경로).
- 실전이었다면: 사용자가 "청산했다"고 믿는 순간에 노출이 남거나(청산 미완료) 반대 포지션이 생겨
  자금 위험. 비상 기능이 자금 안전을 훼손하는 부류라 Critical.

## 근본 원인 (R6 — 상세: docs/REDESIGN/autotrade-position-integrity-redesign.md §6)

`_apply_fill`(trader.py:666~692)은 체결의 open/close 여부를 **`p["strategy_id"]`(sid)로 원장을 조회**해
판단한다(BUY: 원장에 그 sid의 숏이 있으면 환매·차감, 없으면 `else`=신규 롱). 전략 사이클에선 sid=원장 키라
정상. 그러나 **비상청산**(`liquidate_all_held`, trader.py:1647)은 브로커 실보유(sid 무관)를 청산하며
`sid = "liquidate:{symbol}"` 합성 키로 `_submit_close_short`→`broker.buy`를 낸다 → `_apply_fill`이 그
합성 sid로 매칭 실패 → BUY가 신규 롱으로 booked.

- 구조적 seam: **비상청산은 종목(instrument) 단위로 브로커를 청산**하는데, **기록 계층은 전략(sid) 단위로
  open/close를 판정**한다. `_submit_close_short` 주석(1069)이 가정한 "buy → ledger 숏 차감"의 전제
  (sid가 원장 숏과 매칭)가 비상 경로에서 조용히 붕괴 — R2(정규화 조용한 실패)와 동형의 *조용한 전제 붕괴*.

## 대응 (설계 = 부류 단위 · 구현 대기)

브랜치 `fix/emergency-liquidation-orphan` (서버/웹 무변경). 설계 = **D6**:

- **불변식 신설(I7)**: 비상청산(liquidation) 체결은 **신규 원장 포지션을 생성할 수 없다** — 종목의 반대
  방향 보유를 차감하거나, 매칭 없으면 `external_liquidated`로 **기록만** 한다.
- **불변식(I8)**: 비상청산 매칭은 합성 sid가 아니라 **(symbol, 반대 side)** 기준(commingle 시 결정적
  순서 차감).
- **구현 seam**: `liquidation=True` 플래그를 `_submit_close_short`/`_submit_sell`→`_after_submit`→
  `_apply_fill`로 전달(기존 `netted` 플래그 선례·기본 False=byte-identical). booking seam 한 곳에
  불변식을 두고 호출부는 단순 유지.
- **회귀 테스트**(신규 `test_liquidation_no_phantom.py`): 07-06 재현(원장 숏10 + 비상 buy4 → 숏6·유령
  없음)·외부보유 청산(원장 무변화)·commingle·정상 close byte-identical.

## 결과 (해소 검증)

- **구현·로컬검증 완료.** `trader._book_liquidation_fill` + `liquidation` 플래그(발주→booking 전파)로
  I7/I8 강제. 07-06 재현 회귀 포함 신규 5종 + `local/tests`+`core/tests` 전체 **1437 passed·회귀 0**.
  정상 전략 close(`liquidation=False`)는 byte-identical(기존 테스트 green 유지). 서버/웹 무변경.
- **모의 라이브 재검증은 릴리스 게이트로 남음**: 새 빌드 설치 후 비상청산 → 원장에 유령 없음·브로커와 정합
  확인.
- 이미 생긴 유령(현 계좌)은 복원 불요 — 모의·사용자 결정(초기화·재실행).

## 재발 방지

- **비상 기능의 booking은 "닫기만·절대 열지 않기" 불변식으로 강제**(I7) — 비상청산이 노출을 늘리는 어떤
  경로도 booking seam에서 구조적으로 차단.
- **종목 단위 브로커 청산 ↔ 전략 단위 원장 판정의 seam**을 명시 문서화 — 비상/reconcile 등 sid-무관
  경로가 sid-키 booking을 재사용할 때의 부류 함정(2026-07-03 R2와 동형: *조용한 전제 붕괴*).
- 별도 검증(R6 범위 밖): 비상청산 BUY가 브로커 숏을 실제로 닫았는지(norm_side 오판 시 2배 확대 가능) —
  모의 HTS 실포지션으로 확인.

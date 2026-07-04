# 자동매매 포지션 정합성 구조 재설계 (2026-07-04)

〔담당: 조대표〕 모듈: 자동매매 엔진(`local/localapp/`) + `core/quant_core/futures_contract.py`(순수 추가).
**서버/웹 무변경.** 트리거 인시던트: 2026-06-30~07-03 라이브(모의·LS 국내선물) 원장↔브로커 분기.

---

## 0. 요약

엔진은 포지션 상태를 3개 표현(①브로커 잔고 ②로컬 원장 ledger.json ③주문로그 orders.jsonl)으로
따로 들고, 이를 **포맷별 ad-hoc 코드 변환**으로 잇고, 어긋나면 **파괴적(자동삭제) reconcile**로 덮는다.
정합 불변식이 없어 셋은 조용히 발산한다. 2026-07-03 실측: ①롱4 ②{} ③숏1 — 전부 불일치.

이 문서는 발견된 오류 전부를 **부류(class) 단위**로 닫는 재설계다. 개별 증상 패치가 아니라
각 부류의 **단일 진입점**을 고쳐 같은 부류의 미래 변종까지 함께 차단한다.

| 뿌리 | 부류 | 해결(결정) | 상태 |
|---|---|---|---|
| R1 | reconcile가 매칭 실패를 "외부 매도"로 단정하고 원장을 파괴적 자동삭제 | D3: 신원계층 비정상 시 파괴적 정정 전면 차단(fail-safe) | 이번 PR |
| R2 | 브로커 잔고코드→상품명 정규화가 LS에서 조용히 실패 | D1+D2: KRX 숫자형 코드 정규화 확립 + 실패 fail-loud | 이번 PR |
| R3 | 3중 장부 간 정합 불변식 부재(조용한 발산) | D4: 정산 시점 상태 불변식 감시(당일매매 잔존=경보) + D3의 blocked 표면화 | 이번 PR(1단계) |
| R4 | 종가창 사이클 미실행이 무감지·무보완 | D4가 감지·표면화(익일 아침 청산은 기존 보장) | 이번 PR(감지) |
| R5 | 한 계약을 여러 전략이 commingle → 귀속 취약 | 후속(넷팅 설계 §12.5 E11과 통합 검토) | 후속 |
| C | 오버나이트 롱 전략 `hold_days=0` → 종가창 가드가 영구 미진입(preview≠실행) | 후속(서버: 컴파일러/저장 검증 + preview 정합) — 로컬 가드는 정상 | 후속 |

---

## 1. 인시던트 증거 (아티팩트 확정)

- **분기 씨앗**: 06-26 매수 롱3(전략 sid10) → 06-26 종가창 미실행(R4) → 06-30 reconcile가
  그 롱3을 "외부 매도 추정"으로 **원장에서 삭제**(trades.jsonl `external_close`, 브로커 주문 0건).
  브로커엔 실포지션이 그대로 남음. 07-01에도 반복(sid10 q2·sid15 q2).
- **왜 못 알아봤나(R2)**: LS 잔고(t0441)는 포지션 symbol을 KRX식 계약코드(`101T9000` 형태·LS 가이드
  실측)로 보고. 라우터의 정규화 콜백(`broker_router.account_snapshot`의 `_d4c`)에 LS가 주입한
  `LsContractResolver.dataset_for_code`는 **shcode 프리픽스(A01/A05)만 인식** → `None` →
  `if ds:` 가드가 **조용히 정규화 스킵** → 원시 코드가 symbol로 잔류 → 원장(상품명)과 영영 불일치.
- **왜 지웠나(R1)**: `trader.reconcile_with_kis`는 원장에 있는데 브로커에 없는 키를 "외부 매도"로
  단정하고 자동 차감/삭제한다. "유저가 진짜 팔았다"와 "내 매칭이 깨졌다"를 구분하지 않는다.
- **왜 조용했나(R3)**: reconcile 결과가 `drift 없음(in_sync 0종목)`으로 로그되고
  external_extras=1은 "수동 매수"로 웹 표시 — 발산이 정상 상태처럼 보임.
- **결과**: `ledger.json={}`인데 브로커 롱4 유령. 앱의 자체 주문 net(③)은 숏1 —
  ①②③ 전부 불일치. 5계약 갭의 정확한 산술은 브로커 체결 이력 필요(모의 리셋 예정이라 복원 불요).
- **KIS·주식 무영향**: KIS 잔고 `shtn_pdno`(A01606)는 core `dataset_for_contract`가 정상 매핑,
  주식은 symbol=종목코드로 양쪽 동일.

부수 확정: 07-02 KRX 종가창(15:25/15:40/15:50) 미실행 — 앱 생존(SSE 재연결 로그) 중 잡 미발화·
미스파이어 이벤트도 없음(스케줄러 내부 기전 미확정). 같은 시각 Railway 502 폭풍은 정황.
종가창 잡은 로컬 apscheduler cron(`misfire_grace_time=300`)이며 **catch-up은 아침 cycle·정산에만
존재**(catchup.py) — 종가창 유실은 그날 영구, 감지 장치 없음(R4).

---

## 2. 설계 결정

### D1. KRX 숫자형 선물 코드 정규화를 core 단일출처에 확립 (R2 근본)

**결정**: `core/quant_core/futures_contract.py`의 `dataset_for_contract`가 국내선물의 **KRX 숫자형
상품코드 프리픽스**(코스피200선물=`101`, 미니=`105`)도 역매핑한다. 프리픽스는 `_DOMESTIC_SPEC`
곁의 단일 테이블로 정의(상품 지식은 core 한 곳 — LS 모듈의 "독립 하드코딩 금지" 원칙 준수).

- LS 잔고 `101T9000`/`101V6000` → "코스피200선물", `105…` → 미니. 8자 길이 가드(주식 6자와 충돌 방지).
- **근거**: KRX 파생상품 표준 상품코드(101=KOSPI200 F, 105=미니 F). LS 가이드 t0441/t2301 예시와
  `broker_router.py`의 기존 주석("자신의 코드(101V6000 등)를 정규화")이 실측 일치.
- 대안(t8467 마스터 교차참조) 기각: t8467의 expcode는 `KR4+shcode` ISIN형으로 t0441의 KRX형과
  직접 브릿지 불가(실측), 마스터 다운로드 실패 시 신원계층 자체가 다운되는 결합 추가. 프리픽스
  테이블이 더 단순·roll 무관·무의존(원칙 3).

### D2. LS 역매퍼는 core로 위임 + 라우터 정규화 실패는 fail-loud (R2 표면화)

**결정**:
- `LsContractResolver.dataset_for_code_static`: LS 고유(shcode A01/A05) 매칭 후 **core
  `dataset_for_contract`로 위임**(KRX형+globex 중복 로직 제거 — 역매핑 지식도 한 곳).
- `broker_router.account_snapshot`: `_d4c` 실패 시 조용히 스킵하지 않고 **`log.error` + 포지션에
  `symbol_unmapped=True` 표식**. 원시 코드는 symbol로 유지(웹 표시·external 집계는 계속).
  이 표식이 D3의 파괴 차단 신호다. — "조용한 정규화 실패" 부류를 재은닉 불가능하게 만든다.

### D3. reconcile fail-safe — 신원계층 비정상 시 파괴적 정정 전면 차단 (R1 근본)

**결정**: `trader.reconcile_with_kis`는 다음 중 하나라도 참이면 **orphan 자동 차감/삭제 패스를
전부 건너뛴다**(보고·표면화는 그대로):
1. 스냅샷 `balance.fetch_failed` 비어있지 않음 — 구성된 계좌 leg 조회 실패로 포지션 목록이 불완전.
   (기존 결함: LS 선물 API 다운 시 선물 원장 전체가 orphan으로 보여 **전량 삭제**될 수 있었다 — R1과
   동일 부류의 잠복 사고.)
2. 포지션에 `symbol_unmapped=True` 존재 — 신원 매핑이 깨져 (symbol,side) 매칭 신뢰 불가.

차단 시: `log.error` + 결과에 `blocked`(사유)·`has_drift=True` → cycle summary로 서버/웹 표면화.
**원리**: 파괴적 자동 정정은 "매칭이 신뢰 가능"할 때만 정당하다. 신뢰 불가면 fail-safe(무동작+경보)가
유일하게 안전하다. 유저 실수동매도 자동 차감(승인된 제품 동작)은 신원계층 정상일 때 종전대로 유지.

### D4. 정산 시점 당일매매 잔존 불변식 감시 (R3 1단계 + R4 감지)

**결정**: `_run_settlement_locked`(15:50 KRX / close+5 US)에 **상태 불변식** 추가:
> "정산 시점에 당일매매(hold_days==0) 포지션이 원장에 남아 있으면 안 된다."

위반 시 `log.error` + error decision + summary `n_daytrade_unclosed` → 서버 타임라인 표면화.
잡 실행 여부가 아니라 **상태**를 검사하므로, 종가창 미실행(R4)·발주 거부·부분 체결 등 원인 무관하게
"당일 청산 실패" 부류 전체를 잡는다. 익일 아침 청산은 기존 로직이 보장(07-03 실측) — 이 감시는
그 사이 오버나이트 노출을 **당일 15:50에 즉시 인지**하게 한다.

catch-up 미추가 근거: KRX 종가창을 놓치면 장이 이미 닫혀(선물 15:45) 당일 청산 자체가 불가능 —
"보완 실행"은 물리적으로 없고, 가능한 근본 대응은 감지+경보+익일 보장뿐(정직한 한계 명시).
07-02 잡 미발화의 스케줄러 내부 기전은 이 감시가 다음 발생 시 즉시 증거를 만든다.

### D5. 범위 제외 (후속)

- **R5 commingle 귀속**: 계약별 net을 진실로, 전략별 귀속을 파생 계층으로 — 넷팅 설계 E11과 통합
  검토. 이번 PR의 신원계층 위에서 진행해야 안전.
- **결함 C**: 전략18 정의 `hold_days=0` 교정은 유저/웹, 컴파일러·저장 검증과 preview↔실행 정합은
  서버 변경 — 별도 PR. 로컬 종가창 가드(trader.py:1444~)는 올바른 방어라 유지.
- 07-02 스케줄러 잡 미발화의 내부 기전 규명: D4 감시가 재발 시 즉시 신호 — 그 증거로 후속 진단.

---

## 3. 불변식 (테스트 가능 형태)

| # | 불변식 | 강제 지점 |
|---|---|---|
| I1 | 브로커 선물 포지션은 정규화 실패 시 반드시 `symbol_unmapped` 표식+ERROR 로그를 남긴다(조용한 잔류 금지) | broker_router.account_snapshot |
| I2 | `fetch_failed` 또는 `symbol_unmapped` 존재 시 reconcile는 원장을 변경하지 않는다 | trader.reconcile_with_kis |
| I3 | 신원계층 정상 시 주식 외부매도 자동 차감은 종전과 동일(회귀 금지) | trader.reconcile_with_kis |
| I4 | `dataset_for_contract`: A01606(KIS)·101T9000/101V6000(LS KRX형)·GCM26(globex) → 상품명, 미등록 → None | core futures_contract |
| I5 | 정산 시 hold_days==0 포지션 잔존 → `n_daytrade_unclosed>0` + error decision | runner._run_settlement_locked |
| I6 | 주식 6자 코드(005930)는 KRX형 프리픽스에 오매칭되지 않는다 | core(8자 가드) |

## 4. 테스트 계획

- `core` — `dataset_for_contract` KRX형(I4·I6): 101T9000/101V6000→코스피200선물, 105…→미니,
  005930→None(주식), 미등록 8자→None.
- `local/tests/test_ls_contract_resolver.py` 확장 — `dataset_for_code_static`: shcode(A01/A05)
  종전 + KRX형 위임 + globex 위임 + 미등록 None.
- `local/tests/test_broker_router_dataset_cb.py` 확장 — 미매핑 코드 병합 시 `symbol_unmapped`
  표식+원시 symbol 유지(I1).
- `local/tests/test_reconcile_failsafe.py` 신규 — I2(fetch_failed 차단·unmapped 차단)·I3(정상 시
  주식 차감 유지)·blocked 표면화.
- `local/tests/test_settlement_daytrade_watchdog.py` 신규 — I5(잔존→경보·정상→0).
- 기존 전체 회귀: `PYTHONPATH=core python -m pytest local/tests core/tests -q` green.

## 5. 마이그레이션·롤아웃

1. 기존 분기 상태는 복원하지 않는다(사용자 결정 — 모의 초기화·재실행). 배포 전 사용자: 모의 계좌
   flat 정리 + 자동매매 초기화(원장 리셋).
2. 릴리스 게이트(별도 승인): PR 머지 → 로컬앱 버전 bump + PyInstaller zip → quantman-releases.
   **모의 재검증 체크리스트**: LS 선물 진입 후 reconcile `in_sync=1`(웹 정합성 경보 없음)·
   수동 매도 시나리오에서 주식 차감 유지·정산 15:50 `n_daytrade_unclosed=0`.
3. 인시던트 기록: `docs/incidents/2026-07-03-futures-ledger-divergence.md`(발생·대응·결과).

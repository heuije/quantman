# 자동매매 readiness pre-flight 프로브 설계 (Phase 3)

> **상태:** 설계 승인(2026-07-13, 조대표). 3a(읽기전용 CLI+메모리) 먼저 구현 → 3b(프로브) 릴리스.
> **담당:** 조대표 (자동매매 엔진). **전제:** Phase 1(건강 모니터 PR#397)·Phase 0/2(신호 emit PR#398) 배포됨.

## 1. 문제 (왜)

건강 모니터(evaluate_health)는 **유저의 최신 스냅샷**을 판정하고, 그 스냅샷 content(데이터·브로커·
판단)는 **클라가 사이클 때만** 갱신한다. 그래서:
- **시간 기반 조건(C1 앱생존·C2 동기화)** 은 cron */15로 24/7 신선(now 비교).
- **content 기반 조건(C3 데이터·C5 브로커·C7 판단·C8 발주·C9 정합)** 은 **장 시간 밖엔 stale**
  (지난 사이클 값). → "지금, 내일 개장을 앞두고 데이터·브로커·전략이 준비됐나"를 **사전 보증 못 함.**

현재 = *"터지면 곧 안다"*(반응형 탐지). 목표 = *"터지기 전에 안 터질 것을 보증"*(능동 사전점검).

## 2. 목표

장 시간 밖에서도 **주기(1시간)+온디맨드(웹·CLI)로 fresh·full 건강 진단** — 데이터·브로커·전략
준비상태를 **무주문으로 실제 exercise**해 서버가 상시 신선한 판정을 내게 한다.

## 3. 무주문 readiness 프로브 (클라)

`run_readiness_probe(trigger)` — 실 사이클과 같은 전제조건을 돌리되 **주문만 없음**:
1. 데이터 refresh(번들 다운로드+추출, ETag 조건부) → bundle 결과·n_failed (C3)
2. 라이브 전략 needed 심볼 로드 → needed/loaded/커버리지·missing (C3·C4)
3. 브로커 토큰 발급 + `account_snapshot` **read-only** → token_ok·account_query_ok (C5)
4. **dry decisions** — 엔진으로 결정 계산 → would_buy·skip_no_data·전략별 (C7)
5. `readiness` 스냅샷 push — **기존 payload 키(diagnostics·cycle_summary·health) 재사용** +
   `cycle_summary.kind="readiness_probe"` 마킹.

### ⚠ 무주문 보장 = 플래그가 아니라 구조 (fail-safe)
`probe=True` 플래그를 엔진에 심지 않는다(로직으로 우회 가능). 대신 **`NoOrderBroker` 래퍼**로
`make_broker()` 결과를 감싼다: 읽기(account·quote·balance)는 실 브로커에 위임, **모든 주문
메서드(buy/sell/cancel/…)는 no-op(기록만)**. 엔진이 발주를 시도해도 *구조적으로* 불가.

### ⚠ LS/KIS 파리티 (필수)
- 프로브는 `make_broker()`(secrets_store.get_active_broker() SSOT가 KIS/LS resolve)를 쓰므로
  **broker-agnostic** — NoOrderBroker는 **Broker 프로토콜** 기준 order 메서드만 no-op(자산군
  브로커 kis/ls·주식/선물/해외 전부 커버).
- `token_ok`/`account_query_ok`는 **양 브로커** 커버. **Phase 0/2에서 LS 토큰 만료 신호가
  미커버였던 갭을 여기서 닫는다**(analytics가 KIS `.kis_token.json`만 읽던 것 → 활성 브로커별
  토큰 캐시로 일반화, LS 토큰 포함).
- 검증은 KIS·LS 각각 모의/paper로 실측(브로커 패리티 원칙).

## 4. 트리거 (1시간 주기 + 온디맨드)

- **주기(클라 스케줄러):** **1시간마다** 자동. **라이브 전략 유저만**, **직전 실 사이클이 최근(예:
  <1h)이면 skip**(이미 신선), 번들 ETag 조건부(미변경 304·저렴).
- **온디맨드 웹:** 새 command `RUN_READINESS_PROBE`(서버→클라, `RUN_CYCLE_NOW`와 같은 채널) + `/admin` 버튼.
- **온디맨드 CLI:** ↓ §6.

## 5. 서버 소비 (evaluate_health 재사용)

readiness 스냅샷이 기존 payload 키를 **fresh하게** 채우므로 evaluate_health는 **거의 그대로**
C3·C5·C7를 신선 판정. 조정 1건:
- **dead-man's-switch(C8 0발주)는 프로브에 미적용** — 프로브는 설계상 0발주. `kind="readiness_probe"`
  를 `_TRADING_CYCLE_KINDS`에서 제외. 대신 프로브의 RED = **"데이터/브로커 미준비 OR 전략이
  skip_no_data"** = *"지금 장 열리면 실패할 것"* (C3/C5/C7이 판정).

## 6. CLI — 운영자·Claude Code 사전 디버깅 (핵심 요구)

`server/app/health_cli.py` (chat_analytics/manage.py CLI 패턴):
- `railway run python -m app.health_cli` → **전 유저 건강 즉시 출력**(compute_user_health·읽기전용).
  **3a·지금 동작**(마지막 스냅샷 기준). 클라 변경 불요. `--user ID`·`--json`·`--red-only`·`-v`.
- `railway run python -m app.health_cli --probe [--all|--user ID]` → **3b**: `RUN_READINESS_PROBE`
  enqueue → fresh readiness 도착까지 poll → full 신선 진단 출력. Claude Code가 장 밖에서 전 유저
  준비상태를 능동 확인.

## 7. 정직한 비용·안전

- **1시간 full 프로브** — 라이브유저만·직전사이클 skip·ETag 304·dry 엔진 수초라 부하 관리됨. 브로커
  토큰 호출 부하는 릴리스 후 실측.
- **무주문 = NoOrderBroker 구조 보장**(플래그 아님).
- 클라 변경(프로브·스케줄러·command·NoOrderBroker·LS토큰)은 **릴리스 후 발효**. 서버(evaluate_health
  readiness·CLI·command enqueue)는 **하위호환**.

## 8. 검증

- 3a: CLI가 prod에서 compute_user_health를 정확히 출력(읽기전용 실측).
- 3b: NoOrderBroker가 **어떤 주문도 안 냄**(단위테스트로 order 메서드 no-op 잠금) · 프로브가
  KIS·LS 각각 paper/모의로 전제조건 exercise · readiness 스냅샷이 서버 fresh 판정 갱신 · dead-man's
  -switch가 프로브 0발주를 오탐 안 함.

## 9. 롤아웃

- **3a (지금·릴리스 불요):** 읽기전용 `health_cli` + 메모리 기억. Claude Code 즉시 전 유저 진단
  (마지막 스냅샷·stale content 한계 명시).
- **3b (릴리스 필요):** readiness 프로브 + NoOrderBroker + 스케줄러 1h + `RUN_READINESS_PROBE`
  (웹·CLI) + evaluate_health readiness 소비 + LS 토큰 커버. KIS·LS 각각 모의 검증 후 릴리스.

## 10. 한계 (정직)

- 프로브는 *dry* — 실제 발주·체결의 브로커 수락(증거금·NewOrdAbleQty)까지는 보증 못 함(계좌 상태는
  read-only로 봄). "사이징 반영"과 "브로커 수락"의 간극은 남음.
- 1시간 주기라 그 사이 발생한 환경 변화(토큰 만료·디스크풀)는 최대 1시간 지연 탐지(온디맨드로 보완).
- 클라 자기보고 신뢰(성공처럼 보이는 오답은 못 잡음 — 유일 교차검증 C9).

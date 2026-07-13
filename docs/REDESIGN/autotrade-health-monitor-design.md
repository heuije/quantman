# 자동매매 건강 모니터 설계 (per-user · end-to-end observability)

> **상태:** Phase 1(서버·PR#397) 배포·프로덕션 검증 완료. Phase 0(확정결함 3건)·Phase 2(구조화
> emit + 서버 소비) 구현·검증 완료(브랜치 feat/autotrade-health-emit). diagnostics를 전 push에
> 부착하는 하위항목은 **보류**(egress 경로 위험 대비 가치 낮음 — cron+일일 사이클이 이미 커버).
> **담당:** 조대표 (자동매매 엔진). **범위 경계:** 서버측 관측·평가·운영자 알림·admin 패널 + 로컬 emit.

## 1. 문제 (왜)

mwmw CON.parquet 인시던트에서 **자동매매가 설치 이래 발주 0**인 총체적 실패가 **며칠간 서버에서
미탐지**됐다. 근본은 감시 설계의 구멍이다:

- 자동매매 건강 텔레메트리 전량이 `SyncSnapshot.payload`(free-form JSON)에 갇혀 있고,
  **`admin_metrics`는 `SyncSnapshot`을 아예 조회하지 않는다** → 운영자가 오늘 유저에 대해
  볼 수 있는 자동매매 신호는 `local_app_at`(생존 타임스탬프) **하나뿐**.
- `_check_alerts`는 kill_switch·drawdown·preview_missing·손실·reconcile·slippage만 보고,
  **데이터로드율·0발주·번들실패는 목록에 없다.** 그나마 **유저 본인 webhook**으로만 발송.
- "발주 0"이 정상 조용한 날과 **픽셀 단위로 동일**해 침묵이 경보를 못 낸다.

교훈: 우리는 **생존**(앱이 ping하나)과 **집계 활동**(DAU·백테스트)만 봤지, **유저별 end-to-end
성공 전제**를 본 적이 없다. 총체적 침묵 실패는 "무엇이 일어나야 하는가"가 정의되지 않으면
경보를 못 낸다.

## 2. 원칙

1. **per-user · end-to-end.** "앱이 켜졌나"가 아니라 "데이터→신호→발주→정합"을 본다.
2. **부재에 경보.** 명시적 에러뿐 아니라 0발주·push 공백 같은 *침묵*에 알람.
3. **서버 우선.** 데이터는 이미 payload에 있다 — Phase 1은 **소비**만으로 대부분 커버(클라 릴리스 불요).
4. **보안 불변식 유지.** 신호는 플래그·카운트·타임스탬프뿐. 자격증명·계좌번호·원시주문 서버 미유입.
5. **Neon egress 절약.** 스캔은 필요 필드만 projection(ETag tag-first 패턴, egress 인시던트 재발 방지).

## 3. MECE 정상작동 조건 → 건강 규칙

| 조건 | RED 규칙 | 신호 원천(payload 키) | Phase |
|---|---|---|---|
| **C1 런타임** | 개장 시간대 heartbeat stale(>임계) | `HeartbeatEvent.at`(기존) | P1 |
| **C2 연결** | heartbeat 정상인데 스냅샷 push N시간 공백 | `SyncSnapshot.received_at`(기존) | P1 |
| **C3 데이터 신선도** | `diagnostics.dataset.loaded ≪ needed` or `bundle.result=failed` | `diagnostics`(v0.9.72·이미 emit) | P1 |
| **C4 전략 유효성** | 라이브 전략 有인데 전 전략 skipped/유효후보 0 | `next_day_preview.by_strategy[].skipped`(서버 계산) | P1 |
| **C5 브로커/계좌** | 브로커 미등록 | `health.account_handles`(기존) | P1 |
| " | 토큰 만료·증거금 부족 | 건강 플래그(신규 emit) | **P2** |
| **C6 사이클 실행** | 예정 사이클 미완료(error/미도달) | `cycle_summary.error`+heartbeat(기존) | P1 |
| **C7 판단 산출** | 데이터 결손으로 전량 skip_no_data | `decisions[].action` + `diagnostics.dataset`(기존) | P1 |
| **C8 발주** | 라이브+사이클 실행+0발주+상류결손 | `cycle_summary.n_buy_placed/n_bought`(기존) | P1 |
| **C9 사후 정합** | reconcile drift/external_extras 지속 | `reconciliation.has_drift`(기존) | P1 |

→ **9조건 중 7개가 Phase 1(서버만)** 에서 커버. C5(토큰·증거금)·C7(정밀)만 P2.

## 4. Dead-man's-switch (핵심 · 단순 조합 휴리스틱)

단일 최신 스냅샷 + heartbeat + 라이브 전략 수로 판정(시장 캘린더 불요 — "사이클이 실제 돌았는데
데이터결손"이 CON 시그니처라 그걸 직접 본다):

```
RED "데이터/발주 실패 의심"  ← 라이브 전략≥1 AND 최근 사이클 실행됨(cycle_summary.kind∈cycle/day_trade_close)
                              AND n_bought==0 AND (dataset.loaded ≪ needed OR decisions에 skip_no_data)
RED "앱 다운"                ← 라이브 전략≥1 AND heartbeat age > STALE_MIN
RED "동기화 실패"            ← heartbeat 정상인데 스냅샷 age > PUSH_GAP_HR
AMBER "포지션 불일치"        ← reconciliation.has_drift/external_extras 존재
```
임계 상수는 `health_monitor.py` 상단에 명시(초기값: STALE_MIN=30, PUSH_GAP_HR=6, LOAD_RATIO_RED=0.5).

## 5. 아키텍처 (3층)

### 5.1 Emit (신호 원천)
Phase 1은 **이미 payload에 있는 것만 소비** — `cycle_summary`·`diagnostics`·`reconciliation`·
`decisions`·`health.account_handles` + `HeartbeatEvent`. 신규 emit 없음.

### 5.2 Evaluate — `server/app/health_monitor.py` (신규 · 순수함수)
`admin_metrics`/`chat_analytics` 패턴 미러(입력→dict, hermetic 테스트).
- `evaluate_health(payload, *, heartbeat_at, live_strategies, now, prior_status=None) -> dict`
  순수함수. 각 조건 `{status: GREEN|AMBER|RED, detail: str}` + `overall` + `red_reasons[]`.
- `compute_user_health(session, *, now=None) -> list[dict]`
  유저별 최신 스냅샷(**payload는 projection**) + 최신 heartbeat + 라이브 전략 수 로드 →
  evaluate_health 호출 → per-user 건강 행. `compute_admin_metrics`와 동거.

### 5.3 Alert + Surface
- **`/admin/health`** (admin.py, require_admin) → `compute_user_health` 반환. 웹 admin 패널이 소비.
- **운영자 능동 푸시** — `config.Settings.OPERATOR_ALERT_WEBHOOK`(env `QP_OPERATOR_ALERT_WEBHOOK`).
  RED 진입 시 `sync._post_webhook`(재사용) 운영자 채널로 발송. **유저 webhook 아님.**
- **쿨다운/상태** — 신규 `HealthAlertState{user_id, condition, status, since, last_alerted_at}`
  테이블(운영자 알림은 유저 설정과 무관해 UserSettings 아닌 별도). 상태 *전이* 시에만 발송(스팸 방지).
- **Dead-man's-switch cron** — main.py에 `CronTrigger(minute="*/15", timezone=_TZ_SEOUL)` 잡.
  전 유저 `compute_user_health` → 신규 RED에 운영자 알림. **on-ingest로는 못 잡는 "push 없는
  침묵 유저"를 cron이 잡는다.** (payload projection으로 egress 최소화.)
- **웹 admin 패널** — `/admin` 화면에 유저별 조건 신호등 + "마지막 발주 후 N일"·로드율·drift·
  브로커 준비·마지막 push. DESIGN.md 토큰 사용.

## 6. 보안·성능 불변식
- payload에서 읽는 키는 전부 안전정보(플래그·카운트·심볼·타임스탬프). 자격증명·계좌번호·원시주문 미접근.
- cron/admin 스캔은 `SyncSnapshot.payload` 전체가 아니라 **필요 JSON 필드만** projection(Neon egress).
  유저별 최신 1건만(`received_at` desc, per user) 조회.

## 7. 검증 (검증된 해결책만)
1. **evaluator 단위 테스트** — 각 조건 GREEN/AMBER/RED 케이스(`tests/test_health_monitor.py`).
2. **mwmw replay(결정적 증거)** — mwmw의 실제 07-13 pre-fix 스냅샷 형상(`skip_no_data`·
   `dataset.loaded=1,needed=129`·`n_bought=0`)을 evaluator에 넣어 **RED "데이터/발주 실패 의심"**
   을 assert. post-fix(발주 성공) 형상은 GREEN. → 과거 사고에 실제로 작동함을 실데이터로 증명.
3. **cron 알림** — 시뮬 유저가 RED 진입 시 운영자 알림 1회·전이 없으면 무발송(쿨다운) 테스트.
4. **admin 패널** — 브라우저로 신호등·수치 렌더 확인.

## 8. 롤아웃
- **Phase 1(이 PR·서버 전용):** health_monitor + compute_user_health + /admin/health + cron +
  운영자 푸시 + HealthAlertState + 웹 패널 + 테스트. C1·C2·C3·C4(부분)·C6·C7·C8·C9.
- **Phase 2(별도·클라 릴리스):** 건강 플래그(broker_ready·token_ok·account_query_ok·sizable) emit +
  `n_skip_no_data` 승격 + diagnostics를 전 push에 부착 → C5·C7 GREEN화.
- **Phase 0(확정결함, P1 또는 별도):** 죽은 KIS 토큰 신호(+LS)·정산 실패 미push·KIS헬스 오분류.

## 9. 한계 (정직)
- **예방 아닌 탐지.** CON류 버그를 막지 않음(sanitize가 담당) — 다음 미지 실패를 빨리 *발견*.
- **총체적 실패는 잡되 부분 오작동(5중 3발주·오사이징)은 못 잡음** — 단순 휴리스틱 선택의 대가(P2+ 전략별 모델 전까지).
- **클라 자기보고 신뢰** — "성공처럼 보이는 오답"은 못 잡음(유일 교차검증 C9 reconcile).
- **오탐 가능성** — 진짜 조용한 전략을 0발주로 오인 가능 → 로드율/skip과 *조합*해 억제, 초기 임계 튜닝 필요.

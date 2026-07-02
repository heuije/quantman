# 트레이딩(/monitor) 재설계 — 1차 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development(권장) 또는 executing-plans로 task 단위 구현. 각 step은 `- [ ]` 체크박스.

**Goal:** 트레이딩 화면을 raw 데이터 덤프에서 **성과 우선·해석된 실행 현황**(성과 히어로→상태→오늘 활동+넷팅→전략별 기여→보유→감사로그 접힘)으로 재구성한다. 웹 전용(서버·엔진 무변경).

**Architecture:** `Monitor.tsx`가 소비하는 기존 `SyncSnapshot.payload`(추가 fetch 없음)만으로 새 계층을 조립한다. 새 표시 컴포넌트는 focused 파일 2개(`MonitorHero.tsx`·`MonitorActivity.tsx`)로 분리하고, 기존 재사용 컴포넌트(PositionDetailCards·TradingTimeline·TradeOutcomes·CsvExportBar)는 감사 섹션으로 접는다. 색·타이포는 DESIGN.md 토큰(`index.css` :root)만.

**Tech Stack:** React 18 + TypeScript + Vite. recharts(자본곡선). 스타일=`index.css` 전역 클래스 + DESIGN 토큰. 검증=`tsc -b` + 브라우저 preview.

**시각 기준:** 목업 `trading_redesign_performance_first_mockup`(이 세션)이 각 섹션의 레이아웃·클래스 의도를 보여준다. 아래 task는 그 목업을 실데이터에 바인딩한다.

---

## 데이터 바인딩 사실 (전 task 공유 — 근거: web/src/types.ts)

- `snap.payload.balance` = `{ cash, total_eval }`.
- `snap.payload.equity` = `{ date, value }[]` — **현재 미사용**, 자본곡선·누적수익률 원천.
- `snap.payload.strategy_pnl` = `{ by_strategy: {strategy, trades, win_rate, pnl, today_pnl, week_pnl, month_pnl}[], total: {today, week, month, all} }`. ⚠ **`win_rate`는 이미 백분율**(×100 금지 — fix/strategies-winrate-double-pct 교훈).
- `snap.payload.kill_switch` = `{ active, since, reason, day_start_equity, day_start_date }`. **오늘 수익률 기준자본 = day_start_equity.**
- `snap.payload.cycle_summary` = CycleSummary(+ Task 0에서 `n_netted`·`commission_saved_krw` 추가).
- `snap.payload.drawdown` = `{ depth_pct, days_since_high, high, current, high_date }`.
- `snap.payload.reconciliation` = `{ ledger_orphans[], external_extras[], has_drift, applied[] }`.
- `snap.payload.health` = `{ last_cycle_ts, warnings[], account_handles[], active_account_ids[] }`.
- `snap.payload.next_day_preview` = `{ available, summary:{n_buy_candidates, est_total_buy_amount, n_holding, cash}, by_strategy[], exit_candidates[] }`.
- `snap.payload.auto_status` = `"running"|"paused"|"stopped"`. `snap.received_at`·`snap.last_heartbeat_at`.
- `snap.payload.positions` = PositionRich[] (`symbol, name, qty, avg_price, eval_price, strategy_name, cur_return_pct`).
- **벤치마크 없음**: 라이브 스냅샷에 benchmark_equity 부재 → 히어로에 "코스피 대비" 미표시(스펙 §7 대체안 확정).

## 파일 구조

- Modify `web/src/types.ts` — CycleSummary에 넷팅 2필드.
- Create `web/src/components/MonitorHero.tsx` — `PerformanceHero`·`StatusStrip`.
- Create `web/src/components/MonitorActivity.tsx` — `TodayActivity`(넷팅 카드 포함)·`StrategyContribution`.
- Modify `web/src/components/MonitorCards.tsx` — `PositionDetailCards` 파이 색 토큰화.
- Modify `web/src/pages/Monitor.tsx` — 새 계층 조립 + 감사 섹션 `<details>` 접기 + 미연동 온보딩 우선.
- Modify `web/src/index.css` — 새 클래스(토큰 재사용, 새 색 도입 금지).

---

## Task 0: CycleSummary 넷팅 필드

**Files:** Modify `web/src/types.ts:629-639`

- [ ] **Step 1: 필드 추가**

```ts
export interface CycleSummary {
  today?: string; n_strategies?: number;
  n_bought?: number; n_sold?: number;
  n_skip_held?: number;
  n_rejected?: number; n_unfilled?: number; n_errors?: number;
  n_unparseable_orphan?: number;
  kill_switch?: boolean;
  equity_pre?: number; equity_post?: number;
  us_realtime_unavailable?: boolean;
  // 넷팅(발주창 넷팅) — 로컬 cycle_summary가 이미 실어 보냄(서버 passthrough). 값>0일 때만 표시.
  n_netted?: number;
  commission_saved_krw?: number;
}
```

- [ ] **Step 2: 타입체크** — `cd web && npx tsc -b` → PASS(에러 0).
- [ ] **Step 3: Commit** — `git add web/src/types.ts && git commit -m "feat(web): CycleSummary에 넷팅 집계 필드(n_netted·commission_saved_krw)"`

---

## Task 1: 성과 히어로 (PerformanceHero)

**Files:** Create `web/src/components/MonitorHero.tsx`

지표 정의(위 데이터 사실 기준):
- 총 평가금액 = `balance.total_eval`.
- 오늘 수익률% = `day_start_equity`가 있으면 `(total_eval − day_start_equity)/day_start_equity×100`, 없으면 `—`. 오늘 손익(KRW) = `strategy_pnl.total.today`.
- 누적 손익(KRW) = `strategy_pnl.total.all`. 누적 수익률% = `equity[]` 존재 시 `(equity.at(-1).value/equity[0].value − 1)×100`, 없으면 `—`.
- 자본곡선 = `equity[]`(골드 선 `#d4a738`, recharts, `isAnimationActive={false}`).
- 기간 토글(오늘/7일/30일/전체)은 자본곡선 표시 구간만 slice(수익률 KPI는 누적 고정 — 토글은 곡선 range만).
- 방향색: 값≥0 → `.pos`(빨강 `--up`), <0 → `.neg`(파랑 `--down`).

- [ ] **Step 1: 컴포넌트 작성** — props `{ balance, equity, strategyPnl, killSwitch }`(타입은 SyncSnapshot payload 필드 그대로). 목업 `.tm-hero` 구조를 `.panel` + `.stat` 패턴으로. 숫자는 기존 `won()`(천단위+원)·`toFixed` 사용. 데이터 없으면(`equity` 빈 배열) 곡선 대신 "연동 후 자본곡선이 표시됩니다" 안내.

```tsx
// 핵심 파생만 발췌 — 전체 JSX는 목업 tm-hero를 클래스 매핑.
const totalEval = balance?.total_eval ?? null;
const dayStart = killSwitch?.day_start_equity ?? null;
const todayPct = totalEval != null && dayStart ? (totalEval - dayStart) / dayStart * 100 : null;
const cumPct = equity && equity.length > 1
  ? (equity[equity.length - 1].value / equity[0].value - 1) * 100 : null;
const cumPnl = strategyPnl?.total.all ?? null;
const todayPnl = strategyPnl?.total.today ?? null;
```

- [ ] **Step 2: 타입체크** — `npx tsc -b` PASS.
- [ ] **Step 3: Commit** — `git commit -m "feat(web): 트레이딩 성과 히어로(누적수익률·오늘손익·자본곡선)"`

---

## Task 2: 상태 바 (StatusStrip)

**Files:** `web/src/components/MonitorHero.tsx`(같은 파일에 export)

- 칩: 자동매매 상태(`auto_status` → 🟢실행중/⏸일시정지/⚪정지), 활성 계좌(`health.account_handles`에서 `active_account_ids[0]` 매칭 → 별칭 + 모의/실전 배지; 기존 `accountLabel` 재사용), 연결·하트비트(`last_heartbeat_at`·`received_at` 중 최신과 now 차이 → `<5분`=초록·`<30분`=앰버·그외 빨강 + "N분 전"), 다음 사이클(다음 08:55/15:40 KST까지 카운트다운 — 클라 시계).
- 경보(문제 시에만 확장, 각 "다음 조치" + 버튼):
  - `kill_switch.active` → 빨강 "킬스위치 발동: {reason}" + [해제](`send("RESET_KILL_SWITCH")`).
  - `drawdown.depth_pct <= -10` → 앰버 "고점 대비 {depth_pct}% 하락({days_since_high}일 전 고점)" + "peak 회복 시 자동 해제".
  - `reconciliation.has_drift` → 앰버 "N종목 정합성 불일치(수동매매 추정)" + [지금 점검](`send("RECONCILE_NOW")`).
  - `cycle_summary.n_unparseable_orphan > 0` → 앰버 "고아 포지션 N(전략 삭제·구버전)" + 안내.
  - `cycle_summary.us_realtime_unavailable` → 앰버 "미국 실시간 미신청 — 장중 손절 미동작".
  - 경보 0건이면 "상태 이상 없음 ✓"(초록 한 줄).

- [ ] **Step 1: 작성** — props `{ autoStatus, health, receivedAt, lastHeartbeatAt, killSwitch, drawdown, reconciliation, cycleSummary, onCommand }`. `onCommand(type)`는 Monitor의 `send`를 주입. 목업 `.tm-status` 구조.
- [ ] **Step 2: 타입체크** PASS.
- [ ] **Step 3: Commit** — `git commit -m "feat(web): 트레이딩 상태 바(상태 칩 + 다음조치 경보)"`

---

## Task 3: 오늘 활동 + 넷팅 (TodayActivity)

**Files:** Create `web/src/components/MonitorActivity.tsx`

- **넷팅 카드**(핵심): `cycle_summary.n_netted`·`commission_saved_krw` 둘 다 있고 `n_netted > 0`일 때만 렌더 — 목업 `.tm-net`("넷팅 {n_netted}건 · 수수료 약 {commission_saved_krw원} 절약" + 설명). 없으면 생략.
- **거래 라인**: `snap.payload.decisions`(오늘 사이클 결정) 또는 `recent_orders` 중 오늘분을 평문화. 각 라인 `{액션 태그}{종목} {수량} · {시각} — {reason 평문}`. 신호 reason의 코드조각은 `title` 툴팁으로 원문 보존(평문 매핑 없으면 원문 노출 + 툴팁 동일).
- 액션 태그 색: 매수=`--up`, 매도/청산=`--down`, 넷팅=`--accent`.

- [ ] **Step 1: 작성** — props `{ cycleSummary, decisions, recentOrders }`. `n_bought`/`n_sold` 헤더 요약 + 넷팅 카드 + 거래 라인 리스트. `won()` 재사용.
- [ ] **Step 2: 타입체크** PASS.
- [ ] **Step 3: Commit** — `git commit -m "feat(web): 트레이딩 오늘 활동 + 넷팅 절감 카드"`

---

## Task 4: 전략별 성과 기여 (StrategyContribution)

**Files:** `web/src/components/MonitorActivity.tsx`(같은 파일에 export)

- `strategy_pnl.by_strategy`를 `pnl` 큰 순 정렬. 카드마다: 전략명 + 손익(오늘=`today_pnl`/7일=`week_pnl`/30일=`month_pnl`/누적=`pnl`, 방향색) + 승률(`win_rate` **그대로**·×100 금지) + 보유(=`positions.filter(strategy_name==)` 개수 + 평균 `cur_return_pct`) + 다음 예정매매(`next_day_preview.by_strategy`에서 해당 전략 매핑, 평문). "오늘 실행됨" 배지는 `cycle_summary.today`가 오늘이고 해당 전략이 decisions에 등장하면 표시.
- 목업 `.tm-scard` 구조.

- [ ] **Step 1: 작성** — props `{ strategyPnl, positions, nextDayPreview, cycleSummary, decisions }`.
- [ ] **Step 2: 타입체크** PASS.
- [ ] **Step 3: Commit** — `git commit -m "feat(web): 트레이딩 전략별 성과 기여 카드"`

---

## Task 5: 보유 종목 파이 토큰화

**Files:** Modify `web/src/components/MonitorCards.tsx`(PositionDetailCards의 `PIE_COLORS`)

- 하드코딩 `PIE_COLORS` 배열을 DESIGN §8 계열 네이비 톤으로 교체: `["#264a85", "#3a629f", "#1d3a63", "#5578b0", "#0f2342"]`(회색 금지·골드는 선 전용이라 파이엔 미사용). 표는 그대로 두되 `cur_return_pct` 기준 내림차순 정렬 추가.

- [ ] **Step 1: 색 배열 교체 + 정렬** — 기존 렌더 구조 유지, 색·정렬만.
- [ ] **Step 2: 타입체크** PASS.
- [ ] **Step 3: Commit** — `git commit -m "refactor(web): 보유 파이 색 DESIGN 토큰화 + 수익률 정렬"`

---

## Task 6: 감사 로그 접기

**Files:** Modify `web/src/pages/Monitor.tsx`

- 기존 미체결·최근 주문·최근 사이클·최근 명령·TradeOutcomes·TradingTimeline 상세를 하나의 `<details className="audit-fold">`(summary "감사 로그 · 미체결 · 주문 · 사이클 · 명령")로 감싼다. CsvExportBar는 그 안 유지. 기본 접힘.

- [ ] **Step 1: 래핑** — 기존 JSX를 잘라 `<details>` 안으로 이동(내용 변경 없음).
- [ ] **Step 2: 타입체크** PASS.
- [ ] **Step 3: Commit** — `git commit -m "refactor(web): 트레이딩 감사 로그 섹션 접기"`

---

## Task 7: Monitor.tsx 조립 + 온보딩 우선

**Files:** Modify `web/src/pages/Monitor.tsx`

- 렌더 순서: (미연동이면 `PairingOnboarding`을 히어로 자리에 우선) → ①`<PerformanceHero>` → ②`<StatusStrip>` → ③`<TodayActivity>` → ④`<StrategyContribution>` → ⑤`<PositionDetailCards>` → ⑥`<details>` 감사. 기존 import 정리(새 컴포넌트 추가). `send`를 StatusStrip `onCommand`로 주입.
- 미연동 판정: 기존 페어링 게이트 로직 재사용(`devices`/health 기준 현행 유지).

- [ ] **Step 1: 조립** — 새 컴포넌트 import + 순서 배치 + props 전달(전부 `snap.payload.*`).
- [ ] **Step 2: 타입체크** PASS.
- [ ] **Step 3: Commit** — `git commit -m "feat(web): 트레이딩 성과 우선 계층 조립"`

---

## Task 8: 스타일 (index.css)

**Files:** Modify `web/src/index.css`

- 목업의 `.tm-*` 의도를 프로젝트 클래스로 옮긴 새 규칙 추가(hero grid·status chips·alert·netting card·strategy contribution card). **새 색/폰트/간격 토큰 도입 금지** — 기존 `var(--*)` 재사용. 파이·곡선 색은 컴포넌트 인라인(DESIGN §8 값).

- [ ] **Step 1: 클래스 추가** — 각 컴포넌트가 참조하는 클래스명과 1:1.
- [ ] **Step 2: Commit** — `git commit -m "style(web): 트레이딩 재설계 컴포넌트 스타일(토큰 재사용)"`

---

## Task 9: 통합 검증 (브라우저)

**Files:** 없음(검증)

- [ ] **Step 1: 빌드** — `cd web && npx tsc -b && npx vite build` → 에러 0.
- [ ] **Step 2: dev 서버 + preview 도구** — `.claude/launch.json`에 web dev(`vite`, port 5173) 등록 후 preview_start. `/monitor` 렌더 확인: 콘솔 에러 0(preview_console_logs), 섹션 구조(preview_snapshot), 방향색·다크테마(preview_inspect: `.stat` 색이 `--up`/`--down`), 넷팅 카드는 데이터 없으면 미표시.
- [ ] **Step 3: 반응형·상태** — preview_resize(mobile) 레이아웃 붕괴 없음. 미연동 계정 상태에서 온보딩 우선 표시.
- [ ] **Step 4: 정직 한계 기록** — 라이브 `strategy_pnl`·`equity`·넷팅 값은 연동+체결 데이터가 있어야 실수치 렌더 → dev 데이터 없으면 빈/온보딩 상태만 검증 가능. 실수치 시각검증은 로컬 시각 테스트환경([[project_local_visual_testenv]]) 또는 사장님 실계정에서.

---

## 자기검토 (스펙 대조)

- **스펙 §3 ①~⑥** → Task 1~7 각각 매핑(①T1·②T2·③T3·④T4·⑤T5·⑥T6·조립T7). ✅
- **§5 넷팅 웹표시** → Task 0(타입)+Task 3(렌더). 서버 무변경 확인됨. ✅
- **§6 디자인 토큰** → Task 8 새 색 금지·§8 차트색. ✅
- **§7 데이터 가용성** → 벤치마크 미표시 확정(Task 1), 월별 히스토리는 내 전략(2차) 소관이라 1차 스코프 밖. ✅
- **§8 비목표** → 서버·엔진 무변경, 새 엔드포인트 없음(전부 기존 payload). ✅
- **§10 검증** → Task 9 브라우저 preview + 한계 명시. ✅
- **Placeholder 스캔:** 각 task에 파일·데이터필드·클래스·검증·commit 명시. 전 JSX를 반복하지 않은 곳은 "목업 구조를 클래스 매핑"으로 위임(목업이 시각 SSOT). 타입/필드명은 데이터 사실 절과 일치.

## 2차 (내 전략) — 별도 계획

트레이딩 1차 완료·브라우저 검증 후 `web-trading-strategies-ux-design.md §4` 기준으로 내 전략(백테스트 전용 카드 + 상세 버전diff·연동준비도) 계획을 별도 작성한다.

# 내 전략(/strategies) 재설계 — 2차 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development(권장)/executing-plans. `- [ ]` 체크박스.

**Goal:** 「내 전략」을 **자동매매 이전 백테스트 연구 라이브러리**로 재설계한다 — 라이브 손익·평가금액·보유(=트레이딩 탭 전용)를 제거하고, 카드·상세를 **백테스트 성과 중심**으로.

**Architecture:** 목록은 `listStrategies()` + 전략별 `listStrategyBacktests(id)` 병렬 fetch로 최신 백테스트 메트릭을 카드에 바인딩. 상세는 라이브 "현황" P&L을 제거하고 백테스트 요약·연동 상태·설정·버전·백테스트 내역만. 서버 무변경.

**Tech Stack:** React+TS+Vite. 색=DESIGN 토큰. 검증=`tsc -b`+`eslint`+`vite build`.

**시각 기준:** 목업 `my_strategies_backtest_only_redesign_mockup`(이 세션).

---

## 데이터 사실 (근거: web/src/types.ts·api.ts·StrategyDetail.tsx)

- `listStrategies()` → `StrategyRow[]`(`id, name, run_mode("draft"|"paper"|"live"), engine, definition, created_at, updated_at, account_ref, account_broker`). **백테스트 메트릭 미포함.**
- `listStrategyBacktests(id)` → `BacktestRunSummary[]`(`id, created_at, initial_capital, metrics: Record<string,number|null>, version_no?, start?, end?`). **전략별 호출**(N+1·병렬).
- **메트릭 키·스케일**(StrategyDetail.tsx:722-738와 동일): `total_return`·`cagr`·`max_drawdown`는 **분수 → ×100 표시**, `sharpe`는 원값. (⚠ 라이브 `win_rate`는 이미 %였던 것과 반대 — 여기선 ×100이 맞음.)
- 목표 평문화 헬퍼는 기존 `summarizeIrUniverse`·`summarizeTargets`·`IR_*_LABEL` 재사용.
- `getStrategyStats(id)`(라이브 P&L)는 **상세에서 제거**(재스코프). `account_handles`는 snapshot health(계좌 배지·연동 표시용).

## 파일 구조

- Modify `web/src/pages/Strategies.tsx` — 데이터 fetch(백테스트 병렬) + 카드 백테스트 전용 재작성.
- Modify `web/src/pages/StrategyDetail.tsx` — 라이브 "현황" P&L 제거 + 백테스트 요약/연동 상태 우선.
- Modify `web/src/index.css` — 전략 카드 클래스(토큰 재사용).

---

## Task S1: 내 전략 목록 — 백테스트 전용 카드

**Files:** Modify `web/src/pages/Strategies.tsx`

- [ ] **Step 1: 데이터 fetch 교체** — `load()`에서 snapshot(라이브 pnl) 대신 백테스트 병렬 fetch.

```tsx
function load() {
  setErr("");
  api.listStrategies()
    .then(async (rs) => {
      setRows(rs);
      // 전략별 최신 백테스트 메트릭(병렬 · N+1이지만 요약 엔드포인트라 저비용).
      const entries = await Promise.all(rs.map(async (s) => {
        try {
          const bts = await api.listStrategyBacktests(s.id);
          const latest = bts.slice().sort((a, b) => (a.created_at < b.created_at ? 1 : -1))[0];
          return [s.id, latest ?? null] as const;
        } catch { return [s.id, null] as const; }
      }));
      setBtByStrategy(Object.fromEntries(entries));
    })
    .catch((e) => setErr((e as Error).message))
    .finally(() => setLoaded(true));
}
```
State: `const [btByStrategy, setBtByStrategy] = useState<Record<number, BacktestRunSummary | null>>({});`
Import: add `BacktestRunSummary` type; remove `SyncSnapshot`·snapshot 사용.

- [ ] **Step 2: StrategyCard 백테스트 전용 재작성** — props `{ strategy, backtest }`(라이브 pnl·positionCount·handles 제거). 목업 `.mb-card` 구조:
  - 헤드: 이름 + 상태(run_mode: live→"트레이딩 연동됨"(green dot)·paper→"모의" 배지·draft→"초안" 배지).
  - 대상: 기존 `summarizeIrUniverse`/`summarizeTargets` + 조건 칩(있으면).
  - 백테스트 KPI 4칸: 총수익(`m.total_return*100`·`.pos/.neg`)·CAGR(`m.cagr*100`)·MDD(`m.max_drawdown*100`·`.neg`)·샤프(`m.sharpe` 원값). `backtest`가 null이면 "아직 백테스트를 실행하지 않았습니다" + 액션.
  - 푸: 백테스트 기간(`start`~`end`) · 최종 실행일(`created_at`) · 연동 시 "실행 현황은 트레이딩 →"(Link `/monitor`), draft면 "트레이딩 연동"/"열기".
  - **라이브 손익·평가금액·보유수 미표시**(재스코프).

```tsx
const btPct = (v: number | null | undefined) =>
  v == null ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "%";
const btCls = (v: number | null | undefined) => (v == null ? "" : v >= 0 ? "pos" : "neg");
// m = backtest?.metrics ?? {}; total_return/cagr/max_drawdown는 분수, sharpe 원값.
```

- [ ] **Step 3: 필터 탭 유지** — 기존 전체/실전/모의/초안(run_mode) 카운트. 라이브 롤업 없음.
- [ ] **Step 4: 타입체크·린트** — `npx tsc -b && npx eslint src/pages/Strategies.tsx` PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(web): 내 전략 목록 백테스트 전용 카드(라이브 손익 제거)"`

---

## Task S2: 상세 — 라이브 P&L 제거 + 백테스트 우선

**Files:** Modify `web/src/pages/StrategyDetail.tsx`

- [ ] **Step 1: "현황" 탭 라이브 P&L 제거** — `getStrategyStats` 기반 `pnl_total`·`pnl_pct`·`win_rate`·`traded_amount`·`n_positions` StatBox 제거(재스코프: 실행 손익은 트레이딩). 유지: 운용 모드·paper/live 기간·계좌 바인딩 섹션(연동 setup). 상단에 대표 백테스트 성과(최신 run: 총수익·CAGR·MDD·샤프) 요약 + 연동 상태.
- [ ] **Step 2: 연동된 전략엔 "실행 현황 → 트레이딩" 링크** 추가(손익 숫자 대신).
- [ ] **Step 3: 타입체크·린트** PASS. (`getStrategyStats` 호출이 불필요해지면 제거해 미사용 경고 방지.)
- [ ] **Step 4: Commit** — `git commit -m "refactor(web): 내 전략 상세 라이브 P&L 제거·백테스트 우선(재스코프)"`

> **후속(선택·별 task):** 버전 diff 뷰·paper→live 연동 준비도 배지·capability 차단 사유 선표시. 핵심 재스코프(라이브 제거)와 분리해 필요 시 추가.

---

## Task S3: 전략 카드 스타일

**Files:** Modify `web/src/index.css`

- [ ] **Step 1: 클래스 추가** — 카드(`.strat-card`)·상태 배지·백테스트 KPI 그리드(`.strat-bt`)·빈 상태·푸터 링크. 기존 `.mode-badge`·`.dot-green`·`.pos/.neg`·`.panel` 재사용, 새 색 도입 금지.
- [ ] **Step 2: Commit** — `git commit -m "style(web): 내 전략 백테스트 카드 스타일(토큰 재사용)"`

---

## Task S4: 검증

- [ ] **Step 1: 빌드** — `cd web && npx tsc -b && npx eslint . && npx vite build` 에러 0.
- [ ] **Step 2: dev 프리뷰** — SPA 부팅·콘솔 에러 0. (⚠ `/strategies` 실카드는 인증+전략 데이터 필요 → dev에선 로그인까지. 백테스트 메트릭 시각검증은 실계정/로컬 시각환경.)
- [ ] **Step 3: 정직 한계 기록** — 실데이터 시각검증 미가능 명시.

---

## 자기검토 (스펙 §4 대조)

- **§4.1 목록 백테스트 전용** → S1(카드·병렬 fetch·라이브 제거). ✅
- **§4.2 상세** → S2(라이브 P&L 제거·백테스트 우선). 버전 diff·준비도는 후속 명시(YAGNI). ✅
- **역할 분리(§2)** → 라이브 손익 전부 제거(트레이딩 전용). ✅
- **§6 토큰 / §8 비목표(서버 무변경)** → S3 토큰 재사용·N+1은 기존 엔드포인트. ✅
- **Placeholder:** 데이터 키·스케일(분수 ×100)·fetch 코드·클래스 명시. 목업이 시각 SSOT.

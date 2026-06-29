# P6-3 — 웹 invest·수수료·레버리지 표시 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. 상위 spec §3.4·§3.5.
> ⚠ UI — DESIGN.md 토큰. **서버 변경 0** — invest/est_fee는 이미 snapshot JSON에 있음(P6-1·P6-2). 웹은 타입+렌더만.

**Goal:** P6-1(체결 invest: 주식 투입금액 / 선물 명목·증거금·레버리지)·P6-2(preview est_fee·선물 레버리지)를 웹에 표시.

**Architecture:** invest는 **decision `extra.invest`**(P6-1)에, 수수료·레버리지는 **preview candidate**(P6-2)에 이미 실려 snapshot으로 옴. JSON은 미선언 필드를 버리지 않으므로(런타임 보존) **타입 선언 추가 + 렌더만** 하면 된다(서버 재전송 불필요). 표시 지점: `TradeOutcomes.tsx`(decisions)·`MonitorCards.tsx`(preview candidates). 포맷=`format.ts`의 `wonReadable`/`fmt2`, 스타일=`.muted .small`. (Monitor orders는 invest 없음 — P6-1이 log_order 미변경 — 제외.)

**Tech Stack:** React+TS. `web/src/{types.ts, format.ts, components/TradeOutcomes.tsx, components/MonitorCards.tsx}`.

**불변식:** 표시 추가만 — 데이터 없으면(실거래 전/미바인딩) 조용히 미표시. DESIGN 토큰·인라인 hex 0.

---

## Task 1: 타입 + 표시 (tsc/build)

**Files:**
- Modify: `web/src/types.ts` (CycleRow.decisions ~607, PreviewBuyCandidate ~910)
- Modify: `web/src/components/TradeOutcomes.tsx` (decision row ~244-259)
- Modify: `web/src/components/MonitorCards.tsx` (candidate row ~346-376)

- [ ] **Step 1: 타입 확장**

`types.ts` CycleRow.decisions 요소에 추가:
```typescript
    extra?: { intended?: number; fill?: number;
              invest?: { amount?: number; notional?: number; margin?: number;
                         leverage?: number; currency?: string } };
```
`PreviewBuyCandidate`에 추가:
```typescript
  est_fee_krw?: number | null;
  leverage?: number | null;
  multiplier?: number | null;
  margin_rate?: number | null;
```

- [ ] **Step 2: TradeOutcomes — invest 표시**

decision row(detail 셀 ~258)에 invest 보조 라인 추가(있을 때만):
```tsx
{d.extra?.invest && (
  <div className="muted small" style={{ marginTop: 2 }}>
    {d.extra.invest.amount != null
      ? `투입 ${wonReadable(d.extra.invest.amount)}`
      : d.extra.invest.notional != null
        ? `명목 ${wonReadable(d.extra.invest.notional)}`
          + (d.extra.invest.margin != null ? ` · 증거금 ${wonReadable(d.extra.invest.margin)}` : "")
          + (d.extra.invest.leverage != null ? ` · 레버리지 ${d.extra.invest.leverage}x` : "")
        : ""}
  </div>
)}
```
`wonReadable` import(`../format`). (USD면 wonReadable 대신 통화 표기 — currency 'USD'면 `$` + toLocaleString; 간단히 KRW 우선·USD는 숫자+$.)

- [ ] **Step 3: MonitorCards — 예상수수료·레버리지 표시**

candidate row(~346-376)에 보조 라인: 주식이면 `est_fee_krw`("예상수수료 {wonReadable}"), 선물이면 `leverage`/`margin_rate`("레버리지 {leverage}x · 증거금률 {margin_rate*100}%"). 있을 때만, `.muted small`.

- [ ] **Step 4: tsc + build + 커밋**

Run: `cd web && node node_modules/typescript/lib/tsc.js --noEmit` → 0; `node node_modules/vite/bin/vite.js build` → clean.
```bash
git add web/src/types.ts web/src/components/TradeOutcomes.tsx web/src/components/MonitorCards.tsx
git commit -m "feat(web): 체결 투입금액·레버리지·예상수수료 표시 (P6-3)"
```

- [ ] **Step 5: 브라우저 렌더(가능 범위)**

dev에서 /monitor·결과뷰 렌더(콘솔 0). **한계:** invest는 실거래 decision, est_fee는 실사이클 candidate가 있어야 채워짐 — dev엔 데이터 없어 *미표시 렌더*만 검증(populated는 라이브). DESIGN 토큰 점검.

---

## Self-Review
- **Spec 커버리지:** P6-1 invest·P6-2 수수료/레버리지 웹 노출 → Task 1. ✓
- **서버 무변경 확인:** 데이터는 이미 snapshot JSON — 타입+렌더만(구현자에 명시). 오해(필드 드롭) 방지.
- **타입 일관성:** extra.invest{amount|notional/margin/leverage}·PreviewBuyCandidate{est_fee_krw,leverage,multiplier,margin_rate}. 렌더가 동일 키 읽음.
- **무영향:** 옵셔널·있을 때만 표시. 기존 행 렌더 무변경. DESIGN 토큰.
- **검증 한계:** populated 표시는 실데이터(라이브) 필요 — 미표시 렌더+tsc+build만 dev 검증(정직).

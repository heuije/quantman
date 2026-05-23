/* Monitor 페이지의 위험·성과·시장 카드 모음.
 * Phase 13.1~13.9의 visualization 컴포넌트들. */

import { useEffect, useState } from "react";
import {
  Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip as PieTooltip,
} from "recharts";
import type {
  DrawdownState, KillSwitchState, LocalHealth, MarketContext, PortfolioRisk,
  PositionRich, ReconciliationResult, RejectionReason, SlippageBucket,
  StrategyPnlSummary,
} from "../types";
import { fmt2, wonReadable } from "../format";

// 파이 슬라이스 컬러 — DESIGN.md 따뜻한 톤. accent → 변주.
const PIE_COLORS = [
  "#d97757", "#7a6a55", "#b3a692", "#ad5019", "#6f6a62",
  "#e8a87c", "#c38a5a", "#8b6f4e", "#a89077", "#d4b896",
];

// ── 1. 위험 한도 banner — 임계 미달 평시엔 표시하지 않는다 ───────────────────

/** 일일 손실 한도 80% 이상 사용 또는 drawdown depth ≤ -10% 일 때만 banner 노출.
 *  평시에 게이지 패널을 띄워두면 시각 노이즈만 되고 사용자 액션이 없다.
 *  kill switch active는 별도 banner(Monitor.tsx)가 담당. */
export function RiskBanner({ ks, dd, equityNow }: {
  ks?: KillSwitchState;
  dd?: DrawdownState;
  equityNow?: number;
}) {
  const dayStart = ks?.day_start_equity ?? null;
  const cur = equityNow ?? null;
  const limitPct = 3;          // DEFAULT_EXECUTION.daily_loss_limit_pct
  let dayChange = 0;
  if (dayStart && cur && dayStart > 0) dayChange = (cur - dayStart) / dayStart * 100;
  const usagePct = Math.min(100, Math.max(0, -dayChange / limitPct * 100));
  const depth = dd?.depth_pct ?? 0;

  const dayWarn = usagePct >= 80;
  const ddWarn = depth <= -10;
  if (!dayWarn && !ddWarn) return null;

  return (
    <div className="panel" style={{
      borderLeft: "4px solid var(--amber)",
      background: "var(--amber-soft)", marginBottom: 14,
    }}>
      <div style={{ fontWeight: 700, color: "var(--amber)", marginBottom: 4 }}>
        ⚠ 위험 한도 근접
      </div>
      <div className="muted small" style={{ lineHeight: 1.6 }}>
        {dayWarn && (
          <div>
            오늘 손실 {fmt2(dayChange)}% (한도 -{limitPct}% 대비 사용{" "}
            <b>{fmt2(usagePct)}%</b>) — 100% 도달 시 신규 진입 차단
          </div>
        )}
        {ddWarn && (
          <div>
            현재 drawdown <b>{fmt2(depth)}%</b>
            {dd?.days_since_high ? ` · 고점 후 ${dd.days_since_high}일` : ""}
            {dd?.high_date ? ` (${dd.high_date})` : ""} — 자본 고점 대비 손실 누적
          </div>
        )}
      </div>
    </div>
  );
}

// ── 2. 포지션 디테일 카드 (청산까지 거리) ────────────────────────────────────

export function PositionDetailCards({
  positions, reconciliation, onReconcile, reconcileDisabled, reconcileTooltip,
}: {
  positions: PositionRich[];
  reconciliation?: ReconciliationResult;
  onReconcile?: () => void;
  reconcileDisabled?: boolean;
  reconcileTooltip?: string;
}) {
  const checkedAt = reconciliation?.checked_at
    ? new Date(reconciliation.checked_at).toLocaleString("ko-KR", { hour12: false })
    : null;

  if (!positions || positions.length === 0) {
    return (
      <div className="panel">
        <PositionHeader count={0} onReconcile={onReconcile}
                        reconcileDisabled={reconcileDisabled}
                        reconcileTooltip={reconcileTooltip} />
        <p className="muted">현재 보유 종목이 없습니다.</p>
      </div>
    );
  }

  // reconciliation 데이터: symbol → ledger qty 맵.
  // - ledger_orphans: ledger에 있고 KIS 부족 (매도 누락 추정)
  // - external_extras: KIS에 있고 ledger 부족 (외부 매수 등)
  // - in_sync: 양쪽 일치 — 별도 ledger 수치 불필요(=KIS qty와 동일).
  const ledgerByKis: Record<string, number> = {};
  for (const o of reconciliation?.ledger_orphans ?? []) {
    ledgerByKis[o.symbol] = o.ledger_total_qty;
  }
  for (const e of reconciliation?.external_extras ?? []) {
    ledgerByKis[e.symbol] = e.ledger_total_qty;
  }

  // 파이차트: 각 보유 종목의 평가금액 비중. eval_price·qty 없는 행은 제외.
  const pieData = positions
    .filter((p) => p.qty > 0 && p.eval_price)
    .map((p) => ({
      name: p.name ?? p.symbol,
      value: Math.round(p.qty * (p.eval_price ?? 0)),
    }))
    .sort((a, b) => b.value - a.value);

  return (
    <div className="panel">
      <PositionHeader count={positions.length} onReconcile={onReconcile}
                      reconcileDisabled={reconcileDisabled}
                      reconcileTooltip={reconcileTooltip}
                      checkedAt={checkedAt} />

      <div className="position-layout">
        {pieData.length > 0 && (
          <div className="position-pie">
            <ResponsiveContainer width="100%" height={220}>
              <PieChart>
                <Pie data={pieData} dataKey="value" nameKey="name" innerRadius={45}
                     outerRadius={85} paddingAngle={1}>
                  {pieData.map((_, i) => (
                    <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />
                  ))}
                </Pie>
                <PieTooltip formatter={(v) => wonReadable(Number(v))} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
              </PieChart>
            </ResponsiveContainer>
          </div>
        )}

        <div className="position-table-wrap">
          <table className="position-table">
            <thead>
              <tr>
                <th>종목</th><th>전략</th>
                <th style={{ textAlign: "right" }} title="KIS 실 잔고 수량">KIS</th>
                <th style={{ textAlign: "right" }} title="자동매매 ledger 수량">ledger</th>
                <th style={{ textAlign: "right" }} title="KIS − ledger (0이면 정상)">차이</th>
                <th style={{ textAlign: "right" }}>평단가</th>
                <th style={{ textAlign: "right" }}>현재가</th>
                <th style={{ textAlign: "right" }}>수익률</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => {
                const ret = p.cur_return_pct ?? 0;
                const ledger = ledgerByKis[p.symbol];
                const drift = ledger !== undefined ? ledger - p.qty : 0;
                const driftClass = drift !== 0 ? "drift" : "";
                return (
                  <tr key={p.symbol} className={driftClass}>
                    <td>
                      <strong>{p.name ?? p.symbol}</strong>
                      <div className="muted" style={{ fontSize: 11 }}>
                        {p.symbol} · 보유 {p.held_days ?? 0}일
                      </div>
                    </td>
                    <td className="muted">{p.strategy_name || "—"}</td>
                    <td style={{ textAlign: "right" }}>{p.qty.toLocaleString()}</td>
                    <td style={{ textAlign: "right" }}>
                      {ledger !== undefined ? ledger.toLocaleString() : p.qty.toLocaleString()}
                    </td>
                    <td style={{ textAlign: "right",
                                  color: drift === 0 ? "var(--muted)" : "var(--amber)" }}>
                      {drift === 0 ? "—" : (drift > 0 ? `+${drift}` : `${drift}`)}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {p.entry_price ? wonReadable(p.entry_price) : "—"}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      {p.eval_price ? wonReadable(p.eval_price) : "—"}
                    </td>
                    <td className={ret >= 0 ? "pos" : "neg"} style={{ textAlign: "right" }}>
                      {ret >= 0 ? "+" : ""}{fmt2(ret)}%
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function PositionHeader({ count, onReconcile, reconcileDisabled, reconcileTooltip, checkedAt }: {
  count: number;
  onReconcile?: () => void;
  reconcileDisabled?: boolean;
  reconcileTooltip?: string;
  checkedAt?: string | null;
}) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                   gap: 12, marginBottom: 12 }}>
      <h3 style={{ margin: 0 }}>보유 종목 ({count})</h3>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        {checkedAt && (
          <span className="muted small" title={`KIS 잔고 ↔ ledger 마지막 점검 시각`}>
            잔고 점검: {checkedAt}
          </span>
        )}
        {onReconcile && (
          <button className="ghost sm" onClick={onReconcile}
                  disabled={reconcileDisabled} title={reconcileTooltip}>
            지금 점검
          </button>
        )}
      </div>
    </div>
  );
}

// ── 3. 전략별 P&L ─────────────────────────────────────────────────────────────

export function StrategyPnl({ data }: { data?: StrategyPnlSummary }) {
  if (!data || data.by_strategy.length === 0) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>전략별 P&L</h3>
        <p className="muted">아직 청산된 거래가 없어 집계가 불가능합니다.</p>
      </div>
    );
  }
  const t = data.total;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>전략별 P&L</h3>
      <div className="cards" style={{ marginBottom: 12 }}>
        <PnlStat label="오늘" v={t.today} />
        <PnlStat label="7일" v={t.week} />
        <PnlStat label="30일" v={t.month} />
        <PnlStat label="누적" v={t.all} />
      </div>
      <table>
        <thead>
          <tr>
            <th>전략</th><th>거래</th><th>승률</th>
            <th>오늘</th><th>7일</th><th>30일</th><th>누적</th>
          </tr>
        </thead>
        <tbody>
          {data.by_strategy.map((r) => (
            <tr key={r.strategy}>
              <td>{r.strategy}</td>
              <td>{r.trades}</td>
              <td>{fmt2(r.win_rate)}%</td>
              <td className={r.today_pnl >= 0 ? "pos" : "neg"}>
                {wonReadable(r.today_pnl)}
              </td>
              <td className={r.week_pnl >= 0 ? "pos" : "neg"}>
                {wonReadable(r.week_pnl)}
              </td>
              <td className={r.month_pnl >= 0 ? "pos" : "neg"}>
                {wonReadable(r.month_pnl)}
              </td>
              <td className={r.pnl >= 0 ? "pos" : "neg"}>
                {wonReadable(r.pnl)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PnlStat({ label, v }: { label: string; v: number }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className={"value " + (v >= 0 ? "pos" : "neg")}>{wonReadable(v)}</div>
    </div>
  );
}

// ── 4. 시간대별 슬리피지 + 거부 사유 ──────────────────────────────────────────

export function ExecutionQuality({ buckets, reasons }: {
  buckets?: SlippageBucket[]; reasons?: RejectionReason[];
}) {
  const hasBuckets = buckets && buckets.length > 0;
  const hasReasons = reasons && reasons.length > 0;
  if (!hasBuckets && !hasReasons) return null;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>실행 품질</h3>
      <div className="exec-grid">
        {hasBuckets && (
          <div>
            <div className="sub-h">시간대별 슬리피지 (bps)</div>
            <table>
              <thead><tr><th>구간</th><th>표본</th><th>평균</th><th>최대</th></tr></thead>
              <tbody>
                {buckets!.map((b) => (
                  <tr key={b.bucket}>
                    <td>{b.bucket}</td>
                    <td>{b.n}</td>
                    <td className={b.avg_bps > 30 ? "neg" : ""}>{fmt2(b.avg_bps)}</td>
                    <td>{fmt2(b.max_bps)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {hasReasons && (
          <div>
            <div className="sub-h">거부/취소/타임아웃 사유 (최근)</div>
            <table>
              <thead><tr><th>사유</th><th>회수</th></tr></thead>
              <tbody>
                {reasons!.map((r) => (
                  <tr key={r.label}>
                    <td>{r.label}</td>
                    <td>{r.n}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}

// ── 5. 로컬앱 헬스 ────────────────────────────────────────────────────────────

export function HealthCard({ snapAt, health }: {
  snapAt?: string; health?: LocalHealth;
}) {
  // Date.now()를 render 중 직접 호출하면 React purity 위반 + 매 render 다른 값.
  // state로 보관하고 30초마다 refresh.
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);
  const items: { label: string; value: string; tone: "" | "warn" | "neg" }[] = [];
  if (snapAt) {
    const ageMin = (now - new Date(snapAt).getTime()) / 60000;
    items.push({
      label: "마지막 동기화",
      value: ageMin < 1 ? "방금 전"
        : ageMin < 60 ? `${Math.floor(ageMin)}분 전`
        : `${Math.floor(ageMin / 60)}시간 전`,
      tone: ageMin > 30 ? "warn" : "",
    });
  }
  if (health?.last_cycle_ts) {
    const ts = new Date(health.last_cycle_ts);
    items.push({
      label: "마지막 사이클",
      value: ts.toLocaleString(),
      tone: "",
    });
  }
  if (health?.kis_token_expires_at) {
    const exp = new Date(health.kis_token_expires_at);
    const hoursLeft = (exp.getTime() - now) / 3600000;
    items.push({
      label: "KIS 토큰 만료",
      value: hoursLeft < 0 ? "만료됨"
        : hoursLeft < 24 ? `${fmt2(hoursLeft)}시간 남음`
        : `${Math.floor(hoursLeft / 24)}일 남음`,
      tone: hoursLeft < 0 ? "neg" : hoursLeft < 2 ? "warn" : "",
    });
  }
  if (health?.kis_master_pushed_date) {
    items.push({
      label: "KIS 마스터 sync",
      value: health.kis_master_pushed_date,
      tone: "",
    });
  }
  if (items.length === 0) return null;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>로컬앱 헬스</h3>
      <div className="health-grid">
        {items.map((it, i) => (
          <div key={i} className={"health-cell " + it.tone}>
            <div className="muted" style={{ fontSize: 11 }}>{it.label}</div>
            <div style={{ fontWeight: 600 }}>{it.value}</div>
          </div>
        ))}
      </div>
      {health?.warnings && health.warnings.length > 0 && (
        <ul style={{ marginTop: 10, fontSize: 13, color: "var(--amber)" }}>
          {health.warnings.map((w, i) => <li key={i}>⚠ {w}</li>)}
        </ul>
      )}
    </div>
  );
}

// ── 6. 시장 컨텍스트 ──────────────────────────────────────────────────────────

export function MarketBar({ ctx }: { ctx: MarketContext | null }) {
  if (!ctx) return null;
  return (
    <div className="market-bar">
      <span className="badge" style={{ background: "var(--accent-soft)" }}>
        {ctx.session.phase}
      </span>
      {ctx.indicators.filter((i) => i.available).map((i) => (
        <span key={i.label} className="mkt-chip">
          <span className="muted">{i.label}</span>{" "}
          <strong>{i.value != null ? fmt2(i.value) : "—"}</strong>
          {" "}
          <span className={(i.change_pct ?? 0) >= 0 ? "pos" : "neg"}>
            {(i.change_pct ?? 0) >= 0 ? "+" : ""}{fmt2(i.change_pct)}%
          </span>
        </span>
      ))}
    </div>
  );
}

// ── 7. 상관관계 + 섹터 노출 ───────────────────────────────────────────────────

export function PortfolioRiskCard({ risk, err }: {
  risk: PortfolioRisk | null;
  err?: string;
}) {
  // W-02 — 위험 카드 로드 실패를 silent하게 빈 상태로 두지 않는다.
  // 직전 성공값이 있으면 그대로 표시(톤다운), 없을 때만 에러 카드 표시.
  if (!risk && err) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>포트폴리오 위험</h3>
        <p className="muted">지표를 불러오지 못했습니다 — {err}. 30초 후 자동 재시도.</p>
      </div>
    );
  }
  if (!risk || risk.positions.length === 0) return null;
  const N = risk.positions.length;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>포트폴리오 위험 (최근 {risk.window}일)</h3>
      <div className="exec-grid">
        <div>
          <div className="sub-h">종목 간 일별 수익률 상관계수</div>
          <table className="corr-table">
            <thead>
              <tr>
                <th></th>
                {risk.positions.map((s) => <th key={s}>{s}</th>)}
              </tr>
            </thead>
            <tbody>
              {risk.matrix.map((row, i) => (
                <tr key={i}>
                  <td><strong>{risk.positions[i]}</strong></td>
                  {row.map((v, j) => (
                    <td key={j} className="corr-cell"
                        style={{ background: corrColor(v) }}>
                      {i === j ? "—" : fmt2(v)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {N === 1 && (
            <p className="muted" style={{ fontSize: 12 }}>
              보유 종목이 1개라 분산 효과 분석에는 더 많은 종목이 필요합니다.
            </p>
          )}
        </div>
        <div>
          <div className="sub-h">섹터(카테고리) 노출</div>
          <div className="sector-bars">
            {risk.sectors.map((s) => (
              <div key={s.label} className="sector-row">
                <div className="sector-label">{s.label}</div>
                <div className="sector-bar-wrap">
                  <div className="sector-bar" style={{ width: `${s.share_pct}%` }} />
                </div>
                <div className="sector-pct">{fmt2(s.share_pct)}%</div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function corrColor(v: number): string {
  // -1: 파랑, 0: 회색, +1: 빨강
  const t = Math.max(-1, Math.min(1, v));
  if (t >= 0) {
    const a = Math.round(t * 60);
    return `hsla(0, 70%, 55%, 0.${String(a).padStart(2, "0")})`;
  } else {
    const a = Math.round(-t * 60);
    return `hsla(210, 70%, 55%, 0.${String(a).padStart(2, "0")})`;
  }
}

/* Monitor 페이지의 위험·성과·시장 카드 모음.
 * Phase 13.1~13.9의 visualization 컴포넌트들. */

import { useEffect, useState } from "react";
import type {
  DrawdownState, KillSwitchState, LocalHealth, MarketContext, PortfolioRisk,
  PositionRich, RejectionReason, SlippageBucket, StrategyPnlSummary,
} from "../types";
import { fmt2, wonReadable } from "../format";

// ── 1. 일일 손실 한도 게이지 + drawdown ───────────────────────────────────────

export function RiskGauges({ ks, dd, equityNow }: {
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

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>위험 한도</h3>
      <div className="risk-grid">
        <div>
          <div className="risk-label">오늘 손익</div>
          <div className={"risk-value " + (dayChange < 0 ? "neg" : "pos")}>
            {dayChange >= 0 ? "+" : ""}{fmt2(dayChange)}%
          </div>
          <div className="muted" style={{ fontSize: 12 }}>
            시작: {dayStart ? wonReadable(dayStart) : "—"} · 현재:{" "}
            {cur ? wonReadable(cur) : "—"}
          </div>
        </div>
        <div>
          <div className="risk-label">일일 손실 한도</div>
          <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
            오늘 한도 {limitPct}% · 현재 사용 {fmt2(usagePct)}%
          </div>
          <div className="gauge">
            <div className={"gauge-fill " + (usagePct >= 80 ? "danger"
                  : usagePct >= 50 ? "warn" : "")}
                  style={{ width: `${usagePct}%` }} />
            <span className="gauge-label">{fmt2(usagePct)}%</span>
          </div>
        </div>
        <div>
          <div className="risk-label">현재 Drawdown</div>
          <div className={"risk-value " + ((dd?.depth_pct ?? 0) < 0 ? "neg" : "")}>
            {dd ? fmt2(dd.depth_pct) : "—"}%
          </div>
          <div className="muted" style={{ fontSize: 12 }}>
            {dd?.days_since_high ? `고점 후 ${dd.days_since_high}일` : "신고가 근접"}
            {dd?.high_date ? ` (${dd.high_date})` : ""}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── 2. 포지션 디테일 카드 (청산까지 거리) ────────────────────────────────────

export function PositionDetailCards({ positions }: { positions: PositionRich[] }) {
  if (!positions || positions.length === 0) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>보유 종목</h3>
        <p className="muted">현재 보유 종목이 없습니다.</p>
      </div>
    );
  }
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>보유 종목 ({positions.length})</h3>
      <div className="pos-grid">
        {positions.map((p) => {
          const ret = p.cur_return_pct ?? 0;
          const dist = p.distances ?? {};
          return (
            <div key={p.symbol} className="pos-card">
              <div className="pos-head">
                <strong>{p.name ?? p.symbol}</strong>
                <span className={"pos-ret " + (ret >= 0 ? "pos" : "neg")}>
                  {ret >= 0 ? "+" : ""}{fmt2(ret)}%
                </span>
              </div>
              <div className="muted" style={{ fontSize: 12 }}>
                {p.symbol} · {p.strategy_name || "(전략 미상)"} · 보유 {p.held_days ?? 0}일
              </div>
              <div className="pos-row">
                <div className="muted">평균가</div>
                <div>{p.entry_price ? wonReadable(p.entry_price) : "—"}</div>
                <div className="muted">현재가</div>
                <div>{p.eval_price ? wonReadable(p.eval_price) : "—"}</div>
              </div>
              <div className="pos-dists">
                {dist.tp_gap_pct !== undefined && (
                  <DistChip label="익절까지" v={dist.tp_gap_pct} suffix="%p" tone="pos" />
                )}
                {dist.sl_gap_pct !== undefined && (
                  <DistChip label="손절까지" v={dist.sl_gap_pct} suffix="%p" tone="neg" />
                )}
                {dist.trail_gap_pct !== undefined && (
                  <DistChip label="트레일" v={dist.trail_gap_pct} suffix="%p" tone="warn" />
                )}
                {dist.hold_days_left !== undefined && (
                  <DistChip label="보유일" v={dist.hold_days_left} suffix="일 남음" tone="" />
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function DistChip({ label, v, suffix, tone }: {
  label: string; v: number; suffix: string; tone: string;
}) {
  // 0에 가까울수록 빨강 (트리거 임박)
  const close = Math.abs(v) <= 1.5;
  const cls = close ? "danger" : (tone === "pos" ? "pos" : tone === "neg" ? "neg" : "");
  return (
    <span className={"dist-chip " + cls}>
      <span className="muted">{label}</span>{" "}
      <strong>{v >= 0 ? "+" : ""}{fmt2(v)}{suffix}</strong>
    </span>
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

export function PortfolioRiskCard({ risk }: { risk: PortfolioRisk | null }) {
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

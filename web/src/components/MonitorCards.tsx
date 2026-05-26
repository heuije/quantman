/* Monitor 페이지의 위험·성과·시장 카드 모음.
 * Phase 13.1~13.9의 visualization 컴포넌트들. */

import { useEffect, useState } from "react";
import {
  Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip as PieTooltip,
} from "recharts";
import type {
  DrawdownState, KillSwitchState, LocalHealth, MarketContext, NextDayPreview,
  PortfolioRisk, PositionRich, ReconciliationResult, RejectionReason,
  SlippageBucket, StrategyPnlSummary,
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
                        {(p.phases_total ?? 0) > 1 && (
                          <>
                            {" · "}
                            <span title="분할매수 진행 — 진입한 차수 / 전체 차수">
                              {(p.phases_executed?.length ?? 1)}/{p.phases_total}차
                            </span>
                          </>
                        )}
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

// ── 3. 전략별 카드 그리드 — P&L + 매매 예정 + 신호 근거 + 보유 통합 ────────

/** 전략 키별로 join: pnl(by_strategy) + nextDayPreview(by_strategy)
 *  + positions(strategy_name으로 그룹) → 카드 1개로 표시. */
export function StrategyCardGrid({
  pnl, preview, positions,
}: {
  pnl?: StrategyPnlSummary;
  preview?: NextDayPreview | null;
  positions: PositionRich[];
}) {
  // 전략 이름 합집합 — pnl, preview, positions 어디에든 등장하는 전략을 모두 표시.
  const names = new Set<string>();
  for (const r of pnl?.by_strategy ?? []) names.add(r.strategy);
  for (const bs of preview?.by_strategy ?? []) names.add(bs.strategy_name);
  for (const p of positions) if (p.strategy_name) names.add(p.strategy_name);

  if (names.size === 0) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>전략별 현황</h3>
        <p className="muted">활성 전략이 없습니다. [내 전략] 탭에서 전략을 모의로 두면 자동 사이클이 시작됩니다.</p>
      </div>
    );
  }

  const total = pnl?.total;
  return (
    <div className="panel">
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between",
                     gap: 12, marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>전략별 현황 ({names.size})</h3>
        {total && (
          <span className="muted small">
            전 전략 합계 · 오늘 <b className={total.today >= 0 ? "pos" : "neg"}>{wonReadable(total.today)}</b>
            {" · "}30일 <b className={total.month >= 0 ? "pos" : "neg"}>{wonReadable(total.month)}</b>
            {" · "}누적 <b className={total.all >= 0 ? "pos" : "neg"}>{wonReadable(total.all)}</b>
          </span>
        )}
      </div>
      <div className="strategy-grid">
        {[...names].map((name) => (
          <StrategyCard
            key={name} name={name}
            pnlRow={pnl?.by_strategy.find((r) => r.strategy === name)}
            previewRow={preview?.by_strategy?.find((bs) => bs.strategy_name === name)}
            heldPositions={positions.filter((p) => p.strategy_name === name)}
          />
        ))}
      </div>
    </div>
  );
}

function StrategyCard({ name, pnlRow, previewRow, heldPositions }: {
  name: string;
  pnlRow?: StrategyPnlSummary["by_strategy"][number];
  previewRow?: NextDayPreview["by_strategy"] extends (infer T)[] | undefined ? T : never;
  heldPositions: PositionRich[];
}) {
  return (
    <div className="strategy-card">
      <div className="strategy-card-head">
        <strong>{name}</strong>
        {previewRow && (
          <span className={"sc-badge " + previewRow.run_mode}>
            {previewRow.run_mode === "live" ? "실전" : "모의"}
          </span>
        )}
      </div>

      {/* P&L 한 줄 */}
      {pnlRow ? (
        <div className="strategy-pnl-row">
          <PnlMini label="오늘" v={pnlRow.today_pnl} />
          <PnlMini label="7일" v={pnlRow.week_pnl} />
          <PnlMini label="30일" v={pnlRow.month_pnl} />
          <PnlMini label="누적" v={pnlRow.pnl} />
          <span className="muted small" style={{ marginLeft: "auto" }}>
            거래 {pnlRow.trades} · 승률 {fmt2(pnlRow.win_rate)}%
          </span>
        </div>
      ) : (
        <div className="muted small">아직 청산된 거래 없음 (집계 대기)</div>
      )}

      {/* 매매 예정 — 신호 근거 + 금액 산정 근거 */}
      {previewRow && (
        <div className="strategy-section">
          <div className="strategy-section-title">
            매매 예정{" "}
            {previewRow.signal_passed
              ? <span className="pos small">신호 통과 ✓</span>
              : <span className="muted small">신호 미충족</span>}
          </div>
          {previewRow.signal_summary && (
            <div className="muted small" style={{ marginBottom: 4 }}>
              공통 조건: <code>{previewRow.signal_summary}</code>
            </div>
          )}
          {previewRow.candidates.length > 0 ? (
            <table className="strategy-mini-table">
              <thead>
                <tr>
                  <th>종목</th>
                  <th style={{ textAlign: "right" }}>수량</th>
                  <th style={{ textAlign: "right" }}>발주가</th>
                  <th style={{ textAlign: "right" }}>총액</th>
                </tr>
              </thead>
              <tbody>
                {previewRow.candidates.map((c) => (
                  <tr key={c.symbol}>
                    <td>{c.name || c.symbol}</td>
                    <td style={{ textAlign: "right" }}>{c.qty.toLocaleString()}</td>
                    <td style={{ textAlign: "right" }}>
                      {c.est_limit_price.toLocaleString()}원
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <strong>{c.est_total.toLocaleString()}원</strong>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="muted small">매수 후보 없음</div>
          )}
          {previewRow.per_symbol_details
            && Object.keys(previewRow.per_symbol_details).length > 0
            && Object.keys(previewRow.per_symbol_details).length <= 30 && (
            <details className="strategy-detail">
              <summary className="muted small">
                종목별 조건 평가 ({Object.keys(previewRow.per_symbol_details).length}종목)
              </summary>
              <div style={{ marginTop: 4 }}>
                {Object.entries(previewRow.per_symbol_details).map(([sym, ev]) => (
                  <div key={sym} className="small">
                    <span className={ev.passed ? "pos" : "muted"}>{ev.passed ? "✓" : "✗"}</span>{" "}
                    <strong>{sym}</strong>{" "}
                    <span className="muted">{ev.summary}</span>
                  </div>
                ))}
              </div>
            </details>
          )}
          {previewRow.skipped.length > 0 && (
            <div className="muted small" style={{ marginTop: 4 }}>
              {previewRow.skipped.map((sk, i) => (
                <div key={i}>⊘ {sk.symbol ? `${sk.symbol}: ` : ""}{sk.reason}</div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 현재 보유 — 이 전략으로 매수한 포지션 */}
      {heldPositions.length > 0 && (
        <div className="strategy-section">
          <div className="strategy-section-title">보유 ({heldPositions.length})</div>
          <table className="strategy-mini-table">
            <thead>
              <tr>
                <th>종목</th>
                <th style={{ textAlign: "right" }}>수량</th>
                <th style={{ textAlign: "right" }}>수익률</th>
              </tr>
            </thead>
            <tbody>
              {heldPositions.map((p) => {
                const ret = p.cur_return_pct ?? 0;
                return (
                  <tr key={p.symbol}>
                    <td>{p.name ?? p.symbol}</td>
                    <td style={{ textAlign: "right" }}>{p.qty.toLocaleString()}</td>
                    <td className={ret >= 0 ? "pos" : "neg"} style={{ textAlign: "right" }}>
                      {ret >= 0 ? "+" : ""}{fmt2(ret)}%
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function PnlMini({ label, v }: { label: string; v: number }) {
  return (
    <div className="pnl-mini">
      <div className="muted" style={{ fontSize: 11 }}>{label}</div>
      <div className={v >= 0 ? "pos" : "neg"} style={{ fontWeight: 700, fontSize: 13 }}>
        {wonReadable(v)}
      </div>
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

// ── 5. 로컬앱 상태 — 칩 1줄 + hover tooltip ──────────────────────────────────

/** 동기화 지연·토큰 만료·warnings를 결합해 단일 상태로 환원.
 *  로컬앱이 살아있는지(=자동매매가 실제 도는지) 한눈에 보여주는 게 핵심.
 *  자세한 timestamp는 tooltip(title)으로만 제공 — raw 시각 노출은 의미 약함. */
export function HealthCard({ snapAt, heartbeatAt, health }: {
  snapAt?: string; heartbeatAt?: string | null; health?: LocalHealth;
}) {
  const [now, setNow] = useState<number>(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(t);
  }, []);

  if (!snapAt && !heartbeatAt && !health) return null;

  // Phase 58 — alive 시각은 snapshot received_at과 heartbeat 중 최신 사용.
  // cycle 외 시간(새벽 등)에도 heartbeat가 5분 주기로 갱신 → 살아있음 표시.
  const snapMs = snapAt ? new Date(snapAt).getTime() : 0;
  const hbMs = heartbeatAt ? new Date(heartbeatAt).getTime() : 0;
  const aliveMs = Math.max(snapMs, hbMs);
  const syncAgeSec = aliveMs > 0 ? (now - aliveMs) / 1000 : Infinity;
  // KIS 토큰 만료
  const tokenHoursLeft = health?.kis_token_expires_at
    ? (new Date(health.kis_token_expires_at).getTime() - now) / 3600000 : null;
  const warnings = health?.warnings ?? [];

  // 가장 심각한 상태가 칩 톤 결정.
  let tone: "ok" | "warn" | "error" = "ok";
  const reasons: string[] = [];
  if (syncAgeSec > 300) {       // 5분 이상 끊김
    tone = "error";
    reasons.push("끊김 — 자동매매 중단됨");
  } else if (syncAgeSec > 30) { // 30초~5분 응답 지연
    tone = "warn";
    reasons.push("응답 지연");
  }
  if (tokenHoursLeft != null) {
    if (tokenHoursLeft < 0) {
      tone = "error";
      reasons.push("KIS 토큰 만료 — 재인증 필요");
    } else if (tokenHoursLeft < 2 && tone !== "error") {
      tone = "warn";
      reasons.push(`KIS 토큰 ${fmt2(tokenHoursLeft)}시간 후 만료`);
    }
  }
  if (warnings.length > 0 && tone === "ok") tone = "warn";

  const icon = tone === "ok" ? "✅" : tone === "warn" ? "⚠" : "❌";
  const label = tone === "ok" ? "정상" : reasons[0] ?? "주의";
  const ageStr = !isFinite(syncAgeSec) ? "—"
    : syncAgeSec < 60 ? `${Math.floor(syncAgeSec)}초 전`
    : syncAgeSec < 3600 ? `${Math.floor(syncAgeSec / 60)}분 전`
    : `${Math.floor(syncAgeSec / 3600)}시간 전`;

  // tooltip: 자세한 상태 (raw timestamp + 토큰 + warnings)
  const tipLines: string[] = [];
  if (snapAt) tipLines.push(`마지막 동기화: ${new Date(snapAt).toLocaleString()} (${ageStr})`);
  if (health?.last_cycle_ts) tipLines.push(`마지막 사이클: ${new Date(health.last_cycle_ts).toLocaleString()}`);
  if (tokenHoursLeft != null) {
    tipLines.push(tokenHoursLeft < 0 ? "KIS 토큰: 만료됨"
      : tokenHoursLeft < 24 ? `KIS 토큰: ${fmt2(tokenHoursLeft)}시간 후 만료`
      : `KIS 토큰: ${Math.floor(tokenHoursLeft / 24)}일 후 만료`);
  }
  if (health?.kis_master_pushed_date) tipLines.push(`KIS 마스터 sync: ${health.kis_master_pushed_date}`);
  for (const w of warnings) tipLines.push(`⚠ ${w}`);

  return (
    <span className={"health-chip " + tone} title={tipLines.join("\n")}>
      <span aria-hidden>{icon}</span>
      <span>로컬앱 {label}</span>
      <span className="muted small">· {ageStr}</span>
    </span>
  );
}

// ── 6. 시장 컨텍스트 ──────────────────────────────────────────────────────────

export function MarketBar({ ctx }: { ctx: MarketContext | null }) {
  if (!ctx) return null;
  // as_of 포맷 — "YYYY-MM-DD HH:mm" 등 표시. 빈 값/null이면 "—"
  const fmtAsOf = (s?: string): string => {
    if (!s) return "—";
    // ISO datetime → "MM/DD HH:mm" (간결, 같은 해 가정)
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/);
    if (!m) return s;
    const [, , mo, d, h, mi] = m;
    return h ? `${mo}/${d} ${h}:${mi}` : `${mo}/${d}`;
  };
  return (
    <div className="market-bar">
      <span className="badge" style={{ background: "var(--accent-soft)" }}>
        {ctx.session.phase}
      </span>
      {ctx.indicators.filter((i) => i.available).map((i) => (
        <span key={i.label} className="mkt-chip" title={i.as_of ? `기준: ${i.as_of}` : undefined}>
          <span className="muted">{i.label}</span>{" "}
          <strong>{i.value != null ? fmt2(i.value) : "—"}</strong>
          {" "}
          <span className={(i.change_pct ?? 0) >= 0 ? "pos" : "neg"}>
            {(i.change_pct ?? 0) >= 0 ? "+" : ""}{fmt2(i.change_pct)}%
          </span>
          {i.as_of && (
            <span className="muted small mkt-asof"> · {fmtAsOf(i.as_of)}</span>
          )}
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

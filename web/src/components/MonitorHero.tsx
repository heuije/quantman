/* 트레이딩 성과 히어로 + 상태 바 — 재설계 §3 ①②.
 * 전부 기존 SyncSnapshot.payload 필드만 소비(추가 fetch 없음). 색은 DESIGN 토큰. */
import { useState } from "react";
import { Line, LineChart, ResponsiveContainer } from "recharts";
import type {
  CommandType, CycleSummary, DrawdownState, KillSwitchState,
  LocalHealth, ReconciliationResult, StrategyPnlSummary,
} from "../types";
import { wonReadable } from "../format";

type Balance = { cash: number; total_eval: number };
type EquityPt = { date: string; value: number };

// 방향성 숫자 — 한국식(수익 빨강 .pos / 손실 파랑 .neg). 값 자체는 이미 부호 포함.
const signedPct = (v: number | null) =>
  v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
const signedWon = (v: number | null) =>
  v == null ? "—" : (v >= 0 ? "+" : "") + v.toLocaleString() + "원";
const dirClass = (v: number | null) => (v == null ? "" : v >= 0 ? "pos" : "neg");

const RANGES = [
  { key: "1d", label: "오늘", days: 1 },
  { key: "1w", label: "7일", days: 7 },
  { key: "1m", label: "30일", days: 30 },
  { key: "all", label: "전체", days: 0 },
] as const;
type RangeKey = (typeof RANGES)[number]["key"];

export function PerformanceHero({ balance, equity, strategyPnl, killSwitch }: {
  balance?: Balance;
  equity?: EquityPt[];
  strategyPnl?: StrategyPnlSummary;
  killSwitch?: KillSwitchState;
}) {
  const [range, setRange] = useState<RangeKey>("1m");

  const totalEval = balance?.total_eval ?? null;
  // 오늘 수익률 기준자본 = kill switch가 쓰는 day_start_equity(동일 기준 — 일관).
  const dayStart = killSwitch?.day_start_equity ?? null;
  const todayPct = totalEval != null && dayStart && dayStart > 0
    ? (totalEval - dayStart) / dayStart * 100 : null;
  const todayPnl = strategyPnl?.total.today ?? null;
  const cumPnl = strategyPnl?.total.all ?? null;
  const eq = equity ?? [];
  // 누적 수익률 = 자본곡선 시작 대비(라이브 스냅샷엔 벤치마크 부재 → 코스피 대비 미표시).
  const cumPct = eq.length > 1 && eq[0].value > 0
    ? (eq[eq.length - 1].value / eq[0].value - 1) * 100 : null;

  const curve = (() => {
    if (!eq.length) return [];
    const days = RANGES.find((r) => r.key === range)!.days;
    if (!days) return eq;
    const last = new Date(eq[eq.length - 1].date);
    const cutoff = new Date(last);
    cutoff.setDate(cutoff.getDate() - days);
    return eq.filter((p) => new Date(p.date) >= cutoff);
  })();
  const rangeLabel = RANGES.find((r) => r.key === range)!.label;

  return (
    <section className="panel hero-perf">
      <div className="hero-perf-main">
        <div className="hero-kpis">
          <div className="hero-kpi">
            <div className="hero-kpi-label">누적 수익률</div>
            <div className={"hero-kpi-value " + dirClass(cumPct)}>{signedPct(cumPct)}</div>
            <div className={"hero-kpi-sub " + dirClass(cumPnl)}>{signedWon(cumPnl)}</div>
          </div>
          <div className="hero-kpi">
            <div className="hero-kpi-label">오늘 손익</div>
            <div className={"hero-kpi-value " + dirClass(todayPct)}>{signedPct(todayPct)}</div>
            <div className={"hero-kpi-sub " + dirClass(todayPnl)}>{signedWon(todayPnl)}</div>
          </div>
          <div className="hero-kpi">
            <div className="hero-kpi-label">총 평가금액</div>
            <div className="hero-kpi-value">
              {totalEval != null ? totalEval.toLocaleString() + "원" : "—"}
            </div>
            <div className="hero-kpi-sub muted">
              예수금 {balance ? wonReadable(balance.cash) : "—"}
            </div>
          </div>
        </div>
        {cumPct != null && (
          <div className="hero-interp">
            📈 자본곡선 기준 {rangeLabel} 누적{" "}
            <b className={dirClass(cumPct)}>{signedPct(cumPct)}</b> 유지 중.
          </div>
        )}
      </div>
      <div className="hero-perf-chart">
        <div className="hero-range">
          {RANGES.map((r) => (
            <button key={r.key} type="button"
              className={"seg-btn" + (range === r.key ? " on" : "")}
              onClick={() => setRange(r.key)}>{r.label}</button>
          ))}
        </div>
        {curve.length > 1 ? (
          <ResponsiveContainer width="100%" height={80}>
            <LineChart data={curve} margin={{ top: 4, right: 2, bottom: 0, left: 2 }}>
              <Line type="monotone" dataKey="value" stroke="#d4a738" strokeWidth={2}
                dot={false} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        ) : (
          <div className="hero-chart-empty muted">연동 후 자본곡선이 표시됩니다</div>
        )}
        <div className="hero-chart-cap muted">자본곡선</div>
      </div>
    </section>
  );
}

// ── ② 상태 바 — 상태 칩 + "다음 조치" 경보(문제 시에만 확장) ─────────────────
type Alert = {
  tone: "red" | "amber"; main: string; act?: string;
  cmd?: CommandType; cmdLabel?: string;
};

export function StatusStrip({
  autoStatus, health, receivedAt, lastHeartbeatAt,
  killSwitch, drawdown, reconciliation, cycleSummary, equityNow, onCommand,
}: {
  autoStatus?: "running" | "paused" | "stopped";
  health?: LocalHealth;
  receivedAt?: string | null;
  lastHeartbeatAt?: string | null;
  killSwitch?: KillSwitchState;
  drawdown?: DrawdownState;
  reconciliation?: ReconciliationResult;
  cycleSummary?: CycleSummary;
  equityNow?: number;
  onCommand: (type: CommandType) => void;
}) {
  const now = new Date();

  // 활성 계좌(비민감 핸들 — 계좌번호 아님).
  const handles = health?.account_handles ?? [];
  const activeIds = health?.active_account_ids ?? [];
  const active = handles.filter((h) => activeIds.includes(h.account_id));
  const acct = active[0];

  // 하트비트 신선도 — snapshot·heartbeat 중 최신 대비 경과분.
  const stamps = [receivedAt, lastHeartbeatAt]
    .filter(Boolean).map((s) => new Date(s as string).getTime());
  const latest = stamps.length ? Math.max(...stamps) : null;
  const ageMin = latest != null ? Math.round((now.getTime() - latest) / 60000) : null;
  const hbClass = ageMin == null ? "dot-red"
    : ageMin < 5 ? "dot-green" : ageMin < 30 ? "dot-amber" : "dot-red";
  const hbLabel = ageMin == null ? "연결 정보 없음" : ageMin < 1 ? "방금" : `${ageMin}분 전`;

  // 다음 사이클 — 08:55 / 15:40 중 다음(클라 KST 시계 관례·TradingTimeline과 동일).
  const nc = (() => {
    const times: [number, number][] = [[8, 55], [15, 40]];
    for (const [h, m] of times) {
      const t = new Date(now); t.setHours(h, m, 0, 0);
      if (t > now) return { at: `${h}:${String(m).padStart(2, "0")}`, min: Math.round((t.getTime() - now.getTime()) / 60000) };
    }
    const t = new Date(now); t.setDate(t.getDate() + 1); t.setHours(8, 55, 0, 0);
    return { at: "08:55", min: Math.round((t.getTime() - now.getTime()) / 60000) };
  })();
  const ncLabel = nc.min >= 60 ? `${Math.floor(nc.min / 60)}시간 ${nc.min % 60}분 후` : `${nc.min}분 후`;

  const status = autoStatus ?? "stopped";
  const statusText = status === "running" ? "🟢 자동매매 실행 중"
    : status === "paused" ? "⏸ 일시정지" : "⚪ 정지";

  const alerts: Alert[] = [];
  if (killSwitch?.active) {
    alerts.push({ tone: "red", main: `킬스위치 발동: ${killSwitch.reason || "일일 손실 한도"}`,
      act: "신규 진입 차단 중 · 청산은 계속", cmd: "RESET_KILL_SWITCH", cmdLabel: "해제" });
  }
  const depth = drawdown?.depth_pct;
  if (depth != null && depth <= -10) {
    alerts.push({ tone: "amber",
      main: `고점 대비 ${depth.toFixed(1)}% 하락 (${drawdown!.days_since_high}일 전 고점)`,
      act: "peak 회복 시 자동 해제 · 청산은 계속" });
  }
  // 일일 손실 한도 근접(킬스위치 발동 전 소프트 경고) — 기준자본 = kill switch와 동일 day_start.
  const dsEq = killSwitch?.day_start_equity ?? null;
  if (!killSwitch?.active && dsEq && dsEq > 0 && equityNow != null) {
    const dayChange = (equityNow - dsEq) / dsEq * 100;
    const usage = -dayChange / 3 * 100;   // 일일 손실 한도 -3%
    if (usage >= 80) {
      alerts.push({ tone: "amber",
        main: `오늘 손실 ${dayChange.toFixed(2)}% · 일일 한도 −3% 대비 ${Math.min(100, usage).toFixed(0)}% 사용`,
        act: "100% 도달 시 신규 진입 자동 차단" });
    }
  }
  if (reconciliation?.has_drift) {
    const n = (reconciliation.ledger_orphans?.length ?? 0)
      + (reconciliation.external_extras?.length ?? 0);
    alerts.push({ tone: "amber", main: `${n}종목이 증권사 잔고와 내부 기록 불일치 (수동 매매 추정)`,
      act: "지금 점검하면 자동 정리됩니다", cmd: "RECONCILE_NOW", cmdLabel: "지금 점검" });
  }
  const orphan = cycleSummary?.n_unparseable_orphan ?? 0;
  if (orphan > 0) {
    alerts.push({ tone: "amber", main: `고아 포지션 ${orphan}건 (삭제·구버전 전략 보유분)`,
      act: "자동 청산 불가 — 전략 연구소에서 확인·수동 정리" });
  }
  if (cycleSummary?.us_realtime_unavailable) {
    alerts.push({ tone: "amber", main: "미국 실시간 시세 미신청",
      act: "장중 실시간 손절 미동작 — 설정에서 확인" });
  }

  return (
    <section className="panel status-strip">
      <div className="status-chips">
        <span className={"status-chip" + (status === "running" ? " ok" : "")}>{statusText}</span>
        {acct && (
          <span className="status-chip">
            <i className={"mode-badge " + acct.mode}>{acct.mode === "live" ? "실전" : "모의"}</i>
            {acct.nickname}{active.length > 1 ? ` 외 ${active.length - 1}` : ""}
          </span>
        )}
        <span className="status-chip"><i className={"dot " + hbClass} />연결 · {hbLabel}</span>
        <span className="status-chip">다음 사이클 <b>{nc.at}</b>까지 {ncLabel}</span>
      </div>
      {alerts.map((a) => (
        <div key={a.main} className={"status-alert " + a.tone}>
          <div className="status-alert-main">
            <b>{a.main}</b>{a.act ? <span className="muted"> · {a.act}</span> : null}
          </div>
          {a.cmd && (
            <button type="button" className="ghost sm" onClick={() => onCommand(a.cmd!)}>
              {a.cmdLabel}
            </button>
          )}
        </div>
      ))}
      {alerts.length === 0 && (
        <div className="status-ok muted">
          킬스위치 · 손실한도 · 연결 상태 <b className="ok-green">이상 없음 ✓</b>
        </div>
      )}
    </section>
  );
}

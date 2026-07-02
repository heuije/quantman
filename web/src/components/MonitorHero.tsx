/* 트레이딩 성과 히어로 + 상태 바 — 재설계 §3 ①②.
 * 전부 기존 SyncSnapshot.payload 필드만 소비(추가 fetch 없음). 색은 DESIGN 토큰. */
import { useState } from "react";
import { Line, LineChart, ResponsiveContainer } from "recharts";
import type { KillSwitchState, StrategyPnlSummary } from "../types";
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

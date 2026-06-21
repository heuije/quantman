import { useState } from "react";
import {
  Area, Bar, BarChart, CartesianGrid, Cell, ComposedChart, Line, LineChart,
  Pie, PieChart, ReferenceLine, ResponsiveContainer, Tooltip, Treemap, XAxis, YAxis,
} from "recharts";
import type {
  BreadthResult, IrDistribution, IrEventStat, IrExtremizeResult, IrICStat, IrPartition,
  IrPortfolioDiagnosis, IrRegressionResult, IrSingleReport, IrSweepBucket, NewsDigestResult,
  PrescribeResult,
} from "../types";

/* recharts SVG는 CSS var를 못 받아 토큰값(DESIGN.md)을 직접 인라인한다(EquityChart와 동일 규약).
   변경 시 web/src/index.css :root와 동기화. up=상승/이익(빨강) · down=하락/손실(파랑, 한국 관례). */
const C = {
  accent: "#d4a738", strong: "#e0b958", muted: "#8b94a3", grid: "#2b323e",
  text: "#d2d8e0", up: "#de3033", down: "#1668c4", upSoft: "rgba(222,48,51,0.18)", downSoft: "rgba(22,104,196,0.22)",
  panel: "#1c212b",   // 다크 카드 배경
};

const num = (v: unknown): number => Number(String(v).split("=").pop());
const f2 = (v: number | null | undefined) =>
  v == null || Number.isNaN(v) ? "—" : (Number.isInteger(v) ? String(v) : v.toFixed(2));

// ── 공통 헬퍼 (신규 동사 차트 공유) ────────────────────────────────────────────
// 범주형 섹터 팔레트 — 색맹 안전 톤(파랑/주황/청록/보라/올리브 계열 회피 충돌).
// 같은 섹터명은 항상 같은 색(모든 차트 공유)이도록 이름 해시로 고정 인덱싱.
const SECTOR_PALETTE = [
  "#4e79a7", "#f28e2b", "#59a14f", "#b07aa1", "#76b7b2",
  "#edc948", "#e15759", "#9c755f", "#bab0ac", "#ff9da7",
];
export function sectorColor(name: string): string {
  const s = name || "기타";
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return SECTOR_PALETTE[Math.abs(h) % SECTOR_PALETTE.length];
}

// 영문 지표키 → 한글 라벨. 미정의 키는 원본 그대로 사용.
export const METRIC_LABEL: Record<string, string> = {
  pb_ratio: "PBR", trailing_pe: "PER", ev_ebitda: "EV/EBITDA",
  sharpe: "샤프", cagr: "CAGR", cum_return: "누적수익", mdd: "MDD",
  sortino: "소르티노", market_cap: "시가총액", vol_annualized: "연변동성",
  max_drawdown: "최대낙폭",
};
const mlabel = (k: string): string => METRIC_LABEL[k] ?? k;

function Box({ title, sub, children }:
  { title: string; sub?: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 13, fontWeight: 600, color: C.text, marginBottom: 2 }}>{title}</div>
      {sub ? <div style={{ fontSize: 12, color: C.muted, marginBottom: 6 }}>{sub}</div> : null}
      {children}
    </div>
  );
}

function tip(rows: [string, string][], fmt: (v: number | null | undefined) => string = f2) {
  return ({ active, payload, label }:
    { active?: boolean; payload?: { payload: Record<string, unknown> }[]; label?: unknown }) => {
    if (!active || !payload?.length) return null;
    return (
      <div style={{ background: C.panel, border: `1px solid ${C.grid}`, borderRadius: 8,
        padding: "8px 10px", fontSize: 12, color: C.muted }}>
        <div style={{ fontWeight: 600, color: C.text, marginBottom: 4 }}>{String(label)}</div>
        {rows.map(([k, key]) => (
          <div key={k}>{k} {fmt(payload[0].payload[key] as number)}</div>
        ))}
      </div>
    );
  };
}

// ── 펼침 차트 (파라미터·종목·국면·기간분할) — buckets 동형 ─────────────────────
type SweepMetric = { key: keyof IrSweepBucket; label: string; kind: "signed" | "loss" | "pos" };
const SWEEP_METRICS: SweepMetric[] = [
  { key: "cum_return", label: "누적(%)", kind: "signed" },
  { key: "cagr", label: "CAGR(%)", kind: "signed" },
  { key: "sharpe", label: "샤프", kind: "signed" },
  { key: "sortino", label: "소르티노", kind: "signed" },
  { key: "mdd", label: "MDD(%)", kind: "loss" },
  { key: "win_rate", label: "승률(%)", kind: "pos" },
  { key: "payoff_ratio", label: "손익비", kind: "pos" },
];

const colorFor = (m: SweepMetric, v: number | null | undefined) =>
  m.kind === "loss" ? C.down : m.kind === "pos" ? C.accent
    : (v ?? 0) >= 0 ? C.up : C.down;

export function SweepChart({ axis, buckets, axes }: {
  axis: string;
  buckets: Record<string, IrSweepBucket>;
  axes?: { path: string; values: (number | string)[] }[];
}) {
  const [mi, setMi] = useState(0);
  const m = SWEEP_METRICS[mi];
  const entries = Object.entries(buckets).filter(([, b]) => !b.error);
  if (!entries.length) return null;

  // 가로축 의미(제목) — 축 종류별 커스터마이즈. 파라미터·종목·국면은 엔진 키가 의미를 담고,
  // 기간분할은 키 형태(2015 / 2015Q1 / 2015-01)로 주기를 판별해 연도/분기/월로 명명.
  const xTitle = axis === "parameter" ? "파라미터"
    : axis === "asset" ? "종목"
    : axis === "condition" ? "국면"
    : entries.every(([k]) => /^\d{4}$/.test(k)) ? "연도"
    : entries.every(([k]) => /^\d{4}Q\d$/.test(k)) ? "분기"
    : entries.every(([k]) => /^\d{4}-\d{2}$/.test(k)) ? "월" : "기간";

  // 파라미터 단일축이고 키가 모두 수치면 라인(민감도 곡선), 아니면 막대(범주 비교).
  const numericX = axis === "parameter" && (axes?.length ?? 1) <= 1
    && entries.every(([k]) => !Number.isNaN(num(k)));
  const data = entries.map(([k, b]) => ({
    label: k, x: numericX ? num(k) : k, value: (b[m.key] as number) ?? null, n: b.n,
  }));
  if (numericX) (data as { x: number }[]).sort((a, z) => a.x - z.x);
  const many = data.length > 24;   // 버킷 과다 시 x라벨 자동 솎기(252개 뭉개짐 방지)

  const Tip = tip([[m.label, "value"], ["표본", "n"]]);
  const yLabel = { value: m.label, angle: -90, position: "insideLeft" as const,
    style: { fontSize: 11, fill: C.muted, textAnchor: "middle" as const } };
  const xLabel = { value: xTitle, position: "insideBottom" as const, offset: -2,
    fontSize: 11, fill: C.muted };
  return (
    <Box title="시각화" sub={numericX
      ? `${xTitle}별 ${m.label} 민감도 — 지표 탭 전환 (평평=robust)`
      : `${xTitle}별 ${m.label} — 지표 탭으로 비교`}>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 8 }}>
        {SWEEP_METRICS.map((mm, i) => (
          <button key={mm.key} type="button" onClick={() => setMi(i)}
            style={{
              fontSize: 12, padding: "3px 9px", borderRadius: 999, cursor: "pointer",
              border: `1px solid ${i === mi ? C.accent : C.grid}`,
              background: i === mi ? C.accent : C.panel, color: i === mi ? "#fff" : C.muted,
            }}>{mm.label}</button>
        ))}
      </div>
      <ResponsiveContainer width="100%" height={272}>
        {numericX ? (
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
            <CartesianGrid stroke={C.grid} />
            <XAxis dataKey="x" type="number" domain={["dataMin", "dataMax"]}
              tick={{ fontSize: 11 }} height={40} label={xLabel} />
            <YAxis tick={{ fontSize: 11 }} width={54} label={yLabel} />
            <Tooltip content={<Tip />} />
            <ReferenceLine y={0} stroke={C.muted} strokeDasharray="3 3" />
            <Line type="monotone" dataKey="value" stroke={C.accent} strokeWidth={2}
              dot={{ r: 2 }} isAnimationActive={false} />
          </LineChart>
        ) : (
          <BarChart data={data} margin={{ top: 8, right: 16, bottom: 4, left: 8 }}>
            <CartesianGrid stroke={C.grid} vertical={false} />
            <XAxis dataKey="label" tick={{ fontSize: 11 }} interval={many ? "preserveStartEnd" : 0}
              angle={data.length > 6 ? -30 : 0} textAnchor={data.length > 6 ? "end" : "middle"}
              height={data.length > 6 ? 58 : 34} label={xLabel} />
            <YAxis tick={{ fontSize: 11 }} width={54} label={yLabel} />
            <Tooltip content={<Tip />} cursor={{ fill: C.accent + "14" }} />
            <ReferenceLine y={0} stroke={C.muted} strokeDasharray="3 3" />
            <Bar dataKey="value" isAnimationActive={false}>
              {data.map((d, i) => <Cell key={i} fill={colorFor(m, d.value)} />)}
            </Bar>
          </BarChart>
        )}
      </ResponsiveContainer>
    </Box>
  );
}

// ── 신호값 분포 — 분위수 박스/휘스커 (q05–q95 · q25–q75 · q50 · 평균) ───────────
function QuantileBox({ rows }: {
  rows: { label: string; d: IrDistribution }[];
}) {
  const vals = rows.flatMap(({ d }) => [d.quantiles?.q05, d.quantiles?.q95, d.mean])
    .filter((v): v is number => v != null && !Number.isNaN(v));
  if (!vals.length) return null;
  let lo = Math.min(...vals), hi = Math.max(...vals);
  if (lo === hi) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
  const X = (v: number) => ((v - lo) / (hi - lo)) * 100;
  const W = 100, H = 30;
  return (
    <div>
      {rows.map(({ label, d }) => {
        const q = d.quantiles ?? {};
        return (
          <div key={label} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
            <div style={{ width: 90, fontSize: 12, color: C.muted, textAlign: "right",
              overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{label}</div>
            <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none"
              style={{ flex: 1, height: 30 }}>
              {q.q05 != null && q.q95 != null && (
                <line x1={X(q.q05)} x2={X(q.q95)} y1={H / 2} y2={H / 2}
                  stroke={C.muted} strokeWidth={1} vectorEffect="non-scaling-stroke" />
              )}
              {q.q25 != null && q.q75 != null && (
                <rect x={X(q.q25)} y={H / 2 - 7} width={Math.max(0.4, X(q.q75) - X(q.q25))} height={14}
                  fill={C.accent} fillOpacity={0.25} stroke={C.accent} vectorEffect="non-scaling-stroke" />
              )}
              {q.q50 != null && (
                <line x1={X(q.q50)} x2={X(q.q50)} y1={H / 2 - 8} y2={H / 2 + 8}
                  stroke={C.strong} strokeWidth={2} vectorEffect="non-scaling-stroke" />
              )}
              {d.mean != null && (
                <circle cx={X(d.mean)} cy={H / 2} r={2.5} fill={C.text} />
              )}
            </svg>
            <div style={{ width: 120, fontSize: 11, color: C.muted, fontVariantNumeric: "tabular-nums" }}>
              중앙 {f2(q.q50)} · 평균 {f2(d.mean)}
            </div>
          </div>
        );
      })}
      <div style={{ fontSize: 11, color: C.muted, marginTop: 2 }}>
        막대 q25–q75 · 가는선 q05–q95 · 진한선 중앙값 · 점 평균
      </div>
    </div>
  );
}

export function SignalDistChart({ overall, byRegime }: {
  overall: IrDistribution; byRegime?: IrPartition | null;
}) {
  const rows: { label: string; d: IrDistribution }[] = [{ label: "전체", d: overall }];
  if (byRegime?.by_label) {
    for (const [k, d] of Object.entries(byRegime.by_label)) rows.push({ label: k, d });
  }
  return <Box title="시각화" sub="신호값의 분포 폭과 치우침(skew)을 봅니다 — 손익 아님">
    <QuantileBox rows={rows} />
  </Box>;
}

// ── 팩터 IC — horizon별 평균 IC 막대 (유의 p<0.05 강조) ────────────────────────
export function ICChart({ windows, byWindow }: {
  windows: string[];
  byWindow: Record<string, { overall: IrICStat }>;
}) {
  const data = windows.map((w) => {
    const o = byWindow[w]?.overall ?? ({} as IrICStat);
    return { label: `${w}일`, value: o.mean ?? null, sig: o.p_value != null && o.p_value < 0.05,
      ir: o.ir, t: o.t_stat, p: o.p_value, n: o.n };
  });
  if (!data.length) return null;
  const Tip = tip([["평균 IC", "value"], ["IR", "ir"], ["t", "t"], ["p", "p"], ["표본", "n"]]);
  return (
    <Box title="시각화" sub="예측 horizon별 평균 IC. 0에서 멀고 유의(진한 막대)할수록 예측력 ↑">
      <ResponsiveContainer width="100%" height={240}>
        <BarChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid stroke={C.grid} vertical={false} />
          <XAxis dataKey="label" tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} width={48} />
          <Tooltip content={<Tip />} cursor={{ fill: C.accent + "14" }} />
          <ReferenceLine y={0} stroke={C.muted} />
          <Bar dataKey="value" isAnimationActive={false}>
            {data.map((d, i) => (
              <Cell key={i} fill={(d.value ?? 0) >= 0 ? C.up : C.down}
                fillOpacity={d.sig ? 1 : 0.35} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </Box>
  );
}

// ── 이벤트 스터디 — forward 수익 곡선 + MAE/MFE 밴드 ──────────────────────────
export function EventStudyChart({ windows, overall }: {
  windows: string[];
  overall: Record<string, IrEventStat>;
}) {
  const data = [...windows]
    .sort((a, z) => (Number(a) || 0) - (Number(z) || 0))
    .map((w) => {
      const o = overall[w] ?? ({} as IrEventStat);
      return { label: `${w}일`, mean: o.mean ?? null,
        mae: o.mean_mae ?? null, mfe: o.mean_mfe ?? null,
        band: [o.mean_mae ?? null, o.mean_mfe ?? null] as [number | null, number | null],
        prob: o.prob_positive, p: o.p_value, n: o.n };
    });
  if (!data.length) return null;
  const Tip = tip([["평균수익", "mean"], ["MAE", "mae"], ["MFE", "mfe"],
    ["양(+)확률", "prob"], ["p", "p"], ["표본", "n"]]);
  return (
    <Box title="시각화"
      sub="진입 후 경과일별 평균 forward 수익(선) · 평균 최대낙폭~최대상승 범위(음영)">
      <ResponsiveContainer width="100%" height={260}>
        <ComposedChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid stroke={C.grid} />
          <XAxis dataKey="label" tick={{ fontSize: 11 }} />
          <YAxis tick={{ fontSize: 11 }} width={48} />
          <Tooltip content={<Tip />} />
          <ReferenceLine y={0} stroke={C.muted} strokeDasharray="3 3" />
          <Area dataKey="band" stroke="none" fill={C.accent} fillOpacity={0.12}
            isAnimationActive={false} />
          <Line type="monotone" dataKey="mean" stroke={C.accent} strokeWidth={2}
            dot={{ r: 2 }} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </Box>
  );
}

// ════════ 신규 동사 결과 컴포넌트 (select·describe·relate-regression·extremize) ════════
// 모두 위 C 토큰·Box·f2 규약을 미러. 결측 None=f2로 "—". 엔진이 준 메타(eligible/
// coverage/CI/t/oos_guard)를 버리지 않고 표면화(visualization_spec §2 정직성).

// 작은 배지/칩 — 리스크·밸류·PIT 라벨용.
function Badge({ children, tone = "muted" }:
  { children: React.ReactNode; tone?: "muted" | "up" | "down" | "accent" | "warn" }) {
  const map = {
    muted: { bg: "rgba(139,148,163,0.14)", fg: C.muted, bd: C.grid },
    up: { bg: C.upSoft, fg: C.up, bd: C.up },
    down: { bg: C.downSoft, fg: C.down, bd: C.down },
    accent: { bg: "rgba(196,152,43,0.16)", fg: C.strong, bd: C.accent },
    warn: { bg: "rgba(245,158,11,0.16)", fg: "#e0a948", bd: "#f59e0b" },
  }[tone];
  return (
    <span style={{
      display: "inline-block", fontSize: 11, padding: "1px 7px", borderRadius: 999,
      background: map.bg, color: map.fg, border: `1px solid ${map.bd}`, whiteSpace: "nowrap",
    }}>{children}</span>
  );
}

// % 포맷 — 결측 "—", 그 외 소수 2자리 + "%". 부호색은 호출측이 결정.
// 입력은 **소수(fraction)** — 엔진이 수익률·변동성·MDD를 0.05(=5%)로 반환하므로 표시 시 ×100.
const pct = (v: number | null | undefined) =>
  v == null || Number.isNaN(v) ? "—" : `${(v * 100).toFixed(2)}%`;

const tdStyle: React.CSSProperties = {
  padding: "5px 8px", borderBottom: `1px solid ${C.grid}`, fontSize: 12,
  color: C.text, fontVariantNumeric: "tabular-nums",
};
const thStyle: React.CSSProperties = {
  padding: "5px 8px", borderBottom: `2px solid ${C.grid}`, fontSize: 11,
  color: C.muted, fontWeight: 600, textAlign: "left", whiteSpace: "nowrap",
};

// 작은 KPI 카드 래퍼.
function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{
      border: `1px solid ${C.grid}`, borderRadius: 10, padding: "10px 12px",
      background: C.panel, minWidth: 0,
    }}>
      <div style={{ fontSize: 11, color: C.muted, fontWeight: 600, marginBottom: 6 }}>{title}</div>
      {children}
    </div>
  );
}

// 신규 동사용 커스텀 툴팁(모듈 스코프 — 렌더마다 재생성 방지). 다필드라 tip()로는 표현 불가.
const tipBox: React.CSSProperties = {
  background: C.panel, border: `1px solid ${C.grid}`, borderRadius: 8,
  padding: "8px 10px", fontSize: 12, color: C.muted,
};
function RankedTip({ active, payload }: {
  active?: boolean;
  payload?: { payload: { symbol: string; score: number | null; sector: string } }[];
}) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div style={tipBox}>
      <div style={{ fontWeight: 600, color: C.text }}>{d.symbol}</div>
      <div>섹터 {d.sector || "—"}</div>
      <div>점수 {f2(d.score)}</div>
    </div>
  );
}
function SectorPieTip({ active, payload }: {
  active?: boolean; payload?: { payload: { name: string; value: number } }[];
}) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div style={{ ...tipBox, padding: "6px 9px" }}>
      <span style={{ color: C.text, fontWeight: 600 }}>{d.name}</span> {pct(d.value)}
    </div>
  );
}
function RegressionTip({ active, payload }: {
  active?: boolean;
  payload?: { payload: { name: string; coef: number; ci_low: number; ci_high: number;
                         t_stat: number | null; t_inf: boolean } }[];
}) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div style={tipBox}>
      <div style={{ fontWeight: 600, color: C.text }}>{d.name}</div>
      <div>계수 {f2(d.coef)}</div>
      <div>95% CI [{f2(d.ci_low)}, {f2(d.ci_high)}]</div>
      <div>t {d.t_inf ? "유의(∞)" : f2(d.t_stat)}</div>
    </div>
  );
}

// ── RankedListChart (select) ──────────────────────────────────────────────────
export function RankedListChart({ results, as_of, universe_size, eligible_size }: {
  results: { symbol: string; score: number | null; sector: string;
             metrics: Record<string, number | null> }[];
  as_of?: string; universe_size?: number; eligible_size?: number;
}) {
  const rows = results ?? [];
  if (!rows.length) {
    return <Box title="랭킹 선별" sub="조건을 만족하는 종목이 없습니다."><div /></Box>;
  }
  // metric 컬럼 합집합(순서 보존) — 표 헤더.
  const metricKeys: string[] = [];
  for (const r of rows) {
    for (const k of Object.keys(r.metrics ?? {})) if (!metricKeys.includes(k)) metricKeys.push(k);
  }
  const data = rows.map((r) => ({ symbol: r.symbol, score: r.score ?? null, sector: r.sector }));
  const cap = `${as_of ?? "—"} 기준 · 전체 ${universe_size ?? "—"} 중 자격 ${eligible_size ?? "—"}`
    + ` → 상위 ${rows.length}`;
  return (
    <Box title="랭킹 선별" sub={cap}>
      <ResponsiveContainer width="100%" height={Math.max(160, rows.length * 30 + 30)}>
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid stroke={C.grid} horizontal={false} />
          <XAxis type="number" tick={{ fontSize: 11 }} />
          <YAxis type="category" dataKey="symbol" tick={{ fontSize: 11 }} width={88}
            interval={0} />
          <Tooltip content={<RankedTip />} cursor={{ fill: C.accent + "14" }} />
          <Bar dataKey="score" isAnimationActive={false}>
            {data.map((d, i) => <Cell key={i} fill={sectorColor(d.sector)} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      <div style={{ overflowX: "auto", marginTop: 8 }}>
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <thead><tr>
            <th style={thStyle}>종목</th><th style={thStyle}>섹터</th>
            <th style={{ ...thStyle, textAlign: "right" }}>점수</th>
            {metricKeys.map((k) => (
              <th key={k} style={{ ...thStyle, textAlign: "right" }}>{mlabel(k)}</th>
            ))}
          </tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td style={tdStyle}>{r.symbol}</td>
                <td style={tdStyle}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                    <span style={{ width: 9, height: 9, borderRadius: 2,
                      background: sectorColor(r.sector), display: "inline-block" }} />
                    {r.sector || "—"}
                  </span>
                </td>
                <td style={{ ...tdStyle, textAlign: "right" }}>{f2(r.score)}</td>
                {metricKeys.map((k) => (
                  <td key={k} style={{ ...tdStyle, textAlign: "right" }}>{f2(r.metrics?.[k])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Box>
  );
}

// ── ReportCards (describe single) ─────────────────────────────────────────────
export function ReportCards({ r }: { r: IrSingleReport }) {
  const price = r.price ?? ({} as IrSingleReport["price"]);
  const risk = r.risk ?? ({} as IrSingleReport["risk"]);
  const fund = r.fundamentals ?? ({} as IrSingleReport["fundamentals"]);
  const longTermNA = r.data_points != null && r.data_points < 252;   // 장기수익 None 자연 표기

  // 기간수익 막대 — 1/3/6/12m, 부호색 up/down.
  const retOrder: ("1m" | "3m" | "6m" | "12m")[] = ["1m", "3m", "6m", "12m"];
  const retData = retOrder.map((k) => ({
    label: k, value: price.returns?.[k] ?? null,
  }));

  // 52주 레인지바 — low~high 위 last 마커.
  const lo = price.low_52w, hi = price.high_52w, last = price.last;
  const range = hi != null && lo != null && hi > lo ? hi - lo : null;
  const lastPos = range != null && last != null
    ? Math.min(100, Math.max(0, ((last - lo!) / range) * 100)) : null;

  const RetTip = tip([["수익", "value"]], pct);
  const grid: React.CSSProperties = {
    display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10,
  };
  const cons = r.consensus;
  const flow = r.flow;
  const eok = (v: number | null | undefined) =>
    v == null ? "—" : Math.round(v / 1e8).toLocaleString();
  const opLabel = (v: number | null | undefined) =>
    v == null ? "—" : v > 0.3 ? "매수 우위" : v < -0.3 ? "매도 우위" : "중립";
  return (
    <Box title={`${r.symbol} · ${r.sector || "—"} · ${r.as_of ?? "—"}`}
      sub={`데이터 ${r.data_points ?? "—"}일${longTermNA ? " · 252일 미만 — 장기수익 일부 미표기" : ""}`}>
      <div style={grid}>
        {/* ① 가격 + 52주 레인지 */}
        <Card title="가격 · 52주 위치">
          <div style={{ fontSize: 20, fontWeight: 700, color: C.text,
            fontVariantNumeric: "tabular-nums" }}>{f2(last)}</div>
          <div style={{ margin: "8px 0 4px" }}>
            <div style={{ position: "relative", height: 8, borderRadius: 999, background: C.grid }}>
              {lastPos != null && (
                <div style={{ position: "absolute", left: `calc(${lastPos}% - 4px)`, top: -2,
                  width: 8, height: 12, borderRadius: 2, background: C.accent }} />
              )}
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11,
              color: C.muted, marginTop: 3, fontVariantNumeric: "tabular-nums" }}>
              <span>저 {f2(lo)}</span><span>고 {f2(hi)}</span>
            </div>
          </div>
          <div style={{ fontSize: 12, color: C.muted }}>
            52주 고점 대비 <span style={{ color: (price.pct_from_52w_high ?? 0) < 0 ? C.down : C.up }}>
              {pct(price.pct_from_52w_high)}</span>
          </div>
        </Card>

        {/* ② 기간 수익 */}
        <Card title="기간 수익 (1·3·6·12개월)">
          <ResponsiveContainer width="100%" height={120}>
            <BarChart data={retData} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
              <CartesianGrid stroke={C.grid} vertical={false} />
              <XAxis dataKey="label" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 10 }} width={36}
                tickFormatter={(v) => `${(v * 100).toFixed(0)}%`} />
              <Tooltip content={<RetTip />} cursor={{ fill: C.accent + "14" }} />
              <ReferenceLine y={0} stroke={C.muted} />
              <Bar dataKey="value" isAnimationActive={false}>
                {retData.map((d, i) => (
                  <Cell key={i} fill={(d.value ?? 0) >= 0 ? C.up : C.down} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </Card>

        {/* ③ 리스크 */}
        <Card title="리스크">
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            <Badge tone="muted">연변동성 {pct(risk.vol_annualized)}</Badge>
            <Badge tone="down">MDD {pct(risk.max_drawdown)}</Badge>
          </div>
        </Card>

        {/* ④ 밸류 — null이면 "데이터 없음" */}
        <Card title="밸류에이션">
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            {(["pb_ratio", "trailing_pe", "ev_ebitda"] as const).map((k) => (
              fund[k] == null
                ? <Badge key={k} tone="muted">{mlabel(k)} 데이터 없음</Badge>
                : <Badge key={k} tone="accent">{mlabel(k)} {f2(fund[k])}</Badge>
            ))}
          </div>
        </Card>

        {/* ⑤ 애널 컨센서스 — KR 라이브. 데이터 있을 때만(가짜 채움 금지). */}
        {cons && cons.consensus_target != null && (
          <Card title="애널 컨센서스">
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              <Badge tone="accent">목표가 {f2(cons.consensus_target)}</Badge>
              <Badge tone={(cons.target_upside ?? 0) >= 0 ? "up" : "down"}>
                상승여력 {pct(cons.target_upside)}</Badge>
              <Badge tone="muted">애널 {cons.analyst_count != null ? Math.round(cons.analyst_count) : "—"}명</Badge>
              <Badge tone="muted">의견 {opLabel(cons.consensus_opinion)}</Badge>
            </div>
          </Card>
        )}

        {/* ⑥ 수급 — 최근 20거래일 기관·외국인 순매수(억원). 데이터 있을 때만. */}
        {flow && (flow.inst_net_buy_20d != null || flow.foreign_net_buy_20d != null) && (
          <Card title="수급 · 최근 20일 순매수">
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {flow.inst_net_buy_20d != null && (
                <Badge tone={flow.inst_net_buy_20d >= 0 ? "up" : "down"}>
                  기관 {eok(flow.inst_net_buy_20d)}억</Badge>
              )}
              {flow.foreign_net_buy_20d != null && (
                <Badge tone={flow.foreign_net_buy_20d >= 0 ? "up" : "down"}>
                  외국인 {eok(flow.foreign_net_buy_20d)}억</Badge>
              )}
            </div>
          </Card>
        )}
      </div>

      {/* ⑤ 최근 뉴스 — '왜 움직였나' facet(네이버 검색). 없으면 카드 자체를 숨긴다. */}
      {r.news && r.news.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <Card title="최근 뉴스 · 왜 움직였나">
            <ul style={{ margin: 0, padding: 0, listStyle: "none",
              display: "flex", flexDirection: "column", gap: 9 }}>
              {r.news.slice(0, 5).map((n, i) => (
                <li key={i} style={{ borderLeft: `2px solid ${C.grid}`, paddingLeft: 9 }}>
                  <a href={n.link} target="_blank" rel="noopener noreferrer"
                    style={{ color: C.text, textDecoration: "none", fontWeight: 600,
                      fontSize: 13, lineHeight: 1.35 }}>{n.title}</a>
                  {n.desc && <div style={{ color: C.muted, fontSize: 12, marginTop: 2,
                    lineHeight: 1.4 }}>{n.desc}</div>}
                </li>
              ))}
            </ul>
          </Card>
        </div>
      )}
    </Box>
  );
}

// ── DiagnosisPanel (describe portfolio) ───────────────────────────────────────
export function DiagnosisPanel({ r }: { r: IrPortfolioDiagnosis }) {
  const holdings = r.holdings ?? [];
  const conc = r.concentration ?? ({} as IrPortfolioDiagnosis["concentration"]);
  const risk = r.risk ?? ({} as IrPortfolioDiagnosis["risk"]);
  const val = r.valuation ?? ({} as IrPortfolioDiagnosis["valuation"]);
  const cov = r.coverage ?? ({} as IrPortfolioDiagnosis["coverage"]);
  const exposure = r.sector_exposure ?? {};

  const pieData = Object.entries(exposure)
    .map(([sector, weight]) => ({ name: sector, value: weight }))
    .sort((a, b) => b.value - a.value);
  const concentrated = conc.hhi != null && conc.hhi > 0.25;
  const valCovNote = cov.with_fundamentals != null && r.n_holdings != null
    && cov.with_fundamentals < r.n_holdings;

  const grid: React.CSSProperties = {
    display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 10,
  };
  return (
    <Box title={`포트폴리오 진단 · ${r.as_of ?? "—"}`}
      sub={`보유 ${r.n_holdings ?? holdings.length}종목${valCovNote
        ? ` · 가중밸류 ${cov.with_fundamentals}/${r.n_holdings} 종목 기준` : ""}`}>
      <div style={grid}>
        {/* 섹터 노출 도넛 */}
        <Card title="섹터 노출">
          {pieData.length ? (
            <ResponsiveContainer width="100%" height={180}>
              <PieChart>
                <Pie data={pieData} dataKey="value" nameKey="name" cx="50%" cy="50%"
                  innerRadius={42} outerRadius={70} isAnimationActive={false}>
                  {pieData.map((d, i) => <Cell key={i} fill={sectorColor(d.name)} />)}
                </Pie>
                <Tooltip content={<SectorPieTip />} />
              </PieChart>
            </ResponsiveContainer>
          ) : <div style={{ fontSize: 12, color: C.muted }}>섹터 정보 없음</div>}
        </Card>

        {/* 집중도 */}
        <Card title="집중도">
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            <Badge tone={concentrated ? "warn" : "muted"}>HHI {f2(conc.hhi)}</Badge>
            <Badge tone="muted">유효종목수 {f2(conc.effective_n)}</Badge>
            <Badge tone="muted">최대비중 {pct(conc.top_weight)}</Badge>
            <Badge tone="muted">상위3 {pct(conc.top3_weight)}</Badge>
          </div>
          {concentrated && (
            <div style={{ fontSize: 11, color: "#b45309", marginTop: 6 }}>
              HHI &gt; 0.25 — 집중된 포트폴리오</div>
          )}
        </Card>

        {/* 리스크 */}
        <Card title="리스크">
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            <Badge tone="muted">연변동성 {pct(risk.portfolio_vol_annualized)}</Badge>
            <Badge tone="muted">평균상관 {f2(risk.avg_pairwise_corr)}</Badge>
          </div>
        </Card>

        {/* 가중 밸류 */}
        <Card title="가중 밸류에이션">
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
            <Badge tone={val.weighted_pb == null ? "muted" : "accent"}>
              가중 PBR {f2(val.weighted_pb)}</Badge>
            <Badge tone={val.weighted_pe == null ? "muted" : "accent"}>
              가중 PER {f2(val.weighted_pe)}</Badge>
          </div>
        </Card>
      </div>

      {/* holdings 테이블 */}
      <div style={{ overflowX: "auto", marginTop: 10 }}>
        <table style={{ borderCollapse: "collapse", width: "100%" }}>
          <thead><tr>
            <th style={thStyle}>종목</th>
            <th style={{ ...thStyle, textAlign: "right" }}>비중</th>
            <th style={thStyle}>섹터</th>
          </tr></thead>
          <tbody>
            {holdings.map((h, i) => (
              <tr key={i}>
                <td style={tdStyle}>{h.symbol}</td>
                <td style={{ ...tdStyle, textAlign: "right" }}>
                  {pct(h.weight)}</td>
                <td style={tdStyle}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
                    <span style={{ width: 9, height: 9, borderRadius: 2,
                      background: sectorColor(h.sector), display: "inline-block" }} />
                    {h.sector || "—"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Box>
  );
}

// ── RegressionChart (relate regression) ───────────────────────────────────────
export function RegressionChart({ r }: { r: IrRegressionResult }) {
  const windows = r.windows ?? [];
  const [wi, setWi] = useState(0);
  const w = windows[wi] ?? windows[0];
  const wd = w != null ? r.by_window?.[w] : undefined;
  const factors = wd?.factors ?? null;

  const data = (factors ?? []).map((f) => ({
    name: f.name, coef: f.coef, ci_low: f.ci_low, ci_high: f.ci_high,
    t_stat: f.t_stat, t_inf: f.t_inf,
  }));
  return (
    <Box title="다중팩터 회귀 (Fama-MacBeth)"
      sub="분석 전용(forward 미래참조) · 계수가 0에서 멀고 CI가 0을 비포함하면 예측 기여">
      {windows.length > 1 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 8 }}>
          {windows.map((ww, i) => (
            <button key={ww} type="button" onClick={() => setWi(i)}
              style={{
                fontSize: 12, padding: "3px 9px", borderRadius: 999, cursor: "pointer",
                border: `1px solid ${i === wi ? C.accent : C.grid}`,
                background: i === wi ? C.accent : C.panel, color: i === wi ? "#fff" : C.muted,
              }}>{ww}일</button>
          ))}
        </div>
      )}
      {factors == null ? (
        <div style={{ fontSize: 12, color: C.muted }}>
          {wd?.note ?? "이 윈도우는 회귀 결과가 없습니다(기간 부족)."}</div>
      ) : (
        <>
          <div style={{ fontSize: 11, color: C.muted, marginBottom: 4 }}>
            {w}일 forward · 기간 {wd?.n_periods ?? "—"}</div>
          <ResponsiveContainer width="100%" height={Math.max(160, data.length * 34 + 30)}>
            <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 0, left: 8 }}>
              <CartesianGrid stroke={C.grid} horizontal={false} />
              <XAxis type="number" tick={{ fontSize: 11 }} />
              <YAxis type="category" dataKey="name" tick={{ fontSize: 11 }} width={96} interval={0} />
              <Tooltip content={<RegressionTip />} cursor={{ fill: C.accent + "14" }} />
              <ReferenceLine x={0} stroke={C.text} />
              <Bar dataKey="coef" isAnimationActive={false}>
                {data.map((d, i) => (
                  <Cell key={i} fill={(d.coef ?? 0) >= 0 ? C.up : C.down} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
          <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>
            {data.map((d) => `${d.name}: β=${f2(d.coef)} t=${d.t_inf ? "∞" : f2(d.t_stat)}`)
              .join(" · ")}
          </div>
        </>
      )}
    </Box>
  );
}

// ── ExtremizeChart (extremize) ────────────────────────────────────────────────
export function ExtremizeChart({ r }: { r: IrExtremizeResult }) {
  const ranked = r.ranked ?? [];
  const obj = r.objective ?? ({} as IrExtremizeResult["objective"]);
  const best = r.best ?? ({} as IrExtremizeResult["best"]);
  const data = ranked.map((d) => ({ label: d.label, value: d.metric_value ?? null,
    best: d.label === best.label }));

  const dirLabel = obj.direction === "min" ? "최소" : obj.direction === "max" ? "최대" : obj.direction;
  const Tip = tip([[mlabel(obj.metric ?? "값"), "value"]]);

  // OOS 가드 — buckets(폴드별) 일관성. 부호 반전·큰 분산이면 경고.
  const guard = r.oos_guard;
  const buckets = guard && !("error" in guard) ? (guard.buckets as
    Record<string, unknown> | undefined) : undefined;
  const bucketRows = buckets
    ? Object.entries(buckets).map(([k, v]) => {
        const val = typeof v === "number" ? v
          : (v && typeof v === "object" && "metric_value" in v
              ? (v as { metric_value: number | null }).metric_value : null);
        return { label: k, value: val as number | null };
      })
    : [];
  const signs = bucketRows.map((b) => b.value).filter((v): v is number => v != null);
  const flipped = signs.length > 1 && signs.some((v) => v >= 0) && signs.some((v) => v < 0);

  return (
    <Box title="최적해 탐색"
      sub={`목적: ${mlabel(obj.metric ?? "")} ${dirLabel ?? ""}`
        + (obj.oos_guard ? " · OOS 과최적화 가드" : "")}>
      {data.length ? (
        <ResponsiveContainer width="100%" height={Math.max(160, data.length * 30 + 30)}>
          <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 0, left: 8 }}>
            <CartesianGrid stroke={C.grid} horizontal={false} />
            <XAxis type="number" tick={{ fontSize: 11 }} />
            <YAxis type="category" dataKey="label" tick={{ fontSize: 11 }} width={110} interval={0} />
            <Tooltip content={<Tip />} cursor={{ fill: C.accent + "14" }} />
            <ReferenceLine x={0} stroke={C.muted} strokeDasharray="3 3" />
            <Bar dataKey="value" isAnimationActive={false}>
              {data.map((d, i) => (
                <Cell key={i} fill={d.best ? C.accent : C.muted}
                  stroke={d.best ? C.strong : "none"} strokeWidth={d.best ? 1.5 : 0} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      ) : <div style={{ fontSize: 12, color: C.muted }}>탐색 결과가 없습니다.</div>}

      <div style={{ fontSize: 12, color: C.muted, marginTop: 4 }}>
        최적: <span style={{ color: C.strong, fontWeight: 600 }}>{best.label ?? "—"}</span>
        {" "}({mlabel(obj.metric ?? "")} {f2(best.metric_value)})
      </div>

      {/* OOS 폴드 일관성 */}
      {guard && "error" in guard && guard.error ? (
        <div style={{ fontSize: 11, color: C.muted, marginTop: 8 }}>
          OOS 가드: {guard.error}</div>
      ) : bucketRows.length ? (
        <div style={{ marginTop: 10 }}>
          <div style={{ fontSize: 11, color: C.muted, marginBottom: 4 }}>
            OOS 폴드 일관성{flipped
              ? <span style={{ color: "#b45309", fontWeight: 600 }}> · 부호 반전 — 과최적화 주의</span>
              : null}
          </div>
          <table style={{ borderCollapse: "collapse" }}>
            <thead><tr>
              <th style={thStyle}>폴드</th>
              <th style={{ ...thStyle, textAlign: "right" }}>{mlabel(obj.metric ?? "값")}</th>
            </tr></thead>
            <tbody>
              {bucketRows.map((b, i) => (
                <tr key={i}>
                  <td style={tdStyle}>{b.label}</td>
                  <td style={{ ...tdStyle, textAlign: "right",
                    color: b.value != null && b.value < 0 ? C.down : C.text }}>{f2(b.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Box>
  );
}

// ── 상관행렬 히트맵 (P5a) — N×N 셀, +상관=빨강(동행)·−상관=파랑(역행) 디버징(한국식 방향색) ──
export function CorrelationHeatmap({ symbols, matrix }: {
  symbols: string[]; matrix: (number | null)[][];
}) {
  if (!symbols.length) return null;
  const cellColor = (v: number | null): string => {
    if (v == null) return "transparent";
    const t = Math.max(-1, Math.min(1, v));
    const a = (0.1 + 0.6 * Math.abs(t)).toFixed(3);
    return t >= 0 ? `rgba(222,48,51,${a})` : `rgba(22,104,196,${a})`;
  };
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="corr-heatmap">
        <thead>
          <tr><th />{symbols.map((s) => <th key={s} title={s}>{s}</th>)}</tr>
        </thead>
        <tbody>
          {symbols.map((s, i) => (
            <tr key={s}>
              <th title={s}>{s}</th>
              {symbols.map((t, j) => {
                const v = matrix[i]?.[j] ?? null;
                return (
                  <td key={t} style={{ background: cellColor(v) }}
                      title={`${s} ↔ ${t}: ${v == null ? "—" : v.toFixed(3)}`}>
                    {v == null ? "—" : v.toFixed(2)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>
        +상관(빨강)=함께 움직임 · −상관(파랑)=반대로 움직임(분산·헤지 후보)
      </div>
    </div>
  );
}

// ── PRESCRIBE 비중 추천 (P5b) — 추천 목적의 비중 트리맵 + 목적별 비교표 ──────────
const OBJ_LABEL: Record<string, string> = {
  min_variance: "최소분산", max_sharpe: "최대샤프", risk_parity: "리스크패리티", equal_weight: "동일가중",
};
const _pctOr = (v: number | null | undefined) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);

// 트리맵 셀 — 면적=비중. recharts가 x/y/width/height/name/value/index를 주입.
function WeightCell(props: {
  x?: number; y?: number; width?: number; height?: number; name?: string; value?: number; index?: number;
}) {
  const { x = 0, y = 0, width = 0, height = 0, name = "", value, index = 0 } = props;
  return (
    <g>
      <rect x={x} y={y} width={width} height={height}
            style={{ fill: SECTOR_PALETTE[index % SECTOR_PALETTE.length], stroke: C.panel, strokeWidth: 2 }} />
      {width > 44 && height > 22
        ? <text x={x + 5} y={y + 15} fill="#fff" fontSize={11} fontWeight={600}>{name}</text> : null}
      {width > 44 && height > 36 && value != null
        ? <text x={x + 5} y={y + 29} fill="#fff" fontSize={10}>{(value * 100).toFixed(1)}%</text> : null}
    </g>
  );
}

export function PrescribePanel({ r }: { r: PrescribeResult }) {
  const objs = r.objectives ?? {};
  const rec = r.recommended ?? "max_sharpe";
  const recObj = objs[rec];
  const order = ["max_sharpe", "min_variance", "risk_parity", "equal_weight"].filter((k) => objs[k]);
  const treeData = recObj
    ? Object.entries(recObj.weights).filter(([, v]) => (v ?? 0) > 0.005)
        .sort((a, b) => b[1] - a[1]).map(([name, size]) => ({ name, size }))
    : [];
  return (
    <Box title="포트폴리오 비중 추천"
         sub={`${r.symbols?.length ?? 0}종목 · 추천 ${OBJ_LABEL[rec] ?? rec} · n=${r.n_obs ?? "?"}일`}>
      {treeData.length > 0 ? (
        <ResponsiveContainer width="100%" height={220}>
          <Treemap data={treeData} dataKey="size" nameKey="name"
                   isAnimationActive={false} content={<WeightCell />} />
        </ResponsiveContainer>
      ) : null}
      <div style={{ overflowX: "auto", marginTop: 8 }}>
        <table className="sweep-table">
          <thead><tr><th>목적</th><th>기대수익(연)</th><th>기대변동성(연)</th><th>샤프</th><th>상위 비중</th></tr></thead>
          <tbody>
            {order.map((k) => {
              const o = objs[k];
              if (!o) return null;
              const top = Object.entries(o.weights).sort((a, b) => b[1] - a[1]).slice(0, 4)
                .map(([s, v]) => `${s} ${(v * 100).toFixed(1)}%`).join(", ");
              return (
                <tr key={k} style={k === rec ? { fontWeight: 600 } : undefined}>
                  <td>{OBJ_LABEL[k] ?? k}{k === rec ? " ★" : ""}</td>
                  <td>{_pctOr(o.exp_return)}</td>
                  <td>{_pctOr(o.exp_vol)}</td>
                  <td>{o.sharpe != null ? o.sharpe.toFixed(2) : "—"}</td>
                  <td style={{ fontSize: 11, color: C.muted }}>{top}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{ fontSize: 11, color: C.muted, marginTop: 4 }}>
        ★ 추천 · 트리맵=추천 목적의 비중(면적). 최대샤프는 기대수익=과거평균 추정(노이즈 큼) — 위험기반이 더 안정적.
      </div>
    </Box>
  );
}

// ── 시장 breadth (P5c) — 상승/하락 폭 + MA 상회 + 섹터 분산("시장이 왜/어떤가") ──
export function BreadthPanel({ r }: { r: BreadthResult }) {
  const n = r.n ?? 0;
  const pctUp = r.pct_up != null ? r.pct_up * 100 : 50;
  const sectors = r.sector_breakdown ?? [];
  const maxAbs = Math.max(0.0001, ...sectors.map(([, v]) => Math.abs(v)));
  const sign = (v: number | null | undefined) => (v == null ? C.text : v >= 0 ? C.up : C.down);
  const movers = (xs?: [string, number][]) =>
    (xs ?? []).map(([s, v]) => `${s} ${(v * 100).toFixed(1)}%`).join(", ") || "—";
  return (
    <Box title="시장 breadth" sub={`${n}종목 · 상승 ${r.n_up ?? 0} / 하락 ${r.n_down ?? 0}`}>
      <div style={{ display: "flex", height: 22, borderRadius: 4, overflow: "hidden", marginBottom: 8 }}>
        <div style={{ width: `${pctUp}%`, background: C.up, color: "#fff", fontSize: 11,
          textAlign: "center", lineHeight: "22px", minWidth: 30 }}>상승 {r.n_up ?? 0}</div>
        <div style={{ flex: 1, background: C.down, color: "#fff", fontSize: 11,
          textAlign: "center", lineHeight: "22px", minWidth: 30 }}>하락 {r.n_down ?? 0}</div>
      </div>
      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", fontSize: 12, marginBottom: 8 }}>
        <span>평균 1일 <b style={{ color: sign(r.avg_r1) }}>{_pctOr(r.avg_r1)}</b></span>
        <span>5일 <b style={{ color: sign(r.avg_r5) }}>{_pctOr(r.avg_r5)}</b></span>
        <span>20일 <b style={{ color: sign(r.avg_r20) }}>{_pctOr(r.avg_r20)}</b></span>
        <span style={{ color: C.muted }}>20일선 위 {_pctOr(r.pct_above_ma20)} · 60일선 위 {_pctOr(r.pct_above_ma60)}</span>
      </div>
      {sectors.length > 0 ? (
        <div style={{ marginBottom: 8 }}>
          <div style={{ fontSize: 12, color: C.muted, marginBottom: 4 }}>섹터별 평균 1일 수익</div>
          {sectors.map(([name, v]) => (
            <div key={name} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, margin: "2px 0" }}>
              <span style={{ width: 84, textAlign: "right", color: C.muted, overflow: "hidden",
                textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{name}</span>
              <div style={{ flex: 1, height: 12, background: C.panel, borderRadius: 2 }}>
                <div style={{ height: 12, borderRadius: 2, background: v >= 0 ? C.up : C.down,
                  width: `${(Math.abs(v) / maxAbs) * 100}%` }} />
              </div>
              <span style={{ width: 52, color: v >= 0 ? C.up : C.down }}>{(v * 100).toFixed(1)}%</span>
            </div>
          ))}
        </div>
      ) : null}
      <div style={{ display: "flex", gap: 16, fontSize: 11, flexWrap: "wrap" }}>
        <div><span style={{ color: C.muted }}>상위: </span>{movers(r.top_gainers)}</div>
        <div><span style={{ color: C.muted }}>하위: </span>{movers(r.top_losers)}</div>
      </div>
    </Box>
  );
}

// ── 뉴스 리서치 (shape "news_research") — 증거 다이제스트 + 결정적 인용 링크 ──────
export function NewsDigest({ r }: { r: NewsDigestResult }) {
  const cites = r.citations ?? [];
  return (
    <Box title="뉴스 리서치" sub={`${r.n ?? 0}건 · ${(r.sources ?? []).join(", ")}`}>
      <div style={{ whiteSpace: "pre-wrap", fontSize: 13, color: C.text, lineHeight: 1.55 }}>
        {r.digest ?? "—"}
      </div>
      {cites.length > 0 ? (
        <div style={{ marginTop: 8, borderTop: `1px solid ${C.grid}`, paddingTop: 6 }}>
          <div style={{ fontSize: 11, color: C.muted, marginBottom: 4 }}>출처</div>
          <ol style={{ margin: 0, paddingLeft: 18, fontSize: 11 }}>
            {cites.map((c) => (
              <li key={c.n} style={{ margin: "2px 0" }}>
                <a href={c.url} target="_blank" rel="noopener noreferrer"
                   style={{ color: C.accent, textDecoration: "none" }}>{c.title}</a>
                <span style={{ color: C.muted }}> · {c.source} · {(c.date ?? "").slice(0, 8)}</span>
              </li>
            ))}
          </ol>
        </div>
      ) : null}
    </Box>
  );
}

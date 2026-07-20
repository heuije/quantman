/**
 * ChatResultView — tool_result 인라인 차트 렌더러 (P1a · P3 레지스트리)
 *
 * P3(seam #2 정비): 엔진(run_query)이 스탬프한 result.shape를 키로 **렌더러 레지스트리**를
 * 단일 조회한다(순서의존 if/elif 제거). 새 형상(히트맵·트리맵 등) = RENDERERS 등록 1건.
 *   shape → 컴포넌트:
 *     select→RankedListChart · describe_single→ReportCards · describe_portfolio→DiagnosisPanel
 *     extremize→ExtremizeChart · relate_regression→RegressionChart · relate_ic→ICChart
 *     event_study→EventStudyChart · signal_dist→SignalDistChart · sweep→SweepChart(+버킷표·유의성)
 *     inspect→원시 시계열 라인 · simulate→지표행+EquityChart
 *   save_strategy(r.strategy_id)는 엔진 형상이 아니라 상태 카드 → 레지스트리 밖 선처리.
 *
 * 폴백: 미스탬프 결과(레거시·우회)는 deriveShape(summarize.result_shape와 동일 순서)로 형상 추론.
 *   ⚠ deriveShape 순서: 국면 contrast는 top-level equity를 함께 실어 보내므로 axis/buckets 판별을
 *   equity 판별 *앞*에 둔다(일반 백테스트로 오인 방지). 스탬프가 있으면 이 순서는 무관.
 *
 * 차트 컴포넌트는 ResultCharts에서 재사용(노코드 ResultPanel과 공유). ResultPanel/IrBuilder는 미수정.
 */

import {
  Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { Link } from "react-router-dom";
import { Fragment, useState, type ReactElement } from "react";
import EquityChart from "./EquityChart";
import { StatNote } from "./StatGlossary";
import { chartC, useThemeName } from "../chartColors";
import ExcelExportButton from "./ExcelExportButton";
import AutotradeLinkButton from "./AutotradeLinkButton";
import ParamControls, { type AdjustableParam } from "./ParamControls";
import {
  BreadthPanel, CorrelationHeatmap, DiagnosisPanel, EventStudyChart, ExtremizeChart, HeatmapPanel, ICChart,
  NewsDigest, PrescribePanel, RankedListChart, RegressionChart, ReportCards, SignalDistChart, SweepChart,
} from "./ResultCharts";
import type { HeatmapResult } from "./ResultCharts";
import type {
  BreadthResult, IrDistribution, IrEventStat, IrExtremizeResult, IrICStat, IrPartition,
  IrPortfolioDiagnosis, IrRegressionResult, IrSingleReport, IrStrategyResult, NewsDigestResult,
  PrescribeResult,
} from "../types";

// IrBuilder의 fmt 함수와 동일한 규칙 — % 지표는 toFixed(2)+"%" , 비율/정수는 그대로
function fmt(v: number | null | undefined, suf: string): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = suf === "%" ? v.toFixed(2) : (Number.isInteger(v) ? String(v) : v.toFixed(2));
  return `${n}${suf}`;
}

// 채팅 버블 안에서 보여줄 핵심 4지표
const CHAT_METRICS: [string, string, string][] = [
  ["cagr",         "CAGR",   "%"],
  ["sharpe",       "샤프",    ""],
  ["mdd",          "MDD",    "%"],
  ["total_return", "누적수익", "%"],
];

interface Props {
  result: Record<string, unknown>;
}

// inspect 원시 시계열 — 컬럼별 라인. recharts SVG는 CSS var 불가라 토큰값 직접 인라인(EquityChart와 동기).
const INSPECT_COLORS = ["#d4a738", "#1668c4", "#de3033", "#8b94a3", "#e0b958", "#2ea65a"];

const inspectFmt = (v: number) =>
  Math.abs(v) >= 1e4 ? Math.round(v).toLocaleString()
    : Math.abs(v) >= 1 ? v.toFixed(1) : v.toFixed(3);

// 컬럼 대표 크기(중앙값 절대값의 자릿수) — 이질 스케일 분리 기준(T5 #6·#9c).
function magOrder(vals: (number | null)[]): number {
  const abs = vals.filter((v): v is number => v != null && !Number.isNaN(v) && v !== 0).map(Math.abs);
  if (abs.length === 0) return 0;
  abs.sort((a, b) => a - b);
  return Math.round(Math.log10(abs[Math.floor(abs.length / 2)]));
}

// 크기 차가 ~100x(2자릿수) 넘는 컬럼은 별도 패널로 분리 — 한 축에 종가(~3만)+RSI(0~100)+거래량을
// 겹쳐 RSI·이동평균이 납작하게 깔려 안 보이던 단일축 한계(#6·#9c)를 닫는다.
function scaleGroups(columns: string[], series: Record<string, (number | null)[]>): string[][] {
  const ord = new Map(columns.map((c) => [c, magOrder(series[c] ?? [])] as const));
  const sorted = [...columns].sort((a, b) => ord.get(a)! - ord.get(b)!);
  const groups: string[][] = [];
  for (const c of sorted) {
    const g = groups[groups.length - 1];
    if (g && Math.abs(ord.get(g[0])! - ord.get(c)!) <= 1) g.push(c);   // ~10x 이내 → 같은 패널
    else groups.push([c]);
  }
  return groups;
}

// 한 스케일 패널 — 같은 자릿수대 컬럼만 한 축에. colorOf로 색을 원본 컬럼순서에 고정(범례 일관).
function InspectPanel({ data, columns, height, colorOf }: {
  data: Record<string, string | number | null>[];
  columns: string[]; height: number; colorOf: (c: string) => string;
}) {
  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 8 }}>
        <CartesianGrid stroke={chartC.grid} />
        <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={48} />
        <YAxis tick={{ fontSize: 10 }} width={52} domain={["auto", "auto"]}
               tickFormatter={(v) => inspectFmt(Number(v))} />
        <Tooltip formatter={(v) => (v == null ? "—" : inspectFmt(Number(v)))} />
        {columns.length > 1 && <Legend />}
        {columns.map((c) => (
          <Line key={c} type="monotone" dataKey={c} stroke={colorOf(c)}
                dot={false} strokeWidth={1.8} connectNulls isAnimationActive={false} />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}

function InspectChart({ result }: Props) {
  const symbol = (result.symbol as string) ?? "";
  const columns = (result.columns as string[]) ?? [];
  const dates = (result.dates as string[]) ?? [];
  const series = (result.series as Record<string, (number | null)[]>) ?? {};
  const data = dates.map((d, i) => {
    const row: Record<string, string | number | null> = { date: d };
    for (const c of columns) row[c] = series[c]?.[i] ?? null;
    return row;
  });
  const colorOf = (c: string) => INSPECT_COLORS[columns.indexOf(c) % INSPECT_COLORS.length];
  const groups = scaleGroups(columns, series);
  return (
    <div className="chat-result">
      <div className="muted" style={{ fontSize: "0.8em", marginBottom: 4 }}>
        {symbol} · {dates[0]}~{dates[dates.length - 1]} ({dates.length}일)
        {groups.length > 1 ? " · 스케일이 달라 패널 분리" : ""}
      </div>
      {groups.map((cols, gi) => (
        <InspectPanel key={gi} data={data} columns={cols} colorOf={colorOf}
                      height={groups.length > 1 ? 150 : 200} />
      ))}
    </div>
  );
}

// ── 펼침(parameter/asset/period_split/condition) — SweepChart + 버킷 표 + 유의성 ──
// IrBuilder ResultPanel(axis && buckets 분기, L1383-1427)을 chat 버블용 최소형으로 미러.
function SweepBuckets({ result }: { result: IrStrategyResult }) {
  const axis = result.axis!;
  const buckets = result.buckets!;
  const rows = Object.entries(buckets);
  const pairwise = result.compare?.pairwise ?? {};
  // 축 종류별 라벨 — 기간분할은 키 형태로 연/분기/월 판별(엔진 split_period 결과). 표 머리·부제 공용.
  const colLabel = axis === "parameter" ? "파라미터"
    : axis === "asset" ? "종목"
    : axis === "variant" ? "대안"
    : axis === "condition" ? "국면"
    : rows.every(([k]) => /^\d{4}$/.test(k)) ? "연도"
    : rows.every(([k]) => /^\d{4}Q\d$/.test(k)) ? "분기"
    : rows.every(([k]) => /^\d{4}-\d{2}$/.test(k)) ? "월" : "기간";
  return (
    <div className="chat-result">
      <div className="muted" style={{ fontSize: "0.8em", marginBottom: 4 }}>
        {colLabel}별 성과 (백테스트 손익)</div>
      <SweepChart axis={axis} buckets={buckets} axes={result.axes} />
      <div style={{ overflowX: "auto" }}>
        <table className="sweep-table">
          <thead><tr><th>{colLabel}</th><th>표본</th><th>누적(%)</th><th>CAGR(%)</th>
            <th>MDD(%)</th><th>샤프</th><th>소르티노</th><th>승률(%)</th><th>손익비</th></tr></thead>
          <tbody>
            {rows.map(([k, b]) => (
              b.error ? (
                <tr key={k}><td>{k}</td><td colSpan={8} className="neg">{b.error}</td></tr>
              ) : (
                <tr key={k}>
                  <td>{k}</td>
                  <td>{b.n ?? "—"}</td>
                  <td className={(b.cum_return ?? 0) >= 0 ? "pos" : "neg"}>{fmt(b.cum_return, "")}</td>
                  <td>{fmt(b.cagr, "")}</td>
                  <td className="neg">{fmt(b.mdd, "")}</td>
                  <td>{fmt(b.sharpe, "")}</td>
                  <td>{fmt(b.sortino, "")}</td>
                  <td>{fmt(b.win_rate, "")}</td>
                  <td>{fmt(b.payoff_ratio, "")}</td>
                </tr>
              )
            ))}
          </tbody>
        </table>
      </div>
      {Object.keys(pairwise).length ? (
        <div className="muted" style={{ fontSize: 12, marginTop: 8 }}>
          유의성(2표본 t): {Object.entries(pairwise).map(([k, v]) =>
            `${k} → p=${v.p_value != null ? v.p_value.toFixed(4) : "—"}` +
            (v.p_value != null && v.p_value < 0.05 ? " (유의)" : "")).join(" · ")}
        </div>
      ) : null}
      <StatNote keys={["sharpe", "sortino", "mdd", "win_rate", "payoff_ratio",
                       ...(Object.keys(pairwise).length ? ["p_value_2s"] : [])]} />
    </div>
  );
}

// ── 이벤트 스터디(axis="time") — EventStudyChart + 표 ──
// IrBuilder EventStudyPanel(L1546-1629)을 chat 버블용 최소형으로 미러(국면별 표는 생략 — 본문 길이).
function EventStudy({ result }: { result: IrStrategyResult }) {
  const windows = result.windows ?? [];
  const overall = (result.overall ?? {}) as Record<string, IrEventStat>;
  // 유의성은 색이 아니라 굵기로 — 양측검정이라 '유의'는 방향(상승)이 아니다. 평균이 음수인
  // 유의 결과를 상승색(pos)으로 칠하던 오도를 제거(방향=평균 부호 열이 담당).
  const pcell = (p?: number) => (
    <td style={p != null && p < 0.05 ? { fontWeight: 700 } : undefined}>
      {p != null ? p.toFixed(4) : "—"}</td>);
  const basisLabel = { close: "종가→종가", intraday: "시가→종가(당일)", excess: "시장초과" }[
    result.basis ?? "close"] ?? "종가→종가";
  // 음수 창(전조·WS1) 포함 여부 — 헤더 설명을 방향에 맞춘다(전조를 forward로 오독 방지).
  const hasPre = windows.some((w) => Number(w) < 0);
  const kindLabel = hasPre ? "전N일=이벤트 이전 구간 누적수익 · +N일=발생 후 forward — 손익 아님"
    : "forward 수익, 손익 아님";
  // T7 — 풀 구성 분해(종목 상위·연도범위)를 표면화해 'n=수천 무차별 pooling'(#4c)을 투명화.
  const comp = result.composition;
  const bySym = comp?.by_symbol ? Object.entries(comp.by_symbol) : [];
  const byYear = comp?.by_year ? Object.keys(comp.by_year) : [];
  return (
    <div className="chat-result">
      <div className="muted" style={{ fontSize: "0.8em", marginBottom: 4 }}>
        이벤트 분석 — 총 {result.n_events ?? 0}건 · 기준 {basisLabel} ({kindLabel})
      </div>
      {bySym.length > 0 && (
        <div className="muted" style={{ fontSize: "0.78em", marginBottom: 6 }}>
          구성: {bySym.slice(0, 5).map(([s, n]) => `${s} ${n}건`).join(" · ")}
          {bySym.length > 5 ? ` 외 ${bySym.length - 5}종목` : ""}
          {byYear.length > 0 ? ` · ${byYear[0]}~${byYear[byYear.length - 1]}` : ""}
        </div>
      )}
      <EventStudyChart windows={windows} overall={overall} />
      <div style={{ overflowX: "auto" }}>
        <table className="sweep-table">
          <thead><tr><th>윈도우(일)</th><th>표본</th><th>평균수익(%)</th>
            <th>MAE(%)</th><th>MFE(%)</th><th>양(+)확률(%)</th><th>손익비</th><th>p-value</th></tr></thead>
          <tbody>
            {windows.map((w) => {
              const o = overall[w] ?? ({} as IrEventStat);
              return (
                <tr key={w}>
                  <td>{w}</td><td>{o.n ?? "—"}</td>
                  <td className={(o.mean ?? 0) >= 0 ? "pos" : "neg"}>{fmt(o.mean, "")}</td>
                  <td className="neg">{fmt(o.mean_mae, "")}</td>
                  <td className="pos">{fmt(o.mean_mfe, "")}</td>
                  <td>{fmt(o.prob_positive, "")}</td>
                  <td>{fmt(o.payoff_ratio, "")}</td>
                  {pcell(o.p_value)}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <StatNote keys={["p_value", "mean_mae", "mean_mfe", "prob_positive", "payoff_ratio"]} />
    </div>
  );
}

// ── 종목 코호트(#A P1: 다종목 동일 이벤트스터디 per-symbol 비교) — 종목×윈도우 표 ──
// pool(합쳐 하나의 통계) 아닌 종목별 개별 비교. 프로덕션 실측: 다종목 이벤트스터디가 종목당 1콜로
// 팬아웃하던 것을 1콜 코호트로 — 각 종목의 유의성을 나란히 보여 "어느 종목에 통하나"를 한눈에.
function CohortComparison({ result }: { result: IrStrategyResult }) {
  const windows = result.windows ?? [];
  const buckets = (result.buckets ?? {}) as Record<string, {
    name?: string; n_events?: number; overall?: Record<string, IrEventStat>; error?: string }>;
  const syms = Object.keys(buckets);
  const nSym = (result as { n_symbols?: number }).n_symbols ?? syms.length;
  // WS2 — 행축이 종목(entity)뿐 아니라 파라미터(이벤트×격자)·조건(variant 대안)일 수 있다.
  const rowAxis = (result as { row_axis?: string }).row_axis ?? "종목";
  const label = (s: string) => {           // 종목명(코드) — 코드만 뜨던 것 식별 보강(#C P2)
    const nm = buckets[s]?.name;
    return nm && nm !== s ? `${nm} (${s})` : s;
  };
  return (
    <div className="chat-result">
      <div className="muted" style={{ fontSize: "0.8em", marginBottom: 4 }}>
        {rowAxis} 코호트 — {nSym}개 {rowAxis}별 이벤트스터디 비교 (수익 분석, 손익 아님 · 색=방향, 굵게=p&lt;0.05 유의)
      </div>
      <div style={{ overflowX: "auto" }}>
        <table className="sweep-table">
          <thead><tr><th>{rowAxis}</th><th>표본</th>
            {windows.map((w) => (
              <th key={w}>{Number(w) < 0 ? `전${-Number(w)}일` : `+${w}일`} 평균%(p)</th>
            ))}</tr></thead>
          <tbody>
            {syms.map((s) => {
              const b = buckets[s] ?? {};
              if (b.error) return (
                <tr key={s}><td>{label(s)}</td>
                  <td colSpan={windows.length + 1} className="muted">실행 실패</td></tr>);
              const ov = (b.overall ?? {}) as Record<string, IrEventStat>;
              return (
                <tr key={s}>
                  <td>{label(s)}</td><td>{b.n_events ?? "—"}</td>
                  {windows.map((w) => {
                    const o = ov[w] ?? ({} as IrEventStat);
                    const sig = o.p_value != null && o.p_value < 0.05;
                    // 색=방향(평균 부호)·굵기=유의성 분리 — 양측검정이라 '유의'가 상승을
                    // 뜻하지 않는다(음수 유의를 상승색으로 칠하던 오도 제거).
                    return (
                      <td key={w} className={(o.mean ?? 0) >= 0 ? "pos" : "neg"}
                          style={sig ? { fontWeight: 700 } : undefined}>
                        {fmt(o.mean, "")}
                        <span className="muted" style={{ fontSize: "0.85em" }}>
                          {" "}(p{o.p_value != null ? o.p_value.toFixed(3) : "—"})</span>
                      </td>);
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <StatNote keys={["p_value"]} />
    </div>
  );
}

// ── analyze_event(전조·후행 이벤트 스터디) — 후보 지표 × 윈도우 통일 표 + 막대그래프 ──
// 여러 후보 이벤트(거래량·수급·RSI 등)를 결정적으로 조립·실행한 다중 결과(tool="analyze_event").
// 통일 규격: 지표별 행 × 윈도우 열(전N일=전조 구간수익·+N일=forward 수익) + 대표 지표 막대그래프.
type AeStat = { mean_pct?: number | null; p_value?: number | null; n?: number | null };
type AeRow = {
  label?: string; event?: { ref?: string; op?: string; value?: number };
  success?: boolean; n_events?: number | null; error?: string;
  stats?: Record<string, AeStat>;
  hit_rate?: { threshold_pct?: number; by_window?: Record<string, { prob_pct?: number; n?: number }> };
};
type AeResult = { market?: string; windows?: number[]; event_basis?: string;
  lookback_years?: number; results?: AeRow[] };

function AnalyzeEventView({ result }: { result: IrStrategyResult }) {
  const ae = result as unknown as AeResult;
  const rows = ae.results ?? [];
  const wins = (ae.windows ?? []).map(Number);
  const posWins = wins.filter((w) => w > 0).sort((a, b) => b - a);
  const primW = posWins.length ? posWins[0] : [...wins].sort((a, b) => a - b)[0];  // 대표: 최대 forward, 없으면 최전조
  const hasHit = rows.some((r) => r.hit_rate?.by_window);
  const thr = rows.find((r) => r.hit_rate)?.hit_rate?.threshold_pct;
  const ok = rows.filter((r) => r.success);
  const wlabel = (w: number) => (w < 0 ? `전${-w}일` : `+${w}일`);

  // 대표 막대그래프 — 지표별 대표값(급등확률 있으면 hit-rate, 없으면 대표 윈도우 평균수익%).
  const useHit = hasHit && primW > 0;
  const chart = ok.map((r) => {
    const hv = r.hit_rate?.by_window?.[String(primW)]?.prob_pct;
    const mv = r.stats?.[String(primW)]?.mean_pct;
    return { label: r.label ?? r.event?.ref ?? "", value: useHit ? (hv ?? null) : (mv ?? null) };
  }).filter((d) => d.value != null);
  const chartTitle = useHit
    ? `이벤트 후 +${primW}일 내 ≥${thr}% 급등 확률(%) — 지표별`
    : `${wlabel(primW)} 평균수익(%) — 지표별`;

  return (
    <div className="chat-result">
      <div className="muted" style={{ fontSize: "0.8em", marginBottom: 6 }}>
        이벤트 스터디 — {ae.market ?? "전체"} · 최근 {ae.lookback_years ?? 5}년 · 후보 {rows.length}개 지표
        (전N일=이벤트 직전 구간 누적수익 · +N일=이벤트 후 forward 수익 · 손익 아님)
      </div>

      {chart.length > 0 && (
        <>
          <div className="muted" style={{ fontSize: "0.78em", margin: "2px 0 2px" }}>{chartTitle}</div>
          <ResponsiveContainer width="100%" height={Math.max(180, chart.length * 34 + 40)}>
            <BarChart data={chart} layout="vertical" margin={{ top: 4, right: 24, bottom: 4, left: 8 }}>
              <CartesianGrid stroke={chartC.grid} horizontal={false} />
              <XAxis type="number" tick={{ fontSize: 11 }}
                domain={useHit ? [0, "dataMax"] : ["dataMin", "dataMax"]} />
              <YAxis type="category" dataKey="label" tick={{ fontSize: 11 }} width={116} />
              <Tooltip formatter={(v) => `${Number(Array.isArray(v) ? v[0] : (v ?? 0)).toFixed(1)}%`}
                contentStyle={{ background: chartC.panel, border: `1px solid ${chartC.grid}`, fontSize: 12 }} />
              {!useHit && <ReferenceLine x={0} stroke={chartC.muted} strokeDasharray="3 3" />}
              <Bar dataKey="value" isAnimationActive={false}>
                {chart.map((d, i) => (
                  <Cell key={i} fill={useHit ? chartC.accent : ((d.value ?? 0) >= 0 ? chartC.up : chartC.down)} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </>
      )}

      <div style={{ overflowX: "auto" }}>
        <table className="sweep-table">
          <thead><tr>
            <th>지표 (이벤트 조건)</th><th>표본</th>
            {wins.map((w) => <th key={w}>{wlabel(w)} 평균%(p)</th>)}
            {hasHit && <th>급등확률 (+{posWins[0] ?? primW}일 ≥{thr}%)</th>}
          </tr></thead>
          <tbody>
            {rows.map((r, ri) => {
              const cond = r.event ? `${r.event.ref} ${r.event.op} ${r.event.value}` : "";
              if (!r.success) return (
                <tr key={ri}><td>{r.label ?? cond}</td>
                  <td colSpan={wins.length + 1 + (hasHit ? 1 : 0)} className="muted">
                    {r.error ?? "실행 실패"}</td></tr>);
              const hw = posWins[0] ?? primW;
              const hit = r.hit_rate?.by_window?.[String(hw)]?.prob_pct;
              return (
                <tr key={ri}>
                  <td>{r.label ?? cond}
                    {r.label && cond && <span className="muted" style={{ fontSize: "0.85em" }}> · {cond}</span>}</td>
                  <td>{r.n_events ?? "—"}</td>
                  {wins.map((w) => {
                    const o = r.stats?.[String(w)] ?? {};
                    const sig = o.p_value != null && o.p_value < 0.05;
                    return (
                      <td key={w} className={(o.mean_pct ?? 0) >= 0 ? "pos" : "neg"}
                          style={sig ? { fontWeight: 700 } : undefined}>
                        {fmt(o.mean_pct, "")}
                        <span className="muted" style={{ fontSize: "0.85em" }}>
                          {" "}(p{o.p_value != null ? o.p_value.toFixed(3) : "—"})</span>
                      </td>);
                  })}
                  {hasHit && <td>{hit != null ? `${hit.toFixed(1)}%` : "—"}</td>}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <StatNote keys={["p_value"]} />
    </div>
  );
}

// ── 신호값 분포(axis="signal") — SignalDistChart + 표 ──
// IrBuilder SignalStudyPanel(L1459-1508)을 chat 버블용 최소형으로 미러.
function SignalStudy({ result }: { result: IrStrategyResult }) {
  const dist = (result.overall ?? {}) as IrDistribution;
  // by_regime 결과 키는 타입상 이벤트 스터디 shape이라 IrBuilder처럼 partition으로 캐스트(표시 전용).
  const reg = result.by_regime as unknown as IrPartition | null | undefined;
  const q = dist.quantiles ?? {};
  return (
    <div className="chat-result">
      <div className="muted" style={{ fontSize: "0.8em", marginBottom: 4 }}>
        신호값 분포 — 표본 {dist.n ?? 0}개 (신호 자체의 값 분포, 손익 아님)
      </div>
      <SignalDistChart overall={dist} byRegime={reg} />
      <div className="chat-result-metrics">
        <div className="chat-result-stat"><div className="chat-result-label">평균</div>
          <div className="chat-result-value">{fmt(dist.mean, "")}</div></div>
        <div className="chat-result-stat"><div className="chat-result-label">중앙값</div>
          <div className="chat-result-value">{fmt(q.q50, "")}</div></div>
        <div className="chat-result-stat"><div className="chat-result-label">하위5%</div>
          <div className="chat-result-value neg">{fmt(q.q05, "")}</div></div>
        <div className="chat-result-stat"><div className="chat-result-label">상위5%</div>
          <div className="chat-result-value pos">{fmt(q.q95, "")}</div></div>
      </div>
      <StatNote keys={["quantiles"]} />
    </div>
  );
}

// ── 횡단 IC(axis="relation", relation!=="regression") — ICChart + 표 ──
// IrBuilder ICStudyPanel(L1510-1544)을 chat 버블용 최소형으로 미러.
function ICStudy({ result }: { result: IrStrategyResult }) {
  const windows = result.windows ?? [];
  const byWindow = result.by_window ?? {};
  return (
    <div className="chat-result">
      <div className="muted" style={{ fontSize: "0.8em", marginBottom: 4 }}>
        팩터 IC — forward 수익 예측력 (분석 전용 · p&lt;0.05면 유의)
      </div>
      <ICChart windows={windows} byWindow={byWindow} />
      <div style={{ overflowX: "auto" }}>
        <table className="sweep-table">
          <thead><tr><th>horizon(일)</th><th>표본</th><th>평균 IC</th><th>IR</th>
            <th>t</th><th>양(+)확률(%)</th><th>p-value</th></tr></thead>
          <tbody>
            {windows.map((w) => {
              const o = byWindow[w]?.overall ?? ({} as IrICStat);
              return (
                <tr key={w}>
                  <td>{w}</td><td>{o.n ?? "—"}</td>
                  <td className={(o.mean ?? 0) >= 0 ? "pos" : "neg"}>
                    {o.mean != null ? o.mean.toFixed(4) : "—"}</td>
                  <td>{o.ir != null ? o.ir.toFixed(3) : "—"}</td>
                  <td>{o.t_stat != null ? o.t_stat.toFixed(2) : "—"}</td>
                  <td>{fmt(o.prob_positive, "")}</td>
                  <td style={o.p_value != null && o.p_value < 0.05 ? { fontWeight: 700 } : undefined}>
                    {o.p_value != null ? o.p_value.toFixed(4) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <StatNote keys={["ic", "ic_ir", "t_stat", "p_value", "prob_positive"]} />
    </div>
  );
}

// 엑셀 증빙(라이브 수식)을 지원하는 형상 — 백테스트·분석만. 시각화 전용 신형상(상관 히트맵·
// 트리맵·breadth 등)은 수식 증빙 대상이 아니라 버튼을 숨긴다(build_strategy_excel 미지원 형상).
const EXCEL_SHAPES = new Set([
  "simulate", "select", "describe_single", "describe_portfolio",
  "extremize", "sweep", "relate_ic", "relate_regression", "event_study", "signal_dist",
]);

// 자동매매 연동 대상 — **실행 가능한 단일 전략**만(백테스트·최적화 승자). 이벤트스터디·스크린·
// 비교 등 분석은 매매 전략이 아니라 제외. simulate=result.ir이 곧 전략, extremize=result.best.ir.
const AUTOTRADE_SHAPES = new Set(["simulate", "extremize"]);

// P4 맥락 카드 — 사이드카(준실시간 시세·뉴스)를 형상 렌더러와 **직교**하게 결과 아래 표시.
// context 없으면(엔진 단독·다른 형상) 렌더 안 함. 시세 등락은 한국식 방향색(상승=빨강·하락=파랑).
function ContextCard({ context }: { context?: IrStrategyResult["context"] }) {
  if (!context) return null;
  const quotes = Object.entries(context.quotes ?? {});
  const market = Object.entries(context.market ?? {});
  const news = context.news ?? [];
  if (quotes.length === 0 && market.length === 0 && news.length === 0) return null;
  return (
    <div className="chat-context">
      {market.length > 0 && (
        <div className="chat-context-quotes">
          <span className="chat-context-label">시장</span>
          {market.map(([name, m]) => (
            <span key={name} className="chat-context-quote">
              {name} {m.price != null ? m.price.toLocaleString() : "—"}
              {m.chg != null && (
                <span className={m.chg >= 0 ? "pos" : "neg"}>
                  {" "}{m.chg >= 0 ? "+" : ""}{m.chg.toFixed(2)}%
                </span>
              )}
            </span>
          ))}
        </div>
      )}
      {quotes.length > 0 && (
        <div className="chat-context-quotes">
          <span className="chat-context-label">준실시간</span>
          {quotes.slice(0, 8).map(([code, q]) => (
            <span key={code} className="chat-context-quote">
              {code} {q.price != null ? q.price.toLocaleString() : "—"}
              {q.chg != null && (
                <span className={q.chg >= 0 ? "pos" : "neg"}>
                  {" "}{q.chg >= 0 ? "+" : ""}{q.chg.toFixed(2)}%
                </span>
              )}
            </span>
          ))}
        </div>
      )}
      {news.length > 0 && (
        <div className="chat-context-news">
          <span className="chat-context-label">최근뉴스</span>
          <ul>
            {news.slice(0, 5).map((n, i) => (
              <li key={i}>
                <a href={n.link} target="_blank" rel="noopener noreferrer">{n.title}</a>
              </li>
            ))}
          </ul>
        </div>
      )}
      {context.source && <div className="chat-context-src muted">{context.source}</div>}
    </div>
  );
}

export default function ChatResultView({ result, hideMethodology }: Props & { hideMethodology?: boolean }) {
  // 결과가 IR을 들고 오면(=엔진 분석) 결과 아래에 '엑셀로 내보내기'(증빙)와 '변수 조정'(실시간
  // 재실행) 도구를 붙인다. inspect(원시 dump)·저장 카드 등 IR 없는 결과엔 미노출.
  const ir0 = (result as { ir?: Record<string, unknown> }).ir;
  const manifest = (result as { adjustable?: AdjustableParam[] }).adjustable;
  // 변수 조정 재실행 시 표시 결과·IR을 교체(원본 챗 메시지는 불변 — 로컬 상태로 미리보기).
  const [live, setLive] = useState<{ ir: Record<string, unknown> | undefined; result: Record<string, unknown> }>(
    { ir: ir0, result },
  );
  // 엑셀 버튼은 증빙 지원 형상에만 — 시각화 전용 형상(상관 히트맵 등)은 숨김.
  const shape = (live.result as { shape?: string }).shape
    ?? deriveShape(live.result as unknown as IrStrategyResult);
  // 자동매매 연동 대상 IR — simulate=result.ir(전략 그 자체), extremize=best.ir(승자 단일전략).
  // 퇴화(불가능 손실 등) 결과는 실돈 연동 부적합이라 버튼 숨김(infeasible은 ir 부재라 자동 숨김).
  const status = (live.result as { status?: string }).status;
  const tradableIr = shape === "extremize"
    ? (live.result as { best?: { ir?: Record<string, unknown> } }).best?.ir
    : (live.ir ?? ir0);
  // 합성(WS3·components)은 백테스트 전용 — 서버 승격 게이트(422)와 정합해 버튼 자체를 숨긴다.
  const isComposite = !!(tradableIr as { components?: unknown[] } | undefined)?.components?.length;
  const canLink = AUTOTRADE_SHAPES.has(shape) && status !== "degenerate" && !!tradableIr
    && !isComposite;
  // 테마 전환(다크↔라이트, PDF 화이트 인쇄) 시 결과 본문을 remount해 recharts가 라이브 팔레트(C)를
  // 새로 읽게 한다 — 차트 색이 CSS var를 못 받아 렌더 시점에만 반영되므로 key 교체로 강제 재렌더.
  const theme = useThemeName();
  return (
    <>
      <ChatResultBody key={theme} result={live.result} hideMethodology={hideMethodology} />
      <ContextCard context={(live.result as unknown as IrStrategyResult).context} />
      {ir0 && EXCEL_SHAPES.has(shape) && <ExcelExportButton ir={live.ir ?? ir0} />}
      {canLink && <AutotradeLinkButton ir={tradableIr as Record<string, unknown>} />}
      {ir0 && manifest && manifest.length > 0 && (
        <ParamControls baseIr={ir0} manifest={manifest}
          onRun={(ir, res) => setLive({ ir, result: res })} />
      )}
    </>
  );
}

// ── simulate: 지표행 + 자산곡선 (axis 없는 단일 백테스트) ─────────────────────
function SimulateChart({ result }: Props) {
  const r = result as unknown as IrStrategyResult;
  const m = r.metrics ?? {};
  return (
    <div className="chat-result">
      <div className="chat-result-metrics">
        {CHAT_METRICS.map(([key, label, suf]) => {
          const v = m[key] as number | null | undefined;
          const pol =
            key === "cagr" || key === "total_return"
              ? typeof v === "number" ? (v >= 0 ? "pos" : "neg") : ""
              : key === "mdd" ? "neg" : "";
          return (
            <div className="chat-result-stat" key={key}>
              <div className="chat-result-label">{label}</div>
              <div className={`chat-result-value ${pol}`}>{fmt(v, suf)}</div>
            </div>
          );
        })}
      </div>
      <EquityChart equity={r.equity ?? []} benchmark={r.benchmark} trades={r.trades} />
      {(() => {
        // 적립식(WS4) — 납입 요약(지표=TWR·원금대비 분리). contributions 없으면 렌더 0.
        const ct = (result as { contributions?: { n?: number; total?: number;
          total_invested?: number; final_value?: number; profit_pct?: number } }).contributions;
        if (!ct) return null;
        return (
          <div className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            적립식 — 납입 {ct.n ?? "—"}회 · 누계 {ct.total != null ? ct.total.toLocaleString() : "—"}원
            · 평가액 {ct.final_value != null ? Math.round(ct.final_value).toLocaleString() : "—"}원
            · <b>원금대비 {ct.profit_pct != null ? `${ct.profit_pct.toFixed(1)}%` : "—"}</b>
            {" "}(위 지표는 납입 왜곡을 제거한 시간가중 TWR)
          </div>
        );
      })()}
      {(() => {
        // 전략 합성(WS3) — 구성별 분해 표(비중·성과). components 없으면 렌더 0.
        const comps = (result as { components?: Record<string, {
          weight?: number; cum_return?: number; cagr?: number; sharpe?: number;
          mdd?: number; error?: string }> }).components;
        if (!comps || !Object.keys(comps).length) return null;
        return (
          <div style={{ overflowX: "auto", marginTop: 8 }}>
            <table className="sweep-table">
              <thead><tr><th>구성</th><th>비중</th><th>누적(%)</th><th>CAGR(%)</th>
                <th>샤프</th><th>MDD(%)</th></tr></thead>
              <tbody>
                {Object.entries(comps).map(([k, b]) => (
                  b.error ? (
                    <tr key={k}><td>{k}</td><td colSpan={5} className="neg">{b.error}</td></tr>
                  ) : (
                    <tr key={k}>
                      <td>{k}</td>
                      <td>{b.weight != null ? `${(b.weight * 100).toFixed(0)}%` : "—"}</td>
                      <td className={(b.cum_return ?? 0) >= 0 ? "pos" : "neg"}>{fmt(b.cum_return, "")}</td>
                      <td>{fmt(b.cagr, "")}</td>
                      <td>{fmt(b.sharpe, "")}</td>
                      <td className="neg">{fmt(b.mdd, "")}</td>
                    </tr>
                  )
                ))}
              </tbody>
            </table>
          </div>
        );
      })()}
      <StatNote keys={["cagr", "sharpe", "mdd", "win_rate"]} />
    </div>
  );
}

// 형상 폴백 판별 — 1차는 항상 엔진 스탬프(result.shape). 미스탬프 결과만 여기서 재추론하며
// summarize.result_shape와 **동일 순서**를 유지한다(행동보존). axis/buckets를 equity보다 앞에.
function deriveShape(r: IrStrategyResult): string {
  if (r.query === "select") return "select";
  if (r.report === "single") return "describe_single";
  if (r.report === "portfolio") return "describe_portfolio";
  if (r.reduction === "extremize") return "extremize";
  if (r.axis === "relation") return r.relation === "regression" ? "relate_regression" : "relate_ic";
  if (r.axis === "time") return "event_study";
  if (r.axis === "signal") return "signal_dist";
  if (r.axis && r.buckets) return "sweep";
  if ((r as { query?: string }).query === "inspect") return "inspect";
  if (r.equity && r.equity.length > 0) return "simulate";
  return "unknown";
}

// shape → 렌더러 레지스트리. 새 형상 = 여기 1건 등록(+ 엔진 스탬프 + summarize 케이스)으로 완결.
// 순서 무관(키 조회) — equity-last 같은 순서의존 버그 부류가 구조적으로 사라진다.
const RENDERERS: Record<string, (result: Record<string, unknown>) => ReactElement> = {
  select: (result) => {
    const r = result as unknown as IrStrategyResult;
    return (
      <div className="chat-result">
        <RankedListChart results={r.results ?? []} columns={r.columns} scoring={r.scoring}
          groups={r.groups} as_of={r.as_of}
          universe_size={r.universe_size} eligible_size={r.eligible_size} />
      </div>
    );
  },
  describe_single: (result) => {
    // 추정실적은 웹 직접 경로=top-level(r.estimates), 챗 경로=사이드카(context.estimates).
    // 둘 중 있는 것을 ReportCards에 합쳐 360 리포트 안에서 표면화(뿌리①).
    const r = result as unknown as IrSingleReport;
    const est = r.estimates ?? (result as unknown as IrStrategyResult).context?.estimates;
    return <div className="chat-result"><ReportCards r={est ? { ...r, estimates: est } : r} /></div>;
  },
  describe_portfolio: (result) => (
    <div className="chat-result"><DiagnosisPanel r={result as unknown as IrPortfolioDiagnosis} /></div>
  ),
  extremize: (result) => (
    <div className="chat-result"><ExtremizeChart r={result as unknown as IrExtremizeResult} /></div>
  ),
  relate_regression: (result) => (
    <div className="chat-result"><RegressionChart r={result as unknown as IrRegressionResult} /></div>
  ),
  relate_ic: (result) => <ICStudy result={result as unknown as IrStrategyResult} />,
  prescribe: (result) => (
    <div className="chat-result"><PrescribePanel r={result as unknown as PrescribeResult} /></div>
  ),
  breadth: (result) => (
    <div className="chat-result"><BreadthPanel r={result as unknown as BreadthResult} /></div>
  ),
  news_research: (result) => (
    <div className="chat-result"><NewsDigest r={result as unknown as NewsDigestResult} /></div>
  ),
  correlation_matrix: (result) => {
    const r = result as unknown as {
      symbols?: string[]; matrix?: (number | null)[][]; avg_corr?: number | null; n_obs?: number;
    };
    return (
      <div className="chat-result">
        <div className="muted" style={{ fontSize: "0.8em", marginBottom: 4 }}>
          상관행렬 — {r.symbols?.length ?? 0}종목 일별수익 (n={r.n_obs ?? "?"}일) · 평균 상관{" "}
          {r.avg_corr != null ? r.avg_corr.toFixed(3) : "—"}
        </div>
        <CorrelationHeatmap symbols={r.symbols ?? []} matrix={r.matrix ?? []} />
      </div>
    );
  },
  heatmap: (result) => (
    <div className="chat-result"><HeatmapPanel r={result as unknown as HeatmapResult} /></div>
  ),
  event_study: (result) => <EventStudy result={result as unknown as IrStrategyResult} />,
  cohort: (result) => <CohortComparison result={result as unknown as IrStrategyResult} />,
  signal_dist: (result) => <SignalStudy result={result as unknown as IrStrategyResult} />,
  sweep: (result) => <SweepBuckets result={result as unknown as IrStrategyResult} />,
  inspect: (result) => <InspectChart result={result} />,
  simulate: (result) => <SimulateChart result={result} />,
};

// 결과 품질 계약(chat-reliability §3) — 빈/퇴화/불가 결과를 가짜 "분석 완료" 대신 경고 배너로.
// status·verdict는 엔진(run_query)이 스탬프. 일반 div라 DESIGN.md CSS var 토큰 사용(SVG 아님).
const STATUS_LABEL: Record<string, string> = {
  empty: "결과 없음", degenerate: "신뢰 불가", data_insufficient: "데이터 부족", infeasible: "실행 불가",
};
function StatusBanner({ status, verdict }: { status: string; verdict?: string }) {
  const danger = status === "degenerate" || status === "infeasible";
  // status=ok + verdict = 정직한 caveat(레짐 편중·저신뢰 표본 등) — 경고(빈/퇴화) 아닌 정보성.
  // 골드 accent로 red/amber 경고와 구별한다(DESIGN.md 브랜드 accent).
  const info = status === "ok";
  const tone = danger ? "--danger" : info ? "--accent" : "--amber";
  const soft = danger ? "--red-soft" : info ? "--accent-soft" : "--amber-soft";
  return (
    <div style={{
      border: `1px solid var(${tone})`,
      background: `var(${soft})`,
      borderRadius: 8, padding: "8px 11px", marginBottom: 8, fontSize: 13, lineHeight: 1.5,
    }}>
      <span style={{ color: `var(${tone})`, fontWeight: 700 }}>
        {info ? "📌 참고" : `⚠ ${STATUS_LABEL[status] ?? status}`}
      </span>
      {verdict ? <span style={{ color: "var(--text)", marginLeft: 6 }}>{verdict}</span> : null}
    </div>
  );
}

// Wave 2 T2 — 분석 방법(provenance.ir_summary) 패널. 사용자가 백테스트 로직(기간·기준자본·종목·
// 방향·사이징·진입/청산·체결가정)을 보고 결과를 직접 검증하게 한다(증상 #7·#1). 사실 출처는 core
// execution_summary 단일(서버 attach_methodology가 붙임 — TS에 가정값 중복 없음). DESIGN var 토큰.
export function MethodologyPanel({ m, open }: { m: NonNullable<IrStrategyResult["methodology"]>; open?: boolean }) {
  const items = [...(m.confirmed ?? []), ...(m.assumed ?? [])];
  const cap = m.initial_capital != null ? Math.round(m.initial_capital).toLocaleString() : null;
  const head = [m.period ? `기간 ${m.period}` : null, cap ? `기준자본 ${cap}원` : null]
    .filter(Boolean).join(" · ");
  if (items.length === 0 && !head && !m.data_source) return null;
  return (
    <details className="chat-methodology" open={open} style={{
      border: "1px solid var(--border)", background: "var(--panel)",
      borderRadius: 8, padding: "6px 10px", marginBottom: 8, fontSize: 12.5,
    }}>
      <summary style={{ cursor: "pointer", color: "var(--accent-strong)", fontWeight: 600 }}>
        분석 방법{head ? ` · ${head}` : ""}
      </summary>
      <div style={{
        display: "grid", gridTemplateColumns: "auto 1fr", gap: "3px 10px",
        marginTop: 6, color: "var(--text)",
      }}>
        {/* 데이터 출처 — 데이터 기반 답변의 원천 표면화(서버 populated). 없으면 렌더 안 함. */}
        {m.data_source && (
          <Fragment>
            <span style={{ color: "var(--muted)" }}>데이터 출처</span>
            <span>{m.data_source}</span>
          </Fragment>
        )}
        {items.map((it, i) => (
          <Fragment key={i}>
            <span style={{ color: "var(--muted)" }}>{it.label}</span>
            <span>{it.value}</span>
          </Fragment>
        ))}
      </div>
    </details>
  );
}

function ChatResultBody({ result, hideMethodology }: Props & { hideMethodology?: boolean }) {
  const r = result as unknown as IrStrategyResult;

  // save_strategy: 저장 완료 카드 (엔진 형상이 아닌 상태 결과 — 레지스트리 밖 선처리)
  const savedId = (result as { strategy_id?: number }).strategy_id;
  if (r.success !== false && typeof savedId === "number") {
    const name = (result as { name?: string }).name ?? "전략";
    return (
      <div className="chat-result">
        <div className="chat-tool done">✅ '{name}' 전략을 초안으로 저장했어요</div>
        <div className="muted" style={{ marginTop: 6, fontSize: "0.85em" }}>
          모의·실전 실행은 <Link to={`/strategies/${savedId}`}>내 전략</Link>에서 진행하세요.
        </div>
      </div>
    );
  }

  // 결과 품질 계약 — status≠ok면 경고 배너(빈/퇴화/불가). status=ok여도 verdict가 있으면
  // 정보성 caveat 배너(P3-a 레짐 편중·저신뢰 표본) — 함정 경고가 챗 텍스트뿐 아니라 결과 카드
  // (숫자가 있는 그 자리)에도 닿게 한다. 배너는 차트 위에 얹어 맥락 보존.
  const status = (r as { status?: string }).status;
  const verdict = (r as { verdict?: string }).verdict;
  const banner = status && status !== "ok"
    ? <StatusBanner status={status} verdict={verdict} />
    : verdict
      ? <StatusBanner status="ok" verdict={verdict} />
      : null;

  // 결과 경고(데이터 품질·교차달력 carry-forward 등)를 **전 shape에서** 표면화한다. 이전엔
  // SweepBuckets에만 있어 백테스트/이벤트/관계분석의 warnings가 데이터엔 있어도 화면에 안 떴다
  // (예: 교차달력 '전일값 유지' 경고 미도달). status와 별개 — status=ok여도 경고는 보여준다.
  const warns = (r as { warnings?: { message?: string }[] }).warnings;
  const warnBanner = warns?.length
    ? <div className="warn-banner">⚠ {warns.map((w) => w.message).filter(Boolean).join(" · ")}</div>
    : null;

  // T2 자기서술 — 백테스트면 방법론 패널(배너 다음·차트 위). 서버가 backtest 결과에만 붙인다.
  const method = !hideMethodology && r.methodology ? <MethodologyPanel m={r.methodology} /> : null;

  // analyze_event(다중 후보 이벤트 스터디) — 엔진 shape 밖 도구 결과라 레지스트리 前 선처리
  // (통일 표+막대그래프로 렌더 — 미처리 시 "분석 완료" 폴백 칩으로 떨어지던 것을 대체).
  if ((r as { tool?: string }).tool === "analyze_event") {
    return <>{banner}{warnBanner}<AnalyzeEventView result={r} /></>;
  }

  // 형상 레지스트리 단일 조회 — 엔진 스탬프(result.shape) 우선, 미스탬프는 deriveShape 폴백.
  const shape = (r as { shape?: string }).shape ?? deriveShape(r);
  const renderer = RENDERERS[shape];
  if (renderer) return <>{banner}{warnBanner}{method}{renderer(result)}</>;

  // fallback: 오류 메시지 or 완료 칩. 배너가 있으면(비ok) 가짜 "분석 완료"를 보이지 않는다.
  if (r.success === false && r.error) {
    return (
      <div className="chat-result-fallback">
        {banner ?? <div className="chat-tool done">✓ 분석 완료</div>}
        <div className="chat-result-error">{r.error}</div>
      </div>
    );
  }
  return banner ?? <div className="chat-tool done">✓ 분석 완료</div>;
}

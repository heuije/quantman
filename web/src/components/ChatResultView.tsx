/**
 * ChatResultView — tool_result 인라인 차트 렌더러 (P1a)
 *
 * 라우팅:
 *   save_strategy (r.strategy_id)  → 저장 완료 카드 + 내 전략 링크 (P2)
 *   describe (r.report==="single") → ReportCards 단일종목 360 (P3)
 *   inspect  (r.query==="inspect") → 원시 시계열 라인차트 (P3)
 *   select  (r.query === "select") → RankedListChart
 *   simulate (r.equity?.length)    → 지표행 + EquityChart
 *   fallback                       → 칩 + 오류 메시지(있을 때)
 *
 * IrBuilder의 ResultPanel 로직을 참조하되 chat 버블 안에서 쓰기 적합한
 * 최소형으로 구현한다. ResultPanel / IrBuilder는 수정하지 않는다.
 */

import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { Link } from "react-router-dom";
import EquityChart from "./EquityChart";
import { RankedListChart, ReportCards } from "./ResultCharts";
import type { IrSingleReport, IrStrategyResult } from "../types";

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
const INSPECT_COLORS = ["#d4a738", "#1668c4", "#de3033", "#8b94a3"];

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
  const fmtNum = (v: number) =>
    Math.abs(v) >= 1e4 ? Math.round(v).toLocaleString()
      : Math.abs(v) >= 1 ? v.toFixed(1) : v.toFixed(3);
  return (
    <div className="chat-result">
      <div className="muted" style={{ fontSize: "0.8em", marginBottom: 4 }}>
        {symbol} · {dates[0]}~{dates[dates.length - 1]} ({dates.length}일)
      </div>
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 8 }}>
          <CartesianGrid stroke="#2b323e" />
          <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={48} />
          <YAxis tick={{ fontSize: 10 }} width={52} domain={["auto", "auto"]}
                 tickFormatter={(v) => fmtNum(Number(v))} />
          <Tooltip formatter={(v) => (v == null ? "—" : fmtNum(Number(v)))} />
          {columns.length > 1 && <Legend />}
          {columns.map((c, i) => (
            <Line key={c} type="monotone" dataKey={c}
                  stroke={INSPECT_COLORS[i % INSPECT_COLORS.length]}
                  dot={false} strokeWidth={1.8} connectNulls isAnimationActive={false} />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export default function ChatResultView({ result }: Props) {
  const r = result as unknown as IrStrategyResult;

  // ── save_strategy: 저장 완료 카드 ──────────────────────────────────────────
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

  // ── describe: 단일종목 360 리포트 ──────────────────────────────────────────
  if (r.report === "single") {
    return (
      <div className="chat-result">
        <ReportCards r={result as unknown as IrSingleReport} />
      </div>
    );
  }

  // ── inspect: 원시 시계열 라인차트 ──────────────────────────────────────────
  if ((result as { query?: string }).query === "inspect") {
    return <InspectChart result={result} />;
  }

  // ── select: 랭킹 선별 ──────────────────────────────────────────────────────
  if (r.query === "select") {
    return (
      <div className="chat-result">
        <RankedListChart
          results={r.results ?? []}
          as_of={r.as_of}
          universe_size={r.universe_size}
          eligible_size={r.eligible_size}
        />
      </div>
    );
  }

  // ── simulate: 지표행 + 자산곡선 ────────────────────────────────────────────
  if (r.equity && r.equity.length > 0) {
    const m = r.metrics ?? {};
    return (
      <div className="chat-result">
        <div className="chat-result-metrics">
          {CHAT_METRICS.map(([key, label, suf]) => {
            const v = m[key] as number | null | undefined;
            const pol =
              key === "cagr" || key === "total_return"
                ? typeof v === "number"
                  ? v >= 0
                    ? "pos"
                    : "neg"
                  : ""
                : key === "mdd"
                ? "neg"
                : "";
            return (
              <div className="chat-result-stat" key={key}>
                <div className="chat-result-label">{label}</div>
                <div className={`chat-result-value ${pol}`}>{fmt(v, suf)}</div>
              </div>
            );
          })}
        </div>
        <EquityChart
          equity={r.equity ?? []}
          benchmark={r.benchmark}
          trades={r.trades}
        />
      </div>
    );
  }

  // ── fallback: 성공 칩 or 오류 메시지 ──────────────────────────────────────
  if (r.success === false && r.error) {
    return (
      <div className="chat-result-fallback">
        <div className="chat-tool done">✓ 분석 완료</div>
        <div className="chat-result-error">{r.error}</div>
      </div>
    );
  }

  return <div className="chat-tool done">✓ 분석 완료</div>;
}

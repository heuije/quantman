/**
 * ChatResultView — tool_result 인라인 차트 렌더러 (P1a)
 *
 * 라우팅:
 *   select  (r.query === "select") → RankedListChart
 *   simulate (r.equity?.length)    → 지표행 + EquityChart
 *   fallback                       → 칩 + 오류 메시지(있을 때)
 *
 * IrBuilder의 ResultPanel 로직을 참조하되 chat 버블 안에서 쓰기 적합한
 * 최소형으로 구현한다. ResultPanel / IrBuilder는 수정하지 않는다.
 */

import EquityChart from "./EquityChart";
import { RankedListChart } from "./ResultCharts";
import type { IrStrategyResult } from "../types";

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

export default function ChatResultView({ result }: Props) {
  const r = result as unknown as IrStrategyResult;

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

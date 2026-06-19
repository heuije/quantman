/**
 * ChatResultView — tool_result 인라인 차트 렌더러 (P1a)
 *
 * 라우팅 (IrBuilder의 ResultPanel과 동일 판별자·동일 컴포넌트 재사용):
 *   save_strategy (r.strategy_id)        → 저장 완료 카드 + 내 전략 링크 (P2)
 *   select        (r.query === "select") → RankedListChart
 *   describe      (r.report==="single")  → ReportCards 단일종목 360
 *                 (r.report==="portfolio")→ DiagnosisPanel 포트폴리오 진단
 *   extremize     (r.reduction==="extremize") → ExtremizeChart
 *   relate-회귀   (r.axis==="relation" && r.relation==="regression") → RegressionChart
 *   relate-IC     (r.axis==="relation")  → ICChart + 표
 *   이벤트 스터디  (r.axis==="time")       → EventStudyChart + 표
 *   신호값 분포    (r.axis==="signal")     → SignalDistChart + 표
 *   펼침          (r.axis && r.buckets)   → SweepChart + 버킷 표 + 유의성
 *   inspect       (r.query==="inspect")  → 원시 시계열 라인차트
 *   simulate      (r.equity?.length)     → 지표행 + EquityChart  ← 위 분기 미스 시 폴백
 *   fallback                             → 칩 + 오류 메시지(있을 때)
 *
 * ⚠ 순서 주의: 국면 contrast 결과는 top-level `equity`를 같이 실어 보내므로, axis/buckets
 *   기반 분기를 반드시 equity 분기 *앞*에 둬야 contrast가 일반 백테스트 곡선으로 오인 렌더되지
 *   않는다(buckets·compare.pairwise 유의성 누락 방지). equity 분기는 axis 없는 단일 백테스트 폴백.
 *
 * IrBuilder의 ResultPanel 로직을 참조하되 chat 버블 안에서 쓰기 적합한 최소형으로 구현한다.
 * 차트 컴포넌트는 ResultCharts에서 그대로 재사용한다. ResultPanel / IrBuilder는 수정하지 않는다.
 */

import {
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { Link } from "react-router-dom";
import { useState } from "react";
import EquityChart from "./EquityChart";
import ExcelExportButton from "./ExcelExportButton";
import ParamControls, { type AdjustableParam } from "./ParamControls";
import {
  DiagnosisPanel, EventStudyChart, ExtremizeChart, ICChart, RankedListChart,
  RegressionChart, ReportCards, SignalDistChart, SweepChart,
} from "./ResultCharts";
import type {
  IrDistribution, IrEventStat, IrExtremizeResult, IrICStat, IrPartition,
  IrPortfolioDiagnosis, IrRegressionResult, IrSingleReport, IrStrategyResult,
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
    : axis === "condition" ? "국면"
    : rows.every(([k]) => /^\d{4}$/.test(k)) ? "연도"
    : rows.every(([k]) => /^\d{4}Q\d$/.test(k)) ? "분기"
    : rows.every(([k]) => /^\d{4}-\d{2}$/.test(k)) ? "월" : "기간";
  return (
    <div className="chat-result">
      <div className="muted" style={{ fontSize: "0.8em", marginBottom: 4 }}>
        {colLabel}별 성과 (백테스트 손익)</div>
      {result.warnings?.length ? (
        <div className="warn-banner">⚠ {result.warnings.map((w) => w.message).join(" · ")}</div>
      ) : null}
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
    </div>
  );
}

// ── 이벤트 스터디(axis="time") — EventStudyChart + 표 ──
// IrBuilder EventStudyPanel(L1546-1629)을 chat 버블용 최소형으로 미러(국면별 표는 생략 — 본문 길이).
function EventStudy({ result }: { result: IrStrategyResult }) {
  const windows = result.windows ?? [];
  const overall = (result.overall ?? {}) as Record<string, IrEventStat>;
  const pcell = (p?: number) => (
    <td className={p != null && p < 0.05 ? "pos" : ""}>{p != null ? p.toFixed(4) : "—"}</td>);
  const basisLabel = { close: "종가→종가", intraday: "시가→종가(당일)", excess: "시장초과" }[
    result.basis ?? "close"] ?? "종가→종가";
  return (
    <div className="chat-result">
      <div className="muted" style={{ fontSize: "0.8em", marginBottom: 4 }}>
        이벤트 분석 — 총 {result.n_events ?? 0}건 · 기준 {basisLabel} (forward 수익, 손익 아님)
      </div>
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
                  <td className={o.p_value != null && o.p_value < 0.05 ? "pos" : ""}>
                    {o.p_value != null ? o.p_value.toFixed(4) : "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function ChatResultView({ result }: Props) {
  // 결과가 IR을 들고 오면(=엔진 분석) 결과 아래에 '엑셀로 내보내기'(증빙)와 '변수 조정'(실시간
  // 재실행) 도구를 붙인다. inspect(원시 dump)·저장 카드 등 IR 없는 결과엔 미노출.
  const ir0 = (result as { ir?: Record<string, unknown> }).ir;
  const manifest = (result as { adjustable?: AdjustableParam[] }).adjustable;
  // 변수 조정 재실행 시 표시 결과·IR을 교체(원본 챗 메시지는 불변 — 로컬 상태로 미리보기).
  const [live, setLive] = useState<{ ir: Record<string, unknown> | undefined; result: Record<string, unknown> }>(
    { ir: ir0, result },
  );
  return (
    <>
      <ChatResultBody result={live.result} />
      {ir0 && <ExcelExportButton ir={live.ir ?? ir0} />}
      {ir0 && manifest && manifest.length > 0 && (
        <ParamControls baseIr={ir0} manifest={manifest}
          onRun={(ir, res) => setLive({ ir, result: res })} />
      )}
    </>
  );
}

function ChatResultBody({ result }: Props) {
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

  // ── describe single: 단일종목 360 리포트 ───────────────────────────────────
  if (r.report === "single") {
    return (
      <div className="chat-result">
        <ReportCards r={result as unknown as IrSingleReport} />
      </div>
    );
  }

  // ── describe portfolio: 포트폴리오 진단 ────────────────────────────────────
  if (r.report === "portfolio") {
    return (
      <div className="chat-result">
        <DiagnosisPanel r={result as unknown as IrPortfolioDiagnosis} />
      </div>
    );
  }

  // ── extremize: 목적함수 최적해 + OOS 가드 ──────────────────────────────────
  if (r.reduction === "extremize") {
    return (
      <div className="chat-result">
        <ExtremizeChart r={result as unknown as IrExtremizeResult} />
      </div>
    );
  }

  // ── relate regression: 다중팩터 횡단 회귀 ──────────────────────────────────
  if (r.axis === "relation" && r.relation === "regression") {
    return (
      <div className="chat-result">
        <RegressionChart r={result as unknown as IrRegressionResult} />
      </div>
    );
  }

  // ── 이벤트 스터디(axis="time") ─────────────────────────────────────────────
  if (r.axis === "time") {
    return <EventStudy result={r} />;
  }

  // ── 신호값 분포(axis="signal") ─────────────────────────────────────────────
  if (r.axis === "signal") {
    return <SignalStudy result={r} />;
  }

  // ── 횡단 IC(axis="relation", 회귀 외) ──────────────────────────────────────
  if (r.axis === "relation") {
    return <ICStudy result={r} />;
  }

  // ── 펼침(parameter/asset/period_split/condition) — 버킷 표 ──────────────────
  // ⚠ 반드시 equity 분기보다 앞: condition contrast가 top-level equity를 실어 보내므로
  //   여기서 먼저 잡지 않으면 일반 백테스트 곡선으로 오인 렌더되어 buckets·유의성이 사라진다.
  if (r.axis && r.buckets) {
    return <SweepBuckets result={r} />;
  }

  // ── inspect: 원시 시계열 라인차트 ──────────────────────────────────────────
  if ((result as { query?: string }).query === "inspect") {
    return <InspectChart result={result} />;
  }

  // ── simulate: 지표행 + 자산곡선 (axis 없는 단일 백테스트 폴백) ───────────────
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

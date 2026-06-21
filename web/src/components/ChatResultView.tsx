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
  CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { Link } from "react-router-dom";
import { useState, type ReactElement } from "react";
import EquityChart from "./EquityChart";
import ExcelExportButton from "./ExcelExportButton";
import ParamControls, { type AdjustableParam } from "./ParamControls";
import {
  BreadthPanel, CorrelationHeatmap, DiagnosisPanel, EventStudyChart, ExtremizeChart, ICChart,
  PrescribePanel, RankedListChart, RegressionChart, ReportCards, SignalDistChart, SweepChart,
} from "./ResultCharts";
import type {
  BreadthResult, IrDistribution, IrEventStat, IrExtremizeResult, IrICStat, IrPartition,
  IrPortfolioDiagnosis, IrRegressionResult, IrSingleReport, IrStrategyResult, PrescribeResult,
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

// 엑셀 증빙(라이브 수식)을 지원하는 형상 — 백테스트·분석만. 시각화 전용 신형상(상관 히트맵·
// 트리맵·breadth 등)은 수식 증빙 대상이 아니라 버튼을 숨긴다(build_strategy_excel 미지원 형상).
const EXCEL_SHAPES = new Set([
  "simulate", "select", "describe_single", "describe_portfolio",
  "extremize", "sweep", "relate_ic", "relate_regression", "event_study", "signal_dist",
]);

// P4 맥락 카드 — 사이드카(준실시간 시세·뉴스)를 형상 렌더러와 **직교**하게 결과 아래 표시.
// context 없으면(엔진 단독·다른 형상) 렌더 안 함. 시세 등락은 한국식 방향색(상승=빨강·하락=파랑).
function ContextCard({ context }: { context?: IrStrategyResult["context"] }) {
  if (!context) return null;
  const quotes = Object.entries(context.quotes ?? {});
  const news = context.news ?? [];
  if (quotes.length === 0 && news.length === 0) return null;
  return (
    <div className="chat-context">
      {quotes.length > 0 && (
        <div className="chat-context-quotes">
          <span className="chat-context-label">준실시간</span>
          {quotes.slice(0, 8).map(([code, q]) => (
            <span key={code} className="chat-context-quote">
              {code} {q.close != null ? q.close.toLocaleString() : "—"}
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

export default function ChatResultView({ result }: Props) {
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
  return (
    <>
      <ChatResultBody result={live.result} />
      <ContextCard context={(live.result as unknown as IrStrategyResult).context} />
      {ir0 && EXCEL_SHAPES.has(shape) && <ExcelExportButton ir={live.ir ?? ir0} />}
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
        <RankedListChart results={r.results ?? []} as_of={r.as_of}
          universe_size={r.universe_size} eligible_size={r.eligible_size} />
      </div>
    );
  },
  describe_single: (result) => (
    <div className="chat-result"><ReportCards r={result as unknown as IrSingleReport} /></div>
  ),
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
  event_study: (result) => <EventStudy result={result as unknown as IrStrategyResult} />,
  signal_dist: (result) => <SignalStudy result={result as unknown as IrStrategyResult} />,
  sweep: (result) => <SweepBuckets result={result as unknown as IrStrategyResult} />,
  inspect: (result) => <InspectChart result={result} />,
  simulate: (result) => <SimulateChart result={result} />,
};

function ChatResultBody({ result }: Props) {
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

  // 형상 레지스트리 단일 조회 — 엔진 스탬프(result.shape) 우선, 미스탬프는 deriveShape 폴백.
  const shape = (r as { shape?: string }).shape ?? deriveShape(r);
  const renderer = RENDERERS[shape];
  if (renderer) return renderer(result);

  // fallback: 오류 메시지 or 완료 칩
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

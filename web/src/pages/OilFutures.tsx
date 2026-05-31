/**
 * WTI 원유선물 분석 대시보드 (Phase 2).
 *
 * 5개 섹션:
 *   ① 데이터 메타 (기간/가격범위/최근가)
 *   ② 임계값 × horizon 히트맵 — 색으로 좋고 나쁨 직관 표시
 *   ③ TOP 조합 표 — 신뢰 가능(n≥30) / 의심(low_sample) 구분
 *   ④ 단일 조합 백테스트 상세 — 등 자산 곡선 + trade 리스트
 *   ⑤ Walk-forward 검증 — IS vs OOS 비교 (overfit 체크)
 *
 * 백엔드: server/app/routers/oil_futures.py → quant_core.oil_futures
 */

import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  oilApi,
  type OilBacktest,
  type OilDataInfo,
  type OilGridCell,
  type OilWalkForward,
} from "../api";

// 엑셀 원본과 동일한 기본 범위 (서버 디폴트와 일치)
const DEFAULT_SHORTS = [80, 90, 100, 110, 120, 130, 140, 150];
const DEFAULT_LONGS = [10, 20, 30, 40, 50, 60];
const DEFAULT_HORIZONS = [20, 40, 60, 120];

// 색 스케일: -inf→짙은 빨강, 0→회색, +inf→짙은 녹색
function heatColor(v: number, max: number): string {
  if (!Number.isFinite(v) || max <= 0) return "#1f2937";
  const r = Math.max(-1, Math.min(1, v / max));
  if (r >= 0) {
    // 0~1 → 회색에서 녹색
    const g = Math.round(80 + r * 160);
    return `rgb(40, ${g}, 80)`;
  } else {
    const ri = Math.round(80 + -r * 160);
    return `rgb(${ri}, 50, 60)`;
  }
}

const pct = (v: number, digits = 1) =>
  (v >= 0 ? "+" : "") + (v * 100).toFixed(digits) + "%";
const usd = (v: number) =>
  (v >= 0 ? "" : "-") + "$" + Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 });

export default function OilFutures() {
  // 데이터 메타
  const [info, setInfo] = useState<OilDataInfo | null>(null);

  // Grid (전체 56칸)
  const [grid, setGrid] = useState<OilGridCell[] | null>(null);
  const [gridLoading, setGridLoading] = useState(true);
  const [gridError, setGridError] = useState<string | null>(null);

  // 정렬·필터
  const [sortBy, setSortBy] = useState<"net_pnl" | "sharpe" | "win_rate" | "n_trades">("net_pnl");
  const [hideLowSample, setHideLowSample] = useState(false);

  // 백테스트 상세 (선택된 cell)
  const [selected, setSelected] = useState<OilGridCell | null>(null);
  const [backtest, setBacktest] = useState<OilBacktest | null>(null);
  const [btLoading, setBtLoading] = useState(false);

  // Walk-forward
  const [splitDate, setSplitDate] = useState("2020-01-01");
  const [wf, setWf] = useState<OilWalkForward | null>(null);
  const [wfLoading, setWfLoading] = useState(false);
  const [wfError, setWfError] = useState<string | null>(null);

  // ── 초기 로드 ─────────────────────────────────────────────────────
  useEffect(() => {
    oilApi.dataInfo().then(setInfo).catch((e) => console.error("data-info", e));

    setGridLoading(true);
    oilApi
      .grid()
      .then((g) => {
        setGrid(g);
        // 첫 진입 시 통계 신뢰 가능한 TOP을 자동 선택
        const trusted = g.filter((c) => !c.low_sample && c.net_pnl_usd > 0)
                         .sort((a, b) => b.net_pnl_usd - a.net_pnl_usd);
        if (trusted.length) setSelected(trusted[0]);
      })
      .catch((e) => setGridError(e.message))
      .finally(() => setGridLoading(false));
  }, []);

  // 선택 변경 시 백테스트 상세 가져오기
  useEffect(() => {
    if (!selected) return;
    setBtLoading(true);
    setBacktest(null);
    oilApi
      .backtest({
        side: selected.side,
        threshold: selected.threshold,
        horizon_days: selected.horizon,
      })
      .then(setBacktest)
      .catch((e) => console.error("backtest", e))
      .finally(() => setBtLoading(false));
  }, [selected]);

  // ── 정렬/필터된 그리드 ─────────────────────────────────────────────
  const gridSorted = useMemo(() => {
    if (!grid) return [];
    const filtered = hideLowSample ? grid.filter((c) => !c.low_sample) : grid;
    const sorters: Record<string, (a: OilGridCell, b: OilGridCell) => number> = {
      net_pnl: (a, b) => b.net_pnl_usd - a.net_pnl_usd,
      sharpe: (a, b) => b.sharpe - a.sharpe,
      win_rate: (a, b) => b.win_rate - a.win_rate,
      n_trades: (a, b) => b.n_trades - a.n_trades,
    };
    return [...filtered].sort(sorters[sortBy]);
  }, [grid, hideLowSample, sortBy]);

  // 히트맵 데이터 (side별로 임계×horizon 매트릭스)
  const heatmaps = useMemo(() => {
    if (!grid) return { short: [], long: [], max: 1 };
    const max = Math.max(
      1,
      ...grid.filter((c) => !c.low_sample).map((c) => Math.abs(c.net_pnl_usd)),
    );
    const short = DEFAULT_SHORTS.map((th) => ({
      threshold: th,
      cells: DEFAULT_HORIZONS.map((h) =>
        grid.find((c) => c.side === "short" && c.threshold === th && c.horizon === h),
      ),
    }));
    const long = DEFAULT_LONGS.map((th) => ({
      threshold: th,
      cells: DEFAULT_HORIZONS.map((h) =>
        grid.find((c) => c.side === "long" && c.threshold === th && c.horizon === h),
      ),
    }));
    return { short, long, max };
  }, [grid]);

  // ── Walk-forward 실행 ─────────────────────────────────────────────
  function runWalkForward() {
    setWfLoading(true);
    setWfError(null);
    oilApi
      .walkforward({ split_date: splitDate })
      .then(setWf)
      .catch((e) => setWfError(e.message))
      .finally(() => setWfLoading(false));
  }

  // ── 렌더 ─────────────────────────────────────────────────────────
  return (
    <div className="oil-page">
      <header className="oil-header">
        <h1>WTI 원유선물 분석</h1>
        <p className="muted">
          장중 high/low가 임계값을 첫 터치하면 신호 → N영업일 보유 백테스트.
          엑셀 원본 한계(종가 cross, look-ahead, low-sample 미경고)를 보완한 결과.
        </p>
      </header>

      {/* ① 데이터 메타 */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">① 데이터 메타</h2>
        {!info ? (
          <div className="muted">로딩 중…</div>
        ) : (
          <div className="meta-grid">
            <div>
              <div className="muted">기간</div>
              <div className="meta-value">
                {info.start_date} ~ {info.end_date}
              </div>
            </div>
            <div>
              <div className="muted">영업일</div>
              <div className="meta-value">{info.n_rows.toLocaleString()}일 (~{Math.round(info.n_rows / 252)}년)</div>
            </div>
            <div>
              <div className="muted">가격 범위</div>
              <div className="meta-value">
                ${info.price_min.toFixed(2)} ~ ${info.price_max.toFixed(2)}
              </div>
            </div>
          </div>
        )}
      </section>

      {/* ② 히트맵 */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">② Net PnL 히트맵 (임계값 × horizon)</h2>
        <p className="muted" style={{ marginBottom: 12 }}>
          색이 진할수록 강함 — <span style={{ color: "#62c884" }}>녹색=수익</span>,{" "}
          <span style={{ color: "#d96265" }}>빨강=손실</span>. 클릭하면 백테스트 상세.{" "}
          <span style={{ opacity: 0.6 }}>* low_sample(⚠) 셀은 회색 톤.</span>
        </p>
        {gridLoading ? (
          <div className="muted">그리드 계산 중…</div>
        ) : gridError ? (
          <div className="error">{gridError}</div>
        ) : (
          <div className="heatmap-wrap">
            <HeatmapBlock
              title="Short (위로 첫 터치)"
              rows={heatmaps.short}
              max={heatmaps.max}
              selected={selected}
              onSelect={setSelected}
            />
            <HeatmapBlock
              title="Long (아래로 첫 터치)"
              rows={heatmaps.long}
              max={heatmaps.max}
              selected={selected}
              onSelect={setSelected}
            />
          </div>
        )}
      </section>

      {/* ③ TOP 조합 표 */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">③ 조합 순위표</h2>
        <div className="oil-toolbar">
          <label>
            정렬:&nbsp;
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value as typeof sortBy)}
            >
              <option value="net_pnl">Net PnL ($)</option>
              <option value="sharpe">Sharpe</option>
              <option value="win_rate">승률</option>
              <option value="n_trades">샘플 수</option>
            </select>
          </label>
          <label>
            <input
              type="checkbox"
              checked={hideLowSample}
              onChange={(e) => setHideLowSample(e.target.checked)}
            />
            &nbsp;low_sample(n&lt;30) 숨기기
          </label>
          <span className="muted">{gridSorted.length}개 표시</span>
        </div>
        <div className="table-scroll">
          <table className="oil-table">
            <thead>
              <tr>
                <th>Side</th>
                <th>임계</th>
                <th>H일</th>
                <th>n</th>
                <th>승률</th>
                <th>평균수익</th>
                <th>Sharpe</th>
                <th>PF</th>
                <th>MDD($)</th>
                <th>Net PnL($)</th>
                <th>⚠</th>
              </tr>
            </thead>
            <tbody>
              {gridSorted.slice(0, 30).map((c) => {
                const isSel =
                  selected?.side === c.side &&
                  selected?.threshold === c.threshold &&
                  selected?.horizon === c.horizon;
                return (
                  <tr
                    key={`${c.side}-${c.threshold}-${c.horizon}`}
                    className={isSel ? "selected-row" : ""}
                    onClick={() => setSelected(c)}
                  >
                    <td className={c.side === "short" ? "short" : "long"}>{c.side}</td>
                    <td>${c.threshold}</td>
                    <td>{c.horizon}</td>
                    <td>{c.n_trades}</td>
                    <td>{(c.win_rate * 100).toFixed(1)}%</td>
                    <td className={c.avg_return >= 0 ? "pos" : "neg"}>
                      {pct(c.avg_return, 2)}
                    </td>
                    <td>{c.sharpe.toFixed(2)}</td>
                    <td>{c.profit_factor == null ? "∞" : c.profit_factor.toFixed(2)}</td>
                    <td className="neg">{usd(c.mdd_usd)}</td>
                    <td className={c.net_pnl_usd >= 0 ? "pos" : "neg"}>
                      {usd(c.net_pnl_usd)}
                    </td>
                    <td>{c.low_sample ? "⚠️" : ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {/* ④ 선택 조합 백테스트 상세 */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">
          ④ 백테스트 상세 {selected && `— ${selected.side} $${selected.threshold} × ${selected.horizon}일`}
        </h2>
        {!selected ? (
          <div className="muted">위 히트맵/표에서 한 셀을 클릭하면 상세가 표시됩니다.</div>
        ) : btLoading || !backtest ? (
          <div className="muted">백테스트 실행 중…</div>
        ) : (
          <BacktestDetail bt={backtest} />
        )}
      </section>

      {/* ⑤ Walk-forward */}
      <section className="panel">
        <h2 className="section-title">⑤ Walk-forward 검증 (overfit 체크)</h2>
        <div className="oil-toolbar">
          <label>
            분할 날짜:&nbsp;
            <input
              type="date"
              value={splitDate}
              onChange={(e) => setSplitDate(e.target.value)}
              min={info?.start_date ?? "2004-01-05"}
              max={info?.end_date ?? "2026-04-29"}
            />
          </label>
          <button onClick={runWalkForward} disabled={wfLoading}>
            {wfLoading ? "실행 중…" : "Walk-forward 실행"}
          </button>
        </div>
        {wfError && <div className="error">{wfError}</div>}
        {wf && <WalkForwardView wf={wf} />}
      </section>
    </div>
  );
}

// ── 히트맵 블록 ────────────────────────────────────────────────────
function HeatmapBlock({
  title,
  rows,
  max,
  selected,
  onSelect,
}: {
  title: string;
  rows: { threshold: number; cells: (OilGridCell | undefined)[] }[];
  max: number;
  selected: OilGridCell | null;
  onSelect: (c: OilGridCell) => void;
}) {
  return (
    <div className="heatmap-block">
      <div className="heatmap-title">{title}</div>
      <table className="heatmap-table">
        <thead>
          <tr>
            <th></th>
            {DEFAULT_HORIZONS.map((h) => (
              <th key={h}>{h}일</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.threshold}>
              <th>${row.threshold}</th>
              {row.cells.map((c, i) => {
                if (!c) return <td key={i} />;
                const isSel =
                  selected?.side === c.side &&
                  selected?.threshold === c.threshold &&
                  selected?.horizon === c.horizon;
                const dim = c.low_sample;
                const bg = dim ? "#444b55" : heatColor(c.net_pnl_usd, max);
                return (
                  <td
                    key={i}
                    title={`n=${c.n_trades}, 승률 ${(c.win_rate * 100).toFixed(
                      0,
                    )}%, Net $${c.net_pnl_usd.toLocaleString()}, Sharpe ${c.sharpe.toFixed(2)}${
                      dim ? " (low sample)" : ""
                    }`}
                    style={{
                      background: bg,
                      cursor: c.n_trades > 0 ? "pointer" : "default",
                      outline: isSel ? "2px solid #fff" : "none",
                      opacity: c.n_trades > 0 ? 1 : 0.3,
                    }}
                    onClick={() => c.n_trades > 0 && onSelect(c)}
                  >
                    {c.n_trades > 0 ? (
                      <div className="heat-cell">
                        <div style={{ fontSize: 11 }}>
                          {(c.win_rate * 100).toFixed(0)}%
                        </div>
                        <div style={{ fontSize: 10, opacity: 0.85 }}>
                          {(c.net_pnl_usd / 1000).toFixed(0)}k
                        </div>
                      </div>
                    ) : null}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── 백테스트 상세 ──────────────────────────────────────────────────
function BacktestDetail({ bt }: { bt: OilBacktest }) {
  const s = bt.summary;
  const eq = bt.equity_curve;
  return (
    <>
      <div className="bt-metrics">
        <Metric label="거래 수" value={s.n_trades} highlight={s.low_sample ? "warn" : null} />
        <Metric label="승률" value={(s.win_rate * 100).toFixed(1) + "%"} />
        <Metric
          label="평균 수익률"
          value={pct(s.avg_return, 2)}
          highlight={s.avg_return >= 0 ? "good" : "bad"}
        />
        <Metric label="Sharpe (연환산)" value={s.sharpe.toFixed(2)} />
        <Metric
          label="Profit Factor"
          value={s.profit_factor == null ? "∞" : s.profit_factor.toFixed(2)}
        />
        <Metric label="최대 낙폭 (USD)" value={usd(s.mdd_usd)} highlight="bad" />
        <Metric
          label="누적 Net PnL"
          value={usd(s.net_pnl_usd)}
          highlight={s.net_pnl_usd >= 0 ? "good" : "bad"}
        />
      </div>
      {s.low_sample && (
        <div className="warn-banner">
          ⚠️ 거래 수 {s.n_trades}건 (&lt;30) — 통계적 유의성 낮음. 평균이 좋아 보여도 신중히 해석하세요.
        </div>
      )}

      <div style={{ marginTop: 16 }}>
        <div className="muted" style={{ fontSize: 13, marginBottom: 6 }}>
          등 자산 곡선 (1계약 기준, 누적 USD)
        </div>
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={eq.map((p) => ({ ...p, idx: p.date }))}>
            <CartesianGrid strokeDasharray="3 3" stroke="#333" />
            <XAxis
              dataKey="idx"
              tick={{ fontSize: 10, fill: "#9aa" }}
              minTickGap={50}
            />
            <YAxis
              tick={{ fontSize: 10, fill: "#9aa" }}
              tickFormatter={(v) => (v / 1000).toFixed(0) + "k"}
            />
            <Tooltip
              labelStyle={{ color: "#333" }}
              formatter={(v) => "$" + Number(v).toLocaleString()}
            />
            <ReferenceLine y={0} stroke="#666" />
            <Line
              type="monotone"
              dataKey="cumulative_usd"
              stroke={s.net_pnl_usd >= 0 ? "#62c884" : "#d96265"}
              dot={false}
              strokeWidth={2}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <details style={{ marginTop: 16 }}>
        <summary className="muted" style={{ cursor: "pointer" }}>
          개별 거래 ({bt.trades.length}건) 보기
        </summary>
        <div className="table-scroll" style={{ maxHeight: 360, marginTop: 8 }}>
          <table className="oil-table">
            <thead>
              <tr>
                <th>신호일</th>
                <th>진입일</th>
                <th>진입가</th>
                <th>청산일</th>
                <th>청산가</th>
                <th>수익률</th>
                <th>Net PnL($)</th>
              </tr>
            </thead>
            <tbody>
              {bt.trades.map((t, i) => (
                <tr key={i}>
                  <td>{t.signal_date}</td>
                  <td>{t.entry_date}</td>
                  <td>${t.entry_price.toFixed(2)}</td>
                  <td>{t.exit_date}</td>
                  <td>${t.exit_price.toFixed(2)}</td>
                  <td className={t.return_pct >= 0 ? "pos" : "neg"}>
                    {pct(t.return_pct, 2)}
                  </td>
                  <td className={t.net_pnl_usd >= 0 ? "pos" : "neg"}>
                    {usd(t.net_pnl_usd)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </>
  );
}

// ── Walk-forward 결과 패널 ─────────────────────────────────────────
function WalkForwardView({ wf }: { wf: OilWalkForward }) {
  const b = wf.best_in_sample;
  const oos = wf.out_of_sample;
  const ratio = b.summary.avg_return ? oos.avg_return / b.summary.avg_return : 0;
  let badge: { color: string; text: string };
  if (oos.avg_return < 0) badge = { color: "#d96265", text: "⚠ OOS 음수 — 강한 overfit 의심" };
  else if (ratio < 0.3) badge = { color: "#e6c259", text: "⚠ OOS가 IS의 30% 미만 — overfit 가능" };
  else badge = { color: "#62c884", text: `✓ OOS가 IS의 ${(ratio * 100).toFixed(0)}% 수준 — 견고` };

  return (
    <div className="wf-view">
      <div className="wf-row">
        <div className="wf-block">
          <div className="muted">Train (학습) 구간</div>
          <div>{wf.train_start} ~ {wf.train_end}</div>
          <div style={{ marginTop: 8 }}>
            <strong>Best in-sample</strong>: {b.side} ${b.threshold} × {b.horizon}일
          </div>
          <SummaryGrid s={b.summary} />
        </div>
        <div className="wf-block">
          <div className="muted">Test (out-of-sample) 구간</div>
          <div>{wf.test_start} ~ {wf.test_end}</div>
          <div style={{ marginTop: 8 }}>
            <strong>같은 파라미터의 Test 결과</strong>
          </div>
          <SummaryGrid s={oos} />
        </div>
      </div>
      <div className="wf-badge" style={{ background: badge.color }}>
        {badge.text}
      </div>
    </div>
  );
}

function SummaryGrid({ s }: { s: import("../api").OilSummary }) {
  return (
    <div className="summary-grid">
      <div><span className="muted">n</span> {s.n_trades}</div>
      <div><span className="muted">승률</span> {(s.win_rate * 100).toFixed(1)}%</div>
      <div><span className="muted">평균수익</span> <span className={s.avg_return >= 0 ? "pos" : "neg"}>{pct(s.avg_return, 2)}</span></div>
      <div><span className="muted">Sharpe</span> {s.sharpe.toFixed(2)}</div>
      <div><span className="muted">Net PnL</span> <span className={s.net_pnl_usd >= 0 ? "pos" : "neg"}>{usd(s.net_pnl_usd)}</span></div>
      {s.low_sample && <div style={{ color: "#e6c259" }}>⚠ low sample</div>}
    </div>
  );
}

function Metric({
  label, value, highlight = null,
}: {
  label: string;
  value: React.ReactNode;
  highlight?: "good" | "bad" | "warn" | null;
}) {
  const color = highlight === "good" ? "#62c884"
              : highlight === "bad" ? "#d96265"
              : highlight === "warn" ? "#e6c259"
              : undefined;
  return (
    <div className="metric-card">
      <div className="muted" style={{ fontSize: 12 }}>{label}</div>
      <div className="metric-value" style={{ color }}>{value}</div>
    </div>
  );
}

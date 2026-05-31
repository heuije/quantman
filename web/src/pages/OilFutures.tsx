/**
 * WTI 원유선물 분석 대시보드 (Phase 2 + 사용자 피드백 반영).
 *
 * 섹션 순서:
 *   ① 데이터 메타
 *   ② Net PnL 히트맵 (셀: 수익률/승률 + 표본 작아도 색 보존)
 *   ③ 백테스트 상세 (등 자산 곡선 + BUY/SELL 마킹 trade 표)
 *   ④ 조합 순위표 (헤더 정렬 + sticky + Profit/Loss 컬럼)
 *   ⑤ Walk-forward 검증
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

const DEFAULT_SHORTS = [80, 90, 100, 110, 120, 130, 140, 150];
const DEFAULT_LONGS = [10, 20, 30, 40, 50, 60];
const DEFAULT_HORIZONS = [20, 40, 60, 120];

// 색 스케일: 음수→빨강, 양수→녹색.
// low_sample이면 채도·명도 낮춰서 회색쪽으로 블렌드(원본 부호는 유지).
function heatColor(v: number, max: number, lowSample: boolean): string {
  if (!Number.isFinite(v) || max <= 0) return "#1f2937";
  const r = Math.max(-1, Math.min(1, v / max));
  let R: number, G: number, B: number;
  if (r >= 0) {
    R = 40;
    G = Math.round(80 + r * 160);
    B = 80;
  } else {
    R = Math.round(80 + -r * 160);
    G = 50;
    B = 60;
  }
  if (lowSample) {
    // 회색(110,110,110)과 60% 블렌드 → 부호/방향 보이지만 채도 ↓
    const mix = (c: number) => Math.round(c * 0.4 + 110 * 0.6);
    R = mix(R);
    G = mix(G);
    B = mix(B);
  }
  return `rgb(${R}, ${G}, ${B})`;
}

const pct = (v: number, digits = 1) =>
  (v >= 0 ? "+" : "") + (v * 100).toFixed(digits) + "%";
const pctNoSign = (v: number, digits = 1) =>
  (v * 100).toFixed(digits) + "%";
const usd = (v: number) =>
  (v >= 0 ? "" : "-") + "$" + Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 });

// 정렬 가능 컬럼 키
type SortKey =
  | "side" | "threshold" | "horizon" | "n_trades" | "win_rate"
  | "avg_return" | "sharpe" | "profit_factor" | "mdd_usd"
  | "gross_profit_usd" | "gross_loss_usd" | "net_pnl_usd";

type SortDir = "asc" | "desc";

export default function OilFutures() {
  const [info, setInfo] = useState<OilDataInfo | null>(null);

  const [grid, setGrid] = useState<OilGridCell[] | null>(null);
  const [gridLoading, setGridLoading] = useState(true);
  const [gridError, setGridError] = useState<string | null>(null);

  // 정렬 상태 (헤더 클릭 → 토글)
  const [sortKey, setSortKey] = useState<SortKey>("net_pnl_usd");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [hideLowSample, setHideLowSample] = useState(false);

  const [selected, setSelected] = useState<OilGridCell | null>(null);
  const [backtest, setBacktest] = useState<OilBacktest | null>(null);
  const [btLoading, setBtLoading] = useState(false);

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
        const trusted = g.filter((c) => !c.low_sample && c.net_pnl_usd > 0)
                         .sort((a, b) => b.net_pnl_usd - a.net_pnl_usd);
        if (trusted.length) setSelected(trusted[0]);
      })
      .catch((e) => setGridError(e.message))
      .finally(() => setGridLoading(false));
  }, []);

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

  // 정렬·필터된 그리드
  const gridSorted = useMemo(() => {
    if (!grid) return [];
    const filtered = hideLowSample ? grid.filter((c) => !c.low_sample) : grid;
    const getVal = (c: OilGridCell): string | number => {
      switch (sortKey) {
        case "side": return c.side;
        case "threshold": return c.threshold;
        case "horizon": return c.horizon;
        case "n_trades": return c.n_trades;
        case "win_rate": return c.win_rate;
        case "avg_return": return c.avg_return;
        case "sharpe": return c.sharpe;
        case "profit_factor": return c.profit_factor ?? Number.POSITIVE_INFINITY;
        case "mdd_usd": return c.mdd_usd;
        case "gross_profit_usd": return c.gross_profit_usd;
        case "gross_loss_usd": return c.gross_loss_usd;
        case "net_pnl_usd": return c.net_pnl_usd;
      }
    };
    const sign = sortDir === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const av = getVal(a);
      const bv = getVal(b);
      if (typeof av === "string" && typeof bv === "string") return sign * av.localeCompare(bv);
      return sign * ((av as number) - (bv as number));
    });
  }, [grid, hideLowSample, sortKey, sortDir]);

  const heatmaps = useMemo(() => {
    if (!grid) return { short: [], long: [], max: 1 };
    // low_sample 포함해서 스케일 — 색 보존 위해 (단 채도는 낮춤)
    const max = Math.max(1, ...grid.map((c) => Math.abs(c.net_pnl_usd)));
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

  function runWalkForward() {
    setWfLoading(true);
    setWfError(null);
    oilApi
      .walkforward({ split_date: splitDate })
      .then(setWf)
      .catch((e) => setWfError(e.message))
      .finally(() => setWfLoading(false));
  }

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      // 숫자 컬럼은 큰 값이 기본 의미 있어서 desc, 문자열은 asc
      setSortDir(key === "side" ? "asc" : "desc");
    }
  }

  return (
    <div className="oil-page">
      <header className="oil-header">
        <h1>WTI 원유선물 분석</h1>
        <p className="muted">
          장중 high/low가 임계값을 첫 터치하면 신호 → N영업일 보유 백테스트.
        </p>
      </header>

      {/* ① 데이터 메타 */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">① 데이터 메타</h2>
        {!info ? (
          <div className="muted">로딩 중…</div>
        ) : (
          <div className="meta-grid">
            <div><div className="muted">기간</div>
              <div className="meta-value">{info.start_date} ~ {info.end_date}</div></div>
            <div><div className="muted">영업일</div>
              <div className="meta-value">{info.n_rows.toLocaleString()}일 (~{Math.round(info.n_rows / 252)}년)</div></div>
            <div><div className="muted">가격 범위</div>
              <div className="meta-value">${info.price_min.toFixed(2)} ~ ${info.price_max.toFixed(2)}</div></div>
          </div>
        )}
      </section>

      {/* ② 히트맵 */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">② Net PnL 히트맵 (임계값 × horizon)</h2>
        <p className="muted" style={{ marginBottom: 12 }}>
          셀: <b>거래당 평균수익률</b> / <b>승률</b> (소수점 1자리).
          색 진하기 = Net PnL 크기. <span style={{ color: "#62c884" }}>녹색=수익</span>,{" "}
          <span style={{ color: "#d96265" }}>빨강=손실</span>.{" "}
          low_sample(n&lt;30)은 같은 색이되 채도 낮춤(부호는 보존). 클릭하면 백테스트 상세.
        </p>
        {gridLoading ? (
          <div className="muted">그리드 계산 중…</div>
        ) : gridError ? (
          <div className="error">{gridError}</div>
        ) : (
          <div className="heatmap-wrap">
            <HeatmapBlock
              title="Short (위로 첫 터치 → 매도)"
              rows={heatmaps.short}
              max={heatmaps.max}
              selected={selected}
              onSelect={setSelected}
            />
            <HeatmapBlock
              title="Long (아래로 첫 터치 → 매수)"
              rows={heatmaps.long}
              max={heatmaps.max}
              selected={selected}
              onSelect={setSelected}
            />
          </div>
        )}
      </section>

      {/* ③ 백테스트 상세 (조합 순위표보다 먼저) */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">
          ③ 백테스트 상세 {selected && `— ${selected.side} $${selected.threshold} × ${selected.horizon}일`}
        </h2>
        {!selected ? (
          <div className="muted">위 히트맵/아래 순위표에서 한 셀을 클릭하면 상세가 표시됩니다.</div>
        ) : btLoading || !backtest ? (
          <div className="muted">백테스트 실행 중…</div>
        ) : (
          <BacktestDetail bt={backtest} side={selected.side} />
        )}
      </section>

      {/* ④ 조합 순위표 — 헤더 클릭 정렬 + sticky + Profit/Loss 추가 */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">④ 조합 순위표 ({gridSorted.length}개)</h2>
        <div className="oil-toolbar">
          <label>
            <input
              type="checkbox"
              checked={hideLowSample}
              onChange={(e) => setHideLowSample(e.target.checked)}
            />
            &nbsp;low_sample(n&lt;30) 숨기기
          </label>
          <span className="muted">헤더 클릭 = 정렬 (다시 클릭하면 방향 토글)</span>
        </div>
        <div className="table-scroll sticky-table">
          <table className="oil-table">
            <thead>
              <tr>
                <SortableTh k="side" cur={sortKey} dir={sortDir} onClick={toggleSort}>Side</SortableTh>
                <SortableTh k="threshold" cur={sortKey} dir={sortDir} onClick={toggleSort}>임계</SortableTh>
                <SortableTh k="horizon" cur={sortKey} dir={sortDir} onClick={toggleSort}>H일</SortableTh>
                <SortableTh k="n_trades" cur={sortKey} dir={sortDir} onClick={toggleSort}>n</SortableTh>
                <SortableTh k="win_rate" cur={sortKey} dir={sortDir} onClick={toggleSort}>승률</SortableTh>
                <SortableTh k="avg_return" cur={sortKey} dir={sortDir} onClick={toggleSort}>평균수익</SortableTh>
                <SortableTh k="sharpe" cur={sortKey} dir={sortDir} onClick={toggleSort}>Sharpe</SortableTh>
                <SortableTh k="profit_factor" cur={sortKey} dir={sortDir} onClick={toggleSort}>PF</SortableTh>
                <SortableTh k="mdd_usd" cur={sortKey} dir={sortDir} onClick={toggleSort}>MDD($)</SortableTh>
                <SortableTh k="gross_profit_usd" cur={sortKey} dir={sortDir} onClick={toggleSort}>Profit($)</SortableTh>
                <SortableTh k="gross_loss_usd" cur={sortKey} dir={sortDir} onClick={toggleSort}>Loss($)</SortableTh>
                <SortableTh k="net_pnl_usd" cur={sortKey} dir={sortDir} onClick={toggleSort}>Net PnL($)</SortableTh>
                <th>⚠</th>
              </tr>
            </thead>
            <tbody>
              {gridSorted.map((c) => {
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
                    <td>{pctNoSign(c.win_rate, 1)}</td>
                    <td className={c.avg_return >= 0 ? "pos" : "neg"}>{pct(c.avg_return, 2)}</td>
                    <td>{c.sharpe.toFixed(2)}</td>
                    <td>{c.profit_factor == null ? "∞" : c.profit_factor.toFixed(2)}</td>
                    <td className="neg">{usd(c.mdd_usd)}</td>
                    <td className="pos">{usd(c.gross_profit_usd)}</td>
                    <td className="neg">{usd(c.gross_loss_usd)}</td>
                    <td className={c.net_pnl_usd >= 0 ? "pos" : "neg"}>{usd(c.net_pnl_usd)}</td>
                    <td>{c.low_sample ? "⚠️" : ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
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

// ── 정렬 가능 헤더 ────────────────────────────────────────────────
function SortableTh({
  k, cur, dir, onClick, children,
}: {
  k: SortKey;
  cur: SortKey;
  dir: SortDir;
  onClick: (k: SortKey) => void;
  children: React.ReactNode;
}) {
  const active = cur === k;
  return (
    <th
      onClick={() => onClick(k)}
      className={"sortable" + (active ? " active" : "")}
      style={{ cursor: "pointer", userSelect: "none" }}
      title="클릭 = 정렬"
    >
      {children}
      <span className="sort-arrow">{active ? (dir === "asc" ? " ▲" : " ▼") : " ↕"}</span>
    </th>
  );
}

// ── 히트맵 블록 ────────────────────────────────────────────────────
function HeatmapBlock({
  title, rows, max, selected, onSelect,
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
      <div className="sticky-table" style={{ maxHeight: 400, overflow: "auto" }}>
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
                  const bg = heatColor(c.net_pnl_usd, max, c.low_sample);
                  return (
                    <td
                      key={i}
                      title={
                        `n=${c.n_trades}, 평균수익 ${pct(c.avg_return, 2)}, ` +
                        `승률 ${pctNoSign(c.win_rate, 1)}, Net ${usd(c.net_pnl_usd)}, ` +
                        `Sharpe ${c.sharpe.toFixed(2)}` +
                        (c.low_sample ? " (low sample)" : "")
                      }
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
                          <div style={{ fontSize: 11, fontWeight: 600 }}>
                            {pct(c.avg_return, 1)}
                          </div>
                          <div style={{ fontSize: 10, opacity: 0.85 }}>
                            {pctNoSign(c.win_rate, 1)}
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
    </div>
  );
}

// ── 백테스트 상세 ──────────────────────────────────────────────────
function BacktestDetail({ bt, side }: { bt: OilBacktest; side: "short" | "long" }) {
  const s = bt.summary;
  const eq = bt.equity_curve;
  // Long: entry=BUY, exit=SELL.  Short: entry=SELL(공매), exit=BUY(환매).
  const entryLabel = side === "long" ? "BUY" : "SELL";
  const exitLabel = side === "long" ? "SELL" : "BUY";

  return (
    <>
      <div className="bt-metrics">
        <Metric label="거래 수" value={s.n_trades} highlight={s.low_sample ? "warn" : null} />
        <Metric label="승률" value={pctNoSign(s.win_rate, 1)} />
        <Metric label="평균 수익률" value={pct(s.avg_return, 2)}
                highlight={s.avg_return >= 0 ? "good" : "bad"} />
        <Metric label="Sharpe (연환산)" value={s.sharpe.toFixed(2)} />
        <Metric label="Profit Factor"
                value={s.profit_factor == null ? "∞" : s.profit_factor.toFixed(2)} />
        <Metric label="MDD (USD)" value={usd(s.mdd_usd)} highlight="bad" />
        <Metric label="Profit (USD)" value={usd(s.gross_profit_usd)} highlight="good" />
        <Metric label="Loss (USD)" value={usd(s.gross_loss_usd)} highlight="bad" />
        <Metric label="Net PnL (USD)" value={usd(s.net_pnl_usd)}
                highlight={s.net_pnl_usd >= 0 ? "good" : "bad"} />
      </div>
      {s.low_sample && (
        <div className="warn-banner">
          ⚠️ 거래 수 {s.n_trades}건 (&lt;30) — 통계적 유의성 낮음. 평균이 좋아 보여도 신중히 해석.
        </div>
      )}

      <div style={{ marginTop: 16 }}>
        <div className="muted" style={{ fontSize: 13, marginBottom: 6 }}>
          등 자산 곡선 (1계약 기준, 누적 USD)
        </div>
        <ResponsiveContainer width="100%" height={240}>
          <LineChart data={eq.map((p) => ({ ...p, idx: p.date }))}>
            <CartesianGrid strokeDasharray="3 3" stroke="#333" />
            <XAxis dataKey="idx" tick={{ fontSize: 10, fill: "#9aa" }} minTickGap={50} />
            <YAxis tick={{ fontSize: 10, fill: "#9aa" }}
                   tickFormatter={(v) => (v / 1000).toFixed(0) + "k"} />
            <Tooltip labelStyle={{ color: "#333" }}
                     formatter={(v) => "$" + Number(v).toLocaleString()} />
            <ReferenceLine y={0} stroke="#666" />
            <Line type="monotone" dataKey="cumulative_usd"
                  stroke={s.net_pnl_usd >= 0 ? "#62c884" : "#d96265"}
                  dot={false} strokeWidth={2} />
          </LineChart>
        </ResponsiveContainer>
      </div>

      <details style={{ marginTop: 16 }} open>
        <summary className="muted" style={{ cursor: "pointer" }}>
          개별 거래 ({bt.trades.length}건) — 진입 [{entryLabel}], 청산 [{exitLabel}]
        </summary>
        <div className="table-scroll sticky-table" style={{ maxHeight: 420, marginTop: 8 }}>
          <table className="oil-table">
            <thead>
              <tr>
                <th>신호일</th>
                <th>진입일</th>
                <th>액션</th>
                <th>진입가</th>
                <th>청산일</th>
                <th>액션</th>
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
                  <td><span className={`bs-badge bs-${entryLabel.toLowerCase()}`}>{entryLabel}</span></td>
                  <td>${t.entry_price.toFixed(2)}</td>
                  <td>{t.exit_date}</td>
                  <td><span className={`bs-badge bs-${exitLabel.toLowerCase()}`}>{exitLabel}</span></td>
                  <td>${t.exit_price.toFixed(2)}</td>
                  <td className={t.return_pct >= 0 ? "pos" : "neg"}>{pct(t.return_pct, 2)}</td>
                  <td className={t.net_pnl_usd >= 0 ? "pos" : "neg"}>{usd(t.net_pnl_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </>
  );
}

// ── Walk-forward 결과 ─────────────────────────────────────────────
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
      <div className="wf-badge" style={{ background: badge.color }}>{badge.text}</div>
    </div>
  );
}

function SummaryGrid({ s }: { s: import("../api").OilSummary }) {
  return (
    <div className="summary-grid">
      <div><span className="muted">n</span> {s.n_trades}</div>
      <div><span className="muted">승률</span> {pctNoSign(s.win_rate, 1)}</div>
      <div><span className="muted">평균수익</span> <span className={s.avg_return >= 0 ? "pos" : "neg"}>{pct(s.avg_return, 2)}</span></div>
      <div><span className="muted">Sharpe</span> {s.sharpe.toFixed(2)}</div>
      <div><span className="muted">Profit</span> <span className="pos">{usd(s.gross_profit_usd)}</span></div>
      <div><span className="muted">Loss</span> <span className="neg">{usd(s.gross_loss_usd)}</span></div>
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

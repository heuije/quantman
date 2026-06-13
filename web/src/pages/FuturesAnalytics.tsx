/**
 * 선물 분석 대시보드 (멀티 종목 — 원유·나스닥·천연가스·금·은·비트코인).
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
  Bar,
  BarChart,
  Brush,
  CartesianGrid,
  Cell,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  futuresApi,
  type OilBacktest,
  type OilDataInfo,
  type OilGridCell,
  type OilInstrument,
  type OilLatestPrice,
  type OilMacroContext,
  type OilSeasonality,
  type OilTrendEvents,
  type OilWalkForward,
} from "../api";

// 색 스케일: 음수→빨강, 양수→녹색. 진하기 = |거래당 평균수익률| / 그리드 최대.
// 색은 수익률에만 비례 — 샘플수 채도 억제 없음(저샘플은 ⚠·툴팁으로만 경고).
function heatColor(v: number, max: number): string {
  if (!Number.isFinite(v) || max <= 0) return "#1f2937";
  const r = Math.max(-1, Math.min(1, v / max));
  if (r >= 0) {
    return `rgb(40, ${Math.round(80 + r * 160)}, 80)`;
  }
  return `rgb(${Math.round(80 + -r * 160)}, 50, 60)`;
}

const pct = (v: number, digits = 1) =>
  (v >= 0 ? "+" : "") + (v * 100).toFixed(digits) + "%";
const pctNoSign = (v: number, digits = 1) =>
  (v * 100).toFixed(digits) + "%";
const money = (v: number, sym: string) =>
  (v >= 0 ? "" : "-") + sym + Math.abs(v).toLocaleString("en-US", { maximumFractionDigits: 0 });

// 정렬 가능 컬럼 키
type SortKey =
  | "side" | "threshold" | "horizon" | "n_trades" | "win_rate"
  | "avg_return" | "sharpe" | "profit_factor" | "mdd_usd"
  | "gross_profit_usd" | "gross_loss_usd" | "net_pnl_usd";

type SortDir = "asc" | "desc";

export default function FuturesAnalytics() {
  const [instruments, setInstruments] = useState<OilInstrument[]>([]);
  const [symbol, setSymbol] = useState<string>("oil");
  const [info, setInfo] = useState<OilDataInfo | null>(null);
  const [price, setPrice] = useState<OilLatestPrice | null>(null);

  const [grid, setGrid] = useState<OilGridCell[] | null>(null);
  const [gridLoading, setGridLoading] = useState(true);
  const [gridError, setGridError] = useState<string | null>(null);

  // 정렬 상태 (헤더 클릭 → 토글)
  const [sortKey, setSortKey] = useState<SortKey>("net_pnl_usd");
  const [sortDir, setSortDir] = useState<SortDir>("desc");
  const [hideLowSample, setHideLowSample] = useState(false);

  // 히트맵에서 어느 사이드 보여줄지 (한 번에 하나)
  const [heatmapSide, setHeatmapSide] = useState<"short" | "long">("short");

  const [selected, setSelected] = useState<OilGridCell | null>(null);
  const [backtest, setBacktest] = useState<OilBacktest | null>(null);
  const [btLoading, setBtLoading] = useState(false);

  const [splitDate, setSplitDate] = useState("2020-01-01");
  const [wf, setWf] = useState<OilWalkForward | null>(null);
  const [wfLoading, setWfLoading] = useState(false);
  const [wfError, setWfError] = useState<string | null>(null);

  // 🅒 Seasonality
  const [season, setSeason] = useState<OilSeasonality | null>(null);
  // 🅔 Macro
  const [macro, setMacro] = useState<OilMacroContext | null>(null);

  const [rollCost, setRollCost] = useState<number | "">("");  // 롤 비용 %/회 (예: 0.5)
  const [exporting, setExporting] = useState(false);          // 엑셀 내보내기 진행중

  // 전략 비용·신호 설정 — 히트맵 grid + 선택 셀 백테스트 양쪽에 일괄 적용.
  const [minGapDays, setMinGapDays] = useState(0);       // 신호 쿨타임 (영업일)
  const [commission, setCommission] = useState(2.5);     // 수수료 ($/계약·레그)
  const [slippageTicks, setSlippageTicks] = useState(1); // 슬리피지 (틱/체결)

  // macro·seasonality·walk-forward·순위표는 당분간 숨김(코드 보존). 셀 선택은 히트맵 클릭.
  const showArchivedSections = false;

  // 종목 목록 1회 로드
  useEffect(() => {
    futuresApi.instruments().then(setInstruments).catch((e) => console.error("instruments", e));
  }, []);

  // 종목 변경 시 메타·계절성·매크로·가격 재로드
  useEffect(() => {
    setInfo(null);
    setPrice(null);
    setSelected(null);
    setBacktest(null);
    setWf(null);
    futuresApi.dataInfo(symbol).then(setInfo).catch((e) => console.error("data-info", e));
    futuresApi.seasonality(symbol).then(setSeason).catch((e) => console.error("seasonality", e));
    futuresApi.macroContext(symbol).then(setMacro).catch((e) => console.error("macro", e));
    futuresApi.latestPrice(symbol).then(setPrice).catch((e) => console.error("price", e));
  }, [symbol]);

  // 그리드 — 종목·비용·신호 설정 변경 시 재계산·최적 셀 자동선택.
  useEffect(() => {
    setGridLoading(true);
    setGrid(null);
    setGridError(null);
    futuresApi
      .grid(symbol, {
        commission,
        slippage_ticks: slippageTicks,
        roll_cost_pct: rollCost === "" ? 0 : rollCost / 100,
        min_gap_days: minGapDays,
      })
      .then((g) => {
        setGrid(g);
        const trusted = g.filter((c) => !c.low_sample && c.net_pnl_usd > 0)
                         .sort((a, b) => b.net_pnl_usd - a.net_pnl_usd);
        if (trusted.length) setSelected(trusted[0]);
      })
      .catch((e) => setGridError(e.message))
      .finally(() => setGridLoading(false));
  }, [symbol, commission, slippageTicks, rollCost, minGapDays]);

  useEffect(() => {
    if (!selected) return;
    setBtLoading(true);
    setBacktest(null);
    futuresApi
      .backtest(symbol, {
        side: selected.side,
        threshold: selected.threshold,
        horizon_days: selected.horizon,
        commission,
        slippage_ticks: slippageTicks,
        roll_cost_pct: rollCost === "" ? 0 : rollCost / 100,
        min_gap_days: minGapDays,
      })
      .then(setBacktest)
      .catch((e) => console.error("backtest", e))
      .finally(() => setBtLoading(false));
  }, [symbol, selected, commission, slippageTicks, rollCost, minGapDays]);

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
    if (!grid) return { short: [], long: [], horizons: [] as number[], max: 1 };
    // 색 스케일 기준 = 거래당 평균수익률(avg_return). 수익률에 정확히 비례하도록.
    const max = Math.max(1e-9, ...grid.map((c) => Math.abs(c.avg_return)));
    const horizons = [...new Set(grid.map((c) => c.horizon))].sort((a, b) => a - b);
    const byKey = new Map(grid.map((c) => [`${c.side}|${c.threshold}|${c.horizon}`, c] as const));
    const build = (side: "short" | "long") => {
      const ths = [...new Set(grid.filter((c) => c.side === side).map((c) => c.threshold))]
        .sort((a, b) => a - b);
      return ths.map((th) => {
        const cells = horizons.map((h) => byKey.get(`${side}|${th}|${h}`));
        const n = Math.max(0, ...cells.map((c) => c?.n_trades ?? 0));
        return { threshold: th, n, cells };
      });
    };
    return { short: build("short"), long: build("long"), horizons, max };
  }, [grid]);

  function runWalkForward() {
    setWfLoading(true);
    setWfError(null);
    futuresApi
      .walkforward(symbol, { split_date: splitDate })
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

  // 통화 인식 심볼 — KOSPI200 선물=KRW(₩, 가격은 접두 없음), 나머지 6개=USD($)
  const cur = info?.currency === "KRW" ? "₩" : "$";        // 금액(실현 P&L) 심볼
  const curCode = info?.currency ?? "USD";                  // "USD"/"KRW" 텍스트 라벨
  const priceSym = info?.currency === "KRW" ? "" : "$";     // 가격/임계(지수포인트) 접두

  return (
    <div className="oil-page">
      <header className="oil-header">
        <div className="oil-title-row">
          <div>
            <div className="oil-eyebrow">{info?.eyebrow ?? "FUTURES"}</div>
            <h1>{info?.name ?? "선물"} 분석</h1>
          </div>
          {price && <LivePriceTag price={price} priceSym={priceSym} />}
        </div>
        <div className="futures-selector">
          {instruments.map((it) => (
            <button
              key={it.symbol}
              className={it.symbol === symbol ? "active" : ""}
              onClick={() => setSymbol(it.symbol)}
            >
              {it.name}
            </button>
          ))}
        </div>
        <p className="muted">
          장중 high/low가 임계값을 첫 터치하면 신호 → N영업일 보유 백테스트.
        </p>
        <p className="oil-source-note">
          데이터: Yahoo Finance 최근월 선물 · 일배치 갱신 · front-month 롤 점프 포함
        </p>

      </header>

      {/* ① 데이터 메타 */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">DATA OVERVIEW · 데이터 메타</h2>
        {!info ? (
          <div className="muted">로딩 중…</div>
        ) : (
          <div className="meta-grid">
            <div><div className="muted">현재가 (일배치 종가)</div>
              <div className="meta-value">
                {price ? `${priceSym}${price.price.toFixed(2)}` : "—"}
                <span className="meta-unit">/{info?.unit ?? ""}</span>
                {price?.change_pct != null && (
                  <span className={"meta-delta " + (price.change_pct >= 0 ? "pos" : "neg")}>
                    {price.change_pct >= 0 ? "▲" : "▼"} {Math.abs(price.change_pct * 100).toFixed(2)}%
                  </span>
                )}
              </div>
              {price && (
                <div className="meta-source">
                  {price.source} · 일배치 종가
                </div>
              )}
            </div>
            <div><div className="muted">기간</div>
              <div className="meta-value meta-value-range">{info.start_date} ~ {info.end_date}</div></div>
            <div><div className="muted">영업일</div>
              <div className="meta-value">D+{info.n_rows.toLocaleString()} <span className="meta-sub-inline">(~{Math.round(info.n_rows / 252)}년)</span></div></div>
            <div><div className="muted">가격 범위 (~{Math.round(info.n_rows / 252)}년)</div>
              <div className="meta-value">{priceSym}{info.price_min.toFixed(2)} ~ {priceSym}{info.price_max.toFixed(2)}</div></div>
          </div>
        )}
      </section>

      {/* ② 히트맵 */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">PnL HEATMAP · 임계값 × 보유기간</h2>
        <p className="muted" style={{ marginBottom: 12 }}>
          셀: <b>거래당 평균수익률</b> / <b>승률</b> (소수점 1자리).
          색 진하기 = <b>거래당 평균수익률</b> 크기(수익률에 비례). <span style={{ color: "#62c884" }}>녹색=수익</span>,{" "}
          <span style={{ color: "#d96265" }}>빨강=손실</span>.{" "}
          low_sample(n&lt;30)은 <b>⚠</b>로만 표시(색 억제 없음 — 거래 적은 셀의 수익률은 노이즈일 수 있어 신중히). 클릭하면 아래 백테스트 상세.
        </p>

        {/* 전략 비용·신호 설정 — 히트맵 + 선택 셀 백테스트 양쪽에 일괄 적용 */}
        <div className="oil-toolbar sltp-toolbar">
          <span style={{ fontWeight: 600 }}>⚙ 설정:</span>
          <label title="진입+청산 양레그 계약당 수수료">
            수수료&nbsp;{cur}
            <input
              type="number" min={0} step={0.5} value={commission}
              onChange={(e) => setCommission(Math.max(0, Number(e.target.value) || 0))}
              style={{ width: 60 }}
            />
          </label>
          <label title="체결당 슬리피지(틱) — 진입/청산에 불리하게 적용">
            슬리피지&nbsp;
            <input
              type="number" min={0} step={1} value={slippageTicks}
              onChange={(e) => setSlippageTicks(Math.max(0, Number(e.target.value) || 0))}
              style={{ width: 52 }}
            />
            &nbsp;틱
          </label>
          <label title="만기 롤오버 비용(%/롤). 양수=콘탱고 비용, 음수=backwardation 이익. 추정 가정.">
            롤오버&nbsp;
            <input
              type="number" min={-5} max={5} step={0.1} value={rollCost} placeholder="0"
              onChange={(e) => setRollCost(e.target.value === "" ? "" : Number(e.target.value))}
              style={{ width: 60 }}
            />
            &nbsp;%
          </label>
          <label title="신호 발생 후 최소 M영업일 동안 같은 임계의 다른 신호 무시 — 반복신호 노이즈 제거">
            쿨타임&nbsp;
            <input
              type="number" min={0} max={250} step={1} value={minGapDays}
              onChange={(e) => setMinGapDays(Math.max(0, Number(e.target.value) || 0))}
              style={{ width: 52 }}
            />
            &nbsp;일
          </label>
          <button className="ghost" onClick={() => { setCommission(2.5); setSlippageTicks(1); setRollCost(""); setMinGapDays(0); }}>
            리셋
          </button>
          <span className="muted" style={{ fontSize: 12 }}>
            네 설정 모두 <b>히트맵·백테스트</b>에 일괄 적용 · 롤={info?.roll_note ?? "추정 가정"}.
          </span>
        </div>
        {/* 라디오 토글 — 한 번에 short 또는 long */}
        <div className="oil-radio-group">
          <label className={heatmapSide === "short" ? "active" : ""}>
            <input
              type="radio" name="heatmap-side" value="short"
              checked={heatmapSide === "short"}
              onChange={() => setHeatmapSide("short")}
            />
            Short (위로 첫 터치 → 매도)
          </label>
          <label className={heatmapSide === "long" ? "active" : ""}>
            <input
              type="radio" name="heatmap-side" value="long"
              checked={heatmapSide === "long"}
              onChange={() => setHeatmapSide("long")}
            />
            Long (아래로 첫 터치 → 매수)
          </label>
        </div>

        {gridLoading ? (
          <div className="muted">그리드 계산 중…</div>
        ) : gridError ? (
          <div className="error">{gridError}</div>
        ) : (
          <HeatmapBlock
            title={heatmapSide === "short"
              ? `Short — 위로 첫 터치 (${heatmaps.short.length}개 임계)`
              : `Long — 아래로 첫 터치 (${heatmaps.long.length}개 임계)`}
            rows={heatmapSide === "short" ? heatmaps.short : heatmaps.long}
            horizons={heatmaps.horizons}
            max={heatmaps.max}
            selected={selected}
            onSelect={setSelected}
            cur={cur}
            priceSym={priceSym}
          />
        )}

        {/* 선택 셀 백테스트 상세 (이전 ③ — 히트맵 섹션에 통합) */}
        <div style={{ marginTop: 22, paddingTop: 16, borderTop: "1px solid rgba(255,255,255,0.12)" }}>
          <h3 className="section-title" style={{ fontSize: 16 }}>
            백테스트 상세 {selected && <span className="title-tag">{selected.side.toUpperCase()} {priceSym}{selected.threshold} × {selected.horizon}D</span>}
            {selected && backtest && (
              <button
                className="export-btn"
                disabled={exporting}
                title="임계·보유기간·비용·롤·쿨타임을 엑셀에서 바꿔가며 재계산 (앱 결과와 일치)"
                onClick={async () => {
                  setExporting(true);
                  try {
                    await futuresApi.exportExcel(symbol, {
                      side: selected.side,
                      threshold: selected.threshold,
                      horizon_days: selected.horizon,
                      commission,
                      slippage_ticks: slippageTicks,
                      roll_cost_pct: rollCost === "" ? 0 : rollCost / 100,
                      min_gap_days: minGapDays,
                    });
                  } catch (e) {
                    alert("엑셀 내보내기 실패: " + (e as Error).message);
                  } finally {
                    setExporting(false);
                  }
                }}
              >
                {exporting ? "엑셀 생성 중…" : "📥 엑셀로 내보내기 (라이브 수식)"}
              </button>
            )}
          </h3>
          {!selected ? (
            <div className="muted">위 히트맵에서 한 셀을 클릭하면 상세가 표시됩니다.</div>
          ) : btLoading || !backtest ? (
            <div className="muted">백테스트 실행 중…</div>
          ) : (
            <BacktestDetail bt={backtest} side={selected.side} cur={cur} curCode={curCode} priceSym={priceSym} />
          )}
        </div>
      </section>

      {showArchivedSections && (
      <>
      {/* ④ 조합 순위표 — 헤더 클릭 정렬 + sticky + Profit/Loss 추가 */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">RANKING TABLE · 조합 순위표 <span className="title-tag">{gridSorted.length}</span></h2>
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
                <SortableTh k="mdd_usd" cur={sortKey} dir={sortDir} onClick={toggleSort}>MDD({cur})</SortableTh>
                <SortableTh k="gross_profit_usd" cur={sortKey} dir={sortDir} onClick={toggleSort}>Profit({cur})</SortableTh>
                <SortableTh k="gross_loss_usd" cur={sortKey} dir={sortDir} onClick={toggleSort}>Loss({cur})</SortableTh>
                <SortableTh k="net_pnl_usd" cur={sortKey} dir={sortDir} onClick={toggleSort}>Net PnL({cur})</SortableTh>
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
                    <td>{priceSym}{c.threshold}</td>
                    <td>{c.horizon}</td>
                    <td>{c.n_trades}</td>
                    <td>{pctNoSign(c.win_rate, 1)}</td>
                    <td className={c.avg_return >= 0 ? "pos" : "neg"}>{pct(c.avg_return, 2)}</td>
                    <td>{c.sharpe.toFixed(2)}</td>
                    <td>{c.profit_factor == null ? "∞" : c.profit_factor.toFixed(2)}</td>
                    <td className="neg">{money(c.mdd_usd, cur)}</td>
                    <td className="pos">{money(c.gross_profit_usd, cur)}</td>
                    <td className="neg">{money(c.gross_loss_usd, cur)}</td>
                    <td className={c.net_pnl_usd >= 0 ? "pos" : "neg"}>{money(c.net_pnl_usd, cur)}</td>
                    <td>{c.low_sample ? "⚠️" : ""}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>

      {/* ⑤ Walk-forward */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">WALK-FORWARD · 과최적화 검증</h2>
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
        {wf && <WalkForwardView wf={wf} cur={cur} priceSym={priceSym} />}
      </section>

      {/* ⑥ Seasonality */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">SEASONALITY · 계절성 패턴 (월별 / 요일별)</h2>
        {!season ? (
          <div className="muted">로딩 중…</div>
        ) : (
          <SeasonalityView data={season} />
        )}
      </section>

      {/* ⑦ Macro context (VIX·DXY) */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">MACRO CONTEXT · 외생 변수 (VIX · DXY)</h2>
        {!macro ? (
          <div className="muted">로딩 중…</div>
        ) : !macro.available ? (
          <div className="muted">VIX·달러지수 데이터 미수집 — 데이터 수집 후 이용 가능</div>
        ) : (
          <MacroView m={macro} />
        )}
      </section>
      </>
      )}

      {/* ⑧ 진입 추세 → 미래 수익률 탐색기 */}
      <section className="panel">
        <h2 className="section-title">TREND → FORWARD · 진입 추세 → 미래 수익률 탐색기</h2>
        <TrendExplorer symbol={symbol} priceSym={priceSym} />
      </section>
    </div>
  );
}

// 헤더 우측 현재가 태그 — 일배치 종가 (Bloomberg/TradingView 스타일)
function LivePriceTag({ price, priceSym }: { price: OilLatestPrice; priceSym: string }) {
  const up = (price.change_pct ?? 0) >= 0;
  return (
    <div className="live-price">
      <div className="live-price-main">
        <span className="live-price-val">{priceSym}{price.price.toFixed(2)}</span>
        {price.change_pct != null && (
          <span className={"live-price-chg " + (up ? "pos" : "neg")}>
            {up ? "▲" : "▼"} {price.change != null ? Math.abs(price.change).toFixed(2) : "—"}
            {" "}({Math.abs(price.change_pct * 100).toFixed(2)}%)
          </span>
        )}
      </div>
      <div className="live-price-src">
        <span className={"live-dot " + (price.delayed ? "delayed" : "live")} />
        {price.source} · 일배치 종가
      </div>
    </div>
  );
}

// BUY/SELL Scatter 점 — 적당한 원 + 흰 테두리 (가독성 ↑, 호버시 강조)
function buySellShape(color: string) {
  return function Shape(props: { cx?: number; cy?: number }) {
    if (props.cx == null || props.cy == null) return null;
    return (
      <g>
        {/* 바깥 흰 후광 — 점 분리감 ↑ */}
        <circle cx={props.cx} cy={props.cy} r={5.5} fill="#fff" opacity={0.85} />
        <circle
          cx={props.cx} cy={props.cy} r={4.5}
          fill={color} stroke="#fff" strokeWidth={1.5}
        />
      </g>
    );
  };
}

// 등 자산 차트 커스텀 tooltip — BUY/SELL Scatter일 때 보유일수·수익률 표시
type EquityTooltipPayload = {
  name?: string;
  value?: number;
  payload?: {
    date?: string;
    kind?: "BUY" | "SELL";
    days?: number;
    return_pct?: number;
    net_pnl_usd?: number;
    exit_reason?: string;
  };
};

function EquityTooltip({ active, payload, label, cur }: {
  active?: boolean;
  payload?: readonly EquityTooltipPayload[];
  label?: string;
  cur: string;
}) {
  if (!active || !payload || payload.length === 0) return null;
  // BUY/SELL Scatter 우선 — payload에 kind 있는 항목 찾기
  const ev = payload.find((p) => p.payload?.kind);
  if (ev && ev.payload) {
    const k = ev.payload.kind;
    const color = k === "BUY" ? "#3b82f6" : "#ef4444";
    const reasonKo = ev.payload.exit_reason === "stop_loss" ? "손절(SL)"
                  : ev.payload.exit_reason === "take_profit" ? "익절(TP)"
                  : "horizon 만기";
    return (
      <div className="chart-tooltip">
        <div style={{ color, fontWeight: 700 }}>● {k}</div>
        <div>{ev.payload.date}</div>
        <div className="muted" style={{ fontSize: 11 }}>
          보유: <b>{ev.payload.days} day</b>
        </div>
        <div className="muted" style={{ fontSize: 11 }}>
          수익률: <b style={{ color: (ev.payload.return_pct ?? 0) >= 0 ? "#16a34a" : "#dc2626" }}>
            {((ev.payload.return_pct ?? 0) >= 0 ? "+" : "") +
              (((ev.payload.return_pct ?? 0) * 100).toFixed(2))}%
          </b>
        </div>
        <div className="muted" style={{ fontSize: 11 }}>
          PnL: <b>{((ev.payload.net_pnl_usd ?? 0) >= 0 ? "+" : "-") + cur +
            Math.abs(ev.payload.net_pnl_usd ?? 0).toLocaleString("en-US", { maximumFractionDigits: 0 })}</b>
        </div>
        {k === "SELL" && <div className="muted" style={{ fontSize: 11 }}>청산사유: {reasonKo}</div>}
      </div>
    );
  }
  // 일반 line hover: 날짜 + 곡선 값들
  return (
    <div className="chart-tooltip">
      <div style={{ fontWeight: 600 }}>{label}</div>
      {payload.map((p, i) => (
        <div key={i} style={{ fontSize: 11 }}>
          {p.name}: {cur}
          {Number(p.value ?? 0).toLocaleString("en-US", { maximumFractionDigits: 0 })}
        </div>
      ))}
    </div>
  );
}

// ── Helper components: ExitReason 뱃지/요약 + Macro 뷰 ───────────
function ExitReasonBadge({ reason }: { reason: "horizon" | "stop_loss" | "take_profit" }) {
  if (reason === "stop_loss")
    return <span className="bs-badge bs-sell" title="손절(SL hit)">SL</span>;
  if (reason === "take_profit")
    return <span className="bs-badge bs-buy" title="익절(TP hit)">TP</span>;
  return <span className="bs-badge bs-horizon" title="horizon 만기 보유 종료">H</span>;
}

function ExitReasonSummary({ trades }: { trades: import("../api").OilTrade[] }) {
  const counts = { horizon: 0, stop_loss: 0, take_profit: 0 };
  for (const t of trades) counts[t.exit_reason]++;
  const total = trades.length || 1;
  if (counts.stop_loss === 0 && counts.take_profit === 0) {
    return (
      <div className="muted" style={{ marginTop: 8, fontSize: 12 }}>
        청산 사유: 전부 horizon 만기 보유 (SL/TP 비활성)
      </div>
    );
  }
  return (
    <div className="exit-summary">
      <span>
        <ExitReasonBadge reason="horizon" /> Horizon 만기:{" "}
        <b>{counts.horizon}</b> ({((counts.horizon / total) * 100).toFixed(0)}%)
      </span>
      <span>
        <ExitReasonBadge reason="stop_loss" /> 손절(SL):{" "}
        <b>{counts.stop_loss}</b> ({((counts.stop_loss / total) * 100).toFixed(0)}%)
      </span>
      <span>
        <ExitReasonBadge reason="take_profit" /> 익절(TP):{" "}
        <b>{counts.take_profit}</b> ({((counts.take_profit / total) * 100).toFixed(0)}%)
      </span>
    </div>
  );
}

function MacroView({ m }: { m: OilMacroContext }) {
  return (
    <>
      <p className="muted" style={{ marginBottom: 12 }}>
        종목 일간 수익률과 VIX(공포지수)/DXY(달러지수)의 관계 — 외생 변수가 신호 가치에
        주는 영향. 일별 종가 기준, <b>{m.coverage_days.toLocaleString()}</b>일 표본.
      </p>

      <div className="muted" style={{ fontSize: 13, marginBottom: 6 }}>
        상관관계 (Pearson, -1 ~ +1)
      </div>
      <table className="oil-table" style={{ marginBottom: 16 }}>
        <thead><tr><th>변수 쌍</th><th>Pearson r</th><th>방향</th></tr></thead>
        <tbody>
          {m.correlations.map((c) => (
            <tr key={c.pair}>
              <td>{c.pair}</td>
              <td className={c.pearson >= 0 ? "pos" : "neg"}>{c.pearson.toFixed(3)}</td>
              <td>
                {Math.abs(c.pearson) < 0.05
                  ? "거의 무상관"
                  : Math.abs(c.pearson) < 0.15
                    ? (c.pearson > 0 ? "약한 양" : "약한 음")
                    : (c.pearson > 0 ? "유의미 양" : "유의미 음")}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="season-grid">
        <RegimeTable title="VIX 체제별 평균 일간 수익률" rows={m.vix_regime} />
        <RegimeTable title="DXY(달러) 체제별 평균 일간 수익률" rows={m.dxy_regime} />
      </div>

      <div className="muted" style={{ fontSize: 12, marginTop: 10 }}>
        💡 해석: 고VIX 구간 평균수익이 명확히 음수면 "공포 구간 진입 회피" 필터,
        강달러 구간이 약하면 "약달러 시기만 long 진입" 필터를 신호에 추가하는 식의 전략 강화 가능.
        OPEC 회의 일정은 별도 캘린더 필요 → 추후 추가 예정.
      </div>
    </>
  );
}

function RegimeTable({ title, rows }: { title: string; rows: import("../api").OilMacroRegimeCell[] }) {
  return (
    <div>
      <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>{title}</div>
      <table className="oil-table">
        <thead>
          <tr><th>체제 구간</th><th>표본일수</th><th>평균수익</th><th>승률</th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.bucket}>
              <td>{r.bucket}</td>
              <td>{r.n_days.toLocaleString()}</td>
              <td className={r.avg_return >= 0 ? "pos" : "neg"}>
                {(r.avg_return >= 0 ? "+" : "") + (r.avg_return * 100).toFixed(3)}%
              </td>
              <td>{(r.win_rate * 100).toFixed(1)}%</td>
            </tr>
          ))}
        </tbody>
      </table>
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
  title, rows, horizons, max, selected, onSelect, cur, priceSym,
}: {
  title: string;
  rows: { threshold: number; n: number; cells: (OilGridCell | undefined)[] }[];
  horizons: number[];
  max: number;
  selected: OilGridCell | null;
  onSelect: (c: OilGridCell) => void;
  cur: string;
  priceSym: string;
}) {
  return (
    <div className="heatmap-block">
      <div className="heatmap-title">{title}</div>
      <div className="sticky-table" style={{ maxHeight: 400, overflow: "auto" }}>
        <table className="heatmap-table">
          <thead>
            <tr>
              <th></th>
              {horizons.map((h) => (
                <th key={h}>{h}일</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.threshold}>
                <th>{priceSym}{row.threshold} <span className="heat-n">(n={row.n})</span></th>
                {row.cells.map((c, i) => {
                  if (!c) return <td key={i} />;
                  const isSel =
                    selected?.side === c.side &&
                    selected?.threshold === c.threshold &&
                    selected?.horizon === c.horizon;
                  const bg = heatColor(c.avg_return, max);
                  return (
                    <td
                      key={i}
                      title={
                        `n=${c.n_trades}, 평균수익 ${pct(c.avg_return, 2)}, ` +
                        `승률 ${pctNoSign(c.win_rate, 1)}, Net ${money(c.net_pnl_usd, cur)}, ` +
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
function BacktestDetail({ bt, side, cur, curCode, priceSym }: {
  bt: OilBacktest;
  side: "short" | "long";
  cur: string;
  curCode: string;
  priceSym: string;
}) {
  const s = bt.summary;
  // Long: entry=BUY, exit=SELL.  Short: entry=SELL(공매), exit=BUY(환매).
  const entryLabel = side === "long" ? "BUY" : "SELL";
  const exitLabel = side === "long" ? "SELL" : "BUY";

  // 연도 필터 — "all" 또는 "YYYY"
  const [yearFilter, setYearFilter] = useState<string>("all");

  // 사용 가능한 연도 목록
  const availableYears = useMemo(() => {
    const ys = new Set<string>();
    for (const p of bt.portfolio_equity_curve) ys.add(p.date.slice(0, 4));
    return Array.from(ys).sort();
  }, [bt.portfolio_equity_curve]);

  // 필터 적용한 곡선·점
  const filtered = useMemo(() => {
    const matches = (d: string) =>
      yearFilter === "all" || d.startsWith(yearFilter);
    return {
      portfolio: bt.portfolio_equity_curve.filter((p) => matches(p.date)),
      realized: bt.equity_curve.filter((p) => matches(p.date)),
    };
  }, [bt.portfolio_equity_curve, bt.equity_curve, yearFilter]);

  // BUY/SELL 점 데이터: 시가평가 곡선의 entry/exit 시점 값 위에 dot 표시.
  // 연도 필터 적용 — 필터된 곡선 안에 진입/청산이 있는 trade만.
  const tradeDots = useMemo(() => {
    const map = new Map<string, number>();
    for (const p of filtered.portfolio) map.set(p.date, p.cumulative_usd);
    const dayDiff = (a: string, b: string) =>
      Math.round((new Date(b).getTime() - new Date(a).getTime()) / 86400000);
    const inRange = (d: string) =>
      yearFilter === "all" || d.startsWith(yearFilter);
    const buy = bt.trades
      .filter((t) => inRange(t.entry_date))
      .map((t) => ({
        date: t.entry_date,
        value: map.get(t.entry_date) ?? 0,
        kind: "BUY" as const,
        days: dayDiff(t.entry_date, t.exit_date),
        return_pct: t.return_pct,
        net_pnl_usd: t.net_pnl_usd,
        exit_reason: t.exit_reason,
      }));
    const sell = bt.trades
      .filter((t) => inRange(t.exit_date))
      .map((t) => ({
        date: t.exit_date,
        value: map.get(t.exit_date) ?? 0,
        kind: "SELL" as const,
        days: dayDiff(t.entry_date, t.exit_date),
        return_pct: t.return_pct,
        net_pnl_usd: t.net_pnl_usd,
        exit_reason: t.exit_reason,
      }));
    return { buy, sell };
  }, [bt.trades, filtered.portfolio, yearFilter]);

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
        <Metric label={`MDD (realized, ${curCode})`} value={money(s.mdd_usd, cur)} highlight="bad"
                sub="청산 시점 누적 PnL 곡선의 peak-trough" />
        <Metric label={`MDD (시가평가, ${curCode})`} value={money(bt.portfolio_mdd_usd, cur)} highlight="bad"
                sub="🅓 매일 mark-to-market 포트폴리오 가치 곡선의 peak-trough" />
        <Metric label={`Profit (${curCode})`} value={money(s.gross_profit_usd, cur)} highlight="good" />
        <Metric label={`Loss (${curCode})`} value={money(s.gross_loss_usd, cur)} highlight="bad" />
        <Metric label={`Net PnL (${curCode})`} value={money(s.net_pnl_usd, cur)}
                highlight={s.net_pnl_usd >= 0 ? "good" : "bad"} />
      </div>

      {/* 🅐 MAE/MFE 분석 (장중 평가손익) */}
      <div className="bt-subgrid">
        <div className="subgrid-title">🅐 장중 평가손익 (MAE/MFE) — 시가평가 위험 가시화</div>
        <div className="bt-metrics">
          <Metric
            label="Worst MAE (장중 최악)"
            value={money(s.worst_mae_usd, cur)}
            highlight="bad"
            sub="모든 trade 중 가장 깊은 평가손실 — 시가평가 MDD에 근접"
          />
          <Metric
            label="Avg MAE (평균 평가손실)"
            value={money(s.avg_mae_usd, cur)}
            highlight="bad"
            sub="거래당 평균 장중 최악 평가손실"
          />
          <Metric
            label="Avg MFE (평균 평가이익)"
            value={money(s.avg_mfe_usd, cur)}
            highlight="good"
            sub="평균 보유 중 최고점 — 익절 룰 설계 근거"
          />
        </div>
      </div>

      {/* 🅑 Streak */}
      <div className="bt-subgrid">
        <div className="subgrid-title">🅑 연속 streak — 심리·자금관리 척도</div>
        <div className="bt-metrics">
          <Metric label="최장 연승" value={s.max_win_streak} highlight="good" />
          <Metric
            label="최장 연패"
            value={s.max_loss_streak}
            highlight="bad"
            sub="이 만큼 연속으로 진 적 있음 — 자금 견딜지 검토"
          />
        </div>
      </div>

      {/* 🛢 선물 만기 강제 롤오버 */}
      <div className="bt-subgrid">
        <div className="subgrid-title">🛢 선물 만기 강제 롤오버 — 실물 인수도 회피</div>
        <div className="bt-metrics">
          <Metric
            label="총 롤오버 횟수"
            value={s.total_rollovers}
            sub={`전체 거래 합산 (trade당 평균 ${s.n_trades ? (s.total_rollovers / s.n_trades).toFixed(1) : 0}회)`}
          />
          <Metric
            label={`롤 손익 합계 (${curCode})`}
            value={money(s.total_roll_cost_usd, cur)}
            highlight={s.total_roll_cost_usd < 0 ? "bad" : s.total_roll_cost_usd > 0 ? "good" : null}
            sub={
              s.total_roll_cost_usd < 0 ? "contango 비용 — Net PnL에 차감 반영됨"
              : s.total_roll_cost_usd > 0 ? "backwardation 이익 — Net PnL에 가산 반영됨"
              : "롤 비용 0% (미적용 — 횟수만 표시)"
            }
          />
        </div>
        {s.total_roll_cost_usd !== 0 && (
          <div className="muted" style={{ fontSize: 11, marginTop: 6 }}>
            ⚠️ 롤 손익은 <b>추정 가정</b>입니다. 우리 데이터는 연속물 단일 시계열이라
            실제 근월/원월 가격차(term structure)가 없어, 정확한 contango/backwardation
            yield는 만기물별 데이터가 필요합니다.
          </div>
        )}
      </div>

      {s.low_sample && (
        <div className="warn-banner">
          ⚠️ 거래 수 {s.n_trades}건 (&lt;30) — 통계적 유의성 낮음. 평균이 좋아 보여도 신중히 해석.
        </div>
      )}

      <div style={{ marginTop: 16 }}>
        <div className="muted" style={{ fontSize: 13, marginBottom: 6 }}>
          등 자산 곡선 — <b style={{ color: "#62c884" }}>녹: realized (청산 시점)</b>{" "}
          vs <b style={{ color: "#6c9ce9" }}>파랑: 시가평가 (매일 MTM)</b>
          {" · "}
          <span style={{ color: "#3b82f6", fontWeight: 700 }}>● BUY</span>{" "}
          <span style={{ color: "#ef4444", fontWeight: 700 }}>● SELL</span>
        </div>

        {/* 연도 필터 + Brush 안내 */}
        <div className="oil-toolbar" style={{ marginBottom: 8 }}>
          <label>
            연도:&nbsp;
            <select
              value={yearFilter}
              onChange={(e) => setYearFilter(e.target.value)}
            >
              <option value="all">전체 ({availableYears.length}년)</option>
              {availableYears.map((y) => (
                <option key={y} value={y}>{y}년</option>
              ))}
            </select>
          </label>
          <button
            onClick={() => setYearFilter("all")}
            disabled={yearFilter === "all"}
            style={{
              padding: "4px 12px", fontSize: 12,
              opacity: yearFilter === "all" ? 0.5 : 1,
            }}
          >
            전체 보기
          </button>
          <span className="muted" style={{ fontSize: 11 }}>
            💡 차트 아래 회색 막대(Brush) 양끝을 끌어 zoom · 마우스 휠 안 됨 (드래그로 범위 조정)
          </span>
        </div>

        <ResponsiveContainer width="100%" height={340}>
          <ComposedChart data={filtered.portfolio}>
            <CartesianGrid strokeDasharray="3 3" stroke="#e5e5e5" />
            <XAxis dataKey="date" type="category" allowDuplicatedCategory={false}
                   tick={{ fontSize: 10, fill: "#9aa" }} minTickGap={50} />
            <YAxis tick={{ fontSize: 10, fill: "#9aa" }}
                   tickFormatter={(v) => (v / 1000).toFixed(0) + "k"} />
            <Tooltip content={({ active, payload, label }) => (
              <EquityTooltip
                active={active}
                payload={payload as readonly EquityTooltipPayload[] | undefined}
                label={label as string | undefined}
                cur={cur}
              />
            )} />
            <ReferenceLine y={0} stroke="#666" />
            {/* 시가평가 line — 부모 ComposedChart의 data 사용 */}
            <Line type="monotone" dataKey="cumulative_usd" name="시가평가(MTM)"
                  stroke="#6c9ce9" dot={false} strokeWidth={2} />
            {/* Realized line — 별도 data prop (sparse, exit 시점만) */}
            <Line data={filtered.realized} type="stepAfter"
                  dataKey="cumulative_usd" name="Realized"
                  stroke={s.net_pnl_usd >= 0 ? "#62c884" : "#d96265"}
                  dot={false} strokeWidth={2} />
            <Scatter data={tradeDots.buy} dataKey="value" name="BUY"
                     fill="#3b82f6" shape={buySellShape("#3b82f6")} />
            <Scatter data={tradeDots.sell} dataKey="value" name="SELL"
                     fill="#ef4444" shape={buySellShape("#ef4444")} />
            {/* Brush — 차트 하단 zoom slider. 양끝 traveller 드래그로 zoom */}
            <Brush
              dataKey="date"
              height={28}
              stroke="#6c9ce9"
              fill="rgba(108,156,233,0.08)"
              travellerWidth={10}
              tickFormatter={(v) => String(v).slice(0, 7)}
            />
          </ComposedChart>
        </ResponsiveContainer>
      </div>

      {/* 🅒 청산 사유 분포 (SL/TP 활성 시 의미) */}
      <ExitReasonSummary trades={bt.trades} />

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
                <th>청산사유</th>
                <th>수익률</th>
                <th title="보유 중 만기 통과(강제 롤오버) 횟수">롤</th>
                <th>MAE({cur})</th>
                <th>MFE({cur})</th>
                <th>Net PnL({cur})</th>
              </tr>
            </thead>
            <tbody>
              {bt.trades.map((t, i) => (
                <tr key={i}>
                  <td>{t.signal_date}</td>
                  <td>{t.entry_date}</td>
                  <td><span className={`bs-badge bs-${entryLabel.toLowerCase()}`}>{entryLabel}</span></td>
                  <td>{priceSym}{t.entry_price.toFixed(2)}</td>
                  <td>{t.exit_date}</td>
                  <td><span className={`bs-badge bs-${exitLabel.toLowerCase()}`}>{exitLabel}</span></td>
                  <td>{priceSym}{t.exit_price.toFixed(2)}</td>
                  <td><ExitReasonBadge reason={t.exit_reason} /></td>
                  <td className={t.return_pct >= 0 ? "pos" : "neg"}>{pct(t.return_pct, 2)}</td>
                  <td title={t.roll_cost_usd < 0 ? `롤 비용 ${money(t.roll_cost_usd, cur)}` : ""}>
                    {t.num_rollovers}{t.roll_cost_usd < 0 ? "🛢" : ""}
                  </td>
                  <td className="neg" title="장중 최악 평가손실">{money(t.mae_usd, cur)}</td>
                  <td className="pos" title="장중 최고 평가이익">{money(t.mfe_usd, cur)}</td>
                  <td className={t.net_pnl_usd >= 0 ? "pos" : "neg"}>{money(t.net_pnl_usd, cur)}</td>
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
function WalkForwardView({ wf, cur, priceSym }: { wf: OilWalkForward; cur: string; priceSym: string }) {
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
            <strong>Best in-sample</strong>: {b.side} {priceSym}{b.threshold} × {b.horizon}일
          </div>
          <SummaryGrid s={b.summary} cur={cur} />
        </div>
        <div className="wf-block">
          <div className="muted">Test (out-of-sample) 구간</div>
          <div>{wf.test_start} ~ {wf.test_end}</div>
          <div style={{ marginTop: 8 }}>
            <strong>같은 파라미터의 Test 결과</strong>
          </div>
          <SummaryGrid s={oos} cur={cur} />
        </div>
      </div>
      <div className="wf-badge" style={{ background: badge.color }}>{badge.text}</div>
    </div>
  );
}

function SummaryGrid({ s, cur }: { s: import("../api").OilSummary; cur: string }) {
  return (
    <div className="summary-grid">
      <div><span className="muted">n</span> {s.n_trades}</div>
      <div><span className="muted">승률</span> {pctNoSign(s.win_rate, 1)}</div>
      <div><span className="muted">평균수익</span> <span className={s.avg_return >= 0 ? "pos" : "neg"}>{pct(s.avg_return, 2)}</span></div>
      <div><span className="muted">Sharpe</span> {s.sharpe.toFixed(2)}</div>
      <div><span className="muted">Profit</span> <span className="pos">{money(s.gross_profit_usd, cur)}</span></div>
      <div><span className="muted">Loss</span> <span className="neg">{money(s.gross_loss_usd, cur)}</span></div>
      <div><span className="muted">Net PnL</span> <span className={s.net_pnl_usd >= 0 ? "pos" : "neg"}>{money(s.net_pnl_usd, cur)}</span></div>
      {s.low_sample && <div style={{ color: "#e6c259" }}>⚠ low sample</div>}
    </div>
  );
}

function Metric({
  label, value, highlight = null, sub = null,
}: {
  label: string;
  value: React.ReactNode;
  highlight?: "good" | "bad" | "warn" | null;
  sub?: string | null;
}) {
  const color = highlight === "good" ? "#62c884"
              : highlight === "bad" ? "#d96265"
              : highlight === "warn" ? "#e6c259"
              : undefined;
  return (
    <div className="metric-card">
      <div className="muted" style={{ fontSize: 12 }}>{label}</div>
      <div className="metric-value" style={{ color }}>{value}</div>
      {sub && <div className="metric-sub">{sub}</div>}
    </div>
  );
}

// ── 🅒 Seasonality 섹션 ─────────────────────────────────────────────
function SeasonalityView({ data }: { data: OilSeasonality }) {
  const monthly = data.monthly.map((c) => ({
    name: c.name,
    avg_return_pct: c.avg_return * 100,
    win_rate_pct: c.win_rate * 100,
    n_days: c.n_days,
  }));
  const weekday = data.weekday.map((c) => ({
    name: c.name,
    avg_return_pct: c.avg_return * 100,
    win_rate_pct: c.win_rate * 100,
    n_days: c.n_days,
  }));

  // 색: 양수 녹색 / 음수 빨강
  const barColor = (v: number) => (v >= 0 ? "#62c884" : "#d96265");

  return (
    <>
      <p className="muted" style={{ marginBottom: 12 }}>
        신호 무관 단순 통계 — 일간 종가-종가 수익률을 월별/요일별로 집계.
        구조적 약세 시즌(예: 10월 음수)·요일 효과 발견용.
      </p>
      <div className="season-grid">
        <div>
          <div className="muted" style={{ fontSize: 13, marginBottom: 6 }}>
            월별 평균 일간 수익률 (%)
          </div>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={monthly}>
              <CartesianGrid strokeDasharray="3 3" stroke="#333" />
              <XAxis dataKey="name" tick={{ fontSize: 11, fill: "#9aa" }} />
              <YAxis tick={{ fontSize: 10, fill: "#9aa" }} tickFormatter={(v) => v.toFixed(2)} />
              <Tooltip
                labelStyle={{ color: "#333" }}
                formatter={(v, _name, item) => {
                  const p = item.payload as { win_rate_pct: number; n_days: number };
                  return [
                    `${Number(v).toFixed(3)}% (승률 ${p.win_rate_pct.toFixed(1)}%, n=${p.n_days})`,
                    "평균 일간 수익률",
                  ];
                }}
              />
              <ReferenceLine y={0} stroke="#666" />
              <Bar dataKey="avg_return_pct">
                {monthly.map((m, i) => (
                  <Cell key={i} fill={barColor(m.avg_return_pct)} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
        <div>
          <div className="muted" style={{ fontSize: 13, marginBottom: 6 }}>
            요일별 평균 일간 수익률 (%)
          </div>
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={weekday}>
              <CartesianGrid strokeDasharray="3 3" stroke="#333" />
              <XAxis dataKey="name" tick={{ fontSize: 11, fill: "#9aa" }} />
              <YAxis tick={{ fontSize: 10, fill: "#9aa" }} tickFormatter={(v) => v.toFixed(2)} />
              <Tooltip
                labelStyle={{ color: "#333" }}
                formatter={(v, _name, item) => {
                  const p = item.payload as { win_rate_pct: number; n_days: number };
                  return [
                    `${Number(v).toFixed(3)}% (승률 ${p.win_rate_pct.toFixed(1)}%, n=${p.n_days})`,
                    "평균 일간 수익률",
                  ];
                }}
              />
              <ReferenceLine y={0} stroke="#666" />
              <Bar dataKey="avg_return_pct">
                {weekday.map((d, i) => (
                  <Cell key={i} fill={barColor(d.avg_return_pct)} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>
      <div className="season-tables">
        <SeasonTable title="월별 통계" rows={monthly} />
        <SeasonTable title="요일별 통계" rows={weekday} />
      </div>
    </>
  );
}

function SeasonTable({
  title, rows,
}: {
  title: string;
  rows: { name: string; avg_return_pct: number; win_rate_pct: number; n_days: number }[];
}) {
  return (
    <div>
      <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>{title}</div>
      <table className="oil-table">
        <thead>
          <tr>
            <th>구간</th>
            <th>평균수익</th>
            <th>승률</th>
            <th>표본일수</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.name}>
              <td>{r.name}</td>
              <td className={r.avg_return_pct >= 0 ? "pos" : "neg"}>
                {(r.avg_return_pct >= 0 ? "+" : "") + r.avg_return_pct.toFixed(3)}%
              </td>
              <td>{r.win_rate_pct.toFixed(1)}%</td>
              <td>{r.n_days.toLocaleString()}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── ⑧ 진입 추세 → 미래 수익률 탐색기 ──────────────────────────────────
// 서버가 (L,H) 이벤트 배열을 1회 내려주면, 증감율 밴드 필터·집계는 브라우저에서 실시간.
const TE_RET_BUCKETS = [
  { label: "≤−10%", lo: -Infinity, hi: -10 },
  { label: "−10~−3%", lo: -10, hi: -3 },
  { label: "−3~+3%", lo: -3, hi: 3 },
  { label: "+3~+10%", lo: 3, hi: 10 },
  { label: "≥+10%", lo: 10, hi: Infinity },
];

// 가격 범위를 ~12개로 나눌 "보기 좋은" 버킷 폭 (1·2·5·10 × 10ⁿ).
function teNiceStep(raw: number): number {
  if (raw <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const nice = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
  return nice * mag;
}

// ⑧ 진입 추세 → 미래 수익률 탐색기 (문장형 빈칸 + 현재값×증감율 히트맵).
// 서버 /trend-events(전체영업일)가 이벤트마다 {close, past_return, forward_return}를
// 내려주면, 현재값 버킷팅·증감율 밴드 필터·히트맵 집계는 전부 브라우저에서 실시간.
function TrendExplorer({ symbol, priceSym }: { symbol: string; priceSym: string }) {
  const [lookback, setLookback] = useState(20);
  const [horizon, setHorizon] = useState(60);
  const [data, setData] = useState<OilTrendEvents | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [currentN, setCurrentN] = useState<number | "">("");
  const [band, setBand] = useState<{ lo: number; hi: number } | null>(null);

  // 종목 변경 시 현재값 리셋 — 다음 데이터 로드에서 그 종목 최신 종가로 초기화.
  useEffect(() => { setCurrentN(""); }, [symbol]);

  // 전체영업일 이벤트 fetch (L·H 변경 시).
  useEffect(() => {
    setLoading(true);
    setErr(null);
    futuresApi
      .trendEvents(symbol, { lookback, horizon })
      .then(setData)
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
  }, [symbol, lookback, horizon]);

  const priceRange = useMemo(() => {
    if (!data || data.events.length === 0) return null;
    const cs = data.events.map((e) => e.close);
    return { min: Math.min(...cs), max: Math.max(...cs) };
  }, [data]);

  const pastRange = useMemo(() => {
    if (!data || data.events.length === 0) return null;
    const ps = data.events.map((e) => e.past_return * 100);
    return { min: Math.floor(Math.min(...ps)), max: Math.ceil(Math.max(...ps)) };
  }, [data]);

  // 가격 버킷(히트맵 행) — nice step, ~12개.
  const buckets = useMemo(() => {
    if (!priceRange) return null;
    const step = teNiceStep((priceRange.max - priceRange.min) / 12);
    const start = Math.floor(priceRange.min / step) * step;
    const count = Math.max(1, Math.round((Math.ceil(priceRange.max / step) * step - start) / step));
    const dec = step >= 10 ? 0 : step >= 1 ? 1 : step >= 0.1 ? 2 : 3;
    const arr = Array.from({ length: count }, (_, i) => ({
      lo: start + i * step, hi: start + (i + 1) * step, dec,
    }));
    return { arr, step, start };
  }, [priceRange]);

  // 데이터 로드 시: 현재값=최신 종가(미설정 시), 밴드=증감율 전체 범위.
  useEffect(() => {
    if (data && data.events.length) {
      const lastClose = data.events[data.events.length - 1].close;
      setCurrentN((prev) => (prev === "" ? Number(lastClose.toFixed(2)) : prev));
    }
    setBand(pastRange ? { lo: pastRange.min, hi: pastRange.max } : null);
  }, [data, pastRange]);

  const bucketIndex = (close: number) => {
    if (!buckets) return -1;
    return Math.max(0, Math.min(buckets.arr.length - 1,
      Math.floor((close - buckets.start) / buckets.step)));
  };
  const selBucket = currentN === "" ? -1 : bucketIndex(Number(currentN));

  // 문장 결과: 현재값 버킷 ∧ 증감율 밴드 → 평균 미래수익률.
  const sentence = useMemo(() => {
    if (!data || !band || !buckets || selBucket < 0) return null;
    const b = buckets.arr[selBucket];
    const m = data.events.filter(
      (e) => e.close >= b.lo && e.close < b.hi &&
        e.past_return * 100 >= band.lo && e.past_return * 100 <= band.hi,
    );
    const n = m.length;
    const mean = n ? m.reduce((s, e) => s + e.forward_return * 100, 0) / n : 0;
    const win = n ? (m.filter((e) => e.forward_return > 0).length / n) * 100 : 0;
    return { n, mean, win, bucket: b };
  }, [data, band, buckets, selBucket]);

  // 히트맵: 가격버킷(행) × 증감율구간(열) → 평균 미래수익률 + n.
  const heat = useMemo(() => {
    if (!data || !buckets) return null;
    const grid = buckets.arr.map(() => TE_RET_BUCKETS.map(() => ({ sum: 0, n: 0 })));
    for (const e of data.events) {
      const pi = bucketIndex(e.close);
      const pastPct = e.past_return * 100;
      const ri = TE_RET_BUCKETS.findIndex((rb) => pastPct >= rb.lo && pastPct < rb.hi);
      if (pi < 0 || ri < 0) continue;
      grid[pi][ri].sum += e.forward_return * 100;
      grid[pi][ri].n++;
    }
    let maxAbs = 1e-9;
    for (const row of grid) for (const c of row) if (c.n) maxAbs = Math.max(maxAbs, Math.abs(c.sum / c.n));
    return { grid, maxAbs };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, buckets]);

  const span = pastRange ? Math.max(1, pastRange.max - pastRange.min) : 1;

  // 회귀분석(과거 L일 × 미래 H일) — 백엔드 trend_regression(forward~past OLS+HAC) 사용.
  const reg = data?.regression ?? null;
  const scatterPts = useMemo(() => {
    if (!data) return [];
    const evs = data.events;
    const stride = Math.max(1, Math.ceil(evs.length / 600));
    const pts: { x: number; y: number }[] = [];
    for (let i = 0; i < evs.length; i += stride) {
      pts.push({ x: evs[i].past_return * 100, y: evs[i].forward_return * 100 });
    }
    return pts;
  }, [data]);
  const regSeg = useMemo<[{ x: number; y: number }, { x: number; y: number }] | null>(() => {
    if (!reg || !pastRange) return null;
    const yAt = (xp: number) => reg.slope * xp + reg.intercept * 100;
    return [
      { x: pastRange.min, y: yAt(pastRange.min) },
      { x: pastRange.max, y: yAt(pastRange.max) },
    ];
  }, [reg, pastRange]);

  return (
    <>
      <style>{`
        .te-blank{display:inline-flex;align-items:center;gap:2px;background:rgba(255,255,255,0.06);border-radius:6px;padding:2px 8px;margin:0 3px;font-weight:600}
        .te-blank input{width:58px;border:none;background:transparent;color:inherit;font:inherit;font-weight:600;text-align:center}
        .te-dr{position:relative;width:188px;height:26px;display:inline-block;vertical-align:middle}
        .te-dr-track{position:absolute;top:11px;left:0;right:0;height:4px;background:rgba(255,255,255,0.18);border-radius:2px}
        .te-dr-fill{position:absolute;top:11px;height:4px;background:#6c9ce9;border-radius:2px}
        .te-dr input[type=range]{position:absolute;top:0;left:0;width:100%;height:26px;margin:0;background:none;pointer-events:none;-webkit-appearance:none;appearance:none}
        .te-dr input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;pointer-events:auto;width:15px;height:15px;border-radius:50%;background:#6c9ce9;border:2px solid #0e1420;cursor:pointer}
        .te-dr input[type=range]::-moz-range-thumb{pointer-events:auto;width:15px;height:15px;border-radius:50%;background:#6c9ce9;border:2px solid #0e1420;cursor:pointer}
        .te-hm{border-collapse:separate;border-spacing:3px}
        .te-hm td,.te-hm th{padding:6px 8px;text-align:center;font-size:12px;white-space:nowrap}
        .te-hm th{color:#9aa;font-weight:500}
      `}</style>

      <p className="muted" style={{ marginBottom: 14 }}>
        현재 가격대와 진입 직전 추세에 따라 이후 수익률이 어떻게 갈리는지 — 아래 문장의 빈칸을 채우면 실시간 계산됩니다.
      </p>

      {loading || !data ? (
        <div className="muted">계산 중…</div>
      ) : err ? (
        <div className="error">{err}</div>
      ) : !data.events.length || !buckets || !band || !pastRange ? (
        <div className="muted">데이터가 충분하지 않습니다 — 과거/향후 기간을 조정하세요.</div>
      ) : (
        <>
          <div style={{ fontSize: 15, lineHeight: 2.5, marginBottom: 16 }}>
            현재 값이{" "}
            <span className="te-blank">
              {priceSym}
              <input type="number" value={currentN}
                onChange={(e) => setCurrentN(e.target.value === "" ? "" : Number(e.target.value))} />
            </span>{" "}
            부근일 때, 과거{" "}
            <span className="te-blank">
              <input type="number" min={1} max={250} value={lookback}
                onChange={(e) => setLookback(Math.max(1, Number(e.target.value) || 1))} />일
            </span>{" "}
            동안 증감율이{" "}
            <span className="te-blank" style={{ padding: "2px 8px" }}>
              <span style={{ width: 44, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                {(band.lo >= 0 ? "+" : "") + band.lo}%
              </span>
              <span className="te-dr" style={{ margin: "0 8px" }}>
                <span className="te-dr-track" />
                <span className="te-dr-fill" style={{
                  left: ((band.lo - pastRange.min) / span) * 100 + "%",
                  width: ((band.hi - band.lo) / span) * 100 + "%",
                }} />
                <input type="range" min={pastRange.min} max={pastRange.max} step={1} value={band.lo}
                  onChange={(e) => setBand((b) => (b ? { ...b, lo: Math.min(Number(e.target.value), b.hi) } : b))} />
                <input type="range" min={pastRange.min} max={pastRange.max} step={1} value={band.hi}
                  onChange={(e) => setBand((b) => (b ? { ...b, hi: Math.max(Number(e.target.value), b.lo) } : b))} />
              </span>
              <span style={{ width: 44, fontVariantNumeric: "tabular-nums" }}>
                {(band.hi >= 0 ? "+" : "") + band.hi}%
              </span>
            </span>{" "}
            였다면, 향후{" "}
            <span className="te-blank">
              <input type="number" min={1} max={500} value={horizon}
                onChange={(e) => setHorizon(Math.max(1, Number(e.target.value) || 1))} />일
            </span>{" "}
            후{" "}
            {sentence && sentence.n > 0 ? (
              <b>평균 수익률 ={" "}
                <span className={sentence.mean >= 0 ? "pos" : "neg"} style={{ fontSize: 17 }}>
                  {(sentence.mean >= 0 ? "+" : "") + sentence.mean.toFixed(2)}%
                </span>
              </b>
            ) : (
              <b className="muted">해당 조건 표본 없음</b>
            )}
            {sentence && (
              <span className="muted" style={{ fontSize: 13 }}>
                {" "}(이 구간 {priceSym}{sentence.bucket.lo.toFixed(sentence.bucket.dec)}–{priceSym}{sentence.bucket.hi.toFixed(sentence.bucket.dec)},
                n={sentence.n}{sentence.n > 0 ? `, 승률 ${sentence.win.toFixed(0)}%` : ""}
                {sentence.n > 0 && sentence.n < 30 ? " ⚠" : ""})
              </span>
            )}
          </div>

          <button className="ghost" style={{ marginBottom: 14 }}
            onClick={() => setBand({ lo: pastRange.min, hi: pastRange.max })}>
            증감율 밴드 전체로
          </button>

          <div className="muted" style={{ fontSize: 13, margin: "6px 0 8px" }}>
            현재값별 수익률 히트맵 — 행=현재 가격대, 열=진입 직전 증감율, 셀=향후 {horizon}일 평균수익률.{" "}
            <span style={{ color: "#62c884" }}>녹=상승</span> / <span style={{ color: "#d96265" }}>빨강=하락</span>,
            n&lt;30은 ⚠. 선택한 현재값 행은 ◀.
          </div>
          {heat && (
            <div className="table-scroll sticky-table" style={{ maxHeight: 460 }}>
              <table className="te-hm">
                <thead>
                  <tr>
                    <th style={{ textAlign: "left" }}>현재값</th>
                    {TE_RET_BUCKETS.map((rb) => <th key={rb.label}>{rb.label}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {buckets.arr.map((b, pi) => (
                    <tr key={pi}>
                      <th style={{ textAlign: "left", color: pi === selBucket ? "#6c9ce9" : undefined }}>
                        {priceSym}{b.lo.toFixed(b.dec)}–{priceSym}{b.hi.toFixed(b.dec)}{pi === selBucket ? " ◀" : ""}
                      </th>
                      {heat.grid[pi].map((c, ri) => {
                        const mean = c.n ? c.sum / c.n : NaN;
                        return (
                          <td key={ri}
                            title={c.n ? `n=${c.n}, 평균 ${(mean >= 0 ? "+" : "") + mean.toFixed(2)}%` : "표본 없음"}
                            style={{
                              background: c.n ? heatColor(mean, heat.maxAbs) : "#1f2937",
                              color: "#fff", borderRadius: 5, opacity: c.n ? 1 : 0.25,
                              outline: pi === selBucket ? "1.5px solid rgba(255,255,255,0.55)" : "none",
                            }}>
                            {c.n ? (
                              <>
                                <div style={{ fontWeight: 600 }}>{(mean >= 0 ? "+" : "") + mean.toFixed(1)}%</div>
                                <div style={{ fontSize: 10, opacity: 0.85 }}>n={c.n}{c.n < 30 ? " ⚠" : ""}</div>
                              </>
                            ) : "·"}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {reg && (
            <div style={{ marginTop: 20, paddingTop: 16, borderTop: "1px solid rgba(255,255,255,0.1)" }}>
              <div className="muted" style={{ fontSize: 13, marginBottom: 8 }}>
                회귀분석 — 과거 <b>{lookback}일</b> 증감율(x) → 미래 <b>{horizon}일</b> 수익률(y) · 전체 영업일 {reg.n.toLocaleString()}건
              </div>
              <div className="bt-metrics" style={{ marginBottom: 10 }}>
                <Metric label="기울기 β" value={(reg.slope >= 0 ? "+" : "") + reg.slope.toFixed(2)}
                  highlight={reg.slope >= 0 ? "good" : "bad"}
                  sub={reg.slope >= 0 ? "모멘텀(추세↑→미래수익↑)" : "반전(추세↑→미래수익↓)"} />
                <Metric label="R²" value={reg.r_squared.toFixed(3)} sub="설명력(0~1)" />
                <Metric label="p-value (HAC)" value={reg.hac_p_value.toFixed(3)}
                  highlight={reg.hac_p_value < 0.05 ? "good" : "warn"} sub="겹침보정 후 유의성" />
                <Metric label="표본 n" value={reg.n} highlight={reg.n < 30 ? "warn" : null} />
              </div>
              <ResponsiveContainer width="100%" height={300}>
                <ScatterChart margin={{ top: 8, right: 16, bottom: 16, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#333" />
                  <XAxis type="number" dataKey="x" name="과거 증감율" domain={["dataMin", "dataMax"]}
                    tick={{ fontSize: 10, fill: "#9aa" }} tickFormatter={(v) => v + "%"} />
                  <YAxis type="number" dataKey="y" name="미래 수익률"
                    tick={{ fontSize: 10, fill: "#9aa" }} tickFormatter={(v) => v + "%"} />
                  <Tooltip cursor={{ strokeDasharray: "3 3" }} formatter={(v) => `${Number(v).toFixed(2)}%`} />
                  <ReferenceLine y={0} stroke="#666" />
                  {regSeg && <ReferenceLine segment={regSeg} stroke="#e6c259" strokeWidth={2} />}
                  <Scatter data={scatterPts} fill="#6c9ce9" fillOpacity={0.5} />
                </ScatterChart>
              </ResponsiveContainer>
              <div className="muted" style={{ fontSize: 11, marginTop: 8 }}>
                ⚠️ forward 윈도우가 겹쳐 자기상관 → p값은 <b>Newey-West(HAC, maxlags=H)</b>로 보정.
                회귀선(노란색)은 전체 영업일 기준 추세→수익 관계입니다.
              </div>
            </div>
          )}

          <div className="muted" style={{ fontSize: 11, marginTop: 12 }}>
            ⚠️ 수익률은 <b>종가-종가 서술용</b>(실제 백테스트의 익일 시가 진입·비용·SL/TP와 다름 — 관계 측정용).
            현재값 구간·증감율 밴드를 좁게 잡을수록 표본이 줄어 통계가 불안정해집니다(n&lt;30 ⚠).
          </div>
        </>
      )}
    </>
  );
}

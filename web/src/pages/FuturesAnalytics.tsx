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
  ReferenceArea,
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
  type OilPricePoint,
  type OilSeasonality,
  type OilSweepAxis,
  type OilTrendEvents,
  type OilTrendScan,
  type OilTrendScanCell,
  type OilTrendSweep,
  type OilWalkForward,
} from "../api";

// 색 스케일: 음수→빨강, 양수→녹색. 진하기 = |거래당 평균수익률| / 그리드 최대.
// 색은 수익률에만 비례 — 샘플수 채도 억제 없음(저샘플은 ⚠·툴팁으로만 경고).
function heatColor(v: number, max: number): string {
  if (!Number.isFinite(v) || max <= 0) return "#f1efe8";
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

  const [exporting, setExporting] = useState(false);          // 엑셀 내보내기 진행중

  // 전략 비용·신호 설정 — applied=그리드/백테스트가 실제 사용하는 값, draft=입력칸 값.
  // [확인] 클릭 시 draft→applied (라이브 재계산 대신 명시적 적용).
  const [commission, setCommission] = useState(2.5);     // 수수료 (적용값)
  const [slippageTicks, setSlippageTicks] = useState(1); // 슬리피지 (적용값)
  const [rollCost, setRollCost] = useState<number | "">("");  // 롤 비용 %/회 (적용값)
  const [minGapDays, setMinGapDays] = useState(0);       // 신호 쿨타임 (적용값)
  const [dCommission, setDCommission] = useState(2.5);   // 입력 draft
  const [dSlippage, setDSlippage] = useState(1);
  const [dRoll, setDRoll] = useState<number | "">("");
  const [dMinGap, setDMinGap] = useState(0);

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

  // 입력(draft)이 적용값과 다르면 [확인] 활성화.
  const settingsDirty =
    dCommission !== commission || dSlippage !== slippageTicks ||
    dRoll !== rollCost || dMinGap !== minGapDays;

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
          색 진하기 = <b>거래당 평균수익률</b> 크기(수익률에 비례). <span style={{ color: "#15803d", fontWeight: 600 }}>녹색=수익</span>,{" "}
          <span style={{ color: "#b91c1c", fontWeight: 600 }}>빨강=손실</span>.{" "}
          low_sample(n&lt;30)은 <b>⚠</b>로만 표시(색 억제 없음 — 거래 적은 셀의 수익률은 노이즈일 수 있어 신중히). 클릭하면 아래 백테스트 상세.
        </p>

        {/* 전략 비용·신호 설정 — 입력 후 [확인]을 눌러야 히트맵·백테스트에 적용 */}
        <div className="oil-toolbar sltp-toolbar">
          <span style={{ fontWeight: 600 }}>⚙ 설정:</span>
          <label title="진입+청산 양레그 계약당 수수료">
            수수료&nbsp;{cur}
            <input
              type="number" min={0} step={0.5} value={dCommission}
              onChange={(e) => setDCommission(Math.max(0, Number(e.target.value) || 0))}
              style={{ width: 60 }}
            />
          </label>
          <label title="체결당 슬리피지(틱) — 진입/청산에 불리하게 적용">
            슬리피지&nbsp;
            <input
              type="number" min={0} step={1} value={dSlippage}
              onChange={(e) => setDSlippage(Math.max(0, Number(e.target.value) || 0))}
              style={{ width: 52 }}
            />
            &nbsp;틱
          </label>
          <label title="만기 롤오버 비용(%/롤). 양수=콘탱고 비용, 음수=backwardation 이익. 추정 가정.">
            롤오버&nbsp;
            <input
              type="number" min={-5} max={5} step={0.1} value={dRoll} placeholder="0"
              onChange={(e) => setDRoll(e.target.value === "" ? "" : Number(e.target.value))}
              style={{ width: 60 }}
            />
            &nbsp;%
          </label>
          <label title="신호 발생 후 최소 M영업일 동안 같은 임계의 다른 신호 무시 — 반복신호 노이즈 제거">
            쿨타임&nbsp;
            <input
              type="number" min={0} max={250} step={1} value={dMinGap}
              onChange={(e) => setDMinGap(Math.max(0, Number(e.target.value) || 0))}
              style={{ width: 52 }}
            />
            &nbsp;일
          </label>
          <button
            className={settingsDirty ? "" : "ghost"}
            disabled={!settingsDirty}
            title="입력한 4개 설정을 히트맵·백테스트에 적용"
            onClick={() => {
              setCommission(dCommission); setSlippageTicks(dSlippage);
              setRollCost(dRoll); setMinGapDays(dMinGap);
            }}
          >
            확인
          </button>
          <span className="muted" style={{ fontSize: 12 }}>
            {settingsDirty ? (
              <b style={{ color: "var(--amber)" }}>미적용 변경 있음 — [확인]을 눌러 반영</b>
            ) : (
              <>네 설정 모두 <b>히트맵·백테스트</b>에 적용됨 · 롤={info?.roll_note ?? "추정 가정"}.</>
            )}
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
        <div style={{ marginTop: 22, paddingTop: 16, borderTop: "1px solid var(--border)" }}>
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
        <h2 className="section-title">TREND → FORWARD · 진입 추세 → 향후 종가 증감율 탐색기</h2>
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
  // 자산곡선·개별거래는 현재 불필요 → 숨김(코드 보존, 필요시 true). 사용자 요청 2026-06-14.
  const showBtChartAndTrades = false;
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
        <Metric label="롤오버 횟수" value={s.total_rollovers}
                sub={`선물 만기 강제 롤 합산 (trade당 평균 ${s.n_trades ? (s.total_rollovers / s.n_trades).toFixed(1) : 0}회)`} />
      </div>

      {s.low_sample && (
        <div className="warn-banner">
          ⚠️ 거래 수 {s.n_trades}건 (&lt;30) — 통계적 유의성 낮음. 평균이 좋아 보여도 신중히 해석.
        </div>
      )}

      {showBtChartAndTrades && (
      <>
      <div style={{ marginTop: 16 }}>
        <div className="muted" style={{ fontSize: 13, marginBottom: 6 }}>
          등 자산 곡선 — <b style={{ color: "#15803d" }}>녹: realized (청산 시점)</b>{" "}
          vs <b style={{ color: "#d97757" }}>주황: 시가평가 (매일 MTM)</b>
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
            <CartesianGrid strokeDasharray="3 3" stroke="#e8e3db" />
            <XAxis dataKey="date" type="category" allowDuplicatedCategory={false}
                   tick={{ fontSize: 10, fill: "#6f6a62" }} minTickGap={50} />
            <YAxis tick={{ fontSize: 10, fill: "#6f6a62" }}
                   tickFormatter={(v) => (v / 1000).toFixed(0) + "k"} />
            <Tooltip content={({ active, payload, label }) => (
              <EquityTooltip
                active={active}
                payload={payload as readonly EquityTooltipPayload[] | undefined}
                label={label as string | undefined}
                cur={cur}
              />
            )} />
            <ReferenceLine y={0} stroke="#6f6a62" />
            {/* 시가평가 line — 부모 ComposedChart의 data 사용 */}
            <Line type="monotone" dataKey="cumulative_usd" name="시가평가(MTM)"
                  stroke="#d97757" dot={false} strokeWidth={2} />
            {/* Realized line — 별도 data prop (sparse, exit 시점만) */}
            <Line data={filtered.realized} type="stepAfter"
                  dataKey="cumulative_usd" name="Realized"
                  stroke={s.net_pnl_usd >= 0 ? "#15803d" : "#b91c1c"}
                  dot={false} strokeWidth={2} />
            <Scatter data={tradeDots.buy} dataKey="value" name="BUY"
                     fill="#3b82f6" shape={buySellShape("#3b82f6")} />
            <Scatter data={tradeDots.sell} dataKey="value" name="SELL"
                     fill="#ef4444" shape={buySellShape("#ef4444")} />
            {/* Brush — 차트 하단 zoom slider. 양끝 traveller 드래그로 zoom */}
            <Brush
              dataKey="date"
              height={28}
              stroke="#d97757"
              fill="rgba(217,119,87,0.08)"
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
      )}
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
  const color = highlight === "good" ? "var(--green)"
              : highlight === "bad" ? "var(--red)"
              : highlight === "warn" ? "var(--amber)"
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
              <CartesianGrid strokeDasharray="3 3" stroke="#e8e3db" />
              <XAxis dataKey="name" tick={{ fontSize: 11, fill: "#6f6a62" }} />
              <YAxis tick={{ fontSize: 10, fill: "#6f6a62" }} tickFormatter={(v) => v.toFixed(2)} />
              <Tooltip
                labelStyle={{ color: "#646464" }}
                formatter={(v, _name, item) => {
                  const p = item.payload as { win_rate_pct: number; n_days: number };
                  return [
                    `${Number(v).toFixed(3)}% (승률 ${p.win_rate_pct.toFixed(1)}%, n=${p.n_days})`,
                    "평균 일간 수익률",
                  ];
                }}
              />
              <ReferenceLine y={0} stroke="#6f6a62" />
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
              <CartesianGrid strokeDasharray="3 3" stroke="#e8e3db" />
              <XAxis dataKey="name" tick={{ fontSize: 11, fill: "#6f6a62" }} />
              <YAxis tick={{ fontSize: 10, fill: "#6f6a62" }} tickFormatter={(v) => v.toFixed(2)} />
              <Tooltip
                labelStyle={{ color: "#646464" }}
                formatter={(v, _name, item) => {
                  const p = item.payload as { win_rate_pct: number; n_days: number };
                  return [
                    `${Number(v).toFixed(3)}% (승률 ${p.win_rate_pct.toFixed(1)}%, n=${p.n_days})`,
                    "평균 일간 수익률",
                  ];
                }}
              />
              <ReferenceLine y={0} stroke="#6f6a62" />
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

// ── ⑧ 진입 추세 → 향후 종가 증감율 탐색기 ──────────────────────────────
// 서버가 (L,H) 이벤트 배열을 1회 내려주면, 증감율 밴드 필터·집계는 브라우저에서 실시간.

// TREND→FORWARD 탐색기 입력 기본값 (단일 출처).
const TE_DEF_LOOKBACK = 90;   // 과거 N일
const TE_DEF_HORIZON = 120;   // 향후 N일
const TE_DEF_GAP = 60;        // 이벤트 최소 간격(영업일)
const TE_BAND_PCT = 0.06;     // 종가 밴드 반폭 = 현재가 × 6% → nice 단위 반올림(종목 스케일 자동대응)

// 1·2·5×10ⁿ 중 가장 가까운 "깔끔한" 단위로 — 종가 밴드폭·반올림 단위 산정용.
// 종목 스케일에 자동 대응(오일 5·천연가스 0.2·금 200·비트코인 5000 …). 오일 6%≈5.04 → 5.
function teNiceUnit(raw: number): number {
  if (raw <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const nice = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
  return nice * mag;
}

type TeMetric = "ret" | "up" | "r2" | "n";

// (L,H) 지도·2D 스윕 공용 셀 스타일 — 지표별 색·텍스트. 수익률·상승비율=발산(수익/높음=빨강·
// 손실/낮음=파랑, 한국관례), R²·표본수=오렌지 농도. lowConf면 알파 상한↓. maxAbs/maxPos는
// 호출측이 고신뢰 칸 기준으로 계산한 정규화 분모.
function teMetricCell(metric: TeMetric, v: number, lowConf: boolean, maxPos: number, maxAbs: number):
    { bg: string; txt: string; fg: string } {
  const cap = lowConf ? 0.4 : 0.85;
  let a: number, bg: string, txt: string;
  if (metric === "ret") {
    a = Math.min(cap, (Math.abs(v) / maxAbs) * 0.85);
    bg = v >= 0 ? `rgba(222,48,51,${a})` : `rgba(22,104,196,${a})`;
    txt = (v >= 0 ? "+" : "") + v.toFixed(1) + "%";
  } else if (metric === "up") {
    a = Math.min(cap, (Math.abs(v - 50) / 50) * 0.85);
    bg = v >= 50 ? `rgba(222,48,51,${a})` : `rgba(22,104,196,${a})`;
    txt = v.toFixed(0) + "%";
  } else {  // r2 | n — 오렌지 농도
    a = Math.min(cap, (v / maxPos) * 0.85);
    bg = `rgba(217,119,87,${a})`;
    txt = metric === "n" ? String(v) : v.toFixed(2);
  }
  return { bg, txt, fg: a > 0.55 ? "#fff" : "var(--text)" };
}

// 2D 스윕 축 옵션 (라벨·코너 약칭).
const TE_SWEEP_AXES: { v: OilSweepAxis; label: string; short: string }[] = [
  { v: "close", label: "종가 범위", short: "종가" },
  { v: "change", label: "증감율 범위", short: "증감율" },
  { v: "lookback", label: "과거 L", short: "과거L" },
  { v: "horizon", label: "향후 H", short: "향후H" },
];

// 매칭 조건의 평균 향후 종가 증감율에 따른 한줄 전망.
// G영업일 이내 연속/겹친 이벤트는 1건만(그리디). events 는 날짜 오름차순 가정.
function teDeclusterByGap<T extends { date: string }>(events: T[], gap: number, dateIdx: Map<string, number>): T[] {
  if (gap <= 0) return events;
  const kept: T[] = [];
  let last = -Infinity;
  for (const e of events) {
    const i = dateIdx.get(e.date);
    if (i === undefined) continue;
    if (i - last >= gap) { kept.push(e); last = i; }
  }
  return kept;
}

// 정규근사 누적분포용 erf (Abramowitz-Stegun 7.1.26) — 백엔드 _normal_cdf(math.erf)과 동일 정의.
function teErf(x: number): number {
  const s = x < 0 ? -1 : 1, ax = Math.abs(x);
  const t = 1 / (1 + 0.3275911 * ax);
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * Math.exp(-ax * ax);
  return s * y;
}

// 단순 OLS(forward~past) — 디클러스터(독립 표본) 위라 HAC 불필요. 정규근사 양측 p.
function teOls(pts: { x: number; y: number }[]): { slope: number; intercept: number; r2: number; n: number; p: number } | null {
  const n = pts.length;
  if (n < 3) return null;
  let sx = 0, sy = 0;
  for (const p of pts) { sx += p.x; sy += p.y; }
  const mx = sx / n, my = sy / n;
  let sxx = 0, sxy = 0, syy = 0;
  for (const p of pts) { const dx = p.x - mx, dy = p.y - my; sxx += dx * dx; sxy += dx * dy; syy += dy * dy; }
  if (sxx <= 0) return null;
  const slope = sxy / sxx, intercept = my - slope * mx;
  let sse = 0;
  for (const p of pts) { const e = p.y - (intercept + slope * p.x); sse += e * e; }
  const r2 = syy > 0 ? 1 - sse / syy : 0;
  const se = Math.sqrt((sse / (n - 2)) / sxx);
  const t = se > 0 ? slope / se : 0;
  const p = 2 * (1 - 0.5 * (1 + teErf(Math.abs(t) / Math.SQRT2)));
  return { slope, intercept, r2, n, p };
}

function teOpinion(mean: number): { txt: string; cls: string } {
  if (mean >= 4) return { txt: "📈 강한 상승 경향", cls: "pos" };
  if (mean >= 1) return { txt: "📈 상승 경향", cls: "pos" };
  if (mean <= -4) return { txt: "📉 강한 하락 경향", cls: "neg" };
  if (mean <= -1) return { txt: "📉 하락 경향", cls: "neg" };
  return { txt: "⚖ 중립 (뚜렷한 방향성 없음)", cls: "muted" };
}

// ⑧ 진입 추세 → 향후 종가 증감율 탐색기.
// 폼(종가범위·과거L·증감율범위·미래H·이벤트최소간격)을 채우고 [확인]을 누르면 계산: 문장 결과+전망 /
// 전체기간 가격차트(매칭 구간 음영) / 종가대×증감율 히트맵 / 회귀(과거×미래).
// /trend-events(이벤트) + /prices(전체 가격 시계열)을 브라우저에서 집계.
function TrendExplorer({ symbol, priceSym }: { symbol: string; priceSym: string }) {
  // 입력 draft (확인 전엔 계산 안 함)
  const [dNLo, setDNLo] = useState<number | "">("");   // 종가 하한
  const [dNHi, setDNHi] = useState<number | "">("");   // 종가 상한
  const [dL, setDL] = useState(TE_DEF_LOOKBACK);
  const [dH, setDH] = useState(TE_DEF_HORIZON);
  const [dLo, setDLo] = useState<number | "">("");     // 과거 증감율 하한
  const [dHi, setDHi] = useState<number | "">("");     // 과거 증감율 상한
  const [dGap, setDGap] = useState(TE_DEF_GAP);        // 이벤트 최소 간격(영업일) — 클러스터/겹침 디클러스터. 기본 60: 독립 표본 확보. (L,H) 지도는 저표본도 그리므로(n≥3) 60이어도 안 비고 저신뢰 음영으로 표시됨
  // (L,H) 지도 셀 지표 — 기본은 사용자가 최적화하는 '평균 향후수익률'.
  const [scanMetric, setScanMetric] = useState<TeMetric>("ret");
  // 2D 스윕 — 행/열 축 선택(나머지 2축은 현재 입력 고정) + 지표.
  const [sweepRowAxis, setSweepRowAxis] = useState<OilSweepAxis>("close");
  const [sweepColAxis, setSweepColAxis] = useState<OilSweepAxis>("horizon");
  const [sweepMetric, setSweepMetric] = useState<TeMetric>("ret");
  const [sweep, setSweep] = useState<OilTrendSweep | null>(null);
  const [sweepLoading, setSweepLoading] = useState(false);
  const [sweepError, setSweepError] = useState<string | null>(null);

  // 적용된 설정 (확인 클릭 시 스냅샷)
  const [applied, setApplied] = useState<
    { nLo: number; nHi: number; L: number; H: number; lo: number; hi: number; gap: number } | null
  >(null);
  const [data, setData] = useState<OilTrendEvents | null>(null);
  const [prices, setPrices] = useState<OilPricePoint[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [scan, setScan] = useState<OilTrendScan | null>(null);   // (L,H) 설명력 격자
  const [scanLoading, setScanLoading] = useState(false);
  const [teExporting, setTeExporting] = useState(false);   // 엑셀 내보내기 진행중

  // 종목 변경 → 전체 가격 시계열 로드 + draft 종가범위=현재가 부근 버킷, 적용 리셋.
  useEffect(() => {
    setApplied(null);
    setData(null);
    setPrices([]);
    setScan(null);
    setDNLo("");
    setDNHi("");
    futuresApi.prices(symbol)
      .then((p) => {
        setPrices(p);
        if (p.length) {
          const last = p[p.length - 1].close;
          // 종가 범위 기본값 = 현재가 ± (현재가×6%를 nice 단위로 반올림). 종목 스케일 자동대응
          // (오일≈±5·천연가스±0.2·금±200·비트코인±5000 …). 중심도 그 단위로 반올림.
          const hw = teNiceUnit(last * TE_BAND_PCT);
          const c = Math.round(last / hw) * hw;
          const dec = hw >= 10 ? 0 : hw >= 1 ? 1 : hw >= 0.1 ? 2 : 3;
          setDNLo(Number((c - hw).toFixed(dec)));
          setDNHi(Number((c + hw).toFixed(dec)));
          // 증감율 밴드 기본값 = 현재(기본 lookback일) 증감율 ±5%p (5로 반올림) →
          // 첫 확인부터 '종가범위 ∧ 증감율' 두 조건이 모두 활성(음영=두 조건 교집합).
          const w = Math.min(TE_DEF_LOOKBACK, p.length - 1);
          if (w >= 1) {
            const chg = (last / p[p.length - 1 - w].close - 1) * 100;
            const cc = Math.round(chg / 5) * 5;
            setDLo(cc - 5);
            setDHi(cc + 5);
          }
        }
      })
      .catch((e) => console.error("prices", e));
  }, [symbol]);

  const latestClose = prices.length ? prices[prices.length - 1].close : null;

  // 현재 (과거 dL일) 증감율 — 최신 가격 기준 안내 (draft L에 live; 분석과 무관).
  const currentChange = useMemo(() => {
    if (prices.length <= dL) return null;
    const last = prices[prices.length - 1].close;
    const prev = prices[prices.length - 1 - dL].close;
    return prev ? (last / prev - 1) * 100 : null;
  }, [prices, dL]);

  // lOverride/hOverride: (L,H) 설명력 지도 칸 클릭 시 그 윈도우로 즉시 재계산(드래프트도 갱신).
  function applyAndCompute(lOverride?: number, hOverride?: number) {
    const L = lOverride ?? dL;
    const H = hOverride ?? dH;
    if (lOverride !== undefined) setDL(lOverride);
    if (hOverride !== undefined) setDH(hOverride);
    const lo = dLo === "" ? -1e9 : Number(dLo);
    const hi = dHi === "" ? 1e9 : Number(dHi);
    const nLo = dNLo === "" ? -1e9 : Number(dNLo);
    const nHi = dNHi === "" ? 1e9 : Number(dNHi);
    const a = {
      nLo: Math.min(nLo, nHi), nHi: Math.max(nLo, nHi),
      L, H,
      lo: Math.min(lo, hi), hi: Math.max(lo, hi),
      gap: Math.max(0, dGap),
    };
    setApplied(a);
    setLoading(true);
    setErr(null);
    futuresApi.trendEvents(symbol, { lookback: a.L, horizon: a.H })
      .then(setData)
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
    // 설명력 지도는 종가범위만 의존(폼 L,H 무관) → 본 확인 시에만 갱신, 칸 클릭 땐 유지.
    if (lOverride === undefined) {
      setScanLoading(true);
      futuresApi.trendScan(symbol, { price_lo: a.nLo, price_hi: a.nHi, gap: a.gap })
        .then(setScan)
        .catch(() => setScan(null))
        .finally(() => setScanLoading(false));
    }
  }

  // 매칭 이벤트: 설정 종가범위 ∧ 과거 증감율 범위.
  const matched = useMemo(() => {
    if (!data || !applied) return [];
    return data.events.filter(
      (e) => e.close >= applied.nLo && e.close <= applied.nHi &&
        e.past_return * 100 >= applied.lo && e.past_return * 100 <= applied.hi,
    );
  }, [data, applied]);

  // 2D 스윕 — applied(확인 스냅샷)·축 변경 시 재조회. 나머지 2축은 applied 고정값.
  useEffect(() => {
    if (!applied) { setSweep(null); return; }
    if (sweepRowAxis === sweepColAxis) { setSweep(null); setSweepError("행 축과 열 축을 다르게 선택하세요"); return; }
    setSweepLoading(true);
    setSweepError(null);
    futuresApi.trendSweep(symbol, {
      row_axis: sweepRowAxis, col_axis: sweepColAxis,
      lookback: applied.L, horizon: applied.H,
      price_lo: applied.nLo, price_hi: applied.nHi,
      change_lo: applied.lo, change_hi: applied.hi, gap: applied.gap,
    })
      .then(setSweep)
      .catch((e) => { setSweep(null); setSweepError(e?.message || "스윕 조회 실패"); })
      .finally(() => setSweepLoading(false));
  }, [applied, sweepRowAxis, sweepColAxis, symbol]);

  // 스윕 색 정규화 — 선택 지표값 추출 + 고신뢰 칸(n≥min_n) 기준 정규화(저표본 과장 방지).
  const sweepView = useMemo(() => {
    if (!sweep) return null;
    const valOf = (c: OilTrendSweep["cells"][number]): number | null =>
      sweepMetric === "r2" ? c.r_squared
        : sweepMetric === "up" ? c.up_ratio
          : sweepMetric === "n" ? c.n
            : c.mean_forward;
    const hiv = sweep.cells.filter((c) => valOf(c) != null && c.n >= sweep.min_n).map((c) => valOf(c) as number);
    const anyv = sweep.cells.filter((c) => valOf(c) != null).map((c) => valOf(c) as number);
    const base = hiv.length ? hiv : anyv;
    const maxPos = Math.max(1e-9, ...base, 0);
    const maxAbs = Math.max(1e-9, ...base.map((x) => Math.abs(x)), 0);
    const byKey = new Map(sweep.cells.map((c) => [`${c.row}|${c.col}`, c] as const));
    return { valOf, maxPos, maxAbs, byKey };
  }, [sweep, sweepMetric]);

  // 날짜→가격 인덱스(영업일 간격 계산용).
  const dateIdx = useMemo(() => new Map(prices.map((p, i) => [p.date, i] as const)), [prices]);

  // 디클러스터: 같은 조건이 G영업일 내 연속/겹쳐 발생하면 1건만 채택 — 겹치는 forward
  // 윈도우·레짐 집중으로 인한 표본 과대평가 방지(독립 표본 근접). gap=0이면 원시 그대로.
  const declustered = useMemo(
    () => teDeclusterByGap(matched, applied?.gap ?? 0, dateIdx),
    [matched, applied, dateIdx],
  );

  const result = useMemo(() => {
    const n = declustered.length;
    if (!n) return null;
    const mean = declustered.reduce((s, e) => s + e.forward_return * 100, 0) / n;
    const up = (declustered.filter((e) => e.forward_return > 0).length / n) * 100;
    return { n, mean, up };
  }, [declustered]);

  // 회귀/산점도: 종가범위만(증감율 밴드 제외 — x축 절단 회피) → gap 디클러스터.
  // = (L,H) 지도 셀과 동일 조건이고 결과·음영과 일관. 독립 표본이라 단순 OLS로 충분(HAC 불필요).
  const scatterDecl = useMemo(() => {
    if (!data || !applied) return [];
    const band = data.events.filter((e) => e.close >= applied.nLo && e.close <= applied.nHi);
    return teDeclusterByGap(band, applied.gap, dateIdx);
  }, [data, applied, dateIdx]);
  const scatterPts = useMemo(
    () => scatterDecl.map((e) => ({ x: e.past_return * 100, y: e.forward_return * 100 })),
    [scatterDecl],
  );
  const reg = useMemo(() => teOls(scatterPts), [scatterPts]);
  const pastRange = useMemo(() => {
    if (!scatterPts.length) return null;
    const xs = scatterPts.map((p) => p.x);
    return { min: Math.floor(Math.min(...xs)), max: Math.ceil(Math.max(...xs)) };
  }, [scatterPts]);
  const regSeg = useMemo<[{ x: number; y: number }, { x: number; y: number }] | null>(() => {
    if (!reg || !pastRange) return null;
    const yAt = (xp: number) => reg.slope * xp + reg.intercept;
    return [{ x: pastRange.min, y: yAt(pastRange.min) }, { x: pastRange.max, y: yAt(pastRange.max) }];
  }, [reg, pastRange]);

  // 전체기간 가격 차트 + 매칭 구간 음영(각 이벤트 [신호발생일, 청산예정일=+H] 윈도우 병합).
  const chart = useMemo(() => {
    const empty = { line: [] as { t: number; close: number }[], shades: [] as { x1: number; x2: number }[] };
    if (!prices.length) return empty;
    const ms = (d: string) => new Date(d).getTime();
    const stride = Math.max(1, Math.ceil(prices.length / 800));
    const line = prices
      .filter((_, i) => i % stride === 0 || i === prices.length - 1)
      .map((p) => ({ t: ms(p.date), close: p.close }));
    if (!applied || !declustered.length) return { line, shades: [] };
    const wins: [number, number][] = [];
    for (const e of declustered) {
      const i = dateIdx.get(e.date);
      if (i === undefined) continue;
      wins.push([i, Math.min(prices.length - 1, i + applied.H)]);
    }
    wins.sort((a, b) => a[0] - b[0]);
    const merged: [number, number][] = [];
    for (const w of wins) {
      const last = merged[merged.length - 1];
      if (last && w[0] <= last[1]) last[1] = Math.max(last[1], w[1]);
      else merged.push([w[0], w[1]]);
    }
    const shades = merged.map(([a, b]) => ({ x1: ms(prices[a].date), x2: ms(prices[b].date) }));
    return { line, shades };
  }, [prices, declustered, applied, dateIdx]);

  // (L,H) 지도용 — 칸 조회 Map + 선택 지표값 추출/정규화(지표별 색scale).
  const scanView = useMemo(() => {
    if (!scan) return null;
    const byKey = new Map(scan.cells.map((c) => [`${c.lookback}|${c.horizon}`, c] as const));
    // 선택 지표의 셀 값(없으면 null → '·'). R²는 n<3이면 null이지만 수익률/상승비율/표본수는
    // n≥1이면 값이 있어 더 많은 칸이 그려진다.
    const valOf = (c: OilTrendScanCell): number | null =>
      scanMetric === "r2" ? c.r_squared
        : scanMetric === "up" ? c.up_ratio
          : scanMetric === "n" ? c.n
            : c.mean_forward;
    // 색 정규화 — 고신뢰 칸(n≥min_n) 기준(저표본 과장이 스케일 끌어올리는 것 방지), 없으면 전체.
    const hi = scan.cells.filter((c) => valOf(c) != null && c.n >= scan.min_n).map((c) => valOf(c) as number);
    const any = scan.cells.filter((c) => valOf(c) != null).map((c) => valOf(c) as number);
    const base = hi.length ? hi : any;
    const maxPos = Math.max(1e-9, ...base, 0);                  // r2·n: 양수 정규화
    const maxAbs = Math.max(1e-9, ...base.map((v) => Math.abs(v)), 0);  // ret: 발산(절댓값)
    // 빈상태 = 모든 칸이 이 지표로 그릴 값 없음(R²=전부 n<3 / 수익률·상승=전부 n=0).
    const allNull = scan.cells.length > 0 && scan.cells.every((c) => valOf(c) == null);
    return { byKey, valOf, maxPos, maxAbs, allNull };
  }, [scan, scanMetric]);

  const op = result ? teOpinion(result.mean) : null;

  return (
    <>
      <style>{`
        .te-blank{display:inline-flex;align-items:center;gap:2px;background:var(--accent-soft);border:1px solid var(--accent);border-radius:7px;padding:2px 9px;margin:0 3px;font-weight:600;color:var(--accent-strong)}
        .te-blank input{width:58px;border:none;background:transparent;color:var(--accent-strong);font:inherit;font-weight:600;text-align:center;-moz-appearance:textfield;appearance:textfield}
        .te-blank input::-webkit-outer-spin-button,.te-blank input::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
        .te-blank input::placeholder{color:#c19a86}
        .te-hm{border-collapse:separate;border-spacing:3px}
        .te-hm td,.te-hm th{padding:6px 8px;text-align:center;font-size:12px;white-space:nowrap}
        .te-hm th{color:var(--muted);font-weight:500}
      `}</style>

      <p className="muted" style={{ marginBottom: 12 }}>
        종가 범위·진입 직전 추세·기간을 채우고 <b>확인</b>을 누르면, 과거 비슷했던 구간들의 향후 종가 증감율·전망과
        해당 구간들의 가격 차트를 보여줍니다.
      </p>

      {prices.length === 0 ? (
        <div className="muted">가격 데이터 로딩 중…</div>
      ) : (
        <>
          {/* 입력 폼 (확인 시 계산) */}
          <div style={{ fontSize: 15, lineHeight: 2.6 }}>
            종가가{" "}
            <span className="te-blank">{priceSym}
              <input type="number" value={dNLo} placeholder="하한"
                onChange={(e) => setDNLo(e.target.value === "" ? "" : Number(e.target.value))} />
            </span>{" "}~{" "}
            <span className="te-blank">{priceSym}
              <input type="number" value={dNHi} placeholder="상한"
                onChange={(e) => setDNHi(e.target.value === "" ? "" : Number(e.target.value))} />
            </span>{" "}
            일 때, 과거{" "}
            <span className="te-blank">
              <input type="number" min={1} max={250} value={dL} onChange={(e) => setDL(Math.max(1, Number(e.target.value) || 1))} />일
            </span>{" "}
            동안 증감율이{" "}
            <span className="te-blank">
              <input type="number" step={1} value={dLo} placeholder="하한"
                onChange={(e) => setDLo(e.target.value === "" ? "" : Number(e.target.value))} />%
            </span>{" "}~{" "}
            <span className="te-blank">
              <input type="number" step={1} value={dHi} placeholder="상한"
                onChange={(e) => setDHi(e.target.value === "" ? "" : Number(e.target.value))} />%
            </span>{" "}
            였다면, 향후{" "}
            <span className="te-blank">
              <input type="number" min={1} max={500} value={dH} onChange={(e) => setDH(Math.max(1, Number(e.target.value) || 1))} />일
            </span>{" "}
            후 종가 증감율은?
          </div>

          {/* 현재 종가·증감율 참고 문구 (질문 문장 뒤) */}
          {latestClose !== null && (
            <div className="muted" style={{ fontSize: 13, marginTop: 6 }}>
              (참고: 현재 종가는 <b>{priceSym}{latestClose.toFixed(2)}</b>이며, 과거 <b>{dL}일</b> 동안 증감율은{" "}
              {currentChange !== null
                ? <b className={currentChange >= 0 ? "pos" : "neg"}>{(currentChange >= 0 ? "+" : "") + currentChange.toFixed(1)}%</b>
                : "—"}
              입니다)
            </div>
          )}

          {/* 샘플링 통제 — 이벤트 최소 간격(디클러스터) */}
          <div className="muted" style={{ fontSize: 13, marginTop: 8, display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" }}>
            이벤트 최소 간격
            <span className="te-blank" style={{ fontSize: 13 }}>
              <input type="number" min={0} max={250} value={dGap}
                onChange={(e) => setDGap(Math.max(0, Number(e.target.value) || 0))} style={{ width: 44 }} />
            </span>
            영업일 — 연속·겹치는 매칭을 1건으로 묶어 표본 독립성 확보. 0=원시(겹침 많음), 클수록 독립적이나 표본 급감 — (L,H) 지도가 비면 줄이세요.
          </div>

          <button onClick={() => applyAndCompute()} disabled={loading} style={{ margin: "12px 0 16px" }}>
            {loading ? "계산 중…" : "확인 (계산)"}
          </button>

          {err && <div className="error">{err}</div>}

          {/* 결과 + 전망 */}
          {applied && !loading && (
            result ? (
              <>
                <div style={{ fontSize: 15, marginBottom: 6 }}>
                  → 향후 {applied.H}일 평균 종가 증감율{" "}
                  <b className={result.mean >= 0 ? "pos" : "neg"} style={{ fontSize: 19 }}>
                    {(result.mean >= 0 ? "+" : "") + result.mean.toFixed(2)}%
                  </b>
                  {", 상승 비율 "}
                  <b style={{ fontSize: 19 }}>{result.up.toFixed(0)}%</b>{" "}
                  <span className="muted" style={{ fontSize: 13 }}>
                    (독립 표본 n={result.n}{matched.length > result.n ? ` / 원시 ${matched.length}건` : ""})
                  </span>
                </div>
                {op && (
                  <div style={{ marginBottom: 16, fontSize: 15 }}>
                    <span className={op.cls === "muted" ? "muted" : op.cls}
                      style={{ fontWeight: 600, padding: "3px 10px", borderRadius: 6, background: "#f1efe8", border: "1px solid var(--border)" }}>
                      전망: {op.txt}
                    </span>{" "}
                    <span className="muted" style={{ fontSize: 12 }}>
                      (이 조건의 향후 평균 종가 증감율 {(result.mean >= 0 ? "+" : "") + result.mean.toFixed(2)}% 기준)
                    </span>
                  </div>
                )}
              </>
            ) : (
              <div className="muted" style={{ marginBottom: 16 }}>해당 조건에 맞는 독립 이벤트가 없습니다 — 범위를 넓히거나 이벤트 최소 간격을 줄여 보세요.</div>
            )
          )}

          {/* 엑셀 내보내기 — 조건·이벤트·요약 (.xlsx) */}
          {applied && !loading && result && (
            <button className="export-btn" disabled={teExporting} style={{ marginBottom: 14 }}
              title="현재 조건의 향후 종가 증감율 결과(조건·매칭 이벤트·요약)를 엑셀로"
              onClick={async () => {
                setTeExporting(true);
                try {
                  await futuresApi.trendExport(symbol, {
                    lookback: applied.L, horizon: applied.H,
                    price_lo: applied.nLo, price_hi: applied.nHi,
                    change_lo: applied.lo, change_hi: applied.hi, gap: applied.gap,
                  });
                } catch (e) {
                  alert("엑셀 내보내기 실패: " + (e as Error).message);
                } finally {
                  setTeExporting(false);
                }
              }}>
              {teExporting ? "엑셀 생성 중…" : "📥 엑셀로 내보내기 (조건·이벤트·요약)"}
            </button>
          )}

          {/* 전체기간 가격차트 + 매칭 구간 음영 */}
          <div className="muted" style={{ fontSize: 13, margin: "8px 0 6px" }}>
            전체기간 가격 — <span style={{ color: "var(--accent-strong)", fontWeight: 600 }}>음영</span> = 종가{applied ? ` ${applied.nLo <= -1e8 ? "−∞" : priceSym + applied.nLo}~${applied.nHi >= 1e8 ? "∞" : priceSym + applied.nHi}` : ""} <b>∧</b> 증감율{applied ? ` ${applied.lo <= -1e8 ? "−∞" : applied.lo + "%"}~${applied.hi >= 1e8 ? "∞" : applied.hi + "%"}` : ""} <b>둘 다</b> 만족하는 신호발생일~청산예정일(+{applied?.H ?? dH}일) 구간{declustered.length ? ` · ${declustered.length}건${matched.length > declustered.length ? ` (원시 ${matched.length})` : ""}` : ""}.
          </div>
          <ResponsiveContainer width="100%" height={260}>
            <ComposedChart data={chart.line} margin={{ top: 8, right: 16, bottom: 4, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e8e3db" />
              <XAxis type="number" dataKey="t" domain={["dataMin", "dataMax"]}
                tick={{ fontSize: 10, fill: "#6f6a62" }} tickFormatter={(t) => new Date(Number(t)).toISOString().slice(0, 7)} />
              <YAxis tick={{ fontSize: 10, fill: "#6f6a62" }} domain={["auto", "auto"]}
                tickFormatter={(v) => priceSym + (v >= 1000 ? (v / 1000).toFixed(0) + "k" : v.toFixed(0))} />
              <Tooltip labelFormatter={(t) => new Date(Number(t)).toISOString().slice(0, 10)}
                formatter={(v) => priceSym + Number(v).toLocaleString("en-US", { maximumFractionDigits: 2 })} />
              {chart.shades.map((s, i) => (
                <ReferenceArea key={i} x1={s.x1} x2={s.x2} fill="#d97757" fillOpacity={0.16} stroke="none" />
              ))}
              <Line type="monotone" dataKey="close" stroke="#d97757" dot={false} strokeWidth={1.5} />
            </ComposedChart>
          </ResponsiveContainer>

          {/* 회귀 (side-aware) */}
          {applied && !loading && reg && pastRange && (
            <div style={{ marginTop: 20, paddingTop: 16, borderTop: "1px solid var(--border)" }}>
              <div className="muted" style={{ fontSize: 13, marginBottom: 8 }}>
                회귀분석 — 과거 <b>{applied.L}일</b> 증감율(x) → 미래 <b>{applied.H}일</b> 종가 증감율(y) · 종가범위+간격 <b>독립 {reg.n.toLocaleString()}건</b>
              </div>
              <div className="bt-metrics" style={{ marginBottom: 10 }}>
                <Metric label="기울기 β" value={(reg.slope >= 0 ? "+" : "") + reg.slope.toFixed(2)}
                  highlight={reg.slope >= 0 ? "good" : "bad"}
                  sub={reg.slope >= 0 ? "추세↑→이후 상승↑" : "추세↑→이후 하락↑"} />
                <Metric label="R²" value={reg.r2.toFixed(3)} sub="설명력(0~1)" />
                <Metric label="p-value" value={reg.p.toFixed(3)}
                  highlight={reg.p < 0.05 ? "good" : "warn"} sub="독립표본 단순 OLS(정규근사)" />
                <Metric label="표본 n" value={reg.n} highlight={reg.n < 30 ? "warn" : null} />
              </div>
              <ResponsiveContainer width="100%" height={280}>
                <ScatterChart margin={{ top: 8, right: 20, bottom: 30, left: 16 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e8e3db" />
                  <XAxis type="number" dataKey="x" name="과거 증감율" domain={["auto", "auto"]}
                    tick={{ fontSize: 10, fill: "#6f6a62" }} tickFormatter={(v) => Math.round(Number(v)) + "%"}
                    label={{ value: `과거 ${applied.L}일 증감율 (%)`, position: "insideBottom", offset: -6, style: { fontSize: 11, fill: "#6f6a62" } }} />
                  <YAxis type="number" dataKey="y" name="향후 종가 증감율" domain={["auto", "auto"]}
                    tick={{ fontSize: 10, fill: "#6f6a62" }} tickFormatter={(v) => Math.round(Number(v)) + "%"}
                    label={{ value: `향후 ${applied.H}일 종가 증감율 (%)`, angle: -90, position: "insideLeft", style: { fontSize: 11, fill: "#6f6a62", textAnchor: "middle" } }} />
                  <Tooltip cursor={{ strokeDasharray: "3 3" }} formatter={(v) => `${Number(v).toFixed(2)}%`} />
                  <ReferenceLine y={0} stroke="#6f6a62" />
                  {regSeg && <ReferenceLine segment={regSeg} stroke="#d97757" strokeWidth={2} />}
                  <Scatter data={scatterPts} fill="#d97757" fillOpacity={0.5} />
                </ScatterChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* (L,H) 설명력 지도 — 어느 과거·미래 기간이 종가범위 내에서 가장 설명력 높은가 */}
          {applied && scanView && scan && (
            <div style={{ marginTop: 20, paddingTop: 16, borderTop: "1px solid var(--border)" }}>
              <div className="muted" style={{ fontSize: 13, marginBottom: 6 }}>
                <b>(과거 L, 미래 H) 지도</b> — 행=과거 L일, 열=미래 H일, 종가범위{applied.nLo <= -1e8 ? "" : ` ${priceSym}${applied.nLo}~${priceSym}${applied.nHi}`} 내 매칭으로 계산.
                {" "}<b style={{ color: "var(--accent-strong)" }}>칸 클릭 → 그 (L,H)로 위 결과·회귀 재계산.</b> <b>점선·흐린 칸</b>=저신뢰(표본 n&lt;{scan.min_n}).
              </div>
              {/* 셀 지표 토글 — 값 하나씩 넣지 않고 격자에서 최적 조합을 한눈에 */}
              <div style={{ display: "flex", gap: 6, marginBottom: 8, flexWrap: "wrap", alignItems: "center" }}>
                <span className="muted" style={{ fontSize: 12 }}>셀 지표:</span>
                {([["ret", "평균 향후수익률"], ["up", "상승비율"], ["r2", "설명력 R²"], ["n", "표본수"]] as const).map(([m, lbl]) => (
                  <button key={m} onClick={() => setScanMetric(m)}
                    style={{
                      fontSize: 12, padding: "3px 10px", borderRadius: 6, cursor: "pointer",
                      border: scanMetric === m ? "1px solid var(--accent)" : "1px solid var(--border)",
                      background: scanMetric === m ? "var(--accent-soft)" : "transparent",
                      color: scanMetric === m ? "var(--accent-strong)" : "var(--muted)",
                      fontWeight: scanMetric === m ? 700 : 400,
                    }}>{lbl}</button>
                ))}
                <span className="muted" style={{ fontSize: 11 }}>
                  {scanMetric === "ret" ? "수익=빨강 / 손실=파랑" : scanMetric === "up" ? "50% 기준 빨강/파랑" : "값 클수록 진함"}
                </span>
              </div>
              {!scanView.allNull && (
                <div className="muted" style={{ fontSize: 11, marginBottom: 8, color: "var(--amber)" }}>
                  ⚠ 다중비교 주의: {scan.n_cells}개 칸 중 최댓값 한 칸만 믿으면 과최적화. 고-값이 <b>연속 영역</b>이고 <b>표본수</b>·<b>OOS 부호안정</b>이 받쳐줄 때만 신뢰.
                </div>
              )}
              {scanView.allNull && !scanLoading && (
                <div style={{ fontSize: 13, padding: "14px 14px", background: "#f1efe8", borderRadius: 8, lineHeight: 1.65, color: "var(--text)" }}>
                  <b>표본이 거의 없어 지도를 그릴 수 없습니다.</b><br />
                  {scan.n_cells}개 칸 <b>전부</b> 이 조건에서 매칭 표본이 거의 없습니다(R² 지표는 칸당 3건 이상 필요). 현재 종가 범위{applied.nLo <= -1e8 ? "" : ` ${priceSym}${applied.nLo}~${priceSym}${applied.nHi}`}와 이벤트 최소 간격 <b>{applied.gap}일</b> 조합이 너무 성깁니다.<br />
                  → <b style={{ color: "var(--accent-strong)" }}>이벤트 최소 간격을 줄이거나</b>(현재 {applied.gap}일) <b style={{ color: "var(--accent-strong)" }}>종가 범위를 넓혀</b> 다시 확인하세요.
                </div>
              )}
              {!scanView.allNull && (
              <div className="table-scroll sticky-table" style={{ maxHeight: 380 }}>
                <table className="te-hm">
                  <thead>
                    <tr>
                      <th style={{ textAlign: "left" }}>L＼H</th>
                      {scan.horizons.map((h) => <th key={h}>{h}일</th>)}
                    </tr>
                  </thead>
                  <tbody>
                    {scan.lookbacks.map((L) => (
                      <tr key={L}>
                        <th style={{ textAlign: "left" }}>{L}일</th>
                        {scan.horizons.map((H) => {
                          const c = scanView.byKey.get(`${L}|${H}`);
                          const isCur = applied.L === L && applied.H === H;
                          const v = c ? scanView.valOf(c) : null;
                          if (!c || v == null) {
                            // 이 지표로 그릴 값 없음(R²=n<3 / 수익률·상승=n=0).
                            return (
                              <td key={H} title={c ? `n=${c.n} · (이 지표 값 없음)` : ""}
                                style={{ background: "#f1efe8", color: "var(--muted)", opacity: 0.5, borderRadius: 5,
                                  cursor: "default", outline: isCur ? "2px solid #d97757" : "none" }}>·</td>
                            );
                          }
                          // 저신뢰(n<min_n)는 흐리게(알파 상한↓)+점선+표본수 — 작은 n의 과장된 값 오인 방지.
                          const lowConf = c.n < scan.min_n;
                          const { bg, txt, fg } = teMetricCell(scanMetric, v, lowConf, scanView.maxPos, scanView.maxAbs);
                          const sub = scanMetric === "r2"
                            ? `${(c.slope ?? 0) >= 0 ? "β+" : "β−"}${c.sign_stable ? " ✓" : ""}${c.hac_p_value != null && c.hac_p_value < 0.05 ? " *" : ""}${lowConf ? ` ⚠n${c.n}` : ""}`
                            : `n${c.n}${lowConf ? " ⚠" : ""}`;
                          const tip = `L=${L}, H=${H} · n=${c.n}${lowConf ? ` ⚠저신뢰(<${scan.min_n})` : ""}`
                            + ` · 평균향후 ${c.mean_forward != null ? c.mean_forward.toFixed(2) + "%" : "—"}`
                            + ` · 상승 ${c.up_ratio != null ? c.up_ratio.toFixed(0) + "%" : "—"}`
                            + ` · R² ${c.r_squared != null ? c.r_squared.toFixed(3) : "—"}`
                            + ` · β ${c.slope != null ? c.slope.toFixed(2) : "—"} · 부호안정 ${c.sign_stable ? "예" : "아니오"}`;
                          return (
                            <td key={H} title={tip} onClick={() => applyAndCompute(L, H)}
                              style={{ background: bg, color: fg,
                                borderRadius: 5, cursor: "pointer", outline: isCur ? "2px solid #d97757" : "none",
                                border: lowConf ? "1px dashed var(--muted)" : undefined }}>
                              <div style={{ fontWeight: 600 }}>{txt}</div>
                              <div style={{ fontSize: 10, opacity: 0.9 }}>{sub}</div>
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              )}
              {scanLoading && <div className="muted" style={{ fontSize: 11, marginTop: 4 }}>지도 계산 중…</div>}
            </div>
          )}

          {/* 2D 스윕 — 4축 중 2개 격자(값 하나씩 안 넣고 한눈에). 나머지 2축은 현재 입력 고정 */}
          {applied && (
            <div style={{ marginTop: 20, paddingTop: 16, borderTop: "1px solid var(--border)" }}>
              <div className="muted" style={{ fontSize: 13, marginBottom: 6 }}>
                <b>2D 스윕</b> — 4축(종가범위·증감율범위·과거L·향후H) 중 둘을 격자로. 나머지 2축은 현재 입력 고정. 값 하나씩 안 넣고 최적 조합을 한눈에.
              </div>
              <div style={{ display: "flex", gap: 8, marginBottom: 8, flexWrap: "wrap", alignItems: "center" }}>
                <span className="muted" style={{ fontSize: 12 }}>행</span>
                <select value={sweepRowAxis} onChange={(e) => setSweepRowAxis(e.target.value as OilSweepAxis)}
                  style={{ fontSize: 12, padding: "3px 6px", borderRadius: 6, border: "1px solid var(--border)" }}>
                  {TE_SWEEP_AXES.map((ax) => <option key={ax.v} value={ax.v} disabled={ax.v === sweepColAxis}>{ax.label}</option>)}
                </select>
                <span className="muted" style={{ fontSize: 12 }}>열</span>
                <select value={sweepColAxis} onChange={(e) => setSweepColAxis(e.target.value as OilSweepAxis)}
                  style={{ fontSize: 12, padding: "3px 6px", borderRadius: 6, border: "1px solid var(--border)" }}>
                  {TE_SWEEP_AXES.map((ax) => <option key={ax.v} value={ax.v} disabled={ax.v === sweepRowAxis}>{ax.label}</option>)}
                </select>
                <span style={{ flex: "0 0 12px" }} />
                {([["ret", "평균 향후수익률"], ["up", "상승비율"], ["r2", "설명력 R²"], ["n", "표본수"]] as const).map(([m, lbl]) => (
                  <button key={m} onClick={() => setSweepMetric(m)}
                    style={{
                      fontSize: 12, padding: "3px 10px", borderRadius: 6, cursor: "pointer",
                      border: sweepMetric === m ? "1px solid var(--accent)" : "1px solid var(--border)",
                      background: sweepMetric === m ? "var(--accent-soft)" : "transparent",
                      color: sweepMetric === m ? "var(--accent-strong)" : "var(--muted)",
                      fontWeight: sweepMetric === m ? 700 : 400,
                    }}>{lbl}</button>
                ))}
              </div>
              {sweepError && <div className="muted" style={{ fontSize: 12, color: "var(--amber)", marginBottom: 6 }}>{sweepError}</div>}
              {sweepLoading && <div className="muted" style={{ fontSize: 11 }}>스윕 계산 중…</div>}
              {sweep && sweepView && !sweepLoading && (
                <div className="table-scroll sticky-table" style={{ maxHeight: 420 }}>
                  <table className="te-hm">
                    <thead>
                      <tr>
                        <th style={{ textAlign: "left" }}>
                          {TE_SWEEP_AXES.find((a) => a.v === sweepRowAxis)?.short}＼{TE_SWEEP_AXES.find((a) => a.v === sweepColAxis)?.short}
                        </th>
                        {sweep.col_labels.map((cl, ci) => <th key={ci}>{cl}</th>)}
                      </tr>
                    </thead>
                    <tbody>
                      {sweep.row_labels.map((rl, ri) => (
                        <tr key={ri}>
                          <th style={{ textAlign: "left" }}>{rl}</th>
                          {sweep.col_labels.map((_, ci) => {
                            const c = sweepView.byKey.get(`${ri}|${ci}`);
                            const v = c ? sweepView.valOf(c) : null;
                            if (!c || v == null) {
                              return <td key={ci} title={c ? `n=${c.n}` : ""}
                                style={{ background: "#f1efe8", color: "var(--muted)", opacity: 0.5, borderRadius: 5 }}>·</td>;
                            }
                            const lowConf = c.n < sweep.min_n;
                            const { bg, txt, fg } = teMetricCell(sweepMetric, v, lowConf, sweepView.maxPos, sweepView.maxAbs);
                            const tip = `n=${c.n}${lowConf ? " ⚠저신뢰" : ""}`
                              + ` · 평균향후 ${c.mean_forward != null ? c.mean_forward.toFixed(2) + "%" : "—"}`
                              + ` · 상승 ${c.up_ratio != null ? c.up_ratio.toFixed(0) + "%" : "—"}`
                              + ` · R² ${c.r_squared != null ? c.r_squared.toFixed(3) : "—"}`;
                            return (
                              <td key={ci} title={tip}
                                style={{ background: bg, color: fg, borderRadius: 5, border: lowConf ? "1px dashed var(--muted)" : undefined }}>
                                <div style={{ fontWeight: 600 }}>{txt}</div>
                                <div style={{ fontSize: 10, opacity: 0.9 }}>n{c.n}{lowConf ? " ⚠" : ""}</div>
                              </td>
                            );
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div className="muted" style={{ fontSize: 11, marginTop: 6, color: "var(--amber)" }}>
                ⚠ 다중비교: 최댓값 한 칸만 믿으면 과최적화 — 고-값이 <b>연속 영역</b>이고 <b>표본수</b>가 받쳐줄 때만 신뢰. 점선·흐림 = 저신뢰(n&lt;{sweep?.min_n ?? 30}).
              </div>
            </div>
          )}

          <div className="muted" style={{ fontSize: 11, marginTop: 12 }}>
            ⚠️ <b>종가 증감율</b>은 종가-종가 기준 서술용(실 백테스트의 익일 시가·비용·청산룰과 다름 — 관계 측정용). forward 윈도우 겹침·레짐 집중은 <b>이벤트 최소 간격(디클러스터)</b>으로 독립표본화 — 결과·회귀·(L,H) 지도에 동일 적용.
            범위를 좁힐수록 표본↓·통계 불안정(n&lt;30 ⚠). (L,H) 지도는 종가범위 조건, 위 회귀는 전체기간 — 조건이 달라 R²가 다를 수 있음.
          </div>
        </>
      )}
    </>
  );
}

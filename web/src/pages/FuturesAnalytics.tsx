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
  type OilTrendEvents,
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
        <Metric label="롤오버 횟수" value={s.total_rollovers}
                sub={`선물 만기 강제 롤 합산 (trade당 평균 ${s.n_trades ? (s.total_rollovers / s.n_trades).toFixed(1) : 0}회)`} />
      </div>

      {s.low_sample && (
        <div className="warn-banner">
          ⚠️ 거래 수 {s.n_trades}건 (&lt;30) — 통계적 유의성 낮음. 평균이 좋아 보여도 신중히 해석.
        </div>
      )}

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
                labelStyle={{ color: "#333" }}
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
                labelStyle={{ color: "#333" }}
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

// ── ⑧ 진입 추세 → 미래 수익률 탐색기 ──────────────────────────────────
// 서버가 (L,H) 이벤트 배열을 1회 내려주면, 증감율 밴드 필터·집계는 브라우저에서 실시간.
// 진입 직전 증감율 구간 — 5%p 단위 (−10% 이하부터 +10% 초과까지).
const TE_RET_BUCKETS = [
  { label: "≤−10%", lo: -Infinity, hi: -10 },
  { label: "−10~−5%", lo: -10, hi: -5 },
  { label: "−5~0%", lo: -5, hi: 0 },
  { label: "0~+5%", lo: 0, hi: 5 },
  { label: "+5~+10%", lo: 5, hi: 10 },
  { label: ">+10%", lo: 10, hi: Infinity },
];

// 가격 범위를 ~12개로 나눌 "보기 좋은" 버킷 폭 (1·2·5·10 × 10ⁿ).
function teNiceStep(raw: number): number {
  if (raw <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const nice = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
  return nice * mag;
}

// 매칭 조건의 평균 자산수익에 따른 한줄 투자의견 (표시 토글과 독립).
function teOpinion(assetMean: number): { txt: string; cls: string } {
  if (assetMean >= 4) return { txt: "📈 강한 롱 우위", cls: "pos" };
  if (assetMean >= 1) return { txt: "📈 롱 우위", cls: "pos" };
  if (assetMean <= -4) return { txt: "📉 강한 숏 우위", cls: "neg" };
  if (assetMean <= -1) return { txt: "📉 숏 우위", cls: "neg" };
  return { txt: "⚖ 중립 (뚜렷한 방향성 없음)", cls: "muted" };
}

// ⑧ 진입 추세 → 미래 수익률 탐색기.
// 폼(전략유형·현재값·과거L·증감율범위·미래H)을 채우고 [확인]을 누르면 계산: 문장 결과+투자의견 /
// 전체기간 가격차트(매칭 구간 음영) / 현재값×증감율 히트맵 / 회귀(과거×미래). 롱/숏 토글로 수익 부호 전환.
// /trend-events(이벤트) + /prices(전체 가격 시계열)을 브라우저에서 집계.
function TrendExplorer({ symbol, priceSym }: { symbol: string; priceSym: string }) {
  // 입력 draft (확인 전엔 계산 안 함)
  const [dSide, setDSide] = useState<"long" | "short">("long");
  const [dNLo, setDNLo] = useState<number | "">("");   // 종가 하한
  const [dNHi, setDNHi] = useState<number | "">("");   // 종가 상한
  const [dL, setDL] = useState(20);
  const [dH, setDH] = useState(60);
  const [dLo, setDLo] = useState<number | "">("");     // 과거 증감율 하한
  const [dHi, setDHi] = useState<number | "">("");     // 과거 증감율 상한

  // 적용된 설정 (확인 클릭 시 스냅샷)
  const [applied, setApplied] = useState<
    { side: "long" | "short"; nLo: number; nHi: number; L: number; H: number; lo: number; hi: number } | null
  >(null);
  const [data, setData] = useState<OilTrendEvents | null>(null);
  const [prices, setPrices] = useState<OilPricePoint[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // 종목 변경 → 전체 가격 시계열 로드 + draft 종가범위=현재가 부근 버킷, 적용 리셋.
  useEffect(() => {
    setApplied(null);
    setData(null);
    setPrices([]);
    setDNLo("");
    setDNHi("");
    futuresApi.prices(symbol)
      .then((p) => {
        setPrices(p);
        if (p.length) {
          const closes = p.map((x) => x.close);
          const min = Math.min(...closes), max = Math.max(...closes);
          const step = teNiceStep((max - min) / 12);
          const last = p[p.length - 1].close;
          const lo = Math.floor(last / step) * step;
          const dec = step >= 10 ? 0 : step >= 1 ? 1 : step >= 0.1 ? 2 : 3;
          setDNLo(Number(lo.toFixed(dec)));
          setDNHi(Number((lo + step).toFixed(dec)));
          // 증감율 밴드 기본값 = 현재 추세(과거 20일 증감율)의 5%p 버킷 →
          // 첫 확인부터 '종가범위 ∧ 증감율' 두 조건이 모두 활성(음영=두 조건 교집합).
          if (p.length > 20) {
            const chg = (last / p[p.length - 1 - 20].close - 1) * 100;
            const blo = Math.floor(chg / 5) * 5;
            setDLo(blo);
            setDHi(blo + 5);
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

  function applyAndCompute() {
    const lo = dLo === "" ? -1e9 : Number(dLo);
    const hi = dHi === "" ? 1e9 : Number(dHi);
    const nLo = dNLo === "" ? -1e9 : Number(dNLo);
    const nHi = dNHi === "" ? 1e9 : Number(dNHi);
    const a = {
      side: dSide,
      nLo: Math.min(nLo, nHi), nHi: Math.max(nLo, nHi),
      L: dL, H: dH,
      lo: Math.min(lo, hi), hi: Math.max(lo, hi),
    };
    setApplied(a);
    setLoading(true);
    setErr(null);
    futuresApi.trendEvents(symbol, { lookback: a.L, horizon: a.H })
      .then(setData)
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
  }

  const sign = applied?.side === "short" ? -1 : 1;   // 숏 = −자산수익

  // 가격 버킷(히트맵 행) — 전체 가격 기준.
  const buckets = useMemo(() => {
    const closes = prices.length ? prices.map((p) => p.close) : (data?.events.map((e) => e.close) ?? []);
    if (!closes.length) return null;
    const min = Math.min(...closes), max = Math.max(...closes);
    const step = teNiceStep((max - min) / 12);
    const start = Math.floor(min / step) * step;
    const count = Math.max(1, Math.round((Math.ceil(max / step) * step - start) / step));
    const dec = step >= 10 ? 0 : step >= 1 ? 1 : step >= 0.1 ? 2 : 3;
    const arr = Array.from({ length: count }, (_, i) => ({ lo: start + i * step, hi: start + (i + 1) * step, dec }));
    return { arr, step, start };
  }, [prices, data]);

  const bucketIndex = (close: number) => {
    if (!buckets) return -1;
    return Math.max(0, Math.min(buckets.arr.length - 1, Math.floor((close - buckets.start) / buckets.step)));
  };
  // 선택 종가범위와 겹치는 히트맵 행 강조.
  const rowInSel = (b: { lo: number; hi: number }) =>
    applied != null && b.hi > applied.nLo && b.lo < applied.nHi;

  // 매칭 이벤트: 설정 종가범위 ∧ 과거 증감율 범위.
  const matched = useMemo(() => {
    if (!data || !applied) return [];
    return data.events.filter(
      (e) => e.close >= applied.nLo && e.close <= applied.nHi &&
        e.past_return * 100 >= applied.lo && e.past_return * 100 <= applied.hi,
    );
  }, [data, applied]);

  const result = useMemo(() => {
    const n = matched.length;
    if (!n) return null;
    const mean = matched.reduce((s, e) => s + e.forward_return * 100 * sign, 0) / n;
    const win = (matched.filter((e) => e.forward_return * sign > 0).length / n) * 100;
    const assetMean = matched.reduce((s, e) => s + e.forward_return * 100, 0) / n;
    return { n, mean, win, assetMean };
  }, [matched, sign]);

  // 히트맵 (side-aware): 가격버킷 × 증감율구간 → 평균 수익률(부호 적용).
  const heat = useMemo(() => {
    if (!data || !buckets) return null;
    const grid = buckets.arr.map(() => TE_RET_BUCKETS.map(() => ({ sum: 0, n: 0 })));
    for (const e of data.events) {
      const pi = bucketIndex(e.close);
      const pastPct = e.past_return * 100;
      const ri = TE_RET_BUCKETS.findIndex((rb) => pastPct >= rb.lo && pastPct < rb.hi);
      if (pi < 0 || ri < 0) continue;
      grid[pi][ri].sum += e.forward_return * 100 * sign;
      grid[pi][ri].n++;
    }
    let maxAbs = 1e-9;
    for (const row of grid) for (const c of row) if (c.n) maxAbs = Math.max(maxAbs, Math.abs(c.sum / c.n));
    return { grid, maxAbs };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, buckets, sign]);

  // 회귀 (side-aware): 백엔드 forward~past, 숏이면 부호 반전.
  const reg = data?.regression ?? null;
  const pastRange = useMemo(() => {
    if (!data || data.events.length === 0) return null;
    const ps = data.events.map((e) => e.past_return * 100);
    return { min: Math.floor(Math.min(...ps)), max: Math.ceil(Math.max(...ps)) };
  }, [data]);
  const scatterPts = useMemo(() => {
    if (!data) return [];
    const evs = data.events;
    const stride = Math.max(1, Math.ceil(evs.length / 600));
    const pts: { x: number; y: number }[] = [];
    for (let i = 0; i < evs.length; i += stride) pts.push({ x: evs[i].past_return * 100, y: evs[i].forward_return * 100 * sign });
    return pts;
  }, [data, sign]);
  const regSeg = useMemo<[{ x: number; y: number }, { x: number; y: number }] | null>(() => {
    if (!reg || !pastRange) return null;
    const yAt = (xp: number) => (reg.slope * xp + reg.intercept * 100) * sign;
    return [{ x: pastRange.min, y: yAt(pastRange.min) }, { x: pastRange.max, y: yAt(pastRange.max) }];
  }, [reg, pastRange, sign]);

  // 전체기간 가격 차트 + 매칭 구간 음영(각 이벤트 [진입−L, 진입+H] 윈도우 병합).
  const chart = useMemo(() => {
    const empty = { line: [] as { t: number; close: number }[], shades: [] as { x1: number; x2: number }[] };
    if (!prices.length) return empty;
    const ms = (d: string) => new Date(d).getTime();
    const stride = Math.max(1, Math.ceil(prices.length / 800));
    const line = prices
      .filter((_, i) => i % stride === 0 || i === prices.length - 1)
      .map((p) => ({ t: ms(p.date), close: p.close }));
    if (!applied || !matched.length) return { line, shades: [] };
    const idxOf = new Map(prices.map((p, i) => [p.date, i] as const));
    const wins: [number, number][] = [];
    for (const e of matched) {
      const i = idxOf.get(e.date);
      if (i === undefined) continue;
      wins.push([Math.max(0, i - applied.L), Math.min(prices.length - 1, i + applied.H)]);
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
  }, [prices, matched, applied]);

  const op = result ? teOpinion(result.assetMean) : null;

  return (
    <>
      <style>{`
        .te-blank{display:inline-flex;align-items:center;gap:2px;background:var(--accent-soft);border:1px solid var(--accent);border-radius:7px;padding:2px 9px;margin:0 3px;font-weight:600;color:var(--accent-strong)}
        .te-blank input{width:58px;border:none;background:transparent;color:var(--accent-strong);font:inherit;font-weight:600;text-align:center}
        .te-blank input::placeholder{color:#c19a86}
        .te-hm{border-collapse:separate;border-spacing:3px}
        .te-hm td,.te-hm th{padding:6px 8px;text-align:center;font-size:12px;white-space:nowrap}
        .te-hm th{color:var(--muted);font-weight:500}
      `}</style>

      <p className="muted" style={{ marginBottom: 12 }}>
        전략 유형·현재 가격대·진입 직전 추세·기간을 채우고 <b>확인</b>을 누르면, 그 조건의 미래 수익률·투자의견과
        해당 구간들의 가격 차트를 보여줍니다.
      </p>

      {prices.length === 0 ? (
        <div className="muted">가격 데이터 로딩 중…</div>
      ) : (
        <>
          {/* 전략 유형 */}
          <div className="oil-radio-group" style={{ marginBottom: 10 }}>
            <label className={dSide === "long" ? "active" : ""}>
              <input type="radio" name="te-side" checked={dSide === "long"} onChange={() => setDSide("long")} />
              롱 전략 (매수 — 가격↑이 수익)
            </label>
            <label className={dSide === "short" ? "active" : ""}>
              <input type="radio" name="te-side" checked={dSide === "short"} onChange={() => setDSide("short")} />
              숏 전략 (매도 — 가격↓이 수익)
            </label>
          </div>

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
            후 수익률은?
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

          <button onClick={applyAndCompute} disabled={loading} style={{ margin: "12px 0 16px" }}>
            {loading ? "계산 중…" : "확인 (계산)"}
          </button>

          {err && <div className="error">{err}</div>}

          {/* 결과 + 투자의견 */}
          {applied && !loading && (
            result ? (
              <>
                <div style={{ fontSize: 15, marginBottom: 6 }}>
                  → {applied.side === "long" ? "롱" : "숏"} 전략 평균 수익률{" "}
                  <b className={result.mean >= 0 ? "pos" : "neg"} style={{ fontSize: 19 }}>
                    {(result.mean >= 0 ? "+" : "") + result.mean.toFixed(2)}%
                  </b>{" "}
                  <span className="muted" style={{ fontSize: 13 }}>
                    (n={result.n}, 승률 {result.win.toFixed(0)}%{result.n < 30 ? " ⚠ 저신뢰" : ""})
                  </span>
                </div>
                {op && (
                  <div style={{ marginBottom: 16, fontSize: 15 }}>
                    <span className={op.cls === "muted" ? "muted" : op.cls}
                      style={{ fontWeight: 600, padding: "3px 10px", borderRadius: 6, background: "#f1efe8", border: "1px solid var(--border)" }}>
                      투자의견: {op.txt}
                    </span>{" "}
                    <span className="muted" style={{ fontSize: 12 }}>
                      (이 조건의 자산 평균 {(result.assetMean >= 0 ? "+" : "") + result.assetMean.toFixed(2)}% 기준)
                    </span>
                  </div>
                )}
              </>
            ) : (
              <div className="muted" style={{ marginBottom: 16 }}>해당 조건에 맞는 이벤트가 없습니다 — 범위를 넓혀 보세요.</div>
            )
          )}

          {/* 전체기간 가격차트 + 매칭 구간 음영 */}
          <div className="muted" style={{ fontSize: 13, margin: "8px 0 6px" }}>
            전체기간 가격 — <span style={{ color: "var(--accent-strong)", fontWeight: 600 }}>음영</span> = 종가{applied ? ` ${applied.nLo <= -1e8 ? "−∞" : priceSym + applied.nLo}~${applied.nHi >= 1e8 ? "∞" : priceSym + applied.nHi}` : ""} <b>∧</b> 증감율{applied ? ` ${applied.lo <= -1e8 ? "−∞" : applied.lo + "%"}~${applied.hi >= 1e8 ? "∞" : applied.hi + "%"}` : ""} <b>둘 다</b> 만족하는 진입 구간 [−{applied?.L ?? dL} ~ +{applied?.H ?? dH}일]{matched.length ? ` · ${matched.length}건` : ""}.
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

          {/* 히트맵 (side-aware) */}
          {applied && !loading && heat && buckets && (
            <>
              <div className="muted" style={{ fontSize: 13, margin: "16px 0 8px" }}>
                종가대별 {applied.side === "long" ? "롱" : "숏"} 수익률 히트맵 — 행=가격대, 열=진입 직전 증감율(5%p), 셀=향후 {applied.H}일 평균.
                선택 종가범위 행 ◀, n&lt;30은 ⚠.
              </div>
              <div className="table-scroll sticky-table" style={{ maxHeight: 440 }}>
                <table className="te-hm">
                  <thead>
                    <tr><th style={{ textAlign: "left" }}>종가대</th>{TE_RET_BUCKETS.map((rb) => <th key={rb.label}>{rb.label}</th>)}</tr>
                  </thead>
                  <tbody>
                    {buckets.arr.map((b, pi) => {
                      const sel = rowInSel(b);
                      return (
                        <tr key={pi}>
                          <th style={{ textAlign: "left", color: sel ? "#d97757" : undefined }}>
                            {priceSym}{b.lo.toFixed(b.dec)}–{priceSym}{b.hi.toFixed(b.dec)}{sel ? " ◀" : ""}
                          </th>
                          {heat.grid[pi].map((c, ri) => {
                            const mean = c.n ? c.sum / c.n : NaN;
                            return (
                              <td key={ri} title={c.n ? `n=${c.n}, 평균 ${(mean >= 0 ? "+" : "") + mean.toFixed(2)}%` : "표본 없음"}
                                style={{ background: c.n ? heatColor(mean, heat.maxAbs) : "#f1efe8", color: "#fff", borderRadius: 5,
                                  opacity: c.n ? 1 : 0.25, outline: sel ? "2px solid #d97757" : "none" }}>
                                {c.n ? (<><div style={{ fontWeight: 600 }}>{(mean >= 0 ? "+" : "") + mean.toFixed(1)}%</div>
                                  <div style={{ fontSize: 10, opacity: 0.85 }}>n={c.n}{c.n < 30 ? " ⚠" : ""}</div></>) : "·"}
                              </td>
                            );
                          })}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {/* 회귀 (side-aware) */}
          {applied && !loading && reg && pastRange && (
            <div style={{ marginTop: 20, paddingTop: 16, borderTop: "1px solid var(--border)" }}>
              <div className="muted" style={{ fontSize: 13, marginBottom: 8 }}>
                회귀분석 — 과거 <b>{applied.L}일</b> 증감율(x) → 미래 <b>{applied.H}일</b> {applied.side === "long" ? "롱" : "숏"} 수익률(y) · 전체 {reg.n.toLocaleString()}건
              </div>
              <div className="bt-metrics" style={{ marginBottom: 10 }}>
                <Metric label="기울기 β" value={(reg.slope * sign >= 0 ? "+" : "") + (reg.slope * sign).toFixed(2)}
                  highlight={reg.slope * sign >= 0 ? "good" : "bad"}
                  sub={reg.slope * sign >= 0 ? "추세↑→수익↑" : "추세↑→수익↓"} />
                <Metric label="R²" value={reg.r_squared.toFixed(3)} sub="설명력(0~1)" />
                <Metric label="p-value (HAC)" value={reg.hac_p_value.toFixed(3)}
                  highlight={reg.hac_p_value < 0.05 ? "good" : "warn"} sub="겹침보정 후" />
                <Metric label="표본 n" value={reg.n} highlight={reg.n < 30 ? "warn" : null} />
              </div>
              <ResponsiveContainer width="100%" height={280}>
                <ScatterChart margin={{ top: 8, right: 16, bottom: 16, left: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#e8e3db" />
                  <XAxis type="number" dataKey="x" name="과거 증감율" domain={["dataMin", "dataMax"]}
                    tick={{ fontSize: 10, fill: "#6f6a62" }} tickFormatter={(v) => v + "%"} />
                  <YAxis type="number" dataKey="y" name="미래 수익률" tick={{ fontSize: 10, fill: "#6f6a62" }} tickFormatter={(v) => v + "%"} />
                  <Tooltip cursor={{ strokeDasharray: "3 3" }} formatter={(v) => `${Number(v).toFixed(2)}%`} />
                  <ReferenceLine y={0} stroke="#6f6a62" />
                  {regSeg && <ReferenceLine segment={regSeg} stroke="#d97757" strokeWidth={2} />}
                  <Scatter data={scatterPts} fill="#d97757" fillOpacity={0.5} />
                </ScatterChart>
              </ResponsiveContainer>
            </div>
          )}

          <div className="muted" style={{ fontSize: 11, marginTop: 12 }}>
            ⚠️ 수익률은 <b>종가-종가 서술용</b>(실 백테스트의 익일 시가·비용·청산룰과 다름 — 관계 측정용). forward 윈도우 겹침→HAC 보정.
            범위를 좁힐수록 표본↓·통계 불안정(n&lt;30 ⚠).
          </div>
        </>
      )}
    </>
  );
}

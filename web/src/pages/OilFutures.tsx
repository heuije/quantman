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
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  oilApi,
  type OilBacktest,
  type OilDataInfo,
  type OilGridCell,
  type OilLatestPrice,
  type OilMacroContext,
  type OilSeasonality,
  type OilWalkForward,
} from "../api";

// 임계값 grid — $10 단위 (Railway 무료 티어 메모리 호환). $1 단위는
// 488 셀 컴퓨팅 시 OOM 발생, 추후 캐시 layer 추가 후 복원 예정.
const DEFAULT_SHORTS = [80, 90, 100, 110, 120, 130, 140, 150];
const DEFAULT_LONGS = [10, 20, 30, 40, 50, 60];
// Horizon (영업일 보유 기간) — 365일까지 확장 (장기 평균회귀 패턴 검증용)
const DEFAULT_HORIZONS = [20, 40, 60, 120, 180, 240, 365];

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

  // 🅒 SL/TP — null이면 비활성. 백테스트 재호출 트리거.
  const [sl, setSl] = useState<number | "">("");        // 예: 10 = -10%
  const [tp, setTp] = useState<number | "">("");        // 예: 20 = +20%
  const [rollCost, setRollCost] = useState<number | "">("");  // 롤 비용 %/회 (예: 0.5)

  // ── 초기 로드 ─────────────────────────────────────────────────────
  useEffect(() => {
    oilApi.dataInfo().then(setInfo).catch((e) => console.error("data-info", e));
    oilApi.seasonality().then(setSeason).catch((e) => console.error("seasonality", e));
    oilApi.macroContext().then(setMacro).catch((e) => console.error("macro", e));

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

    // 실시간 현재가 — 즉시 + 60초마다 갱신
    const loadPrice = () =>
      oilApi.latestPrice().then(setPrice).catch((e) => console.error("price", e));
    loadPrice();
    const priceTimer = setInterval(loadPrice, 60_000);
    return () => clearInterval(priceTimer);
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
        stop_loss_pct: sl === "" ? null : sl / 100,
        take_profit_pct: tp === "" ? null : tp / 100,
        roll_cost_pct: rollCost === "" ? 0 : rollCost / 100,
      })
      .then(setBacktest)
      .catch((e) => console.error("backtest", e))
      .finally(() => setBtLoading(false));
  }, [selected, sl, tp, rollCost]);

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
        <div className="oil-title-row">
          <div>
            <div className="oil-eyebrow">CRUDE OIL · NYMEX</div>
            <h1>WTI Crude Oil Futures Analytics</h1>
          </div>
          {price && <LivePriceTag price={price} />}
        </div>
        <p className="muted">
          장중 high/low가 임계값을 첫 터치하면 신호 → N영업일 보유 백테스트.
        </p>
      </header>

      {/* ① 데이터 메타 */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">DATA OVERVIEW · 데이터 메타</h2>
        {!info ? (
          <div className="muted">로딩 중…</div>
        ) : (
          <div className="meta-grid">
            <div><div className="muted">실시간 현재가</div>
              <div className="meta-value">
                {price ? `$${price.price.toFixed(2)}` : "—"}
                <span className="meta-unit">/배럴</span>
                {price?.change_pct != null && (
                  <span className={"meta-delta " + (price.change_pct >= 0 ? "pos" : "neg")}>
                    {price.change_pct >= 0 ? "▲" : "▼"} {Math.abs(price.change_pct * 100).toFixed(2)}%
                  </span>
                )}
              </div>
              {price && (
                <div className="meta-source">
                  {price.source}{price.delayed ? " · ~15분 지연" : " · 실시간"}
                </div>
              )}
            </div>
            <div><div className="muted">기간</div>
              <div className="meta-value meta-value-range">{info.start_date} ~ {info.end_date}</div></div>
            <div><div className="muted">영업일</div>
              <div className="meta-value">D+{info.n_rows.toLocaleString()} <span className="meta-sub-inline">(~{Math.round(info.n_rows / 252)}년)</span></div></div>
            <div><div className="muted">가격 범위 (23년)</div>
              <div className="meta-value">${info.price_min.toFixed(2)} ~ ${info.price_max.toFixed(2)}</div></div>
          </div>
        )}
      </section>

      {/* ② 히트맵 */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">PnL HEATMAP · 임계값 × 보유기간</h2>
        <p className="muted" style={{ marginBottom: 12 }}>
          셀: <b>거래당 평균수익률</b> / <b>승률</b> (소수점 1자리).
          색 진하기 = Net PnL 크기. <span style={{ color: "#62c884" }}>녹색=수익</span>,{" "}
          <span style={{ color: "#d96265" }}>빨강=손실</span>.{" "}
          low_sample(n&lt;30)은 같은 색이되 채도 낮춤(부호는 보존). 클릭하면 백테스트 상세.
        </p>
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
              ? "Short — $80~$150 (위로 첫 터치)"
              : "Long — $10~$60 (아래로 첫 터치)"}
            rows={heatmapSide === "short" ? heatmaps.short : heatmaps.long}
            max={heatmaps.max}
            selected={selected}
            onSelect={setSelected}
          />
        )}
      </section>

      {/* ③ 백테스트 상세 (조합 순위표보다 먼저) */}
      <section className="panel" style={{ marginBottom: 16 }}>
        <h2 className="section-title">
          BACKTEST DETAIL · 백테스트 상세 {selected && <span className="title-tag">{selected.side.toUpperCase()} ${selected.threshold} × {selected.horizon}D</span>}
        </h2>

        {/* 🅒 SL/TP 시뮬레이터 */}
        <div className="oil-toolbar sltp-toolbar">
          <span style={{ fontWeight: 600 }}>SL/TP 시뮬레이터:</span>
          <label>
            Stop-Loss&nbsp;
            <input
              type="number" min={0} max={100} step={1}
              value={sl}
              placeholder="off"
              onChange={(e) => setSl(e.target.value === "" ? "" : Number(e.target.value))}
              style={{ width: 64 }}
            />
            &nbsp;%
          </label>
          <label>
            Take-Profit&nbsp;
            <input
              type="number" min={0} max={200} step={1}
              value={tp}
              placeholder="off"
              onChange={(e) => setTp(e.target.value === "" ? "" : Number(e.target.value))}
              style={{ width: 64 }}
            />
            &nbsp;%
          </label>
          <button onClick={() => { setSl(""); setTp(""); }} className="ghost">
            리셋 (horizon 만기 보유)
          </button>
          <span className="muted">
            진입가 대비 % — 장중 high/low 기준 hit 즉시 청산. 둘 다 비우면 기존 horizon 보유.
          </span>
        </div>

        {/* 선물 만기 강제 롤오버 비용 시뮬레이터 */}
        <div className="oil-toolbar sltp-toolbar roll-toolbar">
          <span style={{ fontWeight: 600 }}>🛢 만기 롤오버 비용:</span>
          <label>
            롤 비용&nbsp;
            <input
              type="number" min={-5} max={5} step={0.1}
              value={rollCost}
              placeholder="0"
              onChange={(e) => setRollCost(e.target.value === "" ? "" : Number(e.target.value))}
              style={{ width: 72 }}
            />
            &nbsp;% / 롤
          </label>
          <span className="roll-quick">
            <button className="ghost" onClick={() => setRollCost(0.5)}>콘탱고 +0.5%</button>
            <button className="ghost" onClick={() => setRollCost(-2)}>backwardation −2%</button>
            <button className="ghost" onClick={() => setRollCost("")}>리셋(0%)</button>
          </span>
          <div className="muted roll-help">
            WTI는 실물 인수도 → 만기마다 강제 롤오버 (보유 ÷ 21일 ≈ 롤 횟수).
            <b> 양수 = contango 비용(차감), 음수 = backwardation 이익(가산).</b><br />
            현재(2026-05) WTI는 <b style={{ color: "var(--green)" }}>backwardation(역조)</b> —
            근월 $87.4 vs 8월물 $85.3(−2.4%), 12월물 $78.3(−10%) → 롤 시 오히려 이익이라
            <b> −2% 정도</b> 입력이 현실적. 역사적 평균은 국면마다 달라(콘탱고 +0.3~1%/월 ~
            슈퍼콘탱고 +10%, 또는 backwardation 시 이익) <b>고정값 없음</b>.
            <span style={{ color: "#c9a227" }}> ⚠️ 추정 가정 — 정확한 롤 yield는 만기물별 데이터 필요.</span>
          </div>
        </div>

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
        {wf && <WalkForwardView wf={wf} />}
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
      <section className="panel">
        <h2 className="section-title">MACRO CONTEXT · 외생 변수 (VIX · DXY)</h2>
        {!macro ? (
          <div className="muted">로딩 중…</div>
        ) : !macro.available ? (
          <div className="muted">macro_daily.csv 미배포 — 데이터 갱신 필요</div>
        ) : (
          <MacroView m={macro} />
        )}
      </section>
    </div>
  );
}

// 헤더 우측 실시간 현재가 태그 (Bloomberg/TradingView 스타일)
function LivePriceTag({ price }: { price: OilLatestPrice }) {
  const up = (price.change_pct ?? 0) >= 0;
  return (
    <div className="live-price">
      <div className="live-price-main">
        <span className="live-price-val">${price.price.toFixed(2)}</span>
        {price.change_pct != null && (
          <span className={"live-price-chg " + (up ? "pos" : "neg")}>
            {up ? "▲" : "▼"} {price.change != null ? Math.abs(price.change).toFixed(2) : "—"}
            {" "}({Math.abs(price.change_pct * 100).toFixed(2)}%)
          </span>
        )}
      </div>
      <div className="live-price-src">
        <span className={"live-dot " + (price.delayed ? "delayed" : "live")} />
        {price.source}{price.delayed ? " · ~15분 지연" : " · LIVE"}
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

function EquityTooltip({ active, payload, label }: {
  active?: boolean;
  payload?: EquityTooltipPayload[];
  label?: string;
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
          PnL: <b>{((ev.payload.net_pnl_usd ?? 0) >= 0 ? "+" : "-") + "$" +
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
          {p.name}: $
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
        WTI 일간 수익률과 VIX(공포지수)/DXY(달러지수)의 관계. 외생 변수가 신호 가치에
        어떤 영향을 주는지 측정 (전통적 가설: WTI ↔ VIX 음, WTI ↔ DXY 음). 일별 종가 기준,{" "}
        <b>{m.coverage_days.toLocaleString()}</b>일 표본.
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
        <RegimeTable title="VIX 체제별 WTI 평균 일간 수익률" rows={m.vix_regime} />
        <RegimeTable title="DXY(달러) 체제별 WTI 평균 일간 수익률" rows={m.dxy_regime} />
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
              <td className={r.wti_avg_return >= 0 ? "pos" : "neg"}>
                {(r.wti_avg_return >= 0 ? "+" : "") + (r.wti_avg_return * 100).toFixed(3)}%
              </td>
              <td>{(r.wti_win_rate * 100).toFixed(1)}%</td>
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
        <Metric label="MDD (realized, USD)" value={usd(s.mdd_usd)} highlight="bad"
                sub="청산 시점 누적 PnL 곡선의 peak-trough" />
        <Metric label="MDD (시가평가, USD)" value={usd(bt.portfolio_mdd_usd)} highlight="bad"
                sub="🅓 매일 mark-to-market 포트폴리오 가치 곡선의 peak-trough" />
        <Metric label="Profit (USD)" value={usd(s.gross_profit_usd)} highlight="good" />
        <Metric label="Loss (USD)" value={usd(s.gross_loss_usd)} highlight="bad" />
        <Metric label="Net PnL (USD)" value={usd(s.net_pnl_usd)}
                highlight={s.net_pnl_usd >= 0 ? "good" : "bad"} />
      </div>

      {/* 🅐 MAE/MFE 분석 (장중 평가손익) */}
      <div className="bt-subgrid">
        <div className="subgrid-title">🅐 장중 평가손익 (MAE/MFE) — 시가평가 위험 가시화</div>
        <div className="bt-metrics">
          <Metric
            label="Worst MAE (장중 최악)"
            value={usd(s.worst_mae_usd)}
            highlight="bad"
            sub="모든 trade 중 가장 깊은 평가손실 — 시가평가 MDD에 근접"
          />
          <Metric
            label="Avg MAE (평균 평가손실)"
            value={usd(s.avg_mae_usd)}
            highlight="bad"
            sub="거래당 평균 장중 최악 평가손실"
          />
          <Metric
            label="Avg MFE (평균 평가이익)"
            value={usd(s.avg_mfe_usd)}
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
            label="롤 손익 합계 (USD)"
            value={usd(s.total_roll_cost_usd)}
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
            <Tooltip content={<EquityTooltip />} />
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
                <th>MAE($)</th>
                <th>MFE($)</th>
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
                  <td><ExitReasonBadge reason={t.exit_reason} /></td>
                  <td className={t.return_pct >= 0 ? "pos" : "neg"}>{pct(t.return_pct, 2)}</td>
                  <td title={t.roll_cost_usd < 0 ? `롤 비용 ${usd(t.roll_cost_usd)}` : ""}>
                    {t.num_rollovers}{t.roll_cost_usd < 0 ? "🛢" : ""}
                  </td>
                  <td className="neg" title="장중 최악 평가손실">{usd(t.mae_usd)}</td>
                  <td className="pos" title="장중 최고 평가이익">{usd(t.mfe_usd)}</td>
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

import { useState, useEffect, type ReactNode } from "react";
import {
  ComposedChart, LineChart, Line, Bar, Scatter, XAxis, YAxis, Tooltip,
  ResponsiveContainer, ReferenceLine, CartesianGrid, Legend, Brush, Cell,
} from "recharts";
import { api } from "../api";
import type { SymbolDetail, SymbolListing, SymbolPoint, CompareItem } from "../types";

const RANGES: [string, string][] = [
  ["1m", "1개월"], ["3m", "3개월"], ["6m", "6개월"], ["12m", "12개월"], ["1y", "1년"],
  ["3y", "3년"], ["5y", "5년"], ["10y", "10년"], ["15y", "15년"],
  ["20y", "20년"], ["25y", "25년"], ["30y", "30년"],
];

// 한국식 시장 색 (상승=빨강 / 하락=파랑). 벤치마크=초록.
const UP = "#de3033", DOWN = "#1668c4", BENCH = "#15803d", ACCENT = "#d97757";

// 이동평균 5종 — 모든(가격·거래량) 차트에 상시 표시.
const PRICE_MA: [keyof SymbolPoint, string, string][] = [
  ["ma5", "MA5", "#9aa0a6"], ["ma20", "MA20", "#d97757"], ["ma60", "MA60", "#1668c4"],
  ["ma120", "MA120", "#7c3aed"], ["ma240", "MA240", "#b45309"],
];
const VOL_MA: [keyof SymbolPoint, string, string][] = [
  ["vma5", "MA5", "#9aa0a6"], ["vma20", "MA20", "#d97757"], ["vma60", "MA60", "#1668c4"],
  ["vma120", "MA120", "#7c3aed"], ["vma240", "MA240", "#b45309"],
];

const IND_COLORS: Record<string, string> = {
  rsi_14: "#ad5019", macd: "#d97757", macd_signal: "#1668c4",
  stoch_k: "#ad5019", stoch_d: "#1668c4", atr_14: "#7c3aed",
  obv: "#22a06b", vol_20d: "#e0823d",
};
const SUB_LABEL: Record<string, string> = {
  rsi_14: "RSI (14)", macd: "MACD (12,26,9)", stoch: "스토캐스틱 (14,3)",
  volume: "거래량", atr_14: "ATR (14)", obv: "OBV", vol_20d: "변동성 (20일)",
  bb: "볼린저밴드 (20,2σ)",
};
const DEFAULT_SEL = ["rsi_14", "macd", "volume", "stoch"];   // 최대 4개

// 다종목 비교 팔레트 (최대 10)
const CMP_COLORS = ["#de3033", "#1668c4", "#15803d", "#d97757", "#7c3aed",
  "#b45309", "#0891b2", "#be185d", "#4d7c0f", "#9333ea"];

// 요약박스 — 현재가·베타는 고정, 나머지 2개는 아래 후보에서 선택.
const METRIC_OPTS: [string, string][] = [
  ["rsi", "RSI (14)"], ["range52", "52주 고/저"], ["volume", "거래량"],
  ["macd", "MACD"], ["stoch", "스토캐스틱"], ["atr", "ATR (14)"], ["vol20", "변동성 (20일)"],
];

// 급등락 세모 마커 (위=급등 빨강 / 아래=급락 파랑)
function TriUp({ cx, cy }: { cx?: number; cy?: number }) {
  if (cx == null || cy == null) return null;
  return <path d={`M ${cx} ${cy - 13} L ${cx - 6} ${cy - 5} L ${cx + 6} ${cy - 5} Z`}
    fill={UP} stroke="#fff" strokeWidth={0.6} />;
}
function TriDown({ cx, cy }: { cx?: number; cy?: number }) {
  if (cx == null || cy == null) return null;
  return <path d={`M ${cx} ${cy + 13} L ${cx - 6} ${cy + 5} L ${cx + 6} ${cy + 5} Z`}
    fill={DOWN} stroke="#fff" strokeWidth={0.6} />;
}

// 캔들(틱) — recharts엔 캔들이 없어 [low,high] 플로팅 바의 픽셀 좌표(y/height)에서
// open/close 픽셀을 선형 보간해 직접 그린다. 상승(종가≥시가)=빨강 / 하락=파랑.
function Candle(props: {
  x?: number; y?: number; width?: number; height?: number;
  payload?: SymbolPoint;
}) {
  const { x, y, width, height, payload } = props;
  if (x == null || y == null || width == null || height == null || !payload) return null;
  const { open, high, low, close } = payload;
  if (open == null || high == null || low == null || close == null) return null;
  const color = close >= open ? UP : DOWN;
  const cx = x + width / 2;
  const span = high - low;
  const pxPer = span ? height / span : 0;
  const yOpen = y + (high - open) * pxPer;
  const yClose = y + (high - close) * pxPer;
  const bodyTop = Math.min(yOpen, yClose);
  const bodyH = Math.max(Math.abs(yClose - yOpen), 1);
  const bw = Math.max(Math.min(width * 0.62, 10), 2);
  return (
    <g>
      <line x1={cx} y1={y} x2={cx} y2={y + height} stroke={color} strokeWidth={1} />
      <rect x={cx - bw / 2} y={bodyTop} width={bw} height={bodyH} fill={color} />
    </g>
  );
}

export default function StockDashboard() {
  const [symbols, setSymbols] = useState<string[]>(["005930"]);
  const [input, setInput] = useState("");
  const [range, setRange] = useState("1y");
  const [data, setData] = useState<SymbolDetail | null>(null);
  const [cmp, setCmp] = useState<CompareItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [listings, setListings] = useState<SymbolListing[]>([]);
  const [selected, setSelected] = useState<string[]>(DEFAULT_SEL);
  const [focused, setFocused] = useState(false);
  const [box3, setBox3] = useState("rsi");
  const [box4, setBox4] = useState("range52");

  const nameMap = new Map(listings.map((l) => [l.symbol, l.name]));
  const nameOf = (s: string) => nameMap.get(s) || s;
  const compareMode = symbols.length >= 2;

  async function loadFor(syms: string[], rng: string) {
    if (syms.length === 0) return;
    setBusy(true); setErr("");
    try {
      if (syms.length === 1) {
        const d = await api.symbolDetail(syms[0], rng);
        setData(d); setCmp(null);
      } else {
        const r = await api.marketCompare(syms, rng);
        setCmp(r.items); setData(null);
      }
      setRange(rng);
    } catch (e) { setErr((e as Error).message); }
    finally { setBusy(false); }
  }

  useEffect(() => {
    loadFor(["005930"], "1y");
    api.marketListings().then((r) => setListings(r.listings)).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 종목 추가/제거 (최대 10) — 부수효과는 updater 밖에서(StrictMode 이중호출 방지)
  function addSymbol(sym: string) {
    const s = sym.trim().toUpperCase();
    if (!s || symbols.includes(s) || symbols.length >= 10) { setInput(""); return; }
    setInput(""); setFocused(false);
    const next = [...symbols, s];
    setSymbols(next);
    loadFor(next, range);
  }
  function removeSymbol(sym: string) {
    const next = symbols.filter((s) => s !== sym);
    if (!next.length) return;
    setSymbols(next);
    loadFor(next, range);
  }

  // 검색 드롭다운 — 입력 있으면 코드/이름 필터, 없으면 상위 목록
  const q = input.trim().toUpperCase();
  const matches = (q
    ? listings.filter((l) => l.symbol.includes(q) || l.name.toUpperCase().includes(q))
    : listings
  ).slice(0, 60);

  const indicators = data?.indicators || [];
  function toggle(key: string) {
    setSelected((s) => s.includes(key) ? s.filter((k) => k !== key)
      : s.length < 4 ? [...s, key] : s);
  }

  const isKR = data?.currency === "KRW";
  const fmtP = (v: number | null | undefined) =>
    v == null ? "—" : isKR ? Math.round(v).toLocaleString()
      : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const won = isKR ? "원" : "", dol = isKR ? "" : "$";
  const last = data?.last;
  const chgColor = last?.change_pct != null ? (last.change_pct >= 0 ? UP : DOWN) : "var(--muted)";

  // 단일 종목 차트 데이터 + 캔들/급등락 컬럼
  const chartData = (data?.series || []).map((p, i, arr) => ({
    ...p,
    hl: p.low != null && p.high != null ? [p.low, p.high] : null,
    up_spike: p.chg_pct != null && p.chg_pct >= 10 ? p.high : null,
    down_spike: p.chg_pct != null && p.chg_pct <= -10 ? p.low : null,
    vol_chg: i > 0 && arr[i - 1].volume && p.volume != null
      ? (p.volume - (arr[i - 1].volume as number)) / (arr[i - 1].volume as number) * 100 : null,
  }));

  // 가격축 도메인 — 캔들·MA·벤치마크 전부 포함하도록 타이트하게(0 강제 방지).
  // 긴 구간(수천 일)에서 spread 인자 한계를 피하려고 reduce로 min/max 계산.
  let pmin = Infinity, pmax = -Infinity;
  chartData.forEach((d) => {
    ([d.low, d.high, d.bench, d.ma5, d.ma20, d.ma60, d.ma120, d.ma240] as (number | null)[])
      .forEach((v) => { if (v != null) { if (v < pmin) pmin = v; if (v > pmax) pmax = v; } });
  });
  const priceDomain: [number, number] = pmin <= pmax
    ? [Math.floor(pmin * 0.985), Math.ceil(pmax * 1.015)] : [0, 1];

  return (
    <div className="dashboard-fullwidth">
      <h1 style={{ marginBottom: 4 }}>Company Analysis</h1>
      <p style={{ color: "var(--muted)", marginTop: 0 }}>
        한국·미국 개별 종목·ETF·ETN을 캔들·지표로 분석합니다. <b>종목명 또는 코드</b>로 검색하고,
        벤치마크(코스피/코스닥/나스닥) 초록 점선과 비교하거나 <b>최대 10종목까지 수익률 비교</b>가 가능합니다.
      </p>

      {/* 검색 + 선택 종목 칩 + 기간 */}
      <div className="panel" style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ position: "relative", width: 320 }}>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onFocus={() => setFocused(true)}
            onBlur={() => setTimeout(() => setFocused(false), 150)}
            onKeyDown={(e) => { if (e.key === "Enter" && matches[0]) addSymbol(matches[0].symbol); }}
            placeholder="종목명·코드 검색 후 추가 (삼성전자 / 005930 / AAPL)"
            aria-label="종목 검색"
            style={{ width: "100%" }}
          />
          {focused && matches.length > 0 && (
            <ul style={{
              position: "absolute", top: "100%", left: 0, right: 0, zIndex: 20,
              margin: 0, padding: 0, listStyle: "none", maxHeight: 320, overflowY: "auto",
              background: "#fff", border: "1px solid var(--border,#e8e3db)", borderRadius: 8,
              boxShadow: "0 6px 20px rgba(0,0,0,0.12)",
            }}>
              {matches.map((l) => (
                <li key={l.symbol}
                  onMouseDown={() => addSymbol(l.symbol)}
                  style={{ padding: "7px 12px", cursor: "pointer", fontSize: 13,
                    display: "flex", justifyContent: "space-between", gap: 8 }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = "var(--accent-soft,#f7ece5)")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "#fff")}>
                  <span>{l.name}</span>
                  <span style={{ color: "var(--muted)" }}>{l.symbol} · {l.market}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <label style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
          <span style={{ color: "var(--muted)" }}>기간</span>
          <select value={range} onChange={(e) => loadFor(symbols, e.target.value)} aria-label="조회 기간">
            {RANGES.map(([v, lbl]) => <option key={v} value={v}>{lbl}</option>)}
          </select>
        </label>
        {/* 선택 종목 칩 */}
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", width: "100%" }}>
          {symbols.map((s, i) => (
            <span key={s} className="ca-symchip">
              <span style={{ width: 9, height: 9, borderRadius: 2, flexShrink: 0,
                background: compareMode ? CMP_COLORS[i % CMP_COLORS.length] : ACCENT }} />
              {nameOf(s)} <span style={{ color: "var(--muted)" }}>{s}</span>
              {symbols.length > 1 && (
                <button className="x-btn" aria-label={`${nameOf(s)} 제거`}
                  onClick={() => removeSymbol(s)}>✕</button>
              )}
            </span>
          ))}
          <span style={{ fontSize: 12, color: "var(--muted)", alignSelf: "center" }}>
            {busy ? "조회 중…" : compareMode ? `${symbols.length}/10 비교 중 — 검색해서 추가`
              : "검색해서 종목을 추가하면 비교 모드"}
          </span>
        </div>
      </div>

      {err && <div className="error" style={{ marginTop: 12 }}>{err}</div>}

      {/* ── 다종목 비교 모드 ── */}
      {compareMode && cmp && (
        <CompareChart items={cmp} />
      )}

      {/* ── 단일 종목 모드 ── */}
      {!compareMode && data && last && (
        <>
          {/* 요약 박스 — 현재가·베타 고정 + 사용자 선택 2개 */}
          <div className="ca-summary">
            <Card title={`${nameOf(data.symbol)} 현재가`}>
              <div style={{ fontSize: 22, fontWeight: 700 }}>{dol}{fmtP(last.close)}{won}</div>
              <div style={{ color: chgColor, fontWeight: 600 }}>
                {last.change_pct != null && last.change_pct >= 0 ? "▲" : "▼"} {last.change_pct?.toFixed(2)}%
              </div>
            </Card>
            <Card title={`베타 (β) vs ${last.benchmark}`}>
              <div style={{ fontSize: 22, fontWeight: 700,
                color: last.beta == null ? "var(--muted)" : last.beta > 1 ? UP : last.beta < 1 ? DOWN : "inherit" }}>
                {last.beta ?? "—"}
              </div>
              <div style={{ fontSize: 12, color: "var(--muted)" }}>
                {last.benchmark} 1.00 · {last.beta == null ? "" : last.beta > 1 ? "변동 더 큼" : "변동 더 작음"}
              </div>
            </Card>
            <MetricCard sel={box3} onSel={setBox3} last={last} fmtP={fmtP} dol={dol} won={won} />
            <MetricCard sel={box4} onSel={setBox4} last={last} fmtP={fmtP} dol={dol} won={won} />
          </div>

          {/* 지표 선택 (최대 4) */}
          <div className="panel" style={{ marginTop: 16 }}>
            <details>
              <summary style={{ cursor: "pointer", fontWeight: 600 }}>
                지표 선택 — {selected.length}/4 (4분할 그리드에 표시 · 이동평균 5종은 모든 차트 상시)
              </summary>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(160px,1fr))", gap: 6, marginTop: 10 }}>
                {indicators.map((i) => {
                  const on = selected.includes(i.key);
                  const dis = !on && selected.length >= 4;
                  return (
                    <label key={i.key} style={{ fontSize: 13, opacity: dis ? 0.4 : 1, cursor: dis ? "not-allowed" : "pointer" }}>
                      <input type="checkbox" checked={on} disabled={dis} onChange={() => toggle(i.key)} />
                      {" "}{i.label}
                    </label>
                  );
                })}
              </div>
            </details>
          </div>

          {/* 주가 캔들차트 (전체폭) — 캔들 + MA5종 + 벤치마크 점선 + 급등락 */}
          <div className="panel" style={{ marginTop: 16 }}>
            <h3 style={{ marginTop: 0 }}>
              주가 추이 (캔들) · 이동평균 · 벤치마크({last.benchmark}) · 급등락(±10%)
            </h3>
            <ResponsiveContainer width="100%" height={420}>
              <ComposedChart data={chartData} margin={{ top: 5, right: 16, bottom: 5, left: 8 }}>
                <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
                <YAxis domain={priceDomain} tick={{ fontSize: 11 }} width={56}
                  tickFormatter={(v) => isKR ? `${Math.round(v / 1000)}k` : String(v)} />
                <Tooltip content={({ active, payload }) => (
                  <PriceTooltip active={active}
                    payload={payload as readonly PriceTipEntry[] | undefined}
                    isKR={isKR} dol={dol} won={won} benchName={last.benchmark} />
                )} />
                <Legend />
                <Bar dataKey="hl" isAnimationActive={false} legendType="none" shape={Candle} />
                {PRICE_MA.map(([k, lbl, col]) => (
                  <Line key={k} type="monotone" dataKey={k} stroke={col} strokeWidth={1} dot={false} name={lbl} />
                ))}
                <Line type="monotone" dataKey="bench" stroke={BENCH} strokeWidth={1.4}
                  strokeDasharray="6 4" dot={false} name={`${last.benchmark}(벤치마크)`} connectNulls />
                <Scatter dataKey="up_spike" shape={<TriUp />} name="급등 +10%" legendType="triangle" fill={UP} />
                <Scatter dataKey="down_spike" shape={<TriDown />} name="급락 −10%" legendType="triangle" fill={DOWN} />
                <Brush dataKey="date" height={24} stroke={ACCENT} travellerWidth={8} />
              </ComposedChart>
            </ResponsiveContainer>
            <ExplToggle ikey="price" series={data.series} isKR={isKR} dol={dol} won={won} benchName={last.benchmark} />
          </div>

          {/* 4분할 — 선택 지표 2×2 */}
          {selected.length > 0 ? (
            <div className="ca-grid" style={{ marginTop: 16 }}>
              {selected.map((k) => (
                <div key={k} className="panel" style={{ marginBottom: 0 }}>
                  <SubChart ikey={k} data={chartData} isKR={isKR} />
                  <ExplToggle ikey={k} series={data.series} isKR={isKR} dol={dol} won={won} benchName={last.benchmark} />
                </div>
              ))}
            </div>
          ) : (
            <p style={{ color: "var(--muted)", marginTop: 12, fontSize: 13 }}>
              위 “지표 선택”에서 RSI·MACD·거래량 등을 고르면 여기에 4분할로 표시됩니다.
            </p>
          )}
        </>
      )}
    </div>
  );
}

// ── 다종목 비교 차트 (수익률% 정규화) ──────────────────────────────────────────
function CompareChart({ items }: { items: CompareItem[] }) {
  // 날짜 합집합으로 병합 (시장별 거래일 상이 → 누락은 connectNulls)
  const dateSet = new Set<string>();
  items.forEach((it) => it.series.forEach((p) => dateSet.add(p.date)));
  const dates = Array.from(dateSet).sort();
  const bySym = new Map(items.map((it) => [it.symbol, new Map(it.series.map((p) => [p.date, p.ret_pct]))]));
  const rows = dates.map((d) => {
    const row: Record<string, string | number | null> = { date: d };
    items.forEach((it) => { row[it.symbol] = bySym.get(it.symbol)?.get(d) ?? null; });
    return row;
  });
  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <h3 style={{ marginTop: 0 }}>
        다종목 수익률 비교 (시작점 = 0%)
        <span style={{ fontSize: 12, color: "var(--muted)", fontWeight: 400 }}>
          {"  "}· 비교 모드에선 지표·벤치마크는 표시하지 않습니다
        </span>
      </h3>
      <ResponsiveContainer width="100%" height={460}>
        <LineChart data={rows} margin={{ top: 5, right: 16, bottom: 5, left: 8 }}>
          <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
          <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
          <YAxis tick={{ fontSize: 11 }} width={50} tickFormatter={(v) => `${v}%`} />
          <Tooltip formatter={(v, n) => [`${Number(v).toFixed(2)}%`, n as string]} />
          <Legend />
          <ReferenceLine y={0} stroke="#9aa0a6" />
          {items.map((it, i) => (
            <Line key={it.symbol} type="monotone" dataKey={it.symbol}
              name={`${it.name} (${it.symbol})`} stroke={CMP_COLORS[i % CMP_COLORS.length]}
              strokeWidth={1.6} dot={false} connectNulls />
          ))}
          <Brush dataKey="date" height={24} stroke={ACCENT} travellerWidth={8} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── 서브 지표 차트 (4분할 그리드 1칸) ──────────────────────────────────────────
function SubChart({ ikey, data, isKR }: { ikey: string; data: Record<string, unknown>[]; isKR: boolean }) {
  const title = SUB_LABEL[ikey] || ikey;
  const M = { top: 5, right: 12, bottom: 5, left: 8 };
  return (
    <>
      <h3 style={{ marginTop: 0 }}>
        {title}
        {ikey === "volume" && (
          <span style={{ fontSize: 11, color: "var(--muted)", fontWeight: 400 }}>
            {"  "}· 전일比 ▲+5% 빨강 / ▼−5% 파랑 · MA 5종
          </span>
        )}
      </h3>
      <ResponsiveContainer width="100%" height={210}>
        {ikey === "volume" ? (
          <ComposedChart data={data} margin={M}>
            <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={40} />
            <YAxis tick={{ fontSize: 10 }} width={44}
              tickFormatter={(v) => v >= 1e6 ? `${(v / 1e6).toFixed(0)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : String(v)} />
            <Tooltip formatter={(v) => Number(v).toLocaleString()} />
            <Bar dataKey="volume" name="거래량" isAnimationActive={false}>
              {data.map((d, i) => {
                const vc = d.vol_chg as number | null;
                const fill = vc != null && vc >= 5 ? UP : vc != null && vc <= -5 ? DOWN : "#d7cfc4";
                return <Cell key={i} fill={fill} />;
              })}
            </Bar>
            {VOL_MA.map(([k, lbl, col]) => (
              <Line key={k} type="monotone" dataKey={k} stroke={col} strokeWidth={1} dot={false} name={lbl} />
            ))}
          </ComposedChart>
        ) : ikey === "macd" ? (
          <ComposedChart data={data} margin={M}>
            <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={40} />
            <YAxis tick={{ fontSize: 10 }} width={44} />
            <Tooltip /><ReferenceLine y={0} stroke="#9aa0a6" />
            <Bar dataKey="macd_hist" fill="#cbb9ac" name="히스토그램" isAnimationActive={false} />
            <Line type="monotone" dataKey="macd" stroke={IND_COLORS.macd} strokeWidth={1.5} dot={false} name="MACD" />
            <Line type="monotone" dataKey="macd_signal" stroke={IND_COLORS.macd_signal} strokeWidth={1} dot={false} name="시그널" />
          </ComposedChart>
        ) : ikey === "stoch" ? (
          <LineChart data={data} margin={M}>
            <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={40} />
            <YAxis domain={[0, 100]} ticks={[20, 50, 80]} tick={{ fontSize: 10 }} width={30} />
            <Tooltip /><ReferenceLine y={80} stroke={UP} strokeDasharray="4 4" />
            <ReferenceLine y={20} stroke={DOWN} strokeDasharray="4 4" />
            <Line type="monotone" dataKey="stoch_k" stroke={IND_COLORS.stoch_k} strokeWidth={1.5} dot={false} name="%K" />
            <Line type="monotone" dataKey="stoch_d" stroke={IND_COLORS.stoch_d} strokeWidth={1} dot={false} name="%D" />
          </LineChart>
        ) : ikey === "rsi_14" ? (
          <LineChart data={data} margin={M}>
            <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={40} />
            <YAxis domain={[0, 100]} ticks={[30, 50, 70]} tick={{ fontSize: 10 }} width={30} />
            <Tooltip /><ReferenceLine y={70} stroke={UP} strokeDasharray="4 4" />
            <ReferenceLine y={30} stroke={DOWN} strokeDasharray="4 4" />
            <Line type="monotone" dataKey="rsi_14" stroke={IND_COLORS.rsi_14} strokeWidth={1.5} dot={false} name="RSI" />
          </LineChart>
        ) : ikey === "bb" ? (
          <LineChart data={data} margin={M}>
            <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={40} />
            <YAxis domain={["auto", "auto"]} tick={{ fontSize: 10 }} width={48}
              tickFormatter={(v) => isKR ? `${Math.round(v / 1000)}k` : String(v)} />
            <Tooltip />
            <Line type="monotone" dataKey="bb_upper" stroke="#22a06b" strokeWidth={1} dot={false} name="상단" />
            <Line type="monotone" dataKey="close" stroke={ACCENT} strokeWidth={1.4} dot={false} name="종가" />
            <Line type="monotone" dataKey="bb_mid" stroke="#9aa0a6" strokeWidth={1} strokeDasharray="3 3" dot={false} name="중심" />
            <Line type="monotone" dataKey="bb_lower" stroke="#22a06b" strokeWidth={1} dot={false} name="하단" />
          </LineChart>
        ) : (
          <LineChart data={data} margin={M}>
            <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={40} />
            <YAxis tick={{ fontSize: 10 }} width={44} />
            <Tooltip />
            <Line type="monotone" dataKey={ikey} stroke={IND_COLORS[ikey] || ACCENT} strokeWidth={1.5} dot={false} name={title} />
          </LineChart>
        )}
      </ResponsiveContainer>
    </>
  );
}

// ── 지표 설명·해석 (버튼 토글) ────────────────────────────────────────────────
function ExplToggle(props: {
  ikey: string; series: SymbolPoint[]; isKR: boolean; dol: string; won: string; benchName: string;
}) {
  const [open, setOpen] = useState(false);
  const { def, reading } = interpret(props.ikey, props.series, props);
  return (
    <div style={{ marginTop: 8 }}>
      <button className="ghost sm" onClick={() => setOpen((o) => !o)}>
        {open ? "설명·해석 닫기 ▴" : "설명·해석 보기 ▾"}
      </button>
      {open && (
        <div className="ca-expl">
          <div><b>설명</b> — {def}</div>
          <div style={{ marginTop: 6 }}><b>최근 1개월 해석</b> — {reading}</div>
        </div>
      )}
    </div>
  );
}

// 규칙기반 해석 — 최근 약 1개월(21 거래일) 수치로 문구 생성.
function interpret(
  ikey: string, series: SymbolPoint[],
  ctx: { isKR: boolean; dol: string; won: string; benchName: string },
): { def: string; reading: string } {
  const recent = series.slice(-21);
  const vals = (k: keyof SymbolPoint) =>
    recent.map((p) => p[k]).filter((v) => v != null) as number[];
  const mean = (a: number[]) => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
  const lastN = (k: keyof SymbolPoint) => { const a = vals(k); return a.length ? a[a.length - 1] : null; };
  const trend = (k: keyof SymbolPoint) => {
    const a = vals(k); if (a.length < 2) return "데이터 부족";
    const d = a[a.length - 1] - a[0];
    return d > 0 ? "상승 추세" : d < 0 ? "하락 추세" : "보합";
  };
  const f1 = (v: number | null) => v == null ? "—" : v.toFixed(1);
  const money = (v: number | null) => v == null ? "—"
    : ctx.isKR ? `${ctx.dol}${Math.round(v).toLocaleString()}${ctx.won}`
      : `${ctx.dol}${v.toFixed(2)}`;

  switch (ikey) {
    case "price": {
      const close = lastN("close"); const m20 = lastN("ma20"); const m60 = lastN("ma60"); const m240 = lastN("ma240");
      const ma5 = vals("ma5"); const ma20a = vals("ma20");
      let cross = "";
      if (ma5.length >= 2 && ma20a.length >= 2) {
        const p5 = ma5[ma5.length - 2], c5 = ma5[ma5.length - 1];
        const p20 = ma20a[ma20a.length - 2], c20 = ma20a[ma20a.length - 1];
        if (p5 <= p20 && c5 > c20) cross = " 최근 단기선(MA5)이 MA20을 상향 돌파(골든크로스).";
        else if (p5 >= p20 && c5 < c20) cross = " 최근 단기선(MA5)이 MA20을 하향 돌파(데드크로스).";
      }
      const pos = close != null && m20 != null
        ? close >= m20 ? "MA20 위(단기 상승 우위)" : "MA20 아래(단기 약세)" : "—";
      return {
        def: "캔들은 일별 시·고·저·종가(상승=빨강/하락=파랑), 이동평균(MA)은 추세, 초록 점선은 "
          + `벤치마크(${ctx.benchName}) 대비 상대 흐름입니다.`,
        reading: `현재가 ${money(close)}, ${pos}. MA60 ${money(m60)} · MA240 ${money(m240)} 기준 `
          + `장기 추세는 ${close != null && m240 != null ? (close >= m240 ? "상승 국면" : "하락 국면") : "—"}.${cross}`,
      };
    }
    case "rsi_14": {
      const m = mean(vals("rsi_14")); const l = lastN("rsi_14");
      const zone = l == null ? "—" : l >= 70 ? "과매수 — 단기 과열·조정 주의" : l <= 30 ? "과매도 — 반등 가능성" : "중립";
      return {
        def: "RSI(14)는 0~100으로 상승/하락 압력 균형을 나타냅니다. 70↑ 과매수, 30↓ 과매도.",
        reading: `최근 1개월 평균 ${f1(m)}, 현재 ${f1(l)} (${zone}). ${trend("rsi_14")}.`,
      };
    }
    case "macd": {
      const macd = lastN("macd"); const sig = lastN("macd_signal"); const hist = lastN("macd_hist");
      const rel = macd != null && sig != null
        ? macd > sig ? "MACD가 시그널 위 — 상승 모멘텀" : "MACD가 시그널 아래 — 하락 모멘텀" : "—";
      return {
        def: "MACD는 단기(12)−장기(26) 지수이평 차이. 시그널선(9) 상향 돌파=매수 신호, 히스토그램=둘의 격차.",
        reading: `현재 MACD ${macd == null ? "—" : macd.toFixed(3)}, 시그널 ${sig == null ? "—" : sig.toFixed(3)} (${rel}). `
          + `히스토그램 ${hist == null ? "—" : hist.toFixed(3)}(${hist != null && hist >= 0 ? "양(+)" : "음(−)"}).`,
      };
    }
    case "stoch": {
      const k = lastN("stoch_k"); const d = lastN("stoch_d");
      const zone = k == null ? "—" : k >= 80 ? "과매수권" : k <= 20 ? "과매도권" : "중립권";
      return {
        def: "스토캐스틱(14,3)은 최근 범위 내 종가 위치(%K)와 그 이동평균(%D). 80↑ 과매수, 20↓ 과매도.",
        reading: `현재 %K ${f1(k)} / %D ${f1(d)} (${zone}). ${trend("stoch_k")}.`,
      };
    }
    case "volume": {
      const recAvg = mean(vals("volume"));
      const prior = series.slice(-42, -21).map((p) => p.volume).filter((v) => v != null) as number[];
      const priAvg = mean(prior);
      const cmp = recAvg != null && priAvg != null
        ? recAvg > priAvg * 1.1 ? "직전 1개월 대비 증가(관심↑)" : recAvg < priAvg * 0.9 ? "직전 1개월 대비 감소(관심↓)" : "비슷"
        : "—";
      return {
        def: "거래량은 매매 활발도. 가격 변동과 함께 보면 추세 신뢰도를 가늠합니다. MA 5종으로 추세 확인.",
        reading: `최근 1개월 평균 거래량 ${recAvg == null ? "—" : Math.round(recAvg).toLocaleString()}주, ${cmp}.`,
      };
    }
    case "atr_14": {
      const a = lastN("atr_14"); const close = lastN("close");
      const pct = a != null && close ? (a / close * 100) : null;
      return {
        def: "ATR(14)은 일중 변동폭의 평균(절대값). 변동성·손절폭 설정의 참고치입니다.",
        reading: `현재 ATR ${money(a)} (종가의 ${pct == null ? "—" : pct.toFixed(1)}%). ${trend("atr_14")} — `
          + `${pct != null && pct >= 3 ? "변동성 큰 편" : "변동성 보통~낮음"}.`,
      };
    }
    case "obv": {
      return {
        def: "OBV는 상승일 거래량을 더하고 하락일은 빼 누적한 지표. 가격과 같이 오르면 매집(추세 신뢰).",
        reading: `최근 1개월 OBV는 ${trend("obv")} — ${trend("obv") === "상승 추세" ? "매집 우위" : trend("obv") === "하락 추세" ? "분산 우위" : "중립"}.`,
      };
    }
    case "vol_20d": {
      const l = lastN("vol_20d"); const m = mean(vals("vol_20d"));
      return {
        def: "변동성(20일)은 최근 20일 수익률의 표준편차(연율화). 위험 수준을 나타냅니다.",
        reading: `현재 ${f1(l)}%, 최근 1개월 평균 ${f1(m)}%. ${trend("vol_20d")}.`,
      };
    }
    case "bb": {
      const close = lastN("close"); const up = lastN("bb_upper"); const low = lastN("bb_lower"); const mid = lastN("bb_mid");
      const pctB = close != null && up != null && low != null && up !== low
        ? ((close - low) / (up - low) * 100) : null;
      const zone = pctB == null ? "—" : pctB >= 100 ? "상단 돌파(과열)" : pctB >= 80 ? "상단 근접"
        : pctB <= 0 ? "하단 이탈(과냉)" : pctB <= 20 ? "하단 근접" : "밴드 중앙권";
      return {
        def: "볼린저밴드는 20일 이동평균 ±2표준편차. 밴드 폭은 변동성, 상단 접근=과열, 하단=과냉 신호.",
        reading: `현재가 ${money(close)}, 밴드 내 위치 %B ${pctB == null ? "—" : pctB.toFixed(0)}% (${zone}). 중심선 ${money(mid)}.`,
      };
    }
    default:
      return { def: "지표 설명을 준비 중입니다.", reading: "—" };
  }
}

// ── 가격 캔들 차트 커스텀 툴팁 (OHLC + MA + 벤치마크) ──────────────────────────
type PriceTipEntry = { payload: SymbolPoint & { date: string } };
function PriceTooltip(props: {
  active?: boolean; payload?: readonly PriceTipEntry[];
  isKR: boolean; dol: string; won: string; benchName: string;
}) {
  if (!props.active || !props.payload || !props.payload.length) return null;
  const p = props.payload[0].payload;
  const m = (v: number | null) => v == null ? "—"
    : props.isKR ? `${props.dol}${Math.round(v).toLocaleString()}${props.won}`
      : `${props.dol}${v.toFixed(2)}`;
  const row = (label: string, v: number | null, color?: string) => (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 16, color }}>
      <span>{label}</span><span>{m(v)}</span>
    </div>
  );
  return (
    <div style={{ background: "#fff", border: "1px solid var(--border,#e8e3db)", borderRadius: 8,
      padding: "8px 11px", fontSize: 12, boxShadow: "0 4px 14px rgba(0,0,0,0.12)", minWidth: 160 }}>
      <div style={{ fontWeight: 700, marginBottom: 5 }}>{p.date}</div>
      {row("시가", p.open)}{row("고가", p.high)}{row("저가", p.low)}
      {row("종가", p.close, p.close != null && p.open != null ? (p.close >= p.open ? UP : DOWN) : undefined)}
      <div style={{ borderTop: "1px solid var(--border,#e8e3db)", margin: "5px 0" }} />
      {row("MA20", p.ma20)}{row("MA60", p.ma60)}{row("MA240", p.ma240)}
      {row(props.benchName, p.bench, BENCH)}
    </div>
  );
}

// ── 요약 박스 (선택형) ────────────────────────────────────────────────────────
function MetricCard(props: {
  sel: string; onSel: (v: string) => void; last: SymbolDetail["last"];
  fmtP: (v: number | null | undefined) => string; dol: string; won: string;
}) {
  const { sel, onSel, last, fmtP, dol, won } = props;
  const head = (
    <div className="ca-card-head">
      <span style={{ fontSize: 12, color: "var(--muted)", fontWeight: 600 }}>
        {METRIC_OPTS.find(([k]) => k === sel)?.[1]}
      </span>
      <select value={sel} onChange={(e) => onSel(e.target.value)} aria-label="요약 지표 선택">
        {METRIC_OPTS.map(([k, lbl]) => <option key={k} value={k}>{lbl}</option>)}
      </select>
    </div>
  );
  let body: ReactNode;
  switch (sel) {
    case "rsi": {
      const r = last.rsi_14;
      const label = r == null ? "" : r >= 70 ? "과매수" : r <= 30 ? "과매도" : "중립";
      const color = r == null ? "var(--muted)" : r >= 70 ? UP : r <= 30 ? DOWN : "var(--muted)";
      body = <><div style={{ fontSize: 22, fontWeight: 700 }}>{r == null ? "—" : r.toFixed(1)}</div>
        <div style={{ color, fontWeight: 600, fontSize: 13 }}>{label}</div></>;
      break;
    }
    case "range52":
      body = <><div style={{ fontSize: 14 }}>고 {dol}{fmtP(last.high_52w)}{won}</div>
        <div style={{ fontSize: 14 }}>저 {dol}{fmtP(last.low_52w)}{won}</div></>;
      break;
    case "volume":
      body = <div style={{ fontSize: 20, fontWeight: 700 }}>{last.volume == null ? "—" : last.volume.toLocaleString()}</div>;
      break;
    case "macd":
      body = <div style={{ fontSize: 20, fontWeight: 700, color: last.macd == null ? "var(--muted)" : last.macd >= 0 ? UP : DOWN }}>
        {last.macd == null ? "—" : last.macd.toFixed(3)}</div>;
      break;
    case "stoch":
      body = <div style={{ fontSize: 22, fontWeight: 700 }}>{last.stoch_k == null ? "—" : last.stoch_k.toFixed(1)}<span style={{ fontSize: 12, color: "var(--muted)" }}> %K</span></div>;
      break;
    case "atr":
      body = <div style={{ fontSize: 20, fontWeight: 700 }}>{dol}{fmtP(last.atr_14)}{won}</div>;
      break;
    case "vol20":
      body = <div style={{ fontSize: 22, fontWeight: 700 }}>{last.vol_20d == null ? "—" : `${last.vol_20d.toFixed(1)}%`}</div>;
      break;
    default:
      body = <div style={{ fontSize: 22, fontWeight: 700 }}>—</div>;
  }
  return <div className="panel" style={{ padding: 14, marginBottom: 0 }}>{head}<div style={{ marginTop: 6 }}>{body}</div></div>;
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="panel" style={{ padding: 14, marginBottom: 0 }}>
      <div style={{ fontSize: 12, color: "var(--muted)", fontWeight: 600, marginBottom: 6 }}>{title}</div>
      {children}
    </div>
  );
}

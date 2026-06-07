import { useState, useEffect, type ReactNode } from "react";
import {
  ComposedChart, LineChart, BarChart, Line, Bar, Scatter, XAxis, YAxis, Tooltip,
  ResponsiveContainer, ReferenceLine, CartesianGrid, Legend, Brush,
} from "recharts";
import { api } from "../api";
import type { SymbolDetail, SymbolListing } from "../types";

const RANGES: [string, string][] = [
  ["1m", "1개월"], ["3m", "3개월"], ["6m", "6개월"], ["12m", "12개월"], ["1y", "1년"],
  ["3y", "3년"], ["5y", "5년"], ["10y", "10년"], ["15y", "15년"],
  ["20y", "20년"], ["25y", "25년"], ["30y", "30년"],
];
const UP = "#de3033", DOWN = "#1f6feb", ACCENT = "#d97757";
const IND_COLORS: Record<string, string> = {
  ma5: "#9aa0a6", ma20: "#b3a692", ma60: "#1f6feb", ma120: "#7c3aed",
  rsi_14: "#ad5019", macd: "#d97757", macd_signal: "#1f6feb",
  stoch_k: "#ad5019", stoch_d: "#1f6feb", atr_14: "#7c3aed",
  obv: "#22a06b", vol_20d: "#e0823d", volume: "#d7cfc4",
};
const SUB_LABEL: Record<string, string> = {
  rsi_14: "RSI (14)", macd: "MACD (12,26,9)", stoch: "스토캐스틱 (14,3)",
  volume: "거래량", atr_14: "ATR (14)", obv: "OBV", vol_20d: "변동성 (20일)",
};
const DEFAULT_SEL = ["ma20", "ma60", "rsi_14", "volume", "macd"];

// 급등락 세모 마커 (위=급등 빨강, 아래=급락 파랑)
function TriUp(props: { cx?: number; cy?: number }) {
  const { cx, cy } = props;
  if (cx == null || cy == null) return null;
  return <path d={`M ${cx} ${cy - 9} L ${cx - 6} ${cy - 1} L ${cx + 6} ${cy - 1} Z`}
    fill={UP} stroke="#fff" strokeWidth={0.6} />;
}
function TriDown(props: { cx?: number; cy?: number }) {
  const { cx, cy } = props;
  if (cx == null || cy == null) return null;
  return <path d={`M ${cx} ${cy + 9} L ${cx - 6} ${cy + 1} L ${cx + 6} ${cy + 1} Z`}
    fill={DOWN} stroke="#fff" strokeWidth={0.6} />;
}

export default function StockDashboard() {
  const [symbol, setSymbol] = useState("005930");
  const [input, setInput] = useState("005930");
  const [range, setRange] = useState("1y");
  const [data, setData] = useState<SymbolDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [listings, setListings] = useState<SymbolListing[]>([]);
  const [selected, setSelected] = useState<string[]>(DEFAULT_SEL);
  const [focused, setFocused] = useState(false);

  async function load(sym: string, rng: string) {
    const s = sym.trim();
    if (!s) return;
    setBusy(true); setErr(""); setFocused(false);
    try {
      const d = await api.symbolDetail(s, rng);
      setData(d); setSymbol(s); setRange(rng); setInput(s);
    } catch (e) { setErr((e as Error).message); setData(null); }
    finally { setBusy(false); }
  }

  useEffect(() => {
    load("005930", "1y");
    api.marketListings().then((r) => setListings(r.listings)).catch(() => {});
    /* eslint-disable-line */
  }, []);

  // 검색: 포커스(클릭)만 해도 목록 표시 — 입력 있으면 필터, 없으면 상위 목록
  const q = input.trim().toUpperCase();
  const matches = (q
    ? listings.filter((l) => l.symbol.includes(q) || l.name.toUpperCase().includes(q))
    : listings
  ).slice(0, 60);

  const indicators = data?.indicators || [];
  const specOf = (k: string) => indicators.find((i) => i.key === k);
  const priceInds = selected.filter((k) => specOf(k)?.pane === "price");
  const subInds = selected.filter((k) => specOf(k)?.pane === "sub");

  function toggle(key: string) {
    setSelected((s) => s.includes(key) ? s.filter((k) => k !== key)
      : s.length < 5 ? [...s, key] : s);
  }

  const isKR = data?.currency === "KRW";
  const fmtP = (v: number | null | undefined) =>
    v == null ? "—" : isKR ? Math.round(v).toLocaleString()
      : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const won = isKR ? "원" : "", dol = isKR ? "" : "$";
  const last = data?.last;
  const chgColor = last?.change_pct != null ? (last.change_pct >= 0 ? UP : DOWN) : "var(--muted)";

  // 차트 데이터 + 급등락 마커 컬럼
  const chartData = (data?.series || []).map((p) => ({
    ...p,
    up_spike: p.chg_pct != null && p.chg_pct >= 10 ? p.close : null,
    down_spike: p.chg_pct != null && p.chg_pct <= -10 ? p.close : null,
  }));

  return (
    <div>
      <h1 style={{ marginBottom: 4 }}>종목 대시보드</h1>
      <p style={{ color: "var(--muted)", marginTop: 0 }}>
        한국·미국 개별 종목·ETF·ETN의 가격·추이·지표를 조회합니다. 급등락 ±10%는
        세모(▲빨강/▼파랑)로, 그래프 하단 막대로 기간 확대·축소가 됩니다.
      </p>

      <div className="panel" style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <div style={{ position: "relative", width: 300 }}>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onFocus={() => setFocused(true)}
            onBlur={() => setTimeout(() => setFocused(false), 150)}
            onKeyDown={(e) => { if (e.key === "Enter") load(input, range); }}
            placeholder="종목명·코드 검색 (삼성전자 / 005930 / AAPL)"
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
                  onMouseDown={() => load(l.symbol, range)}
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
        <button onClick={() => load(input, range)} disabled={busy}>
          {busy ? "조회 중…" : "조회"}
        </button>
        <span style={{ fontSize: 12, color: "var(--muted)" }}>
          {listings.length ? `${listings.length.toLocaleString()}종목` : ""}
        </span>
        <div style={{ display: "flex", gap: 4, marginLeft: "auto", flexWrap: "wrap" }}>
          {RANGES.map(([v, lbl]) => (
            <button key={v} type="button" className={range === v ? "" : "ghost"}
              onClick={() => load(symbol, v)} style={{ padding: "4px 9px" }}>{lbl}</button>
          ))}
        </div>
      </div>

      {err && <div className="error" style={{ marginTop: 12 }}>{err}</div>}

      {data && last && (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 12, marginTop: 16 }}>
            <Card title={`${data.symbol} 현재가`}>
              <div style={{ fontSize: 22, fontWeight: 700 }}>{dol}{fmtP(last.close)}{won}</div>
              <div style={{ color: chgColor, fontWeight: 600 }}>
                {last.change_pct != null && last.change_pct >= 0 ? "▲" : "▼"} {last.change_pct?.toFixed(2)}%
              </div>
            </Card>
            <Card title="RSI (14)"><RsiBadge rsi={last.rsi_14} /></Card>
            <Card title="52주 고 / 저">
              <div style={{ fontSize: 14 }}>고 {dol}{fmtP(last.high_52w)}{won}</div>
              <div style={{ fontSize: 14 }}>저 {dol}{fmtP(last.low_52w)}{won}</div>
            </Card>
            <Card title="MACD / 변동성">
              <div style={{ fontSize: 14 }}>MACD {last.macd ?? "—"}</div>
              <div style={{ fontSize: 14 }}>변동성 {last.vol_20d ?? "—"}%</div>
            </Card>
          </div>

          <div className="panel" style={{ marginTop: 16 }}>
            <details>
              <summary style={{ cursor: "pointer", fontWeight: 600 }}>
                📊 지표 선택 — {selected.length}/5 (주가 오버레이 + 서브차트)
              </summary>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill,minmax(150px,1fr))", gap: 6, marginTop: 10 }}>
                {indicators.map((i) => {
                  const on = selected.includes(i.key);
                  const dis = !on && selected.length >= 5;
                  return (
                    <label key={i.key} style={{ fontSize: 13, opacity: dis ? 0.4 : 1, cursor: dis ? "not-allowed" : "pointer" }}>
                      <input type="checkbox" checked={on} disabled={dis} onChange={() => toggle(i.key)} />
                      {" "}{i.label}{" "}
                      <span style={{ color: "var(--muted)", fontSize: 11 }}>
                        {i.pane === "price" ? "(주가)" : "(서브)"}
                      </span>
                    </label>
                  );
                })}
              </div>
            </details>
          </div>

          <div className="panel" style={{ marginTop: 16 }}>
            <h3 style={{ marginTop: 0 }}>주가 추이 · 지표 오버레이 · 급등락(±10%)</h3>
            <ResponsiveContainer width="100%" height={380}>
              <ComposedChart data={chartData} margin={{ top: 5, right: 12, bottom: 5, left: 8 }}>
                <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
                <YAxis domain={["auto", "auto"]} tick={{ fontSize: 11 }} width={54}
                  tickFormatter={(v) => isKR ? `${Math.round(v / 1000)}k` : String(v)} />
                <Tooltip formatter={(v: number) => `${dol}${fmtP(v)}${won}`} />
                <Legend />
                <Line type="monotone" dataKey="close" stroke={ACCENT} strokeWidth={2} dot={false} name="종가" />
                {priceInds.flatMap((k) => k === "bb"
                  ? [
                    <Line key="bbu" type="monotone" dataKey="bb_upper" stroke="#22a06b" strokeWidth={1} dot={false} name="BB상" />,
                    <Line key="bbm" type="monotone" dataKey="bb_mid" stroke="#9aa0a6" strokeWidth={1} strokeDasharray="3 3" dot={false} name="BB중" />,
                    <Line key="bbl" type="monotone" dataKey="bb_lower" stroke="#22a06b" strokeWidth={1} dot={false} name="BB하" />,
                  ]
                  : [<Line key={k} type="monotone" dataKey={k} stroke={IND_COLORS[k]} strokeWidth={1} dot={false} name={specOf(k)?.label || k} />])}
                <Scatter dataKey="up_spike" shape={<TriUp />} name="급등 +10%" legendType="triangle" fill={UP} />
                <Scatter dataKey="down_spike" shape={<TriDown />} name="급락 −10%" legendType="triangle" fill={DOWN} />
                <Brush dataKey="date" height={24} stroke={ACCENT} travellerWidth={8} />
              </ComposedChart>
            </ResponsiveContainer>
          </div>

          {subInds.map((k) => (
            <SubChart key={k} ikey={k} data={chartData} />
          ))}
          {subInds.length === 0 && (
            <p style={{ color: "var(--muted)", marginTop: 12, fontSize: 13 }}>
              위 “지표 선택”에서 RSI·MACD·거래량 등 서브 지표를 고르면 여기에 차트로 표시됩니다.
            </p>
          )}
        </>
      )}
    </div>
  );
}

function SubChart({ ikey, data }: { ikey: string; data: Record<string, unknown>[] }) {
  const title = SUB_LABEL[ikey] || ikey;
  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <h3 style={{ marginTop: 0 }}>{title}</h3>
      <ResponsiveContainer width="100%" height={160}>
        {ikey === "volume" ? (
          <BarChart data={data} margin={{ top: 5, right: 12, bottom: 5, left: 8 }}>
            <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
            <YAxis tick={{ fontSize: 11 }} width={48}
              tickFormatter={(v) => v >= 1e6 ? `${(v / 1e6).toFixed(0)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : String(v)} />
            <Tooltip formatter={(v: number) => v?.toLocaleString()} />
            <Bar dataKey="volume" fill={IND_COLORS.volume} name="거래량" />
          </BarChart>
        ) : ikey === "macd" ? (
          <ComposedChart data={data} margin={{ top: 5, right: 12, bottom: 5, left: 8 }}>
            <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
            <YAxis tick={{ fontSize: 11 }} width={48} />
            <Tooltip /><ReferenceLine y={0} stroke="#9aa0a6" />
            <Bar dataKey="macd_hist" fill="#cbb9ac" name="히스토그램" />
            <Line type="monotone" dataKey="macd" stroke={IND_COLORS.macd} strokeWidth={1.5} dot={false} name="MACD" />
            <Line type="monotone" dataKey="macd_signal" stroke={IND_COLORS.macd_signal} strokeWidth={1} dot={false} name="시그널" />
          </ComposedChart>
        ) : ikey === "stoch" ? (
          <LineChart data={data} margin={{ top: 5, right: 12, bottom: 5, left: 8 }}>
            <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
            <YAxis domain={[0, 100]} ticks={[20, 50, 80]} tick={{ fontSize: 11 }} width={32} />
            <Tooltip /><ReferenceLine y={80} stroke={UP} strokeDasharray="4 4" />
            <ReferenceLine y={20} stroke={DOWN} strokeDasharray="4 4" />
            <Line type="monotone" dataKey="stoch_k" stroke={IND_COLORS.stoch_k} strokeWidth={1.5} dot={false} name="%K" />
            <Line type="monotone" dataKey="stoch_d" stroke={IND_COLORS.stoch_d} strokeWidth={1} dot={false} name="%D" />
          </LineChart>
        ) : ikey === "rsi_14" ? (
          <LineChart data={data} margin={{ top: 5, right: 12, bottom: 5, left: 8 }}>
            <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
            <YAxis domain={[0, 100]} ticks={[30, 50, 70]} tick={{ fontSize: 11 }} width={32} />
            <Tooltip /><ReferenceLine y={70} stroke={UP} strokeDasharray="4 4" />
            <ReferenceLine y={30} stroke={DOWN} strokeDasharray="4 4" />
            <Line type="monotone" dataKey="rsi_14" stroke={IND_COLORS.rsi_14} strokeWidth={1.5} dot={false} name="RSI" />
          </LineChart>
        ) : (
          <LineChart data={data} margin={{ top: 5, right: 12, bottom: 5, left: 8 }}>
            <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
            <YAxis tick={{ fontSize: 11 }} width={48} />
            <Tooltip />
            <Line type="monotone" dataKey={ikey} stroke={IND_COLORS[ikey] || ACCENT} strokeWidth={1.5} dot={false} name={title} />
          </LineChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="panel" style={{ padding: 14 }}>
      <div style={{ fontSize: 12, color: "var(--muted)", fontWeight: 600, marginBottom: 6 }}>{title}</div>
      {children}
    </div>
  );
}

function RsiBadge({ rsi }: { rsi: number | null }) {
  if (rsi == null) return <div style={{ fontSize: 22, fontWeight: 700 }}>—</div>;
  const label = rsi >= 70 ? "과매수" : rsi <= 30 ? "과매도" : "중립";
  const color = rsi >= 70 ? UP : rsi <= 30 ? DOWN : "var(--muted)";
  return (
    <>
      <div style={{ fontSize: 22, fontWeight: 700 }}>{rsi.toFixed(1)}</div>
      <div style={{ color, fontWeight: 600, fontSize: 13 }}>{label}</div>
    </>
  );
}

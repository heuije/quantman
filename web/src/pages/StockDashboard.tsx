import { useState, useEffect, type ReactNode } from "react";
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, Tooltip,
  ResponsiveContainer, ReferenceLine, CartesianGrid, Legend,
} from "recharts";
import { api } from "../api";
import type { SymbolDetail, SymbolListing } from "../types";

const RANGES: [string, string][] = [
  ["1m", "1개월"], ["3m", "3개월"], ["6m", "6개월"], ["12m", "12개월"], ["1y", "1년"],
  ["3y", "3년"], ["5y", "5년"], ["10y", "10년"], ["15y", "15년"],
  ["20y", "20년"], ["25y", "25년"], ["30y", "30년"],
];
const UP = "#de3033", DOWN = "#1f6feb", ACCENT = "#d97757";

export default function StockDashboard() {
  const [symbol, setSymbol] = useState("005930");
  const [input, setInput] = useState("005930");
  const [range, setRange] = useState("1y");
  const [data, setData] = useState<SymbolDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [listings, setListings] = useState<SymbolListing[]>([]);

  async function load(sym: string, rng: string) {
    const s = sym.trim();
    if (!s) return;
    setBusy(true); setErr("");
    try {
      const d = await api.symbolDetail(s, rng);
      setData(d); setSymbol(s); setRange(rng);
    } catch (e) { setErr((e as Error).message); setData(null); }
    finally { setBusy(false); }
  }

  // 첫 진입 시 예시 종목 1회 로드 + 전종목 목록(검색/드롭다운)
  useEffect(() => {
    load("005930", "1y");
    api.marketListings().then((r) => setListings(r.listings)).catch(() => {});
    /* eslint-disable-line */
  }, []);

  // 입력 기반 자동완성 — 코드·이름 부분일치 상위 50개 (전종목 1만개 전체 렌더 회피)
  const q = input.trim().toUpperCase();
  const matches = q.length >= 1
    ? listings.filter((l) => l.symbol.includes(q) || l.name.toUpperCase().includes(q)).slice(0, 50)
    : [];

  const isKR = data?.currency === "KRW";
  const fmtP = (v: number | null | undefined) =>
    v == null ? "—" : isKR ? Math.round(v).toLocaleString()
      : v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const won = isKR ? "원" : "";
  const dol = isKR ? "" : "$";

  const last = data?.last;
  const chgColor = last && last.change_pct != null
    ? (last.change_pct >= 0 ? UP : DOWN) : "var(--muted)";

  return (
    <div>
      <h1 style={{ marginBottom: 4 }}>종목 대시보드</h1>
      <p style={{ color: "var(--muted)", marginTop: 0 }}>
        한국·미국 개별 종목·ETF·ETN의 가격·추이·지표(RSI·이동평균)를 실시간 조회합니다.
      </p>

      <div className="panel" style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
        <input
          value={input}
          list="symbol-list"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") load(input, range); }}
          placeholder="종목명·코드 검색 (삼성전자 / 005930 / AAPL)"
          aria-label="종목 검색"
          style={{ width: 280 }}
        />
        <datalist id="symbol-list">
          {matches.map((l) => (
            <option key={l.symbol} value={l.symbol}>{l.name} · {l.market}</option>
          ))}
        </datalist>
        <button onClick={() => load(input, range)} disabled={busy}>
          {busy ? "조회 중…" : "조회"}
        </button>
        <span style={{ fontSize: 12, color: "var(--muted)" }}>
          {listings.length ? `${listings.length.toLocaleString()}종목` : ""}
        </span>
        <div style={{ display: "flex", gap: 4, marginLeft: "auto" }}>
          {RANGES.map(([v, lbl]) => (
            <button key={v} type="button" className={range === v ? "" : "ghost"}
              onClick={() => load(symbol, v)} style={{ padding: "4px 10px" }}>
              {lbl}
            </button>
          ))}
        </div>
      </div>

      {err && <div className="error" style={{ marginTop: 12 }}>{err}</div>}

      {data && last && (
        <>
          <div style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit,minmax(160px,1fr))",
            gap: 12, marginTop: 16,
          }}>
            <Card title={`${data.symbol} 현재가`}>
              <div style={{ fontSize: 24, fontWeight: 700 }}>
                {dol}{fmtP(last.close)}{won}
              </div>
              <div style={{ color: chgColor, fontWeight: 600 }}>
                {last.change_pct != null && last.change_pct >= 0 ? "▲" : "▼"}{" "}
                {last.change_pct?.toFixed(2)}%
              </div>
            </Card>
            <Card title="RSI (14)"><RsiBadge rsi={last.rsi_14} /></Card>
            <Card title="52주 고 / 저">
              <div style={{ fontSize: 15 }}>고 {dol}{fmtP(last.high_52w)}{won}</div>
              <div style={{ fontSize: 15 }}>저 {dol}{fmtP(last.low_52w)}{won}</div>
            </Card>
            <Card title="이동평균">
              <div style={{ fontSize: 15 }}>MA20 {dol}{fmtP(last.ma20)}{won}</div>
              <div style={{ fontSize: 15 }}>MA60 {dol}{fmtP(last.ma60)}{won}</div>
            </Card>
          </div>

          <div className="panel" style={{ marginTop: 16 }}>
            <h3 style={{ marginTop: 0 }}>주가 추이 · 이동평균</h3>
            <ResponsiveContainer width="100%" height={320}>
              <LineChart data={data.series} margin={{ top: 5, right: 12, bottom: 5, left: 8 }}>
                <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
                <YAxis domain={["auto", "auto"]} tick={{ fontSize: 11 }} width={52}
                  tickFormatter={(v) => isKR ? `${Math.round(v / 1000)}k` : String(v)} />
                <Tooltip formatter={(v: number) => `${dol}${fmtP(v)}${won}`} />
                <Legend />
                <Line type="monotone" dataKey="close" stroke={ACCENT} strokeWidth={2} dot={false} name="종가" />
                <Line type="monotone" dataKey="ma20" stroke="#b3a692" strokeWidth={1} dot={false} name="MA20" />
                <Line type="monotone" dataKey="ma60" stroke={DOWN} strokeWidth={1} dot={false} name="MA60" />
              </LineChart>
            </ResponsiveContainer>
          </div>

          <div className="panel" style={{ marginTop: 16 }}>
            <h3 style={{ marginTop: 0 }}>RSI (14) — 과매수 70 / 과매도 30</h3>
            <ResponsiveContainer width="100%" height={160}>
              <LineChart data={data.series} margin={{ top: 5, right: 12, bottom: 5, left: 8 }}>
                <CartesianGrid stroke="#e8e3db" strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
                <YAxis domain={[0, 100]} ticks={[30, 50, 70]} tick={{ fontSize: 11 }} width={32} />
                <Tooltip formatter={(v: number) => v?.toFixed(1)} />
                <ReferenceLine y={70} stroke={UP} strokeDasharray="4 4" />
                <ReferenceLine y={30} stroke={DOWN} strokeDasharray="4 4" />
                <Line type="monotone" dataKey="rsi_14" stroke="#ad5019" strokeWidth={1.5} dot={false} name="RSI" />
              </LineChart>
            </ResponsiveContainer>
          </div>

          <div className="panel" style={{ marginTop: 16 }}>
            <h3 style={{ marginTop: 0 }}>거래량</h3>
            <ResponsiveContainer width="100%" height={140}>
              <BarChart data={data.series} margin={{ top: 5, right: 12, bottom: 5, left: 8 }}>
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
                <YAxis tick={{ fontSize: 11 }} width={48}
                  tickFormatter={(v) => v >= 1e6 ? `${(v / 1e6).toFixed(0)}M`
                    : v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : String(v)} />
                <Tooltip formatter={(v: number) => v?.toLocaleString()} />
                <Bar dataKey="volume" fill="#d7cfc4" name="거래량" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
    </div>
  );
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="panel" style={{ padding: 14 }}>
      <div style={{ fontSize: 12, color: "var(--muted)", fontWeight: 600, marginBottom: 6 }}>
        {title}
      </div>
      {children}
    </div>
  );
}

function RsiBadge({ rsi }: { rsi: number | null }) {
  if (rsi == null) return <div style={{ fontSize: 24, fontWeight: 700 }}>—</div>;
  const label = rsi >= 70 ? "과매수" : rsi <= 30 ? "과매도" : "중립";
  const color = rsi >= 70 ? UP : rsi <= 30 ? DOWN : "var(--muted)";
  return (
    <>
      <div style={{ fontSize: 24, fontWeight: 700 }}>{rsi.toFixed(1)}</div>
      <div style={{ color, fontWeight: 600, fontSize: 13 }}>{label}</div>
    </>
  );
}

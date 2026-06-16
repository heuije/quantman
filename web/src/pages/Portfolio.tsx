import { useState, useEffect } from "react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend,
} from "recharts";
import { api } from "../api";
import type {
  PortfolioAnalysis, PortfolioLeg, PositionInput, StrategyRow, IrStrategyDef,
} from "../types";

const BENCHMARKS: [string, string][] = [
  ["^KS11", "코스피"], ["^KQ11", "코스닥"], ["^GSPC", "S&P500"], ["^IXIC", "나스닥"],
];
const CUR_COLOR = "#64748b", PROP_COLOR = "#c4982b", BENCH_COLOR = "#94a3b8";

type Row = { symbol: string; weight: string };
const blank = (): Row => ({ symbol: "", weight: "" });

export default function Portfolio() {
  const [current, setCurrent] = useState<Row[]>([blank()]);
  const [proposed, setProposed] = useState<Row[]>([blank()]);
  const [benchmark, setBenchmark] = useState("^KS11");
  const [years, setYears] = useState(2);
  const [linked, setLinked] = useState<boolean | null>(null);
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);
  const [result, setResult] = useState<PortfolioAnalysis | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  useEffect(() => {
    api.portfolioHoldings().then((h) => {
      setLinked(h.linked);
      if (h.linked && h.positions.length) {
        setCurrent(h.positions.map((p) => ({ symbol: p.symbol, weight: String(p.weight) })));
      }
    }).catch(() => setLinked(false));
    api.listStrategies().then(setStrategies).catch(() => {});
  }, []);

  function toPositions(rows: Row[]): PositionInput[] {
    return rows
      .map((r) => ({ symbol: r.symbol.trim().toUpperCase(), weight: parseFloat(r.weight) || 0 }))
      .filter((p) => p.symbol && p.weight > 0);
  }

  async function loadStrategy(id: number) {
    setErr("");
    try {
      const s = await api.getStrategy(id);
      const def = s.definition as IrStrategyDef;
      const syms = def.universe?.symbols || [];
      if (!syms.length) {
        setErr("선택한 전략은 종목이 지정돼 있지 않습니다(스크리너·조건형). 종목을 직접 입력하세요.");
        return;
      }
      const w = def.position?.sizing?.weights || null;
      const eq = Math.round((100 / syms.length) * 100) / 100;
      setProposed(syms.map((sym) => ({
        symbol: sym,
        weight: w && w[sym] != null ? String(w[sym]) : String(eq),
      })));
    } catch (e) { setErr((e as Error).message); }
  }

  async function analyze() {
    const cur = toPositions(current);
    const prop = toPositions(proposed);
    if (!cur.length && !prop.length) { setErr("종목과 비중을 입력하세요."); return; }
    setBusy(true); setErr("");
    try {
      setResult(await api.analyzePortfolio({
        current: cur.length ? { label: "현재", positions: cur } : null,
        proposed: prop.length ? { label: "예상", positions: prop } : null,
        benchmark, years,
      }));
    } catch (e) { setErr((e as Error).message); setResult(null); }
    finally { setBusy(false); }
  }

  return (
    <div>
      <h1 style={{ marginBottom: 4 }}>포트폴리오</h1>
      <p style={{ color: "var(--muted)", marginTop: 0 }}>
        현재 포트폴리오와 새로 검토 중인 포트폴리오를 위험·베타·벤치마크 대비
        성과로 비교합니다. 예상 투자안은 전략 연구소의 최근 전략에서 가져올 수 있습니다.
      </p>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(300px,1fr))", gap: 16 }}>
        <div className="panel">
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <h3 style={{ margin: 0 }}>현재 포트폴리오</h3>
            {linked === true && <span style={{ fontSize: 12, color: "var(--accent-strong,#8a6a14)" }}>● 증권사 연동됨</span>}
            {linked === false && <span style={{ fontSize: 12, color: "var(--muted)" }}>미연동 — 직접 입력</span>}
          </div>
          <PositionEditor rows={current} setRows={setCurrent} />
        </div>

        <div className="panel">
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <h3 style={{ margin: 0 }}>예상 포트폴리오</h3>
            <select defaultValue="" onChange={(e) => { if (e.target.value) loadStrategy(Number(e.target.value)); }}
              style={{ marginLeft: "auto", fontSize: 13 }} aria-label="최근 전략에서 가져오기">
              <option value="">최근 전략에서 가져오기…</option>
              {strategies.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </div>
          <PositionEditor rows={proposed} setRows={setProposed} />
        </div>
      </div>

      <div className="panel" style={{ marginTop: 16, display: "flex", gap: 14, flexWrap: "wrap", alignItems: "flex-end" }}>
        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span style={{ fontWeight: 600, fontSize: 13 }}>벤치마크</span>
          <select value={benchmark} onChange={(e) => setBenchmark(e.target.value)}>
            {BENCHMARKS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span style={{ fontWeight: 600, fontSize: 13 }}>분석 기간</span>
          <select value={years} onChange={(e) => setYears(Number(e.target.value))}>
            {[1, 2, 3, 5].map((y) => <option key={y} value={y}>{y}년</option>)}
          </select>
        </label>
        <button onClick={analyze} disabled={busy} style={{ marginLeft: "auto" }}>
          {busy ? "분석 중…" : "비교 분석"}
        </button>
      </div>

      {err && <div className="error" style={{ marginTop: 12 }}>{err}</div>}

      {result && <Results result={result} />}
    </div>
  );
}

function PositionEditor({ rows, setRows }: { rows: Row[]; setRows: (r: Row[]) => void }) {
  const total = rows.reduce((s, r) => s + (parseFloat(r.weight) || 0), 0);
  function update(i: number, patch: Partial<Row>) {
    setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  }
  return (
    <div style={{ marginTop: 10 }}>
      {rows.map((r, i) => (
        <div key={i} style={{ display: "flex", gap: 6, marginBottom: 6 }}>
          <input value={r.symbol} placeholder="005930 / AAPL"
            onChange={(e) => update(i, { symbol: e.target.value })} style={{ flex: 1 }} />
          <input value={r.weight} placeholder="비중" type="number" min={0}
            onChange={(e) => update(i, { weight: e.target.value })} style={{ width: 80 }} />
          <button type="button" className="ghost sm"
            onClick={() => setRows(rows.length > 1 ? rows.filter((_, j) => j !== i) : [blank()])}
            aria-label="삭제">✕</button>
        </div>
      ))}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 4 }}>
        <button type="button" className="ghost sm" onClick={() => setRows([...rows, blank()])}>+ 종목 추가</button>
        <span style={{ fontSize: 12, color: total === 100 ? "var(--muted)" : "#8a6a14" }}>
          합계 {total.toFixed(0)}% {total !== 100 && "(자동 정규화됨)"}
        </span>
      </div>
    </div>
  );
}

const METRICS: [keyof PortfolioLeg["metrics"], string, "pct" | "num" | "pp"][] = [
  ["beta", "베타 (β)", "num"],
  ["cum_return", "누적 수익률", "pct"],
  ["cagr", "연환산(CAGR)", "pct"],
  ["vol", "일변동성", "pct"],
  ["mdd", "최대낙폭(MDD)", "pct"],
  ["sharpe", "샤프", "num"],
  ["excess_return", "벤치마크 초과", "pp"],
];

function Results({ result }: { result: PortfolioAnalysis }) {
  const { current, proposed } = result;
  const merged = mergeEquity(result);
  const fmt = (v: number | null, kind: string) => {
    if (v == null) return "—";
    if (kind === "num") return v.toFixed(2);
    if (kind === "pp") return `${v >= 0 ? "+" : ""}${v.toFixed(1)}%p`;
    return `${v.toFixed(1)}%`;
  };
  return (
    <>
      <div className="panel" style={{ marginTop: 16, overflowX: "auto" }}>
        <h3 style={{ marginTop: 0 }}>지표 비교</h3>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
          <thead>
            <tr style={{ borderBottom: "2px solid #e3e8ef" }}>
              <th style={{ textAlign: "left", padding: "8px 6px" }}>지표</th>
              <th style={{ textAlign: "right", padding: "8px 6px", color: CUR_COLOR }}>현재</th>
              <th style={{ textAlign: "right", padding: "8px 6px", color: PROP_COLOR }}>예상</th>
            </tr>
          </thead>
          <tbody>
            {METRICS.map(([key, label, kind]) => (
              <tr key={key} style={{ borderBottom: "1px solid #f0ece6" }}>
                <td style={{ padding: "7px 6px", fontWeight: 600 }}>{label}</td>
                <td style={{ padding: "7px 6px", textAlign: "right" }}>{current ? fmt(current.metrics[key], kind) : "—"}</td>
                <td style={{ padding: "7px 6px", textAlign: "right" }}>{proposed ? fmt(proposed.metrics[key], kind) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="panel" style={{ marginTop: 16 }}>
        <h3 style={{ marginTop: 0 }}>누적 수익 곡선 (벤치마크 대비)</h3>
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={merged} margin={{ top: 5, right: 12, bottom: 5, left: 8 }}>
            <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
            <YAxis tick={{ fontSize: 11 }} width={44}
              tickFormatter={(v) => `${Math.round((v - 1) * 100)}%`} />
            <Tooltip formatter={(v) => `${((Number(v) - 1) * 100).toFixed(1)}%`} />
            <Legend />
            {current && <Line type="monotone" dataKey="현재" stroke={CUR_COLOR} strokeWidth={1.5} dot={false} />}
            {proposed && <Line type="monotone" dataKey="예상" stroke={PROP_COLOR} strokeWidth={2} dot={false} />}
            <Line type="monotone" dataKey="벤치마크" stroke={BENCH_COLOR} strokeWidth={1} strokeDasharray="4 4" dot={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

function mergeEquity(r: PortfolioAnalysis): Record<string, number | string>[] {
  const map = new Map<string, Record<string, number | string>>();
  const add = (pts: { date: string; value: number }[] | null | undefined, key: string) => {
    pts?.forEach((p) => {
      const o = map.get(p.date) || { date: p.date };
      o[key] = p.value;
      map.set(p.date, o);
    });
  };
  add(r.current?.equity, "현재");
  add(r.proposed?.equity, "예상");
  add(r.current?.benchmark_equity || r.proposed?.benchmark_equity, "벤치마크");
  return [...map.values()].sort((a, b) => (a.date < b.date ? -1 : 1));
}

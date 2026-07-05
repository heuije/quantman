import { useState, useEffect, useRef, type CSSProperties } from "react";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, Legend,
} from "recharts";
import { api } from "../api";
import type { GlobalIndices, GlobalCommodities, GlobalCommodity, GlobalEcon, GlobalBattery, BatteryMetal, BondCurve } from "../types";
import { SectorNewsPanel, CompanyPriceChart } from "./IndustryAnalysis";

const UP = "#de3033", DOWN = "#1668c4";
const LINE_COLORS = ["#e0504f", "#4f8ff5", "#2ea65a", "#c4982b", "#9900ff",
  "#00b0b0", "#ff7a00", "#e91e8c", "#5a7d2a", "#7c6cff"];
const TIP = { contentStyle: { fontSize: 12, padding: "8px 11px", borderRadius: 8 },
  labelStyle: { fontSize: 12, fontWeight: 700, marginBottom: 4 }, itemStyle: { fontSize: 12, padding: "1px 0" } };

const MACRO_KW = {
  kr: ["미국 금리", "연준", "FOMC", "기준금리", "물가", "인플레이션", "CPI", "고용지표"],
  glob: ["Fed interest rate", "FOMC", "CPI inflation", "rate decision", "jobs report"],
};

const pctFmt = (v: number | null | undefined) =>
  v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
const numFmt = (v: number | string | null) =>
  v == null ? "—" : typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : v;
const sgn = (v: number | null) => (v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toLocaleString(undefined, { maximumFractionDigits: 4 })}`);

// 기간 컨트롤 — summary 주가 차트(CompanyPriceChart)와 동일한 1개월~10년·전체·직접입력·휠줌.
// 단일 종목 차트가 아닌 오버레이/임의 시계열에 붙이려고 훅으로 분리(차트는 CompanyPriceChart 재사용).
const PERIODS: [string, number][] = [
  ["1개월", 1], ["3개월", 3], ["6개월", 6], ["1년", 12], ["3년", 36], ["5년", 60], ["10년", 120],
];
function usePeriodRange(dates: string[]) {
  const [vStart, setVStart] = useState("");
  const [vEnd, setVEnd] = useState("");
  const [period, setPeriod] = useState<number | "all" | "custom">(12);   // 기본 1년
  const box = useRef<HTMLDivElement>(null);
  const dMin = dates[0] || "", dMax = dates[dates.length - 1] || "";
  const monthsBack = (m: number) => {
    if (!dMax) return dMin;
    const d = new Date(dMax); d.setMonth(d.getMonth() - m);
    const s = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    return s < dMin ? dMin : s;
  };
  const lo = period === "custom" ? (vStart || dMin) : period === "all" ? dMin : monthsBack(period);
  const hi = period === "custom" ? (vEnd || dMax) : dMax;

  // 휠 줌 — 커서 위치를 앵커로 구간 폭을 ½/2배. 끝까지 풀리면 '전체'로 복귀.
  const wheelState = useRef({ dates: [] as string[], lo: "", hi: "" });
  wheelState.current = { dates, lo, hi };
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const { dates: ds, lo: clo, hi: chi } = wheelState.current;
      if (ds.length < 4) return;
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      let i0 = ds.findIndex((d) => d >= clo); if (i0 < 0) i0 = 0;
      let i1 = ds.length - 1; for (let j = ds.length - 1; j >= 0; j--) { if (ds[j] <= chi) { i1 = j; break; } }
      const w = i1 - i0, anchor = i0 + frac * w;
      const nw = Math.min(ds.length - 1, Math.max(8, Math.round(w * (e.deltaY < 0 ? 0.5 : 2.0))));
      let ns = Math.round(anchor - frac * nw); ns = Math.min(ds.length - 1 - nw, Math.max(0, ns));
      const ne = ns + nw;
      if (ns <= 0 && ne >= ds.length - 1) { setVStart(""); setVEnd(""); setPeriod("all"); }
      else { setVStart(ds[ns]); setVEnd(ds[ne]); setPeriod("custom"); }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [dates.length]);

  const controls = (
    <div style={{ display: "flex", justifyContent: "flex-end", gap: 6, alignItems: "center", marginBottom: 6, flexWrap: "wrap" }}>
      {PERIODS.map(([lbl, m]) => (
        <button key={lbl} type="button" className="ghost sm"
          onClick={() => { setPeriod(m); setVStart(""); setVEnd(""); }}
          style={{ fontSize: 12, padding: "3px 10px",
            background: period === m ? "rgba(79,143,245,0.16)" : undefined,
            fontWeight: period === m ? 700 : 400, color: period === m ? "#1668c4" : undefined }}>{lbl}</button>
      ))}
      <button type="button" className="ghost sm" onClick={() => { setPeriod("all"); setVStart(""); setVEnd(""); }}
        style={{ fontSize: 12, padding: "3px 10px",
          background: period === "all" ? "rgba(79,143,245,0.16)" : undefined,
          fontWeight: period === "all" ? 700 : 400, color: period === "all" ? "#1668c4" : undefined }}>전체</button>
      <span style={{ color: "var(--muted)", fontSize: 12, margin: "0 2px" }}>|</span>
      <input type="date" value={lo} min={dMin} max={dMax} onChange={(e) => { setPeriod("custom"); setVStart(e.target.value); }} style={{ fontSize: 12, padding: "2px 6px" }} aria-label="시작일" />
      <span style={{ color: "var(--muted)", fontSize: 12 }}>~</span>
      <input type="date" value={hi} min={dMin} max={dMax} onChange={(e) => { setPeriod("custom"); setVEnd(e.target.value); }} style={{ fontSize: 12, padding: "2px 6px" }} aria-label="종료일" />
    </div>
  );
  return { lo, hi, box, controls };
}

// 배터리·핵심광물(KOMIS) 월별 가격 차트 — 기간 컨트롤은 지수/원자재와 동일(usePeriodRange).
// KOMIS는 Yahoo 심볼이 아니라 CompanyPriceChart 재사용 불가 → 전용 라인 차트.
function KomisChart({ metal }: { metal: BatteryMetal }) {
  const [series, setSeries] = useState<{ date: string; close: number }[]>([]);
  const [unit, setUnit] = useState(metal.unit);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true; setBusy(true); setSeries([]);
    api.globalBatteryChart(metal.code, metal.crtr, metal.hp)
      .then((d) => { if (alive) { setSeries(d.series || []); setUnit(d.unit || metal.unit); } })
      .catch(() => { if (alive) setSeries([]); })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [metal.code, metal.crtr, metal.hp, metal.unit]);
  const dates = series.map((s) => s.date);
  const range = usePeriodRange(dates);
  const view = series.filter((s) => s.date >= range.lo && s.date <= range.hi);
  if (busy) return <p style={{ color: "var(--muted)", fontSize: 13 }}>차트 불러오는 중…</p>;
  if (!series.length) return <p style={{ color: "var(--muted)", fontSize: 13 }}>차트 데이터가 없습니다.</p>;
  return (
    <>
      {range.controls}
      <div ref={range.box} style={{ touchAction: "none" }}>
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={view} margin={{ top: 6, right: 16, bottom: 5, left: 8 }}>
            <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={40} tickFormatter={(d) => String(d).slice(0, 7)} />
            <YAxis tick={{ fontSize: 11 }} width={58} domain={["auto", "auto"]} tickFormatter={(v) => Number(v).toLocaleString()} />
            <Tooltip {...TIP} labelFormatter={(d) => String(d).slice(0, 7)} formatter={(v) => `${Number(v).toLocaleString()} ${unit}`} />
            <Line type="monotone" dataKey="close" stroke="#1f3a5f" strokeWidth={1.6} dot={false} name={metal.name} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

// 국채 금리 서브페이지 — 국가 선택 → 만기별 시계열 차트(토글·기간 컨트롤) + 최신 커브 표 + 엑셀.
function BondsPage() {
  const [cc, setCc] = useState("US");
  const [cache, setCache] = useState<Record<string, BondCurve>>({});
  const [busy, setBusy] = useState(false);
  const [xlBusy, setXlBusy] = useState(false);
  const [hidden, setHidden] = useState<Record<string, boolean>>({});
  const cur = cache[cc];
  useEffect(() => {
    if (cache[cc]) return;
    let alive = true; setBusy(true);
    api.globalBonds(cc)
      .then((d) => { if (alive) setCache((m) => ({ ...m, [cc]: d })); })
      .catch(() => { /* 무시 */ })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cc]);

  const dates = (cur?.series || []).map((s) => String(s.date));
  const range = usePeriodRange(dates);
  const view = (cur?.series || []).filter((s) => {
    const d = String(s.date); return d >= range.lo && d <= range.hi;
  });
  const mats = cur?.maturities || [];
  const COUNTRY_TABS: [string, string][] = [["US", "미국"], ["JP", "일본"], ["EU", "유로존(AAA)"], ["KR", "한국"], ["CN", "중국"]];
  const tailRows = (cur?.series || []).slice(-15).reverse();   // 최근 15영업일 시계열 표

  return (
    <div className="panel">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
        <h3 style={{ margin: 0 }}>국가별 국채 금리 <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 12 }}>(단기~장기 전 만기{cur ? ` · ${cur.freq}` : ""}{cur?.asof ? ` · 기준일 ${cur.asof}` : ""})</span></h3>
        <button type="button" className="ghost sm" disabled={xlBusy} style={{ fontSize: 12, padding: "4px 12px" }}
          onClick={() => { setXlBusy(true); api.globalBondsExcel().catch(() => { /* 무시 */ }).finally(() => setXlBusy(false)); }}>
          {xlBusy ? "엑셀 생성 중…" : "⬇ 엑셀 다운로드 (전체 국가·전체 기간)"}</button>
      </div>
      {/* 국가 탭 */}
      <div style={{ display: "flex", gap: 6, margin: "10px 0" }}>
        {COUNTRY_TABS.map(([k, label]) => (
          <button key={k} type="button" onClick={() => { setCc(k); setHidden({}); }}
            style={{ fontSize: 12, fontWeight: 700, padding: "5px 14px", borderRadius: 8, cursor: "pointer",
              border: `1px solid ${cc === k ? "#4f8ff5" : "var(--border)"}`,
              background: cc === k ? "rgba(79,143,245,0.16)" : "transparent",
              color: cc === k ? "#4f8ff5" : "var(--muted)" }}>{label}</button>
        ))}
      </div>
      {busy && !cur ? (
        <p style={{ color: "var(--muted)", fontSize: 13 }}>국채 금리 불러오는 중…</p>
      ) : !cur || cur.maturities.length === 0 ? (
        <p style={{ color: "var(--muted)", fontSize: 13 }}>데이터가 없습니다.</p>
      ) : (
        <>
          {/* 만기 토글 칩 — 최신 수익률·전기 대비(bp) */}
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 10 }}>
            {mats.map((m, i) => {
              const lt = cur.latest[m];
              const chg = lt?.chg_bp;
              return (
                <button key={m} type="button" onClick={() => setHidden((h) => ({ ...h, [m]: !h[m] }))}
                  title="클릭하여 그래프에서 켜고 끄기"
                  style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, padding: "4px 10px",
                    border: "1px solid var(--border)", borderRadius: 8, cursor: "pointer",
                    background: "transparent", opacity: hidden[m] ? 0.4 : 1 }}>
                  <span style={{ width: 9, height: 9, borderRadius: 2, background: LINE_COLORS[i % LINE_COLORS.length] }} />
                  <b>{m}</b>
                  <span>{lt?.yield != null ? `${lt.yield.toFixed(3)}%` : "—"}</span>
                  {chg != null && (
                    <span style={{ color: chg >= 0 ? UP : DOWN, fontWeight: 700 }}>
                      {chg >= 0 ? "+" : ""}{chg.toFixed(1)}bp</span>
                  )}
                </button>
              );
            })}
          </div>
          {range.controls}
          <div ref={range.box} style={{ touchAction: "none" }}>
            <ResponsiveContainer width="100%" height={400}>
              <LineChart data={view} margin={{ top: 5, right: 16, bottom: 5, left: 4 }}>
                <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
                <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
                <YAxis tick={{ fontSize: 11 }} width={46} domain={["auto", "auto"]} tickFormatter={(v) => `${v}%`} />
                <Tooltip {...TIP} formatter={(v) => `${Number(v).toFixed(3)}%`} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                {mats.map((m, i) => (
                  <Line key={m} type="monotone" dataKey={m} stroke={LINE_COLORS[i % LINE_COLORS.length]}
                    strokeWidth={1.4} dot={false} connectNulls hide={!!hidden[m]} />
                ))}
              </LineChart>
            </ResponsiveContainer>
          </div>
          {/* 최근 시계열 표 (만기 × 최근 15일) */}
          <div style={{ overflowX: "auto", marginTop: 12 }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12, whiteSpace: "nowrap" }}>
              <thead>
                <tr style={{ borderBottom: "2px solid var(--border)" }}>
                  <th style={{ textAlign: "left", padding: "6px 8px" }}>일자</th>
                  {mats.map((m) => <th key={m} style={{ textAlign: "right", padding: "6px 8px" }}>{m}</th>)}
                </tr>
              </thead>
              <tbody>
                {tailRows.map((r, i) => (
                  <tr key={i} style={{ borderBottom: "1px solid var(--border)" }}>
                    <td style={{ padding: "5px 8px", color: "var(--muted)" }}>{String(r.date)}</td>
                    {mats.map((m) => (
                      <td key={m} style={{ padding: "5px 8px", textAlign: "right" }}>
                        {r[m] != null ? Number(r[m]).toFixed(3) : "—"}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p style={{ fontSize: 11, color: "var(--muted)", margin: "8px 2px 0" }}>
            Source: 미국·한국·중국 = FRED(세인트루이스 연준) · 일본 = 재무성(MOF) · 유로존 = ECB 산출
            AAA등급 유로지역 국채 스팟커브(독일 등 최고등급국 국채 기반 — 단일 국가 아님·정책금리 아님).
            한국·중국은 월별 시리즈(FRED 제공 범위){cc === "CN" ? " · 중국 장기물은 무료 신뢰 소스 부재로 3M만 제공(시리즈 2023-11 중단)" : ""}.
          </p>
        </>
      )}
    </div>
  );
}

export default function GlobalMarket() {
  const [idx, setIdx] = useState<GlobalIndices | null>(null);
  const [com, setCom] = useState<GlobalCommodities | null>(null);
  const [econ, setEcon] = useState<GlobalEcon | null>(null);
  const [bat, setBat] = useState<GlobalBattery | null>(null);
  const [hidden, setHidden] = useState<Record<string, boolean>>({});
  const [selCom, setSelCom] = useState<string | null>(null);   // 차트로 펼친 원자재 심볼
  const [selBat, setSelBat] = useState<BatteryMetal | null>(null);   // 차트로 펼친 배터리광물
  const [econPage, setEconPage] = useState(0);   // 경제지표 과거 페이지네이션
  const [page, setPage] = useState<"summary" | "bonds">("summary");   // 서브페이지
  const [showSrc, setShowSrc] = useState(false);   // 데이터 출처 — 좌측 통합·접기(기본 숨김)

  useEffect(() => {
    api.globalIndices().then(setIdx).catch(() => { /* 무시 */ });
    api.globalCommodities().then(setCom).catch(() => { /* 무시 */ });
    api.globalEcon().then(setEcon).catch(() => { /* 무시 */ });
    api.globalBattery().then(setBat).catch(() => { /* 무시 */ });
  }, []);

  // ── 지수 오버레이 — 기간 컨트롤 + 가시 구간 첫 점 기준 % 리베이스 ──
  const idxNames = (idx?.items || []).map((it) => it.name);
  const idxDates = (idx?.series || []).map((s) => String(s.date));
  const idxRange = usePeriodRange(idxDates);
  const idxWin = (idx?.series || []).filter((s) => {
    const d = String(s.date); return d >= idxRange.lo && d <= idxRange.hi;
  });
  const idxBase: Record<string, number> = {};
  for (const nm of idxNames) {
    const f = idxWin.find((p) => p[nm] != null);
    if (f) idxBase[nm] = Number(f[nm]);
  }
  const idxView = idxWin.map((p) => {
    const o: Record<string, number | string | null> = { date: p.date };
    for (const nm of idxNames) {
      const v = p[nm], b = idxBase[nm];
      o[nm] = v != null && b ? +(((Number(v) / b) - 1) * 100).toFixed(2) : null;
    }
    return o;
  });

  const selName = (com?.items || []).find((c) => c.symbol === selCom)?.name || "";

  // 단위는 품목 우측이 아니라 현재가에 합쳐 표기 — "$/bbl" → 접미사 "bbl" → "$69.23/bbl".
  const unitSuffix = (u: string) => { const p = (u || "").split("/"); return p.length > 1 ? p.slice(1).join("/") : (u || ""); };
  const priceFmt = (last: number | null, suffix: string) => last == null ? "—" : `$${numFmt(last)}${suffix ? "/" + suffix : ""}`;
  // KOMIS date='20260625'(YYYYMMDD) / FDR='2026-06-26'(ISO) — ISO로 통일(기준일 정렬·표시).
  const isoDate = (d: string | null | undefined) => !d ? null : /^\d{8}$/.test(d) ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` : d;

  type CRow = {
    key: string; name: string; last: number | null; change: number | null; chg_pct: number | null;
    suffix: string; date: string | null; selected: boolean; basis?: string; src: "fdr" | "komis"; onToggle: () => void;
  };
  const fdrRow = (c: GlobalCommodity): CRow => ({
    key: c.symbol, name: c.name, last: c.last, change: c.change, chg_pct: c.chg_pct,
    suffix: unitSuffix(c.unit), date: isoDate(c.date), src: "fdr", selected: selCom === c.symbol,
    onToggle: () => { setSelBat(null); setSelCom(selCom === c.symbol ? null : c.symbol); },
  });
  const komRow = (m: BatteryMetal): CRow => ({
    key: m.code + m.crtr, name: m.name, last: m.last, change: m.change, chg_pct: m.chg_pct,
    suffix: unitSuffix(m.unit), date: isoDate(m.date), basis: m.basis, src: "komis",
    selected: selBat?.code === m.code && selBat?.crtr === m.crtr,
    onToggle: () => { setSelCom(null); setSelBat((selBat?.code === m.code && selBat?.crtr === m.crtr) ? null : m); },
  });
  // 에너지 = FDR 에너지 / 금속 = FDR 금속 + KOMIS 배터리·핵심광물(전부 합침)
  const energyRows: CRow[] = (com?.items || []).filter((c) => c.category === "에너지").map(fdrRow);
  const metalRows: CRow[] = [
    ...(com?.items || []).filter((c) => c.category === "금속").map(fdrRow),
    ...(bat?.items || []).map(komRow),
  ];
  // 표 아래 Source 라인 — FDR는 한 줄(겹치면 중복 금지), KOMIS는 광물별 한 줄(기준 상이).
  const sourceLines = (rows: CRow[]) => {
    const out: string[] = [];
    const fdr = rows.filter((r) => r.src === "fdr");
    if (fdr.length) out.push(`FinanceDataReader (선물) — ${fdr.map((r) => r.name).join("·")}`);
    for (const r of rows.filter((r) => r.src === "komis")) out.push(`KOMIS 한국광해광업공단 — ${r.name}${r.basis ? ` (${r.basis})` : ""}`);
    return [...new Set(out)];
  };
  const thC: CSSProperties = { padding: "7px 8px", position: "sticky", top: 0, zIndex: 1,
    background: "var(--panel)", borderBottom: "2px solid var(--border)" };

  // 원자재 표 — 기준일자(우상단) + 구리까지 보이고 나머지 스크롤(얇은 스크롤바) + 단위는 현재가에 합침.
  const comTable = (title: string, rows: CRow[]) => {
    const asOf = rows.map((r) => r.date).filter(Boolean).sort().slice(-1)[0];
    return (
      <div>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6 }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: "var(--accent-strong)" }}>{title}</div>
          {asOf && <div style={{ fontSize: 11, color: "var(--muted)" }}>기준일 {asOf}</div>}
        </div>
        <div className="scroll-thin" style={{ maxHeight: 205, overflowY: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, tableLayout: "fixed" }}>
            <colgroup><col style={{ width: "42%" }} /><col style={{ width: "28%" }} /><col style={{ width: "15%" }} /><col style={{ width: "15%" }} /></colgroup>
            <thead>
              <tr>
                <th style={{ ...thC, textAlign: "left" }}>품목</th>
                <th style={{ ...thC, textAlign: "right" }}>현재가</th>
                <th style={{ ...thC, textAlign: "right" }}>변동</th>
                <th style={{ ...thC, textAlign: "right" }}>변동%</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const col = r.chg_pct == null ? "var(--muted)" : r.chg_pct >= 0 ? UP : DOWN;
                return (
                  <tr key={r.key} onClick={r.onToggle} title="클릭하여 차트 보기"
                    style={{ borderBottom: "1px solid var(--border)", cursor: "pointer",
                      background: r.selected ? "rgba(79,143,245,0.10)" : undefined }}>
                    <td style={{ padding: "7px 8px", fontWeight: 700, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      <span style={{ marginRight: 6, color: "var(--muted)" }}>{r.selected ? "▾" : "▸"}</span>{r.name}</td>
                    <td style={{ padding: "7px 8px", textAlign: "right", fontWeight: 700, whiteSpace: "nowrap" }}>{priceFmt(r.last, r.suffix)}</td>
                    <td style={{ padding: "7px 8px", textAlign: "right", fontWeight: 600, color: col }}>{sgn(r.change)}</td>
                    <td style={{ padding: "7px 8px", textAlign: "right", fontWeight: 700, color: col }}>{pctFmt(r.chg_pct)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    );
  };

  // 경제지표 — 과거(발표완료) 페이지네이션 + 연내 예정
  const econEvents = econ?.events || [];
  const econUp = econ?.upcoming || [];
  const econSpan = econ?.span || [];
  const EPER = 20;
  const ePages = Math.max(1, Math.ceil(econEvents.length / EPER));
  const eCur = Math.min(econPage, ePages - 1);
  const eView = econEvents.slice(eCur * EPER, eCur * EPER + EPER);

  return (
    /* mod-std — HOME과 동일 스케일 체계(zoom 0.67 + 22/16/12pt). 공용 패널(뉴스 등)이 절대단위(pt)
       하드코딩이라, 래퍼가 다르면 페이지마다 시각 크기가 어긋난다(글자크기 불일치 재발의 근본 원인). */
    <div className="dashboard-fullwidth mod-std">
      <h1>글로벌 시장 분석</h1>
      <p style={{ color: "var(--muted)", marginTop: 0, fontSize: 13 }}>
        세계 주요 지수·원자재 선물·미국 경제지표·국채 금리를 한 화면에. Source: FinanceDataReader · TradingView · FRED · MOF · ECB.
      </p>
      {/* 서브페이지 탭 — Summary(기존 화면) / 국채 금리 */}
      <div style={{ display: "flex", gap: 6, marginBottom: 14 }}>
        {([["summary", "Summary"], ["bonds", "국채 금리"]] as const).map(([k, label]) => (
          <button key={k} type="button" onClick={() => setPage(k)}
            style={{ fontSize: 13, fontWeight: 700, padding: "6px 18px", borderRadius: 8, cursor: "pointer",
              border: `1px solid ${page === k ? "#4f8ff5" : "var(--border)"}`,
              background: page === k ? "rgba(79,143,245,0.16)" : "transparent",
              color: page === k ? "#4f8ff5" : "var(--muted)" }}>{label}</button>
        ))}
      </div>

      {page === "bonds" && <BondsPage />}
      {page === "summary" && (<>
      {/* 1. 세계 10대 지수 */}
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>세계 10대 지수 <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 12 }}>(선택 구간 시작점 대비 %)</span></h3>
        {/* 현재값·등락 요약 — 클릭하여 그래프에서 켜고 끄기 */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginBottom: 10 }}>
          {(idx?.items || []).map((it, i) => (
            <button key={`${it.name}-${i}`} type="button"
              onClick={() => setHidden((h) => ({ ...h, [it.name]: !h[it.name] }))}
              title="클릭하여 그래프에서 켜고 끄기"
              style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12, padding: "5px 10px",
                border: "1px solid var(--border)", borderRadius: 8, cursor: "pointer",
                background: "transparent", opacity: hidden[it.name] ? 0.45 : 1 }}>
              <span style={{ width: 9, height: 9, borderRadius: 2, background: LINE_COLORS[i % LINE_COLORS.length] }} />
              <b>{it.name}</b>
              <span>{numFmt(it.last)}</span>
              <span style={{ color: it.chg_pct == null ? "var(--muted)" : it.chg_pct >= 0 ? UP : DOWN, fontWeight: 700 }}>
                {pctFmt(it.chg_pct)}</span>
            </button>
          ))}
        </div>
        {idxRange.controls}
        <div ref={idxRange.box} style={{ touchAction: "none" }}>
          <ResponsiveContainer width="100%" height={420}>
            <LineChart data={idxView} margin={{ top: 5, right: 16, bottom: 5, left: 4 }}>
              <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
              <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
              <YAxis tick={{ fontSize: 11 }} width={48} tickFormatter={(v) => `${v}%`} />
              <Tooltip {...TIP} formatter={(v) => `${Number(v).toFixed(2)}%`} />
              <Legend wrapperStyle={{ fontSize: 11 }} />
              {(idx?.items || []).map((it, i) => (
                <Line key={`${it.name}-${i}`} type="monotone" dataKey={it.name} stroke={LINE_COLORS[i % LINE_COLORS.length]}
                  strokeWidth={1.4} dot={false} connectNulls hide={!!hidden[it.name]} />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* 2. 원자재 선물 — 에너지 / 금속·배터리광물 두 박스(열 폭 일치, 기준일자·스크롤) → 클릭 차트 */}
      <div className="panel" style={{ marginTop: 16 }}>
        <h3 style={{ marginTop: 0 }}>원자재 선물 <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 12 }}>(에너지 · 금속·배터리광물 · 행 클릭 시 차트)</span></h3>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))", gap: 16, alignItems: "start" }}>
          {comTable("에너지", energyRows)}
          {comTable("금속 · 배터리·핵심광물", metalRows)}
        </div>
        {selCom && (
          <div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px dashed var(--border)" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
              <b style={{ fontSize: 14 }}>{selName} <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 12 }}>선물 차트</span></b>
              <button type="button" className="ghost sm" onClick={() => setSelCom(null)} style={{ fontSize: 12 }}>닫기 ✕</button>
            </div>
            <CompanyPriceChart ticker={selCom} />
          </div>
        )}
        {selBat && (
          <div style={{ marginTop: 16, paddingTop: 12, borderTop: "1px dashed var(--border)" }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
              <b style={{ fontSize: 14 }}>{selBat.name} <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 12 }}>월별 가격 추이 · {selBat.basis}</span></b>
              <button type="button" className="ghost sm" onClick={() => setSelBat(null)} style={{ fontSize: 12 }}>닫기 ✕</button>
            </div>
            <KomisChart metal={selBat} />
          </div>
        )}
        {/* 출처 — 좌측 한곳으로 통합 + 접기(기본 숨김) */}
        <div style={{ marginTop: 14 }}>
          <button type="button" onClick={() => setShowSrc((v) => !v)}
            style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 11, color: "var(--muted)",
              background: "transparent", border: "none", cursor: "pointer", padding: 0 }}>
            {showSrc ? "▾" : "▸"} 데이터 출처
          </button>
          {showSrc && (
            <div style={{ marginTop: 6, textAlign: "left" }}>
              {[...new Set([...sourceLines(energyRows), ...sourceLines(metalRows)])].map((s, i) => (
                <div key={i} style={{ fontSize: 11, color: "var(--muted)", lineHeight: 1.55 }}>Source: {s}</div>
              ))}
              {metalRows.some((r) => r.src === "komis") && (
                <div style={{ fontSize: 11, color: "var(--muted)", lineHeight: 1.55 }}>KOMIS는 월/일별 가격이라 변동은 전일(없으면 전월) 대비.</div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* 3. 미국 경제지표 — 연내 예정(컨센서스) + 과거(실제 vs 컨센서스) 최장기간 */}
      <div className="panel" style={{ marginTop: 16 }}>
        <h3 style={{ marginTop: 0 }}>미국 경제지표 <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 12 }}>(실제 vs 컨센서스{econSpan.length === 2 ? ` · ${econSpan[0]}~${econSpan[1]}` : ""} · 연내 예정 포함)</span></h3>

        {econUp.length > 0 && (
          <div style={{ marginBottom: 16 }}>
            <div style={{ fontSize: 13, fontWeight: 700, color: "var(--accent-strong)", marginBottom: 6 }}>연내 예정 발표 <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 11 }}>(컨센서스·직전)</span></div>
            <div style={{ overflowX: "auto" }}>
              <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, tableLayout: "fixed" }}>
                <colgroup><col style={{ width: "16%" }} /><col style={{ width: "46%" }} /><col style={{ width: "19%" }} /><col style={{ width: "19%" }} /></colgroup>
                <thead>
                  <tr style={{ borderBottom: "2px solid var(--border)" }}>
                    <th style={{ textAlign: "left", padding: "7px 6px" }}>발표예정일</th>
                    <th style={{ textAlign: "left", padding: "7px 6px" }}>지표</th>
                    <th style={{ textAlign: "right", padding: "7px 6px" }}>컨센서스</th>
                    <th style={{ textAlign: "right", padding: "7px 6px" }}>직전</th>
                  </tr>
                </thead>
                <tbody>
                  {econUp.map((u, i) => (
                    <tr key={i} style={{ borderBottom: "1px solid var(--border)" }}>
                      <td style={{ padding: "6px", color: "var(--accent-strong)", fontWeight: 600 }}>{u.date}</td>
                      <td style={{ padding: "6px", fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                        title={`${u.title}${u.unit ? ` (${u.unit})` : ""}`}>{u.title}</td>
                      <td style={{ padding: "6px", textAlign: "right", fontWeight: 700 }}>{numFmt(u.forecast)}{u.unit && <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 11 }}> {u.unit}</span>}</td>
                      <td style={{ padding: "6px", textAlign: "right", color: "var(--muted)" }}>{numFmt(u.previous)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <div style={{ fontSize: 13, fontWeight: 700, color: "var(--accent-strong)", marginBottom: 6 }}>발표 완료 <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 11 }}>(서프라이즈 = 실제 − 컨센서스)</span></div>
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, tableLayout: "fixed" }}>
            <colgroup><col style={{ width: "13%" }} /><col style={{ width: "27%" }} /><col style={{ width: "15%" }} /><col style={{ width: "15%" }} /><col style={{ width: "15%" }} /><col style={{ width: "15%" }} /></colgroup>
            <thead>
              <tr style={{ borderBottom: "2px solid var(--border)" }}>
                <th style={{ textAlign: "left", padding: "7px 6px" }}>발표일</th>
                <th style={{ textAlign: "left", padding: "7px 6px" }}>지표</th>
                <th style={{ textAlign: "right", padding: "7px 6px" }}>실제</th>
                <th style={{ textAlign: "right", padding: "7px 6px" }}>컨센서스</th>
                <th style={{ textAlign: "right", padding: "7px 6px" }}>직전</th>
                <th style={{ textAlign: "right", padding: "7px 6px" }}>서프라이즈</th>
              </tr>
            </thead>
            <tbody>
              {eView.map((e, i) => (
                <tr key={eCur * EPER + i} style={{ borderBottom: "1px solid var(--border)" }}>
                  <td style={{ padding: "6px", color: "var(--muted)" }}>{e.date}</td>
                  <td style={{ padding: "6px", fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                    title={`${e.title}${e.unit ? ` (${e.unit})` : ""}`}>{e.title}</td>
                  <td style={{ padding: "6px", textAlign: "right", fontWeight: 700 }}>{numFmt(e.actual)}</td>
                  <td style={{ padding: "6px", textAlign: "right", color: "var(--muted)" }}>{numFmt(e.forecast)}</td>
                  <td style={{ padding: "6px", textAlign: "right", color: "var(--muted)" }}>{numFmt(e.previous)}</td>
                  <td style={{ padding: "6px", textAlign: "right", fontWeight: 700,
                    color: e.surprise == null ? "var(--muted)" : e.surprise > 0 ? "#16a34a" : e.surprise < 0 ? "#dc2626" : "inherit" }}>
                    {e.surprise == null ? "—" : `${e.surprise > 0 ? "▲ +" : e.surprise < 0 ? "▼ " : ""}${e.surprise}`}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {ePages > 1 && (
          <div style={{ display: "flex", gap: 4, justifyContent: "center", flexWrap: "wrap", alignItems: "center", marginTop: 10 }}>
            <button type="button" className="ghost sm" disabled={eCur === 0} onClick={() => setEconPage(eCur - 1)} style={{ fontSize: 12, padding: "3px 8px" }}>‹ 이전</button>
            <span style={{ fontSize: 12, color: "var(--muted)" }}>{eCur + 1} / {ePages} 페이지 (총 {econEvents.length}건)</span>
            <button type="button" className="ghost sm" disabled={eCur >= ePages - 1} onClick={() => setEconPage(eCur + 1)} style={{ fontSize: 12, padding: "3px 8px" }}>다음 ›</button>
          </div>
        )}
        <p style={{ fontSize: 11, color: "var(--muted)", margin: "8px 2px 0" }}>
          Source: TradingView 경제캘린더 · ▲초록=컨센서스 상회 / ▼빨강=하회{econSpan.length === 2 ? ` · 데이터 ${econSpan[0]}~${econSpan[1]}(가용 최장)` : ""}.
        </p>
      </div>

      {/* 4. 금리·연준·물가 뉴스 */}
      <div className="panel" style={{ marginTop: 16 }}>
        <h3 style={{ marginTop: 0 }}>금리 · 연준 · 물가 뉴스</h3>
        <SectorNewsPanel kw={MACRO_KW} />
      </div>
      </>)}
    </div>
  );
}

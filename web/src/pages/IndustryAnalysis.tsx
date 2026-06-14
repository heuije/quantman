import { useState, useEffect, useRef } from "react";
import {
  ComposedChart, Bar, Line, Scatter, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, Legend,
} from "recharts";
import Plotly from "plotly.js-dist-min";
import { api } from "../api";
import type { IndustryData, IndustryCompany, SymbolDetail, SymbolPoint } from "../types";

// 한국식 시장 색 (상승=빨강 / 하락=파랑)
const UP = "#de3033", DOWN = "#1668c4", ACCENT = "#c4982b";
const RANGES: [string, string][] = [["1y", "1년"], ["3y", "3년"], ["5y", "5년"], ["10y", "10년"]];

// ── 포맷터 ──
const trillion = (v: number | null, d = 1) => v == null ? "—" : `${(v / 1e12).toFixed(d)}조`;
const pct = (v: number | null, d = 1) => v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(d)}%`;
function won(v: number | null): string {
  if (v == null) return "—";
  const a = Math.abs(v);
  if (a >= 1e12) return `${(v / 1e12).toFixed(1)}조`;
  if (a >= 1e8) return `${Math.round(v / 1e8).toLocaleString()}억`;
  return v.toLocaleString();
}

// 등락률 → 색 (Finviz 맵 팔레트): 보합·결측=회색, 상승=초록 / 하락=빨강, ±3%에서 최대 채도.
const FRAME = "#0c0f15";   // 구획(구분/단계/세부분류) = 거의 검정 → 얇은 검은 테두리처럼 보임
function colorByChg(chg: number | null): string {
  if (chg == null) return "#2b2f38";
  const t = Math.max(-1, Math.min(1, chg / 3));
  const gray = [0x33, 0x38, 0x42];                                // 중립 회색(#333842)
  if (t === 0) return `rgb(${gray.join(",")})`;
  const tgt = t > 0 ? [0x2f, 0xc8, 0x5a] : [0xe6, 0x3a, 0x3a];    // Finviz 초록 / 빨강
  const f = 0.2 + 0.8 * Math.abs(t);
  return `rgb(${gray.map((v, i) => Math.round(v + (tgt[i] - v) * f)).join(",")})`;
}

const STAGE_ORDER = ["원자재", "소재", "셀", "부품", "장비", "리사이클", "애플리케이션"];
const GU_ORDER = ["Upstream", "Midstream", "Downstream"];
const ROOT_ID = "2차전지";

type PlotlyEvt = { points?: { id?: string; customdata?: (string | number)[] }[]; event?: MouseEvent };

// 포트폴리오 대시보드(go.Treemap) 그대로 이식 — Plotly.js 트리맵.
// ROOT→구분→단계→세부분류→기업, 프레임 색(#5b6b7f/#6c7a8c/#7e8a9a)·pathbar 드릴 동일.
// 기업 박스 클릭=주가 / 그룹 클릭=확대, 같은 그룹 재클릭=원래대로. 세부분류·기업 호버=경쟁사 수익률.
function IndustryTreemap({ companies, onPick }: { companies: IndustryCompany[]; onPick: (t: string) => void }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const levelRef = useRef<string>(ROOT_ID);
  const pickRef = useRef(onPick); pickRef.current = onPick;
  const [hover, setHover] = useState<{ stage: string; detail: string; x: number; y: number } | null>(null);

  useEffect(() => {
    const el = ref.current;
    const valid = companies.filter((c) => c.cap && c.cap > 0);
    if (!el || valid.length === 0) return;

    const ids: string[] = [], labels: string[] = [], parents: string[] = [];
    const values: number[] = [], colors: string[] = [], texts: string[] = [];
    const cdata: (string | number)[][] = [];
    const tsizes: number[] = [], tcolors: string[] = [];   // 노드별 글자 크기·색(Finviz 헤더=회색 작게)
    const tril = (v: number) => v / 1e12;
    const sumT = (rows: IndustryCompany[]) => rows.reduce((s, c) => s + (c.cap || 0), 0) / 1e12;
    const detSum: Record<string, number> = {};
    valid.forEach((c) => { detSum[c.detail] = (detSum[c.detail] || 0) + (c.cap || 0); });
    // frame(구분/단계/세부분류) = 작은 회색 헤더 / leaf(기업) = 흰색, 시총 비례 크기
    const add = (id: string, label: string, parent: string, value: number, color: string,
      text: string, cd: (string | number)[], fsize = 11, fcolor = "#aab2bd") => {
      ids.push(id); labels.push(label); parents.push(parent); values.push(value);
      colors.push(color); texts.push(text); cdata.push(cd);
      tsizes.push(fsize); tcolors.push(fcolor);
    };
    add(ROOT_ID, ROOT_ID, "", 0, FRAME, "", [sumT(valid), "", "", ""]);
    for (const gu of GU_ORDER) {
      const inGu = valid.filter((c) => c.gu === gu);
      if (!inGu.length) continue;
      const gid = `${ROOT_ID}/${gu}`;
      add(gid, gu, ROOT_ID, 0, FRAME, "", [sumT(inGu), "", "", ""]);
      for (const dan of STAGE_ORDER) {
        const inDan = inGu.filter((c) => c.stage === dan);
        if (!inDan.length) continue;
        const did = `${gid}/${dan}`;
        add(did, dan, gid, 0, FRAME, "", [sumT(inDan), "", "", ""]);
        for (const det of [...new Set(inDan.map((c) => c.detail))]) {
          const inDet = inDan.filter((c) => c.detail === det);
          const dtid = `${did}/${det}`;
          add(dtid, det, did, 0, FRAME, "", [sumT(inDet), "", dan, det]);
          for (const c of inDet) {
            const cap = c.cap || 0;
            const lsize = Math.min(24, Math.max(10, Math.round((Math.log10(cap || 1) - 11) * 6 + 10)));
            add(`${dtid}/${c.ticker}`, c.name, dtid, cap, colorByChg(c.chg),
              pct(c.chg, 2),                                  // 타일엔 등락%만(Finviz식 라벨+%)
              [tril(cap), c.ticker, c.stage, c.detail], lsize, "#ffffff");
          }
        }
      }
    }
    const trace = {
      type: "treemap", ids, labels, parents, values, text: texts, customdata: cdata,
      level: levelRef.current, branchvalues: "remainder", texttemplate: "%{label}<br>%{text}",
      marker: { colors, line: { width: 0.5, color: "#000000" }, pad: { t: 13, l: 2, r: 2, b: 2 } },
      textfont: { size: tsizes, color: tcolors }, tiling: { pad: 1 },
      pathbar: { visible: false },                                  // 상단 검은 띠 제거(클릭 토글로 복귀)
      hovertemplate: "<b>%{label}</b><br>시가총액 %{customdata[0]:,.2f}조원<extra></extra>",
    };
    const layout = { height: 580, margin: { t: 4, b: 4, l: 4, r: 4 }, paper_bgcolor: "#0c0f15" };
    const config = { displayModeBar: false, responsive: true };
    Plotly.react(el, [trace], layout, config).then(() => {
      const gd = el as unknown as { _wired?: boolean; on: (e: string, cb: (d: PlotlyEvt) => boolean | void) => void };
      if (gd._wired) return;
      gd._wired = true;
      gd.on("plotly_treemapclick", (d) => {
        const pt = d.points?.[0];
        const ticker = pt?.customdata?.[1] as string;
        if (ticker) { pickRef.current(ticker); return false; }      // 기업 → 주가(드릴 막음)
        const id = pt?.id || ROOT_ID;                                // 그룹 → 확대 / 같은 노드 재클릭 → 원래대로
        levelRef.current = id === levelRef.current ? ROOT_ID : id;
        Plotly.react(el, [{ ...trace, level: levelRef.current }], layout, config);
        return false;
      });
      gd.on("plotly_hover", (d) => {
        const cd = d.points?.[0]?.customdata;
        const stage = cd?.[2] as string, detail = cd?.[3] as string;
        if (stage && detail) setHover({ stage, detail, x: d.event?.clientX ?? 0, y: d.event?.clientY ?? 0 });
        else setHover(null);
      });
      gd.on("plotly_unhover", () => setHover(null));
    });
  }, [companies]);

  useEffect(() => () => { const el = ref.current; if (el) Plotly.purge(el); }, []);

  return (
    <div>
      <div ref={ref} style={{ width: "100%", minHeight: 580, background: "#0c0f15", borderRadius: 6 }} />
      {hover && (() => {
        const peers = companies.filter((c) => c.stage === hover.stage && c.detail === hover.detail)
          .sort((a, b) => (b.ret?.d60 ?? -1e9) - (a.ret?.d60 ?? -1e9));
        const cell = (v: number | null | undefined) => (
          <td style={{ padding: "2px 6px", textAlign: "right",
            color: v == null ? "var(--muted)" : v >= 0 ? "#16a34a" : "#dc2626" }}>
            {v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`}
          </td>
        );
        return (
          <div style={{ position: "fixed", left: Math.min(hover.x + 14, window.innerWidth - 340),
            top: Math.min(hover.y + 14, window.innerHeight - 260), zIndex: 60, pointerEvents: "none",
            background: "#0f2342", color: "#e8eef7", border: "1px solid #2b4a63",
            borderRadius: 8, padding: "8px 10px", boxShadow: "0 8px 24px rgba(0,0,0,0.35)", fontSize: 12, minWidth: 290 }}>
            <div style={{ fontWeight: 700, marginBottom: 4 }}>{hover.stage} › {hover.detail} <span style={{ color: "#93a4c0", fontWeight: 400 }}>· 동일 산업 내 경쟁사 주가 수익률</span></div>
            <table style={{ borderCollapse: "collapse", width: "100%" }}>
              <thead>
                <tr style={{ color: "#93a4c0" }}>
                  <th style={{ textAlign: "left", padding: "2px 6px", fontWeight: 600 }}>기업</th>
                  <th style={{ textAlign: "right", padding: "2px 6px", fontWeight: 600 }}>5일</th>
                  <th style={{ textAlign: "right", padding: "2px 6px", fontWeight: 600 }}>1개월</th>
                  <th style={{ textAlign: "right", padding: "2px 6px", fontWeight: 600 }}>3개월</th>
                  <th style={{ textAlign: "right", padding: "2px 6px", fontWeight: 600 }}>1년</th>
                </tr>
              </thead>
              <tbody>
                {peers.map((c) => (
                  <tr key={c.ticker}>
                    <td style={{ padding: "2px 6px", whiteSpace: "nowrap" }}>{c.name}</td>
                    {cell(c.ret?.d5)}{cell(c.ret?.d20)}{cell(c.ret?.d60)}{cell(c.ret?.d240)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        );
      })()}
    </div>
  );
}

// ── 캔들/세모 마커 (개별 종목 차트용) ──
function TriUp({ cx, cy }: { cx?: number; cy?: number }) {
  if (cx == null || cy == null) return null;
  return <path d={`M ${cx} ${cy - 12} L ${cx - 5} ${cy - 5} L ${cx + 5} ${cy - 5} Z`} fill={UP} stroke="#fff" strokeWidth={0.6} />;
}
function TriDown({ cx, cy }: { cx?: number; cy?: number }) {
  if (cx == null || cy == null) return null;
  return <path d={`M ${cx} ${cy + 12} L ${cx - 5} ${cy + 5} L ${cx + 5} ${cy + 5} Z`} fill={DOWN} stroke="#fff" strokeWidth={0.6} />;
}
function Candle(props: { x?: number; y?: number; width?: number; height?: number; payload?: SymbolPoint }) {
  const { x, y, width, height, payload } = props;
  if (x == null || y == null || width == null || height == null || !payload) return null;
  const { open, high, low, close } = payload;
  if (open == null || high == null || low == null || close == null) return null;
  const color = close >= open ? UP : DOWN;
  const cx = x + width / 2;
  const span = high - low;
  const pxPer = span ? height / span : 0;
  const yOpen = y + (high - open) * pxPer, yClose = y + (high - close) * pxPer;
  const bodyTop = Math.min(yOpen, yClose), bodyH = Math.max(Math.abs(yClose - yOpen), 1);
  const bw = Math.max(Math.min(width * 0.62, 10), 2);
  return (
    <g>
      <line x1={cx} y1={y} x2={cx} y2={y + height} stroke={color} strokeWidth={1} />
      <rect x={cx - bw / 2} y={bodyTop} width={bw} height={bodyH} fill={color} />
    </g>
  );
}

const TIP = { contentStyle: { fontSize: 12, padding: "8px 11px", borderRadius: 8 },
  labelStyle: { fontSize: 12, fontWeight: 700, marginBottom: 5 }, itemStyle: { fontSize: 12, padding: "1px 0" } };

export default function IndustryAnalysis() {
  const [data, setData] = useState<IndustryData | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<SymbolDetail | null>(null);
  const [range, setRange] = useState("3y");
  const [dBusy, setDBusy] = useState(false);
  const [fStage, setFStage] = useState<string | null>(null);   // 기업 찾기 — 단계 필터
  const [fDetail, setFDetail] = useState<string | null>(null); // 세부분류 필터

  useEffect(() => {
    setBusy(true);
    api.industryDetail("2차전지")
      .then((d) => { setData(d); setErr(""); })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setBusy(false));
  }, []);

  // 기업 선택 시 주가 상세 fetch
  useEffect(() => {
    if (!sel) { setDetail(null); return; }
    let alive = true;
    setDBusy(true);
    api.symbolDetail(sel, range)
      .then((d) => { if (alive) setDetail(d); })
      .catch(() => { if (alive) setDetail(null); })
      .finally(() => { if (alive) setDBusy(false); });
    return () => { alive = false; };
  }, [sel, range]);

  const companies = data?.companies || [];
  const nameOf = (t: string) => companies.find((c) => c.ticker === t)?.name || t;

  // 개별 종목 차트 데이터 (캔들 + ±10% + 거래량)
  const chartData = (detail?.series || []).map((p) => ({
    ...p,
    hl: p.low != null && p.high != null ? [p.low, p.high] : null,
    up_spike: p.chg_pct != null && p.chg_pct >= 10 ? p.high : null,
    down_spike: p.chg_pct != null && p.chg_pct <= -10 ? p.low : null,
  }));
  let pmin = Infinity, pmax = -Infinity;
  chartData.forEach((d) => {
    [d.low, d.high, d.ma20, d.ma60].forEach((v) => {
      if (v != null) { if (v < pmin) pmin = v; if (v > pmax) pmax = v; }
    });
  });
  const pDomain: [number, number] = pmin <= pmax ? [Math.floor(pmin * 0.985), Math.ceil(pmax * 1.015)] : [0, 1];

  return (
    <div className="dashboard-fullwidth">
      <h1>산업 분석</h1>
      <p style={{ color: "var(--muted)", marginTop: 0, fontSize: 13 }}>
        산업별 밸류체인·기업·시장정보. 박스 크기 = 시가총액 / 색 = 전일대비 등락(상승 초록·하락 빨강·보합 파랑).
        그룹을 클릭하면 확대(다시 클릭 시 복귀), 기업을 클릭하면 주가 추이를 확인할 수 있습니다.
      </p>

      {/* 산업 탭 (현재 2차전지, 이후 확장) */}
      <div style={{ display: "flex", gap: 6, margin: "4px 0 12px" }}>
        {(data?.available || ["2차전지"]).map((nm) => (
          <span key={nm} style={{ fontSize: 13, fontWeight: 700, padding: "5px 14px", borderRadius: 999,
            background: "rgba(79,143,245,0.14)", border: "1px solid rgba(79,143,245,0.5)", color: "#1668c4" }}>
            {nm}
          </span>
        ))}
        <span style={{ fontSize: 12, color: "var(--muted)", alignSelf: "center" }}>
          반도체·AI·모빌리티 등은 추후 동일 구조로 확장 예정
        </span>
      </div>

      {err && <div className="error">{err}</div>}
      {busy && <p style={{ color: "var(--muted)" }}>산업 데이터 불러오는 중…</p>}

      {data && (
        <>
          {/* 밸류체인 시가총액 트리맵 */}
          <div className="panel">
            <h3 style={{ marginTop: 0 }}>2차전지 밸류체인 시가총액 히트맵</h3>
            <IndustryTreemap companies={companies} onPick={setSel} />
            <p style={{ fontSize: 11, color: "var(--muted)", margin: "6px 2px 0" }}>
              박스 크기 = 시가총액 · 색 = 전일대비 등락(상승 초록·하락 빨강·보합 파랑, 변동폭 클수록 진함) ·
              그룹(구분/단계/세부분류) 클릭 = 확대(같은 영역 재클릭 = 원래대로) · 기업 클릭 = 주가 차트.
              출처: FinanceDataReader(시총·등락) / 밸류체인·재무: 내부 데이터.
            </p>
          </div>

          {/* 개별 종목 주가 차트 (클릭 시) */}
          {sel && (
            <div className="panel" style={{ marginTop: 16 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
                <h3 style={{ margin: 0 }}>{nameOf(sel)} <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 13 }}>{sel}</span> · 주가 추이 (캔들 · 급등락 ±10%)</h3>
                <div style={{ display: "flex", gap: 4 }}>
                  {RANGES.map(([v, l]) => (
                    <button key={v} type="button" className="ghost sm" onClick={() => setRange(v)}
                      style={{ fontSize: 12, padding: "3px 10px",
                        background: range === v ? "rgba(79,143,245,0.16)" : undefined,
                        fontWeight: range === v ? 700 : 400, color: range === v ? "#1668c4" : undefined }}>{l}</button>
                  ))}
                  <button type="button" className="ghost sm" onClick={() => setSel(null)}
                    style={{ fontSize: 12, padding: "3px 10px" }}>✕ 닫기</button>
                </div>
              </div>
              {dBusy && <p style={{ color: "var(--muted)", fontSize: 13 }}>주가 불러오는 중…</p>}
              {detail && chartData.length > 0 && (
                <>
                  <ResponsiveContainer width="100%" height={360}>
                    <ComposedChart data={chartData} margin={{ top: 5, right: 16, bottom: 5, left: 8 }}>
                      <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
                      <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
                      <YAxis domain={pDomain} tick={{ fontSize: 11 }} width={56}
                        tickFormatter={(v) => `${Math.round(v / 1000)}k`} />
                      <Tooltip {...TIP} />
                      <Legend wrapperStyle={{ fontSize: 11 }} />
                      <Bar dataKey="hl" isAnimationActive={false} legendType="none" shape={Candle} />
                      <Line type="monotone" dataKey="ma20" stroke={ACCENT} strokeWidth={1} dot={false} name="MA20" connectNulls />
                      <Line type="monotone" dataKey="ma60" stroke={DOWN} strokeWidth={1} dot={false} name="MA60" connectNulls />
                      <Scatter dataKey="up_spike" shape={<TriUp />} name="급등 +10%" legendType="triangle" fill={UP} />
                      <Scatter dataKey="down_spike" shape={<TriDown />} name="급락 −10%" legendType="triangle" fill={DOWN} />
                    </ComposedChart>
                  </ResponsiveContainer>
                  <ResponsiveContainer width="100%" height={130}>
                    <ComposedChart data={chartData} margin={{ top: 5, right: 16, bottom: 5, left: 8 }}>
                      <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
                      <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={48} />
                      <YAxis tick={{ fontSize: 10 }} width={56}
                        tickFormatter={(v) => v >= 1e6 ? `${(v / 1e6).toFixed(0)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : String(v)} />
                      <Tooltip {...TIP} formatter={(v) => Number(v).toLocaleString()} />
                      <Bar dataKey="volume" name="거래량" fill="#9aa6b8" isAnimationActive={false} />
                    </ComposedChart>
                  </ResponsiveContainer>
                  <p style={{ fontSize: 11, color: "var(--muted)", margin: "6px 2px 0" }}>
                    투자자별 순매매·공시·컨센서스 등 상세 분석은 좌측 “개별 종목 분석”에서 {nameOf(sel)} 검색 시 확인할 수 있습니다.
                  </p>
                </>
              )}
            </div>
          )}

          {/* 기업 찾기 — 단계·세부분류 클릭으로 원하는 기업만 */}
          <div className="panel" style={{ marginTop: 16, overflowX: "auto" }}>
            <h3 style={{ marginTop: 0 }}>기업 찾기 — 단계·세부분류 선택</h3>
            {(() => {
              const chip = (active: boolean): React.CSSProperties => ({
                fontSize: 12, padding: "4px 12px",
                background: active ? "rgba(79,143,245,0.16)" : undefined,
                fontWeight: active ? 700 : 400, color: active ? "#1668c4" : undefined,
              });
              const stagesPresent = STAGE_ORDER.filter((st) => companies.some((c) => c.stage === st));
              const detailsOf = (st: string) => [...new Set(companies.filter((c) => c.stage === st).map((c) => c.detail))];
              const filtered = fStage
                ? companies.filter((c) => c.stage === fStage && (!fDetail || c.detail === fDetail))
                : [];
              return (
                <>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 8 }}>
                    {stagesPresent.map((st) => (
                      <button key={st} type="button" className="ghost sm"
                        onClick={() => { setFStage(st === fStage ? null : st); setFDetail(null); }}
                        style={chip(fStage === st)}>{st}</button>
                    ))}
                  </div>
                  {fStage && (
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 10 }}>
                      <button type="button" className="ghost sm" onClick={() => setFDetail(null)} style={chip(!fDetail)}>전체</button>
                      {detailsOf(fStage).map((d) => (
                        <button key={d} type="button" className="ghost sm"
                          onClick={() => setFDetail(d === fDetail ? null : d)} style={chip(fDetail === d)}>{d}</button>
                      ))}
                    </div>
                  )}
                  {!fStage ? (
                    <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>
                      위에서 단계(원자재·소재·셀 …)를 선택하면 해당 기업만 표시됩니다. 행을 클릭하면 주가 차트가 열립니다.
                    </p>
                  ) : (
                    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, whiteSpace: "nowrap" }}>
                      <thead>
                        <tr style={{ borderBottom: "2px solid #e3e8ef", textAlign: "right" }}>
                          <th style={{ textAlign: "left", padding: "7px 6px" }}>기업 (세부분류)</th>
                          <th style={{ padding: "7px 6px" }}>시가총액</th>
                          <th style={{ padding: "7px 6px" }}>M/s</th>
                          <th style={{ padding: "7px 6px" }}>등락</th>
                          <th style={{ padding: "7px 6px" }}>매출액</th>
                          <th style={{ padding: "7px 6px" }}>영익률</th>
                          <th style={{ textAlign: "left", padding: "7px 6px" }}>주요제품</th>
                        </tr>
                      </thead>
                      <tbody>
                        {filtered.map((c) => (
                          <tr key={c.ticker} onClick={() => setSel(c.ticker)}
                            style={{ borderBottom: "1px solid #f0ece6", cursor: "pointer",
                              background: sel === c.ticker ? "rgba(79,143,245,0.1)" : undefined }}>
                            <td style={{ padding: "6px", fontWeight: 600 }}>{c.name}
                              <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 11 }}> {c.ticker} · {c.detail}</span></td>
                            <td style={{ padding: "6px", textAlign: "right" }}>{trillion(c.cap)}</td>
                            <td style={{ padding: "6px", textAlign: "right", color: "var(--muted)" }}>{c.ms != null ? `${c.ms.toFixed(0)}%` : "—"}</td>
                            <td style={{ padding: "6px", textAlign: "right", color: c.chg == null ? "var(--muted)" : c.chg >= 0 ? UP : DOWN }}>{pct(c.chg, 2)}</td>
                            <td style={{ padding: "6px", textAlign: "right" }}>{won(c.revenue)}</td>
                            <td style={{ padding: "6px", textAlign: "right", color: c.op_margin != null && c.op_margin < 0 ? DOWN : "inherit" }}>{c.op_margin != null ? `${c.op_margin.toFixed(1)}%` : "—"}</td>
                            <td style={{ padding: "6px", color: "var(--muted)", fontSize: 12 }}>{c.product}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </>
              );
            })()}
          </div>
        </>
      )}
    </div>
  );
}

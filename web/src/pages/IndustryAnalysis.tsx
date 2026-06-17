import { useState, useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import {
  ComposedChart, Bar, Line, Scatter, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, Legend,
} from "recharts";
import Plotly from "plotly.js-dist-min";
import { api } from "../api";
import type { IndustryData, IndustryCompany, SymbolDetail, SymbolPoint, KrExtras,
  StockOpinion, OpinionStance, SectorNews } from "../types";

// 한국식 시장 색 (상승=빨강 / 하락=파랑)
const UP = "#de3033", DOWN = "#1668c4", ACCENT = "#c4982b";

// ── 포맷터 ──
const pct = (v: number | null, d = 1) => v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(d)}%`;
// 금액 단위 통일 — 원 → 억원(소수점 없이 정수, 천단위 콤마). 표의 모든 금액에 사용.
const eok = (v: number | null) => v == null ? "—" : `${Math.round(v / 1e8).toLocaleString()}억`;

// 등락률 → 색 (Finviz 맵 팔레트): 보합·결측=회색, 상승=초록 / 하락=빨강, ±3%에서 최대 채도.
const FRAME = "#363c45";   // 대분류(구분/단계) 헤더 띠 = 진회색
function colorByChg(chg: number | null): string {
  if (chg == null) return "#2b2f38";
  const t = Math.max(-1, Math.min(1, chg / 3));
  const gray = [0x3d, 0x46, 0x54];                                // 0% 중립 = 슬레이트(finviz 띠 #3d4654)
  if (t === 0) return `rgb(${gray.join(",")})`;
  const tgt = t > 0 ? [0x2e, 0xcc, 0x5a] : [0xe5, 0x48, 0x48];    // +3 초록(#2ecc5a) / -3 빨강(#e54848)
  const f = 0.25 + 0.75 * Math.abs(t);
  return `rgb(${gray.map((v, i) => Math.round(v + (tgt[i] - v) * f)).join(",")})`;
}

const STAGE_ORDER = ["원자재", "소재", "배터리", "부품", "장비", "리사이클", "애플리케이션"];
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
    // 완성차(Downstream)는 시총이 너무 커 배터리 소형주를 가려 트리맵에서 제외 —
    // 좌측 표(companies 전체)에는 그대로 표시됨. 트리맵 폭도 Midstream 끝으로 한정됨.
    const valid = companies.filter((c) => c.cap && c.cap > 0 && c.gu !== "Downstream");
    if (!el || valid.length === 0) return;

    const ids: string[] = [], labels: string[] = [], parents: string[] = [];
    const values: number[] = [], colors: string[] = [], texts: string[] = [];
    const cdata: (string | number)[][] = [];
    const tsizes: number[] = [], tcolors: string[] = [], lcolors: string[] = [];   // 글자크기·글자색·테두리색(노드별)
    const tril = (v: number) => v / 1e12;
    const sumT = (rows: IndustryCompany[]) => rows.reduce((s, c) => s + (c.cap || 0), 0) / 1e12;
    const detSum: Record<string, number> = {};
    valid.forEach((c) => { detSum[c.detail] = (detSum[c.detail] || 0) + (c.cap || 0); });
    // frame(구분/단계/세부분류) = 작은 회색 헤더 / leaf(기업) = 흰색, 시총 비례 크기
    const add = (id: string, label: string, parent: string, value: number, color: string,
      text: string, cd: (string | number)[], fsize = 18, fcolor = "#c5ccd6") => {
      ids.push(id); labels.push(label); parents.push(parent); values.push(value);
      colors.push(color); texts.push(text); cdata.push(cd);
      tsizes.push(fsize); tcolors.push(fcolor);
      lcolors.push(value > 0 ? "#000000" : "#3f4653");   // 개별 기업(leaf) 테두리=검정 / 프레임=진회색
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
          // 소분류(세부분류) 헤더 색 = 구성 기업 시총가중 평균 등락색 (셀 색 비율 반영)
          const wcap = inDet.reduce((s, c) => s + (c.cap || 0), 0);
          const wchg = wcap ? inDet.reduce((s, c) => s + (c.chg || 0) * (c.cap || 0), 0) / wcap : 0;
          add(dtid, det, did, 0, colorByChg(wchg), "", [sumT(inDet), "", dan, det]);
          for (const c of inDet) {
            const cap = c.cap || 0;
            const lsize = Math.min(22, Math.max(12, Math.round((Math.log10(cap || 1) - 11) * 5 + 12)));
            add(`${dtid}/${c.ticker}`, `<b>${c.name}</b>`, dtid, cap, colorByChg(c.chg),
              pct(c.chg, 1),                                  // 타일엔 등락%만(Finviz식 라벨+%)
              [tril(cap), c.ticker, c.stage, c.detail], lsize, "#ffffff");
          }
        }
      }
    }
    const trace = {
      type: "treemap", ids, labels, parents, values, text: texts, customdata: cdata,
      level: levelRef.current, branchvalues: "remainder", texttemplate: "%{label}<br>%{text}",
      marker: { colors, line: { width: 0.5, color: lcolors }, pad: { t: 24, l: 2, r: 2, b: 2 } },
      textfont: { size: tsizes, color: tcolors }, textposition: "middle center", tiling: { pad: 1 },
      pathbar: { visible: false },                                  // 상단 검은 띠 제거(클릭 토글로 복귀)
      hovertemplate: "<b>%{label}</b><br>시가총액 %{customdata[0]:,.2f}조원<extra></extra>",
    };
    const layout = { height: 900, margin: { t: 4, b: 4, l: 4, r: 4 }, paper_bgcolor: "#0c0f15" };
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
      // 기업 hover 시 소속 소분류(세부분류) 박스 테두리에 형광 표시
      const lineIdx = new Map(ids.map((id, i) => [id, i]));
      const baseColors = colors.slice();   // 원본 채움색(호버 시 복원·밝힘용)
      // 메인 하이라이트 컬러 = 브랜드 컬러(--accent, "Stock" 주황). 테마와 항상 동기화.
      const MAIN = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#d4a738";
      // 색을 흰색 쪽으로 amt만큼 보간(rgb()·#hex 모두 처리) — 3D 팝 시 타일을 밝게.
      const lighten = (col: string, amt: number): string => {
        let rgb: number[] | null = null;
        const mh = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(col);
        const mr = /rgb\((\d+),\s*(\d+),\s*(\d+)\)/i.exec(col);
        if (mh) rgb = [parseInt(mh[1], 16), parseInt(mh[2], 16), parseInt(mh[3], 16)];
        else if (mr) rgb = [+mr[1], +mr[2], +mr[3]];
        if (!rgb) return col;
        return `rgb(${rgb.map((v) => Math.round(v + (255 - v) * amt)).join(",")})`;
      };
      // 기업 타일에 호버 시: 소분류 박스=메인 컬러 테두리 + 기업 타일=흰 테두리·밝은 채움(3D 팝).
      const highlight = (compId: string | null) => {
        const w = ids.map(() => 0.5);
        const lc = lcolors.slice();
        const cc = baseColors.slice();
        if (compId) {
          const ci = lineIdx.get(compId);
          const pid = compId.slice(0, compId.lastIndexOf("/"));
          const pi = lineIdx.get(pid);
          if (pi != null && pi >= 0) { w[pi] = 6; lc[pi] = MAIN; }     // 소분류 박스 = 메인 컬러
          if (ci != null && ci >= 0) {                                  // 기업 타일 = 3D 팝
            w[ci] = 4; lc[ci] = "#ffffff"; cc[ci] = lighten(baseColors[ci], 0.34);
          }
        }
        (Plotly as unknown as { restyle: (e: unknown, u: object, t: number[]) => void })
          .restyle(el, { "marker.line.width": [w], "marker.line.color": [lc], "marker.colors": [cc] }, [0]);
      };
      gd.on("plotly_hover", (d) => {
        const pt = d.points?.[0];
        const cd = pt?.customdata;
        const stage = cd?.[2] as string, detail = cd?.[3] as string;
        if (stage && detail) setHover({ stage, detail, x: d.event?.clientX ?? 0, y: d.event?.clientY ?? 0 });
        else setHover(null);
        const isCompany = !!(cd?.[1]);                       // customdata[1]=ticker → 기업 타일
        highlight(isCompany && pt?.id ? String(pt.id) : null);
      });
      gd.on("plotly_unhover", () => { setHover(null); highlight(null); });
    });
  }, [companies]);

  useEffect(() => () => { const el = ref.current; if (el) Plotly.purge(el); }, []);

  return (
    <div>
      <div ref={ref} style={{ width: "100%", minHeight: 900, background: "#0c0f15", borderRadius: 6 }} />
      {hover && (() => {
        const peers = companies.filter((c) => c.stage === hover.stage && c.detail === hover.detail)
          .sort((a, b) => (b.ret?.d60 ?? -1e9) - (a.ret?.d60 ?? -1e9));
        const cell = (v: number | null | undefined) => (
          <td style={{ padding: "3px 9px", textAlign: "right",
            color: v == null ? "var(--muted)" : v >= 0 ? "#16a34a" : "#dc2626" }}>
            {v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`}
          </td>
        );
        return (
          <div style={{ position: "fixed", left: Math.min(hover.x + 16, window.innerWidth - 470),
            top: Math.min(hover.y + 16, window.innerHeight - 380), zIndex: 60, pointerEvents: "none",
            background: "#0f2342", color: "#e8eef7", border: "1px solid #2b4a63",
            borderRadius: 10, padding: "12px 15px", boxShadow: "0 8px 24px rgba(0,0,0,0.35)", fontSize: 18, minWidth: 440 }}>
            <div style={{ fontWeight: 700, marginBottom: 7, fontSize: 18 }}>{hover.stage} › {hover.detail} <span style={{ color: "#93a4c0", fontWeight: 400, fontSize: 14 }}>· 동일 산업 내 경쟁사 주가 수익률</span></div>
            <table style={{ borderCollapse: "collapse", width: "100%" }}>
              <thead>
                <tr style={{ color: "#93a4c0" }}>
                  <th style={{ textAlign: "left", padding: "3px 9px", fontWeight: 600 }}>기업</th>
                  <th style={{ textAlign: "right", padding: "3px 9px", fontWeight: 600 }}>5일</th>
                  <th style={{ textAlign: "right", padding: "3px 9px", fontWeight: 600 }}>1개월</th>
                  <th style={{ textAlign: "right", padding: "3px 9px", fontWeight: 600 }}>3개월</th>
                  <th style={{ textAlign: "right", padding: "3px 9px", fontWeight: 600 }}>1년</th>
                </tr>
              </thead>
              <tbody>
                {peers.map((c) => (
                  <tr key={c.ticker}>
                    <td style={{ padding: "3px 9px", whiteSpace: "nowrap" }}>{c.name}</td>
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

// 선택 종목의 추정 실적 + 애널리스트 리포트 — 개별종목분석(KrSections)의 표를 그대로 재사용.
export function CompanyReport({ ticker, company }: { ticker: string; company?: IndustryCompany }) {
  const [kr, setKr] = useState<KrExtras | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    setBusy(true); setKr(null);
    api.krExtras(ticker)
      .then((d) => { if (alive) setKr(d); })
      .catch(() => { if (alive) setKr(null); })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [ticker]);

  // FnGuide 추정실적은 억원 단위 — 그대로 정수(억) 표기로 통일.
  const jo = (v: number | null) =>
    v == null ? "—" : `${Math.round(v).toLocaleString()}억`;
  const mult = (v: number | null | undefined) =>
    v == null || isNaN(v) ? "—" : `${v.toFixed(1)}x`;
  const pctv = (v: number | null | undefined) =>
    v == null || isNaN(v) ? "—" : `${v.toFixed(1)}%`;
  // 영업이익률은 영업이익 아래, 당기순이익률은 당기순이익 아래(%). 마진은 서버에서 계산·저장됨.
  const EARN_ROWS: { key: string; label: string; pct?: boolean }[] = [
    { key: "매출액", label: "매출액" },
    { key: "영업이익", label: "영업이익" },
    { key: "영업이익률", label: "영업이익률 (%)", pct: true },
    { key: "당기순이익", label: "당기순이익" },
    { key: "당기순이익률", label: "당기순이익률 (%)", pct: true },
    { key: "지배주주", label: "지배주주순이익" },
  ];
  const MULT_ROWS: [string, string][] = [["P/E", "PER"], ["P/B", "PBR"]];   // 표시명 → FnGuide 행 키
  const earnings = kr?.earnings;
  const hasEarnings = !!earnings && earnings.years.length > 0 && EARN_ROWS.some((r) => earnings.rows[r.key]);
  const reports = kr?.reports || [];
  const targetByBroker = new Map((kr?.consensus || []).map((c) => [c.broker, c.target] as const));
  // 멀티플 — 시가총액(억) 기준. 순차입금은 무료 소스 한계로 미반영(EV≈시총).
  const capEok = company?.cap != null ? company.cap / 1e8 : null;   // 억
  const daEok = company?.da != null ? company.da / 1e8 : null;      // 억
  const opRow = earnings?.rows["영업이익"];
  const revRow = earnings?.rows["매출액"];
  const perRow = earnings?.rows["PER"];
  const epsRow = earnings?.rows["EPS"];
  const roeRow = earnings?.rows["ROE"];
  const evEbitdaAt = (i: number): number | null => {           // 시총 / (영업이익ᴱ + 최근 D&A)
    if (capEok == null || daEok == null || !opRow) return null;
    const op = opRow[i];
    if (op == null) return null;
    const e = op + daEok;
    return e > 0 ? capEok / e : null;
  };
  const psrAt = (i: number): number | null =>                  // 시총 / 매출액
    capEok != null && revRow && revRow[i] ? capEok / (revRow[i] as number) : null;
  const pegAt = (i: number): number | null => {               // PER / EPS성장률(%)
    if (!perRow || !epsRow || i <= 0) return null;
    const per = perRow[i], e0 = epsRow[i], e1 = epsRow[i - 1];
    if (per == null || e0 == null || e1 == null || e1 === 0) return null;
    const g = (e0 / e1 - 1) * 100;
    return g > 0 ? per / g : null;
  };
  const hasMult = !!earnings && (!!earnings.rows["PER"] || !!earnings.rows["PBR"] || (capEok != null && daEok != null));

  if (busy) return <p style={{ color: "var(--muted)", fontSize: 13 }}>리포트 불러오는 중…</p>;

  return (
    <>
      {/* 추정 실적 — 개별종목분석과 동일 */}
      {hasEarnings ? (
        <div style={{ overflowX: "auto" }}>
          <h3 style={{ margin: "0 0 6px" }}>추정 실적 <span style={{ fontWeight: 400, fontSize: 12, color: "var(--muted)" }}>(연결 · 단위 억원)</span></h3>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
            <thead>
              <tr style={{ borderBottom: "2px solid #e3e8ef" }}>
                <th style={{ textAlign: "left", padding: "6px 6px" }}>항목</th>
                {earnings!.years.map((y) => (
                  <th key={y} style={{ padding: "6px 6px", textAlign: "right",
                    color: y.includes("(E)") ? ACCENT : "inherit" }}>{y}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {EARN_ROWS.filter((r) => earnings!.rows[r.key]).map((r) => (
                <tr key={r.key} style={{ borderBottom: "1px solid var(--border)" }}>
                  <td style={{ padding: "6px", fontWeight: r.pct ? 400 : 600,
                    color: r.pct ? "var(--muted)" : "inherit", paddingLeft: r.pct ? 16 : 6 }}>{r.label}</td>
                  {earnings!.rows[r.key].map((v, j) => (
                    <td key={j} style={{ padding: "6px", textAlign: "right",
                      color: earnings!.years[j]?.includes("(E)") ? ACCENT : (r.pct ? "var(--muted)" : "inherit") }}>
                      {r.pct ? pctv(v) : jo(v)}</td>
                  ))}
                </tr>
              ))}
              {roeRow && (
                <tr style={{ borderBottom: "1px solid var(--border)" }}>
                  <td style={{ padding: "6px", fontWeight: 600 }}>ROE</td>
                  {roeRow.map((v, j) => (
                    <td key={j} style={{ padding: "6px", textAlign: "right",
                      color: earnings!.years[j]?.includes("(E)") ? ACCENT : "inherit" }}>{pctv(v)}</td>
                  ))}
                </tr>
              )}
              {perRow && epsRow && (
                <tr style={{ borderBottom: "1px solid var(--border)" }}>
                  <td style={{ padding: "6px", fontWeight: 600 }}>PEG</td>
                  {earnings!.years.map((y, j) => (
                    <td key={j} style={{ padding: "6px", textAlign: "right",
                      color: y.includes("(E)") ? ACCENT : "inherit" }}>{mult(pegAt(j))}</td>
                  ))}
                </tr>
              )}
              {hasMult && (
                <>
                  <tr>
                    <td colSpan={earnings!.years.length + 1}
                      style={{ padding: "6px", fontSize: "12pt", fontWeight: 700, color: "var(--muted)",
                        borderTop: "2px solid var(--border)" }}>Multiple</td>
                  </tr>
                  {MULT_ROWS.filter(([, key]) => earnings!.rows[key]).map(([label, key]) => (
                    <tr key={key} style={{ borderBottom: "1px solid var(--border)" }}>
                      <td style={{ padding: "6px", fontWeight: 600 }}>{label}</td>
                      {earnings!.rows[key].map((v, j) => (
                        <td key={j} style={{ padding: "6px", textAlign: "right",
                          color: earnings!.years[j]?.includes("(E)") ? ACCENT : "inherit" }}>{mult(v)}</td>
                      ))}
                    </tr>
                  ))}
                  {capEok != null && daEok != null && (
                    <tr style={{ borderBottom: "1px solid var(--border)" }}>
                      <td style={{ padding: "6px", fontWeight: 600 }}>EV/EBITDA</td>
                      {earnings!.years.map((y, j) => (
                        <td key={j} style={{ padding: "6px", textAlign: "right",
                          color: y.includes("(E)") ? ACCENT : "inherit" }}>{mult(evEbitdaAt(j))}</td>
                      ))}
                    </tr>
                  )}
                  {capEok != null && revRow && (
                    <tr style={{ borderBottom: "1px solid var(--border)" }}>
                      <td style={{ padding: "6px", fontWeight: 600 }}>PSR</td>
                      {earnings!.years.map((y, j) => (
                        <td key={j} style={{ padding: "6px", textAlign: "right",
                          color: y.includes("(E)") ? ACCENT : "inherit" }}>{mult(psrAt(j))}</td>
                      ))}
                    </tr>
                  )}
                </>
              )}
            </tbody>
          </table>
          <p style={{ fontSize: 11, color: "var(--muted)", margin: "6px 2px 0" }}>
            Source: FnGuide Financial Highlight
            {capEok != null && daEok != null && (
              <><br />EV/EBITDA = 시가총액 / (영업이익 + 감가상각비·무형자산상각비) · 순차입금 미반영(EV≈시총)</>
            )}
          </p>
        </div>
      ) : (
        <p style={{ color: "var(--muted)", fontSize: 13 }}>추정 실적 데이터가 없습니다.</p>
      )}

      {/* 애널리스트 리포트 — 제목 클릭 시 원문(PDF·네이버) 다운로드. 소제목·본문 크기 컨센서스와 통일 */}
      <h3 style={{ margin: "16px 0 6px" }}>애널리스트 리포트</h3>
      {reports.length > 0 ? (<>
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {reports.map((r, i) => {
            const tgt = r.target ?? targetByBroker.get(r.broker);   // 리포트 고유 목표가 우선
            return (
              <li key={i} style={{ padding: "7px 0", borderBottom: "1px solid var(--border)",
                fontSize: "12pt", display: "flex", justifyContent: "space-between", gap: 10 }}>
                <div style={{ minWidth: 0 }}>
                  <a href={r.url} target="_blank" rel="noreferrer"
                    style={{ fontWeight: 600, fontSize: "12pt", color: DOWN, textDecoration: "none" }}>{r.title}</a>
                  <div style={{ color: "var(--muted)", fontSize: "12pt", marginTop: 2 }}>{r.broker} · {r.date}</div>
                </div>
                <div style={{ textAlign: "right", flexShrink: 0 }}>
                  <div style={{ color: "var(--muted)", fontSize: 11 }}>목표주가</div>
                  <div style={{ fontWeight: 700, fontSize: "12pt" }}>{tgt != null ? tgt.toLocaleString() + "원" : "—"}</div>
                </div>
              </li>
            );
          })}
        </ul>
        <p style={{ fontSize: 11, color: "var(--muted)", margin: "6px 2px 0" }}>
          제목을 클릭하면 원문(PDF·네이버 리포트)이 열립니다. 목표주가는 해당 증권사 최신 컨센서스 기준.
        </p>
      </>) : <p style={{ color: "var(--muted)", fontSize: 13, marginBottom: 0 }}>최근 리포트가 없습니다.</p>}
    </>
  );
}

// 의견 스탠스 메타 (매수=초록 / 중립=회색 / 매도=빨강)
const STANCE_META: Record<OpinionStance, { label: string; color: string; bg: string }> = {
  buy: { label: "매수", color: "#2ea65a", bg: "rgba(46,166,90,0.16)" },
  neutral: { label: "중립", color: "#8b94a3", bg: "rgba(139,148,163,0.16)" },
  sell: { label: "매도", color: "#e0504f", bg: "rgba(224,80,79,0.16)" },
};

// 사용자 작성 HTML 정화 — script·이벤트핸들러·javascript: 제거. 운영자 승인과 병행한 최소 XSS 방어.
// (data: 이미지 URL은 허용 — 본문 인라인 이미지가 그렇게 저장된다.)
function sanitizeHtml(html: string): string {
  const doc = new DOMParser().parseFromString(html, "text/html");
  doc.querySelectorAll("script,style,iframe,object,embed,link,meta,form").forEach((el) => el.remove());
  doc.querySelectorAll("*").forEach((el) => {
    Array.from(el.attributes).forEach((a) => {
      const n = a.name.toLowerCase();
      if (n.startsWith("on")) el.removeAttribute(a.name);
      if ((n === "href" || n === "src") && /^\s*javascript:/i.test(a.value)) el.removeAttribute(a.name);
    });
  });
  return doc.body.innerHTML;
}

const TEXT_COLORS = ["#e6e9ef", "#e0504f", "#4f8ff5", "#2ea65a", "#c4982b"];
const HILITE_COLORS = ["#5a4d00", "#0d3a5c", "#15401f", "#4a1530"];

// 개별 기업 투자의견(Ratings) — 매수/중립/매도 + 제목·목표주가 + 리치 분석글 + 댓글 + 좋아요/싫어요.
// 운영자(MyStock) 승인된 글만 공개. 작성은 이 보드(Ratings 탭)에서만.
export function OpinionBoard({ ticker, name }: { ticker: string; name: string }) {
  const [list, setList] = useState<StockOpinion[]>([]);
  const [busy, setBusy] = useState(false);
  const [stance, setStance] = useState<OpinionStance>("buy");
  const [title, setTitle] = useState("");
  const [target, setTarget] = useState("");          // 목표주가 입력(문자열)
  const [posting, setPosting] = useState(false);
  const [expanded, setExpanded] = useState(false);   // 작성란 확대
  const [cur, setCur] = useState<number | null>(null);   // 현재가 — 상승여력 계산
  const [cInput, setCInput] = useState<Record<number, string>>({});
  const editorRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let alive = true;
    setBusy(true); setList([]);
    api.opinions(ticker)
      .then((d) => { if (alive) setList(d.opinions); })
      .catch(() => { if (alive) setList([]); })
      .finally(() => { if (alive) setBusy(false); });
    api.symbolDetail(ticker, "1y")
      .then((d) => { if (alive) setCur(d.last?.close ?? null); })
      .catch(() => { /* 무시 */ });
    return () => { alive = false; };
  }, [ticker]);

  const upsideOf = (tp: number | null) =>
    (tp != null && cur != null && cur > 0) ? (tp / cur - 1) * 100 : null;

  // 리치 에디터 — execCommand 기반(서식·색·하이라이트·이미지). 본문은 HTML로 저장.
  const exec = (cmd: string, val?: string) => {
    document.execCommand("styleWithCSS", false, "true");
    document.execCommand(cmd, false, val);
    editorRef.current?.focus();
  };
  const insertImageFile = (file: File) => {
    const r = new FileReader();
    r.onload = () => { editorRef.current?.focus(); document.execCommand("insertImage", false, String(r.result)); };
    r.readAsDataURL(file);
  };
  const onPaste = (e: React.ClipboardEvent) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const it of Array.from(items)) {
      if (it.type.startsWith("image/")) {
        const f = it.getAsFile();
        if (f) { e.preventDefault(); insertImageFile(f); }
      }
    }
  };

  const submit = () => {
    if (posting) return;
    const html = sanitizeHtml(editorRef.current?.innerHTML || "").trim();
    const plain = (editorRef.current?.textContent || "").trim();
    if (!title.trim()) { alert("제목을 입력하세요."); return; }
    if (!plain && !/<img/i.test(html)) { alert("분석 내용을 입력하세요."); return; }
    const tp = target.trim() ? Number(target.replace(/[,\s]/g, "")) : null;
    setPosting(true);
    api.createOpinion(ticker, stance, title.trim(), tp && tp > 0 ? tp : null, html)
      .then((op) => {
        setList((l) => [op, ...l]);
        setTitle(""); setTarget(""); setExpanded(false);
        if (editorRef.current) editorRef.current.innerHTML = "";
        if (op.status === "pending") alert("등록되었습니다. 운영자(MyStock) 승인 후 공개됩니다.");
      })
      .catch((e) => alert((e as Error).message))
      .finally(() => setPosting(false));
  };
  const approve = (id: number) => {
    api.approveOpinion(id)
      .then(() => setList((l) => l.map((o) => o.id === id ? { ...o, status: "approved" } : o)))
      .catch((e) => alert((e as Error).message));
  };
  const vote = (id: number, value: number) => {
    api.voteOpinion(id, value)
      .then((r) => setList((l) => l.map((o) => o.id === id
        ? { ...o, likes: r.likes, dislikes: r.dislikes, my_vote: r.my_vote } : o)))
      .catch(() => { /* 무시 */ });
  };
  const removeOpinion = (id: number) => {
    if (!confirm("이 의견을 삭제할까요?")) return;
    api.deleteOpinion(id)
      .then(() => setList((l) => l.filter((o) => o.id !== id)))
      .catch((e) => alert((e as Error).message));
  };
  const addComment = (id: number) => {
    const text = (cInput[id] || "").trim();
    if (!text) return;
    api.addOpinionComment(id, text)
      .then((c) => {
        setList((l) => l.map((o) => o.id === id ? { ...o, comments: [...o.comments, c] } : o));
        setCInput((m) => ({ ...m, [id]: "" }));
      })
      .catch((e) => alert((e as Error).message));
  };
  const removeComment = (id: number, cid: number) => {
    api.deleteOpinionComment(id, cid)
      .then(() => setList((l) => l.map((o) => o.id === id
        ? { ...o, comments: o.comments.filter((c) => c.id !== cid) } : o)))
      .catch((e) => alert((e as Error).message));
  };
  const fmt = (s: string | null) => (s ? s.slice(0, 16).replace("T", " ") : "");
  const tbBtn: React.CSSProperties = { fontSize: 12, padding: "3px 8px",
    border: "1px solid var(--border)", background: "transparent", borderRadius: 6, cursor: "pointer", color: "var(--fg)" };

  return (
    <>
      {/* 작성 (이 보드에서만) — 제목·투자의견·목표주가·리치 본문 */}
      <div style={{ border: "1px solid var(--border)", borderRadius: 10, padding: 12, marginBottom: 14 }}>
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="분석 제목"
          style={{ width: "100%", fontSize: 14, fontWeight: 700, padding: "8px 10px", marginBottom: 8 }} />
        <div style={{ display: "flex", gap: 6, marginBottom: 8, flexWrap: "wrap", alignItems: "center" }}>
          {(Object.keys(STANCE_META) as OpinionStance[]).map((s) => {
            const m = STANCE_META[s]; const on = stance === s;
            return (
              <button key={s} type="button" onClick={() => setStance(s)}
                style={{ fontSize: 13, fontWeight: 700, padding: "6px 16px", borderRadius: 8,
                  border: `1px solid ${on ? m.color : "var(--border)"}`,
                  background: on ? m.bg : "transparent", color: on ? m.color : "var(--muted)", cursor: "pointer" }}>
                {m.label}
              </button>
            );
          })}
          <span style={{ marginLeft: 8, color: "var(--muted)", fontSize: 12 }}>목표주가</span>
          <input value={target} onChange={(e) => setTarget(e.target.value)} inputMode="numeric"
            placeholder="예: 320000" style={{ width: 110, fontSize: 13, padding: "5px 8px" }} />
          <span style={{ color: "var(--muted)", fontSize: 12 }}>원</span>
          {(() => {
            const tp = target.trim() ? Number(target.replace(/[,\s]/g, "")) : null;
            const up = upsideOf(tp && tp > 0 ? tp : null);
            return up != null ? (
              <span style={{ fontSize: 12, fontWeight: 700, color: up >= 0 ? UP : DOWN }}>
                상승여력 {up >= 0 ? "+" : ""}{up.toFixed(1)}%</span>) : null;
          })()}
        </div>
        {/* 서식 툴바 */}
        <div style={{ display: "flex", gap: 6, marginBottom: 6, flexWrap: "wrap", alignItems: "center" }}>
          <button type="button" style={{ ...tbBtn, fontWeight: 800 }} onMouseDown={(e) => e.preventDefault()} onClick={() => exec("bold")}>B</button>
          <button type="button" style={{ ...tbBtn, fontSize: 11 }} onMouseDown={(e) => e.preventDefault()} onClick={() => exec("fontSize", "2")}>작게</button>
          <button type="button" style={tbBtn} onMouseDown={(e) => e.preventDefault()} onClick={() => exec("fontSize", "3")}>보통</button>
          <button type="button" style={{ ...tbBtn, fontSize: 15 }} onMouseDown={(e) => e.preventDefault()} onClick={() => exec("fontSize", "5")}>크게</button>
          <span style={{ width: 1, height: 18, background: "var(--border)", margin: "0 2px" }} />
          <span style={{ color: "var(--muted)", fontSize: 11 }}>색</span>
          {TEXT_COLORS.map((c) => (
            <button key={c} type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => exec("foreColor", c)}
              title="글자색" style={{ width: 16, height: 16, borderRadius: 4, background: c, border: "1px solid var(--border)", cursor: "pointer" }} />
          ))}
          <span style={{ color: "var(--muted)", fontSize: 11, marginLeft: 4 }}>형광</span>
          {HILITE_COLORS.map((c) => (
            <button key={c} type="button" onMouseDown={(e) => e.preventDefault()} onClick={() => exec("hiliteColor", c)}
              title="형광펜" style={{ width: 16, height: 16, borderRadius: 4, background: c, border: "1px solid var(--border)", cursor: "pointer" }} />
          ))}
          <span style={{ width: 1, height: 18, background: "var(--border)", margin: "0 2px" }} />
          <label style={{ ...tbBtn, display: "inline-flex", alignItems: "center", gap: 4 }}>
            🖼 이미지
            <input type="file" accept="image/*" style={{ display: "none" }}
              onChange={(e) => { const f = e.target.files?.[0]; if (f) insertImageFile(f); e.currentTarget.value = ""; }} />
          </label>
        </div>
        <div ref={editorRef} className="rich-ed" contentEditable suppressContentEditableWarning
          data-ph={`${name}에 대한 투자 의견과 근거를 작성하세요 (이미지 붙여넣기·첨부 가능)`}
          onFocus={() => setExpanded(true)} onPaste={onPaste}
          style={{ width: "100%", fontSize: 13, lineHeight: 1.6, padding: "10px 12px",
            border: "1px solid var(--border)", borderRadius: 8, outline: "none",
            minHeight: expanded ? 200 : 80, transition: "min-height 0.15s", overflowY: "auto", background: "var(--panel)" }} />
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 8 }}>
          <span style={{ color: "var(--muted)", fontSize: 11 }}>※ 등록 글은 운영자(MyStock) 승인 후 공개됩니다.</span>
          <button type="button" onClick={submit} disabled={posting}
            style={{ fontSize: 13, padding: "7px 18px" }}>{posting ? "등록 중…" : "의견 등록"}</button>
        </div>
      </div>

      {/* 목록 */}
      {busy ? (
        <p style={{ color: "var(--muted)", fontSize: 13 }}>불러오는 중…</p>
      ) : list.length === 0 ? (
        <p style={{ color: "var(--muted)", fontSize: 13 }}>아직 등록된 의견이 없습니다. 첫 의견을 남겨보세요.</p>
      ) : list.map((op) => {
        const m = STANCE_META[op.stance];
        const up = upsideOf(op.target_price);
        const pending = op.status === "pending";
        return (
          <div key={op.id} style={{ borderTop: "1px solid var(--border)", padding: "12px 2px", opacity: pending ? 0.72 : 1 }}>
            {op.title && <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 6 }}>{op.title}</div>}
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, flexWrap: "wrap" }}>
              <span style={{ fontSize: 12, fontWeight: 700, padding: "2px 10px", borderRadius: 999, background: m.bg, color: m.color }}>{m.label}</span>
              {op.target_price != null && (
                <span style={{ fontSize: 12, color: "var(--muted)" }}>목표 {op.target_price.toLocaleString()}원
                  {up != null && <b style={{ color: up >= 0 ? UP : DOWN }}> ({up >= 0 ? "+" : ""}{up.toFixed(1)}%)</b>}</span>
              )}
              <span style={{ fontWeight: 600, fontSize: 13 }}>{op.author}</span>
              <span style={{ color: "var(--muted)", fontSize: 12 }}>{fmt(op.created_at)}</span>
              {pending && <span style={{ fontSize: 11, fontWeight: 700, color: ACCENT, border: `1px solid ${ACCENT}`, borderRadius: 999, padding: "1px 8px" }}>승인 대기</span>}
              <span style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
                {op.can_moderate && pending && (
                  <button type="button" className="ghost sm" onClick={() => approve(op.id)}
                    style={{ fontSize: 11, padding: "2px 10px", color: "#2ea65a", borderColor: "#2ea65a" }}>승인</button>
                )}
                {(op.is_mine || op.can_moderate) && (
                  <button type="button" className="ghost sm" onClick={() => removeOpinion(op.id)}
                    style={{ fontSize: 11, padding: "2px 8px" }}>삭제</button>
                )}
              </span>
            </div>
            <div className="op-body" style={{ fontSize: "12pt", lineHeight: 1.7, marginBottom: 8, wordBreak: "break-word" }}
              dangerouslySetInnerHTML={{ __html: sanitizeHtml(op.body) }} />
            <div style={{ display: "flex", gap: 8, marginBottom: 8 }}>
              <button type="button" onClick={() => vote(op.id, 1)}
                style={{ fontSize: 12, padding: "4px 12px", borderRadius: 8, cursor: "pointer",
                  border: `1px solid ${op.my_vote === 1 ? "#2ea65a" : "var(--border)"}`,
                  background: op.my_vote === 1 ? "rgba(46,166,90,0.16)" : "transparent",
                  color: op.my_vote === 1 ? "#2ea65a" : "var(--muted)" }}>👍 {op.likes}</button>
              <button type="button" onClick={() => vote(op.id, -1)}
                style={{ fontSize: 12, padding: "4px 12px", borderRadius: 8, cursor: "pointer",
                  border: `1px solid ${op.my_vote === -1 ? "#e0504f" : "var(--border)"}`,
                  background: op.my_vote === -1 ? "rgba(224,80,79,0.16)" : "transparent",
                  color: op.my_vote === -1 ? "#e0504f" : "var(--muted)" }}>👎 {op.dislikes}</button>
            </div>
            <div style={{ paddingLeft: 12, borderLeft: "2px solid var(--border)" }}>
              {op.comments.map((c) => (
                <div key={c.id} style={{ fontSize: "12pt", padding: "4px 0", display: "flex", gap: 8, alignItems: "baseline" }}>
                  <span style={{ fontWeight: 600 }}>{c.author}</span>
                  <span style={{ flex: 1, whiteSpace: "pre-wrap" }}>{c.body}</span>
                  <span style={{ color: "var(--muted)", fontSize: 11 }}>{fmt(c.created_at)}</span>
                  {c.is_mine && (
                    <button type="button" onClick={() => removeComment(op.id, c.id)}
                      style={{ fontSize: 11, color: "var(--muted)", background: "none", border: 0, cursor: "pointer", padding: 0 }}>✕</button>
                  )}
                </div>
              ))}
              <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
                <input value={cInput[op.id] || ""}
                  onChange={(e) => setCInput((mm) => ({ ...mm, [op.id]: e.target.value }))}
                  onKeyDown={(e) => { if (e.key === "Enter") addComment(op.id); }}
                  placeholder="댓글 달기…" style={{ flex: 1, fontSize: 12.5, padding: "5px 9px" }} />
                <button type="button" className="ghost sm" onClick={() => addComment(op.id)}
                  style={{ fontSize: 12, padding: "5px 12px" }}>댓글</button>
              </div>
            </div>
          </div>
        );
      })}
    </>
  );
}

// Ratings 요약 — Summary 탭용 컴팩트 리스트. 클릭 시 전체 Ratings 탭으로 이동(onOpen).
export function RatingsSummary({ ticker, name, onOpen }:
  { ticker: string; name: string; onOpen?: () => void }) {
  const [list, setList] = useState<StockOpinion[]>([]);
  const [busy, setBusy] = useState(false);
  const [cur, setCur] = useState<number | null>(null);
  useEffect(() => {
    let alive = true; setBusy(true); setList([]);
    api.opinions(ticker).then((d) => { if (alive) setList(d.opinions); }).catch(() => { /* 무시 */ }).finally(() => { if (alive) setBusy(false); });
    api.symbolDetail(ticker, "1y").then((d) => { if (alive) setCur(d.last?.close ?? null); }).catch(() => { /* 무시 */ });
    return () => { alive = false; };
  }, [ticker]);
  const visible = list.filter((o) => o.status === "approved" || o.is_mine);
  if (busy) return <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>불러오는 중…</p>;
  if (visible.length === 0)
    return <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>등록된 투자의견이 없습니다. Ratings 탭에서 작성할 수 있습니다.</p>;
  return (
    <ul style={{ listStyle: "none", margin: 0, padding: 0 }}>
      {visible.slice(0, 10).map((o) => {
        const m = STANCE_META[o.stance];
        const up = (o.target_price != null && cur && cur > 0) ? (o.target_price / cur - 1) * 100 : null;
        return (
          <li key={o.id} onClick={onOpen}
            style={{ padding: "8px 4px", borderBottom: "1px solid var(--border)", cursor: onOpen ? "pointer" : "default",
              fontSize: "12pt", display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
            {/* 기업명(티커) → 매수 → 제목 → 목표주가 → 상승여력 */}
            <span style={{ fontWeight: 700 }}>{name}({ticker})</span>
            <span style={{ fontSize: 12, fontWeight: 700, padding: "1px 8px", borderRadius: 999, background: m.bg, color: m.color, flexShrink: 0 }}>{m.label}</span>
            <span style={{ flex: 1, minWidth: 100, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{o.title || "(제목 없음)"}</span>
            <span style={{ color: "var(--muted)", flexShrink: 0 }}>목표 {o.target_price != null ? `${o.target_price.toLocaleString()}원` : "—"}</span>
            <span style={{ flexShrink: 0, fontWeight: 700, color: up == null ? "var(--muted)" : up >= 0 ? UP : DOWN }}>
              {up != null ? `${up >= 0 ? "+" : ""}${up.toFixed(1)}%` : "—"}</span>
            {o.status === "pending" && <span style={{ fontSize: 11, color: ACCENT, flexShrink: 0 }}>· 승인 대기</span>}
          </li>
        );
      })}
      {onOpen && (
        <li onClick={onOpen} style={{ padding: "8px 4px", cursor: "pointer", color: "#4f8ff5", fontSize: 12.5, fontWeight: 600 }}>전체 Ratings 보기 →</li>
      )}
    </ul>
  );
}

// 섹터 키워드 뉴스 (Google News RSS — 국내/해외). 클릭 시 원문.
const SECTOR_NEWS_KW = {
  kr: ["2차전지", "양극재", "음극재", "배터리", "ESS", "분리막", "동박", "전지박", "폐배터리", "전고체", "데이터센터"],
  glob: ["EV battery", "cathode", "battery recycling", "solid-state battery"],
};
export function SectorNewsPanel() {
  const [news, setNews] = useState<SectorNews | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"kr" | "global">("kr");
  useEffect(() => {
    let alive = true; setBusy(true);
    // 검색은 토픽 키워드(+산업 기업명) 기준이되, 해시태그 문자열은 화면에 노출하지 않는다.
    api.sectorNews(SECTOR_NEWS_KW.kr, SECTOR_NEWS_KW.glob)
      .then((d) => { if (alive) setNews(d); })
      .catch(() => { /* 무시 */ })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, []);
  const items = tab === "kr" ? (news?.kr || []) : (news?.global || []);
  const tabBtn = (k: "kr" | "global", label: string) => (
    <button type="button" onClick={() => setTab(k)}
      style={{ fontSize: 12, fontWeight: 700, padding: "5px 14px", borderRadius: 8, cursor: "pointer",
        border: `1px solid ${tab === k ? "#4f8ff5" : "var(--border)"}`,
        background: tab === k ? "rgba(79,143,245,0.16)" : "transparent",
        color: tab === k ? "#4f8ff5" : "var(--muted)" }}>{label}</button>
  );
  return (
    <>
      <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>{tabBtn("kr", "국내")}{tabBtn("global", "해외")}</div>
      {busy ? (
        <p style={{ color: "var(--muted)", fontSize: 13 }}>뉴스 불러오는 중…</p>
      ) : items.length === 0 ? (
        <p style={{ color: "var(--muted)", fontSize: 13 }}>관련 뉴스가 없습니다.</p>
      ) : items.map((n, i) => (
        <a key={i} href={n.url} target="_blank" rel="noreferrer"
          style={{ display: "block", padding: "8px 2px", borderBottom: "1px solid var(--border)", textDecoration: "none", color: "inherit" }}>
          <div style={{ fontWeight: 600, fontSize: "12pt", color: DOWN }}>{n.title}</div>
          {n.summary && <div style={{ fontSize: "12pt", color: "var(--muted)", marginTop: 3, lineHeight: 1.5 }}>{n.summary}</div>}
          <div style={{ fontSize: 11, color: "var(--muted)", marginTop: 3 }}>{n.source}{n.date ? ` · ${n.date}` : ""}</div>
        </a>
      ))}
    </>
  );
}

// 개별 종목 주가 추이 차트(캔들 + MA + ±10% + 거래량) — 날짜 선택·휠 줌. HOME·산업분석 공용.
// 주가 추이 기간 버튼 — [라벨, 개월수]
const PCHART_PERIODS: [string, number][] = [
  ["1개월", 1], ["3개월", 3], ["6개월", 6], ["1년", 12], ["3년", 36], ["5년", 60], ["10년", 120],
];
export function CompanyPriceChart({ ticker }: { ticker: string }) {
  const [detail, setDetail] = useState<SymbolDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [vStart, setVStart] = useState("");
  const [vEnd, setVEnd] = useState("");
  const [period, setPeriod] = useState<number | "all" | "custom">(12);   // 기본 1년
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let alive = true; setBusy(true); setVStart(""); setVEnd(""); setPeriod(12);
    api.symbolDetail(ticker, "10y")
      .then((d) => { if (alive) setDetail(d); })
      .catch(() => { if (alive) setDetail(null); })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [ticker]);

  const chartData = (detail?.series || []).map((p) => ({
    ...p,
    hl: p.low != null && p.high != null ? [p.low, p.high] : null,
    up_spike: p.chg_pct != null && p.chg_pct >= 10 ? p.high : null,
    down_spike: p.chg_pct != null && p.chg_pct <= -10 ? p.low : null,
  }));
  const dMin = chartData[0]?.date || "", dMax = chartData[chartData.length - 1]?.date || "";
  // 기간 버튼(1개월~10년)·전체·직접입력/휠줌(custom). dMax 기준 역산.
  const monthsBack = (m: number) => {
    if (!dMax) return dMin;
    const d = new Date(dMax); d.setMonth(d.getMonth() - m);
    const s = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    return s < dMin ? dMin : s;
  };
  const lo = period === "custom" ? (vStart || dMin) : period === "all" ? dMin : monthsBack(period);
  const hi = period === "custom" ? (vEnd || dMax) : dMax;
  const view = chartData.filter((d) => d.date >= lo && d.date <= hi);
  let pmin = Infinity, pmax = -Infinity;
  view.forEach((d) => { [d.low, d.high, d.ma20, d.ma60].forEach((v) => { if (v != null) { if (v < pmin) pmin = v; if (v > pmax) pmax = v; } }); });
  const pDomain: [number, number] = pmin <= pmax ? [Math.floor(pmin * 0.985), Math.ceil(pmax * 1.015)] : [0, 1];

  const wheelState = useRef({ dates: [] as string[], lo: "", hi: "" });
  wheelState.current = { dates: chartData.map((d) => d.date), lo, hi };
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      const { dates, lo: clo, hi: chi } = wheelState.current;
      if (dates.length < 4) return;
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
      let i0 = dates.findIndex((d) => d >= clo); if (i0 < 0) i0 = 0;
      let i1 = dates.length - 1; for (let j = dates.length - 1; j >= 0; j--) { if (dates[j] <= chi) { i1 = j; break; } }
      const w = i1 - i0, anchor = i0 + frac * w;
      const nw = Math.min(dates.length - 1, Math.max(8, Math.round(w * (e.deltaY < 0 ? 0.5 : 2.0))));
      let ns = Math.round(anchor - frac * nw); ns = Math.min(dates.length - 1 - nw, Math.max(0, ns));
      const ne = ns + nw;
      if (ns <= 0 && ne >= dates.length - 1) { setVStart(""); setVEnd(""); setPeriod("all"); }
      else { setVStart(dates[ns]); setVEnd(dates[ne]); setPeriod("custom"); }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [detail]);

  if (busy) return <p style={{ color: "var(--muted)", fontSize: 13 }}>주가 불러오는 중…</p>;
  if (!detail || chartData.length === 0) return <p style={{ color: "var(--muted)", fontSize: 13 }}>주가 데이터가 없습니다.</p>;
  return (
    <>
      <div style={{ display: "flex", justifyContent: "flex-end", gap: 6, alignItems: "center", marginBottom: 6, flexWrap: "wrap" }}>
        {PCHART_PERIODS.map(([lbl, m]) => (
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
      <div ref={box} style={{ touchAction: "none" }}>
        <ResponsiveContainer width="100%" height={300}>
          <ComposedChart data={view} margin={{ top: 5, right: 16, bottom: 5, left: 8 }}>
            <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={48} />
            <YAxis domain={pDomain} tick={{ fontSize: 11 }} width={56} tickFormatter={(v) => `${Math.round(v / 1000)}k`} />
            <Tooltip {...TIP} />
            <Legend wrapperStyle={{ fontSize: 11 }} />
            <Bar dataKey="hl" isAnimationActive={false} legendType="none" shape={Candle} />
            <Line type="monotone" dataKey="ma20" stroke={ACCENT} strokeWidth={1} dot={false} name="MA20" connectNulls />
            <Line type="monotone" dataKey="ma60" stroke={DOWN} strokeWidth={1} dot={false} name="MA60" connectNulls />
            <Scatter dataKey="up_spike" shape={<TriUp />} name="급등 +10%" legendType="triangle" fill={UP} />
            <Scatter dataKey="down_spike" shape={<TriDown />} name="급락 −10%" legendType="triangle" fill={DOWN} />
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </>
  );
}

// 경쟁사 비교(Peer Analysis) — 선택 종목이 속한 밸류체인 단계의 동종 기업 비교. HOME 탭에서 사용.
// 산업 데이터를 자체 로딩하고, 기본 단계 필터 = 현재 종목이 속한 단계. 행 클릭 → 해당 종목으로 HOME 전환.
export function PeerAnalysis({ ticker }: { ticker: string }) {
  const navigate = useNavigate();
  const [companies, setCompanies] = useState<IndustryCompany[]>([]);
  const [busy, setBusy] = useState(false);
  const [fStage, setFStage] = useState<string | null>(null);
  const [fDetail, setFDetail] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<string>("cap");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const toggleSort = (k: string) => {
    if (sortKey === k) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(k); setSortDir("desc"); }
  };

  useEffect(() => {
    setBusy(true);
    api.industryDetail("2차전지")
      .then((d) => {
        setCompanies(d.companies || []);
        api.industryEbitda("2차전지")
          .then((m) => setCompanies((cur) => cur.map((c) => m[c.ticker] ? { ...c, ...m[c.ticker] } : c)))
          .catch(() => { /* 무시 */ });
      })
      .catch(() => { /* 무시 */ })
      .finally(() => setBusy(false));
  }, []);

  // 현재 종목이 속한 단계를 기본 필터로(밸류체인에 없으면 전체)
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => {
    if (companies.length === 0) return;
    const c = companies.find((x) => x.ticker === ticker);
    setFStage(c ? c.stage : "전체");
    setFDetail(null);
  }, [ticker, companies]);

  const ALL = "전체";
  const chip = (active: boolean): React.CSSProperties => ({
    fontSize: 12, padding: "4px 12px",
    background: active ? "rgba(79,143,245,0.16)" : undefined,
    fontWeight: active ? 700 : 400, color: active ? "#1668c4" : undefined,
  });
  const stagesPresent = STAGE_ORDER.filter((st) => companies.some((c) => c.stage === st));
  const detailsOf = (st: string) => [...new Set(companies.filter((c) => c.stage === st).map((c) => c.detail))];
  const filtered = fStage === ALL
    ? companies
    : fStage
      ? companies.filter((c) => c.stage === fStage && (!fDetail || c.detail === fDetail))
      : [];
  const COLS: { key: string; label: string; align: "left" | "right"; num: boolean;
    get: (c: IndustryCompany) => number | string | null }[] = [
    { key: "name", label: "기업 (세부분류)", align: "left", num: false, get: (c) => c.name },
    { key: "product", label: "주요제품", align: "left", num: false, get: (c) => c.product },
    { key: "cap", label: "시가총액", align: "right", num: true, get: (c) => c.cap },
    { key: "ms", label: "M/s", align: "right", num: true, get: (c) => c.ms },
    { key: "chg", label: "등락", align: "right", num: true, get: (c) => c.chg },
    { key: "revenue", label: "매출액", align: "right", num: true, get: (c) => c.revenue },
    { key: "op", label: "영업이익", align: "right", num: true, get: (c) => c.op },
    { key: "op_margin", label: "영업이익률 (%)", align: "right", num: true, get: (c) => c.op_margin },
    { key: "ebitda", label: "EBITDA", align: "right", num: true, get: (c) => c.ebitda },
    { key: "ebitda_margin", label: "EBITDA Margin (%)", align: "right", num: true, get: (c) => c.ebitda_margin },
  ];
  const col = COLS.find((x) => x.key === sortKey);
  const sorted = col ? [...filtered].sort((a, b) => {
    const va = col.get(a), vb = col.get(b);
    let cmp: number;
    if (col.num) cmp = ((va as number) ?? -Infinity) - ((vb as number) ?? -Infinity);
    else cmp = String(va ?? "").localeCompare(String(vb ?? ""), "ko");
    return sortDir === "asc" ? cmp : -cmp;
  }) : filtered;
  // 선택(분석 중) 종목은 정렬과 무관하게 최상단 고정
  const ordered = [
    ...sorted.filter((c) => c.ticker === ticker),
    ...sorted.filter((c) => c.ticker !== ticker),
  ];

  if (busy && companies.length === 0)
    return <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>경쟁사 데이터 불러오는 중…</p>;

  return (
    <>
      <div style={{ marginBottom: 10 }}>
        {/* 단계(대분류) 칩 */}
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
          <button type="button" className="ghost sm"
            onClick={() => { setFStage(ALL); setFDetail(null); }}
            style={chip(fStage === ALL)}>전체</button>
          {stagesPresent.map((st) => (
            <button key={st} type="button" className="ghost sm"
              onClick={() => { setFStage(st === fStage ? null : st); setFDetail(null); }}
              style={chip(fStage === st)}>{st}</button>
          ))}
        </div>
        {/* 선택 단계의 세부분류 — 단계 아래에 들여쓰기 + 좌측 라인으로 '소재의 하위 항목'임을 표시 */}
        {fStage && fStage !== ALL && detailsOf(fStage).length > 1 && (
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center",
            marginTop: 7, marginLeft: 12, paddingLeft: 12, borderLeft: "2px solid var(--accent)" }}>
            <span style={{ color: "var(--muted)", fontSize: 11.5, fontWeight: 600 }}>↳ {fStage} 세부분류</span>
            <button type="button" className="ghost sm" onClick={() => setFDetail(null)} style={chip(!fDetail)}>전체</button>
            {detailsOf(fStage).map((d) => (
              <button key={d} type="button" className="ghost sm"
                onClick={() => setFDetail(d === fDetail ? null : d)} style={chip(fDetail === d)}>{d}</button>
            ))}
          </div>
        )}
      </div>
      {!fStage ? (
        <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>
          단계(원자재·소재·배터리 …)를 선택하면 동종 기업이 표시됩니다. 행을 클릭하면 해당 기업으로 전환됩니다.
        </p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, whiteSpace: "nowrap" }}>
          <thead>
            <tr style={{ borderBottom: "2px solid #e3e8ef" }}>
              {COLS.map((cl) => (
                <th key={cl.key} onClick={() => toggleSort(cl.key)}
                  style={{ padding: "7px 6px", textAlign: cl.align, cursor: "pointer",
                    userSelect: "none", whiteSpace: "nowrap",
                    color: sortKey === cl.key ? "#4f8ff5" : undefined }}>
                  {cl.label}{sortKey === cl.key ? (sortDir === "asc" ? " ▲" : " ▼") : " ⇅"}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ordered.map((c) => {
              const isSel = ticker === c.ticker;
              return (
              <tr key={c.ticker} onClick={() => navigate(`/dashboard?symbol=${c.ticker}`)}
                style={{ borderBottom: "1px solid var(--border)", cursor: "pointer",
                  background: isSel ? "rgba(196,152,43,0.16)" : undefined,
                  boxShadow: isSel ? "inset 3px 0 0 var(--accent)" : undefined }}>
                <td style={{ padding: "6px", fontWeight: 600 }}>{c.name}
                  {isSel && <span style={{ color: "var(--accent)", fontSize: 10, fontWeight: 700, marginLeft: 4 }}>● 분석 중</span>}
                  <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 11 }}> {c.ticker} · {c.detail}</span></td>
                <td style={{ padding: "6px", color: "var(--muted)", fontSize: 12 }}>{c.product}</td>
                <td style={{ padding: "6px", textAlign: "right" }}>{eok(c.cap)}</td>
                <td style={{ padding: "6px", textAlign: "right", color: "var(--muted)" }}>{c.ms != null ? `${c.ms.toFixed(1)}%` : "—"}</td>
                <td style={{ padding: "6px", textAlign: "right", color: c.chg == null ? "var(--muted)" : c.chg >= 0 ? UP : DOWN }}>{pct(c.chg, 1)}</td>
                <td style={{ padding: "6px", textAlign: "right" }}>{eok(c.revenue)}</td>
                <td style={{ padding: "6px", textAlign: "right", color: c.op != null && c.op < 0 ? DOWN : "inherit" }}>{eok(c.op)}</td>
                <td style={{ padding: "6px", textAlign: "right", color: c.op_margin != null && c.op_margin < 0 ? DOWN : "inherit" }}>{c.op_margin != null ? `${c.op_margin.toFixed(1)}%` : "—"}</td>
                <td style={{ padding: "6px", textAlign: "right", color: c.ebitda != null && c.ebitda < 0 ? DOWN : "inherit" }}>{eok(c.ebitda)}</td>
                <td style={{ padding: "6px", textAlign: "right", color: c.ebitda_margin != null && c.ebitda_margin < 0 ? DOWN : "inherit" }}>{c.ebitda_margin != null ? `${c.ebitda_margin.toFixed(1)}%` : "—"}</td>
              </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}

export default function IndustryAnalysis() {
  const navigate = useNavigate();
  const [data, setData] = useState<IndustryData | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [asOfReq, setAsOfReq] = useState("");   // 트리맵 기준일(yyyy-mm-dd, ""=최신 거래일)
  const [refreshing, setRefreshing] = useState(false);
  const [searchQ, setSearchQ] = useState("");   // 트리맵 기업 검색어

  // EBITDA 지연 로딩 — 트리맵·표가 먼저 뜬 뒤 FnGuide D&A를 받아 표에 병합.
  const mergeEbitda = () => {
    api.industryEbitda("2차전지")
      .then((m) => setData((cur) => cur
        ? { ...cur, companies: cur.companies.map((c) => m[c.ticker] ? { ...c, ...m[c.ticker] } : c) }
        : cur))
      .catch(() => { /* 무시 */ });
  };

  useEffect(() => {
    setBusy(true);
    api.industryDetail("2차전지", asOfReq || undefined)
      .then((d) => { setData(d); setErr(""); mergeEbitda(); })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setBusy(false));
  }, [asOfReq]);

  // 새로고침 — 서버 주가 캐시를 비우고 실시간 시세로 재조회
  const doRefresh = () => {
    setRefreshing(true);
    api.industryDetail("2차전지", asOfReq || undefined, true)
      .then((d) => { setData(d); setErr(""); mergeEbitda(); })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setRefreshing(false));
  };

  const companies = data?.companies || [];
  // 트리맵·검색에서 기업 선택 → HOME(개별 기업 분석)으로 이동
  const pickCompany = (ticker: string) => navigate(`/dashboard?symbol=${ticker}`);
  // 제목 표기 — 밸류체인(트리맵 표시분=Downstream 제외) 시총 합계. 기준일은 달력 입력으로.
  const tmTotal = companies
    .filter((c) => c.gu !== "Downstream" && c.cap && c.cap > 0)
    .reduce((s, c) => s + (c.cap || 0), 0);
  const totalAmt = tmTotal > 0 ? `${(tmTotal / 1e12).toFixed(1)}조` : "";   // '원' 제거
  const maxDate = new Date().toISOString().slice(0, 10);   // 미래 일자 선택 방지

  return (
    <div className="dashboard-fullwidth industry-67">
      <h1>Industry Analysis</h1>

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
          <div className="panel" style={{ position: "relative" }}>
            <h3 style={{ marginTop: 0, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span>EV/Battery Value-chain{totalAmt && " - "}
                {totalAmt && <span style={{ color: "var(--accent)" }}>{totalAmt}</span>}</span>
              <span style={{ fontWeight: 400, fontSize: "16pt", color: "var(--muted)" }}>as of</span>
              <input type="date" value={asOfReq || (data?.as_of || "")} max={maxDate}
                onChange={(e) => setAsOfReq(e.target.value)}
                title="다른 일자의 트리맵 보기" style={{ fontSize: "16pt", padding: "3px 8px", fontWeight: 600 }} />
              {asOfReq && (
                <button type="button" className="ghost sm" onClick={() => setAsOfReq("")}
                  style={{ fontSize: 11, padding: "2px 8px" }}>최신</button>
              )}
              {busy && <span style={{ fontWeight: 400, fontSize: 12, color: "var(--muted)" }}>불러오는 중…</span>}
            </h3>
            {/* 우상단 새로고침 — 클릭 시 주가 캐시 비우고 실시간 시세로 갱신 */}
            <button type="button" className="ghost sm" onClick={doRefresh} disabled={refreshing}
              title="주가 실시간 새로고침"
              style={{ position: "absolute", top: 14, right: 16, display: "inline-flex",
                alignItems: "center", gap: 6, fontSize: 12, padding: "5px 11px", zIndex: 5 }}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
                style={{ animation: refreshing ? "spin 0.8s linear infinite" : undefined }}>
                <path d="M21 2v6h-6" /><path d="M3 12a9 9 0 0 1 15-6.7L21 8" />
                <path d="M3 22v-6h6" /><path d="M21 12a9 9 0 0 1-15 6.7L3 16" />
              </svg>
              {refreshing ? "갱신 중…" : "새로고침"}
            </button>
            {/* 트리맵 기업 검색 — 선택 시 트리맵 클릭과 동일하게 업데이트(pickCompany) */}
            <div style={{ position: "relative", maxWidth: 380, margin: "0 0 10px" }}>
              <input value={searchQ} onChange={(e) => setSearchQ(e.target.value)}
                placeholder="🔍  이 산업 내 기업 검색 (기업명 · 종목코드)"
                style={{ width: "100%", fontSize: 13, padding: "8px 11px" }} />
              {searchQ.trim() && (() => {
                const q = searchQ.trim().toLowerCase();
                const matches = companies
                  .filter((c) => c.name.toLowerCase().includes(q) || c.ticker.includes(q))
                  .slice(0, 8);
                return (
                  <ul style={{ position: "absolute", top: "100%", left: 0, right: 0, zIndex: 30,
                    listStyle: "none", margin: "4px 0 0", padding: 4, background: "var(--panel)",
                    border: "1px solid var(--border)", borderRadius: 8,
                    boxShadow: "0 8px 24px rgba(0,0,0,0.35)", maxHeight: 320, overflowY: "auto" }}>
                    {matches.length === 0 ? (
                      <li style={{ padding: "8px 10px", color: "var(--muted)", fontSize: 13 }}>일치하는 기업이 없습니다.</li>
                    ) : matches.map((c) => (
                      <li key={c.ticker}
                        onMouseDown={() => { pickCompany(c.ticker); setSearchQ(""); }}
                        style={{ padding: "8px 10px", cursor: "pointer", fontSize: 13, borderRadius: 6,
                          display: "flex", justifyContent: "space-between", gap: 10 }}
                        onMouseEnter={(e) => (e.currentTarget.style.background = "var(--accent-soft)")}
                        onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
                        <span style={{ fontWeight: 600 }}>{c.name}</span>
                        <span style={{ color: "var(--muted)" }}>{c.ticker} · {c.detail}</span>
                      </li>
                    ))}
                  </ul>
                );
              })()}
            </div>
            <IndustryTreemap companies={companies} onPick={pickCompany} />
            {/* 증감률별 셀 색상 범례 — 트리맵 우측 하단 */}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 0, marginTop: 6 }}>
              {[-3, -2, -1, 0, 1, 2, 3].map((v) => (
                <span key={v} style={{ background: colorByChg(v), color: "#fff",
                  fontSize: 14, fontWeight: 700, padding: "5px 18px" }}>{v > 0 ? "+" : ""}{v}%</span>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

import { useState, useEffect, useRef } from "react";
import {
  ComposedChart, Bar, Line, Scatter, XAxis, YAxis, Tooltip, ResponsiveContainer,
  CartesianGrid, Legend,
} from "recharts";
import Plotly from "plotly.js-dist-min";
import { api } from "../api";
import type { IndustryData, IndustryCompany, SymbolDetail, SymbolPoint, KrExtras,
  StockOpinion, OpinionStance } from "../types";

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
function CompanyReport({ ticker, company }: { ticker: string; company?: IndustryCompany }) {
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
    v == null || isNaN(v) ? "—" : `${v.toFixed(1)}배`;
  const EARN_ROWS = ["매출액", "영업이익", "당기순이익", "지배주주"];
  const MULT_ROWS: [string, string][] = [["P/E", "PER"], ["P/B", "PBR"]];   // 표시명 → FnGuide 행 키
  const earnings = kr?.earnings;
  const hasEarnings = !!earnings && earnings.years.length > 0 && EARN_ROWS.some((r) => earnings.rows[r]);
  const reports = kr?.reports || [];
  const targetByBroker = new Map((kr?.consensus || []).map((c) => [c.broker, c.target] as const));
  // 포워드 EV/EBITDA = 시가총액 / (영업이익ᴱ + 최근 D&A). 순차입금은 무료 소스 한계로 미반영(EV≈시총).
  const capEok = company?.cap != null ? company.cap / 1e8 : null;   // 억
  const daEok = company?.da != null ? company.da / 1e8 : null;      // 억
  const opRow = earnings?.rows["영업이익"];
  const evEbitdaAt = (i: number): number | null => {
    if (capEok == null || daEok == null || !opRow) return null;
    const op = opRow[i];
    if (op == null) return null;
    const e = op + daEok;
    return e > 0 ? capEok / e : null;
  };
  const hasMult = !!earnings && (!!earnings.rows["PER"] || !!earnings.rows["PBR"] || (capEok != null && daEok != null));

  if (busy) return <p style={{ color: "var(--muted)", fontSize: 13 }}>리포트 불러오는 중…</p>;

  return (
    <>
      {/* 추정 실적 — 개별종목분석과 동일 */}
      {hasEarnings ? (
        <div style={{ overflowX: "auto" }}>
          <div style={{ fontWeight: 700, fontSize: 13, margin: "0 0 6px" }}>추정 실적 (연결 · 단위 억원)</div>
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
              {EARN_ROWS.filter((r) => earnings!.rows[r]).map((r) => (
                <tr key={r} style={{ borderBottom: "1px solid var(--border)" }}>
                  <td style={{ padding: "6px", fontWeight: 600 }}>{r}</td>
                  {earnings!.rows[r].map((v, j) => (
                    <td key={j} style={{ padding: "6px", textAlign: "right",
                      color: earnings!.years[j]?.includes("(E)") ? ACCENT : "inherit" }}>{jo(v)}</td>
                  ))}
                </tr>
              ))}
              {hasMult && (
                <>
                  <tr>
                    <td colSpan={earnings!.years.length + 1}
                      style={{ padding: "6px", fontSize: 11, fontWeight: 700, color: "var(--muted)",
                        borderTop: "2px solid var(--border)" }}>Multiple (배 · forward 포함)</td>
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

      {/* 애널리스트 리포트 — 제목 클릭 시 원문(PDF·네이버) 다운로드 */}
      <div style={{ fontWeight: 700, fontSize: 13, margin: "16px 0 6px" }}>애널리스트 리포트</div>
      {reports.length > 0 ? (<>
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {reports.map((r, i) => {
            const tgt = r.target ?? targetByBroker.get(r.broker);   // 리포트 고유 목표가 우선
            return (
              <li key={i} style={{ padding: "7px 0", borderBottom: "1px solid var(--border)",
                fontSize: 13, display: "flex", justifyContent: "space-between", gap: 10 }}>
                <div style={{ minWidth: 0 }}>
                  <a href={r.url} target="_blank" rel="noreferrer"
                    style={{ fontWeight: 600, color: DOWN, textDecoration: "none" }}>{r.title}</a>
                  <div style={{ color: "var(--muted)", fontSize: 12, marginTop: 2 }}>{r.broker} · {r.date}</div>
                </div>
                <div style={{ textAlign: "right", flexShrink: 0 }}>
                  <div style={{ color: "var(--muted)", fontSize: 11 }}>목표주가</div>
                  <div style={{ fontWeight: 700, fontSize: 13 }}>{tgt != null ? tgt.toLocaleString() + "원" : "—"}</div>
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

// 개별 기업 투자의견 게시판 — 매수/중립/매도 + 분석글 + 댓글 + 좋아요/싫어요(로그인 회원).
function OpinionBoard({ ticker, name }: { ticker: string; name: string }) {
  const [list, setList] = useState<StockOpinion[]>([]);
  const [busy, setBusy] = useState(false);
  const [stance, setStance] = useState<OpinionStance>("buy");
  const [body, setBody] = useState("");
  const [posting, setPosting] = useState(false);
  const [cInput, setCInput] = useState<Record<number, string>>({});

  useEffect(() => {
    let alive = true;
    setBusy(true); setList([]);
    api.opinions(ticker)
      .then((d) => { if (alive) setList(d.opinions); })
      .catch(() => { if (alive) setList([]); })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [ticker]);

  const submit = () => {
    const text = body.trim();
    if (!text || posting) return;
    setPosting(true);
    api.createOpinion(ticker, stance, text)
      .then((op) => { setList((l) => [op, ...l]); setBody(""); })
      .catch((e) => alert((e as Error).message))
      .finally(() => setPosting(false));
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

  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <h3 style={{ marginTop: 0 }}>투자의견 게시판
        <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 13 }}> · {name} {ticker}</span>
      </h3>

      {/* 작성 (로그인 회원) */}
      <div style={{ border: "1px solid var(--border)", borderRadius: 10, padding: 12, marginBottom: 14 }}>
        <div style={{ display: "flex", gap: 6, marginBottom: 8 }}>
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
        </div>
        <textarea value={body} onChange={(e) => setBody(e.target.value)} rows={3}
          placeholder={`${name}에 대한 투자 의견과 근거를 작성하세요`}
          style={{ width: "100%", fontSize: 13, padding: "8px 10px", resize: "vertical" }} />
        <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 8 }}>
          <button type="button" onClick={submit} disabled={!body.trim() || posting}
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
        return (
          <div key={op.id} style={{ borderTop: "1px solid var(--border)", padding: "12px 2px" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
              <span style={{ fontSize: 12, fontWeight: 700, padding: "2px 10px", borderRadius: 999, background: m.bg, color: m.color }}>{m.label}</span>
              <span style={{ fontWeight: 600, fontSize: 13 }}>{op.author}</span>
              <span style={{ color: "var(--muted)", fontSize: 12 }}>{fmt(op.created_at)}</span>
              {op.is_mine && (
                <button type="button" className="ghost sm" onClick={() => removeOpinion(op.id)}
                  style={{ marginLeft: "auto", fontSize: 11, padding: "2px 8px" }}>삭제</button>
              )}
            </div>
            <div style={{ fontSize: 13, lineHeight: 1.6, whiteSpace: "pre-wrap", marginBottom: 8 }}>{op.body}</div>
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
            {/* 댓글 */}
            <div style={{ paddingLeft: 12, borderLeft: "2px solid var(--border)" }}>
              {op.comments.map((c) => (
                <div key={c.id} style={{ fontSize: 12.5, padding: "4px 0", display: "flex", gap: 8, alignItems: "baseline" }}>
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
    </div>
  );
}

export default function IndustryAnalysis() {
  const [data, setData] = useState<IndustryData | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [detail, setDetail] = useState<SymbolDetail | null>(null);
  const [vStart, setVStart] = useState("");   // 주가차트 시작일(yyyy-mm-dd, ""=데이터 시작)
  const [vEnd, setVEnd] = useState("");        // 종료일(""=데이터 끝)
  const chartBox = useRef<HTMLDivElement>(null);   // 휠 줌 컨테이너
  const [dBusy, setDBusy] = useState(false);
  const [fStage, setFStage] = useState<string | null>(null);   // 기업 찾기 — 단계 필터
  const [fDetail, setFDetail] = useState<string | null>(null); // 세부분류 필터
  const [asOfReq, setAsOfReq] = useState("");   // 트리맵 기준일(yyyy-mm-dd, ""=최신 거래일)
  const [refreshing, setRefreshing] = useState(false);
  const [searchQ, setSearchQ] = useState("");   // 트리맵 기업 검색어

  useEffect(() => {
    setBusy(true);
    api.industryDetail("2차전지", asOfReq || undefined)
      .then((d) => { setData(d); setErr(""); })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setBusy(false));
  }, [asOfReq]);

  // 새로고침 — 서버 주가 캐시를 비우고 실시간 시세로 재조회
  const doRefresh = () => {
    setRefreshing(true);
    api.industryDetail("2차전지", asOfReq || undefined, true)
      .then((d) => { setData(d); setErr(""); })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setRefreshing(false));
  };

  // 기업 선택 시 주가 상세 fetch
  useEffect(() => {
    if (!sel) { setDetail(null); return; }
    let alive = true;
    setDBusy(true);
    setVStart(""); setVEnd("");                 // 종목 바뀌면 전체 기간으로 리셋
    api.symbolDetail(sel, "10y")                // 넉넉히 받아 클라이언트에서 날짜 필터/휠 줌
      .then((d) => { if (alive) setDetail(d); })
      .catch(() => { if (alive) setDetail(null); })
      .finally(() => { if (alive) setDBusy(false); });
    return () => { alive = false; };
  }, [sel]);

  const companies = data?.companies || [];
  const nameOf = (t: string) => companies.find((c) => c.ticker === t)?.name || t;
  // 기업 선택 — 주가/리포트와 함께 좌측 Company Analysis 표도 해당 단계·세부분류로 자동 필터
  const pickCompany = (ticker: string) => {
    setSel(ticker);
    const c = companies.find((x) => x.ticker === ticker);
    if (c) { setFStage(c.stage); setFDetail(c.detail); }
  };
  const [sortKey, setSortKey] = useState<string>("cap");   // Company Analysis 표 정렬
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const toggleSort = (k: string) => {
    if (sortKey === k) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(k); setSortDir("desc"); }
  };
  // 제목 표기 — 밸류체인(트리맵 표시분=Downstream 제외) 시총 합계. 기준일은 달력 입력으로.
  const tmTotal = companies
    .filter((c) => c.gu !== "Downstream" && c.cap && c.cap > 0)
    .reduce((s, c) => s + (c.cap || 0), 0);
  const totalStr = tmTotal > 0 ? ` - ${(tmTotal / 1e12).toFixed(1)}조원` : "";
  const maxDate = new Date().toISOString().slice(0, 10);   // 미래 일자 선택 방지

  // 개별 종목 차트 데이터 (캔들 + ±10% + 거래량)
  const chartData = (detail?.series || []).map((p) => ({
    ...p,
    hl: p.low != null && p.high != null ? [p.low, p.high] : null,
    up_spike: p.chg_pct != null && p.chg_pct >= 10 ? p.high : null,
    down_spike: p.chg_pct != null && p.chg_pct <= -10 ? p.low : null,
  }));
  // 보기 기간 필터 — 날짜 직접 설정 + 휠 줌으로 [vStart, vEnd] 조절
  const dMin = chartData[0]?.date || "", dMax = chartData[chartData.length - 1]?.date || "";
  const lo = vStart || dMin, hi = vEnd || dMax;
  const view = chartData.filter((d) => d.date >= lo && d.date <= hi);
  let pmin = Infinity, pmax = -Infinity;
  view.forEach((d) => {
    [d.low, d.high, d.ma20, d.ma60].forEach((v) => {
      if (v != null) { if (v < pmin) pmin = v; if (v > pmax) pmax = v; }
    });
  });
  const pDomain: [number, number] = pmin <= pmax ? [Math.floor(pmin * 0.985), Math.ceil(pmax * 1.015)] : [0, 1];

  // 주가차트 휠 줌 — 차트 위 휠 ↑확대/↓축소(커서 기준). 보기 기간(vStart/vEnd) 조절.
  const wheelState = useRef({ dates: [] as string[], lo: "", hi: "" });
  wheelState.current = { dates: chartData.map((d) => d.date), lo, hi };
  useEffect(() => {
    const el = chartBox.current;
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
      const nw = Math.min(dates.length - 1, Math.max(8, Math.round(w * (e.deltaY < 0 ? 0.8 : 1.25))));
      let ns = Math.round(anchor - frac * nw); ns = Math.min(dates.length - 1 - nw, Math.max(0, ns));
      const ne = ns + nw;
      if (ns <= 0 && ne >= dates.length - 1) { setVStart(""); setVEnd(""); }
      else { setVStart(dates[ns]); setVEnd(dates[ne]); }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [detail]);   // 차트는 종목 선택 후 마운트 → detail 로드 시 리스너 재부착(초기 mount 시 chartBox=null 버그 수정)

  return (
    <div className="dashboard-fullwidth">
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
              <span>EV/Battery Value-chain{totalStr}</span>
              <span style={{ fontWeight: 400, fontSize: 13, color: "var(--muted)" }}>as of</span>
              <input type="date" value={asOfReq || (data?.as_of || "")} max={maxDate}
                onChange={(e) => setAsOfReq(e.target.value)}
                title="다른 일자의 트리맵 보기" style={{ fontSize: 13, padding: "2px 6px", fontWeight: 600 }} />
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

          {/* 개별 종목 주가 차트 (클릭 시) */}
          {sel && (
            <div className="panel" style={{ marginTop: 16 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
                <h3 style={{ margin: 0 }}>{nameOf(sel)} <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 13 }}>{sel}</span> · 주가 추이 (캔들 · 급등락 ±10%)</h3>
                <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                  <input type="date" value={vStart || dMin} min={dMin} max={dMax}
                    onChange={(e) => setVStart(e.target.value)}
                    style={{ fontSize: 12, padding: "2px 6px" }} aria-label="시작일" />
                  <span style={{ color: "var(--muted)", fontSize: 12 }}>~</span>
                  <input type="date" value={vEnd || dMax} min={dMin} max={dMax}
                    onChange={(e) => setVEnd(e.target.value)}
                    style={{ fontSize: 12, padding: "2px 6px" }} aria-label="종료일" />
                  <button type="button" className="ghost sm" onClick={() => { setVStart(""); setVEnd(""); }}
                    style={{ fontSize: 12, padding: "3px 10px" }}>전체</button>
                  <button type="button" className="ghost sm" onClick={() => setSel(null)}
                    style={{ fontSize: 12, padding: "3px 10px" }}>✕ 닫기</button>
                </div>
              </div>
              {dBusy && <p style={{ color: "var(--muted)", fontSize: 13 }}>주가 불러오는 중…</p>}
              {detail && chartData.length > 0 && (
                <>
                  <div ref={chartBox} style={{ touchAction: "none" }}>
                  <ResponsiveContainer width="100%" height={360}>
                    <ComposedChart data={view} margin={{ top: 5, right: 16, bottom: 5, left: 8 }}>
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
                    <ComposedChart data={view} margin={{ top: 5, right: 16, bottom: 5, left: 8 }}>
                      <CartesianGrid stroke="#e3e8ef" strokeDasharray="3 3" />
                      <XAxis dataKey="date" tick={{ fontSize: 10 }} minTickGap={48} />
                      <YAxis tick={{ fontSize: 10 }} width={56}
                        tickFormatter={(v) => v >= 1e6 ? `${(v / 1e6).toFixed(0)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(0)}k` : String(v)} />
                      <Tooltip {...TIP} formatter={(v) => Number(v).toLocaleString()} />
                      <Bar dataKey="volume" name="거래량" fill="#9aa6b8" isAnimationActive={false} />
                    </ComposedChart>
                  </ResponsiveContainer>
                  </div>
                </>
              )}
            </div>
          )}

          {/* Company Analysis(좌·절반) + 선택종목 기업분석 리포트(우·절반) */}
          <div className="ca-grid2" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginTop: 16, alignItems: "stretch" }}>
            <div className="panel" style={{ overflowX: "auto", marginBottom: 0 }}>
            <h3 style={{ marginTop: 0 }}>경쟁사 비교</h3>
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
              // 정렬 가능한 컬럼 정의 (헤더 클릭 → 오름/내림). 주요제품을 기업 바로 우측에 배치.
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
                        {sorted.map((c) => (
                          <tr key={c.ticker} onClick={() => setSel(c.ticker)}
                            style={{ borderBottom: "1px solid var(--border)", cursor: "pointer",
                              background: sel === c.ticker ? "rgba(79,143,245,0.1)" : undefined }}>
                            <td style={{ padding: "6px", fontWeight: 600 }}>{c.name}
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
                        ))}
                      </tbody>
                    </table>
                  )}
                </>
              );
            })()}
            </div>
            <div className="panel" style={{ overflowX: "auto", marginBottom: 0, minHeight: 560 }}>
              <h3 style={{ marginTop: 0 }}>{sel ? `${nameOf(sel)} 기업분석 리포트` : "기업분석 리포트"}</h3>
              {sel
                ? <CompanyReport ticker={sel} company={companies.find((c) => c.ticker === sel)} />
                : <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>좌측 표에서 기업을 클릭하면 추정 실적·애널리스트 리포트가 표시됩니다.</p>}
            </div>
          </div>

          {/* 개별 기업 투자의견 게시판 — 선택 종목별 */}
          {sel
            ? <OpinionBoard ticker={sel} name={nameOf(sel)} />
            : <div className="panel" style={{ marginTop: 16 }}>
                <h3 style={{ marginTop: 0 }}>투자의견 게시판</h3>
                <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>
                  위 트리맵·검색·표에서 기업을 선택하면 해당 기업의 투자의견 게시판이 표시됩니다.
                </p>
              </div>}
        </>
      )}
    </div>
  );
}

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
// 스트림(구분)별 프레임 색 — 동일 색조에서 명도/채도 차등(Mid=기준, Up=옅게, Down으로 갈수록 진하게).
const STREAM_FRAME: Record<string, string> = { Upstream: "#4a525f", Midstream: "#363c45", Downstream: "#222730" };
const frameOf = (gu: string) => STREAM_FRAME[gu] || FRAME;
function colorByChg(chg: number | null): string {
  if (chg == null) return "#2b2f38";
  const t = Math.max(-1, Math.min(1, chg / 3));
  const gray = [0x3d, 0x46, 0x54];                                // 0% 중립 = 슬레이트(finviz 띠 #3d4654)
  if (t === 0) return `rgb(${gray.join(",")})`;
  const tgt = t > 0 ? [0x2e, 0xcc, 0x5a] : [0xe5, 0x48, 0x48];    // +3 초록(#2ecc5a) / -3 빨강(#e54848)
  const f = 0.25 + 0.75 * Math.abs(t);
  return `rgb(${gray.map((v, i) => Math.round(v + (tgt[i] - v) * f)).join(",")})`;
}

const GU_ORDER = ["Upstream", "Midstream", "Downstream"];
const ROOT_ID = "2차전지";
// 산업 탭 상위 구분 — WICS 대분류 기반, 사용자 요청 반영. 석유화학·2차전지는 상위탭 분리,
// 소비재(필수/경기)는 한 대분류로 묶고 하위 분류로 표시. 없는 대분류도 '준비 중'으로 노출.
// 대분류는 items(직접 산업) 또는 subgroups(하위분류별 산업) 중 하나를 가짐.
type IndustryGroup = { label: string; items?: string[]; subgroups?: { label: string; items: string[] }[] };
const INDUSTRY_GROUPS: IndustryGroup[] = [
  { label: "석유화학", items: ["석유화학"] },
  { label: "2차전지", items: ["2차전지"] },
  { label: "반도체.IT", items: ["반도체", "전자부품"] },
  { label: "산업재", items: ["건설"] },
  { label: "소비재", subgroups: [
    { label: "필수소비재", items: ["화장품"] },
    { label: "경기소비재", items: ["교육출판업"] },
  ] },
  { label: "금융", items: ["금융"] },
  { label: "건강기능식품", items: [] },
  { label: "미디어 엔터테인먼트", items: ["미디어엔터테인먼트"] },
  { label: "에너지", items: [] },
  { label: "소재", items: [] },
  { label: "유틸리티", items: [] },
];
// 대분류가 직접/하위 통틀어 보유한 산업 목록(평탄화).
const groupIndustries = (g: IndustryGroup): string[] =>
  g.items ?? (g.subgroups ?? []).flatMap((s) => s.items);
// 트리맵 타일 표시 크기 보정 — 초대형주가 화면을 가려 다른 종목이 안 보이는 문제 방지.
// 표시 크기만 줄이고 툴팁·시총·M/S는 실제값 유지. ref="maxOther"=보정대상 제외 최대 시총,
// ref=티커=그 종목 시총. 보정 표시값 = 기준 × factor (시총이 변해도 데이터에서 자동 계산).
const TM_CAP: Record<string, { ref: string; factor: number }> = {
  "005930": { ref: "maxOther", factor: 1.2 },  // 삼성전자 = 최대 비초대형주 × 1.2
  "000660": { ref: "maxOther", factor: 1.2 },  // SK하이닉스 = 최대 비초대형주 × 1.2
  "009150": { ref: "maxOther", factor: 1.3 },  // 삼성전기 = 최대 비초대형주 × 1.3 (반도체 삼성전자 수준 보정)
  "011070": { ref: "maxOther", factor: 1.15 }, // LG이노텍 = 최대 비초대형주 × 1.15
};

type PlotlyEvt = { points?: { id?: string; customdata?: (string | number)[] }[]; event?: MouseEvent };

// 포트폴리오 대시보드(go.Treemap) 그대로 이식 — Plotly.js 트리맵.
// ROOT→구분→단계→세부분류→기업, 프레임 색(#5b6b7f/#6c7a8c/#7e8a9a)·pathbar 드릴 동일.
// 기업 박스 클릭=주가 / 그룹 클릭=확대, 같은 그룹 재클릭=원래대로. 세부분류·기업 호버=경쟁사 수익률.
function IndustryTreemap({ companies, onPick, rootId = ROOT_ID }:
  { companies: IndustryCompany[]; onPick: (t: string) => void; rootId?: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const levelRef = useRef<string>(rootId);
  const pickRef = useRef(onPick); pickRef.current = onPick;
  const [hover, setHover] = useState<{ stage: string; detail: string; x: number; y: number } | null>(null);

  useEffect(() => {
    const el = ref.current;
    // 2차전지만 완성차(Downstream) 시총이 너무 커 배터리 소형주를 가려 트리맵에서 제외.
    // 다른 산업은 Downstream(반도체 후공정·기판, 전자부품 카메라모듈, 금융 카드·핀테크 등)도 포함.
    const valid = companies.filter((c) => c.cap && c.cap > 0
      && !(rootId === "2차전지" && c.gu === "Downstream"));
    if (!el || valid.length === 0) return;
    // 타일 표시 크기 보정값(TM_CAP) 계산 — 실제 시총은 보존, 표시 크기만 제한.
    const capTickers = new Set(Object.keys(TM_CAP));
    const maxOther = Math.max(0, ...valid.filter((c) => !capTickers.has(c.ticker)).map((c) => c.cap || 0));
    const capById = new Map(valid.map((c) => [c.ticker, c.cap || 0]));
    const sizeOf = (c: IndustryCompany): number => {
      const cap = c.cap || 0;
      const rule = TM_CAP[c.ticker];
      if (!rule) return cap;
      const base = rule.ref === "maxOther" ? maxOther : (capById.get(rule.ref) || 0);
      const s = base * rule.factor;
      return s > 0 ? s : cap;   // 기준이 없으면(폴백) 실제 시총
    };

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
    add(rootId, rootId, "", 0, FRAME, "", [sumT(valid), "", "", ""]);
    for (const gu of GU_ORDER) {
      const inGu = valid.filter((c) => c.gu === gu);
      if (!inGu.length) continue;
      const gid = `${rootId}/${gu}`;
      add(gid, gu, rootId, 0, frameOf(gu), "", [sumT(inGu), "", "", ""]);
      // 단계는 산업마다 달라 하드코딩 목록 대신 **데이터에서** 추출(백엔드가 이미 단계순 정렬).
      const stagesInGu = [...new Set(inGu.map((c) => c.stage))];
      for (const dan of stagesInGu) {
        const inDan = inGu.filter((c) => c.stage === dan);
        if (!inDan.length) continue;
        const did = `${gid}/${dan}`;
        add(did, dan, gid, 0, frameOf(gu), "", [sumT(inDan), "", "", ""]);
        for (const det of [...new Set(inDan.map((c) => c.detail))]) {
          const inDet = inDan.filter((c) => c.detail === det);
          const dtid = `${did}/${det}`;
          // 소분류(세부분류) 헤더 색 = 구성 기업 시총가중 평균 등락색 (셀 색 비율 반영)
          const wcap = inDet.reduce((s, c) => s + (c.cap || 0), 0);
          const wchg = wcap ? inDet.reduce((s, c) => s + (c.chg || 0) * (c.cap || 0), 0) / wcap : 0;
          add(dtid, det, did, 0, colorByChg(wchg), "", [sumT(inDet), "", dan, det]);
          for (const c of inDet) {
            const cap = c.cap || 0;
            // 초대형주는 표시 크기를 TM_CAP 규칙으로 제한(툴팁·M/S는 실제값).
            const sizeVal = sizeOf(c);
            const lsize = Math.min(22, Math.max(12, Math.round((Math.log10(cap || 1) - 11) * 5 + 12)));
            add(`${dtid}/${c.ticker}`, `<b>${c.name}</b>`, dtid, sizeVal, colorByChg(c.chg),
              pct(c.chg, 1),                                  // 타일엔 등락%만(Finviz식 라벨+%)
              [tril(cap), c.ticker, c.stage, c.detail], lsize, "#ffffff");
          }
        }
      }
    }
    // 산업 전환 시 levelRef가 이전 산업의 노드를 가리키면(현재 ids에 없음) 루트로 리셋 → 빈 화면 방지
    if (!ids.includes(levelRef.current)) levelRef.current = rootId;
    const trace = {
      type: "treemap", ids, labels, parents, values, text: texts, customdata: cdata,
      // sort:false → 크기순 자동정렬 끔. 삽입 순서(Upstream→Midstream→Downstream, 단계·세부분류 순) 유지.
      sort: false,
      level: levelRef.current, branchvalues: "remainder", texttemplate: "%{label}<br>%{text}",
      marker: { colors, line: { width: 0.5, color: lcolors }, pad: { t: 24, l: 2, r: 2, b: 2 } },
      textfont: { size: tsizes, color: tcolors }, textposition: "middle center", tiling: { pad: 1 },
      pathbar: { visible: false },                                  // 상단 검은 띠 제거(클릭 토글로 복귀)
      hovertemplate: "<b>%{label}</b><br>시가총액 %{customdata[0]:,.2f}조원<extra></extra>",
    };
    const layout = { height: 900, margin: { t: 4, b: 4, l: 4, r: 4 }, paper_bgcolor: "#0c0f15" };
    const config = { displayModeBar: false, responsive: true };
    Plotly.react(el, [trace], layout, config).then(() => {
      // 산업 전환·재렌더마다 핸들러를 새로 배선 — 기존 핸들러를 제거하지 않으면 첫 산업(2차전지)
      // 데이터에 고정된(stale closure) 핸들러가 남아 클릭/hover가 옛 트리맵으로 동작한다.
      const gd = el as unknown as {
        on: (e: string, cb: (d: PlotlyEvt) => boolean | void) => void;
        removeAllListeners?: (e: string) => void;
      };
      gd.removeAllListeners?.("plotly_treemapclick");
      gd.removeAllListeners?.("plotly_hover");
      gd.removeAllListeners?.("plotly_unhover");
      gd.on("plotly_treemapclick", (d) => {
        const pt = d.points?.[0];
        const ticker = pt?.customdata?.[1] as string;
        if (ticker) { pickRef.current(ticker); return false; }      // 기업 → 주가(드릴 막음)
        const id = pt?.id || rootId;                                // 그룹 → 확대 / 같은 노드 재클릭 → 원래대로
        levelRef.current = id === levelRef.current ? rootId : id;
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
  }, [companies, rootId]);

  useEffect(() => () => { const el = ref.current; if (el) Plotly.purge(el); }, []);

  // 트리맵 고화질 PNG 저장(Plotly downloadImage — scale로 해상도 2배).
  const saveImage = () => {
    if (!ref.current) return;
    (Plotly as unknown as { downloadImage: (el: HTMLElement, o: object) => void })
      .downloadImage(ref.current, { format: "png", filename: `${rootId}_트리맵`, width: 2400, height: 1500, scale: 2 });
  };

  return (
    <div style={{ position: "relative" }}>
      <button type="button" className="ghost sm" onClick={saveImage}
        title="트리맵을 고화질 PNG 이미지로 저장"
        style={{ position: "absolute", top: 8, right: 8, zIndex: 6, display: "inline-flex", alignItems: "center",
          gap: 6, fontSize: 12, padding: "5px 11px" }}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
          strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
        </svg>
        이미지 저장
      </button>
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
// 투자의견 — 상승여력 기준. Strong Buy(≥+30%)·Buy(+10~30%)·Trading Buy(0~+10%)·Sell(<0%).
const RATING_RULE = "투자의견 = 상승여력 기준: Strong Buy(≥ +30%) · Buy(+10~30%) · Trading Buy(0~+10%) · Sell(< 0%)";
function ratingOf(up: number | null): { label: string; color: string; bg: string } | null {
  if (up == null) return null;
  if (up >= 30) return { label: "Strong Buy", color: "#1f9d57", bg: "rgba(31,157,87,0.18)" };
  if (up >= 10) return { label: "Buy", color: "#2ea65a", bg: "rgba(46,166,90,0.16)" };
  if (up > 0) return { label: "Trading Buy", color: "#2f6fb5", bg: "rgba(47,111,181,0.16)" };
  return { label: "Sell", color: "#e0504f", bg: "rgba(224,80,79,0.16)" };
}

export function CompanyReport({ ticker, company, name }: { ticker: string; company?: IndustryCompany; name?: string }) {
  const [kr, setKr] = useState<KrExtras | null>(null);
  const [cur, setCur] = useState<number | null>(null);   // 현재가(상승여력 계산용)
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    setBusy(true); setKr(null); setCur(null);
    api.krExtras(ticker)
      .then((d) => { if (alive) setKr(d); })
      .catch(() => { if (alive) setKr(null); })
      .finally(() => { if (alive) setBusy(false); });
    api.symbolDetail(ticker, "1mo")
      .then((d) => { if (alive) setCur(d.last?.close ?? null); })
      .catch(() => { /* 무시 */ });
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

  // #5 컨센서스 목표주가(평균) + 상승여력 — 추정실적 상단 배너. 컨센서스 없으면 리포트 목표가로 폴백.
  const cTargets = (kr?.consensus || []).map((c) => c.target).filter((t): t is number => t != null && t > 0);
  const rTargets = (kr?.reports || []).map((r) => r.target).filter((t): t is number => t != null && t > 0);
  const tList = cTargets.length ? cTargets : rTargets;
  const avgTarget = tList.length ? Math.round(tList.reduce((s, t) => s + t, 0) / tList.length) : null;
  const tUpside = (avgTarget != null && cur != null && cur > 0) ? (avgTarget / cur - 1) * 100 : null;

  if (busy) return <p style={{ color: "var(--muted)", fontSize: 13 }}>리포트 불러오는 중…</p>;

  // 좌측 박스 = 컨센서스(목표주가·추정실적·멀티플, 고정) / 우측 박스 = 애널리스트 리포트(스크롤).
  const rating = ratingOf(tUpside);
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 18, alignItems: "stretch" }}>
      {/* 추정실적 박스 = order 2(우측). 고정 높이 + 골드 스크롤(초기 세팅). */}
      <div className="panel scroll-gold" style={{ minWidth: 0, marginBottom: 0, order: 2, maxHeight: 560, overflowY: "auto" }}>
      <h3 style={{ marginTop: 0 }}>{name ? `${name} ` : ""}추정 실적</h3>
      {/* 추정 실적 — 개별종목분석과 동일 */}
      {hasEarnings ? (
        <div style={{ overflowX: "auto" }}>
          <h3 style={{ margin: "0 0 6px", fontWeight: 400, fontSize: 12, color: "var(--muted)" }}>(연결 · 단위 억원)</h3>
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
            <br />{RATING_RULE}
            {capEok != null && daEok != null && (
              <><br />EV/EBITDA = 시가총액 / (영업이익 + 감가상각비·무형자산상각비) · 순차입금 미반영(EV≈시총)</>
            )}
          </p>
        </div>
      ) : (
        <p style={{ color: "var(--muted)", fontSize: 13 }}>추정 실적 데이터가 없습니다.</p>
      )}
      </div>
      <div className="panel scroll-gold" style={{ minWidth: 0, marginBottom: 0, order: 1, maxHeight: 560, overflowY: "auto" }}>
      {/* 컨센서스 박스 = order 1(좌측). 고정 높이 + 골드 스크롤(초기 세팅). 띠 + 리포트. */}
      <h3 style={{ marginTop: 0 }}>{name ? `${name} ` : ""}컨센서스</h3>
      {/* 띠 — 투자의견(좌) · Target Price · 목표주가(+증감%) */}
      {avgTarget != null && (
        <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap",
          marginBottom: 12, padding: "10px 14px", background: "var(--accent-soft)",
          border: "1px solid var(--border)", borderRadius: 8 }}>
          {rating && (
            <span style={{ fontSize: "13pt", fontWeight: 800, padding: "2px 12px", borderRadius: 999,
              color: rating.color, background: rating.bg }}>{rating.label}</span>
          )}
          <span style={{ fontSize: "12pt", color: "var(--muted)", fontWeight: 700 }}>Target Price :</span>
          <span style={{ fontSize: "18pt", fontWeight: 800 }}>{avgTarget.toLocaleString()}원
            {tUpside != null && (
              <span style={{ fontSize: "14pt", fontWeight: 700, marginLeft: 6, color: tUpside >= 0 ? UP : DOWN }}>
                ({tUpside >= 0 ? "+" : ""}{tUpside.toFixed(1)}%)</span>
            )}
          </span>
        </div>
      )}
      <div>
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
      </div>
      </div>
    </div>
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
// 산업별 뉴스 검색 키워드(국내 30+·해외). 종목이 속한 산업을 인식해 동적으로 적용한다.
const INDUSTRY_NEWS_KW: Record<string, { kr: string[]; glob: string[] }> = {
  "2차전지": {
    kr: ["2차전지", "양극재", "음극재", "배터리", "ESS", "분리막", "동박", "전지박", "폐배터리", "전고체",
      "리튬", "니켈", "코발트", "전해질", "전구체", "양극활물질", "NCM", "LFP", "전기차 배터리", "배터리 셀",
      "배터리 소재", "배터리 수주", "캐즘", "IRA", "각형 배터리", "파우치", "원통형", "리튬가격", "배터리 화재", "충전"],
    glob: ["EV battery", "cathode", "battery recycling", "solid-state battery", "lithium price", "gigafactory"],
  },
  "반도체": {
    kr: ["반도체", "HBM", "D램", "낸드", "파운드리", "메모리", "시스템반도체", "웨이퍼", "EUV", "팹리스",
      "AI 반도체", "패키징", "후공정", "전공정", "DDR5", "CXL", "온디바이스 AI", "감산", "반도체 수출",
      "반도체 장비", "소부장", "반도체 업황", "메모리 가격", "TSMC", "엔비디아", "GPU", "칩", "파운드리 수주", "HBM4"],
    glob: ["HBM", "semiconductor", "foundry", "AI chip", "DRAM", "Nvidia"],
  },
  "전자부품": {
    kr: ["전자부품", "MLCC", "카메라모듈", "기판", "FPCB", "PCB", "적층세라믹콘덴서", "수동부품", "패키지기판",
      "디스플레이", "OLED", "폴더블", "스마트폰 부품", "전장부품", "전장", "액추에이터", "인덕터", "안테나",
      "부품 수주", "반도체기판", "FC-BGA", "온디바이스 AI", "갤럭시", "아이폰", "부품 단가"],
    glob: ["MLCC", "camera module", "OLED", "foldable", "electronic components"],
  },
  "건설": {
    kr: ["건설", "건설사", "분양", "재건축", "재개발", "정비사업", "주택", "부동산 PF", "미분양", "수주",
      "해외수주", "플랜트", "토목", "SOC", "건설경기", "시공능력", "청약", "분양가", "부동산 규제", "건자재",
      "시멘트", "철근", "아파트", "도시정비", "원자잿값", "PF 부실", "리츠"],
    glob: ["construction", "real estate", "infrastructure"],
  },
  "금융": {
    kr: ["금융", "은행", "증권", "보험", "금리", "기준금리", "대출", "예대마진", "NIM", "충당금",
      "배당", "자사주", "밸류업", "핀테크", "가계부채", "연체율", "BIS비율", "금융지주", "순이익", "ROE",
      "금융당국", "자본비율", "주주환원", "스트레스 완충자본", "예금금리"],
    glob: ["interest rate", "bank earnings", "dividend", "Korea valuation"],
  },
  "석유화학": {
    kr: ["석유화학", "정유", "나프타", "에틸렌", "프로필렌", "정제마진", "스프레드", "NCC", "화학", "합성수지",
      "폴리에틸렌", "PE", "PP", "PVC", "ABS", "정유사", "윤활유", "벤젠", "부타디엔", "화학제품",
      "유가", "원유", "정제설비", "가동률", "중국 수요", "범용 화학", "친환경 소재", "수소"],
    glob: ["petrochemical", "refining margin", "ethylene", "crude oil"],
  },
  "화장품": {
    kr: ["화장품", "K뷰티", "뷰티", "색조", "기초화장품", "스킨케어", "선크림", "마스크팩", "ODM", "OEM",
      "인디브랜드", "면세점", "따이공", "중국 소비", "더마", "화장품 수출", "미국 수출", "일본 수출",
      "클린뷰티", "리들샷", "뷰티 디바이스", "브랜드", "올리브영", "아마존", "역직구"],
    glob: ["K-beauty", "cosmetics", "skincare", "beauty"],
  },
};
const DEFAULT_NEWS_KW = { kr: ["증시", "코스피", "코스닥", "실적", "공시"], glob: ["Korea stocks", "KOSPI"] };

export function SectorNewsPanel({ ticker, name }: { ticker?: string; name?: string }) {
  const [news, setNews] = useState<SectorNews | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"kr" | "global">("kr");
  useEffect(() => {
    let alive = true; setBusy(true); setNews(null);
    // 종목이 속한 산업을 인식 → 산업별 키워드 + 종목명으로 동적 검색(해시태그는 화면 비노출).
    const run = (kw: { kr: string[]; glob: string[] }) => {
      const kr = name ? [name, ...kw.kr] : kw.kr;
      return api.sectorNews(kr, kw.glob)
        .then((d) => { if (alive) setNews(d); })
        .catch(() => { /* 무시 */ })
        .finally(() => { if (alive) setBusy(false); });
    };
    if (ticker) {
      api.industryOf(ticker)
        .then((r) => run(INDUSTRY_NEWS_KW[r.industry || ""] || DEFAULT_NEWS_KW))
        .catch(() => run(DEFAULT_NEWS_KW));
    } else {
      run(INDUSTRY_NEWS_KW["2차전지"]);   // 산업분석 페이지 등 종목 미지정 시
    }
    return () => { alive = false; };
  }, [ticker, name]);
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
          {/* 본문 삭제 — 기사링크(제목)와 중복. 그 자리에 날짜(연도)·언론사명을 본문 크기(12pt)로 */}
          <div style={{ fontSize: "12pt", color: "var(--muted)", marginTop: 3 }}>{n.date ? `${n.date} · ` : ""}{n.source}</div>
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
  const [indName, setIndName] = useState<string | null>(null);   // 현재 종목이 속한 산업명
  const [busy, setBusy] = useState(false);
  const [ebitdaBusy, setEbitdaBusy] = useState(false);   // EBITDA 비차단 후속 계산 진행중
  const [fStage, setFStage] = useState<string | null>(null);
  const [fDetail, setFDetail] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<string>("cap");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const toggleSort = (k: string) => {
    if (sortKey === k) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(k); setSortDir("desc"); }
  };

  // 현재 종목이 속한 산업을 자동 인식해 그 산업의 경쟁사(밸류체인)를 로드. 종목 변경 시 재조회.
  useEffect(() => {
    let alive = true;
    setBusy(true);
    setCompanies([]);
    api.industryOf(ticker)
      .then((r) => {
        if (!alive) return;
        const nm = r.industry;
        setIndName(nm);
        if (!nm) { setBusy(false); return; }   // 어느 산업에도 없음 → 빈 상태
        api.industryDetail(nm)
          .then((d) => {
            if (!alive) return;
            setCompanies(d.companies || []);
            setEbitdaBusy(true);   // EBITDA·EBITDA Margin은 별도 계산(비차단) — 표는 즉시, 값은 채워짐
            api.industryEbitda(nm)
              .then((m) => { if (alive) setCompanies((cur) => cur.map((c) => m[c.ticker] ? { ...c, ...m[c.ticker] } : c)); })
              .catch(() => { /* 무시 */ })
              .finally(() => { if (alive) setEbitdaBusy(false); });
          })
          .catch(() => { /* 무시 */ })
          .finally(() => { if (alive) setBusy(false); });
      })
      .catch(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [ticker]);

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
  const detailsOf = (st: string) => [...new Set(companies.filter((c) => c.stage === st).map((c) => c.detail))];
  const filtered = fStage === ALL
    ? companies
    : fStage
      ? companies.filter((c) => c.stage === fStage && (!fDetail || c.detail === fDetail))
      : [];
  // M/S(세부분류 내 시총 점유율)는 같은 세부분류 안에서만 합계 100%.
  // 세부분류가 하나뿐인 단계(배터리·장비 등)는 단계 자체가 한 세부분류 → 표시. 여럿(소재)이면
  // 특정 세부분류 선택 시에만 표시. '전체'에선 항상 숨김.
  const showMs = !!fDetail || (!!fStage && fStage !== ALL && detailsOf(fStage).length === 1);
  const ALL_COLS: { key: string; label: string; align: "left" | "right"; num: boolean;
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
  const COLS = ALL_COLS.filter((c) => c.key !== "ms" || showMs);
  // 단계를 구분(Upstream/Midstream/Downstream)으로 묶기 — 단계는 산업마다 달라 데이터에서 추출
  // (백엔드가 _GU_ORDER→_DAN_ORDER로 정렬해 줘서 등장 순서가 곧 올바른 단계 순서).
  const stagesByGu: Record<string, string[]> = {};
  for (const gu of GU_ORDER) {
    const sts = [...new Set(companies.filter((c) => c.gu === gu).map((c) => c.stage))];
    if (sts.length) stagesByGu[gu] = sts;
  }
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
  if (!busy && !indName)
    return <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>이 종목은 등록된 산업 밸류체인 데이터에 없습니다. (현재 지원: 2차전지·반도체·전자부품·건설·금융·석유화학·화장품)</p>;

  return (
    <>
      {indName && <div style={{ fontSize: 11, color: "var(--muted)", marginBottom: 8 }}>
        <b style={{ color: "var(--accent-strong)" }}>{indName}</b> 밸류체인 내 경쟁사</div>}
      <div style={{ marginBottom: 12 }}>
        {/* 전체 + 구분(Upstream/Midstream/Downstream)별 단계 그룹 */}
        <div style={{ marginBottom: 6 }}>
          <button type="button" className="ghost sm"
            onClick={() => { setFStage(ALL); setFDetail(null); }}
            style={chip(fStage === ALL)}>전체</button>
        </div>
        {GU_ORDER.filter((gu) => stagesByGu[gu]).map((gu) => (
          <div key={gu} style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 5, flexWrap: "wrap" }}>
            <span style={{ width: 86, flexShrink: 0, color: "var(--muted)", fontSize: 10.5,
              fontWeight: 700, letterSpacing: "0.04em", textTransform: "uppercase" }}>{gu}</span>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {stagesByGu[gu].map((st) => (
                <button key={st} type="button" className="ghost sm"
                  onClick={() => { setFStage(st === fStage ? null : st); setFDetail(null); }}
                  style={chip(fStage === st)}>{st}</button>
              ))}
            </div>
          </div>
        ))}
        {/* 선택 단계의 세부분류 — 별도 음영 박스로 명확히 구분 */}
        {fStage && fStage !== ALL && detailsOf(fStage).length > 1 && (
          <div style={{ marginTop: 8, marginLeft: 94, padding: "8px 12px",
            background: "var(--accent-soft)", border: "1px solid var(--border)", borderRadius: 8 }}>
            <div style={{ color: "var(--muted)", fontSize: 10.5, fontWeight: 700,
              letterSpacing: "0.04em", marginBottom: 6 }}>{fStage} · 세부분류</div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
              <button type="button" className="ghost sm" onClick={() => setFDetail(null)} style={chip(!fDetail)}>전체</button>
              {detailsOf(fStage).map((d) => (
                <button key={d} type="button" className="ghost sm"
                  onClick={() => setFDetail(d === fDetail ? null : d)} style={chip(fDetail === d)}>{d}</button>
              ))}
            </div>
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
                {showMs && <td style={{ padding: "6px", textAlign: "right", color: "var(--muted)" }}>{c.ms != null ? `${c.ms.toFixed(1)}%` : "—"}</td>}
                <td style={{ padding: "6px", textAlign: "right", color: c.chg == null ? "var(--muted)" : c.chg >= 0 ? UP : DOWN }}>{pct(c.chg, 1)}</td>
                <td style={{ padding: "6px", textAlign: "right" }}>{eok(c.revenue)}</td>
                <td style={{ padding: "6px", textAlign: "right", color: c.op != null && c.op < 0 ? DOWN : "inherit" }}>{eok(c.op)}</td>
                <td style={{ padding: "6px", textAlign: "right", color: c.op_margin != null && c.op_margin < 0 ? DOWN : "inherit" }}>{c.op_margin != null ? `${c.op_margin.toFixed(1)}%` : "—"}</td>
                <td style={{ padding: "6px", textAlign: "right", color: c.ebitda != null && c.ebitda < 0 ? DOWN : "inherit" }}>
                  {c.ebitda == null && ebitdaBusy ? <span style={{ color: "var(--muted)", fontSize: 11 }}>계산 중…</span> : eok(c.ebitda)}</td>
                <td style={{ padding: "6px", textAlign: "right", color: c.ebitda_margin != null && c.ebitda_margin < 0 ? DOWN : "inherit" }}>
                  {c.ebitda_margin == null && ebitdaBusy ? <span style={{ color: "var(--muted)", fontSize: 11 }}>계산 중…</span> : (c.ebitda_margin != null ? `${c.ebitda_margin.toFixed(1)}%` : "—")}</td>
              </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </>
  );
}

// Stream(Up/Mid/Down)별 시총가중 누적수익률 — 딱 3개 선. 주가추이와 동일한 시계열·기간선택 UX.
// 스트림 색 — Up=MyStock 기본 네이비 / Mid=하이라이트 골드 / Down=초록
const STREAM_COLOR: Record<string, string> = { Upstream: "#2f5390", Midstream: "#d4a738", Downstream: "#2ea65a" };
const STREAM_ORDER = ["Upstream", "Midstream", "Downstream"];
const SIDX_PERIODS: [string, number | "all"][] = [
  ["1개월", 1], ["3개월", 3], ["6개월", 6], ["1년", 12], ["2년", 24], ["전체", "all"],
];
export function StreamReturnsChart({ industry }: { industry: string }) {
  const [raw, setRaw] = useState<{ dates: string[]; streams: Record<string, number[]> } | null>(null);
  const [busy, setBusy] = useState(false);
  const [period, setPeriod] = useState<number | "all">(12);   // 기본 1년
  useEffect(() => {
    let alive = true; setBusy(true); setRaw(null); setPeriod(12);
    api.industryStreamIndex(industry)
      .then((d) => { if (alive) setRaw(d); })
      .catch(() => { if (alive) setRaw(null); })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [industry]);

  if (busy) return <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>스트림 수익률 불러오는 중…</p>;
  const dates = raw?.dates || [];
  const sAll = raw?.streams || {};
  const streams = STREAM_ORDER.filter((s) => (sAll[s]?.length || 0) > 0);
  if (!dates.length || !streams.length)
    return <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>표시할 스트림 수익률 데이터가 없습니다.</p>;

  // 기간 윈도우 — 최근일 기준 개월 역산(주가추이와 동일). 윈도우 시작점 기준으로 누적수익률 재정규화.
  const pad = (n: number) => String(n).padStart(2, "0");
  const dMax = dates[dates.length - 1];
  const startDate = period === "all" ? dates[0] : (() => {
    const d = new Date(dMax); d.setMonth(d.getMonth() - period);
    const s = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    return s < dates[0] ? dates[0] : s;
  })();
  const base0 = Math.max(dates.findIndex((dt) => dt >= startDate), 0);
  const reb = (arr: number[], i: number) => ((1 + arr[i] / 100) / (1 + arr[base0] / 100) - 1) * 100;
  const data = dates.slice(base0).map((dt, k) => {
    const i = base0 + k;
    const row: Record<string, number | string | null> = { date: dt };
    streams.forEach((s) => { const a = sAll[s]; row[s] = (a && a[i] != null && a[base0] != null) ? Math.round(reb(a, i) * 10) / 10 : null; });
    return row;
  });
  const xfmt = (dt: string) => (dt || "").slice(2, 7).replace("-", ".");   // YY.MM
  const tip = (o: { active?: boolean; label?: string; payload?: { dataKey: string; value: number | null }[] }) => {
    if (!o.active || !o.payload?.length) return null;
    return (
      <div style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: 6, padding: "8px 11px", fontSize: 12, lineHeight: 1.6 }}>
        <div style={{ color: "var(--muted)", marginBottom: 3 }}>{o.label}</div>
        {streams.map((s) => {
          const v = o.payload!.find((p) => p.dataKey === s)?.value;
          return (
            <div key={s} style={{ color: STREAM_COLOR[s], fontWeight: 700 }}>
              {s}: {v == null ? "—" : `${(v as number) >= 0 ? "+" : ""}${(v as number).toFixed(1)}%`}
            </div>
          );
        })}
      </div>
    );
  };
  const pBtn = (label: string, p: number | "all") => (
    <button key={label} type="button" onClick={() => setPeriod(p)}
      style={{ fontSize: 12, fontWeight: period === p ? 700 : 400, padding: "4px 11px", borderRadius: 8, cursor: "pointer",
        border: `1px solid ${period === p ? "#4f8ff5" : "var(--border)"}`,
        background: period === p ? "rgba(79,143,245,0.16)" : "transparent",
        color: period === p ? "#4f8ff5" : "var(--muted)" }}>{label}</button>
  );
  return (
    <div>
      <div style={{ display: "flex", justifyContent: "flex-end", flexWrap: "wrap", gap: 4, marginBottom: 6 }}>
        {SIDX_PERIODS.map(([l, p]) => pBtn(l, p))}
      </div>
      <ResponsiveContainer width="100%" height={340}>
        <ComposedChart data={data} margin={{ top: 10, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="date" tickFormatter={xfmt} tick={{ fontSize: 12 }} minTickGap={28} />
          <YAxis tick={{ fontSize: 12 }} width={46} tickFormatter={(n) => `${n}%`} />
          <Tooltip content={tip as never} />
          {streams.map((s) => (
            <Line key={s} dataKey={s} stroke={STREAM_COLOR[s]} strokeWidth={2}
              dot={false} isAnimationActive={false} connectNulls />
          ))}
        </ComposedChart>
      </ResponsiveContainer>
      {/* 색 구분자 — 그래프 하단 */}
      <div style={{ display: "flex", justifyContent: "center", gap: 18, marginTop: 8, fontSize: 12 }}>
        {streams.map((s) => (
          <span key={s} style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--muted)" }}>
            <span style={{ width: 18, height: 3, background: STREAM_COLOR[s], borderRadius: 2 }} />{s}
          </span>
        ))}
      </div>
    </div>
  );
}

// 기업별 수익률 표 — STREAM별 그룹(상위↔하위 정렬) + 시총가중 평균 대비 상회/하회. 종목 검색 지원.
const CRT_PERIODS: [string, "d5" | "d20" | "d60" | "d120" | "d240"][] = [
  ["5일", "d5"], ["1개월", "d20"], ["3개월", "d60"], ["6개월", "d120"], ["1년", "d240"],
];
const CRT_STREAMS = ["Upstream", "Midstream", "Downstream"];
function CompanyReturnsTable({ industry, companies }: { industry: string; companies: IndustryCompany[] }) {
  const navigate = useNavigate();
  const [ret, setRet] = useState<Record<string, Record<string, number | null> | null>>({});
  const [busy, setBusy] = useState(false);
  const [period, setPeriod] = useState<"d5" | "d20" | "d60" | "d120" | "d240">("d20");
  const [q, setQ] = useState("");
  const [sorts, setSorts] = useState<Record<string, { key: "ret" | "avg"; dir: "desc" | "asc" }>>({});   // 스트림별 정렬 컬럼·방향
  useEffect(() => {
    let alive = true; setBusy(true); setRet({});
    api.industryReturns(industry)
      .then((d) => { if (alive) setRet(d as Record<string, Record<string, number | null> | null>); })
      .catch(() => { if (alive) setRet({}); })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [industry]);

  // 스트림별: 수익률 보유 종목을 상위→하위 정렬 + 시총가중 평균(차트와 동일 방식). 검색 시 매칭만.
  const sections = CRT_STREAMS.map((s) => {
    const all = (companies.filter((c) => c.gu === s)
      .map((c) => ({ c, v: ret[c.ticker]?.[period] ?? null }))
      .filter((x) => x.v != null)) as { c: IndustryCompany; v: number }[];
    const wsum = all.reduce((a, x) => a + (x.c.cap || 0), 0);
    const avg = all.length === 0 ? null
      : wsum > 0 ? all.reduce((a, x) => a + x.v * (x.c.cap || 0), 0) / wsum
        : all.reduce((a, x) => a + x.v, 0) / all.length;
    const shown = q ? all.filter((x) => x.c.name.includes(q) || x.c.ticker.includes(q)) : all;
    return { s, avg, shown, n: all.length };
  }).filter((sec) => sec.shown.length > 0);

  const pBtn = (label: string, p: typeof period) => (
    <button key={label} type="button" onClick={() => setPeriod(p)}
      style={{ fontSize: 12, fontWeight: period === p ? 700 : 400, padding: "3px 10px", borderRadius: 7, cursor: "pointer",
        border: `1px solid ${period === p ? "#4f8ff5" : "var(--border)"}`,
        background: period === p ? "rgba(79,143,245,0.16)" : "transparent",
        color: period === p ? "#4f8ff5" : "var(--muted)" }}>{label}</button>
  );
  const pctTxt = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;
  return (
    <div>
      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", marginBottom: 10 }}>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="종목 검색 (이름·코드)"
          style={{ flex: "1 1 140px", minWidth: 110, fontSize: 13, padding: "6px 10px" }} />
        <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>{CRT_PERIODS.map(([l, p]) => pBtn(l, p))}</div>
      </div>
      {busy ? (
        <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>수익률 불러오는 중…</p>
      ) : sections.length === 0 ? (
        <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>{q ? "검색 결과가 없습니다." : "수익률 데이터가 없습니다."}</p>
      ) : (
        <div className="scroll-gold" style={{ maxHeight: 380, overflowY: "auto" }}>
          {sections.map(({ s, avg, shown, n }) => {
            const col = STREAM_COLOR[s] || "#7f8aa0";
            const so = sorts[s] || { key: "ret", dir: "desc" };
            const setSort = (k: "ret" | "avg") => setSorts((m) => {
              const cur = m[s] || { key: "ret", dir: "desc" };
              return { ...m, [s]: { key: k, dir: cur.key === k ? (cur.dir === "desc" ? "asc" : "desc") : "desc" } };
            });
            const ind = (k: "ret" | "avg") => (so.key === k ? (so.dir === "desc" ? " ▼" : " ▲") : "");
            const keyv = (x: { v: number }) => (so.key === "avg" ? (avg == null ? 0 : x.v - avg) : x.v);
            const ordered = shown.slice().sort((a, b) => (so.dir === "desc" ? keyv(b) - keyv(a) : keyv(a) - keyv(b)));
            const hbtn = (k: "ret" | "avg", label: string) => (
              <button type="button" onClick={() => setSort(k)}
                style={{ fontSize: 13, fontWeight: 700, padding: 0, cursor: "pointer", background: "transparent",
                  border: 0, color: "#fff" }}>{label}{ind(k)}</button>
            );
            return (
              <div key={s} style={{ border: "1px solid var(--border)", borderLeft: `3px solid ${col}`,
                borderRadius: 6, overflow: "hidden", marginBottom: 14 }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13, whiteSpace: "nowrap" }}>
                  <thead>
                    {/* 구분자 박스 행 — 스트림명·평균(좌) + 수익률(%)·Avg.(우) 동일 행 */}
                    <tr style={{ background: `${col}1f`, borderBottom: "1px solid var(--border)" }}>
                      <th style={{ textAlign: "left", padding: "6px 8px" }}>
                        <span style={{ fontSize: 13, fontWeight: 800, color: col }}>{s}({n}종목)</span>
                        <span style={{ fontSize: 12, fontWeight: 400, color: "var(--muted)", marginLeft: 8 }}>시총가중 평균
                          <b style={{ color: avg == null ? "var(--muted)" : avg >= 0 ? UP : DOWN, marginLeft: 4 }}>
                            {avg == null ? "—" : pctTxt(avg)}</b></span>
                      </th>
                      <th style={{ textAlign: "right", padding: "6px 8px", width: 84 }}>{hbtn("ret", "수익률(%)")}</th>
                      <th style={{ textAlign: "right", padding: "6px 8px", width: 92 }}>{hbtn("avg", "Avg.")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ordered.map(({ c, v }) => {
                      const d = avg == null ? null : v - avg;   // 평균대비(%p)
                      return (
                        <tr key={c.ticker} onClick={() => navigate(`/dashboard?symbol=${c.ticker}`)}
                          style={{ borderBottom: "1px solid var(--border)", cursor: "pointer" }}>
                          <td style={{ padding: "5px 8px", fontWeight: 600 }}>{c.name}
                            <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 11 }}> {c.ticker}</span></td>
                          <td style={{ padding: "5px 8px", textAlign: "right", fontWeight: 700,
                            color: v >= 0 ? UP : DOWN }}>{pctTxt(v)}</td>
                          <td style={{ padding: "5px 8px", textAlign: "right",
                            color: d == null ? "var(--muted)" : d >= 0 ? UP : DOWN }}>
                            {d == null ? "—" : `${d >= 0 ? "+" : ""}${d.toFixed(1)}%p`}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export default function IndustryAnalysis() {
  const navigate = useNavigate();
  const [data, setData] = useState<IndustryData | null>(null);
  const [root, setRoot] = useState("2차전지");   // 선택된 산업(소분류)
  // 펼쳐진 대분류(WICS 섹터) — 클릭 시 그 소분류만 표시. 기본=현재 산업이 속한 대분류.
  const [openSector, setOpenSector] = useState<string>(
    () => INDUSTRY_GROUPS.find((g) => groupIndustries(g).includes("2차전지"))?.label || "2차전지");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [asOfReq, setAsOfReq] = useState("");   // 트리맵 기준일(yyyy-mm-dd, ""=최신 거래일)
  const [refreshing, setRefreshing] = useState(false);
  const [searchQ, setSearchQ] = useState("");   // 트리맵 기업 검색어

  // 지연 로딩 — 트리맵(벌크 시총으로 즉시)이 뜬 뒤 느린 데이터를 받아 병합.
  // EBITDA = FnGuide D&A, 기간수익률(hover) = 종목별 DataReader. 둘 다 비차단.
  const mergeEbitda = () => {
    api.industryEbitda(root)
      .then((m) => setData((cur) => cur
        ? { ...cur, companies: cur.companies.map((c) => m[c.ticker] ? { ...c, ...m[c.ticker] } : c) }
        : cur))
      .catch(() => { /* 무시 */ });
  };
  const mergeReturns = () => {
    api.industryReturns(root)
      .then((m) => setData((cur) => cur
        ? { ...cur, companies: cur.companies.map((c) => m[c.ticker] ? { ...c, ret: m[c.ticker] } : c) }
        : cur))
      .catch(() => { /* 무시 */ });
  };

  useEffect(() => {
    setBusy(true);
    api.industryDetail(root, asOfReq || undefined)
      .then((d) => { setData(d); setErr(""); mergeEbitda(); mergeReturns(); })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setBusy(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asOfReq, root]);

  // 새로고침 — 서버 주가 캐시를 비우고 실시간 시세로 재조회
  const doRefresh = () => {
    setRefreshing(true);
    api.industryDetail(root, asOfReq || undefined, true)
      .then((d) => { setData(d); setErr(""); mergeEbitda(); mergeReturns(); })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setRefreshing(false));
  };

  // #4 실시간 갱신 — 장중(평일 09:00~15:40 KST)엔 60초마다 시총·등락 자동 재조회.
  // 백엔드 _snapshot은 90초 TTL로 신선화되므로(industry.py), 폴링이 최신 시세를 가져온다.
  // 과거(asOfReq) 조회 중엔 폴링하지 않음. 장외엔 시세 불변이라 폴링 불필요.
  useEffect(() => {
    if (asOfReq) return;
    const marketOpen = () => {
      const n = new Date();
      const wd = n.getDay();                        // 0=일,6=토
      const hm = n.getHours() * 60 + n.getMinutes();
      return wd >= 1 && wd <= 5 && hm >= 9 * 60 && hm <= 15 * 60 + 40;
    };
    const id = setInterval(() => {
      if (!marketOpen() || document.hidden) return;
      api.industryDetail(root, undefined)          // 90초 TTL 버킷이 최신 시세 반영(refresh=true 불필요)
        .then((d) => { setData(d); mergeEbitda(); mergeReturns(); })
        .catch(() => { /* 무시 */ });
    }, 60000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [root, asOfReq]);

  const companies = data?.companies || [];
  // 트리맵·검색에서 기업 선택 → HOME(개별 기업 분석)으로 이동
  const pickCompany = (ticker: string) => navigate(`/dashboard?symbol=${ticker}`);
  // 제목 표기 — 밸류체인(트리맵 표시분=Downstream 제외) 시총 합계. 기준일은 달력 입력으로.
  const tmTotal = companies
    .filter((c) => c.cap && c.cap > 0 && !(root === "2차전지" && c.gu === "Downstream"))
    .reduce((s, c) => s + (c.cap || 0), 0);
  const totalAmt = tmTotal > 0 ? `${(tmTotal / 1e12).toFixed(1)}조` : "";   // '원' 제거
  const maxDate = new Date().toISOString().slice(0, 10);   // 미래 일자 선택 방지

  return (
    <div className="dashboard-fullwidth industry-67">
      <h1>Industry Analysis</h1>

      {/* 산업 탭 — WICS 대분류(골드 라운드 버튼) 클릭 시 그 소분류(파란 사각 버튼)만 펼침(아코디언).
          대분류는 버튼형이되 소분류와 다른 디자인. 없는 대분류는 흐리게 + 클릭 시 '준비 중'. */}
      {(() => {
        const avail = data?.available || ["2차전지", "반도체", "전자부품", "건설", "금융", "석유화학", "화장품"];
        const allGrouped = new Set(INDUSTRY_GROUPS.flatMap(groupIndustries));
        const groups: IndustryGroup[] = INDUSTRY_GROUPS.map((g) =>
          g.subgroups
            ? { label: g.label, subgroups: g.subgroups.map((s) => ({ label: s.label, items: s.items.filter((nm) => avail.includes(nm)) })) }
            : { label: g.label, items: (g.items ?? []).filter((nm) => avail.includes(nm)) });
        const extras = avail.filter((nm) => !allGrouped.has(nm));
        if (extras.length) groups.push({ label: "기타", items: extras });
        const cur = groups.find((g) => g.label === openSector) || groups.find((g) => groupIndustries(g).length) || groups[0];
        // 그레이 네이비 팔레트 — 대분류 탭/패널 공용(활성 탭이 패널과 같은 색으로 '연결')
        const GN_BG = "#1a2436", GN_BD = "#33425e";
        // 대분류 = 그레이 네이비 직사각형 탭. 클릭 시 펼침 + 단일 산업이면 바로 로드.
        const secBtn = (g: IndustryGroup) => {
          const on = !!cur && g.label === cur.label;
          const empty = groupIndustries(g).length === 0;
          return (
            <button key={g.label} type="button"
              onClick={() => { setOpenSector(g.label); const inds = groupIndustries(g); if (inds.length === 1 && inds[0] !== root) { setRoot(inds[0]); setAsOfReq(""); } }}
              onMouseEnter={(e) => { if (!on) e.currentTarget.style.background = "rgba(51,66,94,0.30)"; }}
              onMouseLeave={(e) => { if (!on) e.currentTarget.style.background = "transparent"; }}
              style={{ fontSize: "13.5pt", fontWeight: 800, padding: "8px 18px", cursor: "pointer",
                borderRadius: "6px 6px 0 0", marginBottom: -1,
                transition: "background .12s ease",
                background: on ? GN_BG : "transparent",
                borderTop: `1px solid ${on ? GN_BD : "transparent"}`,
                borderLeft: `1px solid ${on ? GN_BD : "transparent"}`,
                borderRight: `1px solid ${on ? GN_BD : "transparent"}`,
                borderBottom: `1px solid ${on ? GN_BG : "transparent"}`,
                color: on ? "var(--text)" : "var(--navy-300)", opacity: empty ? 0.5 : 1 }}>
              {g.label}
            </button>
          );
        };
        // 소분류 = 파란 사각 버튼
        const tab = (nm: string) => {
          const on = nm === root;
          return (
            <button key={nm} type="button" onClick={() => { if (nm !== root) { setRoot(nm); setAsOfReq(""); } }}
              onMouseEnter={(e) => { if (!on) { e.currentTarget.style.background = "rgba(79,143,245,0.14)"; e.currentTarget.style.color = "#1668c4"; e.currentTarget.style.borderColor = "rgba(79,143,245,0.5)"; } }}
              onMouseLeave={(e) => { if (!on) { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = "var(--text)"; e.currentTarget.style.borderColor = "var(--border)"; } }}
              style={{ fontSize: "11.5pt", fontWeight: 800, padding: "6px 16px", borderRadius: 4, cursor: "pointer",
                transition: "background .12s ease, color .12s ease, border-color .12s ease",
                background: on ? "rgba(79,143,245,0.22)" : "transparent",
                border: `1px solid ${on ? "rgba(79,143,245,0.7)" : "var(--border)"}`,
                color: on ? "#1668c4" : "var(--text)" }}>
              {nm}
            </button>
          );
        };
        const naMsg = (txt: string) => (
          <span style={{ fontSize: 12, color: "var(--navy-300)", border: "1px dashed var(--border)", borderRadius: 4, padding: "6px 14px" }}>{txt}</span>
        );
        return (
          <div style={{ margin: "6px 0 14px" }}>
            <div style={{ display: "flex", gap: 3, flexWrap: "wrap", borderBottom: `1px solid ${GN_BD}` }}>
              {groups.map(secBtn)}
            </div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap", padding: "12px 14px",
              background: GN_BG, border: `1px solid ${GN_BD}`, borderTop: "none", borderRadius: "0 6px 6px 6px" }}>
              {cur?.subgroups ? (
                // 소비재 등 — 하위분류별로 그룹핑 표시
                <div style={{ display: "flex", flexDirection: "column", gap: 10, width: "100%" }}>
                  {cur.subgroups.map((s) => (
                    <div key={s.label} style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                      <span style={{ fontSize: 12, fontWeight: 800, color: "var(--navy-300)", minWidth: 76 }}>{s.label}</span>
                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                        {s.items.length ? s.items.map(tab) : naMsg("준비 중")}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (cur?.items?.length ? cur.items.map(tab) : naMsg(`${cur?.label} 산업은 준비 중입니다`))}
            </div>
          </div>
        );
      })()}

      {err && <div className="error">{err}</div>}
      {busy && <p style={{ color: "var(--muted)" }}>산업 데이터 불러오는 중…</p>}

      {data && (
        <>
          {/* 밸류체인 시가총액 트리맵 */}
          <div className="panel" style={{ position: "relative" }}>
            <h3 style={{ marginTop: 0, display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <span>{root} 밸류체인{totalAmt && " - "}
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
            <IndustryTreemap companies={companies} onPick={pickCompany} rootId={root} />
            {/* 증감률별 셀 색상 범례 — 트리맵 우측 하단 */}
            <div style={{ display: "flex", justifyContent: "flex-end", gap: 0, marginTop: 6 }}>
              {[-3, -2, -1, 0, 1, 2, 3].map((v) => (
                <span key={v} style={{ background: colorByChg(v), color: "#fff",
                  fontSize: 14, fontWeight: 700, padding: "5px 18px" }}>{v > 0 ? "+" : ""}{v}%</span>
              ))}
            </div>
          </div>
          {/* 트리맵 아래 — 좌: 기업별 수익률 표 / 우: Stream 누적수익률 차트. 가로·세로 동일(stretch). */}
          <div style={{ display: "grid", gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 12, alignItems: "stretch" }}>
            <div className="panel" style={{ marginBottom: 0 }}>
              <h3 style={{ marginTop: 0 }}>기업별 수익률
                <span style={{ fontWeight: 400, fontSize: "12pt", color: "var(--muted)" }}> · 기간·정렬 선택</span>
              </h3>
              <CompanyReturnsTable industry={root} companies={companies} />
            </div>
            <div className="panel" style={{ marginBottom: 0 }}>
              <h3 style={{ marginTop: 0 }}>Stream별 누적수익률
                <span style={{ fontWeight: 400, fontSize: "12pt", color: "var(--muted)" }}> · 시가총액 가중평균</span>
              </h3>
              <StreamReturnsChart industry={root} />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

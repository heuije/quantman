import { Fragment, useEffect, useState } from "react";
import { stockSearchMatch } from "../format";
import { useSearchParams } from "react-router-dom";
import {
  ComposedChart, Bar, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, LabelList, ReferenceLine,
} from "recharts";
import { api } from "../api";
import type { SymbolListing, CompanyProfile, SymbolDetail, FinancialsData, FinStatement } from "../types";
import StockDashboard from "./StockDashboard";
import { CompanyReport, OpinionBoard, SectorNewsPanel, CompanyPriceChart, PeerAnalysis } from "./IndustryAnalysis";

// HOME(개별 기업 분석) — 시킹알파식. 최상단 검색 + 하위탭. 종목 1개 중심.
const TABS = ["Summary", "Ratings by Mystock", "Stock Price", "Peer Analysis", "Financials", "Estimates", "News"] as const;
type Tab = typeof TABS[number];

// 기업 개요 — 저장본(company_profiles, 분기 갱신) + 검색 이름
function ProfileBlock({ ticker, name }: { ticker: string; name: string }) {
  const [p, setP] = useState<CompanyProfile | null>(null);
  const [d, setD] = useState<SymbolDetail | null>(null);
  useEffect(() => {
    let alive = true; setP(null); setD(null);
    // 시총=shares×현재가, 주요사업=business 모두 /profile이 제공 → 종목무관 산업표 하드코딩 폴백 제거(C6).
    api.companyProfile(ticker).then((x) => { if (alive) setP(x); }).catch(() => { /* 무시 */ });
    // light — 개요는 last(현재가·베타·52주)만 쓰므로 보조지표 38종 계산 생략(콜드 14.6s→0.7s)
    api.symbolDetail(ticker, "1y", true).then((x) => { if (alive) setD(x); }).catch(() => { /* 무시 */ });
    return () => { alive = false; };
  }, [ticker]);
  const num = (v: number | null | undefined) => v == null ? "—" : v.toLocaleString();
  // 시가총액 — 1조 이상은 조, 그 미만은 억 단위
  const capFmt = (v: number | null) => v == null ? "—"
    : v >= 1e12 ? `${(v / 1e12).toFixed(2)}조` : `${Math.round(v / 1e8).toLocaleString()}억`;
  const L = d?.last;
  const cap = (L?.close != null && p?.shares) ? L.close * p.shares : null;
  const row = (l: string, v: React.ReactNode) => (
    <tr style={{ borderBottom: "1px solid var(--border)" }}>
      <td style={{ padding: "7px 6px", color: "var(--muted)", width: 96, whiteSpace: "nowrap" }}>{l}</td>
      <td style={{ padding: "7px 6px" }}>{v}</td>
    </tr>
  );
  // table-fit — 라벨/값 2열이라 가로 스크롤이 필요 없다. 모바일 전역 규칙(.panel table을
  // display:block으로 만들어 넓은 표를 스크롤)에서 빠져야 값이 패널 오른쪽 끝에 정렬된다.
  return (
    <table className="table-fit" style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
      <tbody>
        {row("기업명", <b>{name} ({ticker})</b>)}
        {row("주요 사업", p?.business || "—")}
        {row("현재가/시가총액", L?.close != null ? `${num(L.close)}원 / ${capFmt(cap)}` : "—")}
        {row("베타", L?.beta != null ? L.beta.toFixed(2) : "—")}
        {row("52주 최고/최저", (L?.high_52w != null && L?.low_52w != null) ? `${num(L.high_52w)} / ${num(L.low_52w)}` : "—")}
        {row("설립일", p?.established || "—")}
        {row("대표이사", p?.ceo || "—")}
        {row("종업원수", p?.employees ? `${p.employees}명` : "—")}
        {row("홈페이지", p?.homepage
          ? <a href={p.homepage} target="_blank" rel="noreferrer" style={{ color: "var(--accent)" }}>{p.homepage}</a>
          : "—")}
      </tbody>
    </table>
  );
}

// 재무제표(Financials) — DART 공시 집계(FnGuide) 연결 PL·BS·CF. 연간 YoY%·분기 QoQ%(기울임꼴).
// 저장본 즉시 표시(로딩 없음). 부모 계정은 +/− 버튼으로 상세 접기/펼치기. 본문 12pt(데이터 표준).
const FIN_UP = "#de3033", FIN_DOWN = "#1668c4";
const FIN_STMTS: [keyof FinancialsData["annual"], string][] =
  [["PL", "손익계산서"], ["BS", "재무상태표"], ["CF", "현금흐름표"]];

// 그래프 색상 — 마이스톡 메인 네이비(진하게) + 하이라이트 골드만(회색 금지).
const CHART_NAVY = "#123257", CHART_GOLD = "#d4a738";   // 막대=딥 네이비(살짝 연하게)·선=하이라이트 골드
const CHART_BAR_EDGE = "#3a5283";                        // 다크 카드 위 막대 외곽선(가시성 유지)
type SeriesArr = (number | null)[];
// v=금액(억원, 표시는 ×100=백만원) · mg=해당 계정 비율(%) 선 · mgLabel=선 이름(부채비율/ROE/마진 등)
interface ChartDef {
  t: string; v?: SeriesArr; mg?: SeriesArr; mgLabel?: string; yoy?: SeriesArr; won?: boolean;
  // 다중 막대(유동자산/부채 그룹, NWC 스택) + 추가 골드 선(유동비율·NWC 등)
  bars?: { key: string; label: string; color: string; series: SeriesArr; stackId?: string }[];
  line2?: { label: string; series: SeriesArr; pct?: boolean };
}

// 기간 라벨 — 연간 "2023/12"→FY23, 분기 "2026/03"→1Q26. 차트 x축·기간 드롭다운 공용.
function periodLabel(p: string, quarterly: boolean): string {
  const [y, m] = p.split("/");
  const yy = y.slice(2);
  if (quarterly) {
    const q = ({ "03": 1, "06": 2, "09": 3, "12": 4 } as Record<string, number>)[m] ?? m;
    return `${q}Q${yy}`;
  }
  return `FY${yy}`;
}

// Financials 주요지표 — 한경식. PL/BS/CF 섹션 분리, 한 행 2개, 막대(값)+선(YoY/QoQ),
// 이익률 항목은 마진선 추가, 막대 위 값(1자리) 라벨, 색상=네이비+골드. 연간/분기 src 반영.
function FinCharts({ src, quarterly, stmt, from = "", to = "" }:
  { src: FinancialsData["annual"]; quarterly: boolean; stmt: keyof FinancialsData["annual"]; from?: string; to?: string }) {
  const norm = (s: string) => s.replace(/\s/g, "");
  const row = (st: FinStatement | undefined, names: string[]) =>
    st?.rows.find((r) => names.includes(norm(r.account)) || (!!r.canon && names.includes(norm(r.canon))));
  const pl = src.PL, bs = src.BS, cf = src.CF;
  const periods = pl?.periods || bs?.periods || cf?.periods || [];
  if (periods.length === 0) return null;
  const N = periods.length;
  // #3 사용자 지정 기간(from~to)만 차트에 표시. 기간 "YYYY/MM"은 사전식 비교로 정렬됨.
  const keep = periods.map((p) => (!from || p >= from) && (!to || p <= to));
  const vals = (st: FinStatement | undefined, names: string[]): SeriesArr => row(st, names)?.values ?? Array(N).fill(null);
  const chg = (st: FinStatement | undefined, names: string[]): SeriesArr => row(st, names)?.change ?? Array(N).fill(null);
  const sumS = (...as: SeriesArr[]): SeriesArr => periods.map((_, i) =>
    as.reduce<number | null>((s, a) => (s == null || a[i] == null) ? null : s + (a[i] as number), 0));
  const subS = (a: SeriesArr, b: SeriesArr): SeriesArr => a.map((x, i) => (x != null && b[i] != null) ? x - (b[i] as number) : null);
  const ratio = (n: SeriesArr, d: SeriesArr): SeriesArr => n.map((x, i) => (x != null && d[i]) ? Math.round((x / (d[i] as number)) * 1000) / 10 : null);
  // YoY/QoQ 증감률 — 전기≤0(음수·0) 또는 당기<0이면 % 왜곡(적자전환·적자지속)이라 표시 안 함.
  const yoy = (s: SeriesArr): SeriesArr => s.map((v, i) => {
    const p = s[i - 1];
    return (i === 0 || v == null || p == null || (p as number) <= 0 || (v as number) < 0)
      ? null : Math.round((v / (p as number) - 1) * 1000) / 10;
  });

  const asset = vals(bs, ["자산", "자산총계"]), liab = vals(bs, ["부채", "부채총계"]), eq = vals(bs, ["자본", "자본총계"]);
  const curAsset = vals(bs, ["유동자산"]), curLiab = vals(bs, ["유동부채"]);
  const ar = vals(bs, ["매출채권및기타유동채권", "매출채권"]);
  const inv = vals(bs, ["재고자산"]);
  const ap = vals(bs, ["매입채무및기타유동채무", "매입채무"]);
  const apNeg = ap.map((x) => (x == null ? null : -x));                 // 매입채무 음수(0선 하단)
  const nwc = periods.map((_, i) =>                                     // NWC = 매출채권 + 재고 − 매입채무
    (ar[i] == null && inv[i] == null && ap[i] == null) ? null : (ar[i] || 0) + (inv[i] || 0) - (ap[i] || 0));
  // 차입금 = 단기 + 장기 + 유동성장기차입금(장기→단기 대체분). DART 계정명은 '유동성장기차입금'.
  const borrow = sumS(vals(bs, ["단기차입금"]), vals(bs, ["장기차입금"]),
    vals(bs, ["유동성장기차입금", "유동성장기부채", "유동성사채"]));
  const netDebt = subS(borrow, vals(bs, ["현금및현금성자산"]));
  const opCf = vals(cf, ["영업활동으로인한현금흐름", "영업활동현금흐름"]);
  const invCf = vals(cf, ["투자활동으로인한현금흐름", "투자활동현금흐름"]);
  const finCf = vals(cf, ["재무활동으로인한현금흐름", "재무활동현금흐름"]);
  const ctrlNi = vals(pl, ["지배기업소유주지분", "지배주주순이익"]);   // 지배기업 소유주지분(=지배기업순이익)
  const eps = vals(pl, ["기본주당순이익(원)", "기본주당순이익", "EPS"]).map((x) => (x == null ? null : x * 1e8));   // /1e8 환원 → 원

  // 차트 = 금액 막대(네이비) + 해당 계정 비율 선(골드, 우축). PL 이익률·BS 부채비율/ROE/차입금비율을 선으로.
  const SECTIONS: { key: keyof FinancialsData["annual"]; title: string; charts: ChartDef[] }[] = [
    { key: "PL", title: "손익계산서", charts: [
      { t: "매출액", v: vals(pl, ["매출액"]), yoy: chg(pl, ["매출액"]) },
      { t: "매출총이익", v: vals(pl, ["매출총이익"]), mg: vals(pl, ["매출총이익률(%)"]), mgLabel: "매출총이익률", yoy: chg(pl, ["매출총이익"]) },
      { t: "영업이익", v: vals(pl, ["영업이익"]), mg: vals(pl, ["영업이익률(%)"]), mgLabel: "영업이익률", yoy: chg(pl, ["영업이익"]) },
      { t: "당기순이익", v: vals(pl, ["당기순이익"]), mg: vals(pl, ["당기순이익률(%)"]), mgLabel: "순이익률", yoy: chg(pl, ["당기순이익"]) },
      { t: "EBITDA", v: vals(pl, ["EBITDA"]), mg: vals(pl, ["EBITDAMargin(%)"]), mgLabel: "EBITDA%", yoy: chg(pl, ["EBITDA"]) },
      { t: "지배기업순이익", v: ctrlNi, yoy: yoy(ctrlNi) },
      { t: "EPS (주당순이익)", v: eps, won: true, yoy: yoy(eps) },
    ] },
    { key: "BS", title: "재무상태표", charts: [
      { t: "자산총계", v: asset, yoy: yoy(asset) },
      { t: "부채총계", v: liab, mg: ratio(liab, eq), mgLabel: "부채비율", yoy: yoy(liab) },
      { t: "자본총계", v: eq, mg: ratio(vals(pl, ["당기순이익"]), eq), mgLabel: "ROE", yoy: yoy(eq) },
      { t: "유동자산·유동부채", bars: [
          { key: "ca", label: "유동자산", color: CHART_NAVY, series: curAsset },
          { key: "cl", label: "유동부채", color: CHART_BAR_EDGE, series: curLiab },
        ], line2: { label: "유동비율", series: ratio(curAsset, curLiab), pct: true } },
      { t: "NWC (운전자본·CCC)", bars: [
          { key: "ar", label: "매출채권", color: CHART_NAVY, series: ar, stackId: "nwc" },
          { key: "inv", label: "재고자산", color: CHART_BAR_EDGE, series: inv, stackId: "nwc" },
          { key: "ap", label: "매입채무(−)", color: "#c0504d", series: apNeg, stackId: "nwc" },
        ], line2: { label: "NWC", series: nwc } },
      { t: "Net Debt (순차입금)", v: netDebt, mg: ratio(borrow, eq), mgLabel: "차입금비율", yoy: yoy(netDebt) },
    ] },
    { key: "CF", title: "현금흐름표", charts: [
      { t: "영업활동현금흐름", v: opCf, yoy: chg(cf, ["영업활동으로인한현금흐름", "영업활동현금흐름"]) },
      { t: "투자활동현금흐름", v: invCf, yoy: yoy(invCf) },
      { t: "재무활동현금흐름", v: finCf, yoy: yoy(finCf) },
      { t: "잉여현금흐름 (FCF)", v: sumS(opCf, invCf), yoy: yoy(sumS(opCf, invCf)) },
      { t: "기말현금", v: vals(cf, ["기말현금및현금성자산"]), yoy: yoy(vals(cf, ["기말현금및현금성자산"])) },
    ] },
  ];

  const lineLbl = quarterly ? "QoQ%" : "YoY%";
  const xLabel = (p: string) => periodLabel(p, quarterly);   // FY23 / 1Q26

  // 선택된 재무제표 탭(stmt)의 섹션만. 표시 기간에 값이 전혀 없는 차트는 제외(단일 v 또는 다중 bars).
  const sec = SECTIONS.find((s) => s.key === stmt);
  const hasData = (c: ChartDef) =>
    (!!c.v && c.v.some((x, i) => x != null && keep[i])) ||
    (!!c.bars && c.bars.some((b) => b.series.some((x, i) => x != null && keep[i])));
  const shown = sec ? sec.charts.filter(hasData) : [];

  // 금액=막대(네이비, 좌축·백만원) + 골드 선(우축): 이익률 있으면 마진%, 없으면 YoY/QoQ 증감률. 막대는 0 기준.
  const renderChart = (c: ChartDef) => {
    const useMg = !!c.mg && c.mg.some((x, i) => x != null && keep[i]);   // 이익률/비율 보유
    const lnLabel = useMg ? (c.mgLabel || "비율") : lineLbl;             // 마진% 또는 YoY%/QoQ%
    const mult = c.won ? 1 : 100;            // 일반=억원→백만원(×100), 주당이익=원(×1)
    const unitLbl = c.won ? "원" : "백만원";
    const vSer = c.v ?? [];
    const data = periods.map((p, i) => ({ x: xLabel(p),
      v: vSer[i] == null ? null : Math.round((vSer[i] as number) * mult),
      ln: (useMg ? c.mg?.[i] : c.yoy?.[i]) ?? null }))
      .filter((_, i) => keep[i]);
    const hasLine = data.some((d) => d.ln != null);
    const amtFmt = (n: number) => Math.round(n).toLocaleString();
    // 좌(금액)·우(%) 축 도메인 명시 → 라벨이 잘리지 않게 상하 여백 확보.
    const vVals = data.map((d) => d.v).filter((x): x is number => x != null);
    const lnVals = data.map((d) => d.ln).filter((x): x is number => x != null);
    const vMax = vVals.length ? Math.max(...vVals) : 0;
    const vMin = vVals.length ? Math.min(...vVals) : 0;
    const vRange = Math.max(0, vMax) - Math.min(0, vMin) || Math.abs(vMax) || 1;
    const vHi = Math.max(0, vMax) + vRange * 0.18;
    const vLo = Math.min(0, vMin) - (vMin < 0 ? vRange * 0.12 : 0);
    const rMax = lnVals.length ? Math.max(...lnVals) : 0;
    const rMin = lnVals.length ? Math.min(...lnVals) : 0;
    const rRange = Math.max(0, rMax) - Math.min(0, rMin) || Math.abs(rMax) || 1;
    const rHi = Math.max(0, rMax) + rRange * 0.22;
    const rLo = Math.min(0, rMin) - (rMin < 0 ? rRange * 0.18 : 0);
    // 라벨은 실제 렌더 좌표만으로 배치(추정 금지). 값=막대 안쪽 상단(흰색),
    // %=선 위(골드). 값은 항상 막대에, %는 항상 선에 붙어 선이 값을 관통하지 않음.
    const barLabel = (p: any) => {
      const { x = 0, y = 0, width = 0, height = 0, value } = p;
      if (value == null) return null;
      const num = Math.round(Number(value)).toLocaleString();
      const ty = Number(value) < 0 ? y - 7         // 적자 막대: 0선(y) 위 빈 공간
        : height >= 16 ? y + 14                     // 막대 안쪽 상단(낮은 선과 분리)
        : y - 6;                                    // 매우 짧은 막대만 위
      return <text x={x + width / 2} y={ty} textAnchor="middle" fontSize={13} fontWeight={700} fill="#fff">{num}</text>;
    };
    const pctLabel = (p: any) => {
      const { x = 0, y = 0, value } = p;
      if (value == null) return null;
      const ty = y < 22 ? y + 16 : y - 9;           // 선이 상단에 가까우면 아래, 아니면 위
      return <text x={x} y={ty} textAnchor="middle" fontSize={13} fontWeight={700} fill={CHART_GOLD}>{`${value}%`}</text>;
    };
    return (
      <div key={c.t} className="panel" style={{ marginBottom: 0, padding: "10px 8px 4px" }}>
        <div style={{ fontSize: "12pt", fontWeight: 700, marginBottom: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{c.t}
          <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 11 }}> {unitLbl}{hasLine ? ` · ${lnLabel} %` : ""}</span></div>
        <ResponsiveContainer width="100%" height={250}>
          {/* 0 기준선 + 여유 상단 여백(막대값↔%라벨 겹침 완화) */}
          <ComposedChart data={data} margin={{ top: 34, right: hasLine ? 2 : 6, bottom: 4, left: 0 }} barCategoryGap="24%">
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="x" tick={{ fontSize: 11 }} interval={0} />
            <YAxis yAxisId="v" tick={{ fontSize: 10 }} width={58} tickFormatter={amtFmt} domain={[vLo, vHi]} />
            {hasLine && <YAxis yAxisId="r" orientation="right" tick={{ fontSize: 10 }} width={34} tickFormatter={(n) => `${n}%`} domain={[rLo, rHi]} />}
            <ReferenceLine yAxisId="v" y={0} stroke="var(--muted)" strokeWidth={0.5} />
            <Tooltip content={(o) => {
              if (!o.active || !o.payload?.length) return null;
              const d = o.payload[0].payload as { x: string; v: number | null; ln: number | null };
              return (
                <div style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: 6, padding: "6px 9px", fontSize: 12, lineHeight: 1.55 }}>
                  <div style={{ color: "var(--muted)" }}>{d.x}</div>
                  <div style={{ fontWeight: 700 }}>{d.v == null ? "-" : `${d.v.toLocaleString()} ${unitLbl}`}</div>
                  {d.ln != null && <div style={{ color: CHART_GOLD }}>{lnLabel} {d.ln}%</div>}
                </div>
              );
            }} />
            <Bar yAxisId="v" dataKey="v" fill={CHART_NAVY} stroke={CHART_BAR_EDGE} strokeWidth={1}
              minPointSize={3} name={c.t} isAnimationActive={false}>
              <LabelList dataKey="v" content={barLabel} />
            </Bar>
            {hasLine && (
              <Line yAxisId="r" dataKey="ln" stroke={CHART_GOLD} strokeWidth={2} dot name={lnLabel} isAnimationActive={false} connectNulls>
                {/* %=골드. 막대값과 가까운 기간은 선 아래로 내려 겹침 방지(place[]) */}
                <LabelList dataKey="ln" content={pctLabel} />
              </Line>
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    );
  };

  // 다중 막대(그룹/스택) + 선 — 유동자산·부채(+유동비율), NWC 스택(매출채권·재고 ↑ / 매입채무 ↓ + NWC선)
  const renderMulti = (c: ChartDef) => {
    const bars = c.bars || [];
    const l2 = c.line2;
    const l2pct = !!l2?.pct;
    const data = periods.map((p, i) => {
      const row: Record<string, number | string | null> = { x: xLabel(p) };
      bars.forEach((b) => { row[b.key] = b.series[i] == null ? null : Math.round((b.series[i] as number) * 100); });
      if (l2) row.ln2 = l2.series[i] == null ? null : (l2pct ? l2.series[i] : Math.round((l2.series[i] as number) * 100));
      return row;
    }).filter((_, i) => keep[i]);
    const hasLn = !!l2 && data.some((d) => d.ln2 != null);
    const amtFmt = (n: number) => Math.round(n).toLocaleString();
    return (
      <div key={c.t} className="panel" style={{ marginBottom: 0, padding: "10px 8px 4px" }}>
        <div style={{ fontSize: "12pt", fontWeight: 700, marginBottom: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{c.t}
          <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 11 }}> 백만원{hasLn ? ` · ${l2!.label}${l2pct ? " %" : ""}` : ""}</span></div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap", margin: "0 0 2px 2px" }}>
          {bars.map((b) => (
            <span key={b.key} style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 10, color: "var(--muted)" }}>
              <span style={{ width: 9, height: 9, borderRadius: 2, background: b.color }} />{b.label}</span>
          ))}
          {hasLn && <span style={{ display: "inline-flex", alignItems: "center", gap: 4, fontSize: 10, color: "var(--muted)" }}>
            <span style={{ width: 12, height: 2, background: CHART_GOLD }} />{l2!.label}</span>}
        </div>
        <ResponsiveContainer width="100%" height={250}>
          <ComposedChart data={data} margin={{ top: 30, right: hasLn && l2pct ? 2 : 6, bottom: 0, left: 0 }} barCategoryGap="24%">
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="x" tick={{ fontSize: 11 }} interval={0} />
            <YAxis yAxisId="v" tick={{ fontSize: 10 }} width={58} tickFormatter={amtFmt}
              domain={[(min: number) => Math.min(0, min), "auto"]} />
            {hasLn && l2pct && <YAxis yAxisId="r" orientation="right" tick={{ fontSize: 10 }} width={34} tickFormatter={(n) => `${n}%`} />}
            <ReferenceLine yAxisId="v" y={0} stroke="var(--muted)" strokeWidth={0.5} />
            <Tooltip content={(o) => {
              if (!o.active || !o.payload?.length) return null;
              const d = o.payload[0].payload as Record<string, number | string | null>;
              return (
                <div style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: 6, padding: "6px 9px", fontSize: 12, lineHeight: 1.55 }}>
                  <div style={{ color: "var(--muted)" }}>{d.x}</div>
                  {bars.map((b) => (
                    <div key={b.key} style={{ color: b.color }}>{b.label} {d[b.key] == null ? "-" : `${Number(d[b.key]).toLocaleString()} 백만원`}</div>
                  ))}
                  {hasLn && d.ln2 != null && <div style={{ color: CHART_GOLD }}>{l2!.label} {l2pct ? `${d.ln2}%` : `${Number(d.ln2).toLocaleString()} 백만원`}</div>}
                </div>
              );
            }} />
            {bars.map((b) => (
              <Bar key={b.key} yAxisId="v" dataKey={b.key} fill={b.color} stackId={b.stackId} minPointSize={3} name={b.label} isAnimationActive={false} />
            ))}
            {hasLn && (
              <Line yAxisId={l2pct ? "r" : "v"} dataKey="ln2" stroke={CHART_GOLD} strokeWidth={2} dot name={l2!.label} isAnimationActive={false} connectNulls>
                <LabelList dataKey="ln2" position="top" offset={16}
                  formatter={(n) => (n == null ? "" : l2pct ? `${n}%` : Number(n).toLocaleString())}
                  style={{ fontSize: 12, fill: l2pct ? CHART_GOLD : "#fff", fontWeight: 700 }} />
              </Line>
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    );
  };

  if (!sec) return null;
  return (
    <div style={{ marginBottom: 14 }}>
      <div style={{ fontSize: 11, color: "var(--muted)", margin: "0 0 8px" }}>막대=금액(백만원·네이비) · 골드 선=비율(이익률 등) 또는 {lineLbl} 증감률(우축)</div>
      {/* 한 행에 3개 (모바일은 .ca-grid3가 1열로 접음 — 좁은 폭에서 축 라벨 겹침·잘림 방지) */}
      <div className="ca-grid3" style={{ gap: 10 }}>
        {shown.map((c) => (c.bars ? renderMulti(c) : renderChart(c)))}
      </div>
    </div>
  );
}

function FinancialsTab({ ticker }: { ticker: string; name: string }) {
  const [data, setData] = useState<FinancialsData | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);
  const [openG, setOpenG] = useState<Record<string, boolean>>({});     // 펼친 자식 그룹
  const [stmtTab, setStmtTab] = useState<keyof FinancialsData["annual"]>("PL");  // 상단 탭: 손익/재무/현금
  const [period, setPeriod] = useState<"A" | "Q">("A");                 // 하위 탭: 연간/분기
  const [showChg, setShowChg] = useState(true);                        // 증감률(YoY/QoQ) 행 일괄 표시
  const [showCharts, setShowCharts] = useState(true);                  // #1 그래프 토글(접기/펼치기)
  const [pFrom, setPFrom] = useState("");                              // #3 그래프 기간 시작(""=처음)
  const [pTo, setPTo] = useState("");                                  // #3 그래프 기간 끝(""=마지막)
  const [xlBusy, setXlBusy] = useState(false);                         // #4 엑셀 다운로드 진행

  useEffect(() => {
    let alive = true; setBusy(true); setErr(false); setData(null);
    api.financials(ticker)
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setErr(true); })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [ticker]);
  // 그래프 기간 기본값 = 해당 구분의 전체 범위(예: FY23~FY25). 데이터 로드·연간↔분기 전환 시 재설정.
  useEffect(() => {
    if (!data) return;
    const s = period === "A" ? data.annual : data.quarterly;
    const ps = s.PL?.periods || s.BS?.periods || s.CF?.periods || [];
    setPFrom(ps[0] || "");
    setPTo(ps[ps.length - 1] || "");
  }, [data, period]);

  const downloadExcel = () => {
    setXlBusy(true);
    api.financialsExcel(ticker).catch(() => { /* 무시 */ }).finally(() => setXlBusy(false));
  };

  const mwonN = (v: number | null) => v == null ? "—" : Math.round(v * 100).toLocaleString();   // 억원→백만원(×100). 웹은 정수표시(엑셀 셀만 풀정밀도)
  const wonN = (v: number | null) => v == null ? "—" : Math.round(v * 1e8).toLocaleString() + "원";   // 주당이익 — 원(웹 정수표시)
  const pctV = (v: number | null) => v == null ? "—" : `${v.toFixed(1)}%`;
  const chgI = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;

  const renderTable = (kind: "A" | "Q", key: string, st?: FinStatement) => {
    if (!st || !st.periods.length)
      return <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>해당 기간 데이터가 없습니다.</p>;
    const sKey = `${kind}|${key}`;
    const acctW = 40, perW = (100 - acctW) / st.periods.length;   // 폭 고정 → 펼쳐도 숫자 위치 불변
    return (
      <div style={{ border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", tableLayout: "fixed" }}>
            <colgroup>
              <col style={{ width: `${acctW}%` }} />
              {st.periods.map((p) => <col key={p} style={{ width: `${perW}%` }} />)}
            </colgroup>
            <thead>
              <tr style={{ borderBottom: "2px solid var(--border)" }}>
                <th style={{ textAlign: "left", padding: "6px 8px" }}>계정 <span style={{ fontWeight: 400, color: "var(--muted)" }}>(백만원)</span></th>
                {st.periods.map((p) => <th key={p} style={{ textAlign: "right", padding: "6px 8px" }}>{p}</th>)}
              </tr>
            </thead>
            <tbody>
              {st.rows.map((r, ri) => {
                const gkey = `${sKey}|${r.group}`;
                // 접힘 자식 = child면서 **실제 부모 그룹(group)이 있는** 행만. DART/dart-fss(XBRL)는
                // depth로 child를 붙이지만 부모 토글이 없어(group=null) — 그대로 숨기면 계정이 통째로
                // 사라진다(삼성전자 PL 20행 중 17행이 사라져 3개만 뜨던 버그). group 있는 자식만 접는다.
                const collapsible = r.child && r.group != null;
                if (collapsible && !openG[gkey]) return null;   // 접힌(부모 있는) 자식만 숨김
                const open = openG[gkey];
                const indent = collapsible ? 26 : r.derived ? 22 : 8;   // 자식·파생(이익률/EBITDA) 들여쓰기
                const hasChg = !r.pct && r.change.some((c) => c != null);   // 증감률 행 표시 대상
                const showRow = showChg && hasChg;
                const isEps = r.account.replace(/\s/g, "").includes("주당");   // 주당이익=원 단위
                return (
                  <Fragment key={ri}>
                    <tr style={{ borderBottom: showRow ? "none" : "1px solid var(--border)",
                      background: collapsible ? "rgba(127,127,127,0.05)" : undefined }}>
                      <td style={{ padding: "5px 8px", paddingLeft: indent, whiteSpace: "normal", wordBreak: "keep-all",
                        fontWeight: r.bold ? 700 : r.parent ? 600 : 400,
                        color: r.derived ? "var(--muted)" : "inherit", fontStyle: r.pct ? "italic" : "normal" }}>
                        {r.parent && (
                          <button type="button" onClick={() => setOpenG((m) => ({ ...m, [gkey]: !open }))}
                            aria-label={open ? "접기" : "펼치기"}
                            style={{ marginRight: 6, width: 16, height: 16, lineHeight: "14px", textAlign: "center",
                              border: "1px solid var(--border)", borderRadius: 4, background: "transparent",
                              cursor: "pointer", color: "var(--accent)", fontWeight: 700, padding: 0 }}>
                            {open ? "−" : "+"}</button>
                        )}
                        {r.account}
                      </td>
                      {r.values.map((v, ci) => (
                        <td key={ci} style={{ padding: "5px 8px", textAlign: "right", whiteSpace: "nowrap",
                          color: r.derived ? "var(--muted)" : "inherit", fontStyle: r.pct ? "italic" : "normal" }}>
                          {r.pct ? pctV(v) : isEps ? wonN(v) : mwonN(v)}
                        </td>
                      ))}
                    </tr>
                    {/* 증감률(YoY/QoQ) 별도 행 — 음영 처리, 일괄 토글 */}
                    {showRow && (
                      <tr style={{ borderBottom: "1px solid var(--border)", background: "rgba(127,127,127,0.10)" }}>
                        <td style={{ padding: "2px 8px", paddingLeft: indent + 12, fontSize: "10pt",
                          fontStyle: "italic", color: "var(--muted)" }}>{kind === "A" ? "YoY %" : "QoQ %"}</td>
                        {r.change.map((c, ci) => {
                          // 전기≤0·당기<0이면 %가 왜곡(적자전환·적자지속)이라 표시 안 함
                          const pv = r.values[ci - 1], cv = r.values[ci];
                          const bad = c == null || pv == null || (pv as number) <= 0 || (cv as number) < 0;
                          return (
                          <td key={ci} style={{ padding: "2px 8px", textAlign: "right", fontSize: "10pt", fontStyle: "italic",
                            color: bad ? "var(--muted)" : (c as number) >= 0 ? FIN_UP : FIN_DOWN }}>
                            {bad ? "—" : chgI(c as number)}</td>);
                        })}
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
      </div>
    );
  };

  // 재무상태표 보조 분석 — NWC·CCC, Net Debt 계산과정(맨 아래, 빈 행 후). 값=백만원, CCC=일.
  // 차트(FinCharts)의 NWC/CCC·Net Debt와 동일 정의를 표로 명시. 레벨행엔 YoY(연간)/QoQ(분기) 서브행.
  const renderBsCalc = (kind: "A" | "Q", a?: FinStatement, p?: FinStatement) => {
    if (!a || !a.periods.length) return null;
    const P = a.periods, N = P.length;
    const nm2 = (s: string) => s.replace(/\s/g, "");
    const pick = (st: FinStatement | undefined, names: string[]): (number | null)[] => {
      const r = st?.rows.find((x) => names.includes(nm2(x.account)) || (!!x.canon && names.includes(nm2(x.canon))));
      return r?.values ?? Array(N).fill(null);
    };
    const ar = pick(a, ["매출채권및기타유동채권", "매출채권"]);
    const inv = pick(a, ["재고자산"]);
    const ap = pick(a, ["매입채무및기타유동채무", "매입채무"]);
    const std = pick(a, ["단기차입금"]);
    const ltd = pick(a, ["장기차입금"]);
    const cltd = pick(a, ["유동성장기차입금", "유동성장기부채", "유동성사채"]);
    const cash = pick(a, ["현금및현금성자산"]);
    const rev = pick(p, ["매출액", "수익(매출액)", "영업수익"]);
    const cogs = pick(p, ["매출원가"]);
    const Z = (x: number | null) => (x == null ? 0 : x);
    const some = (...xs: (number | null)[]) => xs.some((x) => x != null);
    const nwc = P.map((_, i) => (some(ar[i], inv[i], ap[i]) ? Z(ar[i]) + Z(inv[i]) - Z(ap[i]) : null));
    const dNwc = P.map((_, i) => (i === 0 || nwc[i] == null || nwc[i - 1] == null) ? null : (nwc[i] as number) - (nwc[i - 1] as number));
    const Dd = kind === "A" ? 365 : 365 / 4;     // CCC 일수 기준 — 연간 365, 분기 ≈91
    const day = (n: number | null, d: number | null) => (n == null || d == null || d === 0) ? null : (n / d) * Dd;
    const ccc = P.map((_, i) => {
      const dso = day(ar[i], rev[i]), dio = day(inv[i], cogs[i]), dpo = day(ap[i], cogs[i]);
      return (dso == null || dio == null || dpo == null) ? null : Math.round((dso + dio - dpo) * 10) / 10;
    });
    const netDebt = P.map((_, i) => (some(std[i], ltd[i], cltd[i], cash[i]) ? Z(std[i]) + Z(ltd[i]) + Z(cltd[i]) - Z(cash[i]) : null));
    const yoyOf = (s: (number | null)[]) => s.map((v, i) => {
      const pv = s[i - 1];
      return (i === 0 || v == null || pv == null || (pv as number) <= 0 || (v as number) < 0) ? null : Math.round((v / (pv as number) - 1) * 1000) / 10;
    });
    const acctW = 40, perW = (100 - acctW) / N;
    type Row = { label: string; vals: (number | null)[]; bold?: boolean; day?: boolean; yoy?: boolean };
    const sections: { title: string; rows: Row[] }[] = [
      { title: "NWC · CCC 분석", rows: [
        { label: "매출채권", vals: ar, yoy: true },
        { label: "재고자산", vals: inv, yoy: true },
        { label: "매입채무", vals: ap, yoy: true },
        { label: "NWC (매출채권+재고자산−매입채무)", vals: nwc, bold: true, yoy: true },
        { label: "NWC 증감 (전기대비)", vals: dNwc },
        { label: "CCC (DSO+DIO−DPO, 일)", vals: ccc, day: true },
      ] },
      { title: "Net Debt 분석", rows: [
        { label: "단기차입금", vals: std, yoy: true },
        { label: "장기차입금", vals: ltd, yoy: true },
        { label: "유동성장기차입금", vals: cltd, yoy: true },
        { label: "현금및현금성자산", vals: cash, yoy: true },
        { label: "Net Debt (차입금합계−현금)", vals: netDebt, bold: true, yoy: true },
      ] },
    ];
    const cell = (v: number | null, isDay?: boolean) =>
      v == null ? "—" : isDay ? `${(Math.round(v * 10) / 10).toLocaleString()}일` : mwonN(v);
    return (
      <div style={{ marginTop: 16, border: "1px solid var(--border)", borderRadius: 8, overflow: "hidden" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", tableLayout: "fixed" }}>
          <colgroup><col style={{ width: `${acctW}%` }} />{P.map((pp) => <col key={pp} style={{ width: `${perW}%` }} />)}</colgroup>
          <tbody>
            {sections.map((sec, si) => (
              <Fragment key={si}>
                {si > 0 && <tr style={{ height: 10 }}><td colSpan={N + 1} /></tr>}
                <tr style={{ background: "rgba(127,127,127,0.10)", borderBottom: "1px solid var(--border)" }}>
                  <td colSpan={N + 1} style={{ padding: "6px 8px", fontWeight: 700, color: "var(--accent)" }}>{sec.title} <span style={{ fontWeight: 400, color: "var(--muted)", fontSize: "10pt" }}>(백만원)</span></td>
                </tr>
                {sec.rows.map((rw, ri) => {
                  const chg = rw.yoy ? yoyOf(rw.vals) : null;
                  const showRow = showChg && !!chg && chg.some((c) => c != null);
                  return (
                    <Fragment key={ri}>
                      <tr style={{ borderBottom: showRow ? "none" : "1px solid var(--border)" }}>
                        <td style={{ padding: "5px 8px", fontWeight: rw.bold ? 700 : 400, color: rw.day ? "var(--muted)" : "inherit" }}>{rw.label}</td>
                        {rw.vals.map((v, ci) => (
                          <td key={ci} style={{ padding: "5px 8px", textAlign: "right", whiteSpace: "nowrap", fontWeight: rw.bold ? 700 : 400, color: rw.day ? "var(--muted)" : "inherit" }}>{cell(v, rw.day)}</td>
                        ))}
                      </tr>
                      {showRow && chg && (
                        <tr style={{ borderBottom: "1px solid var(--border)", background: "rgba(127,127,127,0.10)" }}>
                          <td style={{ padding: "2px 8px", paddingLeft: 20, fontSize: "10pt", fontStyle: "italic", color: "var(--muted)" }}>{kind === "A" ? "YoY %" : "QoQ %"}</td>
                          {chg.map((c, ci) => {
                            const pv = rw.vals[ci - 1], cv = rw.vals[ci];
                            const bad = c == null || pv == null || (pv as number) <= 0 || (cv as number) < 0;
                            return <td key={ci} style={{ padding: "2px 8px", textAlign: "right", fontSize: "10pt", fontStyle: "italic", color: bad ? "var(--muted)" : (c as number) >= 0 ? FIN_UP : FIN_DOWN }}>{bad ? "—" : chgI(c as number)}</td>;
                          })}
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    );
  };

  if (busy && !data) return <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>재무제표 불러오는 중…</p>;
  const hasAnnual = data && FIN_STMTS.some(([k]) => data.annual[k]?.periods.length);
  if (err || !data || !hasAnnual)
    return <p style={{ color: "var(--muted)", fontSize: 13, margin: 0 }}>재무 데이터가 아직 없습니다. (다음 보고서 마감일에 자동 수집)</p>;

  const src = period === "A" ? data.annual : data.quarterly;
  const chartPeriods = src.PL?.periods || src.BS?.periods || src.CF?.periods || [];
  return (
    <>
      {/* 연간 / 분기 — 차트·표 공통 기간 선택 + 엑셀 다운로드 */}
      <div style={{ display: "flex", gap: 6, marginBottom: 12, alignItems: "center", flexWrap: "wrap" }}>
        {([["A", "연간 (YoY %)"], ["Q", "분기 (QoQ %)"]] as const).map(([k, lbl]) => (
          <button key={k} type="button" onClick={() => setPeriod(k)}
            style={{ fontSize: 13, fontWeight: period === k ? 700 : 400, padding: "6px 16px", borderRadius: 8, cursor: "pointer",
              border: `1px solid ${period === k ? "#4f8ff5" : "var(--border)"}`,
              background: period === k ? "rgba(79,143,245,0.16)" : "transparent",
              color: period === k ? "#4f8ff5" : "var(--muted)" }}>{lbl}</button>
        ))}
        {/* #4 재무제표 전체 엑셀 다운로드 */}
        <button type="button" className="ghost sm" onClick={downloadExcel} disabled={xlBusy}
          style={{ marginLeft: "auto", fontSize: 12, padding: "6px 14px", display: "inline-flex", alignItems: "center", gap: 6 }}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          {xlBusy ? "내려받는 중…" : "엑셀 다운로드"}
        </button>
      </div>
      {/* 손익계산서 / 재무상태표 / 현금흐름표 — 탭 선택에 따라 차트·표가 함께 바뀜 */}
      <div style={{ display: "flex", gap: 2, borderBottom: "2px solid var(--border)", marginBottom: 12, flexWrap: "wrap", alignItems: "center" }}>
        {FIN_STMTS.map(([k, l]) => (
          <button key={k} type="button" onClick={() => setStmtTab(k)}
            style={{ fontSize: 14, fontWeight: stmtTab === k ? 700 : 400, padding: "8px 16px", border: 0,
              background: "transparent", cursor: "pointer", color: stmtTab === k ? "#4f8ff5" : "var(--muted)",
              borderBottom: stmtTab === k ? "2px solid #4f8ff5" : "2px solid transparent", marginBottom: -2 }}>{l}</button>
        ))}
        <button type="button" className="ghost sm" onClick={() => setShowChg((v) => !v)}
          style={{ marginLeft: "auto", marginBottom: 4, fontSize: 12, padding: "5px 12px" }}>
          {showChg ? "▾ 증감률 행 접기" : "▸ 증감률 행 펼치기"}
        </button>
      </div>
      {/* #1 그래프 토글 + #3 그래프 기간(연도/분기) 사용자 지정 */}
      <div style={{ display: "flex", gap: 10, marginBottom: 10, alignItems: "center", flexWrap: "wrap" }}>
        <button type="button" className="ghost sm" onClick={() => setShowCharts((v) => !v)}
          style={{ fontSize: 12, padding: "5px 12px" }}>
          {showCharts ? "▾ 그래프 접기" : "▸ 그래프 펼치기"}
        </button>
        {showCharts && chartPeriods.length > 1 && (
          <div style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, color: "var(--muted)" }}>
            <span>그래프 기간</span>
            <select value={pFrom} onChange={(e) => setPFrom(e.target.value)} style={{ fontSize: 12, padding: "3px 6px" }}>
              {chartPeriods.map((p) => <option key={p} value={p}>{periodLabel(p, period === "Q")}</option>)}
            </select>
            <span>~</span>
            <select value={pTo} onChange={(e) => setPTo(e.target.value)} style={{ fontSize: 12, padding: "3px 6px" }}>
              {chartPeriods.map((p) => <option key={p} value={p}>{periodLabel(p, period === "Q")}</option>)}
            </select>
            {(pFrom !== chartPeriods[0] || pTo !== chartPeriods[chartPeriods.length - 1]) && (
              <button type="button" className="ghost sm"
                onClick={() => { setPFrom(chartPeriods[0]); setPTo(chartPeriods[chartPeriods.length - 1]); }}
                style={{ fontSize: 11, padding: "2px 8px" }}>전체 기간</button>
            )}
          </div>
        )}
      </div>
      {/* 선택 재무제표의 지표별 차트(토글·기간 적용) → 상세 표 */}
      {showCharts && <FinCharts src={src} quarterly={period === "Q"} stmt={stmtTab} from={pFrom} to={pTo} />}
      {renderTable(period, stmtTab, src[stmtTab])}
      {stmtTab === "BS" && renderBsCalc(period, src.BS, src.PL)}
    </>
  );
}

// Seeking-Alpha식 가격 헤더 — 기업명·티커 + 현재가·등락(절대/%)·종가일 + 거래소·통화.
function StockHeader({ sym, name, market }: { sym: string; name: string; market?: string }) {
  const [d, setD] = useState<SymbolDetail | null>(null);
  useEffect(() => {
    let alive = true; setD(null);
    api.symbolDetail(sym, "1mo").then((x) => { if (alive) setD(x); }).catch(() => { /* 무시 */ });
    return () => { alive = false; };
  }, [sym]);
  const L = d?.last;
  const pct = L?.change_pct ?? null;
  const abs = (L?.close != null && pct != null) ? L.close - L.close / (1 + pct / 100) : null;
  const up = (pct ?? 0) >= 0;
  const dateStr = L?.date ? L.date.replace(/-/g, ".") : "";
  return (
    <div style={{ marginBottom: 18, paddingBottom: 14, borderBottom: "1px solid var(--border)" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
        <span style={{ fontSize: 30, fontWeight: 800, letterSpacing: "-0.01em" }}>
          {name} <span style={{ color: "var(--muted)", fontWeight: 600 }}>- {sym}</span>
        </span>
      </div>
      <div style={{ color: "var(--muted)", fontSize: 13, margin: "2px 0 10px" }}>Latest Stock Analysis</div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12, flexWrap: "wrap" }}>
        <span style={{ fontSize: 34, fontWeight: 800 }}>{L?.close != null ? `${L.close.toLocaleString()}원` : "—"}</span>
        {pct != null && (
          <span style={{ fontSize: 18, fontWeight: 700, color: up ? FIN_UP : FIN_DOWN }}>
            {up ? "▲" : "▼"} {abs != null ? Math.abs(Math.round(abs)).toLocaleString() : "—"} ({up ? "+" : ""}{pct.toFixed(2)}%)
          </span>
        )}
        {dateStr && <span style={{ color: "var(--muted)", fontSize: 13 }}>{dateStr} 종가</span>}
      </div>
      <div style={{ color: "var(--muted)", fontSize: 12, marginTop: 6 }}>{market || "KRX"} | ₩ KRW</div>
    </div>
  );
}

export default function Home() {
  const [listings, setListings] = useState<SymbolListing[]>([]);
  // 선택 종목은 URL ?symbol로 관리 — 산업분석 트리맵·Peer Analysis 클릭이 이 값을 바꿔 종목 전환
  const [params, setParams] = useSearchParams();
  const sym = params.get("symbol") || "247540";       // 기본: 에코프로비엠
  const setSym = (s: string) => setParams({ symbol: s });
  const [q, setQ] = useState("");
  const [tab, setTab] = useState<Tab>("Summary");

  useEffect(() => {
    api.marketListings().then((d) => setListings(d.listings || [])).catch(() => { /* 무시 */ });
  }, []);

  const name = listings.find((l) => l.symbol === sym)?.name || sym;
  const market = listings.find((l) => l.symbol === sym)?.market;
  const qt = q.trim();
  const matches = qt
    ? listings.filter((l) => stockSearchMatch(l.name, l.symbol, qt)).slice(0, 8)
    : [];

  const Panel = ({ title, children }: { title: string; children: React.ReactNode }) => (
    <div className="panel"><h3 style={{ marginTop: 0 }}>{title}</h3>{children}</div>
  );

  return (
    <div className="dashboard-fullwidth mod-std">
      {/* 최상단 검색창 */}
      <div style={{ position: "relative", maxWidth: 520, marginBottom: 14 }}>
        <input value={q} onChange={(e) => setQ(e.target.value)}
          placeholder="🔍  종목 검색 (기업명 · 종목코드)"
          style={{ width: "100%", fontSize: "12pt", padding: "10px 12px" }} />
        {matches.length > 0 && (
          <ul style={{ position: "absolute", top: "100%", left: 0, right: 0, zIndex: 30, listStyle: "none",
            margin: "4px 0 0", padding: 4, background: "var(--panel)", border: "1px solid var(--border)",
            borderRadius: 8, boxShadow: "0 8px 24px rgba(0,0,0,0.35)", maxHeight: 320, overflowY: "auto" }}>
            {matches.map((m) => (
              <li key={m.symbol} onMouseDown={() => { setSym(m.symbol); setQ(""); }}
                style={{ padding: "8px 10px", cursor: "pointer", fontSize: "12pt", borderRadius: 6,
                  display: "flex", justifyContent: "space-between", gap: 10 }}
                onMouseEnter={(e) => (e.currentTarget.style.background = "var(--accent-soft)")}
                onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
                <span style={{ fontWeight: 600 }}>{m.name}</span>
                <span style={{ color: "var(--muted)" }}>{m.symbol} · {m.market}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Summary에선 아래 StockHeader가 기업명/가격을 크게 보여주므로 상단 h1 생략 */}
      {tab !== "Summary" && (
        <h1>{name} <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 16 }}>{sym}</span></h1>
      )}

      {/* 하위탭 */}
      <div style={{ display: "flex", gap: 2, borderBottom: "2px solid var(--border)", margin: "8px 0 16px", flexWrap: "wrap" }}>
        {TABS.map((t) => (
          <button key={t} type="button" onClick={() => setTab(t)}
            style={{ fontSize: 18, fontWeight: 700, padding: "11px 20px", border: 0,
              background: "transparent", cursor: "pointer", color: tab === t ? "#4f8ff5" : "var(--muted)",
              borderBottom: tab === t ? "2px solid #4f8ff5" : "2px solid transparent", marginBottom: -2 }}>{t}</button>
        ))}
      </div>

      {tab === "Summary" && (
        <>
          <StockHeader sym={sym} name={name} market={market} />
          {/* 기업 개요(좌·절반) | 주가 추이(우·절반) */}
          <div className="ca-grid2" style={{ gap: 16, marginBottom: 18 }}>
            <div className="panel" style={{ marginBottom: 0 }}>
              <h3 style={{ marginTop: 0 }}>기업 개요</h3>
              <ProfileBlock ticker={sym} name={name} />
            </div>
            <div className="panel" style={{ marginBottom: 0 }}>
              <h3 style={{ marginTop: 0 }}>주가 추이 <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 12 }}>(캔들 · MA · 급등락 ±10%)</span></h3>
              <CompanyPriceChart ticker={sym} />
            </div>
          </div>
          {/* 좌측=컨센서스만 (전체폭). Ratings by Mystock 요약 패널은 제거(추후 업데이트 예정). */}
          <div style={{ marginBottom: 18 }}>
            <CompanyReport ticker={sym} name={name} />
          </div>
          <Panel title="실시간 뉴스"><SectorNewsPanel ticker={sym} name={name} /></Panel>
        </>
      )}

      {tab === "Ratings by Mystock" && <Panel title={`Ratings by Mystock · ${name} (${sym})`}><OpinionBoard ticker={sym} name={name} /></Panel>}
      {tab === "Peer Analysis" && <Panel title="경쟁사 비교 (Peer Analysis)"><PeerAnalysis ticker={sym} /></Panel>}
      {tab === "Estimates" && <CompanyReport ticker={sym} name={name} mode="estimates" />}
      {tab === "News" && <Panel title="News"><SectorNewsPanel ticker={sym} name={name} /></Panel>}
      {tab === "Stock Price" && <StockDashboard symbol={sym} hideSearch />}
      {tab === "Financials" && <Panel title="Financials"><FinancialsTab ticker={sym} name={name} /></Panel>}
    </div>
  );
}

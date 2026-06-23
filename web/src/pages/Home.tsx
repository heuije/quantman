import { Fragment, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  ComposedChart, Bar, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid, LabelList,
} from "recharts";
import { api } from "../api";
import type { SymbolListing, CompanyProfile, SymbolDetail, FinancialsData, FinStatement } from "../types";
import StockDashboard from "./StockDashboard";
import { CompanyReport, OpinionBoard, SectorNewsPanel, CompanyPriceChart, PeerAnalysis } from "./IndustryAnalysis";

// HOME(개별 기업 분석) — 시킹알파식. 최상단 검색 + 하위탭. 종목 1개 중심.
const TABS = ["Summary", "Ratings by Mystock", "Stock Price", "Peer Analysis", "Financials", "Valuation & Consensus", "News"] as const;
type Tab = typeof TABS[number];

// 기업 개요 — 저장본(company_profiles, 분기 갱신) + 검색 이름
function ProfileBlock({ ticker, name }: { ticker: string; name: string }) {
  const [p, setP] = useState<CompanyProfile | null>(null);
  const [d, setD] = useState<SymbolDetail | null>(null);
  useEffect(() => {
    let alive = true; setP(null); setD(null);
    // 시총=shares×현재가, 주요사업=business 모두 /profile이 제공 → 종목무관 산업표 하드코딩 폴백 제거(C6).
    api.companyProfile(ticker).then((x) => { if (alive) setP(x); }).catch(() => { /* 무시 */ });
    api.symbolDetail(ticker, "1y").then((x) => { if (alive) setD(x); }).catch(() => { /* 무시 */ });
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
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
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
const CHART_NAVY = "#0a1a30", CHART_GOLD = "#d4a738";   // 막대=웹앱 최상단 딥 네이비(--navy-950)·선=하이라이트 골드
const CHART_BAR_EDGE = "#3a5283";                        // 다크 카드 위 막대 외곽선(가시성 유지)
type SeriesArr = (number | null)[];
// v=금액(억원, 표시는 ×100=백만원) · mg=해당 계정 비율(%) 선 · mgLabel=선 이름(부채비율/ROE/마진 등)
interface ChartDef { t: string; v: SeriesArr; mg?: SeriesArr; mgLabel?: string; yoy?: SeriesArr; }

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
    st?.rows.find((r) => names.includes(norm(r.account)));
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
  const yoy = (s: SeriesArr): SeriesArr => s.map((v, i) =>
    (i === 0 || v == null || s[i - 1] == null || s[i - 1] === 0 || ((v < 0) !== ((s[i - 1] as number) < 0)))
      ? null : Math.round((v / (s[i - 1] as number) - 1) * 1000) / 10);

  const asset = vals(bs, ["자산", "자산총계"]), liab = vals(bs, ["부채", "부채총계"]), eq = vals(bs, ["자본", "자본총계"]);
  const borrow = sumS(vals(bs, ["단기차입금"]), vals(bs, ["장기차입금"]), vals(bs, ["유동성장기부채"]));
  const netDebt = subS(borrow, vals(bs, ["현금및현금성자산"]));
  const opCf = vals(cf, ["영업활동으로인한현금흐름", "영업활동현금흐름"]);
  const invCf = vals(cf, ["투자활동으로인한현금흐름", "투자활동현금흐름"]);
  const finCf = vals(cf, ["재무활동으로인한현금흐름", "재무활동현금흐름"]);

  // 차트 = 금액 막대(네이비) + 해당 계정 비율 선(골드, 우축). PL 이익률·BS 부채비율/ROE/차입금비율을 선으로.
  const SECTIONS: { key: keyof FinancialsData["annual"]; title: string; charts: ChartDef[] }[] = [
    { key: "PL", title: "손익계산서", charts: [
      { t: "매출액", v: vals(pl, ["매출액"]), yoy: chg(pl, ["매출액"]) },
      { t: "매출총이익", v: vals(pl, ["매출총이익"]), mg: vals(pl, ["매출총이익률(%)"]), mgLabel: "매출총이익률", yoy: chg(pl, ["매출총이익"]) },
      { t: "영업이익", v: vals(pl, ["영업이익"]), mg: vals(pl, ["영업이익률(%)"]), mgLabel: "영업이익률", yoy: chg(pl, ["영업이익"]) },
      { t: "당기순이익", v: vals(pl, ["당기순이익"]), mg: vals(pl, ["당기순이익률(%)"]), mgLabel: "순이익률", yoy: chg(pl, ["당기순이익"]) },
      { t: "EBITDA", v: vals(pl, ["EBITDA"]), mg: vals(pl, ["EBITDAMargin(%)"]), mgLabel: "EBITDA%", yoy: chg(pl, ["EBITDA"]) },
    ] },
    { key: "BS", title: "재무상태표", charts: [
      { t: "자산총계", v: asset, yoy: yoy(asset) },
      { t: "부채총계", v: liab, mg: ratio(liab, eq), mgLabel: "부채비율", yoy: yoy(liab) },
      { t: "자본총계", v: eq, mg: ratio(vals(pl, ["당기순이익"]), eq), mgLabel: "ROE", yoy: yoy(eq) },
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

  // 선택된 재무제표 탭(stmt)의 섹션만. 표시 기간에 값이 전혀 없는 차트는 제외.
  const sec = SECTIONS.find((s) => s.key === stmt);
  const shown = sec ? sec.charts.filter((c) => c.v.some((x, i) => x != null && keep[i])) : [];

  // 금액=막대(네이비, 좌축·백만원) + 골드 선(우축): 이익률 있으면 마진%, 없으면 YoY/QoQ 증감률. 막대는 0 기준.
  const renderChart = (c: ChartDef) => {
    const useMg = !!c.mg && c.mg.some((x, i) => x != null && keep[i]);   // 이익률/비율 보유
    const lnLabel = useMg ? (c.mgLabel || "비율") : lineLbl;             // 마진% 또는 YoY%/QoQ%
    const data = periods.map((p, i) => ({ x: xLabel(p),
      v: c.v[i] == null ? null : Math.round((c.v[i] as number) * 100),   // 억원 → 백만원
      ln: (useMg ? c.mg?.[i] : c.yoy?.[i]) ?? null }))
      .filter((_, i) => keep[i]);
    const hasLine = data.some((d) => d.ln != null);
    const amtFmt = (n: number) => Math.round(n).toLocaleString();
    return (
      <div key={c.t} className="panel" style={{ marginBottom: 0, padding: "10px 8px 4px" }}>
        <div style={{ fontSize: "12pt", fontWeight: 700, marginBottom: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{c.t}
          <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 11 }}> 백만원{hasLine ? ` · ${lnLabel} %` : ""}</span></div>
        <ResponsiveContainer width="100%" height={230}>
          {/* barCategoryGap=24% → 막대 폭 ≈ 슬롯 76%(최대폭의 80% 수준) */}
          <ComposedChart data={data} margin={{ top: 18, right: hasLine ? 2 : 6, bottom: 0, left: 0 }} barCategoryGap="24%">
            <CartesianGrid stroke="var(--border)" strokeDasharray="3 3" vertical={false} />
            <XAxis dataKey="x" tick={{ fontSize: 11 }} interval={0} />
            <YAxis yAxisId="v" tick={{ fontSize: 10 }} width={58} tickFormatter={amtFmt}
              domain={[(min: number) => Math.min(0, min), "auto"]} />
            {hasLine && <YAxis yAxisId="r" orientation="right" tick={{ fontSize: 10 }} width={34} tickFormatter={(n) => `${n}%`} />}
            <Tooltip content={(o) => {
              if (!o.active || !o.payload?.length) return null;
              const d = o.payload[0].payload as { x: string; v: number | null; ln: number | null };
              return (
                <div style={{ background: "var(--panel)", border: "1px solid var(--border)", borderRadius: 6, padding: "6px 9px", fontSize: 12, lineHeight: 1.55 }}>
                  <div style={{ color: "var(--muted)" }}>{d.x}</div>
                  <div style={{ fontWeight: 700 }}>{d.v == null ? "-" : `${d.v.toLocaleString()} 백만원`}</div>
                  {d.ln != null && <div style={{ color: CHART_GOLD }}>{lnLabel} {d.ln}%</div>}
                </div>
              );
            }} />
            <Bar yAxisId="v" dataKey="v" fill={CHART_NAVY} stroke={CHART_BAR_EDGE} strokeWidth={1} name={c.t} isAnimationActive={false}>
              <LabelList dataKey="v" position="top" formatter={(n) => (n == null ? "" : Math.round(Number(n)).toLocaleString())}
                style={{ fontSize: 13, fill: "#fff", fontWeight: 700 }} />
            </Bar>
            {hasLine && (
              <Line yAxisId="r" dataKey="ln" stroke={CHART_GOLD} strokeWidth={2} dot name={lnLabel} isAnimationActive={false} connectNulls>
                <LabelList dataKey="ln" position="top" formatter={(n) => (n == null ? "" : `${n}%`)} style={{ fontSize: 14, fill: "#fff", fontWeight: 700 }} />
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
      {/* 한 행에 3개 */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 10 }}>
        {shown.map(renderChart)}
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

  const eokN = (v: number | null) => v == null ? "—" : Math.round(v).toLocaleString();
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
                <th style={{ textAlign: "left", padding: "6px 8px" }}>계정 <span style={{ fontWeight: 400, color: "var(--muted)" }}>(억원)</span></th>
                {st.periods.map((p) => <th key={p} style={{ textAlign: "right", padding: "6px 8px" }}>{p}</th>)}
              </tr>
            </thead>
            <tbody>
              {st.rows.map((r, ri) => {
                const gkey = `${sKey}|${r.group}`;
                if (r.child && !openG[gkey]) return null;        // 접힌 자식은 숨김
                const open = openG[gkey];
                const indent = r.child ? 26 : r.derived ? 22 : 8;   // 자식·파생(이익률/EBITDA) 들여쓰기
                const hasChg = !r.pct && r.change.some((c) => c != null);   // 증감률 행 표시 대상
                const showRow = showChg && hasChg;
                return (
                  <Fragment key={ri}>
                    <tr style={{ borderBottom: showRow ? "none" : "1px solid var(--border)",
                      background: r.child ? "rgba(127,127,127,0.05)" : undefined }}>
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
                          {r.pct ? pctV(v) : eokN(v)}
                        </td>
                      ))}
                    </tr>
                    {/* 증감률(YoY/QoQ) 별도 행 — 음영 처리, 일괄 토글 */}
                    {showRow && (
                      <tr style={{ borderBottom: "1px solid var(--border)", background: "rgba(127,127,127,0.10)" }}>
                        <td style={{ padding: "2px 8px", paddingLeft: indent + 12, fontSize: "10pt",
                          fontStyle: "italic", color: "var(--muted)" }}>{kind === "A" ? "YoY %" : "QoQ %"}</td>
                        {r.change.map((c, ci) => (
                          <td key={ci} style={{ padding: "2px 8px", textAlign: "right", fontSize: "10pt", fontStyle: "italic",
                            color: c == null ? "var(--muted)" : c >= 0 ? FIN_UP : FIN_DOWN }}>
                            {c == null ? "—" : chgI(c)}</td>
                        ))}
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
    ? listings.filter((l) => l.name.includes(qt) || l.symbol.includes(qt)).slice(0, 8)
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
          style={{ width: "100%", fontSize: 14, padding: "10px 12px" }} />
        {matches.length > 0 && (
          <ul style={{ position: "absolute", top: "100%", left: 0, right: 0, zIndex: 30, listStyle: "none",
            margin: "4px 0 0", padding: 4, background: "var(--panel)", border: "1px solid var(--border)",
            borderRadius: 8, boxShadow: "0 8px 24px rgba(0,0,0,0.35)", maxHeight: 320, overflowY: "auto" }}>
            {matches.map((m) => (
              <li key={m.symbol} onMouseDown={() => { setSym(m.symbol); setQ(""); }}
                style={{ padding: "8px 10px", cursor: "pointer", fontSize: 13, borderRadius: 6,
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
            style={{ fontSize: 20, fontWeight: tab === t ? 700 : 400, padding: "11px 20px", border: 0,
              background: "transparent", cursor: "pointer", color: tab === t ? "#4f8ff5" : "var(--muted)",
              borderBottom: tab === t ? "2px solid #4f8ff5" : "2px solid transparent", marginBottom: -2 }}>{t}</button>
        ))}
      </div>

      {tab === "Summary" && (
        <>
          <StockHeader sym={sym} name={name} market={market} />
          {/* 기업 개요(좌·절반) | 주가 추이(우·절반) */}
          <div className="ca-grid2" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, alignItems: "stretch", marginBottom: 18 }}>
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
      {tab === "Valuation & Consensus" && <CompanyReport ticker={sym} name={name} />}
      {tab === "News" && <Panel title="뉴스"><SectorNewsPanel ticker={sym} name={name} /></Panel>}
      {tab === "Stock Price" && <StockDashboard symbol={sym} hideSearch />}
      {tab === "Financials" && <Panel title="Financials"><FinancialsTab ticker={sym} name={name} /></Panel>}
    </div>
  );
}

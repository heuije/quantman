import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { SymbolListing, CompanyProfile, SymbolDetail, FinancialsData, FinStatement } from "../types";
import StockDashboard from "./StockDashboard";
import { CompanyReport, OpinionBoard, RatingsSummary, SectorNewsPanel, CompanyPriceChart, PeerAnalysis } from "./IndustryAnalysis";

// HOME(개별 기업 분석) — 시킹알파식. 최상단 검색 + 하위탭. 종목 1개 중심.
const TABS = ["Summary", "Ratings by Mystock", "Stock Price", "Peer Analysis", "Financials", "Valuation & Consensus", "News"] as const;
type Tab = typeof TABS[number];

// 기업 개요 — 저장본(company_profiles, 분기 갱신) + 검색 이름
function ProfileBlock({ ticker, name }: { ticker: string; name: string }) {
  const [p, setP] = useState<CompanyProfile | null>(null);
  const [d, setD] = useState<SymbolDetail | null>(null);
  const [indCap, setIndCap] = useState<number | null>(null);   // 산업표 시총(폴백)
  const [indBiz, setIndBiz] = useState<string>("");            // 산업표 주요제품(폴백)
  useEffect(() => {
    let alive = true; setP(null); setD(null); setIndCap(null); setIndBiz("");
    api.companyProfile(ticker).then((x) => { if (alive) setP(x); }).catch(() => { /* 무시 */ });
    api.symbolDetail(ticker, "1y").then((x) => { if (alive) setD(x); }).catch(() => { /* 무시 */ });
    // 시총·주요사업 폴백 — 산업표(라이브)의 cap·product. /profile 신규필드가 없어도 즉시 표시.
    api.industryDetail("2차전지")
      .then((x) => {
        const c = (x.companies || []).find((co) => co.ticker === ticker);
        if (alive) { setIndCap(c?.cap ?? null); setIndBiz(c?.product || ""); }
      })
      .catch(() => { /* 무시 */ });
    return () => { alive = false; };
  }, [ticker]);
  const num = (v: number | null | undefined) => v == null ? "—" : v.toLocaleString();
  // 시가총액 — 1조 이상은 조, 그 미만은 억 단위
  const capFmt = (v: number | null) => v == null ? "—"
    : v >= 1e12 ? `${(v / 1e12).toFixed(2)}조` : `${Math.round(v / 1e8).toLocaleString()}억`;
  const L = d?.last;
  // /profile shares × 현재가(재시작 후·임의 종목) 우선, 없으면 산업표 cap 폴백
  const cap = (L?.close != null && p?.shares) ? L.close * p.shares : indCap;
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
        {row("주요 사업", p?.business || indBiz || "—")}
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

function FinancialsTab({ ticker }: { ticker: string; name: string }) {
  const [data, setData] = useState<FinancialsData | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(false);
  const [openG, setOpenG] = useState<Record<string, boolean>>({});     // 펼친 자식 그룹
  const [stmtTab, setStmtTab] = useState<keyof FinancialsData["annual"]>("PL");  // 상단 탭: 손익/재무/현금
  const [period, setPeriod] = useState<"A" | "Q">("A");                 // 하위 탭: 연간/분기

  useEffect(() => {
    let alive = true; setBusy(true); setErr(false); setData(null);
    api.financials(ticker)
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setErr(true); })
      .finally(() => { if (alive) setBusy(false); });
    return () => { alive = false; };
  }, [ticker]);

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
                return (
                  <tr key={ri} style={{ borderBottom: "1px solid var(--border)",
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
                    {r.values.map((v, ci) => {
                      const chg = r.change[ci];
                      return (
                        <td key={ci} style={{ padding: "5px 8px", textAlign: "right", whiteSpace: "nowrap",
                          color: r.derived ? "var(--muted)" : "inherit", fontStyle: r.pct ? "italic" : "normal" }}>
                          <div>{r.pct ? pctV(v) : eokN(v)}</div>
                          {!r.pct && chg != null && (
                            <div style={{ fontStyle: "italic", fontSize: "10pt",
                              color: chg >= 0 ? FIN_UP : FIN_DOWN }}>{chgI(chg)}</div>
                          )}
                        </td>
                      );
                    })}
                  </tr>
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
  return (
    <>
      {/* 상단 탭: 손익계산서 / 재무상태표 / 현금흐름표 */}
      <div style={{ display: "flex", gap: 2, borderBottom: "2px solid var(--border)", marginBottom: 10, flexWrap: "wrap" }}>
        {FIN_STMTS.map(([k, l]) => (
          <button key={k} type="button" onClick={() => setStmtTab(k)}
            style={{ fontSize: 14, fontWeight: stmtTab === k ? 700 : 400, padding: "8px 16px", border: 0,
              background: "transparent", cursor: "pointer", color: stmtTab === k ? "#4f8ff5" : "var(--muted)",
              borderBottom: stmtTab === k ? "2px solid #4f8ff5" : "2px solid transparent", marginBottom: -2 }}>{l}</button>
        ))}
      </div>
      {/* 하위 탭: 연간 / 분기 */}
      <div style={{ display: "flex", gap: 6, marginBottom: 12 }}>
        {([["A", "연간 (YoY %)"], ["Q", "분기 (QoQ %)"]] as const).map(([k, lbl]) => (
          <button key={k} type="button" onClick={() => setPeriod(k)}
            style={{ fontSize: 12.5, fontWeight: period === k ? 700 : 400, padding: "5px 14px", borderRadius: 8, cursor: "pointer",
              border: `1px solid ${period === k ? "#4f8ff5" : "var(--border)"}`,
              background: period === k ? "rgba(79,143,245,0.16)" : "transparent",
              color: period === k ? "#4f8ff5" : "var(--muted)" }}>{lbl}</button>
        ))}
      </div>
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
            style={{ fontSize: 14, fontWeight: tab === t ? 700 : 400, padding: "8px 14px", border: 0,
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
          {/* 컨센서스(좌) | Ratings(우) */}
          <div className="ca-grid2" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, alignItems: "stretch", marginBottom: 18 }}>
            <div className="panel" style={{ marginBottom: 0, overflowY: "auto", maxHeight: 520 }}>
              <h3 style={{ marginTop: 0 }}>{name} 컨센서스·기업분석 리포트 요약</h3>
              <CompanyReport ticker={sym} />
            </div>
            <div className="panel" style={{ marginBottom: 0, overflowY: "auto", maxHeight: 520 }}>
              <h3 style={{ marginTop: 0 }}>Ratings by Mystock <span style={{ color: "var(--muted)", fontWeight: 400, fontSize: 12 }}>요약 · 클릭 시 전체</span></h3>
              <RatingsSummary ticker={sym} name={name} onOpen={() => setTab("Ratings by Mystock")} />
            </div>
          </div>
          <Panel title="실시간 뉴스"><SectorNewsPanel /></Panel>
        </>
      )}

      {tab === "Ratings by Mystock" && <Panel title={`Ratings by Mystock · ${name} (${sym})`}><OpinionBoard ticker={sym} name={name} /></Panel>}
      {tab === "Peer Analysis" && <Panel title="경쟁사 비교 (Peer Analysis)"><PeerAnalysis ticker={sym} /></Panel>}
      {tab === "Valuation & Consensus" && <Panel title={`${name} 컨센서스`}><CompanyReport ticker={sym} /></Panel>}
      {tab === "News" && <Panel title="뉴스"><SectorNewsPanel /></Panel>}
      {tab === "Stock Price" && <StockDashboard symbol={sym} hideSearch />}
      {tab === "Financials" && <Panel title="Financials"><FinancialsTab ticker={sym} name={name} /></Panel>}
    </div>
  );
}

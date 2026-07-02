/**
 * 전략 상세 페이지 (Phase 59).
 *
 * /strategies/:id 경로. 4탭:
 *  1. 설정값 — 모든 정의 조회 (read-only 요약 + 빌더에서 수정 link)
 *  2. 버전 — 자동/수동 스냅샷 이력 + 복원
 *  3. 현황 — 적용 기간 + 누적 P&L + 보유 종목
 *  4. 백테스트 내역 — 이 전략으로 실행된 백테스트 목록
 *
 * 사용자 명세 (요청): "모든 설정값 조회 및 수정 / 버전 관리 / 현황".
 * 인라인 수정은 다음 단계에서 BuildTab 통합으로 추가.
 */

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type {
  AccountHandle, BacktestRunSummary, CapabilityMatrix, ExecutionSummary,
  IndicatorInfo, IrBlockSpec, IrStrategyDef, SymbolInfo,
  StrategyRow, StrategyStats, StrategyVersionRow,
} from "../types";
import SentenceTree, { type Catalog } from "../components/SentenceTree";
import AccountPicker from "../components/AccountPicker";
import { accountLabel } from "../lib/accountLabel";
import { dedupeAssetClasses } from "../lib/assetClasses";

type TabKey = "config" | "versions" | "stats" | "backtests";

const TAB_LABEL: Record<TabKey, string> = {
  config: "설정값",
  versions: "버전",
  stats: "현황",
  backtests: "백테스트 내역",
};

const pct = (v: number | null | undefined, sign = true) =>
  v == null ? "—"
    : (sign && v >= 0 ? "+" : "") + v.toFixed(2) + "%";
const dateOnly = (iso?: string | null) => (iso ?? "").slice(0, 10);

export default function StrategyDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const sid = id ? Number(id) : NaN;

  const [strategy, setStrategy] = useState<StrategyRow | null>(null);
  const [stats, setStats] = useState<StrategyStats | null>(null);
  const [versions, setVersions] = useState<StrategyVersionRow[]>([]);
  const [backtests, setBacktests] = useState<BacktestRunSummary[]>([]);
  // P6-4 — 실행 명세 요약(전략 정의 파생). 비치명 — 실패 시 null(섹션 숨김).
  const [execSummary, setExecSummary] = useState<ExecutionSummary | null>(null);
  // P5-4 — 비민감 계좌 핸들·활성 id(로컬앱이 snapshot에 실어 보고). 바인딩 표시·전환용.
  const [accountHandles, setAccountHandles] = useState<AccountHandle[]>([]);
  const [activeAccountIds, setActiveAccountIds] = useState<string[]>([]);
  // 전환(재바인딩) 시 AccountPicker 필터용 — capability 매트릭스 + 심볼→자산군 맵. 둘 다 best-effort.
  const [capabilities, setCapabilities] = useState<CapabilityMatrix | undefined>(undefined);
  const [assetClassBySymbol, setAssetClassBySymbol] = useState<Map<string, string | null>>(new Map());
  // 신호(진입 조건)를 한글 문장으로 렌더하기 위한 카탈로그·종목·지표 메타(SentenceTree가 소비).
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [indicatorCatalog, setIndicatorCatalog] = useState<IndicatorInfo[]>([]);
  const [catalog, setCatalog] = useState<Catalog>(new Map());
  const [tab, setTab] = useState<TabKey>("config");
  const [err, setErr] = useState("");
  const [loaded, setLoaded] = useState(false);

  function loadAll() {
    if (isNaN(sid)) return;
    setErr("");
    Promise.all([
      api.getStrategy(sid),
      api.getStrategyStats(sid).catch(() => null),
      api.listStrategyVersions(sid).catch(() => []),
      api.listStrategyBacktests(sid).catch(() => []),
      // 핸들 로드는 비치명 — 페어링된 로컬앱 없으면 빈 배열(→ "계좌 미선택"·안내 경로).
      api.snapshot().catch(() => null),
      // 실행 명세 요약도 비치명 — 실패 시 null(섹션만 숨김, 페이지는 정상).
      api.executionSummary(sid).catch(() => null),
      // 전환 picker 필터용 — capability 매트릭스 + 종목 카탈로그(자산군 맵). 둘 다 best-effort.
      api.autotradeCapabilities().catch(() => undefined),
      api.symbols().catch(() => null),
      // 신호 문장 렌더용 블록 카탈로그 — 비치명(실패 시 빈 blocks → 신호 패널만 숨김).
      api.irCatalog().catch(() => ({ blocks: [] as IrBlockSpec[] })),
    ])
      .then(([s, st, vs, bs, snap, es, caps, sym, cat]) => {
        setStrategy(s); setStats(st); setVersions(vs); setBacktests(bs);
        setAccountHandles(snap?.payload?.health?.account_handles ?? []);
        setActiveAccountIds(snap?.payload?.health?.active_account_ids ?? []);
        setExecSummary(es);
        setCapabilities(caps);
        setSymbols(sym?.symbols ?? []);
        setIndicatorCatalog(sym?.indicator_catalog ?? []);
        setCatalog(new Map(cat.blocks.map((b) => [b.op, b])));
        setAssetClassBySymbol(
          new Map((sym?.symbols ?? []).map((x) => [x.symbol, x.autotrade_asset_class ?? null])));
      })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setLoaded(true));
  }
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(loadAll, [sid]);

  async function restoreVersion(versionNo: number) {
    if (!confirm(`v${versionNo}으로 복원할까요?\n현재 정의도 자동 새 버전으로 보존됩니다.`)) return;
    try {
      await api.restoreStrategyVersion(sid, versionNo);
      loadAll();
    } catch (e) { setErr((e as Error).message); }
  }

  // 이 전략이 거래하는 4분 자산군(중복 제거·null 제외) — 전환 picker가 적용 불가 계좌를 거른다.
  const assetClasses = useMemo(() => {
    const def = strategy?.definition as IrStrategyDef | undefined;
    return dedupeAssetClasses(def?.universe?.symbols ?? [], assetClassBySymbol);
  }, [strategy, assetClassBySymbol]);

  // P5-4 — 실행 계좌 재바인딩. 선택 핸들의 mode=run_mode·account_id=account_ref로 저장.
  // 정의는 그대로 유지(전환은 계좌만 바꾼다). 실전 승격 confirm은 AccountPicker가 처리.
  // 라이브면 미검증 ack=true(picker가 confirm으로 경고 후 선택) — 게이트가 최종 차단.
  async function rebind(h: AccountHandle) {
    if (!strategy) return;
    try {
      await api.updateStrategy(strategy.id, strategy.definition, h.mode, "ir",
                               h.account_id, h.broker, h.mode === "live");
      loadAll();
    } catch (e) { setErr((e as Error).message); }
  }

  async function demote() {
    if (!strategy) return;
    const label = strategy.run_mode === "live" ? "실전" : "모의";
    if (!confirm(
      `이 전략의 ${label} 자동매매를 정지하고 초안으로 전환할까요?\n` +
      "보유 종목은 로컬앱이 저장된 규칙으로 계속 청산합니다. 정의·버전·백테스트는 보존됩니다.")) return;
    try {
      await api.stopStrategy(strategy.id);
      loadAll();
    } catch (e) { setErr((e as Error).message); }
  }

  async function remove() {
    if (!strategy) return;
    // 삭제 게이트 — 자동매매 중(모의/실전)이면 먼저 정지해야 한다(서버도 409로 차단).
    if (strategy.run_mode !== "draft") {
      setErr("자동매매 중(모의/실전)인 전략은 삭제할 수 없습니다. 먼저 ‘정지’ 후 삭제하세요.");
      return;
    }
    const held = stats?.n_positions ?? 0;
    const heldWarn = held > 0
      ? `\n\n⚠ 이 전략으로 보유 중인 종목 ${held}개가 있습니다. 삭제해도 보유분은 ` +
        "로컬앱이 저장된 규칙으로 청산하지만, 웹에서 추적이 어려워집니다."
      : "";
    if (!confirm(`이 전략을 삭제할까요? 모든 버전·백테스트도 함께 삭제됩니다.${heldWarn}`)) return;
    try {
      await api.deleteStrategy(strategy.id);
      navigate("/strategies");
    } catch (e) { setErr((e as Error).message); }
  }

  if (isNaN(sid)) return <div className="error">잘못된 전략 ID입니다.</div>;
  if (!loaded) return <p className="muted">불러오는 중…</p>;
  if (err) return <div className="error">{err}</div>;
  if (!strategy) return <div className="error">전략을 찾을 수 없습니다.</div>;

  return (
    <div>
      <div className="strategy-detail-head">
        <Link to="/strategies" className="muted small">← 내 전략</Link>
        <h1 className="page-title" style={{ marginBottom: 4 }}>
          {strategy.name}
        </h1>
        <div className="strategy-detail-sub">
          <span className={"sc-badge " + strategy.run_mode}>
            {strategy.run_mode === "live" ? "실전"
              : strategy.run_mode === "paper" ? "모의" : "초안"}
          </span>
          <span className="muted small">
            생성 {dateOnly(strategy.created_at)} · 최근 수정 {dateOnly(strategy.updated_at)}
          </span>
        </div>
      </div>

      <nav className="tabs" style={{ marginTop: 16 }}>
        {(Object.keys(TAB_LABEL) as TabKey[]).map((k) => (
          <button key={k} type="button"
                  className={"tab" + (tab === k ? " active" : "")}
                  onClick={() => setTab(k)}>
            {TAB_LABEL[k]}
            {k === "versions" && versions.length > 0 && (
              <span className="tab-count">{versions.length}</span>
            )}
            {k === "backtests" && backtests.length > 0 && (
              <span className="tab-count">{backtests.length}</span>
            )}
          </button>
        ))}
      </nav>

      {tab === "config" && (strategy.engine === "ir" ? (
        <IrConfigTab strategy={strategy}
                     isFutures={assetClasses.some((ac) => ac.endsWith("_futures"))}
                     catalog={catalog} symbols={symbols} indicatorCatalog={indicatorCatalog}
                     onRemove={remove} onDemote={demote} />
      ) : (
        <LegacyConfigTab runMode={strategy.run_mode} execSummary={execSummary}
                         onRemove={remove} onDemote={demote} />
      ))}
      {tab === "versions" && (
        <VersionsTab versions={versions} backtests={backtests} onRestore={restoreVersion} />
      )}
      {tab === "stats" && (
        <StatsTab
          stats={stats}
          strategy={strategy}
          handles={accountHandles}
          activeIds={activeAccountIds}
          capabilities={capabilities}
          assetClasses={assetClasses}
          onRebind={rebind}
        />
      )}
      {tab === "backtests" && <BacktestsTab backtests={backtests} />}
    </div>
  );
}

/** 설정값 탭 하단 액션바 — 정지(자동매매 중단)·삭제.
 *  자동매매 중(paper/live)이면 삭제를 막고 '정지'를 노출한다(서버 삭제 게이트와 이중 방어). */
function StrategyActionBar({ runMode, onDemote, onRemove, extraLeft }: {
  runMode: string;
  onDemote: () => void;
  onRemove: () => void;
  extraLeft?: ReactNode;
}) {
  const active = runMode !== "draft";
  return (
    <div className="strategy-save-bar">
      {extraLeft}
      <span style={{ flex: 1 }} />
      {active && (
        <button className="stop-btn" onClick={onDemote}>정지 (자동매매 중단)</button>
      )}
      <button className="danger-btn" onClick={onRemove} disabled={active}
              title={active ? "삭제하려면 먼저 정지하세요" : undefined}>
        전략 삭제
      </button>
    </div>
  );
}

// ── 탭 1 (IR): 설정값 조회 + 전략 연구소에서 편집 ─────────────────────────────

const IR_SIZING_LABEL: Record<string, string> = {
  equal_weight: "동일가중", signal_proportional: "신호비례", vol_inverse: "변동성 역가중",
  target_vol: "목표변동성", fixed_weight: "정적 비중", fixed_amount: "종목당 고정금액",
  pct_cash: "자본대비 %",
};
const IR_ENTRY_LABEL: Record<string, string> = {
  on_signal: "이벤트 (신호 참인 날)", scheduled: "정기 리밸런싱", always: "상시 (매일)",
};
const IR_DIR_LABEL: Record<string, string> = {
  long: "롱", short: "숏", long_short: "롱숏중립",
};
const IR_REBALANCE_LABEL: Record<string, string> = {
  daily: "매일", weekly: "매주", monthly: "매월", quarterly: "분기", annual: "매년",
  every_n_days: "N일마다",
};

// query(동사) + study(축×환원) → 분석 유형 한글 요약(빌더 드롭다운 라벨과 동기).
// none(단일 백테스트)이면 null을 돌려 행을 숨긴다.
function summarizeIrAnalysis(def: IrStrategyDef): string | null {
  const query = def.query ?? "simulate";
  const st = def.study;
  if (query === "describe") return "신호값 분포";
  if (query === "relate") return st?.relation_kind === "ic" ? "팩터 IC (예측력)" : "이벤트 분석";
  switch (st?.axis) {
    case "parameter": return "파라미터 민감도";
    case "entity": return "종목별";
    case "label": return "국면별 비교";
    case "time_fold": return "기간 분할 / 워크포워드";
    default: return null;   // none — 단일 백테스트
  }
}

function summarizeIrUniverse(def: IrStrategyDef): string {
  const u = def.universe ?? { kind: "single" };
  const detail = u.screener ? " · 세부조건 적용" : "";
  if (u.kind === "all") return `전체 종목${detail}`;
  const syms = u.symbols ?? [];
  if (syms.length === 0) return "(없음)";
  if (syms.length === 1) return `${syms[0]}${detail}`;
  return `${syms.join(", ")}${detail}`;
}

/** IR 전략의 활성 청산 규칙을 한 줄로. */
function summarizeIrExit(ex: IrStrategyDef["position"]["exit"]): string {
  const parts: string[] = [];
  if (ex?.hold_days != null) parts.push(`보유 ${ex.hold_days}일`);
  if (ex?.take_profit != null) parts.push(`익절 ${ex.take_profit}%`);
  if (ex?.stop_loss != null) parts.push(`손절 ${ex.stop_loss}%`);
  if (ex?.trail_pct != null) parts.push(`트레일링 ${ex.trail_pct}%`);
  if (ex?.trail_atr_mult != null) parts.push(`ATR 트레일링 ×${ex.trail_atr_mult}`);
  if (ex?.condition) parts.push("매도 조건");
  return parts.length ? parts.join(" · ") : "없음 (정기 리밸런싱 교체 또는 무청산)";
}

// 설정값 그룹 헤더 — 라이브(자동매매 실사용)는 골드 강조, 백테스트 가정은 muted.
// (DESIGN.md: 빨강 --up은 방향성 숫자 전용 → 그룹 강조엔 골드 --accent 사용)
function SettingsGroupHeader({ live, title, note }: { live: boolean; title: string; note: string }) {
  return (
    <div style={{
      marginTop: 20, marginBottom: 8, paddingLeft: 11,
      borderLeft: `3px solid ${live ? "var(--accent)" : "var(--navy-700)"}`,
    }}>
      <div style={{
        fontSize: 13, fontWeight: 700, letterSpacing: "0.01em",
        color: live ? "var(--accent-strong)" : "var(--muted)",
      }}>{title}</div>
      <div className="muted small" style={{ marginTop: 3, lineHeight: 1.45 }}>{note}</div>
    </div>
  );
}

// KRX 코스피200 위탁증거금 개시율 ~19.5% — core exec_defaults와 동기·표시용 근사.
// 라이브 실제 계약수는 모델 A(브로커 실시간 주문가능수량)로 산정되므로 이 상수와 무관.
const KOSPI200_MARGIN_RATE = 0.195;

function IrConfigTab({ strategy, isFutures, catalog, symbols, indicatorCatalog, onRemove, onDemote }: {
  strategy: StrategyRow;
  isFutures: boolean;
  catalog: Catalog;
  symbols: SymbolInfo[];
  indicatorCatalog: IndicatorInfo[];
  onRemove: () => void;
  onDemote: () => void;
}) {
  const navigate = useNavigate();
  const def = strategy.definition as IrStrategyDef;
  const p = def.position ?? ({} as IrStrategyDef["position"]);
  const sim = def.simulation ?? {};
  const entry = p.entry ?? ({} as IrStrategyDef["position"]["entry"]);
  const sizing = p.sizing ?? ({} as IrStrategyDef["position"]["sizing"]);
  const analysisSummary = summarizeIrAnalysis(def);

  const runModeLabel = strategy.run_mode === "live" ? "실전"
    : strategy.run_mode === "paper" ? "모의" : null;
  const isAutotrading = strategy.run_mode !== "draft";

  // 사이징은 자동매매에 실제 쓰이는 값으로 표시 — 주식은 정액/정률 한 줄.
  // (선물은 sizing.mode와 무관하게 futures_margin_pct로 사이징 — engine event_buy_qty 동일.
  //  선물은 아래에서 증거금 사용률·레버리지 효과·증거금률 3행으로 개념 분리해 보여준다.)
  const sizingDetail = sizing.mode === "fixed_amount" && sizing.amount_krw != null
    ? `종목당 ${Number(sizing.amount_krw).toLocaleString()}원`
    : sizing.mode === "pct_cash"
      ? `가용 자본의 ${sizing.amount_pct ?? 10}%`
      : (IR_SIZING_LABEL[sizing.mode ?? "equal_weight"] ?? "동일가중");

  // 선물 레버리지 효과 = 증거금 사용률 ÷ 증거금률. 유저는 사용률만 정하고 증거금률은 브로커가 정한다.
  // 사용률이 낮으면(예 20%) 레버리지가 ~1배(거의 무레버리지)임을 드러내는 게 목적.
  const marginPct = sizing.futures_margin_pct ?? 100;
  const leverage = marginPct / 100 / KOSPI200_MARGIN_RATE;
  const marginRatePct = KOSPI200_MARGIN_RATE * 100;

  // 백테스트 비용 가정 — bps 필드는 ExecutionPolicy(def.execution)에 산다(engine execution_summary와 동일 소스).
  // 수수료는 전략이 simulation.commission(소수)을 명시했으면 그 값(×100), 아니면 default bt_commission_bps/100.
  const exec = def.execution ?? undefined;
  const commissionPct = sim.commission != null
    ? sim.commission * 100
    : (exec?.bt_commission_bps ?? 3) / 100;
  const slippagePct = (exec?.bt_slippage_bps ?? 10) / 100;
  const sellTaxPct = (exec?.bt_sell_tax_bps ?? 23) / 100;

  return (
    <div className="strategy-detail-body">
      <p className="muted small">
        전략 연구소(IR)에서 만든 전략입니다. 전체 설정을 조회하고, 연구소에서 신호·진입·청산을 수정하세요.
      </p>

      {/* 🟡 자동매매 실사용 설정 — 실제 라이브 발주를 결정하는 조건·설정값 */}
      <SettingsGroupHeader live title="자동매매 실사용 설정"
        note={isAutotrading
          ? `${runModeLabel} 자동매매로 실행 중 — 아래 설정이 실제 발주를 결정합니다.`
          : "실제 자동매매(모의·실전)에서 발주를 결정하는 설정입니다."} />

      <section className="panel">
        <h4>유니버스</h4>
        <Rule label="대상" v={summarizeIrUniverse(def)} />
      </section>

      {/* 신호(진입 조건) — IR을 한글 문장으로(SentenceTree 재사용·read-only).
          catalog 로딩 전엔 빈칸 picker가 떠 혼란을 주므로 catalog.size>0일 때만 렌더. */}
      {def.signal && (
        <section className="panel" style={{ marginTop: 12 }}>
          <h4>신호 (진입 조건)</h4>
          {catalog.size > 0 ? (
            <div className="st-readonly">
              <SentenceTree node={def.signal} catalog={catalog} symbols={symbols}
                selfIndicators={indicatorCatalog}
                requiredType={catalog.get(def.signal.op)?.out_type ?? "condition"}
                onChange={() => {}} depth={0} />
            </div>
          ) : (
            <p className="muted small">신호 불러오는 중…</p>
          )}
        </section>
      )}

      <section className="panel" style={{ marginTop: 12 }}>
        <h4>진입 · 포지션</h4>
        <Rule label="진입 트리거" v={IR_ENTRY_LABEL[entry.mode ?? "on_signal"] ?? "이벤트"} />
        {entry.mode === "scheduled" && (
          <Rule label="리밸런싱" v={IR_REBALANCE_LABEL[entry.rebalance ?? "monthly"] ?? "매월"} />
        )}
        <Rule label="방향" v={IR_DIR_LABEL[p.direction ?? "long"] ?? "롱"} />
        {isFutures ? (
          <>
            <Rule label="증거금 사용률"
                  v={`${marginPct}% (가용 자본의 ${marginPct}%를 증거금으로 투입)`} />
            <Rule label="레버리지 효과"
                  v={`약 ${leverage.toFixed(1)}배 (사용률 ${marginPct}% ÷ 증거금률 ${marginRatePct.toFixed(1)}%)`} />
            <Rule label="증거금률"
                  v={`약 ${marginRatePct.toFixed(1)}% — 브로커·거래소 결정(유저 미설정), 라이브는 실시간`} />
          </>
        ) : (
          <Rule label="사이징" v={sizingDetail} />
        )}
        {entry.mode !== "on_signal" && entry.top_n != null && (
          <Rule label="상위 N" v={`${entry.top_n}종목`} />
        )}
        {entry.mode !== "on_signal" && entry.top_pct != null && (
          <Rule label="상위 %" v={`${entry.top_pct}%`} />
        )}
      </section>

      <section className="panel" style={{ marginTop: 12 }}>
        <h4>청산</h4>
        <Rule label="규칙" v={summarizeIrExit(p.exit)} />
      </section>

      {/* 🧪 백테스트 전용 가정값 — 라이브 발주에 영향 없음 */}
      <SettingsGroupHeader live={false} title="백테스트 전용 가정값"
        note="과거 데이터 백테스트에만 적용됩니다. 실제 계좌의 수수료·체결가·증거금과 다를 수 있으며, 자동매매 발주에는 영향을 주지 않습니다." />

      <section className="panel">
        <h4>시뮬레이션</h4>
        <Rule label="기간" v={`${sim.start || "전체"} ~ ${sim.end || "전체"}`} />
        <Rule label="초기자본" v={`${(sim.initial_capital ?? 10_000_000).toLocaleString()}원`} />
        <Rule label="체결" v={`지연 ${sim.delay ?? 1}일 · ${sim.fill === "close" ? "당일 종가"
          : sim.fill === "typical" ? "당일 (고+저+종)/3" : "익일 시가"}`} />
        <Rule label="수수료(편도)" v={`${commissionPct.toFixed(3)}%`} />
        <Rule label="슬리피지(편도)" v={`${slippagePct.toFixed(3)}%`} />
        {!isFutures && (
          <Rule label="매도세" v={`${sellTaxPct.toFixed(3)}%`} />
        )}
        {analysisSummary && <Rule label="분석" v={analysisSummary} />}
        {isFutures && (
          <p className="muted small" style={{ marginTop: 8, lineHeight: 1.45 }}>
            ⓘ 선물 레버리지·증거금률 표시는 백테스트용 카탈로그 추정값입니다. 실제 자동매매 계약수는
            증권사가 알려주는 실시간 주문가능수량(모델 A)으로 산정됩니다.
          </p>
        )}
      </section>

      <StrategyActionBar
        runMode={strategy.run_mode}
        onDemote={onDemote}
        onRemove={onRemove}
        extraLeft={
          <button className="apply-btn"
                  onClick={() => navigate(`/lab?edit=${strategy.id}`)}>
            전략 연구소에서 편집 →
          </button>
        }
      />
    </div>
  );
}

// ── 탭 1 (레거시 operand): 안내만 — 편집·백테스트는 전략 연구소(IR) 전용 ───────

function LegacyConfigTab({ runMode, execSummary, onRemove, onDemote }: {
  runMode: string;
  execSummary: ExecutionSummary | null;
  onRemove: () => void;
  onDemote: () => void;
}) {
  return (
    <div className="strategy-detail-body">
      <section className="panel">
        <p className="muted">
          구버전 형식 전략입니다. 전략 연구소에서 새로 만들어 백테스트·자동매매를 진행해 주세요.
        </p>
      </section>
      <ExecutionSummarySection summary={execSummary} />
      <StrategyActionBar runMode={runMode} onDemote={onDemote} onRemove={onRemove} />
    </div>
  );
}

// ── 탭 2: 버전 ────────────────────────────────────────────────────────────────

function VersionsTab({ versions, backtests, onRestore }: {
  versions: StrategyVersionRow[];
  backtests: BacktestRunSummary[];
  onRestore: (versionNo: number) => void;
}) {
  if (versions.length === 0) {
    return <p className="muted">아직 저장된 버전이 없습니다.</p>;
  }
  // 버전별 백테스트 수익률 — version_no 매칭, 같은 버전 다회면 가장 최근 실행.
  const retByVersion = new Map<number, number | null>();
  for (const b of [...backtests].sort((a, z) => (a.created_at < z.created_at ? -1 : 1))) {
    if (b.version_no != null) retByVersion.set(b.version_no, b.metrics?.total_return ?? null);
  }
  return (
    <div className="strategy-detail-body">
      <p className="muted small">
        매 저장마다 자동 스냅샷. 최대 50건 또는 30일까지 보관 — 그 이전 버전은 자동 회전.
        각 버전의 백테스트 수익률을 함께 표시합니다.
      </p>
      <div className="version-list">
        {versions.map((v) => {
          const ret = retByVersion.get(v.version_no);
          return (
            <div key={v.version_no} className="version-row">
              <div className="version-no">v{v.version_no}</div>
              <div className="version-meta">
                <div className="version-name">{v.name}</div>
                <div className="muted small">
                  {dateOnly(v.created_at)} · {labelReason(v.created_reason)}
                </div>
              </div>
              <div className="version-actions">
                {ret != null && (
                  <span className={"sc-stat " + (ret >= 0 ? "pos" : "neg")}
                        title="이 버전으로 실행한 가장 최근 백테스트 누적수익률">
                    백테스트 {ret >= 0 ? "+" : ""}{ret.toFixed(1)}%
                  </span>
                )}
                <button className="ghost sm" onClick={() => onRestore(v.version_no)}>
                  이 버전 적용
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function labelReason(reason: string): string {
  if (reason === "initial") return "최초 생성";
  if (reason === "manual_edit") return "수정";
  if (reason.startsWith("restore_from_v"))
    return `v${reason.slice("restore_from_v".length)} 복원 직전`;
  return reason;
}

// ── 탭 3: 현황 ────────────────────────────────────────────────────────────────

function StatsTab({ stats, strategy, handles, activeIds, capabilities, assetClasses, onRebind }: {
  stats: StrategyStats | null;
  strategy: StrategyRow;
  handles: AccountHandle[];
  activeIds: string[];
  capabilities?: CapabilityMatrix;
  assetClasses: string[];
  onRebind: (h: AccountHandle) => void;
}) {
  // 현황 데이터가 없어도(초안 등) 실행 계좌 섹션은 보여준다 — 바인딩·전환은 stats와 무관.
  if (!stats) {
    return (
      <div className="strategy-detail-body">
        <p className="muted">현황 데이터가 없습니다.</p>
        <AccountBindingSection
          strategy={strategy} handles={handles} activeIds={activeIds}
          capabilities={capabilities} assetClasses={assetClasses} onRebind={onRebind} />
      </div>
    );
  }
  return (
    <div className="strategy-detail-body">
      <p className="muted small" style={{ marginBottom: 14 }}>
        ⓘ 실행 손익·평가금액·보유는 <b>트레이딩</b> 탭에서 확인하세요.
        이 탭은 연동·운용 상태만 보여줍니다.
      </p>

      <section className="panel" style={{ marginTop: 0 }}>
        <h4>운용 모드</h4>
        <Rule label="현재 모드"
              v={strategy.run_mode === "live" ? "실전"
                : strategy.run_mode === "paper" ? "모의" : "초안"} />
        <Rule label="모의 시작" v={dateOnly(stats.paper_started_at) || "—"} />
        <Rule label="실전 시작" v={dateOnly(stats.live_started_at) || "—"} />
        <Rule label="최근 동기화"
              v={stats.last_snapshot_at
                ? new Date(stats.last_snapshot_at).toLocaleString("ko-KR")
                : "—"} />
      </section>

      <AccountBindingSection
        strategy={strategy} handles={handles} activeIds={activeIds}
        capabilities={capabilities} assetClasses={assetClasses} onRebind={onRebind} />

      <p className="muted small" style={{ marginTop: 12 }}>
        ⓘ 종목별 매매 상세는 로컬앱 "주문 내역" 탭에서 확인하세요 (서버에는 요약만 보관).
      </p>
    </div>
  );
}

/** 실행 계좌 섹션 (P5-4) — 바인딩된 계좌(별명·mode 배지) 표시 + "전환"으로 재바인딩.
 *  전환은 AccountPicker(핸들 선택, 실전 confirm 내장)를 열어 선택 핸들로 account_ref를 갱신. */
function AccountBindingSection({ strategy, handles, activeIds, capabilities, assetClasses, onRebind }: {
  strategy: StrategyRow;
  handles: AccountHandle[];
  activeIds: string[];
  capabilities?: CapabilityMatrix;
  assetClasses: string[];
  onRebind: (h: AccountHandle) => void;
}) {
  const [showPicker, setShowPicker] = useState(false);
  const account = accountLabel(strategy.account_ref, handles);

  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <h4>실행 계좌</h4>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        {account ? (
          <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <span style={{ fontWeight: 600 }}>{account.nickname}</span>
            <span className={"sc-badge " + account.mode}>
              {account.mode === "live" ? "실전" : "모의"}
            </span>
          </span>
        ) : (
          <span className="muted">계좌 미선택</span>
        )}
        <span style={{ flex: 1 }} />
        <button className="ghost sm" onClick={() => setShowPicker(true)}>전환</button>
      </div>

      {showPicker && (
        <AccountPicker
          handles={handles}
          activeIds={activeIds}
          currentRef={strategy.account_ref}
          capabilities={capabilities}
          assetClasses={assetClasses}
          onSelect={(h) => { setShowPicker(false); onRebind(h); }}
          onClose={() => setShowPicker(false)}
        />
      )}
    </section>
  );
}

// ── 탭 4: 백테스트 내역 ────────────────────────────────────────────────────────

function BacktestsTab({ backtests }: {
  backtests: BacktestRunSummary[];
}) {
  if (backtests.length === 0) {
    return (
      <div className="strategy-detail-body">
        <p className="muted">이 전략으로 실행된 백테스트가 없습니다.</p>
        <Link to="/lab" className="cta sm">전략 연구소에서 백테스트 →</Link>
      </div>
    );
  }
  return (
    <div className="strategy-detail-body">
      <table className="bt-history-table">
        <thead>
          <tr>
            <th>실행일</th>
            <th>버전</th>
            <th>기간</th>
            <th>초기자본</th>
            <th>총수익률</th>
            <th>MDD</th>
            <th>샤프</th>
          </tr>
        </thead>
        <tbody>
          {backtests.map((b) => {
            const m = b.metrics ?? {};
            const ret = (m.total_return as number | null) ?? null;
            const mdd = (m.max_drawdown as number | null) ?? null;
            const sharpe = (m.sharpe as number | null) ?? null;
            return (
              <tr key={b.id}>
                <td>{new Date(b.created_at).toLocaleString("ko-KR", {
                  year: "2-digit", month: "2-digit", day: "2-digit",
                  hour: "2-digit", minute: "2-digit",
                })}</td>
                <td>{b.version_no != null ? `v${b.version_no}` : "—"}</td>
                <td className="small muted">{b.start ?? "—"} ~ {b.end ?? "—"}</td>
                <td>{b.initial_capital.toLocaleString()}원</td>
                <td className={ret != null && ret >= 0 ? "pos" : ret != null ? "neg" : ""}>
                  {ret != null ? pct(ret * 100) : "—"}
                </td>
                <td>{mdd != null ? pct(mdd * 100, false) : "—"}</td>
                <td>{sharpe != null ? sharpe.toFixed(2) : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── 실행 명세 요약 (P6-4 #2) — "이 전략은 이렇게 매매합니다" ──────────────────────
// core execution_summary 파생(전략 정의에서). 4분류:
//  확정=전략이 정한 값 · 가정=시스템 기본값(백테스트 기준) · 발주시점=실시간 결정 · 미지=사후 확인.
// 가정/미지를 명시해 "이건 실제 계좌 값이 아니라 가정"임을 투명하게 드러낸다(사장님 신뢰 요구).
function ExecutionSummarySection({ summary }: { summary: ExecutionSummary | null }) {
  if (!summary) return null;
  const { confirmed, assumed, at_order, unknown } = summary;
  return (
    <section className="panel">
      <h4>이 전략은 이렇게 매매합니다</h4>

      {confirmed.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div className="small" style={{ fontWeight: 600, marginBottom: 4 }}>확정</div>
          <div className="muted small" style={{ marginBottom: 6 }}>이 전략이 정한 것</div>
          {confirmed.map((e) => <Rule key={e.label} label={e.label} v={e.value} />)}
        </div>
      )}

      {assumed.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div className="small" style={{ fontWeight: 600, marginBottom: 4 }}>가정</div>
          <div className="muted small" style={{ marginBottom: 6 }}>
            아래는 시스템 가정값입니다 (실제 계좌 값과 다를 수 있음) · 백테스트 기준
          </div>
          {assumed.map((e) => (
            <div key={e.label} className="rule-row">
              <span className="rule-label">{e.label}</span>
              <span className="rule-val muted">{e.value}</span>
            </div>
          ))}
        </div>
      )}

      {at_order.length > 0 && (
        <div style={{ marginBottom: 14 }}>
          <div className="small" style={{ fontWeight: 600, marginBottom: 6 }}>발주 시점 결정</div>
          <ul className="muted small" style={{ margin: 0, paddingLeft: 18 }}>
            {at_order.map((x) => <li key={x}>{x}</li>)}
          </ul>
        </div>
      )}

      {unknown.length > 0 && (
        <div>
          <div className="muted small" style={{ fontWeight: 600, marginBottom: 6 }}>사후 확인</div>
          <ul className="muted small" style={{ margin: 0, paddingLeft: 18, opacity: 0.75 }}>
            {unknown.map((x) => <li key={x}>{x}</li>)}
          </ul>
        </div>
      )}
    </section>
  );
}

// ── 공용 ──────────────────────────────────────────────────────────────────────

function Rule({ label, v }: { label: string; v: string }) {
  return (
    <div className="rule-row">
      <span className="rule-label">{label}</span>
      <span className="rule-val">{v}</span>
    </div>
  );
}


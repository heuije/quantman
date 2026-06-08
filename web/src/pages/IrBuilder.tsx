import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, type IrValidation, type IrExplanation } from "../api";
import SentenceTree, { type Catalog } from "../components/SentenceTree";
import EquityChart from "../components/EquityChart";
import {
  SweepChart, SignalDistChart, ICChart, EventStudyChart,
  RankedListChart, ReportCards, DiagnosisPanel, RegressionChart, ExtremizeChart,
} from "../components/ResultCharts";
import MultiSymbolPicker from "../components/MultiSymbolPicker";
import type {
  IndicatorInfo, IrBlockSpec, IrDistribution, IrEventStat, IrExtremizeResult, IrICStat,
  IrNode, IrPartition, IrPortfolioDiagnosis, IrRegressionResult, IrSingleReport,
  IrStrategyDef, IrStrategyResult, StrategyRow, SymbolInfo,
} from "../types";

// research query(신규 동사)는 빌더 폼(simulate 전용)을 왕복 못함 → 컴파일 IR을 직접 실행.
// 판별: select/describe/relate 동사이거나 extremize 환원(simulate지만 폼 표현 불가).
function isResearch(ir: IrStrategyDef): boolean {
  return ir.query === "select" || ir.query === "describe" || ir.query === "relate"
    || ir.study?.reduction === "extremize";
}

// 출처 칩 색: [기본]=엔진이 채운 silent 가정(주의 환기) / [지정]=유저가 정함(중립).
function provChip(p: string): CSSProperties {
  const isDefault = p === "기본";
  return {
    fontSize: 11, marginLeft: 6, padding: "0 6px", borderRadius: 4, whiteSpace: "nowrap",
    background: isDefault ? "#fcefe8" : "#eef1f4",
    color: isDefault ? "#c2410c" : "#555",
    border: `1px solid ${isDefault ? "#f3d6c4" : "#e2e2e2"}`,
  };
}

/**
 * 전략 연구소 — 노코드 블록트리로 룰·팩터·포트폴리오 전략을 조립·백테스트.
 *
 * 전체 구조(StrategyIR)를 표현: 유니버스 · 신호(condition|score) · 포지션 4부품
 * (방향·사이징·진입·청산) · 펼침(조건/파라미터/자산). /ir/strategy로 실행.
 */

const METRIC_LABELS: [string, string, string][] = [
  ["total_return", "총수익률", "%"], ["cagr", "연수익률(CAGR)", "%"],
  ["mdd", "최대낙폭(MDD)", "%"], ["sharpe", "샤프", ""],
  ["sortino", "소르티노", ""], ["calmar", "칼마", ""],
  ["profit_factor", "손익비(PF)", ""], ["var_95", "VaR95(일)", "%"],
  ["cvar_95", "CVaR95(일)", "%"], ["n_trades", "거래수", ""],
  ["win_rate", "승률", "%"], ["avg_trade_return", "평균 거래수익률", "%"],
  ["avg_hold", "평균 보유일", ""],
];

const SIZING_OPTS: [string, string][] = [
  ["equal_weight", "동일가중"], ["signal_proportional", "신호비례"],
  ["vol_inverse", "변동성 역가중"], ["target_vol", "목표변동성"],
  ["fixed_weight", "정적 비중"], ["fixed_amount", "종목당 고정금액"],
  ["pct_cash", "자본대비 %"],
];
const ENTRY_OPTS: [string, string][] = [
  ["on_signal", "이벤트 (신호 참인 날)"], ["scheduled", "정기 리밸런싱"],
  ["always", "상시 (매일)"],
];
const DIRECTION_OPTS: [string, string][] = [
  ["long", "롱"], ["short", "숏"], ["long_short", "롱숏중립"],
];
const REBALANCE_OPTS: [string, string][] = [
  ["daily", "매일"], ["weekly", "매주"], ["monthly", "매월"],
  ["quarterly", "분기"], ["annual", "매년"], ["every_n_days", "N일마다"],
];
function fmt(v: number | null | undefined, suffix: string): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const n = suffix === "%" ? v.toFixed(2) : (Number.isInteger(v) ? String(v) : v.toFixed(2));
  return `${n}${suffix}`;
}

// fixed_weight 입력 "AAA:0.3, BBB:0.4" → {AAA:0.3, BBB:0.4}
function parseWeights(text: string): Record<string, number> {
  const out: Record<string, number> = {};
  for (const part of text.split(",")) {
    const [sym, w] = part.split(":").map((x) => x.trim());
    if (sym && w !== undefined && !Number.isNaN(Number(w))) out[sym] = Number(w);
  }
  return out;
}
// 역: {AAA:0.3, BBB:0.4} → "AAA:0.3, BBB:0.4" (불러오기 하이드레이션)
function weightsToText(w?: Record<string, number> | null): string {
  if (!w) return "";
  return Object.entries(w).map(([s, v]) => `${s}:${v}`).join(", ");
}
// 선택 숫자 빈칸 — null/undefined → "" (UI placeholder), 그 외 그대로.
function numOrEmpty(v: number | null | undefined): number | "" {
  return v === null || v === undefined ? "" : v;
}

// 분석 유형 — 펼침 축(condition/parameter/asset/time) + 신호값(signal)·IC(relation)·기간분할(period).
// 평면 단일 드롭다운으로 노출. query(동사: describe/relate/simulate) + study(축×환원)로 매핑.
// (asset→entity, condition→label, period→time_fold, signal→describe, relation/time→relate.)
type AnalysisType = "none" | "condition" | "parameter" | "asset" | "time"
  | "signal" | "relation" | "period";

// 기간분할 스킴 → time_fold 기본 fold 수(split_dates 미지정 시). 옛 period_split 스킴의 UI 표현.
const SPLIT_FOLDS: Record<string, number> = { oos: 2, walk_forward: 4, kfold: 5 };

export default function IrBuilder() {
  const [catalogList, setCatalogList] = useState<IrBlockSpec[]>([]);
  const [symbols, setSymbols] = useState<SymbolInfo[]>([]);
  const [strategies, setStrategies] = useState<StrategyRow[]>([]);   // "내 전략" 탭(전략 조합 자산)
  const [indicatorCatalog, setIndicatorCatalog] = useState<IndicatorInfo[]>([]);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [validation, setValidation] = useState<IrValidation | null>(null);

  // 진입 트리거 — 엔진의 직교 축(이벤트/정기/상시). 신호·청산·사이징과 독립.
  const [entryMode, setEntryMode] = useState("on_signal");
  const [universeSymbols, setUniverseSymbols] = useState<string>("005930");
  const [signal, setSignal] = useState<IrNode | null>(null);

  // 포지션
  const [direction, setDirection] = useState("long");
  const [sizingMode, setSizingMode] = useState("equal_weight");
  const [topN, setTopN] = useState<number | "">(20);
  const [topPct, setTopPct] = useState<number | "">("");        // 상위 X% (top_n 대안)
  const [threshold, setThreshold] = useState<number | "">("");  // 임계 선택(부호 — 롱숏 TSMOM)
  const [refill, setRefill] = useState("cash");                  // 중간청산 후 cash|replace
  const [targetVolPct, setTargetVolPct] = useState<number | "">(20);  // target_vol 목표 연변동성
  const [weightsText, setWeightsText] = useState("");            // fixed_weight: "AAA:0.3, BBB:0.4"
  const [amountPct, setAmountPct] = useState<number | "">("");   // pct_cash: 종목당 자본%
  const [amountKrw, setAmountKrw] = useState<number | "">("");   // fixed_amount: 종목당 금액
  const [rebalance, setRebalance] = useState("monthly");
  const [everyNDays, setEveryNDays] = useState<number | "">(10);   // rebalance=every_n_days
  // 청산 — 채운 규칙 OR 결합(이벤트·정기 모두 적용)
  const [useExitCond, setUseExitCond] = useState(false);
  const [holdDays, setHoldDays] = useState<number | "">("");
  const [takeProfit, setTakeProfit] = useState<number | "">("");
  const [stopLoss, setStopLoss] = useState<number | "">("");
  const [trailPct, setTrailPct] = useState<number | "">("");
  const [trailAtr, setTrailAtr] = useState<number | "">("");
  const [exitCond, setExitCond] = useState<IrNode | null>(null);

  // 분석 (펼침 + 신호값/IC/기간분할 통합)
  const [sweepAxis, setSweepAxis] = useState<AnalysisType>("none");
  const [targetNode, setTargetNode] = useState<IrNode | null>(null);   // signal/relation 분석 노드
  const [splitDates, setSplitDates] = useState("");                    // period: 분할 시점(쉼표 ISO)
  // 파라미터 격자 — 축 1개=1D, 2개+=Cartesian (예: commission×slippage 민감도)
  const [paramAxes, setParamAxes] = useState<{ path: string; values: string }[]>(
    [{ path: "position.entry.top_n", values: "10, 20, 30" }]);
  const [sweepAssets, setSweepAssets] = useState("");
  const [sweepLabel, setSweepLabel] = useState<IrNode | null>(null);
  const [sweepWindows, setSweepWindows] = useState("5, 10, 20");
  const [eventBasis, setEventBasis] = useState("close");   // close|intraday|excess
  const [sweepEvent, setSweepEvent] = useState<IrNode | null>(null);  // time축 별도 이벤트 조건(선택)

  // 유니버스 — 세부조건: 선택 종목에 적용하는 2차 필터(condition) + 재선별 시점.
  const [screenerCond, setScreenerCond] = useState<IrNode | null>(null);
  const [screenerRefresh, setScreenerRefresh] =
    useState<"each_rebalance" | "once_at_start">("each_rebalance");

  const [capital, setCapital] = useState(10_000_000);
  // 시뮬레이션 (A5)
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  // <input type="date">는 placeholder 미지원 → 비었고 비포커스면 text로 렌더해 "전체 기간"
  // 힌트를 보이고, 포커스 시 date로 전환해 달력 선택기를 쓴다(다른 선택 항목 placeholder와 일관).
  const [startFocus, setStartFocus] = useState(false);
  const [endFocus, setEndFocus] = useState(false);
  const [delay, setDelay] = useState(1);
  const [fill, setFill] = useState("next_open");
  const [leverage, setLeverage] = useState(1);
  const [periodSplit, setPeriodSplit] = useState("single");
  // 비용 (연율% — borrow/funding/rfr; 거래비용은 비율)
  const [commission, setCommission] = useState<number | "">("");
  const [slippage, setSlippage] = useState<number | "">("");
  const [sellTax, setSellTax] = useState<number | "">("");
  const [shortBorrow, setShortBorrow] = useState<number | "">("");
  const [funding, setFunding] = useState<number | "">("");
  const [rfr, setRfr] = useState<number | "">("");
  const [maintMargin, setMaintMargin] = useState<number | "">("");   // 유지증거금률(%) — 마진콜
  // 포지션 세부·오버레이 (A6)
  const [maxPositionPct, setMaxPositionPct] = useState<number | "">("");
  const [volWindow, setVolWindow] = useState(20);
  const [volTarget, setVolTarget] = useState<number | "">("");
  const [turnoverDamp, setTurnoverDamp] = useState<number | "">("");
  const [maxDdStop, setMaxDdStop] = useState<number | "">("");        // hard (kill)
  const [maxDdSoft, setMaxDdSoft] = useState<number | "">("");        // G4 디리스킹 시작
  const [maxGroupPct, setMaxGroupPct] = useState<number | "">("");    // G3 그룹 노출 캡
  const [groupLabel, setGroupLabel] = useState<IrNode | null>(null);

  const [result, setResult] = useState<IrStrategyResult | null>(null);
  const [running, setRunning] = useState(false);
  // 컴파일된 research IR(신규 동사) — 있으면 빌더 폼 대신 이 IR을 직접 실행.
  const [researchIr, setResearchIr] = useState<IrStrategyDef | null>(null);

  // 자연어 → IR 컴파일 (베타). compileId/baseline로 "수정 없이 실행=정확" 신호 추적.
  const [nlText, setNlText] = useState("");
  const [compiling, setCompiling] = useState(false);
  const [compileErr, setCompileErr] = useState("");
  const [compileAssumptions, setCompileAssumptions] = useState<string[]>([]);
  const [compileExplanation, setCompileExplanation] = useState<IrExplanation | null>(null);
  const [compileId, setCompileId] = useState<number | null>(null);
  const compileBaseline = useRef<string>("");

  // 저장/불러오기 — 이름·편집대상·저장 상태. ?edit=<id>면 기존 IR 전략을 불러와 수정.
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const editId = searchParams.get("edit");
  const [name, setName] = useState("새 전략");
  const [editRunMode, setEditRunMode] = useState<string | null>(null);  // 편집 중 전략의 run_mode
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState("");

  const catalog: Catalog = useMemo(
    () => new Map(catalogList.map((b) => [b.op, b])), [catalogList]);

  useEffect(() => {
    // 독립 fetch — 묶지 않는다. catalog(가벼움)가 오면 신호 빌더 즉시 렌더,
    // symbols(콜드스타트 시 갱신에 막혀 느릴 수 있음)는 늦게 와도 유니버스 피커만 대기.
    // (이전 Promise.all은 느린 /symbols가 빠른 catalog까지 묶어 신호 섹션이 먹통.)
    api.irCatalog()
      .then((cat) => setCatalogList(cat.blocks))
      .catch((e) => setLoadErr(e.message ?? String(e)));   // catalog는 빌더 필수 — 실패만 에러
    api.symbols()
      .then((sym) => { setSymbols(sym.symbols); setIndicatorCatalog(sym.indicator_catalog ?? []); })
      .catch(() => {/* 종목 지연/실패해도 신호 빌더는 동작 — 유니버스/스크리너만 영향 */});
    api.listStrategies()
      .then((strats) => setStrategies(strats))
      .catch(() => {/* "내 전략" 탭만 비활성 — 비치명 */});
  }, []);

  // ?edit=<id> — 저장된 IR 전략을 불러와 전 폼 state로 역-하이드레이션.
  useEffect(() => {
    if (!editId) return;
    api.getStrategy(Number(editId))
      .then((row) => {
        if (row.engine !== "ir") {
          setSaveErr("이 전략은 전략 연구소(IR) 형식이 아닙니다.");
          return;
        }
        setEditRunMode(row.run_mode);
        hydrate(row.definition as IrStrategyDef);
      })
      .catch((e) => setSaveErr((e as Error).message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editId]);

  // 카탈로그 로드 시 시작 신호 1회 시드 (조건 예시 — 사용자가 자유 교체).
  // 편집(?edit) 모드에선 시드하지 않음 — hydrate가 저장된 신호를 채운다.
  useEffect(() => {
    if (!catalog.size || signal || editId) return;
    setSignal({
      op: "compare", params: { op: ">" },
      inputs: {
        left: { op: "data", params: { ref: "__SELF__.Close" } },
        right: { op: "ts_mean", params: { window: 20 },
                 inputs: { signal: { op: "data", params: { ref: "__SELF__.Close" } } } },
      },
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalog.size]);

  // 저장된 IR 정의 → 전 폼 state 복원 (buildStrategy의 역). 함수 선언이라 위 effect에서 hoisting으로 참조됨.
  function hydrate(def: IrStrategyDef) {
    setName(def.name ?? "새 전략");
    // 유니버스 — 선택 종목 + 세부조건(2차 필터·재선별 시점)
    const u = def.universe ?? { kind: "single" };
    setUniverseSymbols(u.kind === "all" ? "" : (u.symbols ?? []).join(", "));
    setScreenerCond(u.screener?.condition ?? null);
    setScreenerRefresh(u.screener?.refresh ?? "each_rebalance");
    // 신호
    if (def.signal) setSignal(def.signal);
    // 포지션
    const p = def.position ?? ({} as IrStrategyDef["position"]);
    setDirection(p.direction ?? "long");
    const sz = p.sizing ?? ({} as IrStrategyDef["position"]["sizing"]);
    setSizingMode(sz.mode ?? "equal_weight");
    setVolWindow(sz.vol_window ?? 20);
    // max_position_pct 기본 100(무제한)은 빈칸으로 환원해 placeholder 유지
    setMaxPositionPct(sz.max_position_pct != null && sz.max_position_pct !== 100
      ? sz.max_position_pct : "");
    setTargetVolPct(numOrEmpty(sz.target_vol_pct) === "" ? 20 : numOrEmpty(sz.target_vol_pct));
    setWeightsText(weightsToText(sz.weights));
    setAmountPct(sz.mode === "pct_cash" ? numOrEmpty(sz.amount_pct) : "");
    setAmountKrw(sz.mode === "fixed_amount" ? numOrEmpty(sz.amount_krw) : "");
    const en = p.entry ?? ({} as IrStrategyDef["position"]["entry"]);
    setEntryMode(en.mode ?? "on_signal");
    setRebalance(en.rebalance ?? "monthly");
    setEveryNDays(numOrEmpty(en.every_n_days) === "" ? 10 : numOrEmpty(en.every_n_days));
    setRefill(en.refill ?? "cash");
    setTopN(numOrEmpty(en.top_n));
    setTopPct(numOrEmpty(en.top_pct));
    setThreshold(numOrEmpty(en.threshold));
    const ex = p.exit ?? ({} as IrStrategyDef["position"]["exit"]);
    setHoldDays(numOrEmpty(ex.hold_days));
    setTakeProfit(numOrEmpty(ex.take_profit));
    setStopLoss(numOrEmpty(ex.stop_loss));
    setTrailPct(numOrEmpty(ex.trail_pct));
    setTrailAtr(numOrEmpty(ex.trail_atr_mult));
    setUseExitCond(!!ex.condition);
    setExitCond(ex.condition ?? null);
    const ov = p.overlays ?? ({} as IrStrategyDef["position"]["overlays"]);
    setVolTarget(numOrEmpty(ov.vol_target));
    setTurnoverDamp(numOrEmpty(ov.turnover_damp));
    setMaxDdStop(numOrEmpty(ov.max_drawdown_stop));
    setMaxDdSoft(numOrEmpty(ov.max_drawdown_soft));
    setMaxGroupPct(numOrEmpty(ov.max_group_pct));
    setGroupLabel(ov.group_label ?? null);
    // 시뮬레이션
    const sim = def.simulation ?? {};
    setCapital(sim.initial_capital ?? 10_000_000);
    setDelay(sim.delay ?? 1);
    setFill(sim.fill ?? "next_open");
    setLeverage(sim.leverage ?? 1);
    setStartDate(sim.start ?? "");
    setEndDate(sim.end ?? "");
    setCommission(numOrEmpty(sim.commission));
    setSlippage(numOrEmpty(sim.slippage));
    setSellTax(numOrEmpty(sim.sell_tax));
    setShortBorrow(numOrEmpty(sim.short_borrow_pct));
    setFunding(numOrEmpty(sim.funding_cost_pct));
    setRfr(numOrEmpty(sim.rfr_pct));
    setMaintMargin(numOrEmpty(sim.maintenance_margin_pct));
    // 분석 — query(동사) + study(축×환원)를 평면 analysisType로 역매핑.
    // describe→signal, relate→(relation_kind=ic ? relation : time),
    // simulate→study.axis(parameter/entity→asset/label→condition/time_fold→period/none→none).
    const query = def.query ?? "simulate";
    const st = def.study ?? {};
    const axis = st.axis ?? "none";
    setSweepAxis(
      query === "describe" ? "signal"
      : query === "relate" ? (st.relation_kind === "ic" ? "relation" : "time")
      : axis === "parameter" ? "parameter"
      : axis === "entity" ? "asset"
      : axis === "label" ? "condition"
      : axis === "time_fold" ? "period"
      : "none");
    setTargetNode(st.target_node ?? null);
    setSplitDates((st.split_dates ?? []).join(", "));
    // folds → 스킴 드롭다운 역매핑(2→oos·4→walk_forward·5→kfold), 그 외엔 oos.
    if (st.folds != null) {
      const scheme = Object.entries(SPLIT_FOLDS).find(([, n]) => n === st.folds)?.[0];
      setPeriodSplit(scheme ?? "oos");
    }
    if (st.param_grid?.length) {
      setParamAxes(st.param_grid.map((ax) => ({
        path: ax.path, values: (ax.values ?? []).join(", "),
      })));
    }
    setSweepAssets((st.assets ?? []).join(", "));
    setSweepLabel(st.label ?? null);
    setSweepEvent(st.event ?? null);
    if (st.windows?.length) setSweepWindows(st.windows.join(", "));
    setEventBasis(st.event_basis ?? "close");
  }

  // 지표 메타는 전역(컬럼별·종목 무관) — /symbols의 indicator_catalog 1회 수신분 사용.
  // (이전엔 종목별 indicators 배열을 골랐으나, 메타가 동일해 전역 카탈로그로 대체.)
  const selfIndicators = indicatorCatalog;

  // 신호 출력 타입 자동 감지(condition=룰 트리거 · score=팩터 알파). 강제하지 않음.
  const signalType = signal ? catalog.get(signal.op)?.out_type : undefined;

  function buildStrategy(): Record<string, unknown> {
    const syms = universeSymbols.split(",").map((s) => s.trim()).filter(Boolean);
    const { query, study } = buildQuery();
    const sim: Record<string, unknown> = {
      initial_capital: capital, delay, fill, leverage,
      start: startDate || null, end: endDate || null,
    };
    if (commission !== "") sim.commission = commission;
    if (slippage !== "") sim.slippage = slippage;
    if (sellTax !== "") sim.sell_tax = sellTax;
    if (shortBorrow !== "") sim.short_borrow_pct = shortBorrow;
    if (funding !== "") sim.funding_cost_pct = funding;
    if (rfr !== "") sim.rfr_pct = rfr;
    if (maintMargin !== "") sim.maintenance_margin_pct = maintMargin;
    const overlays: Record<string, unknown> = {};
    if (volTarget !== "") overlays.vol_target = volTarget;
    if (turnoverDamp !== "") overlays.turnover_damp = turnoverDamp;
    if (maxDdStop !== "") overlays.max_drawdown_stop = maxDdStop;
    if (maxDdSoft !== "") overlays.max_drawdown_soft = maxDdSoft;
    if (maxGroupPct !== "") {
      overlays.max_group_pct = maxGroupPct;
      overlays.group_label = groupLabel;
    }

    // ── 유니버스 ──
    const universe: Record<string, unknown> = {
      kind: syms.length > 1 ? "list" : syms.length === 1 ? "single" : "all",
      ...(syms.length ? { symbols: syms } : {}),
      ...(syms.length && screenerCond
          ? { screener: { condition: screenerCond, refresh: screenerRefresh } }
          : {}),
    };

    // ── 사이징 (전 모드 노출 — 검증이 부적합 조합 안내) ──
    const sizing: Record<string, unknown> = { mode: sizingMode, vol_window: volWindow };
    if (maxPositionPct !== "") sizing.max_position_pct = maxPositionPct;
    if (sizingMode === "target_vol" && targetVolPct !== "") sizing.target_vol_pct = targetVolPct;
    if (sizingMode === "fixed_weight") sizing.weights = parseWeights(weightsText);
    if (sizingMode === "pct_cash" && amountPct !== "") sizing.amount_pct = amountPct;
    if (sizingMode === "fixed_amount" && amountKrw !== "") sizing.amount_krw = amountKrw;

    // ── 진입 트리거 (이벤트 ⊕ 정기/상시). 선택 파라미터는 정기·상시에만 ──
    const entry: Record<string, unknown> = { mode: entryMode };
    if (entryMode !== "on_signal") {
      entry.rebalance = rebalance;
      entry.refill = refill;
      entry.top_n = topN === "" ? null : topN;
      entry.top_pct = topPct === "" ? null : topPct;
      entry.threshold = threshold === "" ? null : threshold;
      if (entryMode === "scheduled" && rebalance === "every_n_days") {
        entry.every_n_days = everyNDays === "" ? null : everyNDays;
      }
    }

    // ── 청산 (이벤트·정기 공통 — 채운 규칙 OR 결합) ──
    const exit: Record<string, unknown> = {
      hold_days: holdDays === "" ? null : holdDays,
      take_profit: takeProfit === "" ? null : takeProfit,
      stop_loss: stopLoss === "" ? null : stopLoss,
      trail_pct: trailPct === "" ? null : trailPct,
      trail_atr_mult: trailAtr === "" ? null : trailAtr,
      condition: useExitCond ? exitCond : null,
    };

    return {
      name: name.trim() || "새 전략",
      universe,
      signal,
      position: { direction, sizing, entry, exit, overlays },
      simulation: sim,
      query,
      ...(study ? { study } : {}),
    };
  }

  // 평면 분석 유형 → 조사형 query(동사) + study(축×환원). 옛 buildSweep/period_split 대체.
  // 매핑: asset→entity, condition→label, period→time_fold, signal→describe, relation/time→relate.
  function buildQuery(): { query: "describe" | "relate" | "simulate";
                           study?: Record<string, unknown> } {
    if (sweepAxis === "parameter") {
      return {
        query: "simulate",
        study: {
          axis: "parameter", reduction: "contrast",
          param_grid: paramAxes
            .map((ax) => ({
              path: ax.path.trim(),
              values: ax.values.split(",").map((x) => Number(x.trim())).filter((n) => !Number.isNaN(n)),
            }))
            .filter((ax) => ax.path && ax.values.length),
        },
      };
    }
    if (sweepAxis === "asset") {
      return {
        query: "simulate",
        study: {
          axis: "entity", reduction: "contrast",
          assets: sweepAssets.split(",").map((s) => s.trim()).filter(Boolean),
        },
      };
    }
    if (sweepAxis === "time") {
      // 이벤트 스터디 — forward 수익 분포(P&L 아님). axis=none, query=relate.
      return {
        query: "relate",
        study: {
          axis: "none", label: sweepLabel, event: sweepEvent,
          event_basis: eventBasis, windows: parseWindows(),
        },
      };
    }
    if (sweepAxis === "condition") {
      return { query: "simulate", study: { axis: "label", reduction: "contrast", label: sweepLabel } };
    }
    // 신호값 분포 — 임의 score 노드의 값 분포를 (선택)국면별로. 국면 블록 있으면 axis=label.
    if (sweepAxis === "signal") {
      return {
        query: "describe",
        study: { axis: sweepLabel ? "label" : "none", target_node: targetNode, label: sweepLabel },
      };
    }
    // 횡단 IC — factor와 forward수익의 예측력. windows=forward 일수, relation_kind=ic.
    if (sweepAxis === "relation") {
      return {
        query: "relate",
        study: {
          axis: "none", relation_kind: "ic", target_node: targetNode,
          label: sweepLabel, windows: parseWindows(),
        },
      };
    }
    // 기간 분할 / 워크포워드 — time_fold × consistency. 명시 split_dates 우선, 없으면 folds.
    // 백엔드는 분할 스킴(oos/wf/kfold)을 fold 수 + consistency 환원으로 흡수했으므로,
    // 스킴 드롭다운은 folds 기본값에 대한 UI 표현으로 매핑(oos=2·walk_forward=4·kfold=5).
    if (sweepAxis === "period") {
      const study: Record<string, unknown> = {
        axis: "time_fold", reduction: "consistency",
      };
      const dates = splitDates.split(",").map((s) => s.trim()).filter(Boolean);
      if (dates.length) study.split_dates = dates;
      else study.folds = SPLIT_FOLDS[periodSplit] ?? 2;
      return { query: "simulate", study };
    }
    return { query: "simulate" };   // none — 단일 백테스트(study 없음)
  }

  function parseWindows(): number[] {
    return sweepWindows.split(",").map((x) => Number(x.trim())).filter((n) => !Number.isNaN(n));
  }

  // 자연어 설명 → IR 컴파일 → 빌더 전 폼에 hydrate. 변환 후 유저가 확인·실행.
  async function compileFromNl() {
    if (!nlText.trim() || compiling) return;
    setCompiling(true); setCompileErr(""); setCompileAssumptions([]); setCompileExplanation(null);
    try {
      const res = await api.compileIr(nlText.trim());
      setCompileAssumptions(res.assumptions ?? []);
      setCompileExplanation(res.explanation ?? null);
      if (!res.success) {
        setCompileErr(res.error || "컴파일에 실패했습니다.");
        setCompileId(null);
        return;
      }
      const compiledIr = res.ir as unknown as IrStrategyDef;
      hydrate(compiledIr);   // 컴파일 IR을 기존 hydrate로 전 폼 복원(요약 표시·일부 항목 확인용)
      // research query면 빌더 폼이 왕복 못하므로 컴파일 IR을 직접 실행하도록 보관.
      // 일반 simulate면 null로 둬 폼(buildStrategy) 경로 사용.
      setResearchIr(isResearch(compiledIr) ? compiledIr : null);
      setCompileId(res.compile_id);
      setResult(null);
    } catch (e) {
      setCompileErr((e as Error).message);
      setCompileId(null);
    } finally {
      setCompiling(false);
    }
  }

  // 컴파일 직후 빌더 상태를 baseline으로 캡처 — 실행 시 수정 여부(정확도 신호) 비교용.
  useEffect(() => {
    if (compileId == null) return;
    try { compileBaseline.current = JSON.stringify(buildStrategy()); }
    catch { compileBaseline.current = ""; }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [compileId]);

  async function run() {
    setRunning(true); setResult(null);
    try {
      // research query(신규 동사)면 컴파일 IR을 그대로 실행 — 빌더 폼은 simulate 전용이라
      // select/describe/relate/extremize를 왕복 못한다(폼 재구성 우회).
      if (researchIr) {
        if (compileId != null) {
          api.compileFeedback(compileId, true, false).catch(() => {});
        }
        setResult(await api.runIrStrategy(researchIr as unknown as Record<string, unknown>));
        return;
      }
      if (!signal) return;
      // 저장된 전략(?edit) 백테스트면 strategy_id를 실어 BacktestRun으로 내역 저장.
      const built = buildStrategy() as Record<string, unknown>;
      // 컴파일된 IR이면 "수정 없이 실행했는지" 기록(베타 정확도 신호).
      if (compileId != null) {
        const edited = JSON.stringify(built) !== compileBaseline.current;
        api.compileFeedback(compileId, true, edited).catch(() => {});
      }
      const body: Record<string, unknown> = { ...built };
      if (editId) body.strategy_id = Number(editId);
      setResult(await api.runIrStrategy(body));
    } catch (e) {
      setResult({ success: false, error: (e as Error).message });
    } finally {
      setRunning(false);
    }
  }

  // 저장 — 새 전략은 create, ?edit이면 update(버전 스냅샷). 둘 다 engine='ir'.
  // runMode="paper"면 모의 적용(로컬앱 자동매매 진입). Stage 3 컷오버로 IR 라이브
  // 신호평가·청산·사이징이 완성돼 IR도 operand와 동일하게 모의/실전을 소비한다.
  async function save(runMode: "draft" | "paper") {
    if (!signal) return;
    const def = buildStrategy() as unknown as IrStrategyDef;
    // 정적 세부조건(once_at_start) 전략을 모의/실전으로 올릴 때 — 바스켓은 지금
    // 이 시점의 데이터로 확정되며 백테스트에서 본 종목과 다를 수 있음을 고지.
    if (runMode === "paper" && def.universe?.screener?.refresh === "once_at_start") {
      if (!confirm(
        "이 전략은 세부조건이 '정적'입니다.\n" +
        "모의/실전을 시작하는 지금 시점의 데이터로 매매할 종목 바스켓을 확정하며, 백테스트에서 본 종목과 다를 수 있습니다.\n" +
        "계속할까요?",
      )) return;
    }
    setSaveErr(""); setSaving(true);
    try {
      if (editId) {
        // 수정: 모의 적용이면 paper 승격, 아니면 기존 run_mode 유지.
        const mode = runMode === "paper" ? "paper" : (editRunMode ?? "draft");
        await api.updateStrategy(Number(editId), def, mode, "ir");
        navigate(`/strategies/${editId}`);
      } else {
        const created = await api.createStrategy(def, runMode, "ir");
        navigate(`/strategies/${created.id}`);
      }
    } catch (e) {
      setSaveErr((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  // 실시간 논리 검증 — 빌드된 전략이 바뀔 때마다(400ms 디바운스) /ir/validate 호출.
  // bodyJson(직렬화)을 변경 감지자로 사용해 다수 state 의존을 한 dep로 요약. 에러 시 버튼 게이팅.
  // research 질의(select/describe/relate/extremize)는 빌더 폼이 왕복 못해 buildStrategy()가
  // query·select를 누락한 simulate IR을 만든다 → 폼 재구성이 아니라 **실제 실행될 researchIr**를
  // 검증해야 spurious 오류(S-entry 등)와 run 버튼 오게이팅을 막는다. 서버 validate는 query-aware.
  const bodyJson = researchIr ? JSON.stringify(researchIr)
    : signal ? JSON.stringify(buildStrategy()) : "";
  useEffect(() => {
    if (!bodyJson) { setValidation(null); return; }
    const t = setTimeout(() => {
      api.validateIr(JSON.parse(bodyJson)).then(setValidation).catch(() => setValidation(null));
    }, 400);
    return () => clearTimeout(t);
  }, [bodyJson]);
  const hasErrors = validation ? !validation.ok : false;

  if (loadErr) {
    return <div className="panel"><div className="page-title">전략 연구소</div>
      <p className="muted">카탈로그를 불러오지 못했습니다: {loadErr}</p></div>;
  }

  return (
    <div>
      <div className="page-title">전략 연구소</div>
      <p className="page-sub">
        블록을 조립해 룰·팩터·포트폴리오 전략을 만들고 백테스트합니다.
        슬롯에 블록을 끼워 중첩할 수 있어요.
      </p>

      {/* 자연어로 전략 만들기 (NL→IR 컴파일, 베타) */}
      <div className="panel">
        <div className="panel-title">
          자연어로 전략 만들기{" "}
          <span className="muted" style={{ fontSize: 12, fontWeight: 400 }}>베타</span>
        </div>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          전략을 문장으로 설명하면 아래 빌더에 자동으로 채워집니다. 변환 후 각 항목을 확인하고 백테스트를 실행하세요.
        </p>
        <textarea
          value={nlText}
          onChange={(e) => setNlText(e.target.value)}
          placeholder="예: 삼성전자를 종가가 20일 이동평균 위로 올라오면 매수하고, 10% 익절·5% 손절"
          rows={3}
          aria-label="자연어 전략 설명"
          style={{
            width: "100%", resize: "vertical", padding: 10, fontSize: 14,
            border: "1px solid var(--input-border)", borderRadius: 8, boxSizing: "border-box",
          }}
        />
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginTop: 8 }}>
          <button type="button" onClick={compileFromNl} disabled={compiling || !nlText.trim()}>
            {compiling ? "변환 중…" : "변환"}
          </button>
          <span className="muted" style={{ fontSize: 12 }}>변환은 아래 폼을 덮어씁니다.</span>
        </div>
        {compileErr && <div className="error" style={{ marginTop: 8 }}>{compileErr}</div>}
        {compileExplanation ? (
          <div style={{ marginTop: 10 }}>
            {/* ① 직관적 산문 요약 — 이 전략이 정확히 어떻게 백테스트되는지 한 문단으로 */}
            <div style={{
              fontSize: 13.5, lineHeight: 1.65, padding: "10px 12px", borderRadius: 8,
              background: "var(--panel, #fff)", border: "1px solid var(--input-border)",
            }}>
              <strong style={{ display: "block", marginBottom: 4 }}>이렇게 백테스트됩니다</strong>
              {compileExplanation.summary}
            </div>

            {/* ② MECE 항목별 설정·가정 (접기) — [기본]=엔진이 채운 silent 가정 */}
            <details style={{ marginTop: 8 }}>
              <summary style={{ cursor: "pointer", fontSize: 13, fontWeight: 600 }}>
                항목별 설정·가정 보기 — 분류 {compileExplanation.buckets.length}개
                <span className="muted" style={{ fontWeight: 400, marginLeft: 6 }}>
                  ([기본]=내가 안 정해 자동 적용된 값)
                </span>
              </summary>
              <div style={{ marginTop: 8 }}>
                {compileExplanation.buckets.map((b) => (
                  <div key={b.key} style={{ marginBottom: 12 }}>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>{b.title}</div>
                    <div className="muted" style={{ fontSize: 11, marginBottom: 4 }}>{b.question}</div>
                    <ul style={{ margin: 0, paddingLeft: 16, listStyle: "none" }}>
                      {b.items.map((it, i) => (
                        <li key={i} style={{ fontSize: 12.5, marginBottom: 4 }}>
                          <span style={{ fontWeight: 500 }}>{it.label}</span>: {it.value}
                          <span style={provChip(it.provenance)}>{it.provenance}</span>
                          {it.meaning && (
                            <div className="muted" style={{ fontSize: 11, lineHeight: 1.4 }}>{it.meaning}</div>
                          )}
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            </details>

            {/* ③ [추론] — LLM이 유저 말에서 보충 해석한 가정 */}
            {compileExplanation.assumptions.length > 0 && (
              <div className="muted" style={{ fontSize: 12.5, marginTop: 8 }}>
                <strong>AI 보충 해석</strong> — 의도와 다르면 아래 폼에서 직접 수정하세요.
                <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                  {compileExplanation.assumptions.map((a, i) => <li key={i}>{a}</li>)}
                </ul>
              </div>
            )}
          </div>
        ) : compileAssumptions.length > 0 ? (
          <div className="muted" style={{ fontSize: 13, marginTop: 8 }}>
            <strong>해석 가정</strong> — 의도와 다르면 아래에서 직접 수정하세요.
            <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
              {compileAssumptions.map((a, i) => <li key={i}>{a}</li>)}
            </ul>
          </div>
        ) : null}
      </div>

      <input
        className="strategy-name-input"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="전략 이름 (예: 코스피 모멘텀 상위 20)"
        aria-label="전략 이름"
      />
      {editId && (
        <p className="muted" style={{ fontSize: 13, marginTop: 4 }}>
          기존 전략 수정 중 — 저장하면 변경 전 정의가 자동으로 새 버전으로 보존됩니다.
        </p>
      )}
      {saveErr && <div className="error">{saveErr}</div>}

      {/* 유니버스 */}
      <div className="panel">
        <div className="panel-title">유니버스</div>
        <div style={{ marginBottom: 10 }}>
          <div className="muted" style={{ fontSize: 13, marginBottom: 4 }}>대상 종목</div>
          <MultiSymbolPicker symbols={symbols} value={universeSymbols}
                             onChange={setUniverseSymbols} scope="tradable"
                             strategies={strategies.filter(
                               (s) => s.engine !== "operand" && String(s.id) !== editId)}
                             screener={screenerCond} onScreenerChange={setScreenerCond}
                             screenerRefresh={screenerRefresh}
                             onScreenerRefreshChange={setScreenerRefresh}
                             catalog={catalog} selfIndicators={selfIndicators} />
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            비우면 전체 종목이 유니버스가 됩니다. 매수/매도 가능 종목에서 선택 ·
            "내 전략" 탭에서 저장한 전략을 자산으로 골라 전략끼리 조합할 수 있습니다.
          </p>
        </div>
        <div className="lab-row">
          <label className="lab-field">초기자본
            <input type="number" value={capital} step={1_000_000}
                   onChange={(e) => setCapital(Number(e.target.value))} />
          </label>
        </div>
      </div>

      {/* 신호 */}
      <div className="panel">
        <div className="panel-title">
          신호{signalType === "condition" ? " · 조건(룰 트리거)"
            : signalType === "score" ? " · 점수(팩터 알파)" : ""}
        </div>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          조건(참/거짓)=룰 트리거(참인 날·종목 진입) · 점수=팩터 알파(줄세워 상위 선택). 어느 쪽이든 조립 가능 — 진입 트리거와 자유 조합.
        </p>
        {catalog.size ? (
          <SentenceTree node={signal} catalog={catalog} symbols={symbols}
                     selfIndicators={selfIndicators} requiredType={["condition", "score"]}
                     onChange={setSignal} />
        ) : <p className="muted">불러오는 중…</p>}
      </div>

      {/* 진입 · 포지션 */}
      <div className="panel">
        <div className="panel-title">진입 · 포지션</div>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          진입 트리거·매매 방향·사이징을 자유 조합합니다. 정기/상시 진입의 "상위 N"은 (스크리너가 추린) 후보 중 신호 점수로 실제 보유 종목을 고릅니다. 부적합 조합(예: 이벤트 진입 + 점수 신호)은 실행 시 안내됩니다.
        </p>
        <div className="lab-row">
          <label className="lab-field">진입 트리거
            <select value={entryMode} onChange={(e) => setEntryMode(e.target.value)}>
              {ENTRY_OPTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label className="lab-field">매매 방향
            <select value={direction} onChange={(e) => setDirection(e.target.value)}>
              {DIRECTION_OPTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          <label className="lab-field">사이징
            <select value={sizingMode} onChange={(e) => setSizingMode(e.target.value)}>
              {SIZING_OPTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
          {entryMode === "scheduled" && (
            <label className="lab-field">리밸런싱
              <select value={rebalance} onChange={(e) => setRebalance(e.target.value)}>
                {REBALANCE_OPTS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
          )}
          {entryMode === "scheduled" && rebalance === "every_n_days" && (
            <label className="lab-field">N일 간격
              <input type="number" value={everyNDays} min={1} placeholder="예: 10"
                     onChange={(e) => setEveryNDays(e.target.value === "" ? "" : Number(e.target.value))} />
            </label>
          )}
          {entryMode !== "on_signal" && (
            <>
              <label className="lab-field">상위 N
                <input type="number" value={topN} placeholder="상위%와 택1"
                       onChange={(e) => setTopN(e.target.value === "" ? "" : Number(e.target.value))} />
              </label>
              <label className="lab-field">상위 %
                <input type="number" value={topPct} placeholder="top_n 대안"
                       onChange={(e) => setTopPct(e.target.value === "" ? "" : Number(e.target.value))} />
              </label>
              {direction === "long_short" && (
                <label className="lab-field">임계(부호선택)
                  <input type="number" value={threshold} placeholder="예: 0 (양수롱·음수숏)"
                         onChange={(e) => setThreshold(e.target.value === "" ? "" : Number(e.target.value))} />
                </label>
              )}
              <label className="lab-field">빈슬롯 충원
                <select value={refill} onChange={(e) => setRefill(e.target.value)}>
                  <option value="cash">현금 유지</option>
                  <option value="replace">차순위 충원</option>
                </select>
              </label>
            </>
          )}
          <label className="lab-field">종목당 상한(%)
            <input type="number" value={maxPositionPct} placeholder="무제한"
                   onChange={(e) => setMaxPositionPct(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          {(sizingMode === "vol_inverse" || sizingMode === "target_vol") && (
            <label className="lab-field">변동성 창(일)
              <input type="number" value={volWindow}
                     onChange={(e) => setVolWindow(Number(e.target.value))} />
            </label>
          )}
          {sizingMode === "target_vol" && (
            <label className="lab-field">목표 연변동성(%)
              <input type="number" value={targetVolPct}
                     onChange={(e) => setTargetVolPct(e.target.value === "" ? "" : Number(e.target.value))} />
            </label>
          )}
          {sizingMode === "pct_cash" && (
            <label className="lab-field">종목당 자본(%)
              <input type="number" value={amountPct} placeholder="기본 10"
                     onChange={(e) => setAmountPct(e.target.value === "" ? "" : Number(e.target.value))} />
            </label>
          )}
          {sizingMode === "fixed_amount" && (
            <label className="lab-field">종목당 금액(원)
              <input type="number" value={amountKrw} step={100000} placeholder="예: 1000000"
                     onChange={(e) => setAmountKrw(e.target.value === "" ? "" : Number(e.target.value))} />
            </label>
          )}
          {sizingMode === "fixed_weight" && (
            <label className="lab-field" style={{ minWidth: 280 }}>정적 비중 (종목:비중)
              <input type="text" value={weightsText} placeholder="005930:0.4, 000660:0.3"
                     onChange={(e) => setWeightsText(e.target.value)} />
            </label>
          )}
        </div>
      </div>

      {/* 청산 (이벤트·정기 공통) */}
      <div className="panel">
        <div className="panel-title">청산</div>
        <div className="muted" style={{ fontSize: 13, marginBottom: 6 }}>
          채운 규칙들이 함께 적용되어 가장 먼저 닿는 조건에서 청산합니다. 정기 리밸런싱에도 적용(상시 진입은 매일 교체라 무시).
        </div>
        <div className="lab-row">
          <label className="lab-field">보유일수
            <input type="number" value={holdDays} placeholder="없음"
                   onChange={(e) => setHoldDays(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">익절(%)
            <input type="number" value={takeProfit} placeholder="없음"
                   onChange={(e) => setTakeProfit(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">손절(%)
            <input type="number" value={stopLoss} placeholder="없음"
                   onChange={(e) => setStopLoss(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">트레일링(%)
            <input type="number" value={trailPct} placeholder="없음"
                   onChange={(e) => setTrailPct(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">ATR 트레일링(배수)
            <input type="number" step={0.5} value={trailAtr} placeholder="없음"
                   onChange={(e) => setTrailAtr(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
        </div>
        <label style={{ marginTop: 10, display: "flex", alignItems: "center", gap: 6, fontSize: 14 }}>
          <input type="checkbox" checked={useExitCond}
                 onChange={(e) => setUseExitCond(e.target.checked)} />
          매도 조건 사용
        </label>
        {useExitCond && (
          <div style={{ marginTop: 8 }}>
            <SentenceTree node={exitCond} catalog={catalog} symbols={symbols}
                       selfIndicators={selfIndicators} requiredType="condition"
                       onChange={setExitCond} />
          </div>
        )}
      </div>

      {/* 오버레이 */}
      <div className="panel">
        <div className="panel-title">오버레이</div>
        <div className="lab-row">
          <label className="lab-field">변동성 타겟(%)
            <input type="number" value={volTarget} placeholder="없음"
                   onChange={(e) => setVolTarget(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">턴오버 억제
            <input type="number" step={0.01} value={turnoverDamp} placeholder="없음"
                   onChange={(e) => setTurnoverDamp(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">MDD 스톱(%)
            <input type="number" value={maxDdStop} placeholder="없음(완전청산)"
                   onChange={(e) => setMaxDdStop(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">MDD 디리스킹 시작(%)
            <input type="number" value={maxDdSoft} placeholder="없음(binary)"
                   onChange={(e) => setMaxDdSoft(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">그룹당 노출 캡(%)
            <input type="number" value={maxGroupPct} placeholder="없음"
                   onChange={(e) => setMaxGroupPct(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
        </div>
        {maxGroupPct !== "" && (
          <div style={{ marginTop: 10 }}>
            <div className="muted" style={{ fontSize: 13, marginBottom: 4 }}>
              그룹 라벨 블록 (예: 시총·섹터 구간분할 — 같은 라벨끼리 노출 합산해 캡 적용)</div>
            <SentenceTree node={groupLabel} catalog={catalog} symbols={symbols}
                       selfIndicators={selfIndicators} requiredType="label"
                       onChange={setGroupLabel} />
          </div>
        )}
      </div>

      {/* 시뮬레이션 */}
      <div className="panel">
        <div className="panel-title">시뮬레이션</div>
        <div className="lab-row">
          <label className="lab-field">시작일
            <input type={startDate || startFocus ? "date" : "text"} value={startDate}
                   placeholder="처음부터(전체)"
                   onFocus={() => setStartFocus(true)} onBlur={() => setStartFocus(false)}
                   onChange={(e) => setStartDate(e.target.value)} />
          </label>
          <label className="lab-field">종료일
            <input type={endDate || endFocus ? "date" : "text"} value={endDate}
                   placeholder="최근까지(전체)"
                   onFocus={() => setEndFocus(true)} onBlur={() => setEndFocus(false)}
                   onChange={(e) => setEndDate(e.target.value)} />
          </label>
          <label className="lab-field">체결지연(일)
            <input type="number" value={delay} onChange={(e) => setDelay(Number(e.target.value))} />
          </label>
          <label className="lab-field">체결
            <select value={fill} onChange={(e) => setFill(e.target.value)}>
              <option value="next_open">익일 시가</option>
              <option value="close">당일 종가</option>
              <option value="typical">당일 (고+저+종)/3</option>
            </select>
          </label>
          <label className="lab-field">레버리지
            <input type="number" step={0.5} value={leverage}
                   onChange={(e) => setLeverage(Number(e.target.value))} />
          </label>
          <label className="lab-field">유지증거금률(%)
            <input type="number" step={5} value={maintMargin} placeholder="없음(마진콜 끄기)"
                   onChange={(e) => setMaintMargin(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
        </div>
        {leverage > 1 && (
          <div className="muted" style={{ fontSize: 12, margin: "8px 0 0" }}>
            레버리지(1배 초과)는 <strong>백테스트 전용</strong>입니다 — 모의·실전 적용은 차단됩니다.
            실거래에서 2배 노출이 필요하면 레버리지 ETF(예: KODEX 레버리지 122630)를 현금으로 매수하세요.
          </div>
        )}
        <div className="muted" style={{ fontSize: 13, margin: "10px 0 4px" }}>
          비용 (비우면 시장 기본값) — 수수료·슬리피지·매도세는 비율(0.0005=5bp), 차입·펀딩·무위험은 연율%</div>
        <div className="lab-row">
          <label className="lab-field">수수료
            <input type="number" step={0.0001} value={commission} placeholder="기본 0.0003"
                   onChange={(e) => setCommission(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">슬리피지
            <input type="number" step={0.0001} value={slippage} placeholder="기본 0.001"
                   onChange={(e) => setSlippage(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">매도세
            <input type="number" step={0.0001} value={sellTax} placeholder="기본 0.0023"
                   onChange={(e) => setSellTax(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">숏 차입(연%)
            <input type="number" step={0.5} value={shortBorrow} placeholder="없음"
                   onChange={(e) => setShortBorrow(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">펀딩(연%)
            <input type="number" step={0.5} value={funding} placeholder="없음"
                   onChange={(e) => setFunding(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
          <label className="lab-field">현금 무위험(연%)
            <input type="number" step={0.5} value={rfr} placeholder="없음"
                   onChange={(e) => setRfr(e.target.value === "" ? "" : Number(e.target.value))} />
          </label>
        </div>
      </div>

      {/* 분석 (펼침 + 신호값·IC·기간분할 통합) */}
      <div className="panel">
        <div className="panel-title">분석</div>
        <div className="lab-row">
          <label className="lab-field">분석 유형
            <select value={sweepAxis} onChange={(e) => setSweepAxis(e.target.value as AnalysisType)}>
              <option value="none">없음 (1회 백테스트)</option>
              <option value="condition">국면별 비교</option>
              <option value="parameter">파라미터 민감도</option>
              <option value="asset">종목별</option>
              <option value="time">이벤트 분석</option>
              <option value="signal">신호값 분포</option>
              <option value="relation">팩터 IC (예측력)</option>
              <option value="period">기간 분할 / 워크포워드</option>
            </select>
          </label>
          {sweepAxis === "asset" && (
            <label className="lab-field">종목들 (쉼표)
              <input type="text" value={sweepAssets} placeholder="005930, 000660" style={{ minWidth: 220 }}
                     onChange={(e) => setSweepAssets(e.target.value)} />
            </label>
          )}
          {sweepAxis === "time" && (
            <label className="lab-field">수익 기준
              <select value={eventBasis} onChange={(e) => setEventBasis(e.target.value)}>
                <option value="close">종가→종가</option>
                <option value="intraday">시가→종가(당일반등)</option>
                <option value="excess">시장초과(2종목+)</option>
              </select>
            </label>
          )}
          {(sweepAxis === "time" || sweepAxis === "relation") && (
            <label className="lab-field">
              {sweepAxis === "relation" ? "예측 horizon (일, 쉼표)" : "forward 윈도우 (일, 쉼표)"}
              <input type="text" value={sweepWindows} placeholder="5, 10, 20" style={{ minWidth: 160 }}
                     onChange={(e) => setSweepWindows(e.target.value)} />
            </label>
          )}
          {sweepAxis === "period" && (
            <>
              <label className="lab-field">분할 방식
                <select value={periodSplit === "single" ? "oos" : periodSplit}
                        onChange={(e) => setPeriodSplit(e.target.value)}>
                  <option value="oos">인/아웃샘플</option>
                  <option value="walk_forward">워크포워드</option>
                  <option value="kfold">K-폴드</option>
                </select>
              </label>
              <label className="lab-field">분할 시점 (선택 · 쉼표 ISO)
                <input type="text" value={splitDates} placeholder="2018-01-01" style={{ minWidth: 200 }}
                       onChange={(e) => setSplitDates(e.target.value)} />
              </label>
            </>
          )}
        </div>
        {sweepAxis === "parameter" && (
          <div style={{ marginTop: 10 }}>
            <div className="muted" style={{ fontSize: 13, marginBottom: 4 }}>
              파라미터 축 — 2개 이상이면 격자(모든 조합)로 펼칩니다 (예: 비용 민감도).</div>
            {paramAxes.map((ax, i) => (
              <div className="lab-row" key={i} style={{ marginTop: i ? 6 : 0 }}>
                <label className="lab-field">경로
                  <input type="text" value={ax.path} style={{ minWidth: 220 }}
                         onChange={(e) => setParamAxes(paramAxes.map((a, j) =>
                           j === i ? { ...a, path: e.target.value } : a))} />
                </label>
                <label className="lab-field">값들 (쉼표)
                  <input type="text" value={ax.values}
                         onChange={(e) => setParamAxes(paramAxes.map((a, j) =>
                           j === i ? { ...a, values: e.target.value } : a))} />
                </label>
                {paramAxes.length > 1 && (
                  <button type="button" className="ghost sm" style={{ alignSelf: "end" }}
                          onClick={() => setParamAxes(paramAxes.filter((_, j) => j !== i))}>삭제</button>
                )}
              </div>
            ))}
            {paramAxes.length < 3 && (
              <button type="button" className="ghost sm" style={{ marginTop: 6 }}
                      onClick={() => setParamAxes([...paramAxes,
                        { path: "simulation.commission", values: "0, 0.0005, 0.001" }])}>
                + 축 추가 (격자)</button>
            )}
          </div>
        )}
        {sweepAxis === "time" && (
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            이벤트 후 윈도우별 수익 분포·유의성을 분석합니다(P&L 아님).
            국면 블록을 넣으면 이벤트 시점 국면별로 나눠 비교합니다(선택).
          </p>
        )}
        {sweepAxis === "time" && (
          <div style={{ marginTop: 10 }}>
            <div className="muted" style={{ fontSize: 13, marginBottom: 4 }}>
              이벤트 조건 (선택 — 비우면 위 신호가 참인 날을 이벤트로 사용)</div>
            <SentenceTree node={sweepEvent} catalog={catalog} symbols={symbols}
                       selfIndicators={selfIndicators} requiredType="condition"
                       onChange={setSweepEvent} />
          </div>
        )}
        {sweepAxis === "signal" && (
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            손익이 아니라 <b>신호 자체의 값 분포</b>를 봅니다(예: 반감기가 변동성 레짐별로 다른가).
            국면 블록을 넣으면 국면별 분포를 비교합니다(선택).
          </p>
        )}
        {sweepAxis === "relation" && (
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            팩터가 <b>다음 N일 수익을 횡단으로 예측</b>하는지(IC). 종목 2개 이상 필요.
            국면 블록을 넣으면 IC가 국면별로 다른지(팩터 타이밍) 봅니다(선택).
          </p>
        )}
        {sweepAxis === "period" && (
          <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
            전략을 기간으로 나눠 일관성을 봅니다. <b>분할 시점</b>을 비우면 균등 분할,
            채우면 그 시점으로 가릅니다(예: 2018-01-01 → 학습 2010-17 / 검증 2018-).
          </p>
        )}
        {(sweepAxis === "signal" || sweepAxis === "relation") && (
          <div style={{ marginTop: 10 }}>
            <div className="muted" style={{ fontSize: 13, marginBottom: 4 }}>
              {sweepAxis === "signal" ? "분석할 신호 (이 값의 분포를 봅니다)"
                : "팩터 (forward 수익과의 횡단 IC를 봅니다)"}</div>
            <SentenceTree node={targetNode} catalog={catalog} symbols={symbols}
                       selfIndicators={selfIndicators} requiredType={["condition", "score"]}
                       onChange={setTargetNode} />
          </div>
        )}
        {(sweepAxis === "condition" || sweepAxis === "time"
          || sweepAxis === "signal" || sweepAxis === "relation") && (
          <div style={{ marginTop: 10 }}>
            <div className="muted" style={{ fontSize: 13, marginBottom: 4 }}>
              {sweepAxis === "condition" ? "국면 라벨 블록 (예: VIX 구간분할)"
                : "국면 라벨 블록 (선택 — 예: 실현변동성 구간분할)"}</div>
            <SentenceTree node={sweepLabel} catalog={catalog} symbols={symbols}
                       selfIndicators={selfIndicators} requiredType="label"
                       onChange={setSweepLabel} />
          </div>
        )}
      </div>

      {validation && validation.issues.length > 0 && (
        <div className={"panel" + (hasErrors ? " result-fail" : "")}>
          <div className="panel-title">
            {hasErrors ? "논리 오류 — 수정해야 백테스트·저장 가능" : "검토 안내"}
          </div>
          <ul className="issue-list">
            {validation.issues.map((i, k) => (
              <li key={k} className={i.is_error ? "neg" : "muted"}>
                <strong>{i.is_error ? "오류" : "경고"}</strong>: {i.message}
                {i.path !== "root" ? ` (${i.path})` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="lab-actions">
        <button type="button" onClick={run} disabled={running || saving || !signal || hasErrors}>
          {running ? "백테스트 중…" : "백테스트 실행"}
        </button>
        <button type="button" disabled={running || saving || !signal || hasErrors}
                onClick={() => save("draft")}>
          {saving ? "저장 중…" : editId ? "✓ 수정 저장 (새 버전)" : "전략 저장 (초안)"}
        </button>
        <button type="button" className="apply-btn" disabled={running || saving || !signal || hasErrors}
                onClick={() => save("paper")}>
          {saving ? "적용 중…" : "모의 적용"}
        </button>
      </div>
      <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
        모의 적용 시 로컬앱이 매일 09:00 자동 실행합니다. 충분히 검증한 뒤 실전으로 승격하세요.
      </p>

      {result && <ResultPanel result={result} />}
    </div>
  );
}

function ResultPanel({ result }: { result: IrStrategyResult }) {
  if (!result.success) {
    return (
      <div className="panel result-fail">
        <div className="panel-title">실행 실패</div>
        <p className="neg">{result.error}</p>
        {result.issues?.length ? (
          <ul className="issue-list">
            {result.issues.map((i, k) => (
              <li key={k}><code>{i.rule}</code> {i.message}{i.path !== "root" ? ` (${i.path})` : ""}</li>
            ))}
          </ul>
        ) : null}
      </div>
    );
  }

  // ── 신규 동사 라우팅(기존 axis 분기에 선행) — visualization_spec §3.2 ──
  // select: as-of 스냅샷 횡단 랭킹.
  if (result.query === "select") {
    return (
      <div className="panel">
        <div className="panel-title">랭킹 선별 — 현 시점 스냅샷 (백테스트 손익 아님)</div>
        <RankedListChart results={result.results ?? []} as_of={result.as_of}
          universe_size={result.universe_size} eligible_size={result.eligible_size} />
      </div>
    );
  }
  // describe single: 단일종목 360 리포트.
  if (result.report === "single") {
    return (
      <div className="panel">
        <div className="panel-title">종목 현황 (백테스트 손익 아님)</div>
        <ReportCards r={result as unknown as IrSingleReport} />
      </div>
    );
  }
  // describe portfolio: 포트폴리오 진단.
  if (result.report === "portfolio") {
    return (
      <div className="panel">
        <div className="panel-title">포트폴리오 진단 (백테스트 손익 아님)</div>
        <DiagnosisPanel r={result as unknown as IrPortfolioDiagnosis} />
      </div>
    );
  }
  // extremize: 목적함수 최적해 + OOS 가드.
  if (result.reduction === "extremize") {
    return (
      <div className="panel">
        <div className="panel-title">최적해 탐색</div>
        <ExtremizeChart r={result as unknown as IrExtremizeResult} />
      </div>
    );
  }
  // relate regression: 다중팩터 횡단 회귀(forward 예측).
  if (result.axis === "relation" && result.relation === "regression") {
    return (
      <div className="panel">
        <div className="panel-title">다중팩터 회귀 — forward 예측력 (손익 아님)</div>
        <RegressionChart r={result as unknown as IrRegressionResult} />
      </div>
    );
  }

  // 이벤트 스터디 결과 (A2)
  if (result.axis === "time") {
    return <EventStudyPanel result={result} />;
  }
  // 신호값 분포 (target=signal)
  if (result.axis === "signal") {
    return <SignalStudyPanel result={result} />;
  }
  // 횡단 IC (target=relation)
  if (result.axis === "relation") {
    return <ICStudyPanel result={result} />;
  }

  // 펼침 결과 — 버킷 표
  if (result.axis && result.buckets) {
    const rows = Object.entries(result.buckets);
    const pairwise = result.compare?.pairwise ?? {};
    return (
      <div className="panel">
        <div className="panel-title">펼침 결과 — {result.axis === "parameter" ? "파라미터"
          : result.axis === "asset" ? "종목별"
          : result.axis === "period_split" ? "기간분할" : "국면별"}</div>
        {result.warnings?.length ? (
          <div className="warn-banner">⚠ {result.warnings.map((w) => w.message).join(" · ")}</div>
        ) : null}
        <SweepChart axis={result.axis} buckets={result.buckets} axes={result.axes} />
        <table className="sweep-table">
          <thead><tr><th>구분</th><th>표본</th><th>누적(%)</th><th>CAGR(%)</th>
            <th>MDD(%)</th><th>샤프</th><th>소르티노</th><th>승률(%)</th><th>손익비</th></tr></thead>
          <tbody>
            {rows.map(([k, b]) => (
              b.error ? (
                <tr key={k}><td>{k}</td><td colSpan={8} className="neg">{b.error}</td></tr>
              ) : (
                <tr key={k}>
                  <td>{k}</td>
                  <td>{b.n ?? "—"}</td>
                  <td className={(b.cum_return ?? 0) >= 0 ? "pos" : "neg"}>{fmt(b.cum_return, "")}</td>
                  <td>{fmt(b.cagr, "")}</td>
                  <td className="neg">{fmt(b.mdd, "")}</td>
                  <td>{fmt(b.sharpe, "")}</td>
                  <td>{fmt(b.sortino, "")}</td>
                  <td>{fmt(b.win_rate, "")}</td>
                  <td>{fmt(b.payoff_ratio, "")}</td>
                </tr>
              )
            ))}
          </tbody>
        </table>
        {Object.keys(pairwise).length ? (
          <div className="muted" style={{ fontSize: 13, marginTop: 8 }}>
            유의성(2표본 t): {Object.entries(pairwise).map(([k, v]) =>
              `${k} → p=${v.p_value != null ? v.p_value.toFixed(4) : "—"}` +
              (v.p_value != null && v.p_value < 0.05 ? " (유의)" : "")).join(" · ")}
          </div>
        ) : null}
      </div>
    );
  }

  // 단일 백테스트
  const m = result.metrics ?? {};
  return (
    <div className="panel">
      <div className="panel-title">백테스트 결과</div>
      {result.warnings?.length ? (
        <div className="warn-banner">⚠ {result.warnings.map((w) => w.message).join(" · ")}</div>
      ) : null}
      <div className="stat-grid">
        {METRIC_LABELS.map(([key, label, suf]) => {
          const v = m[key];
          const pol = (key === "total_return" || key === "cagr")
            ? (typeof v === "number" ? (v >= 0 ? "pos" : "neg") : "") : "";
          return (
            <div className="stat" key={key}>
              <div className="label">{label}</div>
              <div className={"value " + pol}>{fmt(v, suf)}</div>
            </div>
          );
        })}
      </div>
      {result.equity?.length ? (
        <div style={{ marginTop: 16 }}>
          <EquityChart equity={result.equity} benchmark={result.benchmark} trades={result.trades} />
        </div>
      ) : null}
    </div>
  );
}

function SignalStudyPanel({ result }: { result: IrStrategyResult }) {
  const dist = (result.overall ?? {}) as IrDistribution;
  const reg = result.by_regime as unknown as IrPartition | null | undefined;
  const q = dist.quantiles ?? {};
  return (
    <div className="panel">
      <div className="panel-title">신호값 분포</div>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        신호 자체의 값 분포(손익 아님) · 표본 {dist.n ?? 0}개. 국면 블록을 넣으면 국면별로 비교됩니다.
      </p>
      <SignalDistChart overall={dist} byRegime={reg} />
      <div className="stat-grid">
        <div className="stat"><div className="label">평균</div><div className="value">{fmt(dist.mean, "")}</div></div>
        <div className="stat"><div className="label">표준편차</div><div className="value">{fmt(dist.std, "")}</div></div>
        <div className="stat"><div className="label">중앙값</div><div className="value">{fmt(q.q50, "")}</div></div>
        <div className="stat"><div className="label">하위5%</div><div className="value neg">{fmt(q.q05, "")}</div></div>
        <div className="stat"><div className="label">상위5%</div><div className="value pos">{fmt(q.q95, "")}</div></div>
        <div className="stat"><div className="label">왜도</div><div className="value">{fmt(dist.skew, "")}</div></div>
        <div className="stat"><div className="label">첨도</div><div className="value">{fmt(dist.kurtosis, "")}</div></div>
      </div>
      {reg?.by_label && Object.keys(reg.by_label).length ? (
        <>
          <div className="panel-title" style={{ marginTop: 18, fontSize: 14 }}>국면별 분포</div>
          <table className="sweep-table">
            <thead><tr><th>국면</th><th>표본</th><th>평균</th><th>중앙값</th>
              <th>하위5%</th><th>상위5%</th><th>왜도</th></tr></thead>
            <tbody>
              {Object.entries(reg.by_label).map(([k, d]) => (
                <tr key={k}>
                  <td>{k}</td><td>{d.n ?? "—"}</td>
                  <td>{fmt(d.mean, "")}</td><td>{fmt(d.quantiles?.q50, "")}</td>
                  <td className="neg">{fmt(d.quantiles?.q05, "")}</td>
                  <td className="pos">{fmt(d.quantiles?.q95, "")}</td>
                  <td>{fmt(d.skew, "")}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {reg.pairwise && Object.keys(reg.pairwise).length ? (
            <div className="muted" style={{ fontSize: 13, marginTop: 8 }}>
              국면 차이(2표본 t): {Object.entries(reg.pairwise).map(([k, v]) =>
                `${k} → p=${v.p_value != null ? v.p_value.toFixed(4) : "—"}`
                + (v.p_value != null && v.p_value < 0.05 ? " (유의)" : "")).join(" · ")}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function ICStudyPanel({ result }: { result: IrStrategyResult }) {
  const windows = result.windows ?? [];
  const byWindow = result.by_window ?? {};
  return (
    <div className="panel">
      <div className="panel-title">팩터 IC — forward 수익 예측력</div>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        매 거래일 횡단 순위상관(IC). 평균 IC가 0보다 유의하면 예측력 있음(분석 전용 — 미래참조).
        IR = 평균IC ÷ IC표준편차. p&lt;0.05면 유의.
      </p>
      <ICChart windows={windows} byWindow={byWindow} />
      <table className="sweep-table">
        <thead><tr><th>예측 horizon(일)</th><th>표본</th><th>평균 IC</th><th>IR</th>
          <th>t</th><th>양(+)확률(%)</th><th>p-value</th></tr></thead>
        <tbody>
          {windows.map((w) => {
            const o = byWindow[w]?.overall ?? ({} as IrICStat);
            return (
              <tr key={w}>
                <td>{w}</td><td>{o.n ?? "—"}</td>
                <td className={(o.mean ?? 0) >= 0 ? "pos" : "neg"}>
                  {o.mean != null ? o.mean.toFixed(4) : "—"}</td>
                <td>{o.ir != null ? o.ir.toFixed(3) : "—"}</td>
                <td>{o.t_stat != null ? o.t_stat.toFixed(2) : "—"}</td>
                <td>{fmt(o.prob_positive, "")}</td>
                <td className={o.p_value != null && o.p_value < 0.05 ? "pos" : ""}>
                  {o.p_value != null ? o.p_value.toFixed(4) : "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function EventStudyPanel({ result }: { result: IrStrategyResult }) {
  const windows = result.windows ?? [];
  const overall = (result.overall ?? {}) as Record<string, IrEventStat>;
  const byRegime = result.by_regime;
  const pcell = (p?: number) => (
    <td className={p != null && p < 0.05 ? "pos" : ""}>
      {p != null ? p.toFixed(4) : "—"}</td>);
  const basisLabel = { close: "종가→종가", intraday: "시가→종가(당일)", excess: "시장초과" }[
    result.basis ?? "close"] ?? "종가→종가";
  return (
    <div className="panel">
      <div className="panel-title">이벤트 분석 — forward 수익 분포</div>
      <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
        총 이벤트 {result.n_events ?? 0}건 · 기준 {basisLabel}. p&lt;0.05면 평균이 0과 유의하게 다름.
        MAE=보유 중 평균 최대낙폭, MFE=평균 최대상승.
      </p>
      <EventStudyChart windows={windows} overall={overall} />
      <table className="sweep-table">
        <thead><tr><th>윈도우(일)</th><th>표본</th><th>평균수익(%)</th>
          <th>MAE(%)</th><th>MFE(%)</th><th>양(+)확률(%)</th><th>손익비</th><th>p-value</th></tr></thead>
        <tbody>
          {windows.map((w) => {
            const o = overall[w] ?? ({} as IrEventStat);
            return (
              <tr key={w}>
                <td>{w}</td><td>{o.n ?? "—"}</td>
                <td className={(o.mean ?? 0) >= 0 ? "pos" : "neg"}>{fmt(o.mean, "")}</td>
                <td className="neg">{fmt(o.mean_mae, "")}</td>
                <td className="pos">{fmt(o.mean_mfe, "")}</td>
                <td>{fmt(o.prob_positive, "")}</td>
                <td>{fmt(o.payoff_ratio, "")}</td>
                {pcell(o.p_value)}
              </tr>
            );
          })}
        </tbody>
      </table>

      {byRegime ? (
        <>
          <div className="panel-title" style={{ marginTop: 18, fontSize: 14 }}>
            국면별 (이벤트 시점 기준)</div>
          {windows.map((w) => {
            const wr = byRegime[w];
            if (!wr) return null;
            return (
              <div key={w} style={{ marginBottom: 12 }}>
                <div className="muted" style={{ fontSize: 12, marginBottom: 2 }}>{w}일 후</div>
                <table className="sweep-table">
                  <thead><tr><th>국면</th><th>표본</th><th>평균수익(%)</th>
                    <th>MAE(%)</th><th>양확률(%)</th><th>p(vs 0)</th></tr></thead>
                  <tbody>
                    {Object.entries(wr.by_regime).map(([rk, o]) => (
                      <tr key={rk}>
                        <td>{rk}</td><td>{o.n ?? "—"}</td>
                        <td className={(o.mean ?? 0) >= 0 ? "pos" : "neg"}>{fmt(o.mean, "")}</td>
                        <td className="neg">{fmt(o.mean_mae, "")}</td>
                        <td>{fmt(o.prob_positive, "")}</td>
                        {pcell(o.p_value)}
                      </tr>
                    ))}
                  </tbody>
                </table>
                {Object.keys(wr.pairwise).length ? (
                  <div className="muted" style={{ fontSize: 12, marginTop: 2 }}>
                    국면 차이: {Object.entries(wr.pairwise).map(([k, v]) =>
                      `${k} Δ=${v.mean_diff != null ? v.mean_diff.toFixed(2) : "—"}%, ` +
                      `p=${v.p_value != null ? v.p_value.toFixed(4) : "—"}` +
                      (v.p_value != null && v.p_value < 0.05 ? " (유의)" : "")).join(" · ")}
                  </div>
                ) : null}
              </div>
            );
          })}
        </>
      ) : null}

      {result.warnings?.length ? (
        <div className="warn-banner" style={{ marginTop: 10 }}>
          ⚠ {result.warnings.map((w) => w.message).join(" · ")}</div>
      ) : null}
    </div>
  );
}

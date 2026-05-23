/**
 * 내 전략 — 통합 카드뷰 + 필터 + 상세 + 실전 승격 모달.
 *
 * 모의↔실전을 별도 페이지로 나누는 대신 한 페이지의 카드 그리드 + 배지로 통합.
 * 헤더 모의/실전 토글과 필터가 동기화돼 사용자가 같은 정신모델로 이동.
 *
 * 승격 모달은 사용자가 진짜 돈을 거는 순간 — 가장 정성스럽게 만들어야 한다.
 * 모의 성과 요약 + 자본 비중 입력 + 명시적 확인의 3단계.
 */

import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import type { StrategyDef, StrategyRow, SyncSnapshot } from "../types";
import { EXECUTION_DEFAULTS, parseScreenerKey, parseTradeSymbols } from "../types";

/** "005930 외 2종목" 형태로 다중 종목 축약. 단일이면 코드 그대로. */
function summarizeTargets(tradeSymbol: string): string {
  const { mode, symbols } = parseTradeSymbols(tradeSymbol);
  if (mode === "screener") return tradeSymbol;
  if (symbols.length === 0) return "(없음)";
  if (symbols.length === 1) return symbols[0];
  return `${symbols[0]} 외 ${symbols.length - 1}종목`;
}

type Filter = "all" | "paper" | "live" | "draft";

const FILTER_LABEL: Record<Filter, string> = {
  all: "전체", paper: "모의", live: "실전", draft: "초안",
};

const pnl = (v?: number | null) =>
  v == null ? "-" : (v >= 0 ? "+" : "") + v.toLocaleString() + "원";
const pct = (v?: number | null) =>
  v == null ? "-" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%";

export default function Strategies() {
  const [rows, setRows] = useState<StrategyRow[]>([]);
  const [snap, setSnap] = useState<SyncSnapshot | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [err, setErr] = useState("");
  const [filter, setFilter] = useState<Filter>("paper");

  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [promoteFor, setPromoteFor] = useState<StrategyRow | null>(null);

  function load() {
    setErr("");
    Promise.all([
      api.listStrategies(),
      api.snapshot().catch(() => null),
    ])
      .then(([rs, s]) => { setRows(rs); setSnap(s); })
      .catch((e) => setErr((e as Error).message))
      .finally(() => setLoaded(true));
  }
  // 데이터 패칭은 의도적 effect — react-hooks/set-state-in-effect는 적절한 dependencies로
  // 해소되지 않는 사용자 트리거 fetch에 대해선 disable.
  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(load, []);

  async function changeMode(s: StrategyRow, runMode: string) {
    setErr("");
    try {
      await api.updateStrategy(s.id, s.definition, runMode);
      load();
    } catch (e) { setErr((e as Error).message); }
  }

  async function remove(id: number) {
    if (!confirm("이 전략을 삭제할까요? 되돌릴 수 없습니다.")) return;
    await api.deleteStrategy(id);
    if (selectedId === id) setSelectedId(null);
    load();
  }

  const filtered = useMemo(() => {
    if (filter === "all") return rows;
    return rows.filter((r) => r.run_mode === filter);
  }, [rows, filter]);

  const counts = useMemo(() => ({
    all: rows.length,
    paper: rows.filter((r) => r.run_mode === "paper").length,
    live: rows.filter((r) => r.run_mode === "live").length,
    draft: rows.filter((r) => r.run_mode === "draft").length,
  }), [rows]);

  const selected = useMemo(
    () => rows.find((r) => r.id === selectedId) ?? null,
    [rows, selectedId]
  );

  const strategyPnl = snap?.payload.strategy_pnl;
  const positions = snap?.payload.positions ?? [];

  return (
    <div>
      <h1 className="page-title">내 전략</h1>
      <p className="page-sub">
        전략을 모의로 검증하고, 충분히 안정되면 실전으로 승격하세요.
      </p>

      {err && <div className="error">{err}</div>}
      {!loaded && <p className="muted">불러오는 중…</p>}

      {loaded && rows.length === 0 && (
        <div className="panel empty-state">
          <div className="empty-title">아직 저장된 전략이 없습니다</div>
          <p className="muted">
            전략 만들기에서 매수·매도 조건을 짜고 백테스트로 검증한 뒤 저장하세요.
            저장한 전략을 모의로 두면 로컬앱이 매일 09:00 자동 실행합니다.
          </p>
          <Link to="/backtest"><button>전략 만들기로 이동</button></Link>
        </div>
      )}

      {loaded && rows.length > 0 && (
        <>
          {/* 필터 탭 */}
          <div className="filter-tabs">
            {(["all", "paper", "live", "draft"] as const).map((f) => (
              <button
                key={f}
                className={"filter-tab" + (filter === f ? " on" : "")}
                onClick={() => setFilter(f)}
              >
                {FILTER_LABEL[f]} <span className="count">{counts[f]}</span>
              </button>
            ))}
          </div>

          {filtered.length === 0 && (
            <div className="panel empty">
              {FILTER_LABEL[filter]} 모드에 전략이 없습니다
            </div>
          )}

          {/* 카드 그리드 */}
          {filtered.length > 0 && (
            <div className="strategy-grid">
              {filtered.map((s) => (
                <StrategyCard
                  key={s.id}
                  strategy={s}
                  pnl={strategyPnl?.by_strategy.find(r => r.strategy === s.name)}
                  positionCount={positions.filter(p => p.strategy_name === s.name).length}
                  onClick={() => setSelectedId(s.id)}
                />
              ))}
            </div>
          )}
        </>
      )}

      {/* 상세 패널 (사이드 슬라이드) */}
      {selected && (
        <DetailPanel
          strategy={selected}
          pnl={strategyPnl?.by_strategy.find(r => r.strategy === selected.name)}
          positionCount={positions.filter(p => p.strategy_name === selected.name).length}
          onClose={() => setSelectedId(null)}
          onChangeMode={(m) => changeMode(selected, m)}
          onRemove={() => remove(selected.id)}
          onPromote={() => setPromoteFor(selected)}
        />
      )}

      {/* 실전 승격 모달 */}
      {promoteFor && (
        <PromoteModal
          strategy={promoteFor}
          pnl={strategyPnl?.by_strategy.find(r => r.strategy === promoteFor.name)}
          onCancel={() => setPromoteFor(null)}
          onConfirm={async (amountPct) => {
            const def = { ...promoteFor.definition, amount_pct: amountPct };
            await api.updateStrategy(promoteFor.id, def, "live");
            setPromoteFor(null);
            load();
          }}
        />
      )}
    </div>
  );
}

function StrategyCard({
  strategy: s, pnl: row, positionCount, onClick,
}: {
  strategy: StrategyRow;
  pnl?: { pnl: number; today_pnl: number; trades: number; win_rate: number };
  positionCount: number;
  onClick: () => void;
}) {
  const buyN = s.definition.buy?.conditions?.length ?? 0;
  // Phase 32 — sell_rules 우선, legacy sell fallback
  const sellExtraN = s.definition.sell_rules?.conditions?.length
    ?? s.definition.sell?.conditions?.length ?? 0;
  const sr = s.definition.sell_rules ?? {};
  const sellRuleCount = [sr.take_profit, sr.stop_loss, sr.trail_pct,
                          sr.trail_atr_mult, sr.hold_days]
    .filter((v) => v != null).length + sellExtraN;
  const screenerKey = parseScreenerKey(s.definition.trade_symbol);

  return (
    <button className="strategy-card" onClick={onClick}>
      <div className="sc-head">
        <span className="sc-name">{s.name}</span>
        <span className={"sc-badge " + s.run_mode}>
          {s.run_mode === "live" ? "실전"
            : s.run_mode === "paper" ? "모의"
            : "초안"}
        </span>
      </div>
      <div className="sc-target">
        {screenerKey
          ? <>자동 선택: <code>{screenerKey}</code></>
          : <>{summarizeTargets(s.definition.trade_symbol)}</>}
      </div>
      <div className="sc-meta">
        매수 {buyN} · 매도 {sellRuleCount} 규칙 · 자본 {s.definition.amount_pct}%
      </div>
      <div className="sc-stats">
        <div className="sc-stat">
          <span className="sc-stat-label">누적 P&L</span>
          <span className={"sc-stat-value " +
            (row?.pnl == null ? "" : row.pnl >= 0 ? "pos" : "neg")}>
            {row ? pnl(row.pnl) : "-"}
          </span>
        </div>
        <div className="sc-stat">
          <span className="sc-stat-label">보유</span>
          <span className="sc-stat-value">
            {positionCount > 0 ? `${positionCount}종목` : "없음"}
          </span>
        </div>
        <div className="sc-stat">
          <span className="sc-stat-label">승률</span>
          <span className="sc-stat-value">
            {row?.win_rate != null ? pct(row.win_rate * 100) : "-"}
          </span>
        </div>
      </div>
    </button>
  );
}

function DetailPanel({
  strategy: s, pnl: row, positionCount,
  onClose, onChangeMode, onRemove, onPromote,
}: {
  strategy: StrategyRow;
  pnl?: { pnl: number; today_pnl: number; trades: number; win_rate: number };
  positionCount: number;
  onClose: () => void;
  onChangeMode: (m: string) => void;
  onRemove: () => void;
  onPromote: () => void;
}) {
  const [tab, setTab] = useState<"overview" | "performance" | "settings">("overview");
  // Phase 32 — sell_rules 우선, legacy sell+exit_rules fallback
  const sr = s.definition.sell_rules ?? {
    take_profit: s.definition.exit_rules?.take_profit,
    stop_loss: s.definition.exit_rules?.stop_loss,
    trail_pct: s.definition.exit_rules?.trail_pct,
    trail_atr_mult: s.definition.exit_rules?.trail_atr_mult,
    hold_days: s.definition.exit_rules?.hold_days,
    conditions: s.definition.sell?.conditions ?? [],
    sell_amount_pct: s.definition.sell_amount_pct,
  };
  const buyN = s.definition.buy?.conditions?.length ?? 0;
  const sellExtraN = sr.conditions?.length ?? 0;

  return (
    <div className="detail-overlay" onClick={onClose}>
      <aside className="detail-panel" onClick={(e) => e.stopPropagation()}>
        <header className="detail-head">
          <div>
            <h2>{s.name}</h2>
            <div className="detail-sub">
              <span className={"sc-badge " + s.run_mode}>
                {s.run_mode === "live" ? "실전"
                  : s.run_mode === "paper" ? "모의"
                  : "초안"}
              </span>
              <span className="muted">
                {summarizeTargets(s.definition.trade_symbol)}
              </span>
            </div>
          </div>
          <button className="ghost sm" onClick={onClose}>✕</button>
        </header>

        <nav className="detail-tabs">
          {(["overview", "performance", "settings"] as const).map((t) => (
            <button
              key={t}
              className={"detail-tab" + (tab === t ? " on" : "")}
              onClick={() => setTab(t)}
            >
              {t === "overview" ? "개요"
                : t === "performance" ? "성과"
                : "설정"}
            </button>
          ))}
        </nav>

        <div className="detail-body">
          {tab === "overview" && (
            <>
              <section>
                <h4>매매 규칙</h4>
                <div className="rule-row">
                  <span className="rule-label">매수 조건</span>
                  <span className="rule-val">{buyN}개</span>
                </div>
                <div className="rule-row">
                  <span className="rule-label">자본 비중</span>
                  <span className="rule-val">
                    {s.definition.amount_pct}%
                  </span>
                </div>
              </section>

              <section>
                <h4>매도 조건 (먼저 트리거되는 규칙으로 매도)</h4>
                <ExitRow label="익절" v={sr.take_profit != null ? `+${sr.take_profit}%` : "—"} />
                <ExitRow label="손절" v={sr.stop_loss != null ? `${sr.stop_loss}%` : "—"} />
                <ExitRow label="보유기간" v={sr.hold_days != null ? `${sr.hold_days}일` : "—"} />
                <ExitRow label="트레일링 %" v={sr.trail_pct != null ? `-${sr.trail_pct}%` : "—"} />
                <ExitRow label="트레일링 ATR" v={sr.trail_atr_mult != null ? `×${sr.trail_atr_mult}` : "—"} />
                <ExitRow label="추가 조건" v={sellExtraN > 0 ? `${sellExtraN}개` : "—"} />
                <ExitRow label="매도 비율" v={`${sr.sell_amount_pct ?? 100}%`} />
              </section>

              <RiskSummarySection definition={s.definition} />

              <section>
                <h4>일정</h4>
                <div className="rule-row">
                  <span className="rule-label">생성</span>
                  <span className="rule-val">{s.created_at.slice(0, 10)}</span>
                </div>
                <div className="rule-row">
                  <span className="rule-label">최근 수정</span>
                  <span className="rule-val">{s.updated_at.slice(0, 10)}</span>
                </div>
              </section>
            </>
          )}

          {tab === "performance" && (
            <>
              <section>
                <h4>라이브 성과</h4>
                {row ? (
                  <div className="perf-grid">
                    <PerfBox label="누적 P&L"
                      value={pnl(row.pnl)}
                      cls={row.pnl >= 0 ? "pos" : "neg"} />
                    <PerfBox label="오늘 P&L"
                      value={pnl(row.today_pnl)}
                      cls={row.today_pnl >= 0 ? "pos" : "neg"} />
                    <PerfBox label="거래 수" value={row.trades.toString()} />
                    <PerfBox label="승률"
                      value={pct(row.win_rate * 100)} />
                  </div>
                ) : (
                  <div className="empty">아직 라이브 거래 기록 없음</div>
                )}
              </section>
              <section>
                <h4>현재 보유</h4>
                <div className="rule-row">
                  <span className="rule-label">활성 포지션</span>
                  <span className="rule-val">
                    {positionCount > 0 ? `${positionCount}종목` : "없음"}
                  </span>
                </div>
              </section>
              <section>
                <h4>백테스트</h4>
                <p className="muted small">
                  과거 데이터 검증 결과는 전략 만들기 페이지에서 확인하세요.
                </p>
                <Link to="/backtest" className="cta">전략 만들기로 →</Link>
              </section>
            </>
          )}

          {tab === "settings" && (
            <>
              <section>
                <h4>운용 모드</h4>
                <div className="mode-rows">
                  {(["draft", "paper"] as const).map((m) => (
                    <label key={m} className="mode-row">
                      <input
                        type="radio"
                        name="runmode"
                        checked={s.run_mode === m}
                        onChange={() => onChangeMode(m)}
                      />
                      <span>
                        {m === "draft" ? "초안 (자동매매 미실행)" : "모의투자"}
                      </span>
                    </label>
                  ))}
                </div>
                {s.run_mode !== "live" ? (
                  <button className="promote-btn" onClick={onPromote}>
                    실전으로 승격
                  </button>
                ) : (
                  <div className="live-active">
                    <span className="sc-badge live">실전 운용 중</span>
                    <button className="ghost sm"
                      onClick={() => onChangeMode("paper")}>
                      모의로 되돌리기
                    </button>
                  </div>
                )}
              </section>

              <section>
                <h4>위험 작업</h4>
                <button className="danger-btn" onClick={onRemove}>
                  전략 삭제
                </button>
              </section>
            </>
          )}
        </div>
      </aside>
    </div>
  );
}

function PromoteModal({
  strategy: s, pnl: row, onCancel, onConfirm,
}: {
  strategy: StrategyRow;
  pnl?: { pnl: number; today_pnl: number; trades: number; win_rate: number };
  onCancel: () => void;
  onConfirm: (amountPct: number) => void;
}) {
  const [amountPct, setAmountPct] = useState(
    Math.min(s.definition.amount_pct, 10)
  );
  const [step, setStep] = useState<1 | 2>(1);
  const [confirmText, setConfirmText] = useState("");

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <div className="modal promote-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>실전 승격 — {s.name}</h2>
          <button className="ghost sm" onClick={onCancel}>✕</button>
        </header>

        <div className="modal-body">
          <div className="step-pills">
            <span className={"step-pill" + (step >= 1 ? " on" : "")}>
              1. 성과 확인
            </span>
            <span className={"step-pill" + (step >= 2 ? " on" : "")}>
              2. 자본 비중 + 최종 확인
            </span>
          </div>

          {step === 1 && (
            <>
              <section>
                <h4>모의 성과</h4>
                {row && row.trades > 0 ? (
                  <div className="perf-grid">
                    <PerfBox label="누적 P&L" value={pnl(row.pnl)}
                      cls={row.pnl >= 0 ? "pos" : "neg"} />
                    <PerfBox label="거래 수" value={row.trades.toString()} />
                    <PerfBox label="승률"
                      value={pct(row.win_rate * 100)} />
                    <PerfBox label="평균 P&L/거래"
                      value={pnl(row.pnl / row.trades)} />
                  </div>
                ) : (
                  <div className="warn-box">
                    ⚠ 모의 거래 기록이 없습니다.
                    실전 승격 전에 모의로 충분히 검증하길 권장합니다.
                  </div>
                )}
              </section>
              <section>
                <h4>전략 정보</h4>
                <div className="rule-row">
                  <span className="rule-label">매수 대상</span>
                  <span className="rule-val">{summarizeTargets(s.definition.trade_symbol)}</span>
                </div>
                <div className="rule-row">
                  <span className="rule-label">현재 자본 비중</span>
                  <span className="rule-val">{s.definition.amount_pct}%</span>
                </div>
              </section>
            </>
          )}

          {step === 2 && (
            <>
              <section>
                <h4>실전 자본 비중</h4>
                <p className="muted small">
                  실전 계좌 잔고 대비 1회 매수 시 투입할 비율.
                  처음엔 작게 시작 (5~10%) 권장.
                </p>
                <div className="amount-input-row">
                  <input
                    type="number"
                    min={1} max={100} step={1}
                    value={amountPct}
                    onChange={(e) => setAmountPct(Number(e.target.value))}
                  />
                  <span>%</span>
                </div>
                <div className="amount-hint muted small">
                  예: 잔고 1,000만원 × {amountPct}% = {(amountPct * 10).toLocaleString()}만원 / 매수
                </div>
              </section>
              <section>
                <h4>확인</h4>
                <div className="warn-box">
                  ⚠ 실전 모드에서는 실제 계좌의 자금이 사용됩니다.
                  일일 손실 한도·킬스위치 등 안전장치가 동작하지만
                  손실 위험을 완전히 제거하지 않습니다.
                </div>
                <p className="small">
                  계속하려면 <strong>실전 시작</strong>을 입력하세요.
                </p>
                <input
                  type="text"
                  placeholder="실전 시작"
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  className="confirm-input"
                />
              </section>
            </>
          )}
        </div>

        <footer className="modal-foot">
          {step === 1 && (
            <>
              <button className="ghost" onClick={onCancel}>취소</button>
              <button onClick={() => setStep(2)}>다음 →</button>
            </>
          )}
          {step === 2 && (
            <>
              <button className="ghost" onClick={() => setStep(1)}>← 이전</button>
              <button
                className="danger-btn"
                disabled={confirmText !== "실전 시작" || amountPct < 1}
                onClick={() => onConfirm(amountPct)}
              >
                실전 운용 시작
              </button>
            </>
          )}
        </footer>
      </div>
    </div>
  );
}

function ExitRow({ label, v }: { label: string; v: string }) {
  return (
    <div className="rule-row">
      <span className="rule-label">{label}</span>
      <span className="rule-val">{v}</span>
    </div>
  );
}

/** 전략 상세 패널 — 사이징/리스크 요약 (read-only).
 *  execution 미설정 시 글로벌 default(EXECUTION_DEFAULTS) 표시. */
function RiskSummarySection({ definition: d }: { definition: StrategyDef }) {
  const e = d.execution ?? {};
  const mode = e.sizing_mode ?? EXECUTION_DEFAULTS.sizing_mode;
  const amountKrw = e.amount_krw ?? EXECUTION_DEFAULTS.amount_krw;
  const atrPct = e.atr_risk_pct ?? EXECUTION_DEFAULTS.atr_risk_pct;
  const atrMul = e.atr_mult ?? EXECUTION_DEFAULTS.atr_mult;
  const maxPos = e.max_position_pct ?? EXECUTION_DEFAULTS.max_position_pct;
  const dailyLoss = e.daily_loss_limit_pct ?? EXECUTION_DEFAULTS.daily_loss_limit_pct;
  const maxDd = e.max_drawdown_pct ?? EXECUTION_DEFAULTS.max_drawdown_pct;
  // Phase 39 + C-01 — 백테스트 비용 가정 (전략에 명시 저장된 경우만 표시)
  const hasBtCost = (e.bt_commission_bps !== undefined && e.bt_commission_bps !== null)
    || (e.bt_sell_tax_bps !== undefined && e.bt_sell_tax_bps !== null)
    || (e.bt_slippage_bps !== undefined && e.bt_slippage_bps !== null)
    || (e.bt_gap_extra_cost !== undefined && e.bt_gap_extra_cost !== null)
    || (e.bt_gap_threshold_pct !== undefined && e.bt_gap_threshold_pct !== null);
  const btCom = e.bt_commission_bps ?? EXECUTION_DEFAULTS.bt_commission_bps;
  const btTax = e.bt_sell_tax_bps ?? EXECUTION_DEFAULTS.bt_sell_tax_bps;
  const btSlip = e.bt_slippage_bps ?? EXECUTION_DEFAULTS.bt_slippage_bps;
  const btGap = e.bt_gap_extra_cost ?? EXECUTION_DEFAULTS.bt_gap_extra_cost;
  const btGapTh = e.bt_gap_threshold_pct ?? EXECUTION_DEFAULTS.bt_gap_threshold_pct;
  return (
    <section>
      <h4>리스크 한도</h4>
      <ExitRow
        label="사이징"
        v={
          mode === "atr_risk"
            ? `리스크 기반 (자본 ${atrPct}% 위험 / ATR×${atrMul})`
            : mode === "fixed_amount"
              ? `정액 (한 종목당 ${amountKrw.toLocaleString()}원)`
              : mode === "equal_weight"
                ? `균등 분배 (${d.screener_limit ?? 5}종목)`
                : `정률 (자본의 ${d.amount_pct}%)`
        }
      />
      <ExitRow label="단일 종목 상한" v={`${maxPos}%`} />
      <ExitRow label="일일 손실 한도" v={`${dailyLoss}%`} />
      <ExitRow label="누적 손실 한도" v={`${maxDd}%`} />
      {hasBtCost && (
        <>
          <h4 style={{ marginTop: 12 }}>백테스트 비용 가정 <span className="muted small">(실매매 영향 없음)</span></h4>
          <ExitRow label="위탁수수료 (편도)" v={`${btCom} bps`} />
          <ExitRow label="거래세 (매도 단방향)" v={`${btTax} bps`} />
          <ExitRow label="슬리피지 (편도)" v={`${btSlip} bps`} />
          <ExitRow label="갭일 추가 비용" v={btGap ? `ON (≥${btGapTh}%)` : "OFF"} />
        </>
      )}
    </section>
  );
}

function PerfBox({ label, value, cls }: {
  label: string; value: string; cls?: string;
}) {
  return (
    <div className="perf-box">
      <div className="perf-label">{label}</div>
      <div className={"perf-value " + (cls ?? "")}>{value}</div>
    </div>
  );
}

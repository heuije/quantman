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

import { useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type {
  BacktestRunSummary, IrStrategyDef,
  StrategyRow, StrategyStats, StrategyVersionRow,
} from "../types";

type TabKey = "config" | "versions" | "stats" | "backtests";

const TAB_LABEL: Record<TabKey, string> = {
  config: "설정값",
  versions: "버전",
  stats: "현황",
  backtests: "백테스트 내역",
};

const krw = (v: number | null | undefined) =>
  v == null ? "—" : (v >= 0 ? "+" : "") + v.toLocaleString() + "원";
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
    ])
      .then(([s, st, vs, bs]) => {
        setStrategy(s); setStats(st); setVersions(vs); setBacktests(bs);
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
        <IrConfigTab strategy={strategy} onRemove={remove} onDemote={demote} />
      ) : (
        <LegacyConfigTab runMode={strategy.run_mode} onRemove={remove} onDemote={demote} />
      ))}
      {tab === "versions" && (
        <VersionsTab versions={versions} backtests={backtests} onRestore={restoreVersion} />
      )}
      {tab === "stats" && <StatsTab stats={stats} strategy={strategy} />}
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

function IrConfigTab({ strategy, onRemove, onDemote }: {
  strategy: StrategyRow;
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

  return (
    <div className="strategy-detail-body">
      <p className="muted small">
        전략 연구소(IR)에서 만든 전략입니다. 전체 설정을 조회하고, 연구소에서 신호·진입·청산을 수정하세요.
      </p>

      <section className="panel">
        <h4>유니버스</h4>
        <Rule label="대상" v={summarizeIrUniverse(def)} />
      </section>

      <section className="panel" style={{ marginTop: 12 }}>
        <h4>진입 · 포지션</h4>
        <Rule label="진입 트리거" v={IR_ENTRY_LABEL[entry.mode ?? "on_signal"] ?? "이벤트"} />
        {entry.mode === "scheduled" && (
          <Rule label="리밸런싱" v={IR_REBALANCE_LABEL[entry.rebalance ?? "monthly"] ?? "매월"} />
        )}
        <Rule label="방향" v={IR_DIR_LABEL[p.direction ?? "long"] ?? "롱"} />
        <Rule label="사이징" v={IR_SIZING_LABEL[sizing.mode ?? "equal_weight"] ?? "동일가중"} />
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

      <section className="panel" style={{ marginTop: 12 }}>
        <h4>시뮬레이션</h4>
        <Rule label="기간" v={`${sim.start || "전체"} ~ ${sim.end || "전체"}`} />
        <Rule label="초기자본" v={`${(sim.initial_capital ?? 10_000_000).toLocaleString()}원`} />
        <Rule label="체결" v={`지연 ${sim.delay ?? 1}일 · ${sim.fill === "close" ? "당일 종가"
          : sim.fill === "typical" ? "당일 (고+저+종)/3" : "익일 시가"}`} />
        {analysisSummary && <Rule label="분석" v={analysisSummary} />}
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

function LegacyConfigTab({ runMode, onRemove, onDemote }: {
  runMode: string;
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

function StatsTab({ stats, strategy }: {
  stats: StrategyStats | null;
  strategy: StrategyRow;
}) {
  if (!stats) return <p className="muted">현황 데이터가 없습니다.</p>;
  const days = stats.days_live ?? stats.days_paper;
  const lifecycle = stats.live_started_at
    ? `실전 ${stats.days_live ?? 0}일`
    : stats.paper_started_at
      ? `모의 ${stats.days_paper ?? 0}일`
      : "—";

  return (
    <div className="strategy-detail-body">
      <div className="stats-grid">
        <StatBox label="적용 기간" value={lifecycle}
                 sub={days != null && days > 0
                   ? `시작일 ${dateOnly(stats.live_started_at ?? stats.paper_started_at)}`
                   : ""} />
        <StatBox label="누적 P&L" value={krw(stats.pnl_total)}
                 cls={(stats.pnl_total ?? 0) >= 0 ? "pos" : "neg"}
                 sub={stats.pnl_pct != null ? pct(stats.pnl_pct) : ""} />
        <StatBox label="거래된 금액"
                 value={stats.traded_amount != null ? krw(stats.traded_amount) : "—"}
                 sub="총 체결대금(누적)" />
        <StatBox label="승률"
                 value={stats.win_rate != null ? pct(stats.win_rate, false) : "—"}
                 sub={stats.n_trades ? `거래 ${stats.n_trades}건` : ""} />
        <StatBox label="현재 보유"
                 value={stats.n_positions > 0 ? `${stats.n_positions}종목` : "없음"} />
      </div>

      <section className="panel" style={{ marginTop: 16 }}>
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

      <p className="muted small" style={{ marginTop: 12 }}>
        ⓘ 종목별 매매 상세는 로컬앱 "주문 내역" 탭에서 확인하세요 (서버에는 요약만 보관).
      </p>
    </div>
  );
}

function StatBox({ label, value, sub, cls }: {
  label: string; value: string; sub?: string; cls?: string;
}) {
  return (
    <div className="stat-box">
      <div className="stat-label">{label}</div>
      <div className={"stat-value " + (cls ?? "")}>{value}</div>
      {sub && <div className="stat-sub muted small">{sub}</div>}
    </div>
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

// ── 공용 ──────────────────────────────────────────────────────────────────────

function Rule({ label, v }: { label: string; v: string }) {
  return (
    <div className="rule-row">
      <span className="rule-label">{label}</span>
      <span className="rule-val">{v}</span>
    </div>
  );
}


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

import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import type {
  BacktestRunSummary, StrategyRow, StrategyStats, StrategyVersionRow,
} from "../types";
import { EXECUTION_DEFAULTS, parseTradeSymbols } from "../types";

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

  async function remove() {
    if (!strategy) return;
    if (!confirm("이 전략을 삭제할까요? 모든 버전·백테스트도 함께 삭제됩니다.")) return;
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

      {tab === "config" && <ConfigTab strategy={strategy} onRemove={remove} />}
      {tab === "versions" && (
        <VersionsTab versions={versions} onRestore={restoreVersion} />
      )}
      {tab === "stats" && <StatsTab stats={stats} strategy={strategy} />}
      {tab === "backtests" && <BacktestsTab backtests={backtests} />}
    </div>
  );
}

// ── 탭 1: 설정값 (read-only 요약) ─────────────────────────────────────────────

function ConfigTab({ strategy, onRemove }: {
  strategy: StrategyRow;
  onRemove: () => void;
}) {
  const d = strategy.definition;
  const sr = d.sell_rules ?? {};
  const buyN = d.buy?.conditions?.length ?? 0;
  const sellExtraN = sr.conditions?.length ?? 0;
  const { mode, symbols } = parseTradeSymbols(d.trade_symbol);
  const e = d.execution ?? {};

  return (
    <div className="strategy-detail-body">
      <div className="config-section-head">
        <p className="muted small" style={{ margin: 0 }}>
          현재 정의 — 빌더 페이지에서 수정 가능 (다음 단계에서 인라인 수정 지원 예정).
        </p>
        <Link to="/backtest" className="cta sm">빌더에서 수정 →</Link>
      </div>

      <section className="panel">
        <h4>매수후보</h4>
        <div className="rule-row">
          <span className="rule-label">선정 방식</span>
          <span className="rule-val">
            {mode === "screener" ? "자동 선택"
              : symbols.length === 0 ? "(없음)"
              : `${symbols.length}종목 수동 선택`}
          </span>
        </div>
        {mode === "screener" && (
          <div className="rule-row">
            <span className="rule-label">세트</span>
            <span className="rule-val">
              <code>{d.trade_symbol.slice("screener:".length)}</code>
            </span>
          </div>
        )}
        {symbols.length > 0 && symbols.length <= 30 && (
          <div className="rule-row">
            <span className="rule-label">종목</span>
            <span className="rule-val small">
              {symbols.slice(0, 6).join(", ")}
              {symbols.length > 6 && ` 외 ${symbols.length - 6}종목`}
            </span>
          </div>
        )}
        <div className="rule-row">
          <span className="rule-label">상위 N개 보유</span>
          <span className="rule-val">{d.screener_limit ?? "—"}</span>
        </div>
      </section>

      <section className="panel">
        <h4>매수 조건 ({buyN}개)</h4>
        {buyN === 0 && <p className="muted small">조건 없음 — 매수 항상 가능.</p>}
        {buyN > 0 && (
          <p className="muted small">
            "{d.buy?.logic === "AND" ? "모두" : "하나라도"} 만족" — 조건 {buyN}개.
            상세는 빌더에서 확인.
          </p>
        )}
      </section>

      <section className="panel">
        <h4>매도 조건</h4>
        <Rule label="익절" v={sr.take_profit != null ? `+${sr.take_profit}%` : "—"} />
        <Rule label="손절" v={sr.stop_loss != null ? `${sr.stop_loss}%` : "—"} />
        <Rule label="보유기간" v={sr.hold_days != null ? `${sr.hold_days}일` : "—"} />
        <Rule label="트레일링 %" v={sr.trail_pct != null ? `-${sr.trail_pct}%` : "—"} />
        <Rule label="트레일링 ATR" v={sr.trail_atr_mult != null ? `×${sr.trail_atr_mult}` : "—"} />
        <Rule label="추가 조건" v={sellExtraN > 0 ? `${sellExtraN}개` : "—"} />
        <Rule label="매도 비율" v={`${sr.sell_amount_pct ?? 100}%`} />
      </section>

      <section className="panel">
        <h4>매수 가격</h4>
        <Rule label="방식" v={e.use_limit ? "지정가" : "시장가"} />
        {e.use_limit && (
          <Rule label="tolerance" v={`${e.buy_tolerance_pct ?? 0}%`} />
        )}
      </section>

      <section className="panel">
        <h4>매수 규모</h4>
        <Rule label="모드" v={
          e.sizing_mode === "atr_risk"
            ? `ATR 기반 (자본 ${e.atr_risk_pct ?? EXECUTION_DEFAULTS.atr_risk_pct}% 위험)`
            : e.sizing_mode === "fixed_amount"
              ? `정액 ${((e.amount_krw ?? 0) / 10000).toLocaleString()}만원`
              : e.sizing_mode === "equal_weight"
                ? `균등분배 (${d.screener_limit ?? 5}종목)`
                : `정률 (자본의 ${d.amount_pct}%)`
        } />
        <Rule label="단일 종목 상한" v={`${e.max_position_pct ?? EXECUTION_DEFAULTS.max_position_pct}%`} />
        <Rule label="일일 손실 한도" v={`${e.daily_loss_limit_pct ?? EXECUTION_DEFAULTS.daily_loss_limit_pct}%`} />
        <Rule label="누적 손실 한도" v={`${e.max_drawdown_pct ?? EXECUTION_DEFAULTS.max_drawdown_pct}%`} />
      </section>

      <section className="panel">
        <h4>위험 작업</h4>
        <button className="danger-btn" onClick={onRemove}>전략 삭제</button>
      </section>
    </div>
  );
}

// ── 탭 2: 버전 ────────────────────────────────────────────────────────────────

function VersionsTab({ versions, onRestore }: {
  versions: StrategyVersionRow[];
  onRestore: (versionNo: number) => void;
}) {
  if (versions.length === 0) {
    return <p className="muted">아직 저장된 버전이 없습니다.</p>;
  }
  return (
    <div className="strategy-detail-body">
      <p className="muted small">
        매 저장마다 자동 스냅샷. 최대 50건 또는 30일까지 보관 — 그 이전 버전은 자동 회전.
      </p>
      <div className="version-list">
        {versions.map((v) => (
          <div key={v.version_no} className="version-row">
            <div className="version-no">v{v.version_no}</div>
            <div className="version-meta">
              <div className="version-name">{v.name}</div>
              <div className="muted small">
                {dateOnly(v.created_at)} · {labelReason(v.created_reason)}
              </div>
            </div>
            <div className="version-actions">
              <button className="ghost sm" onClick={() => onRestore(v.version_no)}>
                이 버전으로 복원
              </button>
            </div>
          </div>
        ))}
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
        <StatBox label="승률"
                 value={stats.win_rate != null ? pct(stats.win_rate * 100, false) : "—"}
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
        <Link to="/backtest" className="cta sm">빌더에서 백테스트 실행 →</Link>
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


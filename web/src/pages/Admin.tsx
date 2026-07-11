import { useEffect, useState } from "react";
import {
  Bar, CartesianGrid, ComposedChart, Line, LineChart, ResponsiveContainer,
  Tooltip, XAxis, YAxis,
} from "recharts";
import { api } from "../api";
import { useAuth } from "../auth";
import type { AdminMetrics, AdminUserRow } from "../types";

// DESIGN.md §8 차트 규칙 — 네이비 막대 + 골드 선만(회색 등 금지).
const CHART_NAVY = "#264a85";
const CHART_GOLD = "#d4a738";

// recharts 툴팁을 테마(다크/라이트) 토큰에 맞춰 스타일.
const TOOLTIP_STYLE = {
  background: "var(--panel)",
  border: "1px solid var(--border)",
  borderRadius: 8,
  fontSize: 12,
  color: "var(--text)",
} as const;
const AXIS_TICK = { fontSize: 11, fill: "var(--muted)" } as const;

/** "YYYY-MM-DD" → "MM/DD" (축 라벨 간결화). */
const mmdd = (d: string) => d.slice(5).replace("-", "/");

/** ISO 시각 → "N분/시간/일 전" (없으면 "—"). */
function ago(iso: string | null): string {
  if (!iso) return "—";
  const sec = (Date.now() - new Date(iso).getTime()) / 1000;
  if (sec < 60) return "방금";
  if (sec < 3600) return `${Math.floor(sec / 60)}분 전`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}시간 전`;
  return `${Math.floor(sec / 86400)}일 전`;
}

const AUTH_LABEL: Record<string, string> = {
  google: "구글", naver: "네이버", password: "이메일",
};

function Stat({ label, value, sub }: { label: string; value: string; sub?: React.ReactNode }) {
  return (
    <div style={{ flex: "1 1 150px", minWidth: 140 }}>
      <div className="muted small" style={{ marginBottom: 4 }}>{label}</div>
      <div style={{ fontSize: 24, fontWeight: 700, lineHeight: 1.1 }}>{value}</div>
      {sub != null && <div className="muted small" style={{ marginTop: 4 }}>{sub}</div>}
    </div>
  );
}

function ChartPanel({ title, children }: { title: string; children: React.ReactElement }) {
  return (
    <div className="panel" style={{ flex: "1 1 300px", minWidth: 280 }}>
      <div className="sub-h" style={{ marginBottom: 8 }}>{title}</div>
      <ResponsiveContainer width="100%" height={200}>
        {children}
      </ResponsiveContainer>
    </div>
  );
}

export default function Admin() {
  const { isAdmin, ready } = useAuth();
  const [data, setData] = useState<AdminMetrics | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!isAdmin) return;
    let alive = true;
    api.adminMetrics()
      .then((m) => { if (alive) setData(m); })
      .catch((e) => { if (alive) setErr(e instanceof Error ? e.message : String(e)); });
    return () => { alive = false; };
  }, [isAdmin]);

  // 게이트 — 서버도 403으로 막지만, 비운영자에겐 UI를 아예 안 보여준다(방어적 이중 게이트).
  if (ready && !isAdmin) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>운영자 전용</h3>
        <p className="muted">이 페이지는 서비스 운영자만 볼 수 있습니다.</p>
      </div>
    );
  }
  if (err) {
    return (
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>운영 대시보드</h3>
        <p className="muted">지표를 불러오지 못했습니다 — {err}</p>
      </div>
    );
  }
  if (!data) {
    return <div className="panel"><p className="muted">불러오는 중…</p></div>;
  }

  const { totals, active_users, signups, auth_breakdown, daily, users } = data;

  return (
    <div>
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between",
                     gap: 12, flexWrap: "wrap", marginBottom: 4 }}>
        <h1>운영 대시보드</h1>
        <span className="muted small">
          최근 {data.window_days}일 · {new Date(data.generated_at).toLocaleString("ko-KR", { hour12: false })} 기준
        </span>
      </div>
      <p className="muted small" style={{ marginTop: 0, marginBottom: 14 }}>
        로그인 유저의 제품 사용·활성 지표입니다. 익명 방문 트래픽(유입경로·페이지뷰)은 Vercel Analytics에서 봅니다.
      </p>

      {/* ── KPI 카드 ── */}
      <div className="panel" style={{ display: "flex", flexWrap: "wrap", gap: 20, marginBottom: 14 }}>
        <Stat label="총 가입자" value={totals.users.toLocaleString()}
              sub={<>신규 24h <b>{signups.last_24h}</b> · 7일 <b>{signups.last_7d}</b></>} />
        <Stat label="활성 유저 (DAU)" value={active_users.dau.toLocaleString()}
              sub={<>WAU <b>{active_users.wau}</b> · MAU <b>{active_users.mau}</b></>} />
        <Stat label="자동매매 연동" value={totals.devices.toLocaleString()}
              sub={<>실전 전략 <b>{totals.live_strategies}</b> · 모의 <b>{totals.paper_strategies}</b></>} />
        <Stat label="누적 백테스트" value={totals.backtests.toLocaleString()}
              sub={<>챗봇 <b>{totals.chat_turns.toLocaleString()}</b>턴 · 전략 <b>{totals.strategies}</b></>} />
        <Stat label="가입 인증수단"
              value={`${auth_breakdown.google + auth_breakdown.naver}`}
              sub={<>구글 <b>{auth_breakdown.google}</b> · 네이버 <b>{auth_breakdown.naver}</b> · 이메일 <b>{auth_breakdown.password}</b></>} />
      </div>

      {/* ── 일별 추세 (KST) ── */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 14, marginBottom: 14 }}>
        <ChartPanel title="일별 신규 가입">
          <ComposedChart data={daily} margin={{ top: 6, right: 8, left: -18, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
            <XAxis dataKey="date" tickFormatter={mmdd} tick={AXIS_TICK} interval="preserveStartEnd" minTickGap={24} />
            <YAxis tick={AXIS_TICK} allowDecimals={false} width={30} />
            <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: "var(--muted)" }}
                     labelFormatter={(l) => `${l}`} formatter={(v) => [v, "가입"]} />
            <Bar dataKey="signups" name="가입" fill={CHART_NAVY} radius={[2, 2, 0, 0]} />
          </ComposedChart>
        </ChartPanel>

        <ChartPanel title="일별 활성 유저">
          <LineChart data={daily} margin={{ top: 6, right: 8, left: -18, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
            <XAxis dataKey="date" tickFormatter={mmdd} tick={AXIS_TICK} interval="preserveStartEnd" minTickGap={24} />
            <YAxis tick={AXIS_TICK} allowDecimals={false} width={30} />
            <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: "var(--muted)" }}
                     formatter={(v) => [v, "활성 유저"]} />
            <Line type="monotone" dataKey="active_users" name="활성 유저"
                  stroke={CHART_GOLD} strokeWidth={2} dot={false} />
          </LineChart>
        </ChartPanel>

        <ChartPanel title="일별 제품 사용 (백테스트·챗봇)">
          <ComposedChart data={daily} margin={{ top: 6, right: 8, left: -18, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
            <XAxis dataKey="date" tickFormatter={mmdd} tick={AXIS_TICK} interval="preserveStartEnd" minTickGap={24} />
            <YAxis tick={AXIS_TICK} allowDecimals={false} width={30} />
            <Tooltip contentStyle={TOOLTIP_STYLE} labelStyle={{ color: "var(--muted)" }} />
            <Bar dataKey="backtests" name="백테스트" fill={CHART_NAVY} radius={[2, 2, 0, 0]} />
            <Line type="monotone" dataKey="chat_turns" name="챗봇 턴"
                  stroke={CHART_GOLD} strokeWidth={2} dot={false} />
          </ComposedChart>
        </ChartPanel>
      </div>

      {/* ── 유저별 활동 ── */}
      <div className="panel">
        <div className="sub-h" style={{ marginBottom: 10 }}>유저별 활동 ({users.length})</div>
        <div style={{ overflowX: "auto" }}>
          <UserTable users={users} />
        </div>
      </div>
    </div>
  );
}

function UserTable({ users }: { users: AdminUserRow[] }) {
  const th: React.CSSProperties = { textAlign: "right", padding: "6px 10px", whiteSpace: "nowrap" };
  const thL: React.CSSProperties = { textAlign: "left", padding: "6px 10px", whiteSpace: "nowrap" };
  const td: React.CSSProperties = { textAlign: "right", padding: "6px 10px", whiteSpace: "nowrap" };
  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
      <thead>
        <tr style={{ borderBottom: "1px solid var(--border)", color: "var(--muted)" }}>
          <th style={thL}>이메일</th>
          <th style={thL}>가입</th>
          <th style={th}>최근 활동</th>
          <th style={th}>백테스트</th>
          <th style={th}>챗봇</th>
          <th style={th}>전략</th>
          <th style={th}>연동</th>
          <th style={thL}>인증</th>
        </tr>
      </thead>
      <tbody>
        {users.map((u) => (
          <tr key={u.id} style={{ borderBottom: "1px solid var(--border)" }}>
            <td style={{ ...thL, fontWeight: 600 }}>{u.email}</td>
            <td style={thL} className="muted">{u.created_at.slice(0, 10)}</td>
            <td style={td}>{ago(u.last_active_at)}</td>
            <td style={td}>{u.backtests.toLocaleString()}</td>
            <td style={td}>{u.chat_turns.toLocaleString()}</td>
            <td style={td}>
              {u.strategies}
              {u.live_strategies > 0 && (
                <span className="badge" style={{ marginLeft: 6, background: "var(--up-soft)", color: "var(--up)" }}>
                  실전 {u.live_strategies}
                </span>
              )}
            </td>
            <td style={td}>{u.devices > 0 ? u.devices : <span className="muted">—</span>}</td>
            <td style={thL}>
              <span className="badge gray">{AUTH_LABEL[u.auth] ?? u.auth}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

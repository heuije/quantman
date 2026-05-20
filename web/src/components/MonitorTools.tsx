/* Phase 13.4/13.9 — 백테스트-라이브 overlay + 알림 설정 + CSV export */

import { useEffect, useState } from "react";
import { api } from "../api";
import EquityChart from "./EquityChart";
import type {
  BacktestRunSummary, OrderEvent, UserSettingsIO,
} from "../types";

// ── 백테스트 vs 라이브 overlay ────────────────────────────────────────────────

export function BacktestLiveOverlay({ liveEquity }: {
  liveEquity?: { date: string; value: number }[];
}) {
  const [runs, setRuns] = useState<BacktestRunSummary[]>([]);
  const [pickedId, setPickedId] = useState<number | null>(null);
  const [overlay, setOverlay] = useState<{ date: string; value: number | null }[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api.listBacktestRuns()
      .then((rs) => { setRuns(rs); setLoaded(true); })
      .catch(() => setLoaded(true));
  }, []);

  async function loadRun(id: number) {
    setPickedId(id);
    try {
      const r = await api.getBacktestRun(id);
      // BacktestResult.equity는 백테스트 자산곡선 (보통 기간 동안의 일별)
      const eq = r.result.equity ?? [];
      // 정규화: 시작 100 기준
      const base = eq.length ? Number(eq[0].value) || 100 : 100;
      const norm = eq.map((p) => ({
        date: p.date,
        value: p.value != null ? (Number(p.value) / base) * 100 : null,
      }));
      setOverlay(norm);
    } catch {
      setOverlay([]);
    }
  }

  // 라이브도 정규화 (시작값 = 100)
  const liveBase = liveEquity?.[0]?.value;
  const liveNorm = liveBase && liveBase > 0
    ? (liveEquity ?? []).map((p) => ({
        date: p.date, value: (p.value / liveBase) * 100,
      })) : [];

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>백테스트 ↔ 라이브 비교</h3>
      {!loaded ? <p className="muted">불러오는 중…</p> : runs.length === 0 ? (
        <p className="muted">저장된 백테스트가 없습니다. [백테스트 → 전략 구성]에서 실행하세요.</p>
      ) : (
        <>
          <div className="row" style={{ marginBottom: 12 }}>
            <label>참조할 백테스트</label>
            <select value={pickedId ?? ""}
                    onChange={(e) => loadRun(Number(e.target.value))}>
              <option value="">선택…</option>
              {runs.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name} · {new Date(r.created_at).toLocaleDateString()}
                </option>
              ))}
            </select>
            <span className="muted" style={{ fontSize: 12 }}>
              시작값을 100으로 정규화해 표시합니다.
            </span>
          </div>
          <EquityChart
            equity={liveNorm.length ? liveNorm : overlay}
            benchmark={liveNorm.length ? overlay : undefined}
          />
          <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
            진한 선 = 라이브, 옅은 선 = 백테스트. 두 곡선의 괴리가 슬리피지·갭 영향입니다.
          </p>
        </>
      )}
    </div>
  );
}

// ── 알림 설정 ─────────────────────────────────────────────────────────────────

export function AlertSettings() {
  const [s, setS] = useState<UserSettingsIO | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => { api.getSettings().then(setS).catch(() => {}); }, []);

  function update<K extends keyof UserSettingsIO>(k: K, v: UserSettingsIO[K]) {
    if (s) setS({ ...s, [k]: v });
  }

  async function save() {
    if (!s) return;
    setBusy(true); setMsg("");
    try {
      await api.putSettings(s);
      setMsg("저장됐습니다.");
    } catch (e) {
      setMsg("저장 실패: " + (e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  if (!s) return null;

  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>알림 (Discord / Slack webhook)</h3>
      <div className="alert-form">
        <div>
          <label>Webhook URL</label>
          <input
            type="url"
            placeholder="https://discord.com/api/webhooks/... 또는 https://hooks.slack.com/..."
            value={s.alert_webhook_url}
            onChange={(e) => update("alert_webhook_url", e.target.value)}
            style={{ width: "100%" }}
          />
        </div>
        <label className="alert-toggle">
          <input type="checkbox" checked={s.alert_on_killswitch}
                 onChange={(e) => update("alert_on_killswitch", e.target.checked)} />
          Kill Switch 활성 시 알림
        </label>
        <div>
          <label>일일 손실 알림 임계 (%)</label>
          <input type="number" step="0.5" min={0.5} max={10}
                 value={s.alert_on_daily_loss_pct}
                 onChange={(e) => update("alert_on_daily_loss_pct",
                                          Number(e.target.value))} />
        </div>
        <div>
          <label>미체결 누적 알림 (건)</label>
          <input type="number" min={1} value={s.alert_on_unfilled_count}
                 onChange={(e) => update("alert_on_unfilled_count",
                                          Number(e.target.value))} />
        </div>
        <button disabled={busy} onClick={save}>
          {busy ? "저장 중…" : "알림 설정 저장"}
        </button>
        {msg && <span className="muted">{msg}</span>}
      </div>
    </div>
  );
}

// ── CSV export ─────────────────────────────────────────────────────────────────

export function CsvExportBar({ orders }: { orders: OrderEvent[] }) {
  function exportOrders() {
    if (!orders || orders.length === 0) return;
    const headers = ["ts", "event", "side", "symbol", "qty",
                      "intended_price", "limit_price", "fill_price",
                      "strategy", "reason", "order_no"];
    const rows = orders.map((o) =>
      headers.map((h) => {
        const v = (o as unknown as Record<string, unknown>)[h];
        if (v == null) return "";
        const s = String(v);
        return s.includes(",") || s.includes("\"")
          ? `"${s.replace(/"/g, '""')}"` : s;
      }).join(","));
    const csv = [headers.join(","), ...rows].join("\n");
    const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `orders_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <button className="ghost sm" onClick={exportOrders}
            disabled={!orders || orders.length === 0}>
      주문 내역 CSV 내보내기
    </button>
  );
}

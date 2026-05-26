/**
 * 자동매매 타임라인 — 사용자에게 "다음에 무슨 일이 언제 일어나는지" 한눈에.
 *
 * 데이터: GET /trading/timeline. 60초마다 자동 새로고침 (cycle 자체가 5분 이상
 * 간격이라 그보다 더 자주 폴링할 필요 없음).
 *
 * 표시 원칙: 가독성 우선. 핵심만(시각·이름·상태) 보이고 detail은 hover로.
 */

import { useEffect, useState } from "react";
import { api } from "../api";
import type { TimelineEvent, TradingTimeline } from "../types";

const KIND_LABEL: Record<TimelineEvent["kind"], string> = {
  krx_preview:    "국장 매매 후보 결정",
  krx_cycle:      "국장 자동매매 시작",
  krx_settlement: "국장 자동매매 종료",
  us_preview:     "미장 매매 후보 결정",
  us_cycle:       "미장 자동매매 시작",
  us_settlement:  "미장 자동매매 종료",
};

const STATUS_BADGE: Record<TimelineEvent["status"], { icon: string; cls: string }> = {
  done:       { icon: "✓", cls: "tl-done"      },
  scheduled:  { icon: "⏳", cls: "tl-scheduled" },
  missed:     { icon: "✗", cls: "tl-missed"    },
  holiday:    { icon: "—", cls: "tl-holiday"   },
};

const HEARTBEAT_LABEL: Record<TradingTimeline["heartbeat_status"], string> = {
  normal: "정상",
  warning: "응답 느림",
  error: "연결 끊김",
};

/** "오늘"·"내일" 같은 상대 날짜 라벨. KST 기준. */
function dayLabel(iso: string, nowIso: string): string {
  const d = new Date(iso);
  const now = new Date(nowIso);
  const ymd = (dt: Date) =>
    dt.toLocaleDateString("ko-KR", { timeZone: "Asia/Seoul",
      year: "numeric", month: "2-digit", day: "2-digit", weekday: "short" });
  if (ymd(d) === ymd(now)) return `오늘  ·  ${ymd(d)}`;
  const tomorrow = new Date(now);
  tomorrow.setDate(tomorrow.getDate() + 1);
  if (ymd(d) === ymd(tomorrow)) return `내일  ·  ${ymd(d)}`;
  const yesterday = new Date(now);
  yesterday.setDate(yesterday.getDate() - 1);
  if (ymd(d) === ymd(yesterday)) return `어제  ·  ${ymd(d)}`;
  return ymd(d);
}

function hhmm(iso: string): string {
  return new Date(iso).toLocaleTimeString("ko-KR",
    { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", hour12: false });
}

/** "21h 7m 후" / "3분 전" 같은 상대 시각 — scheduled·missed 동시 사용. */
function relativeTime(iso: string, nowIso: string): string {
  const ms = new Date(iso).getTime() - new Date(nowIso).getTime();
  const abs = Math.abs(ms);
  const future = ms > 0;
  const min = Math.floor(abs / 60000);
  if (min < 1) return future ? "곧" : "방금";
  if (min < 60) return future ? `${min}분 후` : `${min}분 전`;
  const h = Math.floor(min / 60);
  const m = min % 60;
  const ms_str = m ? `${h}h ${m}m` : `${h}h`;
  return future ? `${ms_str} 후` : `${ms_str} 전`;
}

/** YYYY-MM-DD (KST) 묶기 키. */
function groupKey(iso: string): string {
  return new Date(iso).toLocaleDateString("en-CA",     // ISO format
    { timeZone: "Asia/Seoul" });
}

export default function TradingTimeline() {
  const [data, setData] = useState<TradingTimeline | null>(null);
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const r = await api.getTradingTimeline();
        if (!cancelled) {
          setData(r);
          setErr("");
        }
      } catch (e) {
        if (!cancelled) setErr(String(e instanceof Error ? e.message : e));
      }
    }
    load();
    const t = setInterval(load, 60_000);     // 60s polling
    return () => { cancelled = true; clearInterval(t); };
  }, []);

  if (err) {
    return (
      <div className="trading-timeline">
        <div className="tl-error">자동매매 상태 조회 실패 — {err}</div>
      </div>
    );
  }
  if (!data) {
    return (
      <div className="trading-timeline">
        <div className="tl-loading">자동매매 상태 불러오는 중…</div>
      </div>
    );
  }

  // 날짜별로 묶음
  const groups: { key: string; events: TimelineEvent[] }[] = [];
  for (const ev of data.events) {
    const key = groupKey(ev.at);
    let g = groups.find(x => x.key === key);
    if (!g) {
      g = { key, events: [] };
      groups.push(g);
    }
    g.events.push(ev);
  }

  const heartbeatRel = data.heartbeat_at
    ? relativeTime(data.heartbeat_at, data.now)
    : "한 번도 응답 없음";

  return (
    <div className="trading-timeline">
      <div className="tl-header">
        <h3>자동매매 상태</h3>
        <span className={`tl-status tl-status-${data.heartbeat_status}`}
              title={`로컬앱 alive: ${heartbeatRel}`}>
          ● {HEARTBEAT_LABEL[data.heartbeat_status]}
          <span className="tl-status-sub">  ·  로컬앱 {heartbeatRel}</span>
        </span>
      </div>

      {groups.length === 0 ? (
        <div className="tl-empty">예정된 이벤트가 없습니다.</div>
      ) : (
        groups.map(g => (
          <div key={g.key} className="tl-group">
            <div className="tl-group-label">{dayLabel(g.events[0].at, data.now)}</div>
            <ul className="tl-list">
              {g.events.map((ev, i) => {
                const badge = STATUS_BADGE[ev.status];
                const isFuture = ev.status === "scheduled";
                const tooltip = ev.detail || ev.summary || "";
                return (
                  <li key={i} className={`tl-item ${badge.cls}`} title={tooltip}>
                    <span className="tl-time">{hhmm(ev.at)}</span>
                    <span className="tl-kind">{KIND_LABEL[ev.kind]}</span>
                    <span className="tl-badge">
                      <span className="tl-icon">{badge.icon}</span>
                      <span className="tl-summary">
                        {isFuture
                          ? relativeTime(ev.at, data.now)
                          : (ev.summary || (ev.status === "missed" ? "누락" : ""))}
                      </span>
                    </span>
                  </li>
                );
              })}
            </ul>
          </div>
        ))
      )}
    </div>
  );
}

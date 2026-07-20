import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { CompileQuota } from "../api";
import type { ChatMessage, ChatPart } from "../types";
import ChatReport from "../components/ChatReport";

// 라인 아이콘(상단 네비와 동일 스타일 — currentColor stroke)
const SIc = ({ d }: { d: string }) => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={d} /></svg>
);

// 빈 화면 스타터 — 어시스턴트의 실제 역량 3갈래(분석·스크리닝·백테스트)를 구체 예시 프롬프트로.
// 클릭하면 그대로 전송된다(무엇을 물어야 할지 모르는 첫 사용자의 진입 장벽 제거).
const STARTERS: { label: string; icon: string; prompts: string[] }[] = [
  { label: "종목 분석", icon: "M3 3v18h18 M7 14l3-4 4 3 5-7",
    prompts: ["삼성전자 실적과 밸류에이션을 요약해줘", "최근 외국인이 많이 사는 종목 알려줘"] },
  { label: "조건 스크리닝", icon: "M3 4h18 M6 9h12 M9 14h6 M11 19h2",
    prompts: ["PER 10배 이하·ROE 15% 이상 저평가주 골라줘", "52주 신고가를 돌파한 코스닥 종목"] },
  { label: "전략 백테스트", icon: "M4 19V5 M4 19h16 M8 16l3-5 3 2 4-7",
    prompts: ["20일선 돌파 매수·5% 익절 전략을 백테스트해줘", "RSI 30 이하 매수 전략의 최근 3년 성과"] },
];

export default function ChatLab() {
  const [convId, setConvId] = useState<number | null>(null);
  const [convs, setConvs] = useState<{ id: number; title: string }[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  // 재개(resume) — 새로고침·탭복귀 시 서버에서 계속 도는 턴의 완성 답변을 폴링으로 이어받는 상태.
  // 이 탭이 스트리밍을 직접 돌리는 busy와 구분(busy=이 탭 전송, resuming=끊긴 턴 재접속).
  const [resuming, setResuming] = useState(false);
  const pollRef = useRef<{ cancel: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  // 사용량 카운터 + 운영진 언락(서버 강제 일일 한도). 비번은 sessionStorage에만 보관하고 매 요청에 동봉.
  const [adminPw, setAdminPw] = useState<string>(() => sessionStorage.getItem("chat_admin_pw") || "");
  const [quota, setQuota] = useState<CompileQuota | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);

  // 새 메시지·스트리밍 갱신마다 맨 아래로 스크롤
  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages, busy]);

  // 사용량 카운터 — 마운트 + 언락 상태 변경 + 매 턴 후 갱신(읽기 전용, 메시지 소모 없음).
  const refreshQuota = useCallback(() => {
    api.chatQuota(adminPw || undefined).then(setQuota).catch(() => {/* 카운터 실패는 대화에 무관 */});
  }, [adminPw]);
  useEffect(() => { refreshQuota(); }, [refreshQuota]);

  const refreshConvs = useCallback(() => {
    api.listConversations().then(setConvs).catch(() => {/* 목록 갱신 실패는 무해 */});
  }, []);

  // 진행 중 폴링 중단(대화 전환·새 대화·언마운트 시).
  const stopPoll = useCallback(() => {
    if (pollRef.current) pollRef.current.cancel = true;
    pollRef.current = null;
    setResuming(false);
  }, []);

  // 끊긴 턴 재접속 — 서버는 클라 끊김과 무관하게 턴을 끝까지 실행·영속하므로(routers/chat.py
  // producer 스레드), 대화 히스토리를 폴링해 assistant 답변이 영속되면 이어받는다. 새로고침·탭복귀·
  // 전송 중 연결단절 모두 이 경로로 완성 답변을 되찾는다(수동 새로고침 없이).
  const resumePendingTurn = useCallback((cid: number) => {
    if (pollRef.current) pollRef.current.cancel = true;
    const tok = { cancel: false };
    pollRef.current = tok;
    setResuming(true);
    const started = Date.now();
    const MAX_MS = 8 * 60 * 1000;       // 콜드 로드+다지표 이벤트 스터디 여유(그 뒤 안내)
    (async () => {
      while (!tok.cancel) {
        await new Promise((r) => setTimeout(r, 2500));
        if (tok.cancel) return;
        let full;
        try { full = await api.getConversation(cid); }
        catch { continue; }             // 일시 네트워크 오류는 계속 폴링(턴은 서버에서 진행 중)
        if (tok.cancel) return;
        const last = full.messages[full.messages.length - 1];
        if (last && last.role === "assistant" && last.parts.length > 0) {
          setMessages(full.messages);   // 완성 답변 영속됨 — 이어받아 렌더
          pollRef.current = null; setResuming(false);
          refreshConvs();
          return;
        }
        if (Date.now() - started > MAX_MS) {
          pollRef.current = null; setResuming(false);
          setError("분석이 예상보다 오래 걸리거나 중단됐을 수 있어요. 잠시 후 이 대화를 다시 열거나 다시 시도해 주세요.");
          return;
        }
      }
    })();
  }, [refreshConvs]);

  // 마운트 시 최근 대화 복원 — 다른 탭 갔다 와도 직전 대화가 보이도록(④ 영속; 서버에 이미 보관).
  // 마지막 메시지가 미답변 user면 = 진행 중이던 턴 → 재접속 폴링으로 완성 답변을 이어받는다.
  useEffect(() => {
    let alive = true;
    api.listConversations()
      .then((list) => {
        if (!alive) return undefined;
        setConvs(list);
        if (list.length === 0) return undefined;
        const recent = list.reduce((a, b) => (b.id > a.id ? b : a));
        return api.getConversation(recent.id).then((full) => {
          if (!alive) return;
          setConvId(full.id); setMessages(full.messages);
          const last = full.messages[full.messages.length - 1];
          if (last && last.role === "user") resumePendingTurn(full.id);   // 진행 중 턴 이어받기
        });
      })
      .catch(() => {/* 복원 실패는 빈(새) 대화로 시작 — 무해 */});
    return () => { alive = false; stopPoll(); };
  }, [resumePendingTurn, stopPoll]);

  async function unlockLimit() {
    const pw = window.prompt("운영진 비밀번호를 입력하면 일일 사용 한도가 상향됩니다.");
    if (pw == null || !pw.trim()) return;
    try {
      const q = await api.chatQuota(pw.trim());
      if (q.admin_unlocked) {
        sessionStorage.setItem("chat_admin_pw", pw.trim());
        setAdminPw(pw.trim());
        setQuota(q);
      } else {
        window.alert("비밀번호가 올바르지 않습니다.");
      }
    } catch {
      window.alert("확인에 실패했습니다. 잠시 후 다시 시도하세요.");
    }
  }

  function lockLimit() {
    sessionStorage.removeItem("chat_admin_pw");
    setAdminPw("");
  }

  function newChat() {        // 복원된/현재 대화를 두고 새 대화 시작(복원이 덫이 되지 않도록)
    stopPoll();
    setConvId(null);
    setMessages([]);
    setError(null);
  }

  async function selectConv(id: number) {
    if (id === convId || busy) return;
    stopPoll();
    try {
      const full = await api.getConversation(id);
      setConvId(full.id); setMessages(full.messages); setError(null);
      const last = full.messages[full.messages.length - 1];
      if (last && last.role === "user") resumePendingTurn(full.id);   // 진행 중 턴 이어받기
    } catch { setError("대화를 불러오지 못했습니다."); }
  }

  async function renameConv(id: number, current: string) {
    const title = window.prompt("대화 제목", current);
    if (title == null || !title.trim()) return;
    try {
      await api.updateConversation(id, title.trim());
      setConvs((cs) => cs.map((c) => (c.id === id ? { ...c, title: title.trim() } : c)));
    } catch { setError("이름 변경에 실패했습니다."); }
  }

  async function deleteConv(id: number) {
    if (!window.confirm("이 대화를 삭제할까요? 되돌릴 수 없습니다.")) return;
    try {
      await api.deleteConversation(id);
      setConvs((cs) => cs.filter((c) => c.id !== id));
      if (id === convId) { setConvId(null); setMessages([]); }
    } catch { setError("삭제에 실패했습니다."); }
  }

  async function send(preset?: string) {
    const text = (preset ?? input).trim();
    if (!text || busy || resuming) return;
    setBusy(true); setError(null);
    setInput("");
    // 사용자 메시지 + 스트리밍으로 채워나갈 빈 assistant 메시지를 함께 추가
    setMessages((m) => [...m,
      { role: "user", parts: [{ type: "text", text }] },
      { role: "assistant", parts: [] }]);

    // 마지막(assistant) 메시지의 parts만 갱신
    const patch = (fn: (parts: ChatPart[]) => ChatPart[]) =>
      setMessages((m) => {
        const copy = m.slice();
        const last = copy[copy.length - 1];
        copy[copy.length - 1] = { ...last, parts: fn(last.parts) };
        return copy;
      });

    let cid = convId;
    try {
      if (cid == null) {
        const conv = await api.createConversation();
        cid = conv.id; setConvId(cid);
      }
      await api.streamChatMessage(cid, text, {
        // 파트는 계속 누적하되(완료 후 ChatReport가 최종 산출만 렌더·서버 영속과 일치) 스트리밍
        // 중엔 화면에 진행 서술을 표시하지 않는다. 델타는 마지막 텍스트 파트에 이어붙인다.
        onDelta: (t) => patch((parts) => {
          const last = parts[parts.length - 1];
          if (last && last.type === "text")
            return [...parts.slice(0, -1), { type: "text", text: last.text + t }];
          return [...parts, { type: "text", text: t }];
        }),
        onToolUse: (p) => patch((parts) => [...parts, { type: "tool_use", ...p }]),
        onToolResult: (p) => patch((parts) => [...parts, { type: "tool_result", ...p }]),
      }, adminPw || undefined);
    } catch (e) {
      const raw = e instanceof Error ? e.message : "";
      // 전송층 단절(TypeError: Failed to fetch 등)은 서버가 턴을 계속 완주·영속하므로(routers/chat.py
      // producer 스레드) 오류가 아니라 '재접속'이다 — 낙관적 빈 assistant 버블을 걷고 폴링으로 완성
      // 답변을 자동으로 이어받는다(수동 새로고침 불필요). 그 외(429 한도 등)만 에러 문구로 표면화.
      const disconnected = e instanceof TypeError || /fetch|network/i.test(raw);
      if (disconnected && cid != null) {
        setMessages((m) => (m.length && m[m.length - 1].role === "assistant"
          && m[m.length - 1].parts.length === 0 ? m.slice(0, -1) : m));
        resumePendingTurn(cid);
      } else {
        const msg = raw || "오류가 발생했습니다.";
        setError(msg);
        // 프론트 단계 오류(429 한도 초과 안내 포함)로 빈 assistant 버블이 남으면 그 문구로 채운다.
        patch((parts) => (parts.length ? parts : [{ type: "text", text: msg }]));
      }
    } finally {
      setBusy(false);
      refreshQuota();              // 턴 후 카운터 갱신(차단 429였어도 used 반영)
      refreshConvs();              // 새 대화 등장 + 첫 메시지 자동제목을 사이드바에 반영
    }
  }

  return (
    <div className="chat-layout">
      <aside className="chat-sessions">
        <button type="button" className="chat-sessions-new" onClick={newChat}>+ 새 대화</button>
        {convs.map((c) => (
          <div key={c.id}
               className={"chat-session" + (c.id === convId ? " active" : "")}
               onClick={() => void selectConv(c.id)}>
            <span className="chat-session-title">{c.title}</span>
            <button type="button" className="chat-session-act" title="이름 변경"
                    onClick={(e) => { e.stopPropagation(); void renameConv(c.id, c.title); }}>✎</button>
            <button type="button" className="chat-session-act" title="삭제"
                    onClick={(e) => { e.stopPropagation(); void deleteConv(c.id); }}>🗑</button>
          </div>
        ))}
      </aside>
      <div className="chat-lab">
      <div className="chat-thread" ref={threadRef}>
        {messages.length === 0 && (
          <div className="chat-welcome">
            <div className="chat-welcome-eyebrow">AI 리서치 어시스턴트</div>
            <h2 className="chat-welcome-title">무엇을 리서치할까요?</h2>
            <p className="chat-welcome-lead">아래 예시로 시작하거나, 원하는 종목·조건·전략을 직접 입력하세요.</p>
            <div className="chat-starters">
              {STARTERS.map((s) => (
                <div key={s.label} className="chat-starter">
                  <div className="chat-starter-head"><span className="chat-starter-ic"><SIc d={s.icon} /></span>{s.label}</div>
                  {s.prompts.map((p) => (
                    <button key={p} type="button" className="chat-starter-chip"
                      onClick={() => void send(p)} disabled={busy}>{p}</button>
                  ))}
                </div>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => {
          // 사용자에게 필요 없는 진행 서술(도구 실행·중간 결과·"~하겠습니다" 델타)은 화면에서 제거.
          // · 진행 중(스트리밍) 어시스턴트 턴: "분석 중…" 한 줄만.
          // · 완료된 어시스턴트 턴: 최종 산출만 4섹션 보고서(ChatReport)로.
          const streaming = m.role === "assistant" && busy && i === messages.length - 1;
          return (
            <div key={i} className={"chat-msg " + m.role}>
              {m.role === "user"
                ? m.parts.map((p, j) => (p.type === "text"
                    ? <p key={j} className="chat-text">{p.text}</p> : null))
                : streaming
                  ? <p className="chat-text muted">분석 중…</p>
                  : <ChatReport parts={m.parts} />}
            </div>
          );
        })}
        {resuming && (
          <div className="chat-msg assistant">
            <p className="chat-text muted">
              🔄 이전 분석이 서버에서 계속 진행 중입니다. 탭을 떠나거나 새로고침해도 유지되며, 완료되면 자동으로 표시됩니다…
            </p>
          </div>
        )}
      </div>
      {error && <div className="chat-error">{error}</div>}
      {quota && (
        <div style={{ display: "flex", alignItems: "center", gap: 8,
                      justifyContent: "flex-end", margin: "2px 2px 6px" }}>
          <span style={{ fontSize: 12,
                         color: quota.remaining <= 0 ? "var(--red)" : "var(--muted)" }}>
            오늘 {quota.used}/{quota.limit}회
            {quota.admin_unlocked && (
              <span style={{ color: "var(--green)", marginLeft: 4 }}>· 운영진</span>
            )}
          </span>
          {quota.admin_unlocked
            ? <button type="button" className="ghost" style={{ fontSize: 12 }}
                      onClick={lockLimit}>제한 잠금</button>
            : <button type="button" className="ghost" style={{ fontSize: 12 }}
                      onClick={unlockLimit}>🔓 제한 해제</button>}
        </div>
      )}
      <div className="chat-input-bar">
        <textarea
          className="chat-input" rows={2} value={input}
          placeholder="메시지를 입력하세요… (Enter 전송, Shift+Enter 줄바꿈)"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void send(); } }}
          disabled={busy || resuming}
        />
        <button type="button" className="chat-send" onClick={() => void send()}
          disabled={busy || resuming || !input.trim()}>전송</button>
      </div>
      </div>
    </div>
  );
}

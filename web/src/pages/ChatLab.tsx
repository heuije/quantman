import { memo, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { CompileQuota } from "../api";
import type { ChatMessage, ChatPart } from "../types";
import ChatResultView from "../components/ChatResultView";

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

// memo — 스트리밍 중 텍스트 델타로 메시지가 갱신돼도 참조가 안 바뀐 tool_result(차트) 파트는
// 재렌더하지 않는다(델타마다 차트 재렌더되는 렉 회피).
const PartView = memo(function PartView({ part }: { part: ChatPart }) {
  if (part.type === "text") return <p className="chat-text">{part.text}</p>;
  if (part.type === "tool_use")
    return <div className="chat-tool">{part.progress ? `${part.progress}…` : `🔧 ${part.name} 실행 중…`}</div>;
  if (part.type === "tool_result") return <ChatResultView result={part.result} />;
  return null;
});

export default function ChatLab() {
  const [convId, setConvId] = useState<number | null>(null);
  const [convs, setConvs] = useState<{ id: number; title: string }[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 진행 단계 라벨(SSE progress·표시 전용) — 델타/도구 이벤트가 오면 그쪽이 진행 표시를
  // 대신하므로 비우고, 다음 progress가 오면 다시 채운다.
  const [progress, setProgress] = useState<string | null>(null);
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

  // 마운트 시 최근 대화 복원 — 다른 탭 갔다 와도 직전 대화가 보이도록(④ 영속; 서버에 이미 보관).
  useEffect(() => {
    let alive = true;
    api.listConversations()
      .then((list) => {
        if (!alive) return undefined;
        setConvs(list);
        if (list.length === 0) return undefined;
        const recent = list.reduce((a, b) => (b.id > a.id ? b : a));
        return api.getConversation(recent.id).then((full) => {
          if (alive) { setConvId(full.id); setMessages(full.messages); }
        });
      })
      .catch(() => {/* 복원 실패는 빈(새) 대화로 시작 — 무해 */});
    return () => { alive = false; };
  }, []);

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
    setConvId(null);
    setMessages([]);
    setError(null);
  }

  const refreshConvs = useCallback(() => {
    api.listConversations().then(setConvs).catch(() => {/* 목록 갱신 실패는 무해 */});
  }, []);

  async function selectConv(id: number) {
    if (id === convId || busy) return;
    try {
      const full = await api.getConversation(id);
      setConvId(full.id); setMessages(full.messages); setError(null);
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
    if (!text || busy) return;
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

    try {
      let cid = convId;
      if (cid == null) {
        const conv = await api.createConversation();
        cid = conv.id; setConvId(cid);
      }
      await api.streamChatMessage(cid, text, {
        // 델타는 마지막 파트가 텍스트면 이어붙이고, 아니면(도구 결과 뒤) 새 텍스트 파트를 연다.
        onDelta: (t) => { setProgress(null); patch((parts) => {
          const last = parts[parts.length - 1];
          if (last && last.type === "text")
            return [...parts.slice(0, -1), { type: "text", text: last.text + t }];
          return [...parts, { type: "text", text: t }];
        }); },
        onToolUse: (p) => { setProgress(null); patch((parts) => [...parts, { type: "tool_use", ...p }]); },
        onToolResult: (p) => { setProgress(null); patch((parts) => [...parts, { type: "tool_result", ...p }]); },
        onProgress: (label) => setProgress(label),
      }, adminPw || undefined);
    } catch (e) {
      const raw = e instanceof Error ? e.message : "";
      // 전송층 단절(TypeError: Failed to fetch 등)은 원문이 사용자에게 무의미하고, 서버는
      // 턴을 계속 완주·영속하므로(keepalive 래퍼) 재열람 안내가 정직한 표면이다.
      const msg = (e instanceof TypeError || /fetch|network/i.test(raw))
        ? "연결이 끊겼습니다. 분석은 서버에서 계속 진행됩니다 — 잠시 후 이 대화를 다시 열면 완성된 답변이 표시됩니다."
        : (raw || "오류가 발생했습니다.");
      setError(msg);
      // 프론트 단계 오류(429 한도 초과 안내 포함)로 빈 assistant 버블이 남으면 그 문구로 채운다.
      patch((parts) => (parts.length ? parts : [{ type: "text", text: msg }]));
    } finally {
      setBusy(false);
      setProgress(null);
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
          // 한 메시지에 도구결과가 여럿이면(에이전트 다중 호출) 마지막만 펼치고 이전은 접는다 —
          // 거의 같은 차트가 난립하던 ④ 표현 결함 차단(③로 재실행이 줄어도 남는 다중호출 대비).
          let lastTr = -1;
          m.parts.forEach((p, j) => { if (p.type === "tool_result") lastTr = j; });
          return (
            <div key={i} className={"chat-msg " + m.role}>
              {m.parts.length === 0
                ? (m.role === "assistant" && busy
                    ? <p className="chat-text muted">{progress ?? "분석 중"}…</p>
                    : null)
                : m.parts.map((p, j) =>
                    p.type === "tool_result" && j !== lastTr ? (
                      <details key={j} style={{ margin: "4px 0" }}>
                        <summary style={{ cursor: "pointer", fontSize: "0.8em", color: "var(--muted)" }}>
                          📊 중간 분석 결과 (펼치기)
                        </summary>
                        <PartView part={p} />
                      </details>
                    ) : (
                      <PartView key={j} part={p} />
                    ))}
              {/* 도구 결과 뒤 다음 LLM 라운드(델타 없는 구간)의 진행 표시 — 마지막 버블에만 */}
              {m.role === "assistant" && busy && progress != null && m.parts.length > 0
                && i === messages.length - 1
                && <p className="chat-text muted">{progress}…</p>}
            </div>
          );
        })}
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
          disabled={busy}
        />
        <button type="button" className="chat-send" onClick={() => void send()}
          disabled={busy || !input.trim()}>전송</button>
      </div>
      </div>
    </div>
  );
}

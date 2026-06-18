import { memo, useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { CompileQuota } from "../api";
import type { ChatMessage, ChatPart } from "../types";
import ChatResultView from "../components/ChatResultView";

// memo — 스트리밍 중 텍스트 델타로 메시지가 갱신돼도 참조가 안 바뀐 tool_result(차트) 파트는
// 재렌더하지 않는다(델타마다 차트 재렌더되는 렉 회피).
const PartView = memo(function PartView({ part }: { part: ChatPart }) {
  if (part.type === "text") return <p className="chat-text">{part.text}</p>;
  if (part.type === "tool_use") return <div className="chat-tool">🔧 {part.name} 실행 중…</div>;
  if (part.type === "tool_result") return <ChatResultView result={part.result} />;
  return null;
});

export default function ChatLab() {
  const [convId, setConvId] = useState<number | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
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

  async function send() {
    const text = input.trim();
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
      const msg = e instanceof Error ? e.message : "오류가 발생했습니다.";
      setError(msg);
      // 프론트 단계 오류(429 한도 초과 안내 포함)로 빈 assistant 버블이 남으면 그 문구로 채운다.
      patch((parts) => (parts.length ? parts : [{ type: "text", text: msg }]));
    } finally {
      setBusy(false);
      refreshQuota();              // 턴 후 카운터 갱신(차단 429였어도 used 반영)
    }
  }

  return (
    <div className="chat-lab">
      <h1 className="chat-title">전략 연구소 <span className="chat-beta">챗봇 베타</span></h1>
      <p className="chat-sub muted">자연어로 종목 분석·백테스트를 요청하고 대화하세요.</p>
      <div className="chat-thread" ref={threadRef}>
        {messages.length === 0 && (
          <div className="chat-empty muted">
            예: "저평가 반도체주 3개 골라줘" · "삼성전자 20일선 돌파 매수 전략 백테스트해줘"
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={"chat-msg " + m.role}>
            {m.parts.length === 0
              ? (m.role === "assistant" && busy
                  ? <p className="chat-text muted">분석 중…</p>
                  : null)
              : m.parts.map((p, j) => <PartView key={j} part={p} />)}
          </div>
        ))}
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
  );
}

import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { ChatMessage, ChatPart } from "../types";
import ChatResultView from "../components/ChatResultView";

function PartView({ part }: { part: ChatPart }) {
  if (part.type === "text") return <p className="chat-text">{part.text}</p>;
  if (part.type === "tool_use") return <div className="chat-tool">🔧 {part.name} 실행 중…</div>;
  if (part.type === "tool_result") return <ChatResultView result={part.result} />;
  return null;
}

export default function ChatLab() {
  const [convId, setConvId] = useState<number | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);

  // 새 메시지마다 맨 아래로 스크롤
  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages, busy]);

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setBusy(true); setError(null);
    setMessages((m) => [...m, { role: "user", parts: [{ type: "text", text }] }]);
    setInput("");
    try {
      let cid = convId;
      if (cid == null) {
        const conv = await api.createConversation();
        cid = conv.id; setConvId(cid);
      }
      const reply = await api.sendChatMessage(cid, text);
      setMessages((m) => [...m, reply]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "오류가 발생했습니다.");
    } finally {
      setBusy(false);
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
            {m.parts.map((p, j) => <PartView key={j} part={p} />)}
          </div>
        ))}
        {busy && (
          <div className="chat-msg assistant">
            <p className="chat-text muted">분석 중…</p>
          </div>
        )}
      </div>
      {error && <div className="chat-error">{error}</div>}
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

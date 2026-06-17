import { memo, useEffect, useRef, useState } from "react";
import { api } from "../api";
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
  const threadRef = useRef<HTMLDivElement>(null);

  // 새 메시지·스트리밍 갱신마다 맨 아래로 스크롤
  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages, busy]);

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
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : "오류가 발생했습니다.";
      setError(msg);
      // 프론트 단계 오류로 빈 assistant 버블이 남으면 오류 문구로 채운다.
      patch((parts) => (parts.length ? parts : [{ type: "text", text: msg }]));
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
            {m.parts.length === 0
              ? (m.role === "assistant" && busy
                  ? <p className="chat-text muted">분석 중…</p>
                  : null)
              : m.parts.map((p, j) => <PartView key={j} part={p} />)}
          </div>
        ))}
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

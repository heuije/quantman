"""전략 연구소 챗봇 agent 루프 + 영속/컨텍스트 헬퍼.

DB는 논리적 턴(parts: text/tool_use/tool_result, full payload)을 저장하고, Anthropic 와이어
포맷으로 복원할 때 tool_result는 compact 요약으로 환원한다(chat_lab_spec §5).

공개 API:
  stream_chat_turn — agent 루프를 이벤트로 흘리는 제너레이터(단일 소스, /chat/stream).
  run_chat_turn — 위를 소진해 parts를 반환하는 비스트리밍 진입점(/chat/message).
  _persist, _history_to_wire — 헬퍼(단위 테스트·내부 직접 호출용).
"""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from ..models import Message
from .tools import TOOL_SCHEMAS, compact_summary, run_tool
from .prompt import chat_system_prompt

_log = logging.getLogger("app.chat.agent")


def _persist(session: Session, conversation_id: int, role: str, parts: list) -> None:
    session.add(Message(conversation_id=conversation_id, role=role, parts=parts))
    session.commit()


def _history_to_wire(session: Session, conversation_id: int) -> list[dict]:
    """저장된 메시지 → Anthropic messages 배열. tool_result는 compact로 환원하고,
    도구 라운드 경계를 복원해 엄격 교대(assistant tool_use → user tool_result → assistant text)."""
    rows = session.exec(
        select(Message).where(Message.conversation_id == conversation_id)
        .order_by(Message.id)).all()
    wire: list[dict] = []
    for m in rows:
        if m.role == "user":
            text = next((p["text"] for p in m.parts if p.get("type") == "text"), "")
            wire.append({"role": "user", "content": text})
            continue
        a_content: list[dict] = []      # 현재 assistant 블록(text + tool_use)
        results: list[dict] = []        # 현재 라운드의 tool_result(compact)
        for p in m.parts:
            t = p.get("type")
            if t in ("text", "tool_use") and results:
                # 이전 라운드 닫기: assistant 블록 + tool_result user 블록 방출 후 새 라운드 시작
                wire.append({"role": "assistant", "content": a_content})
                wire.append({"role": "user", "content": results})
                a_content = []
                results = []
            if t == "text":
                a_content.append({"type": "text", "text": p["text"]})
            elif t == "tool_use":
                a_content.append({"type": "tool_use", "id": p["id"],
                                  "name": p["name"], "input": p.get("input") or {}})
            elif t == "tool_result":
                results.append({"type": "tool_result", "tool_use_id": p["tool_use_id"],
                                "content": compact_summary(p.get("name", ""), p.get("result") or {})})
        if a_content:
            wire.append({"role": "assistant", "content": a_content})
        if results:
            wire.append({"role": "user", "content": results})
    return wire


MAX_TOOL_ROUNDS = 8     # 한 사용자 턴당 도구 라운드 상한(무한루프·비용 가드)


def _block_to_wire(b) -> dict:
    """Anthropic 응답 content 블록(SDK 객체) → 다음 호출용 와이어 dict."""
    t = getattr(b, "type", None)
    if t == "text":
        return {"type": "text", "text": b.text}
    if t == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input or {})}
    return {"type": t}


def stream_chat_turn(session: Session, conversation_id: int, user_text: str,
                     *, client=None, model: str | None = None):
    """한 사용자 메시지에 대해 agent 루프를 **스트리밍**으로 실행하는 제너레이터(단일 소스).

    아래 이벤트 튜플을 발생 순서대로 yield한다:
      ("delta", {"text": ...})              모델 서술 토큰(점진 표시)
      ("tool_use", {"id","name","input"})   도구 호출 시작(🔧 실행 중)
      ("tool_result", {"tool_use_id","name","result"})  도구 결과(full payload — 인라인 렌더)
      ("done", {"parts": [...]})            턴 종료 — 영속된 assistant parts(비스트리밍 drain용)

    도구 호출·결과·서술을 DB에 영속한다(run_chat_turn은 이 제너레이터를 소진해 parts를 반환).
    """
    from ..config import settings
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    model = model or settings.CHAT_MODEL
    system = [{"type": "text", "text": chat_system_prompt(),
               "cache_control": {"type": "ephemeral"}}]

    messages = _history_to_wire(session, conversation_id)
    messages.append({"role": "user", "content": user_text})
    _persist(session, conversation_id, "user", [{"type": "text", "text": user_text}])

    assistant_parts: list[dict] = []      # full payload(영속·렌더용)
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            # .messages.stream() 컨텍스트매니저 — text_stream으로 서술 토큰을 흘리고
            # get_final_message()로 도구블록·stop_reason이 포함된 완성 메시지를 받는다.
            with client.messages.stream(model=model, max_tokens=4096, system=system,
                                        tools=TOOL_SCHEMAS, messages=messages) as stream:
                for delta in stream.text_stream:
                    if delta:
                        yield ("delta", {"text": delta})
                resp = stream.get_final_message()

            for b in resp.content:
                if getattr(b, "type", None) == "text":
                    assistant_parts.append({"type": "text", "text": b.text})
            messages.append({"role": "assistant",
                             "content": [_block_to_wire(b) for b in resp.content]})

            if resp.stop_reason != "tool_use":
                break

            tool_results: list[dict] = []
            for b in resp.content:
                if getattr(b, "type", None) != "tool_use":
                    continue
                inp = dict(b.input or {})
                yield ("tool_use", {"id": b.id, "name": b.name, "input": inp})
                full = run_tool(b.name, inp)
                assistant_parts.append({"type": "tool_use", "id": b.id, "name": b.name, "input": inp})
                assistant_parts.append({"type": "tool_result", "tool_use_id": b.id,
                                        "name": b.name, "result": full})
                yield ("tool_result", {"tool_use_id": b.id, "name": b.name, "result": full})
                tool_results.append({"type": "tool_result", "tool_use_id": b.id,
                                     "content": compact_summary(b.name, full)})
            messages.append({"role": "user", "content": tool_results})
    except Exception:   # noqa: BLE001 — 외부 LLM·도구 호출 실패(3rd-party 한계)는 대화에 오류 답변으로 표면화(고아 방지·로깅)
        _log.exception("[chat] turn failed for conversation %s", conversation_id)
        err = "분석 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요."
        assistant_parts.append({"type": "text", "text": err})
        yield ("delta", {"text": err})
    _persist(session, conversation_id, "assistant", assistant_parts)
    yield ("done", {"parts": assistant_parts})


def run_chat_turn(session: Session, conversation_id: int, user_text: str,
                  *, client=None, model: str | None = None) -> list[dict]:
    """비스트리밍 진입점 — 스트리밍 코어를 소진해 이번 턴 assistant parts(full payload)를 반환.
    영속은 stream_chat_turn 안에서 일어난다(단일 소스)."""
    parts: list[dict] = []
    for kind, payload in stream_chat_turn(session, conversation_id, user_text,
                                          client=client, model=model):
        if kind == "done":
            parts = payload["parts"]
    return parts

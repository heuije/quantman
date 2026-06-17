"""전략 연구소 챗봇 agent 루프 + 영속/컨텍스트 헬퍼.

DB는 논리적 턴(parts: text/tool_use/tool_result, full payload)을 저장하고, Anthropic 와이어
포맷으로 복원할 때 tool_result는 compact 요약으로 환원한다(chat_lab_spec §5).

공개 API:
  run_chat_turn — 한 사용자 턴의 agent 루프 진입점(라우터에서 직접 호출).
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


def run_chat_turn(session: Session, conversation_id: int, user_text: str,
                  *, client=None, model: str | None = None) -> list[dict]:
    """한 사용자 메시지에 대해 agent 루프 실행. 도구 호출·결과·서술을 DB에 영속하고
    이번 턴 assistant parts(full payload 포함, 렌더·반환용)를 돌려준다."""
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
            resp = client.messages.create(model=model, max_tokens=4096, system=system,
                                           tools=TOOL_SCHEMAS, messages=messages)
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
                full = run_tool(b.name, inp)
                assistant_parts.append({"type": "tool_use", "id": b.id, "name": b.name, "input": inp})
                assistant_parts.append({"type": "tool_result", "tool_use_id": b.id,
                                        "name": b.name, "result": full})
                tool_results.append({"type": "tool_result", "tool_use_id": b.id,
                                     "content": compact_summary(b.name, full)})
            messages.append({"role": "user", "content": tool_results})
    except Exception:   # noqa: BLE001 — 외부 LLM·도구 호출 실패(3rd-party 한계)는 대화에 오류 답변으로 표면화(고아 방지·로깅)
        _log.exception("[chat] turn failed for conversation %s", conversation_id)
        assistant_parts.append({"type": "text",
                                "text": "분석 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요."})
    _persist(session, conversation_id, "assistant", assistant_parts)
    return assistant_parts

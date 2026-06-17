"""전략 연구소 챗봇 — 대화 스레드 + 메시지 엔드포인트(P0b 비스트리밍 + P1b 스트리밍 SSE).

대화 영속·소유권만 담당하고 실제 분석/도구 호출은 chat.agent에 위임한다. /chat/message는 한 번에
JSON parts를 돌려주고, /chat/stream은 같은 agent 루프를 SSE 이벤트(delta/tool_use/tool_result/done)로
점진 전송한다(단일 소스 stream_chat_turn). ANTHROPIC_API_KEY 미설정 시 503(/ir/compile 패턴 동일).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..deps import get_current_user
from ..models import Conversation, Message, User
from ..chat.agent import run_chat_turn, stream_chat_turn

router = APIRouter(prefix="/chat", tags=["chat"])


class MessageIn(BaseModel):
    conversation_id: int
    message: str


def _owned(session: Session, user: User, conversation_id: int) -> Conversation:
    conv = session.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다.")
    return conv


@router.post("/conversations", status_code=201)
def create_conversation(user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)):
    conv = Conversation(user_id=user.id)
    session.add(conv); session.commit(); session.refresh(conv)
    return {"id": conv.id, "title": conv.title}


@router.get("/conversations")
def list_conversations(user: User = Depends(get_current_user),
                       session: Session = Depends(get_session)):
    rows = session.exec(
        select(Conversation).where(Conversation.user_id == user.id)
        .order_by(Conversation.id.desc())).all()
    return [{"id": c.id, "title": c.title} for c in rows]


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int, user: User = Depends(get_current_user),
                     session: Session = Depends(get_session)):
    _owned(session, user, conversation_id)
    msgs = session.exec(
        select(Message).where(Message.conversation_id == conversation_id)
        .order_by(Message.id)).all()
    return {"id": conversation_id,
            "messages": [{"role": m.role, "parts": m.parts} for m in msgs]}


@router.post("/message")
def post_message(body: MessageIn, user: User = Depends(get_current_user),
                 session: Session = Depends(get_session)):
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503,
                            detail="챗봇이 아직 설정되지 않았습니다(ANTHROPIC_API_KEY 미설정).")
    _owned(session, user, body.conversation_id)
    parts = run_chat_turn(session, body.conversation_id, body.message)
    return {"role": "assistant", "parts": parts}


def _sse(event: str, data: dict) -> str:
    """SSE 프레임 직렬화. data는 한 줄 JSON(개행 없음 — json.dumps가 문자열 내 개행을 이스케이프)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/stream")
def post_message_stream(body: MessageIn, user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)):
    """같은 agent 루프를 SSE로 스트리밍. 503·404는 스트림 시작 전 동기 검사라 일반 JSON 에러."""
    if not settings.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503,
                            detail="챗봇이 아직 설정되지 않았습니다(ANTHROPIC_API_KEY 미설정).")
    _owned(session, user, body.conversation_id)

    def event_stream():
        for kind, payload in stream_chat_turn(session, body.conversation_id, body.message):
            # done의 parts는 프론트가 증분 구성한 것과 동일하고 GET 재조회로도 정전(canonical) — SSE엔
            # 싣지 않아 tool_result full payload 중복 전송을 피한다.
            yield _sse(kind, {} if kind == "done" else payload)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

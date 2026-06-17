"""전략 연구소 챗봇 — 대화 스레드 + 메시지 엔드포인트(P0b, 비스트리밍).

대화 영속·소유권만 담당하고 실제 분석/도구 호출은 chat.agent.run_chat_turn에 위임한다.
ANTHROPIC_API_KEY 미설정 시 503(/ir/compile 패턴과 동일하게 다른 기능엔 무영향).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..deps import get_current_user
from ..models import Conversation, Message, User
from ..chat.agent import run_chat_turn

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

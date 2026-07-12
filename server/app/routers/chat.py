"""전략 연구소 챗봇 — 대화 스레드 + 메시지 엔드포인트(P0b 비스트리밍 + P1b 스트리밍 SSE).

대화 영속·소유권만 담당하고 실제 분석/도구 호출은 chat.agent에 위임한다. /chat/message는 한 번에
JSON parts를 돌려주고, /chat/stream은 같은 agent 루프를 SSE 이벤트(delta/tool_use/tool_result/done)로
점진 전송한다(단일 소스 stream_chat_turn). ANTHROPIC_API_KEY 미설정 시 503(/ir/compile 패턴 동일).
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from ..config import settings
from ..db import engine, get_session
from ..deps import get_current_user
from ..models import ChatTurnMetric, Conversation, Message, User
from ..chat.agent import run_chat_turn, stream_chat_turn

router = APIRouter(prefix="/chat", tags=["chat"])

_KST = ZoneInfo("Asia/Seoul")


class MessageIn(BaseModel):
    conversation_id: int
    message: str
    # 운영진 언락 비밀번호(선택). 일치 시 이 요청에 한해 상향 한도가 적용된다.
    admin_password: Optional[str] = None


class QuotaIn(BaseModel):
    """일일 사용량 조회 요청 — 비번 동봉 시 언락 상태까지 검증(메시지 소모 없음)."""
    admin_password: Optional[str] = None


def _owned(session: Session, user: User, conversation_id: int) -> Conversation:
    conv = session.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status_code=404, detail="대화를 찾을 수 없습니다.")
    return conv


def _autotitle(session: Session, conv: Conversation, message: str) -> None:
    """첫 user 메시지로 대화 제목 자동 설정(기본 '새 대화'일 때만) — 목록 식별성."""
    if conv.title != "새 대화":
        return
    n = session.exec(select(func.count()).select_from(Message)
                     .where(Message.conversation_id == conv.id)).one()
    if int(n or 0) > 0:
        return
    title = " ".join(message.split())[:40]
    if title:
        conv.title = title
        session.add(conv)
        session.commit()


def _kst_day_start_utc() -> datetime:
    """오늘(KST) 자정의 UTC 시각 — 일일 카운트 하한. Message.created_at은 UTC 저장
    (models._now)이라 KST 자정을 UTC로 환산해 비교한다. '1일'은 한국 날짜 체감 기준.

    NOTE: routers/ir_compile.py에 동일 헬퍼가 있으나, 이 변경의 범위를 챗봇으로 격리하려
    NL 컴파일러 모듈을 건드리지 않고 4줄을 미러한다. 셋째 소비자가 생기면 공용 util로 추출.
    """
    now_kst = datetime.now(_KST)
    midnight_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_kst.astimezone(timezone.utc)


def _chat_quota(session: Session, user_id: int, admin_password: Optional[str]) -> dict:
    """오늘(KST) 챗봇 사용량·한도·언락 상태(읽기 전용, LLM 호출 없음).

    '사용량' = 오늘 이 유저가 보낸 user 메시지 수(= 질문 턴). 챗봇은 매 user 턴을
    Message(role="user")로 영속하므로 별도 카운터 테이블 없이 그 행을 센다(단일 진실원천).
    유저의 모든 대화를 합산해 새 대화로는 우회 불가. admin_password가 일치하면 한도를
    CHAT_ADMIN_LIMIT로 상향(단일 공유 시크릿·무제한 아님).
    """
    unlocked = bool(admin_password) and admin_password == settings.CHAT_ADMIN_PASSWORD
    limit = settings.CHAT_ADMIN_LIMIT if unlocked else settings.CHAT_DAILY_LIMIT
    used = session.exec(
        select(func.count()).select_from(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(Conversation.user_id == user_id)
        .where(Message.role == "user")
        .where(Message.created_at >= _kst_day_start_utc())
    ).one()
    used = int(used or 0)
    return {"used": used, "limit": limit, "remaining": max(0, limit - used),
            "admin_unlocked": unlocked}


def _rate_limited(quota: dict) -> JSONResponse:
    """한도 초과 응답(429). 스트림·비스트림 공통 — 프론트가 카운터 갱신·언락 안내에 쓴다."""
    return JSONResponse(status_code=429, content={
        "rate_limited": True,
        "detail": (f"오늘 챗봇 사용 한도({quota['limit']}회)를 모두 사용했어요. "
                   "내일 다시 시도하거나 운영진 비밀번호로 한도를 해제하세요."),
        "used": quota["used"], "limit": quota["limit"],
        "remaining": quota["remaining"], "admin_unlocked": quota["admin_unlocked"]})


@router.post("/conversations", status_code=201)
def create_conversation(user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)):
    conv = Conversation(user_id=user.id)
    session.add(conv)
    session.commit()
    session.refresh(conv)
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


class ConvUpdateIn(BaseModel):
    title: str


@router.patch("/conversations/{conversation_id}")
def rename_conversation(conversation_id: int, body: ConvUpdateIn,
                        user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)):
    conv = _owned(session, user, conversation_id)
    conv.title = body.title.strip()[:80] or "새 대화"
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return {"id": conv.id, "title": conv.title}


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: int, user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)):
    """대화 + 종속행(메시지·턴지표) 하드 삭제. FK ondelete 미설정이라 명시적 cascade."""
    conv = _owned(session, user, conversation_id)
    for m in session.exec(select(Message)
                          .where(Message.conversation_id == conversation_id)).all():
        session.delete(m)
    for tm in session.exec(select(ChatTurnMetric)
                           .where(ChatTurnMetric.conversation_id == conversation_id)).all():
        session.delete(tm)
    session.delete(conv)
    session.commit()


@router.post("/quota")
def chat_quota(body: QuotaIn, user: User = Depends(get_current_user),
               session: Session = Depends(get_session)):
    """오늘(KST) 챗봇 사용량·한도·언락 상태. 카운터 표시 + 비번 즉시 검증(메시지 소모 없음)."""
    return _chat_quota(session, user.id, body.admin_password)


def _require_chat_llm() -> None:
    """챗 LLM 접근 구성 가드 — 프로덕션=ANTHROPIC_API_KEY, 로컬 구독 모드=QP_CHAT_LOCAL_SUBSCRIPTION(claude -p·$0)."""
    if settings.ANTHROPIC_API_KEY or os.environ.get("QP_CHAT_LOCAL_SUBSCRIPTION") == "1":
        return
    raise HTTPException(status_code=503,
                        detail="챗봇이 아직 설정되지 않았습니다(ANTHROPIC_API_KEY 미설정).")


@router.post("/message")
def post_message(body: MessageIn, user: User = Depends(get_current_user),
                 session: Session = Depends(get_session)):
    _require_chat_llm()
    conv = _owned(session, user, body.conversation_id)
    quota = _chat_quota(session, user.id, body.admin_password)
    if quota["remaining"] <= 0:                       # 서버 강제 — LLM 호출 전 차단(비용·악용 통제)
        return _rate_limited(quota)
    _autotitle(session, conv, body.message)           # 첫 메시지면 제목 자동 설정
    parts = run_chat_turn(session, body.conversation_id, body.message)
    return {"role": "assistant", "parts": parts}


def _sse(event: str, data: dict) -> str:
    """SSE 프레임 직렬화. data는 한 줄 JSON(개행 없음 — json.dumps가 문자열 내 개행을 이스케이프)."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_SSE_KEEPALIVE_SEC = 15.0


def _stream_with_keepalive(gen, interval: float = _SSE_KEEPALIVE_SEC):
    """동기 턴 제너레이터를 producer 스레드에서 소진해 SSE 텍스트로 내보내는 async 제너레이터.

    실측 부류 2건(conv#4/6/7)을 전송층 한 곳에서 마감한다:
    ① 침묵 끊김 — LLM 라운드(로컬 $0 shim은 라운드당 수 분) 동안 이벤트가 0이라 유휴
       연결이 끊기고 클라이언트에 network error로 표면화 → 유휴 interval마다 SSE
       주석(': ka')을 흘려 연결을 살린다(주석은 SSE 표준 무시 대상·웹 파서도
       event: 없는 프레임을 건너뛴다).
    ② 결과 유실 — 클라이언트가 끊기면 종전엔 다음 yield에서 GeneratorExit로 턴이
       중단돼 수십 분짜리 분석이 완성 직전에 날아갔다 → producer 스레드는 소비자와
       무관하게 턴을 끝까지 실행·영속한다(사용자는 대화 재열람으로 완성 답변 수신).
       agent의 GeneratorExit 백스톱은 프로세스 종료 등 진짜 close에 대해 유지된다.
    """
    q: queue.Queue = queue.Queue()
    _DONE = object()

    def _produce():
        try:
            for item in gen:
                q.put(item)
        except Exception as exc:   # noqa: BLE001 — 소비자로 재전파(종전 동기 스트림과 동일 표면).
            q.put(("__raise__", exc))   # 삼키면 '빈 200 스트림'이라는 조용한 오답이 된다.
        finally:
            q.put(_DONE)

    threading.Thread(target=_produce, daemon=True).start()

    async def _consume():
        loop = asyncio.get_running_loop()
        while True:
            try:
                item = await loop.run_in_executor(None, q.get, True, interval)
            except queue.Empty:
                yield ": ka\n\n"
                continue
            if item is _DONE:
                return
            kind, payload = item
            if kind == "__raise__":
                raise payload
            yield _sse(kind, {} if kind == "done" else payload)

    return _consume()


@router.post("/stream")
def post_message_stream(body: MessageIn, user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)):
    """같은 agent 루프를 SSE로 스트리밍. 503·404·429는 스트림 시작 전 동기 검사라 일반 JSON 에러."""
    _require_chat_llm()
    conv = _owned(session, user, body.conversation_id)
    quota = _chat_quota(session, user.id, body.admin_password)
    if quota["remaining"] <= 0:                       # 스트림 시작 전 동기 차단(서버 강제)
        return _rate_limited(quota)
    _autotitle(session, conv, body.message)           # 첫 메시지면 제목 자동 설정

    bind = session.get_bind()   # 요청 세션과 같은 엔진(DI 오버라이드·테스트 DB 포함)에서 파생

    def turn_gen():
        # 턴 전용 세션 — 요청 세션(Depends)은 응답 종료(클라 끊김 포함) 시 닫히는데,
        # producer 스레드는 그 뒤에도 턴을 완주·영속해야 하므로 수명을 분리한다.
        # (done의 parts는 SSE에 싣지 않음 — 프론트 증분 구성과 GET 재조회가 정전.)
        with Session(bind) as turn_session:
            yield from stream_chat_turn(turn_session, body.conversation_id, body.message)

    return StreamingResponse(_stream_with_keepalive(turn_gen()),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

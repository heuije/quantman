"""웹 ↔ 로컬앱 명령 버스.

웹 사용자가 발행한 명령을 큐(DB)에 저장하고, 로컬앱이 SSE로 수신해
실행 후 결과를 ack한다.

지원 명령:
- RUN_CYCLE_NOW: 사이클 즉시 실행
- PAUSE_AUTO / RESUME_AUTO: 스케줄러 일시정지/재개
- LIQUIDATE_ALL: 보유 전량 청산 + kill switch ON
- CANCEL_ORDER: 특정 미체결 주문 취소 (params.order_no)
- RESET_KILL_SWITCH: kill switch 해제
- RECONCILE_NOW: KIS 잔고 ↔ ledger 즉시 정합성 점검 + 자동 정정 (Phase 40)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from ..db import get_session, engine
from ..deps import get_current_device, get_current_user
from ..models import Command, Device, User
from ..schemas import CommandAckIn, CommandIn, CommandOut

router = APIRouter(prefix="/sync/commands", tags=["commands"])

VALID_TYPES = {
    "RUN_CYCLE_NOW", "PAUSE_AUTO", "RESUME_AUTO", "LIQUIDATE_ALL",
    "CANCEL_ORDER", "RESET_KILL_SWITCH",
    "RECONCILE_NOW",   # Phase 40 — 수동 잔고 정합성 점검
}


def _to_out(c: Command) -> CommandOut:
    return CommandOut(
        id=c.id, device_id=c.device_id, type=c.type, params=c.params,
        status=c.status, created_at=c.created_at,
        delivered_at=c.delivered_at, completed_at=c.completed_at,
        result=c.result,
    )


# ── 웹 → 명령 발행 / 조회 ─────────────────────────────────────────────────────

@router.post("", response_model=CommandOut)
def create_command(
    body: CommandIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if body.type not in VALID_TYPES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"지원되지 않는 명령: {body.type}")
    device = session.get(Device, body.device_id)
    if device is None or device.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "기기를 찾을 수 없습니다.")
    cmd = Command(user_id=user.id, device_id=body.device_id,
                  type=body.type, params=body.params)
    session.add(cmd)
    session.commit()
    session.refresh(cmd)
    return _to_out(cmd)


@router.get("", response_model=list[CommandOut])
def list_commands(
    device_id: int | None = Query(default=None),
    only_pending: bool = Query(default=False),
    limit: int = Query(default=50, le=200),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    stmt = select(Command).where(Command.user_id == user.id)
    if device_id is not None:
        stmt = stmt.where(Command.device_id == device_id)
    if only_pending:
        stmt = stmt.where(Command.status.in_(["pending", "delivered"]))
    stmt = stmt.order_by(Command.created_at.desc()).limit(limit)
    return [_to_out(c) for c in session.exec(stmt).all()]


# ── 로컬앱 → 수신 (SSE 또는 폴링) / ack ───────────────────────────────────────

@router.get("/poll", response_model=list[CommandOut])
def poll_pending(
    device: Device = Depends(get_current_device),
    session: Session = Depends(get_session),
):
    """SSE를 못 쓰는 환경에서의 폴링 fallback.

    pending 상태의 명령을 가져오면서 자동으로 delivered 상태로 마킹.
    """
    rows = session.exec(
        select(Command).where(
            Command.device_id == device.id,
            Command.status == "pending",
        ).order_by(Command.created_at.asc())
    ).all()
    now = datetime.now(timezone.utc)
    for c in rows:
        c.status = "delivered"
        c.delivered_at = now
        session.add(c)
    session.commit()
    return [_to_out(c) for c in rows]


@router.post("/{cmd_id}/ack", response_model=CommandOut)
def ack_command(
    cmd_id: int, body: CommandAckIn,
    device: Device = Depends(get_current_device),
    session: Session = Depends(get_session),
):
    cmd = session.get(Command, cmd_id)
    if cmd is None or cmd.device_id != device.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "명령을 찾을 수 없습니다.")
    if body.status not in ("done", "failed"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "status는 'done' 또는 'failed'여야 합니다.")
    cmd.status = body.status
    cmd.result = body.result or {}
    cmd.completed_at = datetime.now(timezone.utc)
    session.add(cmd)
    session.commit()
    session.refresh(cmd)
    return _to_out(cmd)


@router.get("/stream")
async def stream_commands(
    device: Device = Depends(get_current_device),
):
    """SSE — 로컬앱이 long-lived connection을 열어 명령을 실시간 수신.

    매 2초 DB를 폴링해 새 pending 명령을 yield. 30초마다 heartbeat 주석.
    명령 송신 시 자동으로 delivered 상태로 마킹.
    """
    device_id = device.id

    async def event_gen():
        last_hb = asyncio.get_event_loop().time()
        # 초기 카운트
        yield ": connected\n\n"
        while True:
            try:
                # DB 폴링 — 동기 세션이지만 1쿼리라 짧음
                with Session(engine) as sess:
                    rows = sess.exec(
                        select(Command).where(
                            Command.device_id == device_id,
                            Command.status == "pending",
                        ).order_by(Command.created_at.asc())
                    ).all()
                    now = datetime.now(timezone.utc)
                    payload = []
                    for c in rows:
                        c.status = "delivered"
                        c.delivered_at = now
                        sess.add(c)
                        payload.append({
                            "id": c.id, "type": c.type,
                            "params": c.params,
                            "created_at": c.created_at.isoformat(),
                        })
                    if payload:
                        sess.commit()
                for row in payload:
                    yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
                # Heartbeat
                now_t = asyncio.get_event_loop().time()
                if now_t - last_hb > 25:
                    yield ": heartbeat\n\n"
                    last_hb = now_t
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                # 연결 끊김·DB 오류 등은 종료
                yield f"event: error\ndata: {json.dumps({'msg': str(e)})}\n\n"
                break

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",      # 프록시 버퍼링 비활성
        "Connection": "keep-alive",
    }
    return StreamingResponse(event_gen(), media_type="text/event-stream",
                              headers=headers)

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

from fastapi import (APIRouter, Depends, HTTPException, Query, Request,
                     Response, status)
from fastapi.responses import StreamingResponse
from sqlmodel import Session, func, select

from ..db import get_session, engine
from ..deps import get_current_device, get_current_user
from ..models import Command, Device, User
from ..schemas import CommandAckIn, CommandIn, CommandOut

router = APIRouter(prefix="/sync/commands", tags=["commands"])


# ── In-process pub/sub (Audit P0-2) ───────────────────────────────────────────
#
# 옛(v0.9.7-): SSE generator가 매 2초 DB SELECT — idle device 1개당 1800 SELECT/h.
# 새: device당 asyncio.Queue. create_command이 put_nowait로 즉시 broadcast,
#     generator는 queue.get()으로 idle 시 DB 부담 0.
#
# 단일 worker 전제 — Railway uvicorn default 1 worker (SSE 자체가 sticky session
# 필요하므로 multi-worker는 추가 인프라). 향후 multi-worker 도입 시 PostgreSQL
# LISTEN/NOTIFY 마이그레이션 필요.
_device_queues: dict[int, asyncio.Queue] = {}


def _publish_to_device(device_id: int, payload: dict) -> None:
    """create_command 직후 호출 — 해당 device의 SSE generator 즉시 wake."""
    q = _device_queues.get(device_id)
    if q is not None:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            # Queue maxsize 무제한이므로 정상 흐름에선 도달 안 함 (방어적 가드).
            pass


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
    # Audit P0-2 — SSE generator 즉시 wake (in-process pub/sub).
    _publish_to_device(body.device_id, {
        "id": cmd.id, "type": cmd.type, "params": cmd.params,
        "created_at": cmd.created_at.isoformat(),
    })
    return _to_out(cmd)


def _epoch_ms(ts: datetime | None) -> int:
    """ETag 성분용 epoch ms. naive는 UTC 저장 관례로 정규화, 없으면 0."""
    if ts is None:
        return 0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


@router.get("", response_model=list[CommandOut])
def list_commands(
    request: Request,
    response: Response,
    device_id: int | None = Query(default=None),
    only_pending: bool = Query(default=False),
    limit: int = Query(default=50, le=200),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    conds = [Command.user_id == user.id]
    if device_id is not None:
        conds.append(Command.device_id == device_id)
    if only_pending:
        conds.append(Command.status.in_(["pending", "delivered"]))

    # tag-first ETag(서버 인프라 핸드오프 #5) — Monitor가 15s 폴링하는 웹 조회 GET.
    # params/result JSON 컬럼을 읽기 *전에* scalar aggregate로 ETag를 계산해 매칭 시
    # 304만 반환한다(JSON 미조회·본문 0). 모든 명령 변이가 세 timestamp 중 하나를
    # "지금"으로 전진시키고(생성=created_at, SSE/poll pickup=delivered_at,
    # ack=completed_at — result도 ack에서만 갱신) 삭제(revoke)는 count가 포착한다.
    # 필터(device_id·only_pending)를 aggregate에도 동일 적용해 뷰별 tag 분리.
    # ⚠ 명령 버스(POST·/poll·/stream·ack) 경로는 불변 — 웹 GET 조회만 조건부 304 추가.
    # 패턴 출처: /sync/snapshot tag-first
    # (docs/incidents/2026-06-10-neon-data-transfer-quota.md 재발 방지 D3).
    n, c_max, d_max, done_max = session.exec(
        select(func.count(Command.id), func.max(Command.created_at),
               func.max(Command.delivered_at), func.max(Command.completed_at))
        .where(*conds)
    ).one()
    etag = f'W/"{n}-{_epoch_ms(c_max)}-{_epoch_ms(d_max)}-{_epoch_ms(done_max)}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304)
    response.headers["ETag"] = etag

    stmt = (select(Command).where(*conds)
            .order_by(Command.created_at.desc()).limit(limit))
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

    Audit P0-2 — in-process asyncio.Queue로 idle DB SELECT 0 화. create_command이
    put_nowait()로 broadcast → generator는 queue.get()으로 wait. idle 사용자
    1명당 1800 SELECT/h → 0.

    흐름:
      1. SSE 연결 시 device용 queue 생성·등록.
      2. 첫 연결 시 기존 pending 명령 1회 DB fetch (앱 재시작·SSE 끊긴 동안 누적분).
      3. queue.get(timeout=25)로 wait — 명령 들어오면 즉시 yield, timeout 시 heartbeat.
      4. 연결 끊김(CancelledError) 시 queue 정리.

    동시 동일 device 다중 연결: 마지막 연결만 queue 갖고, 이전 SSE는 끊김 시 자동 정리.
    """
    device_id = device.id
    queue: asyncio.Queue = asyncio.Queue()
    _device_queues[device_id] = queue

    async def event_gen():
        yield ": connected\n\n"
        # 첫 연결 시 누적된 pending 명령 fetch (SSE 끊긴 동안 발사된 명령).
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
                    "id": c.id, "type": c.type, "params": c.params,
                    "created_at": c.created_at.isoformat(),
                })
            if payload:
                sess.commit()
        for row in payload:
            yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"

        try:
            while True:
                try:
                    row = await asyncio.wait_for(queue.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                # Queue로 받은 명령 — DB에서 delivered 상태로 마킹 (idempotent).
                with Session(engine) as sess:
                    cmd = sess.get(Command, row["id"])
                    if cmd is not None and cmd.status == "pending":
                        cmd.status = "delivered"
                        cmd.delivered_at = datetime.now(timezone.utc)
                        sess.add(cmd)
                        sess.commit()
                yield f"data: {json.dumps(row, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            # 연결 끊김 — queue 정리 (메모리 누수 방지).
            if _device_queues.get(device_id) is queue:
                _device_queues.pop(device_id, None)
            raise

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",      # 프록시 버퍼링 비활성
        "Connection": "keep-alive",
    }
    return StreamingResponse(event_gen(), media_type="text/event-stream",
                              headers=headers)

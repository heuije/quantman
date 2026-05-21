"""내일 매매 미리보기 라우터 — 사용자가 자기 최신 preview 조회.

각 데이터 갱신 cron 종료 직후 서버가 자동으로 preview를 계산해
sync_snapshot.payload에 next_day_preview로 merge한다. 이 endpoint는
가장 최근 snapshot에서 그 필드만 추출해 반환.

웹 트레이딩/개요 페이지가 polling 또는 페이지 로드 시 호출.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from ..db import get_session
from ..deps import get_current_user
from ..models import SyncSnapshot, User

router = APIRouter(prefix="/preview", tags=["preview"])


@router.get("/next-day")
def next_day_preview(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """가장 최근 snapshot에서 next_day_preview를 반환.

    snapshot이 없거나 preview가 아직 안 들어있으면 available=false.
    """
    snap = session.exec(
        select(SyncSnapshot).where(SyncSnapshot.user_id == user.id)
        .order_by(SyncSnapshot.received_at.desc())
    ).first()

    if snap is None or not snap.payload:
        return {
            "available": False,
            "reason": "로컬앱 페어링·sync 필요",
        }

    preview = (snap.payload or {}).get("next_day_preview")
    if preview is None:
        return {
            "available": False,
            "reason": "preview 아직 생성 안 됨 (다음 cron 갱신 대기)",
        }
    return preview


@router.post("/regenerate")
def regenerate_preview(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """현재 dataset·snapshot으로 즉시 preview 재계산 (수동 trigger)."""
    from .. import preview_engine

    preview = preview_engine.build_user_preview(session, user.id, "manual")
    if not preview.get("available"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            preview.get("reason", "preview 생성 불가"))

    # 마지막 snapshot에 merge
    snap = session.exec(
        select(SyncSnapshot).where(SyncSnapshot.user_id == user.id)
        .order_by(SyncSnapshot.received_at.desc())
    ).first()
    if snap is not None:
        new_payload = dict(snap.payload or {})
        new_payload["next_day_preview"] = preview
        snap.payload = new_payload
        session.add(snap)
        session.commit()

    return preview

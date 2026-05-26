"""전략 CRUD + 버전 이력·현황 라우터 (Phase 59)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import quant_core as qc
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlmodel import Session, select

from ..db import get_session
from ..deps import get_current_user
from ..models import BacktestRun, Strategy, StrategyVersion, SyncSnapshot, User
from ..schemas import (StrategyIn, StrategyOut, StrategyRestoreIn,
                       StrategyStatsOut, StrategyVersionOut)

router = APIRouter(prefix="/strategies", tags=["strategies"])

_VALID_MODES = {"draft", "paper", "live"}

# Phase 59 — 자동 스냅샷 회전 정책
_VERSION_MAX_KEEP = 50         # strategy당 최대 보관 버전 수
_VERSION_MAX_AGE_DAYS = 30     # 30일 이전 버전 자동 삭제


def _validate(definition: dict) -> qc.Strategy:
    """definition이 core Strategy 스키마에 맞는지 검증."""
    try:
        return qc.Strategy(**definition)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"전략 정의가 올바르지 않습니다: {e.errors()[0]['msg']}")


def _out(s: Strategy) -> StrategyOut:
    return StrategyOut(id=s.id, name=s.name, run_mode=s.run_mode,
                       definition=s.definition, created_at=s.created_at,
                       updated_at=s.updated_at,
                       paper_started_at=s.paper_started_at,
                       live_started_at=s.live_started_at)


def _own_or_404(session: Session, strategy_id: int, user_id: int) -> Strategy:
    row = session.get(Strategy, strategy_id)
    if row is None or row.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "전략을 찾을 수 없습니다.")
    return row


def _next_version_no(session: Session, strategy_id: int) -> int:
    cur = session.exec(
        select(StrategyVersion.version_no)
        .where(StrategyVersion.strategy_id == strategy_id)
        .order_by(StrategyVersion.version_no.desc())
    ).first()
    return (cur or 0) + 1


def _snapshot_version(session: Session, row: Strategy, reason: str) -> None:
    """Strategy의 현재 정의를 새 버전으로 스냅샷 + 회전 정책 적용.

    호출 시점: PUT 직전 (변경 전 정의 보존) 또는 restore 직전.
    회전: 50건 초과분 또는 30일 이전 버전 삭제. flush는 호출자가.
    """
    ver = StrategyVersion(
        strategy_id=row.id, version_no=_next_version_no(session, row.id),
        name=row.name, definition=row.definition, created_reason=reason)
    session.add(ver)

    # 회전 정책: 1) 30일 이전 삭제 2) 50건 초과 시 가장 오래된 것부터 삭제
    cutoff = datetime.now(timezone.utc) - timedelta(days=_VERSION_MAX_AGE_DAYS)
    old = session.exec(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == row.id,
            StrategyVersion.created_at < cutoff)).all()
    for v in old:
        session.delete(v)

    all_versions = session.exec(
        select(StrategyVersion).where(StrategyVersion.strategy_id == row.id)
        .order_by(StrategyVersion.version_no.desc())
    ).all()
    if len(all_versions) > _VERSION_MAX_KEEP:
        for v in all_versions[_VERSION_MAX_KEEP:]:
            session.delete(v)


@router.get("", response_model=list[StrategyOut])
def list_strategies(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    rows = session.exec(select(Strategy).where(Strategy.user_id == user.id)).all()
    return [_out(s) for s in rows]


@router.post("", response_model=StrategyOut, status_code=status.HTTP_201_CREATED)
def create_strategy(
    body: StrategyIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if body.run_mode not in _VALID_MODES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "run_mode가 올바르지 않습니다.")
    strat = _validate(body.definition)
    now = datetime.now(timezone.utc)
    row = Strategy(user_id=user.id, name=strat.name, run_mode=body.run_mode,
                   definition=strat.model_dump(),
                   paper_started_at=now if body.run_mode == "paper" else None,
                   live_started_at=now if body.run_mode == "live" else None)
    session.add(row)
    session.commit()
    session.refresh(row)
    # 최초 버전 스냅샷 (initial)
    initial = StrategyVersion(
        strategy_id=row.id, version_no=1, name=row.name,
        definition=row.definition, created_reason="initial")
    session.add(initial)
    session.commit()
    return _out(row)


@router.get("/{strategy_id}", response_model=StrategyOut)
def get_strategy(
    strategy_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    return _out(_own_or_404(session, strategy_id, user.id))


@router.put("/{strategy_id}", response_model=StrategyOut)
def update_strategy(
    strategy_id: int,
    body: StrategyIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    row = _own_or_404(session, strategy_id, user.id)
    if body.run_mode not in _VALID_MODES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "run_mode가 올바르지 않습니다.")
    strat = _validate(body.definition)

    # Phase 59 — 변경 전 정의를 버전으로 스냅샷 (사용자 선택: 매 PUT마다)
    _snapshot_version(session, row, reason="manual_edit")

    # run_mode 전환 timestamp 기록
    now = datetime.now(timezone.utc)
    if body.run_mode == "paper" and row.run_mode != "paper":
        row.paper_started_at = now
    if body.run_mode == "live" and row.run_mode != "live":
        row.live_started_at = now

    row.name = strat.name
    row.run_mode = body.run_mode
    row.definition = strat.model_dump()
    row.updated_at = now
    session.add(row)
    session.commit()
    session.refresh(row)
    return _out(row)


@router.delete("/{strategy_id}")
def delete_strategy(
    strategy_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    row = _own_or_404(session, strategy_id, user.id)
    # 연관 버전·백테스트 cascade 삭제
    for v in session.exec(
            select(StrategyVersion).where(
                StrategyVersion.strategy_id == strategy_id)).all():
        session.delete(v)
    for b in session.exec(
            select(BacktestRun).where(
                BacktestRun.strategy_id == strategy_id)).all():
        session.delete(b)
    session.delete(row)
    session.commit()
    return {"ok": True}


# ── Phase 59 — 버전 이력 endpoint ────────────────────────────────────────────

@router.get("/{strategy_id}/versions", response_model=list[StrategyVersionOut])
def list_versions(
    strategy_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """전략 버전 목록 (최신순). definition은 omit — 상세는 단일 조회로."""
    _own_or_404(session, strategy_id, user.id)
    rows = session.exec(
        select(StrategyVersion).where(StrategyVersion.strategy_id == strategy_id)
        .order_by(StrategyVersion.version_no.desc())
    ).all()
    return [StrategyVersionOut(
        version_no=v.version_no, name=v.name,
        created_at=v.created_at, created_reason=v.created_reason)
        for v in rows]


@router.get("/{strategy_id}/versions/{version_no}",
            response_model=StrategyVersionOut)
def get_version(
    strategy_id: int,
    version_no: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """특정 버전의 전체 정의."""
    _own_or_404(session, strategy_id, user.id)
    v = session.exec(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.version_no == version_no)).first()
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "버전을 찾을 수 없습니다.")
    return StrategyVersionOut(
        version_no=v.version_no, name=v.name,
        created_at=v.created_at, created_reason=v.created_reason,
        definition=v.definition)


@router.post("/{strategy_id}/restore", response_model=StrategyOut)
def restore_version(
    strategy_id: int,
    body: StrategyRestoreIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """특정 버전으로 현재 정의 복원. 복원 직전 현재 상태도 새 버전으로 보존."""
    row = _own_or_404(session, strategy_id, user.id)
    target = session.exec(
        select(StrategyVersion).where(
            StrategyVersion.strategy_id == strategy_id,
            StrategyVersion.version_no == body.version_no)).first()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "복원할 버전을 찾을 수 없습니다.")
    # 복원 전 현재 정의 보존
    _snapshot_version(session, row, reason=f"restore_from_v{body.version_no}")
    row.name = target.name
    row.definition = target.definition
    row.updated_at = datetime.now(timezone.utc)
    session.add(row)
    session.commit()
    session.refresh(row)
    return _out(row)


# ── Phase 59 — 현황·백테스트 내역 endpoint ───────────────────────────────────

def _days_between(then: datetime | None, now: datetime) -> int | None:
    if then is None:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, (now - then).days)


@router.get("/{strategy_id}/stats", response_model=StrategyStatsOut)
def get_stats(
    strategy_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """전략 현황 — 적용 기간 + 최신 snapshot의 strategy_pnl에서 누적 손익 추출."""
    row = _own_or_404(session, strategy_id, user.id)
    now = datetime.now(timezone.utc)

    # 최신 snapshot에서 by_strategy 필드 추출 (전략명 매칭).
    # 로컬앱이 push하는 SyncSnapshot.payload.strategy_pnl.by_strategy[] =
    # [{strategy_name, trades, win_rate, pnl, ...}, ...]
    snap = session.exec(
        select(SyncSnapshot).where(SyncSnapshot.user_id == user.id)
        .order_by(SyncSnapshot.received_at.desc())
    ).first()

    pnl_total = pnl_pct = win_rate = None
    n_trades = 0
    n_positions = 0
    last_snapshot_at = None
    if snap is not None:
        last_snapshot_at = snap.received_at
        payload = snap.payload or {}
        by_strat = (payload.get("strategy_pnl") or {}).get("by_strategy") or []
        for s in by_strat:
            if s.get("strategy_name") == row.name:
                pnl_total = s.get("pnl")
                win_rate = s.get("win_rate")
                n_trades = int(s.get("trades") or 0)
                break
        positions = payload.get("positions") or []
        n_positions = sum(1 for p in positions
                          if p.get("strategy_name") == row.name)
        # 손익률 — live는 live_capital_at_start, 그 외는 initial_capital
        base_capital = row.live_capital_at_start
        if base_capital and pnl_total is not None:
            pnl_pct = (pnl_total / base_capital) * 100.0

    return StrategyStatsOut(
        paper_started_at=row.paper_started_at,
        live_started_at=row.live_started_at,
        days_paper=_days_between(row.paper_started_at, now),
        days_live=_days_between(row.live_started_at, now),
        pnl_total=pnl_total, pnl_pct=pnl_pct,
        win_rate=win_rate, n_trades=n_trades,
        n_positions=n_positions, last_snapshot_at=last_snapshot_at)


@router.get("/{strategy_id}/backtests")
def list_backtests(
    strategy_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """이 전략으로 실행된 백테스트 내역. 요약 메트릭만, 상세는 /backtest/runs/:id."""
    _own_or_404(session, strategy_id, user.id)
    rows = session.exec(
        select(BacktestRun).where(BacktestRun.strategy_id == strategy_id)
        .order_by(BacktestRun.created_at.desc())
    ).all()
    return [
        {"id": b.id, "name": b.name, "version_no": b.version_no,
         "start": b.start, "end": b.end,
         "initial_capital": b.initial_capital,
         "metrics": (b.result or {}).get("metrics") or {},
         "created_at": b.created_at}
        for b in rows
    ]

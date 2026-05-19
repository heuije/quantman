"""전략 CRUD 라우터."""

from __future__ import annotations

from datetime import datetime, timezone

import quant_core as qc
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlmodel import Session, select

from ..db import get_session
from ..deps import get_current_user
from ..models import Strategy, User
from ..schemas import StrategyIn, StrategyOut

router = APIRouter(prefix="/strategies", tags=["strategies"])

_VALID_MODES = {"draft", "paper", "live"}


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
                       updated_at=s.updated_at)


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
    row = Strategy(user_id=user.id, name=strat.name, run_mode=body.run_mode,
                   definition=strat.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    return _out(row)


@router.get("/{strategy_id}", response_model=StrategyOut)
def get_strategy(
    strategy_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    row = session.get(Strategy, strategy_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "전략을 찾을 수 없습니다.")
    return _out(row)


@router.put("/{strategy_id}", response_model=StrategyOut)
def update_strategy(
    strategy_id: int,
    body: StrategyIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    row = session.get(Strategy, strategy_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "전략을 찾을 수 없습니다.")
    if body.run_mode not in _VALID_MODES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "run_mode가 올바르지 않습니다.")
    strat = _validate(body.definition)
    row.name = strat.name
    row.run_mode = body.run_mode
    row.definition = strat.model_dump()
    row.updated_at = datetime.now(timezone.utc)
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
    row = session.get(Strategy, strategy_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "전략을 찾을 수 없습니다.")
    session.delete(row)
    session.commit()
    return {"ok": True}

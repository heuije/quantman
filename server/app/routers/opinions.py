"""개별 기업 투자의견 게시판 — 종목별 매수/중립/매도 의견 + 댓글 + 좋아요/싫어요.

의견은 ticker에 종속(기업별로 게시판이 분리됨). 로그인 회원만 작성·댓글·투표 가능
(get_current_user). 좋아요/싫어요는 사용자당 1표이며 같은 버튼 재클릭 시 취소된다.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_session
from ..deps import get_current_user
from ..models import OpinionComment, OpinionVote, StockOpinion, User

router = APIRouter(prefix="/opinions", tags=["opinions"])

_STANCES = {"buy", "neutral", "sell"}
_MAX_BODY = 4000
_MAX_COMMENT = 1000


class OpinionIn(BaseModel):
    ticker: str
    stance: str
    body: str


class CommentIn(BaseModel):
    body: str


class VoteIn(BaseModel):
    value: int   # +1 좋아요 / -1 싫어요 / 0 취소


def _author(user: User) -> str:
    return (user.email or "회원").split("@")[0]


def _comment_dict(c: OpinionComment, uid: int) -> dict:
    return {"id": c.id, "author": c.author, "body": c.body,
            "is_mine": c.user_id == uid,
            "created_at": c.created_at.isoformat() if c.created_at else None}


def _opinion_dict(op: StockOpinion, comments: dict, my_votes: dict, uid: int) -> dict:
    return {
        "id": op.id, "ticker": op.ticker, "author": op.author,
        "stance": op.stance, "body": op.body,
        "likes": op.likes, "dislikes": op.dislikes,
        "my_vote": my_votes.get(op.id, 0),
        "is_mine": op.user_id == uid,
        "created_at": op.created_at.isoformat() if op.created_at else None,
        "comments": [_comment_dict(c, uid) for c in comments.get(op.id, [])],
    }


@router.get("/{ticker}")
def list_opinions(ticker: str, user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    """해당 종목 게시판의 의견 목록(최신순) — 댓글·좋아요/싫어요·내 투표 포함."""
    ops = session.exec(
        select(StockOpinion).where(StockOpinion.ticker == ticker.strip())
        .order_by(StockOpinion.created_at.desc())
    ).all()
    ids = [o.id for o in ops]
    comments: dict = {}
    my_votes: dict = {}
    if ids:
        for c in session.exec(
            select(OpinionComment).where(OpinionComment.opinion_id.in_(ids))
            .order_by(OpinionComment.created_at.asc())
        ).all():
            comments.setdefault(c.opinion_id, []).append(c)
        for v in session.exec(
            select(OpinionVote).where(OpinionVote.opinion_id.in_(ids),
                                      OpinionVote.user_id == user.id)
        ).all():
            my_votes[v.opinion_id] = v.value
    return {"ticker": ticker,
            "opinions": [_opinion_dict(o, comments, my_votes, user.id) for o in ops]}


@router.post("")
def create_opinion(body: OpinionIn, user: User = Depends(get_current_user),
                   session: Session = Depends(get_session)):
    stance = body.stance.strip().lower()
    if stance not in _STANCES:
        raise HTTPException(422, "의견은 매수/중립/매도 중 하나여야 합니다.")
    text = body.body.strip()
    if not text:
        raise HTTPException(422, "분석 내용을 입력하세요.")
    ticker = body.ticker.strip()
    if not ticker:
        raise HTTPException(422, "종목이 필요합니다.")
    op = StockOpinion(ticker=ticker, user_id=user.id, author=_author(user),
                      stance=stance, body=text[:_MAX_BODY])
    session.add(op)
    session.commit()
    session.refresh(op)
    return _opinion_dict(op, {}, {}, user.id)


@router.delete("/{opinion_id}", status_code=204)
def delete_opinion(opinion_id: int, user: User = Depends(get_current_user),
                   session: Session = Depends(get_session)):
    op = session.get(StockOpinion, opinion_id)
    if op is None:
        raise HTTPException(404, "의견을 찾을 수 없습니다.")
    if op.user_id != user.id:
        raise HTTPException(403, "본인 의견만 삭제할 수 있습니다.")
    for c in session.exec(select(OpinionComment).where(
            OpinionComment.opinion_id == opinion_id)).all():
        session.delete(c)
    for v in session.exec(select(OpinionVote).where(
            OpinionVote.opinion_id == opinion_id)).all():
        session.delete(v)
    session.delete(op)
    session.commit()
    return None


@router.post("/{opinion_id}/comments")
def add_comment(opinion_id: int, body: CommentIn,
                user: User = Depends(get_current_user),
                session: Session = Depends(get_session)):
    op = session.get(StockOpinion, opinion_id)
    if op is None:
        raise HTTPException(404, "의견을 찾을 수 없습니다.")
    text = body.body.strip()
    if not text:
        raise HTTPException(422, "댓글 내용을 입력하세요.")
    c = OpinionComment(opinion_id=opinion_id, user_id=user.id,
                       author=_author(user), body=text[:_MAX_COMMENT])
    session.add(c)
    session.commit()
    session.refresh(c)
    return _comment_dict(c, user.id)


@router.delete("/{opinion_id}/comments/{comment_id}", status_code=204)
def delete_comment(opinion_id: int, comment_id: int,
                   user: User = Depends(get_current_user),
                   session: Session = Depends(get_session)):
    c = session.get(OpinionComment, comment_id)
    if c is None or c.opinion_id != opinion_id:
        raise HTTPException(404, "댓글을 찾을 수 없습니다.")
    if c.user_id != user.id:
        raise HTTPException(403, "본인 댓글만 삭제할 수 있습니다.")
    session.delete(c)
    session.commit()
    return None


@router.post("/{opinion_id}/vote")
def vote(opinion_id: int, body: VoteIn, user: User = Depends(get_current_user),
         session: Session = Depends(get_session)):
    op = session.get(StockOpinion, opinion_id)
    if op is None:
        raise HTTPException(404, "의견을 찾을 수 없습니다.")
    val = 1 if body.value > 0 else (-1 if body.value < 0 else 0)
    existing = session.exec(
        select(OpinionVote).where(OpinionVote.opinion_id == opinion_id,
                                  OpinionVote.user_id == user.id)
    ).first()
    if existing is None:
        if val != 0:
            session.add(OpinionVote(opinion_id=opinion_id, user_id=user.id, value=val))
    elif val == 0 or existing.value == val:
        session.delete(existing)   # 같은 버튼 재클릭 = 취소
        val = 0
    else:
        existing.value = val
        session.add(existing)
    session.commit()
    # 카운트 재계산(드리프트 방지) — 의견당 투표 수가 적어 Python 집계로 충분.
    votes = session.exec(select(OpinionVote).where(
        OpinionVote.opinion_id == opinion_id)).all()
    op.likes = sum(1 for v in votes if v.value == 1)
    op.dislikes = sum(1 for v in votes if v.value == -1)
    session.add(op)
    session.commit()
    return {"likes": op.likes, "dislikes": op.dislikes, "my_vote": val}

"""자연어 → StrategyIR 컴파일 라우터 (베타). /ir/compile · /ir/compile/feedback.

백테스트 라우터(routers/ir.py)에서 분리 — NL 컴파일러(LLM API)는 백테스트와 독립한
기능이라 별도 라우터로 둔다(배포·검증 경계 분리: 백테스트 경로는 NL 없이 단독 배포 가능).
같은 prefix /ir 아래 경로만 다르며, main.py가 두 라우터를 함께 include한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Session, select

from ..compile_service import compile_strategy
from ..config import settings
from ..db import get_session
from ..deps import get_current_user
from ..models import CompileLog, User

router = APIRouter(prefix="/ir", tags=["ir"])

_KST = ZoneInfo("Asia/Seoul")


def _kst_day_start_utc() -> datetime:
    """오늘(KST) 자정의 UTC 시각 — 일일 카운트 하한.

    CompileLog.created_at은 UTC로 저장되므로(models._now=datetime.now(utc)),
    KST 자정을 UTC로 환산해 비교한다. '1일'은 사용자가 체감하는 한국 날짜 기준.
    """
    now_kst = datetime.now(_KST)
    midnight_kst = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_kst.astimezone(timezone.utc)


def _compile_quota(session: Session, user_id: int,
                   admin_password: Optional[str]) -> dict:
    """오늘(KST) 사용량·한도·언락 상태를 계산. LLM 호출·기록 없음(읽기 전용).

    admin_password가 config 비밀번호와 일치하면 한도를 ADMIN_LIMIT로 상향한다
    (단일 공유 시크릿 — 베타 오버라이드. 무제한 아님). 카운트는 성공·실패 무관
    모든 변환 시도(= CompileLog row) — 매 시도가 LLM 비용을 발생시키므로.
    """
    unlocked = bool(admin_password) and admin_password == settings.NL_COMPILE_ADMIN_PASSWORD
    limit = settings.NL_COMPILE_ADMIN_LIMIT if unlocked else settings.NL_COMPILE_DAILY_LIMIT
    used = session.exec(
        select(func.count()).select_from(CompileLog)
        .where(CompileLog.user_id == user_id)
        .where(CompileLog.created_at >= _kst_day_start_utc())
    ).one()
    used = int(used or 0)
    return {"used": used, "limit": limit, "remaining": max(0, limit - used),
            "admin_unlocked": unlocked}


class IrCompileIn(BaseModel):
    """자연어 전략 설명 → StrategyIR 컴파일 요청."""
    nl: str
    # admin 언락 비밀번호(선택). 일치 시 일일 한도가 상향된다.
    admin_password: Optional[str] = None


class QuotaIn(BaseModel):
    """일일 사용량 조회 요청 — 비밀번호로 언락 상태도 함께 검증(컴파일 소모 없음)."""
    admin_password: Optional[str] = None


@router.post("/compile/quota")
def ir_compile_quota(body: QuotaIn, user: User = Depends(get_current_user),
                     session: Session = Depends(get_session)):
    """오늘(KST) 사용량·한도·언락 상태. 비번 버튼 즉시 검증 + 카운터 표시용.

    컴파일을 소모하지 않는 읽기 전용 — 페이지 로드 시 카운터, 비밀번호 입력 시
    즉시 언락 검증(틀리면 admin_unlocked=false) 두 용도로 쓰인다.
    """
    return _compile_quota(session, user.id, body.admin_password)


class IrCompileFeedbackIn(BaseModel):
    """컴파일 정확도 신호 — 유저가 컴파일된 IR을 실행했는지·수정했는지."""
    compile_id: int
    ran: bool = True
    edited: Optional[bool] = None


@router.post("/compile")
def ir_compile(body: IrCompileIn, user: User = Depends(get_current_user),
               session: Session = Depends(get_session)):
    """자연어 전략 설명 → StrategyIR. 내부 validate→repair 루프(유저 명료화 없음).

    결과 IR은 빌더가 hydrate해 유저가 확인·실행. 베타 정확도 측정용으로 CompileLog에 기록.

    일일 사용량 제한(인당·KST): 한도 초과면 LLM 호출 없이 차단(rate_limited=true).
    admin 비밀번호로 상향 한도까지 해제. 카운트는 모든 변환 시도(CompileLog row).
    """
    # 사용량 게이트 — LLM 호출 전에 차단해 비용·악용을 막는다(서버 강제: 클라이언트
    # 우회 불가). admin 비밀번호 일치 시 상향 한도 적용.
    quota = _compile_quota(session, user.id, body.admin_password)
    if quota["remaining"] <= 0:
        return {"success": False, "rate_limited": True,
                "ir": {}, "assumptions": [], "issues": [], "explanation": None,
                "error": (f"오늘 자연어 변환 한도({quota['limit']}회)를 모두 사용했습니다. "
                          "내일 다시 시도하거나 관리자 비밀번호로 한도를 해제하세요."),
                "compile_id": None,
                "used": quota["used"], "limit": quota["limit"],
                "remaining": 0, "admin_unlocked": quota["admin_unlocked"]}

    res = compile_strategy(session, user.id, body.nl)

    log = CompileLog(user_id=user.id, nl_input=body.nl, compiled_ir=res.get("ir") or {},
                     assumptions=res.get("assumptions") or [], issues=res.get("issues") or [],
                     repair_count=res.get("repair_count") or 0, ok=bool(res.get("success")))
    session.add(log)
    session.commit()
    session.refresh(log)

    explanation = res.get("explanation")

    # 이번 시도를 포함한 사용량 — used=직전 카운트+1(방금 기록한 log). UI가
    # "오늘 N/M회"를 즉시 갱신하도록 응답에 싣는다.
    used_now = quota["used"] + 1
    return {"success": bool(res.get("success")), "ir": res.get("ir") or {},
            "assumptions": res.get("assumptions") or [], "issues": res.get("issues") or [],
            "explanation": explanation, "error": res.get("error"), "compile_id": log.id,
            "rate_limited": False, "used": used_now, "limit": quota["limit"],
            "remaining": max(0, quota["limit"] - used_now),
            "admin_unlocked": quota["admin_unlocked"]}


@router.post("/compile/feedback")
def ir_compile_feedback(body: IrCompileFeedbackIn, user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)):
    """컴파일 정확도 신호 기록 — 유저가 컴파일된 IR을 수정 없이 실행=정확."""
    log = session.get(CompileLog, body.compile_id)
    if log is None or log.user_id != user.id:
        return {"ok": False}
    log.ran = body.ran
    if body.edited is not None:
        log.edited = body.edited
    session.add(log)
    session.commit()
    return {"ok": True}

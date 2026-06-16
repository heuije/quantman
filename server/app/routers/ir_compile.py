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
from pydantic import BaseModel, ValidationError
from sqlalchemy import func
from sqlmodel import Session, select

import quant_core as qc
from quant_core.blocks import catalog_spec
from quant_core.ir_engine import (StrategyIR, capability_spec, explain_ir,
                                   field_contract, validate_strategy)

from ..config import settings
from ..db import get_session
from ..deps import get_current_user
from ..ir_compiler import compile_nl
from ..models import CompileLog, TradableSymbol, User

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


def _schema_issues(e: ValidationError) -> list[dict]:
    """Pydantic 스키마 오류를 'fixable'한 이슈 목록으로 — 모든 에러를 위치(loc) + 그 자리
    스키마 계약(field_contract)과 함께 반환한다. 첫 에러만/위치 없이 주면 repair 루프가
    어디를 고칠지 몰라 수렴 못 한다(실측: param_grid 항목 모양 오류가 "Field required·
    path=root"로만 와서 LLM이 2회 다 실패). repair 루프가 이 path·message를 그대로 인용한다."""
    issues: list[dict] = []
    for er in e.errors()[:12]:            # 폭주 상한(다축 격자 등은 에러 수십 개 가능)
        loc = er.get("loc", ())
        contract = field_contract(loc[:-1] if loc else ())   # 그 자리 컨테이너 계약
        hint = f" — 올바른 형식: {contract}" if contract else ""
        ctx = er.get("ctx") or {}
        if ctx.get("expected"):           # enum/리터럴 허용값
            hint += f" (허용: {ctx['expected']})"
        issues.append({"rule": "schema", "severity": 30, "is_error": True,
                       "message": f"정의 형식 오류: {er.get('msg', '')}{hint}",
                       "path": ".".join(str(x) for x in loc) or "root"})
    if not issues:
        issues = [{"rule": "schema", "severity": 30, "is_error": True,
                   "message": f"정의 형식 오류: {e}", "path": "root"}]
    return issues


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

    # 심볼 키는 **관리목록(작은 JSON)에서 직접 조립** — 무거운 dataset 캐시(get_symbol_index)에
    # 의존하면 콜드스타트 데이터 갱신과 락/세대 경합으로 컴파일이 지연·블록된다(검증된 실패).
    # 컴파일 검증은 (1) 정적 지표 vocab + (2) 알려진 심볼 키 집합만 필요 — 둘 다 compute·락 불필요.
    from quant_core import data_fetcher as _df
    sym_keys = (set(_df.ALL_SYMBOLS)
                | {s["name"] for s in _df.load_user_stocks()}
                | set(_df.load_managed_kr_codes())
                | {s["code"] for s in _df.load_managed_overseas()})
    valid_refs = (sym_keys | {"Open", "High", "Low", "Close", "Volume"}
                  | set(qc.get_all_indicator_columns()))
    valid_keys = sym_keys
    indicator_cols = sorted(qc.get_all_indicator_columns())
    rows = session.exec(select(TradableSymbol).where(TradableSymbol.user_id == user.id)).all()
    name_map = {r.name.strip().lower(): r.symbol for r in rows if r.name}

    def _validate(strat: dict) -> tuple[list[dict], bool]:
        try:
            s = StrategyIR.model_validate(strat)
        except ValidationError as e:
            return (_schema_issues(e), False)
        out = [{"rule": i.rule, "severity": i.severity, "is_error": i.is_error,
                "message": i.message, "path": i.path}
               for i in validate_strategy(s, valid_refs=valid_refs)]
        return (out, not any(i["is_error"] for i in out))

    res = compile_nl(body.nl, catalog=catalog_spec(), capabilities=capability_spec(),
                     indicator_cols=indicator_cols, valid_keys=valid_keys,
                     name_map=name_map, validate_fn=_validate)

    log = CompileLog(user_id=user.id, nl_input=body.nl, compiled_ir=res.get("ir") or {},
                     assumptions=res.get("assumptions") or [], issues=res.get("issues") or [],
                     repair_count=res.get("repair_count") or 0, ok=bool(res.get("success")))
    session.add(log)
    session.commit()
    session.refresh(log)

    # 백테스트 구성 설명(MECE 버킷 + 산문 요약) — 성공 IR에 한해 생성. 유저에게
    # "어떤 가정·설정으로 백테스트가 구성되는지"를 그대로 노출(특히 silent 기본값).
    explanation = None
    if res.get("success") and res.get("ir"):
        try:
            explanation = explain_ir(StrategyIR.model_validate(res["ir"]),
                                     res.get("assumptions") or [])
        except ValidationError:
            explanation = None

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

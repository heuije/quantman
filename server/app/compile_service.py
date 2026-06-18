"""NL→검증 StrategyIR 공유 진입점 — compile_nl 배선을 단일화한다.

/ir/compile 라우터와 챗봇 도구(simulate/save)가 같은 컴파일 경로를 쓰도록(DRY). 챗봇은 IR을
맨손으로 짓지 않고 이 헬퍼에 위임 → 모델이 IR 스키마를 추측하는 실패 부류가 제거된다.
"""
from __future__ import annotations

from pydantic import ValidationError

import quant_core as qc
from quant_core.ir_engine import (StrategyIR, capability_spec, explain_ir,
                                   field_contract, unknown_field_issues,
                                   validate_strategy)
from quant_core.blocks import catalog_spec

from sqlmodel import Session, select

from .ir_compiler import compile_nl
from .models import TradableSymbol


def _schema_issues(e: ValidationError) -> list[dict]:
    """Pydantic 스키마 오류를 'fixable' 이슈로 — 위치(loc) + 그 자리 스키마 계약(field_contract).
    repair 루프가 path·message를 인용해 수렴한다(허용 필드를 알려줘야 LLM이 고친다)."""
    issues: list[dict] = []
    for er in e.errors()[:12]:
        loc = er.get("loc", ())
        contract = field_contract(loc[:-1] if loc else ())
        hint = f" — 올바른 형식: {contract}" if contract else ""
        ctx = er.get("ctx") or {}
        if ctx.get("expected"):
            hint += f" (허용: {ctx['expected']})"
        issues.append({"rule": "schema", "severity": 30, "is_error": True,
                       "message": f"정의 형식 오류: {er.get('msg', '')}{hint}",
                       "path": ".".join(str(x) for x in loc) or "root"})
    if not issues:
        issues = [{"rule": "schema", "severity": 30, "is_error": True,
                   "message": f"정의 형식 오류: {e}", "path": "root"}]
    return issues


def compile_strategy(session: Session, user_id: int | None, nl: str) -> dict:
    """자연어 전략 서술 → 검증된 StrategyIR. compile_nl(Haiku) 내부 수리 루프.

    반환: {success, ir, assumptions, issues, repair_count, error?, explanation}. explanation은
    성공 시 explain_ir(MECE 버킷) — 챗봇이 "이렇게 해석했어요"로 유저에게 노출한다.
    """
    # 심볼 키는 관리목록(작은 JSON)에서 직접 조립 — 무거운 dataset 캐시에 의존하면
    # 콜드스타트 데이터 갱신과 락/세대 경합으로 컴파일이 지연·블록된다(검증된 실패).
    from quant_core import data_fetcher as _df
    sym_keys = (set(_df.ALL_SYMBOLS)
                | {s["name"] for s in _df.load_user_stocks()}
                | set(_df.load_managed_kr_codes())
                | {s["code"] for s in _df.load_managed_overseas()})
    valid_refs = (sym_keys | {"Open", "High", "Low", "Close", "Volume"}
                  | set(qc.get_all_indicator_columns()))
    valid_keys = sym_keys
    indicator_cols = sorted(qc.get_all_indicator_columns())
    rows = session.exec(select(TradableSymbol).where(TradableSymbol.user_id == user_id)).all()
    name_map = {r.name.strip().lower(): r.symbol for r in rows if r.name}

    def _validate(strat: dict) -> tuple[list[dict], bool]:
        # extra="ignore" 모델이 환각/오타 필드를 silent drop 하기 전에 raw에서 포착 → repair 피드백.
        # (라이브 결함: commission_pct/transaction_cost_pct 가 버려져 유저 지정 비용이 무시됐다.)
        unknown = unknown_field_issues(strat)
        try:
            s = StrategyIR.model_validate(strat)
        except ValidationError as e:
            return (unknown + _schema_issues(e), False)
        out = unknown + [{"rule": i.rule, "severity": i.severity, "is_error": i.is_error,
                          "message": i.message, "path": i.path}
                         for i in validate_strategy(s, valid_refs=valid_refs)]
        return (out, not any(i["is_error"] for i in out))

    res = compile_nl(nl, catalog=catalog_spec(), capabilities=capability_spec(),
                     indicator_cols=indicator_cols, valid_keys=valid_keys,
                     name_map=name_map, validate_fn=_validate)

    explanation = None
    if res.get("success") and res.get("ir"):
        try:
            explanation = explain_ir(StrategyIR.model_validate(res["ir"]),
                                     res.get("assumptions") or [])
        except ValidationError:
            # res["ir"]는 compile_nl 내부 _validate를 이미 통과했으나, 재검증과의 이론적
            # 스키마 불일치 시 explanation만 None으로 내린다(표시용 — 없어도 컴파일 결과는 온전).
            explanation = None
    return {**res, "explanation": explanation}

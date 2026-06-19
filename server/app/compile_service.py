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


# %-단위 수익률류 지표 — 임계가 |const|<0.01이면 '0.1%→0.001' 식 ÷100(분수화) 오류로 보고
# ×100 교정. 프롬프트가 '−0.1로 쓰라'고 명시해도 LLM이 비결정적으로 −0.001을 내므로(이미 인입된
# nl까지), 결정적 후보정으로 부류를 닫는다. <0.01 한정이라 정상 임계(0.05%=0.05 등)는 불변.
_PCT_RETURN_INDS = {"pct_change_1d", "pct_change_5d", "pct_change_20d", "pct_change_252d",
                    "log_return_1d", "momentum_12_1m"}


def _normalize_pct_thresholds(node, warns: list | None = None) -> list:
    """compare(%수익률 지표, const)에서 |const|<0.01(÷100 오류)을 ×100 결정적 교정. 경고 목록 반환."""
    if warns is None:
        warns = []
    if not isinstance(node, dict):
        return warns
    if node.get("op") == "compare":
        left = (node.get("inputs") or {}).get("left") or {}
        right = (node.get("inputs") or {}).get("right") or {}
        ref = (left.get("params") or {}).get("ref", "") if left.get("op") == "data" else ""
        if ref.split(".")[-1] in _PCT_RETURN_INDS and right.get("op") == "const":
            v = (right.get("params") or {}).get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < abs(v) < 0.01:
                right["params"]["value"] = v * 100
                warns.append(f"임계값 {v}→{v * 100} 교정 — {ref.split('.')[-1]}는 % 단위(0.1%는 0.1, 분수 0.001 아님)")
    for child in (node.get("inputs") or {}).values():
        _normalize_pct_thresholds(child, warns)
    return warns


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

    # 후보정: %수익률 임계 단위 오류(−0.001 등) 결정적 교정 — LLM 비결정 보정(별개 A 부류).
    if res.get("success") and isinstance(res.get("ir"), dict):
        unit_warns = _normalize_pct_thresholds(res["ir"].get("signal"))
        if unit_warns:
            res["assumptions"] = list(res.get("assumptions") or []) + unit_warns

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

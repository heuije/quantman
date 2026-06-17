"""전략 연구소 챗봇 도구 — Anthropic tool 스키마 + IR 조립 + 엔진 디스패치 + compact 요약.

도구는 엔진의 동사(query)를 그대로 노출한다(chat_lab_spec D2). 서버가 도구 입력을
StrategyIR로 조립해 단일 엔진 진입점 strategy_from_spec로 실행한다(검증·valid_refs 자동).
"""
from __future__ import annotations

import quant_core as qc
from pydantic import ValidationError
from quant_core.ir_engine import (StrategyIR, needed_columns, needed_symbols,
                                   strategy_from_spec)

# ── 도구 스키마 ──────────────────────────────────────────────────────────────

SCREEN_TOOL = {
    "name": "screen",
    "description": ("팩터 점수로 종목을 횡단 랭킹해 상위 종목을 선별(스크리닝). "
                    "백테스트가 아니라 현 시점(as-of) 스냅샷. score_ref·top_n 필요."),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {"type": "array", "items": {"type": "string"},
                        "description": "후보 종목 코드. 비우면 전체 유니버스."},
            "score_ref": {"type": "string",
                          "description": "랭킹 기준 지표 ref (예: __SELF__.pb_ratio, momentum_12_1m)."},
            "top_n": {"type": "integer", "description": "상위 N 종목."},
            "descending": {"type": "boolean",
                           "description": "점수 큰 순(true·기본) 또는 작은 순(false, 예: 저PBR)."},
            "display": {"type": "array", "items": {"type": "string"},
                        "description": "결과에 함께 표시할 지표 컬럼."},
        },
        "required": ["score_ref", "top_n"],
    },
}

SIMULATE_TOOL = {
    "name": "simulate",
    "description": ("완전한 매매전략(StrategyIR)을 과거 데이터로 백테스트. 저장 가능한 전략 산출물. "
                    "추상적 의도는 먼저 구체 정의로 협의한 뒤 호출."),
    "input_schema": {
        "type": "object",
        "properties": {
            "strategy": {"type": "object",
                         "description": ("완전한 StrategyIR JSON(universe/signal/position/simulation 등). "
                                         "signal은 필수.")},
        },
        "required": ["strategy"],
    },
}

SAVE_STRATEGY_TOOL = {
    "name": "save_strategy",
    "description": ("합의된 매매전략(StrategyIR)을 사용자의 전략 목록에 draft(초안)로 저장한다. "
                    "저장만 하며 모의/실전 실행은 웹 '트레이딩(자동매매)' 메뉴에서 사용자가 직접 한다. "
                    "사용자가 명시적으로 저장을 원하고, 앞서 simulate로 합의된 전략이 있을 때만 호출. "
                    "ir에는 simulate에 넘긴 것과 동일한 StrategyIR 객체를 그대로 넣는다."),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "전략 이름(사용자에게 보일 이름)."},
            "ir": {"type": "object",
                   "description": "저장할 StrategyIR — simulate에 넘긴 것과 동일 구조."},
        },
        "required": ["name", "ir"],
    },
}

TOOL_SCHEMAS = [SCREEN_TOOL, SIMULATE_TOOL, SAVE_STRATEGY_TOOL]


# ── IR 조립 ──────────────────────────────────────────────────────────────────

def assemble_ir(tool_name: str, tool_input: dict) -> dict:
    """도구 입력 → StrategyIR dict. screen은 부분집합→select IR, simulate는 full IR 통과."""
    if tool_name == "screen":
        symbols = list(tool_input.get("symbols") or [])
        universe = {"kind": "list", "symbols": symbols} if symbols else {"kind": "all"}
        return {
            "universe": universe,
            "signal": {"op": "data", "params": {"ref": tool_input["score_ref"]}},
            "query": "select",
            "select": {"top_n": int(tool_input["top_n"]),
                       "descending": bool(tool_input.get("descending", True)),
                       "display": list(tool_input.get("display") or [])},
        }
    if tool_name == "simulate":
        return dict(tool_input.get("strategy") or {})
    raise ValueError(f"알 수 없는 도구: {tool_name}")


# ── 데이터셋 로드 ─────────────────────────────────────────────────────────────

def _load_dataset(ir: dict) -> dict:
    """IR이 참조하는 데이터셋 로드 — ir.py /ir/strategy와 동일 전략.

    · single/list: 필요 종목만(load_dataset_for). 종목 수가 적어 빠름.
    · all/screener: 전 종목 × 참조 컬럼만(get_projected, 컬럼 프로젝션).
      SELECT(as-of)는 최근 구간(~400행)만 — 전체이력×전종목 프로젝션 OOM 회피.
    · 파싱 실패: 빈 dict → strategy_from_spec가 단일 검증경로에서 오류를 돌려준다
      (여기선 scope 계산만 — 근본 검증은 엔진 진입점이 소유, 증상 봉합 아님).
    """
    from .. import data_cache
    try:
        sir = StrategyIR.model_validate(ir)
    except ValidationError:
        # 파싱 불가 IR → {} 반환, strategy_from_spec 단일 검증경로에서 오류 돌려줌.
        return {}
    needed = needed_symbols(sir)
    if needed is not None:
        return qc.load_dataset_for(needed)
    cols = needed_columns(sir)
    if cols is not None:
        return data_cache.get_projected(cols, symbols=None,
                                        recent_days=400 if sir.query == "select" else None)
    from ..data_cache import get_dataset
    return get_dataset()


# ── 도구 실행 ─────────────────────────────────────────────────────────────────

def run_tool(tool_name: str, tool_input: dict) -> dict:
    """도구 호출 → IR 조립 → 데이터셋 로드 → 엔진 실행. full 결과 dict 반환.

    조립 실패는 예외 대신 {success:False,error}로 — agent 루프가 tool_result로 모델에 피드백.
    """
    try:
        ir = assemble_ir(tool_name, tool_input)
    except (ValueError, KeyError, TypeError) as e:
        return {"success": False, "error": f"도구 입력 오류({tool_name}): {e}"}
    dataset = _load_dataset(ir)
    return strategy_from_spec(ir, dataset)   # valid_refs=None → 엔진이 available_refs 도출


def save_strategy_tool(session, user_id, tool_input: dict) -> dict:
    """합의된 IR을 draft 전략으로 저장 — side-effect 도구라 순수 run_tool과 분리(루프가 세션·소유자 주입).

    name 인자를 IR.name에 주입하고 strategies.save_ir_draft로 검증·저장한다. 검증/저장 실패는
    예외 대신 {success:False,error}로 — agent 루프가 tool_result로 모델에 피드백(고아 방지).
    """
    from fastapi import HTTPException
    from ..routers.strategies import save_ir_draft
    name = (tool_input.get("name") or "").strip()
    ir = dict(tool_input.get("ir") or {})
    if name:
        ir["name"] = name      # IR.name이 전략명 — name 인자를 주입(엔진이 s.name을 씀)
    try:
        row = save_ir_draft(session, user_id, ir)
    except HTTPException as e:
        return {"success": False, "error": str(e.detail)}
    except Exception as e:  # noqa: BLE001 — 저장 실패를 모델 피드백으로 표면화
        return {"success": False, "error": f"전략 저장 실패: {e}"}
    return {"success": True, "strategy_id": row.id, "name": row.name, "run_mode": "draft"}


# ── compact 요약 ──────────────────────────────────────────────────────────────

def compact_summary(tool_name: str, result: dict) -> str:
    """full 엔진 결과 → 모델 컨텍스트용 짧은 요약. 숫자는 결과에서만(지어내기 금지)."""
    if not result.get("success"):
        return f"[{tool_name} 실패] {result.get('error', '알 수 없는 오류')}"
    if tool_name == "save_strategy":
        return (f"[save_strategy] '{result.get('name')}' 전략을 draft로 저장(id={result.get('strategy_id')}). "
                "모의/실전은 웹 자동매매 메뉴에서.")
    if tool_name == "screen":
        rows = result.get("results") or []

        def _one(r):
            sc = r.get("score")
            return f"{r['symbol']}({sc:.3g})" if sc is not None else str(r["symbol"])

        top = ", ".join(_one(r) for r in rows[:8])
        return (f"[screen] as_of={result.get('as_of')}, 후보 {result.get('universe_size')}개 중 "
                f"{len(rows)}개 선별. 상위: {top}")
    if tool_name == "simulate":
        m = result.get("metrics") or {}
        parts = [f"{k}={m[k]:.3g}" for k in ("cagr", "sharpe", "mdd", "cum_return")
                 if isinstance(m.get(k), (int, float))]
        return "[simulate] " + (", ".join(parts) if parts else "결과 산출")
    return f"[{tool_name}] 완료"

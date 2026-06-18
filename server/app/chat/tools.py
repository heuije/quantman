"""전략 연구소 챗봇 도구 — Anthropic tool 스키마 + IR 조립 + 엔진 디스패치 + compact 요약.

도구는 엔진의 동사(query)를 그대로 노출한다(chat_lab_spec D2). 서버가 도구 입력을
StrategyIR로 조립해 단일 엔진 진입점 strategy_from_spec로 실행한다(검증·valid_refs 자동).
simulate/save_strategy는 NL을 compile_strategy에 위임해 모델이 IR을 직접 추측하지 않는다.
"""
from __future__ import annotations

import quant_core as qc
from pydantic import ValidationError
from quant_core.ir_engine import (StrategyIR, needed_columns, needed_symbols,
                                   strategy_from_spec)

from ..compile_service import compile_strategy
from ..models import Message
from ..routers.strategies import save_ir_draft

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
            "sector": {"type": "string",
                       "description": "업종/섹터명으로 후보를 거름(예: 반도체). symbols 대신 사용."},
        },
        "required": ["score_ref", "top_n"],
    },
}

SIMULATE_TOOL = {
    "name": "simulate",
    "description": ("자연어 전략·분석 서술을 검증된 IR로 컴파일해 실행하는 **범용 분석 도구**. "
                    "백테스트(손익)뿐 아니라 파라미터 민감도(sweep)·최적값 탐색(extremize)·"
                    "연도별/기간 분할·국면별 대조·팩터 회귀/IC·이벤트 스터디·포트폴리오 진단을 "
                    "모두 nl 서술로 수행한다(IR JSON을 직접 짓지 말 것). 어떤 분석인지 의도를 nl에 "
                    "명시. 추상적 의도는 먼저 구체 정의로 협의한 뒤 호출."),
    "input_schema": {
        "type": "object",
        "properties": {
            "nl": {"type": "string",
                   "description": ("분석할 전략·질문의 완결된 자연어 서술 — 유니버스·신호조건·"
                                   "방향(롱/숏)·진입/청산·기간/비용 + (해당 시) 분할축(연도별·국면별)·"
                                   "최적화 목표·비교 대상·회귀 팩터 등을 한 문단으로.")},
        },
        "required": ["nl"],
    },
}

SAVE_STRATEGY_TOOL = {
    "name": "save_strategy",
    "description": ("합의된 전략을 사용자 전략 목록에 draft로 저장. 사용자가 명시적으로 '저장'을 원하고 "
                    "앞서 simulate로 백테스트한 전략이 있을 때 호출(그 전략을 그대로 저장한다). "
                    "저장만 하며 모의/실전은 웹 자동매매 메뉴에서 사용자가 한다."),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "전략 이름."},
            "nl": {"type": "string",
                   "description": "(직전 simulate가 없을 때만) 저장할 전략의 자연어 서술."},
        },
        "required": ["name"],
    },
}

DESCRIBE_TOOL = {
    "name": "describe",
    "description": ("단일 종목의 종합 리포트(360) — 가격·52주 레인지·기간수익(1·3·6·12개월)·변동성·"
                    "최대낙폭·밸류에이션(PBR/PER/EV-EBITDA)·뉴스 헤드라인. '○○ 어때?' 같은 "
                    "단일종목 요약 질문에 사용. symbol(코드)만 필요."),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "종목 코드(예: 005930=삼성전자)."},
        },
        "required": ["symbol"],
    },
}

INSPECT_TOOL = {
    "name": "inspect",
    "description": ("단일 종목의 특정 지표 원시 시계열을 최근 N일 조회(집계가 아니라 raw 값). "
                    "예: 목표주가 흐름·종가 추이·괴리율. 차트/표로 그대로 보여준다. symbol·columns 필요 "
                    "(목표주가=consensus_target, 종가=Close, 상승여력=target_upside, RSI=rsi_14 등)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "종목 코드(예: 005930)."},
            "columns": {"type": "array", "items": {"type": "string"},
                        "description": "조회할 컬럼명(예: ['consensus_target','Close'])."},
            "window": {"type": "integer", "description": "최근 거래일 수(기본 120)."},
        },
        "required": ["symbol", "columns"],
    },
}

TOOL_SCHEMAS = [SCREEN_TOOL, SIMULATE_TOOL, SAVE_STRATEGY_TOOL, DESCRIBE_TOOL, INSPECT_TOOL]


# ── IR 조립 ──────────────────────────────────────────────────────────────────

def assemble_ir(tool_name: str, tool_input: dict) -> dict:
    """도구 입력 → StrategyIR dict. screen은 부분집합→select IR, describe는 단일종목 360 IR."""
    if tool_name == "screen":
        sector = str(tool_input.get("sector") or "").strip()
        symbols = list(tool_input.get("symbols") or [])
        if sector:
            # 모델이 종목 universe를 추측하지 않도록 — 섹터를 screener(부분일치)로 결정적 빌드.
            # contains: 분류 데이터가 KSIC 자유서술("반도체 제조업")이라 정확매칭은 0건.
            # attribute(Industry)는 종목 정적 분류 라벨 — projected 컬럼이 아니라 엔진이 심볼
            # 메타데이터(classification)로 해석한다(needed_columns 미추출은 정상, 결손 아님).
            universe = {"kind": "all", "screener": {"condition": {
                "op": "is_in",
                "inputs": {"signal": {"op": "attribute", "params": {"attr": "Industry"}}},
                "params": {"values": [sector], "match": "contains"}}}}
        elif symbols:
            universe = {"kind": "list", "symbols": symbols}
        else:
            universe = {"kind": "all"}
        return {
            "universe": universe,
            "signal": {"op": "data", "params": {"ref": tool_input["score_ref"]}},
            "query": "select",
            "select": {"top_n": int(tool_input["top_n"]),
                       "descending": bool(tool_input.get("descending", True)),
                       "display": list(tool_input.get("display") or [])},
        }
    # simulate는 run_simulate가 compile_strategy로 IR을 만든다(assemble 불필요).
    if tool_name == "describe":
        # 단일종목 360 리포트. signal은 리포트가 미사용하나 StrategyIR 스키마 충족용 placeholder.
        return {
            "universe": {"kind": "single", "symbols": [str(tool_input["symbol"])]},
            "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}},
            "query": "describe",
        }
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

def run_simulate(session, user_id, tool_input: dict) -> dict:
    """전략 NL → compile_strategy(검증 IR) → 백테스트. 모델이 IR을 추측하지 않는다(부류 제거).

    실패(컴파일 불가)는 예외 대신 {success:False,error}로 — agent 루프가 graceful 전달.
    성공 시 검증 IR·explanation을 결과에 동봉(저장 재사용·유저 표시).
    """
    nl = str(tool_input.get("nl") or "").strip()
    if not nl:
        return {"success": False, "error": "simulate: 전략 서술(nl)이 필요합니다."}
    comp = compile_strategy(session, user_id, nl)
    if not comp.get("success"):
        return {"success": False, "error": comp.get("error") or "전략을 IR로 컴파일하지 못했습니다."}
    ir = comp["ir"]
    dataset = _load_dataset(ir)
    res = strategy_from_spec(ir, dataset)
    if isinstance(res, dict) and res.get("success"):
        res["ir"] = ir
        res["explanation"] = comp.get("explanation")
        res["assumptions"] = comp.get("assumptions") or []
    return res


def run_inspect(tool_input: dict) -> dict:
    """단일 종목의 원시 컬럼 시계열을 직접 조회(retrieval). 집계 동사(describe/select)로는 못 주는
    단일종목 raw 시계열을 위해 데이터셋을 직접 슬라이스 — 엔진 query 동사가 아닌 데이터 dump."""
    import pandas as pd
    symbol = str(tool_input.get("symbol") or "").strip()
    columns = [str(c) for c in (tool_input.get("columns") or [])]
    window = int(tool_input.get("window") or 120)
    if not symbol or not columns:
        return {"success": False, "error": "inspect: symbol·columns가 필요합니다."}
    df = qc.load_dataset_for([symbol]).get(symbol)
    if df is None or len(df) == 0:
        return {"success": False, "error": f"데이터가 없습니다: {symbol}"}
    have = [c for c in columns if c in df.columns]
    if not have:
        avail = ", ".join(list(df.columns)[:40])   # 앞 40개면 모델 자가수정에 충분(넓은 DF 덤프 방지)
        return {"success": False,
                "error": f"해당 컬럼이 없습니다: {', '.join(columns)}. 사용 가능한 컬럼(일부): {avail}"}
    sub = df[have].tail(window)
    dates = [d.strftime("%Y-%m-%d") for d in sub.index]
    series = {}
    for c in have:
        col = pd.to_numeric(sub[c], errors="coerce")     # 비수치 컬럼은 None으로(라인차트 안전)
        series[c] = [None if pd.isna(v) else float(v) for v in col]
    return {"success": True, "query": "inspect", "symbol": symbol,
            "columns": have, "dates": dates, "series": series}


def run_tool(tool_name: str, tool_input: dict) -> dict:
    """도구 호출 → IR 조립 → 데이터셋 로드 → 엔진 실행. full 결과 dict 반환.

    inspect는 엔진 동사가 아니라 데이터 retrieval이라 별도 경로. 조립 실패는 예외 대신
    {success:False,error}로 — agent 루프가 tool_result로 모델에 피드백.
    """
    if tool_name == "inspect":
        return run_inspect(tool_input)
    try:
        ir = assemble_ir(tool_name, tool_input)
    except (ValueError, KeyError, TypeError) as e:
        return {"success": False, "error": f"도구 입력 오류({tool_name}): {e}"}
    dataset = _load_dataset(ir)
    return strategy_from_spec(ir, dataset)   # valid_refs=None → 엔진이 available_refs 도출


def _last_simulate_ir(session, conversation_id) -> dict | None:
    """대화의 마지막 성공 simulate tool_result에 동봉된 검증 IR — 재컴파일 회피(토큰 절감)."""
    if session is None or conversation_id is None:
        return None
    from sqlmodel import select
    msgs = session.exec(select(Message).where(Message.conversation_id == conversation_id)
                        .order_by(Message.id.desc())).all()
    for m in msgs:
        if m.role != "assistant":
            continue
        for p in reversed(m.parts or []):
            if p.get("type") == "tool_result" and p.get("name") == "simulate":
                ir = (p.get("result") or {}).get("ir")
                if ir:
                    return dict(ir)
    return None


def save_strategy_tool(session, user_id, conversation_id, tool_input: dict) -> dict:
    """합의된 전략을 draft 저장. **마지막 simulate의 검증 IR을 재사용**(재컴파일 0); 없으면 nl로 컴파일.

    검증/저장 실패는 예외 대신 {success:False,error}로 — agent 루프가 모델에 피드백(고아 방지).
    """
    from fastapi import HTTPException
    name = (tool_input.get("name") or "").strip()
    ir = _last_simulate_ir(session, conversation_id)
    if ir is None:
        nl = str(tool_input.get("nl") or "").strip()
        if not nl:
            return {"success": False, "error": "저장할 전략이 없습니다. 먼저 simulate로 백테스트하거나 전략을 서술해 주세요."}
        comp = compile_strategy(session, user_id, nl)
        if not comp.get("success"):
            return {"success": False, "error": comp.get("error") or "전략 컴파일 실패"}
        ir = comp["ir"]
    if name:
        ir["name"] = name
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
    if tool_name == "describe":
        pr = result.get("price") or {}
        f = result.get("fundamentals") or {}
        return (f"[describe] {result.get('symbol')}({result.get('sector')}) "
                f"종가={pr.get('last')}, PBR={f.get('pb_ratio')}, PER={f.get('trailing_pe')}, "
                f"EV/EBITDA={f.get('ev_ebitda')}")
    if tool_name == "inspect":
        cols = result.get("columns") or []
        dates = result.get("dates") or []
        series = result.get("series") or {}
        last = []
        for c in cols:
            vals = [v for v in (series.get(c) or []) if v is not None]
            if vals:
                last.append(f"{c}={vals[-1]:.6g}")
        rng = f"{dates[0]}~{dates[-1]}" if dates else "?"
        return (f"[inspect] {result.get('symbol')} {rng} ({len(dates)}일). "
                f"최근값: {', '.join(last) if last else '없음'}")
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

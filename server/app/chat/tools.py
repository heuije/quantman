"""전략 연구소 챗봇 도구 — Anthropic tool 스키마 + IR 조립 + 엔진 디스패치 + compact 요약.

도구는 엔진의 동사(query)를 그대로 노출한다(chat_lab_spec D2). 서버가 도구 입력을
StrategyIR로 조립해 단일 엔진 진입점 strategy_from_spec로 실행한다(검증·valid_refs 자동).
simulate/save_strategy는 NL을 compile_strategy에 위임해 모델이 IR을 직접 추측하지 않는다.
"""
from __future__ import annotations

import copy

import quant_core as qc
from pydantic import ValidationError
from quant_core.ir_engine import (StrategyIR, needed_columns, needed_symbols,
                                   param_manifest, strategy_from_spec, summarize_result)

from ..compile_service import compile_strategy
from ..models import Message
from ..routers.strategies import save_ir_draft
from ..serialize import serialize_ir_result
from ..data_manifest import build_dataset_manifest

# ── 도구 스키마 ──────────────────────────────────────────────────────────────

SCREEN_TOOL = {
    "name": "screen",
    "description": ("팩터 점수로 종목을 횡단 랭킹해 상위 종목을 선별(현 시점 as-of 스냅샷). "
                    "**저평가**=여러 밸류 지표를 score_refs로(백분위 합 composite·낮을수록 저평가·"
                    "단일 raw 정렬 금지). **섹터별 N개**=group_by. **여러 섹터**=sectors. top_n 필요."),
    "input_schema": {
        "type": "object",
        "properties": {
            "symbols": {"type": "array", "items": {"type": "string"},
                        "description": "후보 종목 코드. 비우면 전체 유니버스."},
            "score_ref": {"type": "string",
                          "description": "단일 랭킹 지표 ref (예: momentum_12_1m). 복합은 score_refs."},
            "score_refs": {"type": "array", "items": {"type": "string"},
                           "description": ("복합 저평가 점수용 밸류 지표 ref 목록 — 백분위 합(낮을수록 저평가). "
                                           "예: ['__SELF__.pb_ratio','__SELF__.trailing_pe']. "
                                           "낮을수록 저평가인 밸류 지표만(혼합 방향 금지). "
                                           "**ev_ebitda는 저평가 점수에 넣지 말 것**(왜곡 — 사용자 합의로 제외).")},
            "top_n": {"type": "integer", "description": "상위 N 종목(group_by 시 그룹당 N)."},
            "descending": {"type": "boolean",
                           "description": "단일 score_ref일 때만. 큰 순(true·기본)/작은 순(false). composite는 자동."},
            "display": {"type": "array", "items": {"type": "string"},
                        "description": "결과에 함께 표시할 지표 컬럼(composite 팩터는 자동 포함)."},
            "sector": {"type": "string", "description": "단일 섹터(예: 반도체). 여러 개는 sectors."},
            "sectors": {"type": "array", "items": {"type": "string"},
                        "description": ("여러 섹터(예: ['반도체','2차전지']). 표준 테마명 권장 — "
                                        "'배터리'·'바이오' 등 흔한 표현도 서버가 자동 정규화(유효 테마는 시스템 프롬프트).")},
            "group_by": {"type": "string",
                         "description": ("섹터별 N개씩 선별 = group_by='Sector'를 sectors·top_n과 함께 "
                                         "**한 번에** 호출(섹터마다 screen 반복 호출 금지). "
                                         "예: sectors=['반도체','배터리']+group_by='Sector'+top_n=3 → 각 섹터 3종목.")},
        },
        "required": ["top_n"],
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
                    "최대낙폭·밸류에이션(PBR/PER/EV-EBITDA)·뉴스 헤드라인·추정실적(다음 회계연도 추정 "
                    "매출·영업이익·EPS·forward PER, KR). '○○ 어때?'·'전망/추정실적' 같은 "
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
            "window": {"type": "integer", "description": "조회할 최근 거래일 수(기본 120). "
                       "**과거 장기 추이·여러 해를 보려면 크게 줄 것** — 1년≈250·3년≈750·5년≈1250·10년≈2500. "
                       "'과거 PER 추이'처럼 장기 흐름이면 250 이상."},
        },
        "required": ["symbol", "columns"],
    },
}

ADJUST_TOOL = {
    "name": "adjust_analysis",
    "description": ("직전 simulate 분석의 **변수 값만** 바꿔 재실행한다(재컴파일 없이·토큰 0). "
                    "비용·기간·top_n·보유기간·임계값 등 '마지막 분석의 파라미터 조정'일 때만 — "
                    "새 전략/다른 신호/다른 종목은 simulate(nl)로. changes의 path는 직전 결과 "
                    "adjustable(매니페스트)의 경로(예: simulation.commission)."),
    "input_schema": {
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "path": {"type": "string",
                             "description": "조정 경로(예: simulation.commission, position.exit.hold_days)."},
                    "value": {"description": "새 값(숫자·문자·불리언)."}},
                    "required": ["path", "value"]},
                "description": "바꿀 필드 목록.",
            },
        },
        "required": ["changes"],
    },
}

RESEARCH_NEWS_TOOL = {
    "name": "research_news",
    "description": ("뉴스로 답해야 하는 질문(최근 이슈·왜 올랐나/빠졌나·특정 시점 사건·시장/매크로 동향)에 쓴다. "
                    "queries에 엔티티+관련 매크로/섹터 키워드를, period에 기간을 네가 판단해 넣으면 최근=네이버·"
                    "과거=GDELT로 수집해 본문까지 읽고 증거 다이제스트(인용 포함)를 돌려준다. 단순 '○○ 어때'는 "
                    "describe(헤드라인 자동)로 충분 — 심층·기간·매크로·본문이 필요할 때 이 도구."),
    "input_schema": {
        "type": "object",
        "properties": {
            "queries": {"type": "array", "items": {"type": "string"},
                        "description": "엔티티 + 관련 매크로/섹터 키워드(2~4). 예: ['삼성전자','반도체 업황','D램 가격']."},
            "period": {"type": "object",
                       "description": "{kind:'recent',days:N} 또는 {kind:'range',start:'YYYY-MM-DD',end:'YYYY-MM-DD'}."},
            "max_articles": {"type": "integer", "description": "최대 기사 수(기본 8)."},
            "depth": {"type": "string", "enum": ["headlines", "full"],
                      "description": "full=본문+다이제스트(기본) / headlines=빠른 헤드라인만."},
        },
        "required": ["queries", "period"],
    },
}

TOOL_SCHEMAS = [SCREEN_TOOL, SIMULATE_TOOL, SAVE_STRATEGY_TOOL, DESCRIBE_TOOL, INSPECT_TOOL, ADJUST_TOOL,
                RESEARCH_NEWS_TOOL]


# ── IR 조립 ──────────────────────────────────────────────────────────────────

# 밸류에이션 멀티플 — 분모(이익·EBITDA·자본)가 0 이하면 멀티플이 음수가 돼 '저평가' 신호로
# 무의미하다(적자기업은 싼 게 아니라 해당없음). 저평가 스크린은 오름차순 정렬이라 이런 음수
# 종목이 '가장 싼' 1위로 오선별된다 — 자격에서 제외해 근본 차단(희제 라이브 발견).
_POSITIVE_VALUATION_COLS = frozenset({
    "trailing_pe", "forward_pe", "ev_ebitda", "ev_sales", "pb_ratio", "peg"})


def _gt0(ref: str) -> dict:
    """밸류 멀티플 ref > 0 자격 조건(compare leaf)."""
    return {"op": "compare", "params": {"op": ">"},
            "inputs": {"left": {"op": "data", "params": {"ref": ref}},
                       "right": {"op": "const", "params": {"value": 0.0}}}}


def _and_conds(conds: list) -> dict:
    """조건 1개면 그대로, 여러 개면 logic AND로 결합."""
    return conds[0] if len(conds) == 1 else {
        "op": "logic", "params": {"logic": "AND"},
        "inputs": {str(i): c for i, c in enumerate(conds)}}


def assemble_ir(tool_name: str, tool_input: dict) -> dict:
    """도구 입력 → StrategyIR dict. screen은 부분집합→select IR, describe는 단일종목 360 IR."""
    if tool_name == "screen":
        symbols = list(tool_input.get("symbols") or [])
        from quant_core.data.feeds.classification import sector_match_values
        sectors = [str(s).strip() for s in (tool_input.get("sectors")
                   or ([tool_input["sector"]] if tool_input.get("sector") else [])) if str(s).strip()]
        # 점수: 밸류 지표 여러 개면 백분위 합 composite(낮을수록 저평가·산식 투명), 1개면 raw.
        refs = [r for r in (tool_input.get("score_refs")
                or ([tool_input["score_ref"]] if tool_input.get("score_ref") else [])) if r]
        if not refs:
            raise KeyError("score_ref/score_refs")
        if len(refs) > 1:
            def _rank(ref):     # 횡단 백분위(오름차순 — 낮은 값=낮은 분위=저평가)
                return {"op": "rank", "params": {"unit": "pct", "descending": False},
                        "inputs": {"signal": {"op": "data", "params": {"ref": ref}}}}
            signal = _rank(refs[0])
            for ref in refs[1:]:
                signal = {"op": "binary", "params": {"op": "+"},
                          "inputs": {"a": signal, "b": _rank(ref)}}
            descending = False                      # 백분위 합 낮을수록 저평가
        else:
            signal = {"op": "data", "params": {"ref": refs[0]}}
            descending = bool(tool_input.get("descending", True))
        # 자격필터: 밸류 멀티플 score_ref는 >0만(적자·음수 자본 제외) → 음수가 '최저평가'로
        # 오선별되는 것 차단. 섹터 필터(is_in Industry)와 AND 결합. 사용자 섹터어(배터리 등)는
        # sector_match_values로 KSIC+GICS 업종 정규화·확장(테마명≠업종 어휘로 인한 빈 결과 방지).
        val_conds = [_gt0(r) for r in refs if r.split(".")[-1] in _POSITIVE_VALUATION_COLS]
        if sectors:
            match_values = sector_match_values(sectors) or sectors
            sector_cond = {"op": "is_in",
                           "inputs": {"signal": {"op": "attribute", "params": {"attr": "Industry"}}},
                           "params": {"values": match_values, "match": "contains"}}
            universe = {"kind": "all", "screener": {"condition": _and_conds([sector_cond] + val_conds)}}
        elif symbols:
            universe = {"kind": "list", "symbols": symbols}   # 명시 종목은 사용자 선택 존중(자격필터 미적용)
        elif val_conds:
            universe = {"kind": "all", "screener": {"condition": _and_conds(val_conds)}}
        else:
            universe = {"kind": "all"}
        # 근거 투명: composite 구성 팩터를 결과 표시 컬럼에 포함(중복 제거)
        display = list(dict.fromkeys(list(tool_input.get("display") or [])
                                     + [r.split(".")[-1] for r in refs]))
        select = {"top_n": int(tool_input["top_n"]), "descending": descending, "display": display}
        if tool_input.get("group_by"):
            select["group_by"] = str(tool_input["group_by"])
        return {"universe": universe, "signal": signal, "query": "select", "select": select}
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


def _manifest(dataset: dict):
    """챗 경로용 데이터 매니페스트 — IR 라우터(ir.py)와 동일하게 무결성 4액션 게이트와
    필드 커버리지(null≠0)를 가동한다. 빈 데이터셋이면 None(엔진이 단일 검증경로로 처리)."""
    return build_dataset_manifest(dataset) if dataset else None


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
    res = strategy_from_spec(ir, dataset, manifest=_manifest(dataset))
    if isinstance(res, dict) and res.get("success"):
        res, _ = serialize_ir_result(res)        # pandas→JSON(백테스트 영속/렌더) — 추가필드 前 직렬화
        res["ir"] = ir
        res["adjustable"] = param_manifest(ir)   # 실시간 변수조정 노브(웹 '변수 조정' 패널)
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
        return run_inspect(tool_input)   # 원시 시계열 dump — IR 없음(엑셀 증빙 대상 아님)
    if tool_name == "research_news":      # 뉴스 리서치 — 엔진 우회(수집+본문+Haiku 다이제스트)
        from .news_research import research_news
        return research_news(tool_input.get("queries") or [],
                             tool_input.get("period") or {"kind": "recent", "days": 7},
                             int(tool_input.get("max_articles") or 8),
                             str(tool_input.get("depth") or "full"))
    try:
        ir = assemble_ir(tool_name, tool_input)
    except (ValueError, KeyError, TypeError) as e:
        return {"success": False, "error": f"도구 입력 오류({tool_name}): {e}"}
    dataset = _load_dataset(ir)
    res = strategy_from_spec(ir, dataset, manifest=_manifest(dataset))   # 게이트·커버리지 가동
    # 결과에 IR + 조정가능 변수 동봉 → 챗 결과뷰의 '엑셀로 내보내기'(증빙)·'변수 조정'(실시간 재실행).
    if isinstance(res, dict) and res.get("success"):
        res, _ = serialize_ir_result(res)        # 모든 챗 도구 결과를 JSON-안전하게(부류 가드)
        res["ir"] = ir
        res["adjustable"] = param_manifest(ir)
    return res


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


# ── 변수 조정 재실행 (①명세 — nl 재컴파일 대신 IR 핸들 필드 수정) ──────────────

def _set_path(d: dict, path: str, value) -> None:
    """점경로로 중첩 dict에 값 설정 (web ParamControls setPath의 py 대응)."""
    cur = d
    keys = path.split(".")
    for k in keys[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    cur[keys[-1]] = value


def _coerce(entry: dict, value) -> object:
    """매니페스트 항목 타입으로 값 강제 + 범위/옵션 검증. 잘못된 값 → ValueError."""
    typ = entry.get("type")
    if typ == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "yes", "참")
    if typ == "select":
        sval = str(value)
        opts = entry.get("options") or []
        if opts and sval not in opts:
            raise ValueError(f"'{entry['path']}'는 {opts} 중 하나라야 합니다(받음: {sval}).")
        return sval
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"'{entry['path']}'는 숫자라야 합니다(받음: {value}).")
    if entry.get("min") is not None:
        num = max(num, float(entry["min"]))
    if entry.get("max") is not None:
        num = min(num, float(entry["max"]))
    base = entry.get("value")
    return int(num) if isinstance(base, int) and not isinstance(base, bool) else num


def run_adjust(session, conversation_id, tool_input: dict) -> dict:
    """직전 simulate의 검증 IR에서 '값만' 바꿔 재실행(재컴파일 0·토큰 0). ①명세 근본수정.

    nl 재서술→재컴파일의 비결정 발산(같은 의도가 다른 IR·다른 수치로)을 끊는다. 조정 가능한
    필드는 param_manifest(SSOT)가 정의 — 그 외 경로는 거부. 실패는 예외 대신 모델 피드백.
    """
    ir = _last_simulate_ir(session, conversation_id)
    if ir is None:
        return {"success": False, "error": "조정할 직전 분석이 없습니다. 먼저 simulate로 분석하세요."}
    ir = copy.deepcopy(ir)               # 저장된 결과 IR 공유객체 변형 방지
    changes = tool_input.get("changes") or []
    if not changes:
        return {"success": False, "error": "adjust_analysis: changes(바꿀 필드)가 필요합니다."}
    by_path = {p["path"]: p for p in param_manifest(ir)}
    applied = []
    for ch in changes:
        path = str((ch or {}).get("path") or "")
        if path not in by_path:
            return {"success": False, "error": f"조정 불가 필드: '{path}'. 가능: {sorted(by_path)}"}
        try:
            val = _coerce(by_path[path], ch.get("value"))
        except ValueError as e:
            return {"success": False, "error": str(e)}
        _set_path(ir, path, val)
        applied.append(f"{path}={val}")
    try:
        StrategyIR.model_validate(ir)                  # 조정 후 유효성 재검(부류 가드)
    except ValidationError as e:
        return {"success": False, "error": f"조정된 IR이 유효하지 않습니다: {e}"}
    dataset = _load_dataset(ir)
    res = strategy_from_spec(ir, dataset, manifest=_manifest(dataset))
    if isinstance(res, dict) and res.get("success"):
        res, _ = serialize_ir_result(res)        # pandas→JSON(조정 재실행도 백테스트일 수 있음)
        res["ir"] = ir
        res["adjustable"] = param_manifest(ir)
        res["adjusted"] = applied
    return res


# ── compact 요약 ──────────────────────────────────────────────────────────────

def compact_summary(tool_name: str, result: dict) -> str:
    """full 엔진 결과 → 모델 컨텍스트용 **형상 파생** 요약(summarize_result). 숫자는 결과에서만.

    ②관측 근본수정: 도구이름이 아니라 result_shape로 분기해 simulate의 *모든* 분석형상
    (연도별·파라미터별·팩터별·이벤트 등)의 **분할 결과를 모델이 한 번에** 보게 한다 —
    이전엔 simulate를 4스칼라로만 줘 모델이 buckets를 못 보고 재실행하던 헛돌이의 근본.
    save_strategy(저장 카드)만 엔진 결과형상이 아니라 별도 처리.
    """
    if not isinstance(result, dict):
        return f"[{tool_name}] 완료"
    if not result.get("success", True):
        return f"[{tool_name} 실패] {result.get('error', '알 수 없는 오류')}"
    if tool_name == "save_strategy" or result.get("strategy_id") is not None:
        return (f"[save_strategy] '{result.get('name')}' 전략을 draft로 저장(id={result.get('strategy_id')}). "
                "모의/실전은 웹 자동매매 메뉴에서.")
    return _status_header(result) + summarize_result(result)


def _status_header(result: dict) -> str:
    """결과 품질 계약(status/verdict)을 모델 식단 **맨 앞**에 노출 — 모델이 '손실로 0%'와
    '거래가 없어 0%'를 구분하고, 빈/퇴화/불가를 맹목 재실행하지 않게 한다(뿌리 R1·R3).
    status가 없거나 ok면 verdict(저신뢰 주의 등)만, 아니면 경고 헤더를 붙인다."""
    status = result.get("status")
    verdict = (result.get("verdict") or "").strip()
    if status and status != "ok":
        return (f"⚠ 결과상태={status}: {verdict}\n"
                "(이 결과는 유효한 분석이 아닙니다. 같은 분석을 재실행하지 말고, 위 사유를 사용자에게 "
                "정직히 설명하고 구체적 조정안 1개를 제안하세요.)\n")
    return f"[참고: {verdict}]\n" if verdict else ""

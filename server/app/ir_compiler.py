"""자연어 전략 설명 → StrategyIR 컴파일러 (Anthropic Claude).

설계(프롬프트 엔지니어링 베스트프랙티스):
  - 역할·정적 지식을 XML 섹션으로 구조화한 시스템 프롬프트(프롬프트 캐싱).
  - **단계별 추론 강제**: emit_strategy 도구의 필드 순서가 곧 추론 순서다
    (intent_summary → strategy_archetype → mapping_rationale → strategy → ...).
    "번역 전에 이해"하게 만들어, 메커니즘/수식 설명도 그 이면의 매매 의도로 환원한다.
  - **능력(capability) 의미 명세**(capability_spec): 각 프리미티브가 '무엇을 달성하고 어떤
    전략에 쓰는가'를 줘서, '목표 베타·상수 레버리지·종가 리밸런싱'을 always+leverage로
    일반적으로 매핑한다(단건 few-shot 땜질 아님).
  - 내부 validate→repair 루프(유저 명료화 없음). 검증기가 실행불가 IR을 구조적으로 차단.

키(ANTHROPIC_API_KEY) 미설정 시 success=False 사유 반환 — 다른 기능엔 영향 없음.
"""
from __future__ import annotations

import json
from typing import Callable

from .config import settings

# ── few-shot: 단계별 추론 형식을 보여주는 검증된 예시 (특정 답 암기가 아니라 '과정'을 가르침) ──
_FEWSHOT = [
    {
        "nl": "삼성전자를 종가가 60일 이동평균 위로 올라오면 매수하고 15% 익절 7% 손절",
        "out": {
            "intent_summary": "삼성전자 단일 종목. 종가가 60일 이동평균을 상향 돌파한 날 매수, 15% 익절 또는 7% 손절 시 청산.",
            "strategy_archetype": "이벤트룰",
            "mapping_rationale": "단일 종목→universe.single. '돌파한 날 매수'=이벤트→entry.on_signal. 신호=compare(종가 > ts_mean(종가,60)). 익절/손절→exit.take_profit=15/stop_loss=-7.",
            "strategy": {
                "name": "삼성전자 60일선 돌파",
                "universe": {"kind": "single", "symbols": ["005930"]},
                "signal": {"op": "compare", "params": {"op": ">"}, "inputs": {
                    "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                    "right": {"op": "ts_mean", "params": {"window": 60}, "inputs": {
                        "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}}}},
                "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                             "entry": {"mode": "on_signal"},
                             "exit": {"take_profit": 15, "stop_loss": -7}},
                "simulation": {"initial_capital": 10000000, "fill": "next_open"},
            },
            "assumptions": ["'60일 이동평균'을 종가 60일 단순이동평균으로 해석"],
            "expressible": True,
        },
    },
    {
        "nl": "KODEX 200을 노출이 순자산의 2배가 되도록 매일 맞추며 계속 보유",
        "out": {
            "intent_summary": "KODEX200(069500)을 항상 보유하되, 노출을 순자산의 2배로 매일 유지(상승하면 더 사고 하락하면 줄임).",
            "strategy_archetype": "상수레버리지·상시보유",
            "mapping_rationale": "'노출을 순자산의 N배로 매일 유지'=entry.always(매일 리밸런싱)+simulation.leverage=2(노출=leverage×순자산). 매일 종가 기준 조정→fill=close. 계속 보유→신호는 항상 참(compare(종가>0)). exit는 always에서 무시되므로 비움.",
            "strategy": {
                "name": "KODEX200 2배 상수 레버리지",
                "universe": {"kind": "single", "symbols": ["069500"]},
                "signal": {"op": "compare", "params": {"op": ">"}, "inputs": {
                    "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                    "right": {"op": "const", "params": {"value": 0}}}},
                "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                             "entry": {"mode": "always"}, "exit": {}},
                "simulation": {"initial_capital": 10000000, "fill": "close", "leverage": 2},
            },
            "assumptions": ["'2배'를 시뮬레이션 레버리지 2로 해석", "신호는 항상 참(종가>0)으로 매일 보유 유지"],
            "expressible": True,
        },
    },
    {
        "nl": "KODEX200을 종가가 N일 이동평균 위로 올라오면 매수, 이동평균 기간(10·20·60)과 보유일(20·40)을 바꿔가며 성과를 한눈에 비교",
        "out": {
            "intent_summary": "KODEX200(069500) 단일. 종가가 N일 이동평균 상향 돌파 시 매수. 이동평균 기간과 보유일을 격자로 펼쳐 성과 비교.",
            "strategy_archetype": "파라미터 펼침(민감도)",
            "mapping_rationale": "단일→universe.single. 'N일선 위로 올라오면 매수'=신호 compare(종가>ts_mean(종가,N))+entry.on_signal. '바꿔가며 한눈에 비교'=sweep.axis=parameter. 두 변수→param_grid 두 축. **각 축은 {path:점경로, values:[값들]}** — 항목을 이름으로 감싸지 않는다. 신호의 기간은 ts_mean 블록의 window 경로, 보유일은 position.exit.hold_days 경로. 2축이면 데카르트곱.",
            "strategy": {
                "name": "KODEX200 이동평균 돌파 민감도",
                "universe": {"kind": "single", "symbols": ["069500"]},
                "signal": {"op": "compare", "params": {"op": ">"}, "inputs": {
                    "left": {"op": "data", "params": {"ref": "__SELF__.Close"}},
                    "right": {"op": "ts_mean", "params": {"window": 20}, "inputs": {
                        "signal": {"op": "data", "params": {"ref": "__SELF__.Close"}}}}}},
                "position": {"direction": "long", "sizing": {"mode": "equal_weight"},
                             "entry": {"mode": "on_signal"}, "exit": {"hold_days": 20}},
                "simulation": {"initial_capital": 10000000, "fill": "next_open"},
                "sweep": {"axis": "parameter", "param_grid": [
                    {"path": "signal.inputs.right.params.window", "values": [10, 20, 60]},
                    {"path": "position.exit.hold_days", "values": [20, 40]}]},
            },
            "assumptions": ["이동평균 기간 N을 [10,20,60], 보유일을 [20,40]으로 격자 해석"],
            "expressible": True,
        },
    },
]


def _param_text(p: dict) -> str:
    name = p.get("name", "?")
    opts = p.get("options")
    if opts:
        return f'{name}={"|".join(str(o) for o in opts)}'
    if p.get("default") is not None:
        return f'{name}(기본 {p["default"]})'
    return name


def _catalog_text(catalog: list[dict]) -> str:
    lines = []
    for b in catalog:
        parts = [f'{b["op"]}', f'[{b.get("label","")}]', f'→{b.get("out_type","")}']
        slots = b.get("slots") or {}
        if slots:
            parts.append("slots=" + ",".join(f"{k}:{v}" for k, v in slots.items()))
        params = b.get("params") or []
        if params:
            parts.append("params={" + ", ".join(
                _param_text(p) for p in params if isinstance(p, dict)) + "}")
        if b.get("variadic"):
            parts.append("(가변입력)")
        doc = (b.get("doc") or "").strip().splitlines()
        if doc:
            parts.append("— " + doc[0])
        lines.append("  " + " ".join(parts))
    return "\n".join(lines)


def _cap_item(it: dict) -> str:
    v = it.get("value") or it.get("field") or ""
    s = f'    - {v}: {it.get("does", "")}'
    if it.get("use_for"):
        s += f'  〔쓰임: {it["use_for"]}〕'
    return s


def _capabilities_text(caps: dict) -> str:
    out = []
    for cat, val in caps.items():
        out.append(f'  [{cat}]')
        if isinstance(val, list):
            out += [_cap_item(it) for it in val]
        elif isinstance(val, dict):
            out.append(_cap_item(val))
            for k, d in (val.get("knobs") or {}).items():
                out.append(f'        · {k}: {d}')
        else:
            out.append(f'    {val}')
    return "\n".join(out)


def _system_prompt(catalog: list[dict], capabilities: dict, indicator_cols: list[str]) -> str:
    examples = "\n\n".join(
        f"<example>\n입력: {ex['nl']}\nemit_strategy 인자:\n{json.dumps(ex['out'], ensure_ascii=False, indent=1)}\n</example>"
        for ex in _FEWSHOT
    )
    return f"""<role>
너는 한국어 투자전략 설명을 백테스트 IR(StrategyIR JSON)로 변환하는 결정론적 컴파일러다.
오직 emit_strategy 도구로만 결과를 제출한다. 아래 <capabilities>·<block_catalog>·<reference_data>에
없는 진입모드·op·지표·필드를 지어내지 않는다. 출력 언어(설명·가정)는 한국어.
</role>

<ir_structure>
StrategyIR = {{
  "name": str,
  "universe": {{"kind": "single|list|all", "symbols": [..], "screener": {{"condition": <블록>, "refresh": "each_rebalance|once_at_start"}}}},
  "signal": <블록트리>,          // 신호. {{op, params, inputs:{{slot: 자식블록}}}} 재귀. 잎: data{{ref}}, const{{value}}
  "position": {{"direction":.., "sizing":{{"mode":..}}, "entry":{{"mode":..}}, "exit":{{..}}, "overlays":{{..}}}},
  "simulation": {{"initial_capital":.., "fill":.., "leverage":.., "start":"YYYY-MM-DD", "end":"YYYY-MM-DD", ...}}
}}
종목 자신의 컬럼은 ref에 "__SELF__." 접두(예 "__SELF__.Close"). 신호 out_type: condition(룰)·score(팩터)·value·label.
</ir_structure>

<capabilities>
각 구성요소가 '무엇을 하는가(does)'와 '어떤 전략 의도에 쓰는가(쓰임)'. **의도→프리미티브 매핑의 1차 근거.**
{_capabilities_text(capabilities)}
</capabilities>

<block_catalog>
신호 블록(op) 목록 — 이것만 사용:
{_catalog_text(catalog)}
</block_catalog>

<reference_data>
지표 ref(이 외 임의 지표 금지; 종목 자신은 __SELF__. 접두): {", ".join(indicator_cols)}
기본 OHLCV: Open, High, Low, Close, Volume
종목 표기: 국내주식=6자리 코드(삼성전자 005930), 미국주식=티커(AAPL), 내장 자산명=정확한 키
(S&P500, 코스피200선물, 원유선물, 금선물, 은선물(COMEX), 천연가스선물, 나스닥선물, 비트코인선물 등). 모르면 사용자가 쓴 명칭 그대로.
</reference_data>

<idioms>
원자(블록)만으로는 안 보이는 **검증된 합성 레시피**. 의도가 아래 패턴에 해당하면 그대로 따르고,
이 목록을 먼저 대조한 뒤에야 '표현 불가'나 '단순화'를 판단한다(원자는 있어도 합성을 못 찾으면 안 됨).

1. [조건 기반 롱/숏/중립] "A 규칙이면 롱, (다른) B 규칙이면 숏, 아니면 미보유"처럼 롱·숏에 *서로 다른 조건*.
   → long_short는 단일 score의 부호로 방향을 가르므로, select로 **부호 점수**를 만든다:
     signal = select(cond=A, a=const(1), b=select(cond=B, a=const(-1), b=const(0)))
     position.direction="long_short", entry.mode="scheduled"(rebalance="daily"), entry.threshold=0.
   부호>0=롱·<0=숏·=0=미보유(중립밴드 자동). A·B 안의 임계 const를 param_grid 두 축으로 독립 스윕 가능.
   (※ on_signal+condition은 단방향 전용. 서로 다른 조건의 양방향은 반드시 이 부호점수+long_short 경로.)
2. [시계열 모멘텀(TSMOM) 롱숏] "추세가 양이면 롱, 음이면 숏" → signal=score(예: ts_delta(Close,N)),
   direction="long_short", entry.threshold=0. 부호가 곧 방향(중립=정확히 0).
3. [정기 리밸런스 팩터(횡단)] "매월/매주 ___ 상위 N(또는 X%) 보유" → universe.kind=all(또는 list+세부조건),
   signal=score(팩터), entry.mode="scheduled"+rebalance, top_n 또는 top_pct. 롱숏이면 부호/순위로 양다리.
   "거래대금·시총·밸류 등으로 선별한 종목에서"처럼 자격 필터가 붙으면 universe.screener={{condition, refresh}}로 2차 선별.
4. [국면별 비교] "상승장/하락장 등 국면에 따라 신호·성과가 어떻게 다른가" → 신호 대수는 그대로 두고
   sweep.target="signal"(또는 "relation") + label 블록(국면 라벨)으로 분리. axis는 바꾸지 않는다.
5. [조건 지속/최근 발생] "N일 연속 충족"·"최근 M일 내 발생" → condition을 modifier 블록으로 감싼다.
6. [선물 디렉셔널·추세추종] 선물 심볼(원유선물·금선물·나스닥선물·은선물(COMEX)·천연가스선물·
   비트코인선물)은 카탈로그가 승수·증거금·통화를 알아 엔진이 자동 인식 — IR엔
   (※ "코스피200선물"은 현재 ETF 프록시라 주식으로 백테스트됨 — 위 선물 목록에서 제외) —
   자산클래스 표시 불필요, 종목처럼 universe.symbols에 이름만 넣는다(주식과 같은 신호·청산 어휘).
   · ⚠ 백테스트 지원 경로는 **추세추종(always)·정기(scheduled)뿐** — 선물 + on_signal(이벤트 단발)은
     엔진이 아직 차단(증거금 회계 미구현). 따라서 선물은 on_signal을 쓰지 않는다.
   · "선물이 ___면 롱/숏" 같은 지속 레짐: single + entry.mode="always"(보유 마스크, 신호=참인 동안 보유)
     또는 entry.mode="scheduled"(daily). 양방향이면 부호점수+long_short(레시피 1·2, scheduled daily).
   · take_profit/stop_loss(%)·hold_days를 쓰려면 scheduled(daily) 경로로(always는 청산 규칙 무시).
     순수 단발 이벤트 진입이 핵심 의도면, 현재 선물은 그 경로가 백테스트 미지원임을 assumptions에 밝힌다.
   · 숏은 direction="short"(선물은 차입 불필요, 대칭). 레버리지는 증거금으로 내재 → leverage=1 기본
     (명목 노출을 더 키울 때만 >1).
   · ⚠ roll_method·series_adjust·roll_cost_pct·account_currency는 **현재 엔진 미적용(예약)** — 채우지
     말 것(단일 연속 시계열·단일통화 가정). 사용자가 강하게 명시하면 채우되 "현재 미적용"을 assumptions에 명시.
</idioms>

<process>
emit_strategy를 호출하되, 인자 필드를 반드시 아래 순서로 채워 추론한다(순서가 곧 사고 과정):
1. intent_summary: 입력을 '무엇을·언제·어떻게 사고팔까'의 평문 전략으로 한두 문장 재진술.
   ⚠ 입력이 상품의 작동원리·수식·메커니즘 설명이어도, 그것을 '그 상품을 복제하는 매매 주문'으로
      환원한다(예: 레버리지 ETF의 일일 리밸런싱 수식 → "노출을 순자산의 2배로 매일 유지").
2. strategy_archetype: 이벤트룰 / 정기리밸런싱팩터 / 상수레버리지·상시보유 / 스크리너선별 / 롱숏 등.
3. mapping_rationale: intent의 각 요소를 <capabilities>·<idioms>의 어느 것으로 매핑했는지 근거.
   ⚠ expressible=false 또는 *단순화*(한쪽 다리 드롭 등)를 결론짓기 전에 반드시 <idioms> 쿡북과
   <capabilities> '쓰임'을 대조한다. 특히 양방향(롱/숏)·중립밴드·국면조건부는 <idioms>에 합성 레시피가
   있으므로 단방향으로 축소하지 말 것.
4. strategy: 위 매핑을 StrategyIR JSON으로.
5. assumptions: 모호하게 해석한 부분 + **결과에 영향 주는 단순화·생략을 반드시 명시**(예: 한쪽 다리 드롭,
   임계 임의 선정, 이벤트→레벨 근사). 조용히 줄이지 말고 그 사실을 한 줄로 적는다.
6. expressible: <capabilities>·<idioms> 조합으로도 매핑 경로가 없을 때만 false(이때 strategy는 {{}}).
   매핑 경로가 하나라도 있으면 반드시 true. (안전망: 검증기가 잘못된 IR을 거르고 유저가 최종 확인하므로,
   "근사 가능하면" 표현을 시도하라 — 섣불리 표현불가로 포기하지 말 것.)
</process>

<rules>
- 검증 실패 tool_result를 받으면, 그 오류만 고쳐 emit_strategy를 다시 호출한다(유효 op·지표 ref·구조만).
- 단일 종목 상시보유의 신호는 항상 참 condition(compare(종가>0))으로 둔다.
- 청산 규칙(exit)은 on_signal·scheduled에만 의미; always(상시)에선 무시되므로 비운다.
- '종가 부근/종가에' 체결 = simulation.fill="close".
</rules>

<examples>
{examples}
</examples>"""


_EMIT_TOOL = {
    "name": "emit_strategy",
    "description": "단계별 추론(intent→archetype→mapping)을 거쳐 컴파일된 StrategyIR을 제출한다. "
                   "검증 실패 tool_result를 받으면 오류를 고쳐 다시 호출.",
    "input_schema": {
        "type": "object",
        "properties": {
            "intent_summary": {"type": "string",
                               "description": "1단계: 입력을 '무엇을·언제·어떻게 사고팔까'의 평문 전략으로 재진술. 메커니즘/수식 설명이면 그 이면의 매매 의도로 환원."},
            "strategy_archetype": {"type": "string",
                                   "description": "2단계: 전략 유형(이벤트룰/정기리밸런싱팩터/상수레버리지·상시보유/스크리너선별/롱숏 등)."},
            "mapping_rationale": {"type": "string",
                                  "description": "3단계: intent 각 요소를 capabilities의 어느 구성요소로 매핑했는지 근거. 표현불가 결론 전 capabilities '쓰임'과 대조."},
            "strategy": {"type": "object",
                         "description": "4단계: 매핑을 StrategyIR JSON으로. expressible=false면 빈 객체."},
            "assumptions": {"type": "array", "items": {"type": "string"},
                            "description": "5단계: 모호하게 해석한 부분(한국어)."},
            "expressible": {"type": "boolean",
                            "description": "6단계: capabilities 조합으로도 매핑 경로가 없을 때만 false. 경로가 있으면 반드시 true."},
        },
        "required": ["intent_summary", "strategy_archetype", "mapping_rationale",
                     "strategy", "assumptions", "expressible"],
    },
}


def _resolve_symbols(strat: dict, valid_keys: set[str], name_map: dict[str, str]) -> dict:
    """universe.symbols의 이름→데이터셋 키 해석. 유효 키면 그대로, 이름이면 name_map으로,
    둘 다 아니면 원문 유지(검증기가 R0로 잡음)."""
    u = strat.get("universe")
    if isinstance(u, dict) and isinstance(u.get("symbols"), list):
        u["symbols"] = [
            s if (s := str(x).strip()) in valid_keys else name_map.get(s.lower(), s)
            for x in u["symbols"]
        ]
    return strat


def compile_nl(
    nl: str,
    *,
    catalog: list[dict],
    capabilities: dict,
    indicator_cols: list[str],
    valid_keys: set[str],
    name_map: dict[str, str],
    validate_fn: Callable[[dict], tuple[list[dict], bool]],
    max_repairs: int = 2,
) -> dict:
    """자연어 → StrategyIR. validate_fn(strategy_dict)->(issues, ok). 내부 수리 루프.

    반환: {success, ir, assumptions, issues, repair_count, error?}.
    """
    if not settings.ANTHROPIC_API_KEY:
        return {"success": False, "error": "ANTHROPIC_API_KEY가 설정되지 않았습니다 — 서버 환경변수에 키를 설정하세요.",
                "ir": {}, "assumptions": [], "issues": [], "repair_count": 0}
    try:
        import anthropic
    except ImportError:
        return {"success": False, "error": "anthropic 패키지가 설치되지 않았습니다 (pip install anthropic).",
                "ir": {}, "assumptions": [], "issues": [], "repair_count": 0}

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    # 정적·대형 시스템 프롬프트는 프롬프트 캐싱(ephemeral)으로 호출당 비용·지연 절감.
    system = [{"type": "text",
               "text": _system_prompt(catalog, capabilities, indicator_cols),
               "cache_control": {"type": "ephemeral"}}]
    messages: list[dict] = [{"role": "user", "content": f"다음 전략을 컴파일해줘:\n\n{nl}"}]
    last: dict = {"ir": {}, "assumptions": [], "issues": [], "repair_count": 0}

    for attempt in range(max_repairs + 1):
        try:
            resp = client.messages.create(
                model=settings.NL_COMPILE_MODEL, max_tokens=4096, system=system,
                tools=[_EMIT_TOOL], tool_choice={"type": "tool", "name": "emit_strategy"},
                messages=messages)
        except Exception as e:  # noqa: BLE001 — 외부 API 실패는 사유 반환
            return {"success": False, "error": f"LLM 호출 실패: {type(e).__name__}: {e}", **last}

        tu = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if tu is None:
            return {"success": False, "error": "LLM이 emit_strategy를 호출하지 않았습니다.", **last}

        inp = dict(tu.input or {})
        assumptions = inp.get("assumptions") or []
        if inp.get("expressible") is False:
            return {"success": False, "error": "이 전략은 현재 IR 구조로 표현할 수 없습니다.",
                    "ir": {}, "assumptions": assumptions, "issues": [], "repair_count": attempt}

        strat = _resolve_symbols(dict(inp.get("strategy") or {}), valid_keys, name_map)
        issues, ok = validate_fn(strat)
        last = {"ir": strat, "assumptions": assumptions, "issues": issues, "repair_count": attempt}
        if ok:
            return {"success": True, **last}
        if attempt == max_repairs:
            break

        errs = [i for i in issues if i.get("is_error")]
        messages.append({"role": "assistant",
                         "content": [{"type": "tool_use", "id": tu.id, "name": tu.name, "input": inp}]})
        messages.append({"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tu.id,
            "content": ("검증 실패. 아래 오류를 모두 고쳐 emit_strategy를 다시 호출해줘"
                        "(유효한 op·지표 ref·구조만 사용):\n"
                        + json.dumps(errs, ensure_ascii=False))}]})

    return {"success": False, "error": "검증을 통과하는 IR을 생성하지 못했습니다.", **last}
